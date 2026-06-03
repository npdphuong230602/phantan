# transaction_simulator.py
# ═══════════════════════════════════════════════════════════
# Transaction Simulator — generates concurrent workloads
# ═══════════════════════════════════════════════════════════

import os
import random
import time
import uuid
import threading
import requests
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Site Configuration ─────────────────────────────────
# Maps ItemID ranges to site URLs
SITE_CONFIG = {
    1: {'url': 'http://127.0.0.1:5001', 'range': (1, 333)},
    2: {'url': 'http://127.0.0.1:5002', 'range': (334, 666)},
    3: {'url': 'http://127.0.0.1:5003', 'range': (667, 1000)},
}

# Hot items for conflict simulation (always on Site 1)
HOT_ITEMS = list(range(1, 11))  # ItemID 1-10

# Maximum number of retries for aborted transactions
MAX_RETRIES = 5

# Request timeout in seconds — kept short to fail fast on overloaded servers
REQUEST_TIMEOUT = 2

# Shared session for connection pooling (avoids Windows TCP port exhaustion)
_session = None


def get_session():
    """Get or create a shared requests.Session with connection pooling.
    
    Using a Session avoids creating a new TCP connection per request,
    which prevents Windows ephemeral port exhaustion under high concurrency.
    """
    global _session
    if _session is None:
        _session = requests.Session()
        # Increase connection pool size for concurrent access
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=50,
            max_retries=0  # We handle retries ourselves
        )
        _session.mount('http://', adapter)
    return _session


def reset_sites():
    """Reset concurrency control state on all sites.
    
    Sends POST /admin/reset to each site server to reinitialize
    the lock manager or timestamp manager. This clears any stale
    locks from failed transactions between benchmark runs.
    """
    session = get_session()
    for site_id, config in SITE_CONFIG.items():
        try:
            resp = session.post(f"{config['url']}/admin/reset", timeout=15)
            print(f"Reset Site {site_id}: {resp.status_code}", flush=True)
        except Exception as e:
            print(f"Reset Site {site_id} FAILED: {e}", flush=True)


def get_site_for_item(item_id):
    """Determine which site holds a given item based on ID range.
    
    Uses horizontal fragmentation to map items to sites:
      Site 1: ItemID 1-333   (Electronics)
      Site 2: ItemID 334-666 (Clothing)
      Site 3: ItemID 667-1000 (Food)
    
    Args:
        item_id: The integer ID of the item to locate.
        
    Returns:
        str: Base URL of the site holding this item
            (e.g., 'http://localhost:5001').
    
    Raises:
        ValueError: If item_id doesn't fall within any site's range.
    """
    for site_id, config in SITE_CONFIG.items():
        start, end = config['range']
        if start <= item_id <= end:
            return config['url']
    raise ValueError(f"Item {item_id} not in any site's range")


