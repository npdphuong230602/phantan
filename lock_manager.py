# lock_manager.py
# ═══════════════════════════════════════════════════════════
# 2-Phase Locking (2PL) Concurrency Control
# ═══════════════════════════════════════════════════════════
#
# Implements Strict 2-Phase Locking (S2PL):
#   - Growing phase: acquire locks as needed
#   - Shrinking phase: release ALL locks at commit/abort
#   - Deadlock detection via DFS on wait-for graph
#
# Lock compatibility matrix:
#   S + S = COMPATIBLE     (multiple readers allowed)
#   S + X = INCOMPATIBLE   (writer blocks readers)
#   X + S = INCOMPATIBLE   (reader blocks writers)
#   X + X = INCOMPATIBLE   (only one writer at a time)
# ═══════════════════════════════════════════════════════════

import threading
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class LockManager:
    """Implements Strict 2-Phase Locking (S2PL) protocol.

    In S2PL, transactions acquire locks as needed (growing phase)
    and release ALL locks only at commit/abort time (shrinking phase).
    This prevents cascading aborts and ensures serializability.

    Attributes:
        lock_table: Maps item_id to lock info (type and holders).
        wait_for_graph: Directed graph for deadlock detection.
    """

    def __init__(self):
        """Initialize the lock manager with empty lock table and wait-for graph."""
        # lock_table: {item_id: {"type": "S"/"X", "holders": set(txn_ids)}}
        # Stores the current lock state for each item
        self.lock_table = {}

        # wait_for_graph: {txn_id: set(txn_ids_it_waits_for)}
        # Used for deadlock detection — a cycle means deadlock
        self.wait_for_graph = {}

        # Master lock to protect all shared state
        # Every method that reads/writes lock_table or wait_for_graph
        # must hold this mutex
        self._mutex = threading.RLock()

        # Condition variables per item — threads block here waiting for locks
        # Using per-item conditions avoids unnecessary wake-ups
        self._conditions = {}  # {item_id: threading.Condition}

        # Track which locks each transaction holds for efficient release
        self._txn_locks = {}  # {txn_id: [(item_id, lock_type), ...]}

        # Set of transactions aborted by deadlock resolution
        self._aborted = set()

        # Timeout for lock acquisition — prevents indefinite blocking
        # Set to 0.05s to prevent exhausting the limited Flask thread pool
        # during high contention (which causes network timeouts).
        self.LOCK_TIMEOUT = 0.05

    def acquire_lock(self, txn_id, item_id, lock_type):
        """Acquire a lock on an item for a transaction.

        Implements the growing phase of 2PL. If the lock cannot be
        granted immediately (due to incompatible existing locks),
        the transaction waits on a condition variable.

        During waiting, periodically checks for deadlocks.

        Args:
            txn_id: Transaction identifier (string or int).
            item_id: The item to lock (integer).
            lock_type: 'S' for shared (read) or 'X' for exclusive (write).

        Returns:
            True if lock was granted successfully.
            False if transaction was aborted (deadlock victim or timeout).
        """
        with self._mutex:
            # If this transaction was already aborted (e.g., as a deadlock victim
            # in another thread), reject immediately
            if txn_id in self._aborted:
                return False

            # Lazily create a condition variable for this item
            # Condition uses _mutex as its underlying lock
            if item_id not in self._conditions:
                self._conditions[item_id] = threading.Condition(self._mutex)

            start_time = time.time()

            while True:
                # Re-check abort status — may have been set while we were waiting
                if txn_id in self._aborted:
                    self._cleanup_wait(txn_id)
                    return False

                # Check if the lock can be granted based on compatibility matrix
                if self._can_grant(txn_id, item_id, lock_type):
                    self._grant_lock(txn_id, item_id, lock_type)
                    self._cleanup_wait(txn_id)
                    return True

                # Lock cannot be granted — we must wait
                # First, update the wait-for graph so deadlock detection works
                holders = self.lock_table.get(item_id, {}).get('holders', set())
                if txn_id not in self.wait_for_graph:
                    self.wait_for_graph[txn_id] = set()
                # This txn waits for all current holders (except itself)
                self.wait_for_graph[txn_id] = holders - {txn_id}

                # Run deadlock detection
                cycle = self.detect_deadlock()
                if cycle:
                    victim = self._resolve_deadlock(cycle)
                    if victim == txn_id:
                        # We are the victim — abort
                        return False
                    # Another txn was aborted — we just fall through and wait
                    # for it to clean up and notify us.

                # No deadlock — wait with timeout
                elapsed = time.time() - start_time
                remaining = self.LOCK_TIMEOUT - elapsed
                if remaining <= 0:
                    # Timeout — abort self to prevent starvation
                    logger.warning(
                        f"Transaction {txn_id} timed out waiting for "
                        f"{lock_type} lock on item {item_id}"
                    )
                    self._aborted.add(txn_id)
                    self._cleanup_wait(txn_id)
                    self.release_locks(txn_id)
                    return False

                # Wait on the item's condition variable
                # Use short timeout (0.5s) to periodically re-check for deadlocks
                self._conditions[item_id].wait(timeout=min(0.5, remaining))

    def _can_grant(self, txn_id, item_id, lock_type):
        """Check if a lock can be granted based on the compatibility matrix.

        Lock compatibility:
            Request S when held S → COMPATIBLE (multiple readers OK)
            Request S when held X → INCOMPATIBLE
            Request X when held S → INCOMPATIBLE (unless sole holder = upgrade)
            Request X when held X → INCOMPATIBLE (unless same txn)

        Args:
            txn_id: Transaction requesting the lock.
            item_id: Item to be locked.
            lock_type: 'S' or 'X'.

        Returns:
            True if the lock can be granted immediately.
        """
        if item_id not in self.lock_table:
            return True  # No existing lock — always grantable

        entry = self.lock_table[item_id]
        holders = entry['holders']
        current_type = entry['type']

        if not holders:
            return True  # Lock entry exists but empty — grantable

        # If this transaction is the sole holder, any request is OK
        # This handles lock upgrades (S → X) when no other holders
        if holders == {txn_id}:
            return True

        # Transaction already holds a shared lock
        if txn_id in holders:
            if lock_type == 'S' and current_type == 'S':
                return True  # Already holding compatible lock
            if lock_type == 'X' and current_type == 'S' and len(holders) > 1:
                # Want to upgrade S→X but others also hold S — must wait
                return False

        # Apply compatibility matrix for new lock requests
        if current_type == 'S' and lock_type == 'S':
            return True  # S + S = COMPATIBLE

        # All other combinations: S+X, X+S, X+X = INCOMPATIBLE
        return False

    def _grant_lock(self, txn_id, item_id, lock_type):
        """Grant the lock — update lock_table and per-transaction tracking.

        Args:
            txn_id: Transaction receiving the lock.
            item_id: Item being locked.
            lock_type: 'S' or 'X'.
        """
        if item_id not in self.lock_table:
            self.lock_table[item_id] = {'type': lock_type, 'holders': set()}

        entry = self.lock_table[item_id]

        # Handle lock upgrade: if requesting X, change lock type to X
        if lock_type == 'X':
            entry['type'] = 'X'

        entry['holders'].add(txn_id)

        # Track in per-transaction lock list for efficient release
        if txn_id not in self._txn_locks:
            self._txn_locks[txn_id] = []
        self._txn_locks[txn_id].append((item_id, lock_type))

        logger.debug(f"Lock granted: txn={txn_id}, item={item_id}, type={lock_type}")

    def release_locks(self, txn_id):
        """Release ALL locks held by a transaction (shrinking phase of S2PL).

        Called at commit or abort time. After releasing, notifies all
        threads waiting on the released items so they can retry.

        This is the key property of Strict 2PL: locks are held until
        the transaction ends, preventing cascading aborts.

        Args:
            txn_id: Transaction whose locks to release.
        """
        with self._mutex:
            items_to_notify = []

            # Remove txn from all lock entries it holds
            for item_id, entry in list(self.lock_table.items()):
                if txn_id in entry['holders']:
                    entry['holders'].discard(txn_id)
                    items_to_notify.append(item_id)

                    # Clean up empty lock entries to save memory
                    if not entry['holders']:
                        del self.lock_table[item_id]

            # Clean up per-transaction tracking
            self._txn_locks.pop(txn_id, None)
            self._aborted.discard(txn_id)

            # Remove from wait-for graph (both as waiter and as target)
            self.wait_for_graph.pop(txn_id, None)
            for waiter in self.wait_for_graph:
                self.wait_for_graph[waiter].discard(txn_id)

            # Wake up all threads waiting on the released items
            # They will re-check if their lock can now be granted
            for item_id in items_to_notify:
                if item_id in self._conditions:
                    self._conditions[item_id].notify_all()

            logger.debug(f"All locks released for txn={txn_id}")

    def detect_deadlock(self):
        """Detect deadlock by finding a cycle in the wait-for graph using DFS.

        The wait-for graph is a directed graph where an edge from T1 → T2
        means T1 is waiting for a lock held by T2. A cycle in this graph
        indicates a deadlock.

        Uses standard DFS with recursion stack tracking.

        Returns:
            List of txn_ids forming a cycle if deadlock exists, None otherwise.
        """
        visited = set()
        rec_stack = set()  # Nodes in current DFS path
        path = []  # Actual path for cycle extraction

        def dfs(node):
            """Recursive DFS — returns cycle list if found, None otherwise."""
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in self.wait_for_graph.get(node, set()):
                if neighbor not in visited:
                    cycle = dfs(neighbor)
                    if cycle:
                        return cycle
                elif neighbor in rec_stack:
                    # Back edge found — we have a cycle
                    cycle_start = path.index(neighbor)
                    return path[cycle_start:]

            path.pop()
            rec_stack.discard(node)
            return None

        # Try DFS from each unvisited node
        for node in list(self.wait_for_graph.keys()):
            if node not in visited:
                cycle = dfs(node)
                if cycle:
                    logger.warning(f"Deadlock detected! Cycle: {cycle}")
                    return cycle

        return None

    def _resolve_deadlock(self, cycle):
        """Resolve deadlock by aborting the youngest transaction in the cycle.

        Heuristic: 'youngest' = largest txn_id (lexicographically for strings,
        numerically for ints). This assumes newer transactions have done less
        work, so aborting them wastes fewer resources.

        Args:
            cycle: List of txn_ids forming the deadlock cycle.

        Returns:
            The txn_id that was chosen as the deadlock victim.
        """
        # Choose the youngest (most recently started) as victim
        victim = max(cycle, key=lambda x: str(x))

        logger.warning(f"Deadlock resolved: aborting victim txn={victim}")
        self._aborted.add(victim)

        # Remove victim from wait-for graph
        self.wait_for_graph.pop(victim, None)
        
        # Release any locks the victim already holds
        self.release_locks(victim)

        # Wake up ALL waiting threads so the victim can see it's been aborted
        # and other waiters can re-check their lock requests
        for item_id in list(self._conditions.keys()):
            self._conditions[item_id].notify_all()

        return victim

    def _cleanup_wait(self, txn_id):
        """Remove a transaction from the wait-for graph.

        Called when a transaction is no longer waiting (either got the lock
        or was aborted).

        Args:
            txn_id: Transaction to remove from wait-for graph.
        """
        self.wait_for_graph.pop(txn_id, None)

    def is_aborted(self, txn_id):
        """Check if a transaction has been aborted (e.g., as deadlock victim).

        Args:
            txn_id: Transaction to check.

        Returns:
            True if the transaction was aborted.
        """
        with self._mutex:
            return txn_id in self._aborted

    def get_lock_info(self):
        """Return current lock table state for debugging/monitoring.

        Returns:
            dict: {item_id: {"type": str, "holders": list}} for all locked items.
        """
        with self._mutex:
            return {
                item_id: {
                    'type': entry['type'],
                    'holders': list(entry['holders'])
                }
                for item_id, entry in self.lock_table.items()
            }
