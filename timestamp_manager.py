# timestamp_manager.py
# ═══════════════════════════════════════════════════════════
# Timestamp Ordering (TO) Concurrency Control
# ═══════════════════════════════════════════════════════════
#
# Each transaction receives a unique timestamp at birth.
# Operations are validated against read/write timestamps:
#
# Read Rule:
#   If txn_ts < write_ts[item] → ABORT (too old to read)
#   Else → allow, update read_ts = max(read_ts, txn_ts)
#
# Write Rule (Thomas Write Rule):
#   If txn_ts < read_ts[item] → ABORT (too old to write)
#   If txn_ts < write_ts[item] → SKIP (obsolete write)
#   Else → allow, update write_ts
#
# On ABORT: restart with NEW (current) timestamp
# ═══════════════════════════════════════════════════════════

import threading
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TimestampManager:
    """Implements Timestamp Ordering (TO) concurrency control protocol.

    TO uses transaction timestamps to determine serialization order.
    Unlike 2PL which uses locks, TO never causes deadlocks because
    transactions are ordered by their birth time. However, TO may
    abort transactions that arrive 'too late' (their timestamp is
    older than the item's current read/write timestamp).

    The Thomas Write Rule optimization skips obsolete writes instead
    of aborting, reducing unnecessary restarts.

    Attributes:
        timestamp_table: Maps item_id to {read_ts, write_ts}.
        active_txns: Maps txn_id to its timestamp.
        stats: Counters for monitoring performance.
    """

    def __init__(self):
        """Initialize timestamp manager with empty timestamp table."""
        # timestamp_table: {item_id: {"read_ts": float, "write_ts": float}}
        # Tracks the largest timestamp that has successfully read/written each item
        self.timestamp_table = {}

        # Master lock for thread safety on all shared state
        self._mutex = threading.Lock()

        # Counter ensures unique timestamps even when time.time() returns
        # the same value for rapid successive calls
        self._ts_counter = 0
        self._ts_lock = threading.Lock()

        # Active transactions and their timestamps
        self.active_txns = {}  # {txn_id: timestamp}

        # Performance statistics for monitoring and benchmarking
        self.stats = {
            'total_reads': 0,
            'total_writes': 0,
            'aborted_reads': 0,      # Reads rejected by TO rules
            'aborted_writes': 0,     # Writes rejected by TO rules
            'thomas_skips': 0        # Writes skipped by Thomas Write Rule
        }

    def generate_timestamp(self):
        """Generate a unique, monotonically increasing timestamp.

        Uses time.time() as the base value but adds a tiny counter
        increment to handle sub-microsecond calls that might return
        the same wall-clock time.

        Returns:
            float: A unique timestamp guaranteed to be larger than
                   all previously generated timestamps.
        """
        with self._ts_lock:
            ts = time.time()
            self._ts_counter += 1
            # Add counter as sub-nanosecond fractional part
            # This ensures uniqueness without significantly changing the value
            return ts + (self._ts_counter * 1e-9)

    def begin_transaction(self, txn_id):
        """Register a new transaction with a fresh timestamp.

        The timestamp determines the transaction's position in the
        serialization order. Earlier timestamps = older transactions
        that should see an older snapshot of the data.

        Args:
            txn_id: Unique transaction identifier.

        Returns:
            float: The assigned timestamp for this transaction.
        """
        ts = self.generate_timestamp()
        with self._mutex:
            self.active_txns[txn_id] = ts
        logger.debug(f"Transaction {txn_id} started with timestamp {ts:.9f}")
        return ts

    def get_timestamp(self, txn_id):
        """Get the timestamp of an active transaction.

        Args:
            txn_id: Transaction to look up.

        Returns:
            float or None: The transaction's timestamp, or None if not found.
        """
        with self._mutex:
            return self.active_txns.get(txn_id)

    def _ensure_item_entry(self, item_id):
        """Ensure an item has an entry in the timestamp table.

        New items start with read_ts=0 and write_ts=0, meaning
        any transaction can initially read/write them without conflict.

        Args:
            item_id: Item to initialize if not present.
        """
        if item_id not in self.timestamp_table:
            self.timestamp_table[item_id] = {'read_ts': 0.0, 'write_ts': 0.0}

    def validate_read(self, txn_id, item_id):
        """Validate a read operation under Timestamp Ordering rules.

        Rule: If txn_ts < write_ts[item] → ABORT
        Reason: A 'younger' (later) transaction has already written this item.
                If we allow this read, the older transaction would see data
                written by a future transaction, violating timestamp order.

        If allowed: update read_ts = max(read_ts, txn_ts)
        This tells future write operations that a transaction with this
        timestamp has already read the item.

        Args:
            txn_id: Transaction performing the read.
            item_id: Item being read.

        Returns:
            tuple: (allowed: bool, reason: str)
                   allowed=True means read can proceed.
                   allowed=False means transaction must abort.
        """
        with self._mutex:
            txn_ts = self.active_txns.get(txn_id)
            if txn_ts is None:
                return False, "Transaction not found or already ended"

            self._ensure_item_entry(item_id)
            self.stats['total_reads'] += 1

            item_entry = self.timestamp_table[item_id]

            if txn_ts < item_entry['write_ts']:
                # ABORT: A younger transaction already wrote this item.
                # Reading now would violate the timestamp-based serialization
                # order because we'd be reading a "future" value.
                self.stats['aborted_reads'] += 1
                logger.debug(
                    f"READ ABORT: txn={txn_id} (ts={txn_ts:.6f}) < "
                    f"write_ts={item_entry['write_ts']:.6f} for item {item_id}"
                )
                return False, (
                    f"Read too late: txn_ts ({txn_ts:.4f}) < "
                    f"write_ts ({item_entry['write_ts']:.4f})"
                )

            # Read is allowed — update read timestamp
            # This records that a transaction with this timestamp has read
            # the item, which constrains future writes
            if txn_ts > item_entry['read_ts']:
                item_entry['read_ts'] = txn_ts

            return True, "OK"

    def validate_write(self, txn_id, item_id):
        """Validate a write operation under TO with Thomas Write Rule.

        Three possible outcomes:

        Rule 1 — ABORT: If txn_ts < read_ts[item]
            A younger transaction already read the old value of this item.
            If we write now, that read would have been incorrect because
            it should have seen our written value. Must abort.

        Rule 2 — SKIP (Thomas Write Rule): If txn_ts < write_ts[item]
            A younger transaction already wrote a newer value. Our write
            is obsolete — the newer write already supersedes it. We can
            safely skip this write without aborting. This optimization
            reduces the abort rate compared to basic TO.

        Rule 3 — ALLOW: Otherwise
            No conflicts. Write proceeds and we update write_ts.

        Args:
            txn_id: Transaction performing the write.
            item_id: Item being written.

        Returns:
            tuple: (result: str, reason: str)
                   result='ALLOW' — write can proceed, update write_ts
                   result='SKIP' — write ignored (Thomas rule), no abort needed
                   result='ABORT' — transaction must abort and restart
        """
        with self._mutex:
            txn_ts = self.active_txns.get(txn_id)
            if txn_ts is None:
                return 'ABORT', "Transaction not found or already ended"

            self._ensure_item_entry(item_id)
            self.stats['total_writes'] += 1

            item_entry = self.timestamp_table[item_id]

            # Rule 1: Check against read timestamp
            if txn_ts < item_entry['read_ts']:
                # ABORT: A younger transaction already read this item's value.
                # Writing now would invalidate that read because the younger
                # txn should have seen our written value instead.
                self.stats['aborted_writes'] += 1
                logger.debug(
                    f"WRITE ABORT: txn={txn_id} (ts={txn_ts:.6f}) < "
                    f"read_ts={item_entry['read_ts']:.6f} for item {item_id}"
                )
                return 'ABORT', (
                    f"Write too late: txn_ts ({txn_ts:.4f}) < "
                    f"read_ts ({item_entry['read_ts']:.4f})"
                )

            # Rule 2: Thomas Write Rule — skip obsolete writes
            if txn_ts < item_entry['write_ts']:
                # SKIP: A younger transaction already wrote a newer value.
                # Our write is outdated and would be immediately overwritten
                # anyway, so we can safely ignore it. This is the key
                # optimization of the Thomas Write Rule.
                self.stats['thomas_skips'] += 1
                logger.debug(
                    f"THOMAS SKIP: txn={txn_id} (ts={txn_ts:.6f}) < "
                    f"write_ts={item_entry['write_ts']:.6f} for item {item_id}"
                )
                return 'SKIP', "Thomas Write Rule: obsolete write skipped"

            # Rule 3: Write is allowed — update write timestamp
            item_entry['write_ts'] = txn_ts
            return 'ALLOW', "OK"

    def abort_transaction(self, txn_id):
        """Abort a transaction and prepare for restart with a new timestamp.

        In TO, aborted transactions restart with a NEW (current) timestamp.
        The new timestamp will be later (larger), giving the restarted
        transaction a 'younger' identity and reducing the chance of
        another abort.

        Note: We don't roll back timestamp_table entries because:
        1. read_ts only moves forward, and keeping it high is safe
        2. write_ts is only updated on successful writes (before commit)

        Args:
            txn_id: Transaction to abort.

        Returns:
            float: New timestamp for the restarted transaction.
        """
        with self._mutex:
            old_ts = self.active_txns.pop(txn_id, None)

        # Generate a new, later timestamp for the restart
        # Later timestamp = higher priority in future conflicts
        new_ts = self.generate_timestamp()
        with self._mutex:
            self.active_txns[txn_id] = new_ts

        logger.debug(
            f"Transaction {txn_id} restarted: "
            f"old_ts={old_ts:.9f if old_ts else 'N/A'}, new_ts={new_ts:.9f}"
        )
        return new_ts

    def commit_transaction(self, txn_id):
        """Commit a transaction — remove from active transaction list.

        Once committed, the transaction's reads and writes are permanent.
        The timestamp_table entries remain to constrain future transactions.

        Args:
            txn_id: Transaction to commit.
        """
        with self._mutex:
            self.active_txns.pop(txn_id, None)
        logger.debug(f"Transaction {txn_id} committed")

    def get_stats(self):
        """Return current performance statistics.

        Returns:
            dict: Copy of stats counters including total reads/writes,
                  aborted reads/writes, and Thomas Write Rule skips.
        """
        with self._mutex:
            return dict(self.stats)

    def reset_stats(self):
        """Reset all statistics counters to zero."""
        with self._mutex:
            for key in self.stats:
                self.stats[key] = 0

    def get_timestamp_info(self):
        """Return current timestamp table state for debugging/monitoring.

        Returns:
            dict: {item_id: {"read_ts": float, "write_ts": float}}
                  for all items that have been accessed.
        """
        with self._mutex:
            return {
                item_id: dict(entry)
                for item_id, entry in self.timestamp_table.items()
            }