def simulate_purchase(txn_id, conflict_density, protocol_unused, barrier=None):
    """Simulate a single purchase transaction.
    
    With probability = conflict_density, targets a HOT item (ItemID 1-10)
    to create lock contention / timestamp conflicts. Otherwise, targets
    a random item from any site.
    
    Operation sequence: BEGIN → READ stock → if stock > 0 → WRITE stock-1 → COMMIT
    
    Handles aborts by retrying up to MAX_RETRIES times with exponential
    backoff jitter. Each retry generates a new transaction ID to avoid
    conflicts with the aborted transaction's state.
    
    Args:
        txn_id: Unique transaction identifier (str).
        conflict_density: Probability [0.0, 1.0] of targeting a hot item.
            Higher values create more contention between concurrent transactions.
        protocol_unused: Not used here (protocol is set on the server side
            via the PROTOCOL environment variable).
        barrier: Optional threading.Barrier to synchronize start times.
        
    Returns:
        dict: Transaction result containing:
            - txn_id (str): Original transaction identifier
            - success (bool): Whether the transaction ultimately committed
            - abort_count (int): How many times this txn was aborted & retried
            - latency_ms (float): Total wall-clock time including all retries
            - item_id (int): The item that was targeted
            - retries (int): Number of retry attempts made
    """
    if barrier:
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass

    start_time = time.time()
    abort_count = 0
    # Track the last item_id across attempts for the final result
    last_item_id = -1
    
    for attempt in range(MAX_RETRIES + 1):
        # Generate a unique txn_id for each attempt (retries need new IDs
        # because the server may still have state from the aborted txn)
        current_txn_id = f"{txn_id}_attempt{attempt}" if attempt > 0 else txn_id
        
        # Choose target item based on conflict density
        if random.random() < conflict_density:
            # Target a hot item (high conflict) — these are all on Site 1
            item_id = random.choice(HOT_ITEMS)
        else:
            # Target a random item from any site
            item_id = random.randint(1, 1000)
        
        last_item_id = item_id
        site_url = get_site_for_item(item_id)
        
        try:
            # Step 1: BEGIN transaction
            resp = get_session().post(
                f"{site_url}/transaction/begin",
                json={'txn_id': current_txn_id},
                timeout=REQUEST_TIMEOUT
            )
            if resp.status_code != 200:
                abort_count += 1
                continue
            
            # Step 2: READ the item's current stock
            resp = get_session().post(
                f"{site_url}/transaction/read",
                json={'txn_id': current_txn_id, 'item_id': item_id},
                timeout=REQUEST_TIMEOUT
            )
            
            if resp.status_code == 409:  # ABORTED by concurrency control
                abort_count += 1
                # Notify server to clean up transaction state
                get_session().post(
                    f"{site_url}/transaction/abort",
                    json={'txn_id': current_txn_id},
                    timeout=REQUEST_TIMEOUT
                )
                # Small random backoff to reduce repeated collisions
                time.sleep(random.uniform(0.001, 0.01))
                continue
            
            if resp.status_code != 200:
                abort_count += 1
                continue
            
            data = resp.json().get('data', {})
            current_stock = data.get('Stock', 0)
            
            # Step 3: WRITE new stock
            # We unconditionally write (e.g., current_stock + 1) to ensure
            # every transaction attempts an 'X' lock/write operation.
            # If we only wrote when stock > 0, the hot items would quickly
            # run out of stock and transactions would stop conflicting!
            new_stock = current_stock + 1
            resp = get_session().post(
                f"{site_url}/transaction/write",
                json={'txn_id': current_txn_id, 'item_id': item_id, 'new_stock': new_stock},
                timeout=REQUEST_TIMEOUT
            )
            
            if resp.status_code == 409:  # ABORTED by concurrency control
                abort_count += 1
                get_session().post(
                    f"{site_url}/transaction/abort",
                    json={'txn_id': current_txn_id},
                    timeout=REQUEST_TIMEOUT
                )
                time.sleep(random.uniform(0.001, 0.01))
                continue
            
            # Step 4: COMMIT the transaction
            resp = get_session().post(
                f"{site_url}/transaction/commit",
                json={'txn_id': current_txn_id},
                timeout=REQUEST_TIMEOUT
            )
            
            end_time = time.time()
            return {
                'txn_id': txn_id,
                'success': True,
                'abort_count': abort_count,
                'latency_ms': (end_time - start_time) * 1000,
                'item_id': item_id,
                'retries': attempt
            }
            
        except requests.exceptions.RequestException as e:
            # Network error — site might be down or unreachable
            abort_count += 1
            logger.debug(f"Transaction {current_txn_id} failed: {e}")
            
            # IMPORTANT: Must try to abort on server to release any held locks!
            # Otherwise, locks are held forever, causing permanent deadlocks.
            try:
                get_session().post(
                    f"{site_url}/transaction/abort",
                    json={'txn_id': current_txn_id},
                    timeout=2
                )
            except:
                pass
                
            # Longer backoff for network errors since site may need time to recover
            time.sleep(random.uniform(0.01, 0.05))
            continue
    
    # All retries exhausted — transaction permanently failed
    end_time = time.time()
    return {
        'txn_id': txn_id,
        'success': False,
        'abort_count': abort_count,
        'latency_ms': (end_time - start_time) * 1000,
        'item_id': last_item_id,
        'retries': MAX_RETRIES
    }


def run_experiment(conflict_density, protocol, n_threads=50, n_runs=10):
    """Run a complete experiment with given parameters.
    
    Launches n_threads concurrent transactions per run, repeats n_runs
    times, and aggregates the results. This simulates realistic workload
    patterns where many clients access the database simultaneously.
    
    The conflict_density parameter controls what fraction of transactions
    target the same "hot" items, creating contention that exercises the
    concurrency control mechanisms.
    
    Args:
        conflict_density: Probability of targeting hot items [0.0, 1.0].
            0.0 = no conflicts (uniform random), 1.0 = all target hot items.
        protocol: '2PL' or 'TO' (informational label; actual protocol is
            determined by the server's PROTOCOL environment variable).
        n_threads: Number of concurrent transactions per run. Higher values
            create more contention. Default: 50.
        n_runs: Number of runs to average over for statistical stability.
            Default: 10.
        
    Returns:
        dict: Aggregated experiment metrics:
            - abort_rate (float): Fraction of transactions that experienced
              at least one abort (0.0 to 1.0)
            - throughput (float): Successful transactions per second,
              normalized by concurrency level
            - avg_latency_ms (float): Average end-to-end latency per
              transaction in milliseconds
            - avg_restarts (float): Average number of restart attempts
              per transaction
    """
    all_results = []
    
    for run in range(n_runs):
        # Reset server state before each run to clear stale locks
        reset_sites()
        
        run_results = []
        run_start = time.time()
        
        # Use ThreadPoolExecutor for concurrent transactions
        # Each thread simulates an independent client
        # Barrier ensures ALL threads send their HTTP requests at the EXACT same millisecond
        barrier = threading.Barrier(n_threads)
        
        with ThreadPoolExecutor(max_workers=n_threads) as executor:
            futures = []
            for i in range(n_threads):
                # Include protocol, conflict density, run number, and a random
                # suffix to ensure globally unique transaction IDs
                txn_id = f"{protocol}_cd{conflict_density}_run{run}_txn{i}_{uuid.uuid4().hex[:8]}"
                future = executor.submit(
                    simulate_purchase, txn_id, conflict_density, protocol, barrier
                )
                futures.append(future)
            
            # Collect results as they complete (not necessarily in order)
            for future in as_completed(futures):
                try:
                    result = future.result()
                    run_results.append(result)
                except Exception as e:
                    logger.error(f"Transaction failed with exception: {e}")
                    run_results.append({
                        'success': False,
                        'abort_count': 1,
                        'latency_ms': 0,
                        'retries': MAX_RETRIES
                    })
        
        run_end = time.time()
        run_duration = run_end - run_start
        
        all_results.extend(run_results)
        
        logger.info(
            f"  Run {run+1}/{n_runs}: "
            f"{sum(1 for r in run_results if r['success'])}/{len(run_results)} succeeded, "
            f"{run_duration:.2f}s"
        )
    
    # Aggregate results across all runs
    total = len(all_results)
    if total == 0:
        return {'abort_rate': 0, 'throughput': 0, 'avg_latency_ms': 0, 'avg_restarts': 0}
    
    successful = [r for r in all_results if r['success']]
    aborted = [r for r in all_results if r['abort_count'] > 0]
    
    # Abort rate: fraction of transactions that were aborted at least once
    abort_rate = len(aborted) / total
    
    # Throughput: successful transactions per second, normalized by the
    # number of concurrent threads to account for parallelism
    total_latency_s = sum(r['latency_ms'] for r in all_results) / 1000
    throughput = len(successful) / (total_latency_s / n_threads) if total_latency_s > 0 else 0
    
    # Average latency includes both successful and failed transactions
    avg_latency = sum(r['latency_ms'] for r in all_results) / total
    
    # Average restarts shows how many times transactions needed to retry
    avg_restarts = sum(r['retries'] for r in all_results) / total
    
    return {
        'abort_rate': round(abort_rate, 4),
        'throughput': round(throughput, 2),
        'avg_latency_ms': round(avg_latency, 2),
        'avg_restarts': round(avg_restarts, 4)
    }
