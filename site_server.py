# site_server.py
# ═══════════════════════════════════════════════════════════
# Flask Site Server — runs one instance per site
# ═══════════════════════════════════════════════════════════
#
# EXECUTION ORDER: Run after generate_data.py
#
# Windows CMD:
#   set PROTOCOL=2PL
#   python site_server.py --site 1 --port 5001
#
# Windows PowerShell:
#   $env:PROTOCOL="2PL"
#   python site_server.py --site 1 --port 5001
#
# Mac/Linux:
#   PROTOCOL=2PL python site_server.py --site 1 --port 5001
# ═══════════════════════════════════════════════════════════

import os
import sys
import argparse
import sqlite3
import threading
import time
import logging
from flask import Flask, request, jsonify

# Add project root to path so we can import our modules
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from lock_manager import LockManager
from timestamp_manager import TimestampManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Global State ───────────────────────────────────────
# These are set during initialization based on CLI arguments
SITE_ID = None
PROTOCOL = None       # '2PL' or 'TO'
DB_PATH = None
lock_manager = None   # Only used when PROTOCOL='2PL'
ts_manager = None     # Only used when PROTOCOL='TO'
site_alive = True     # Set to False by /admin/kill

# Thread lock for database operations
db_lock = threading.Lock()


def get_db_connection():
    """Get a SQLite connection for the current site's database.
    
    Uses check_same_thread=False because Flask handles requests
    in multiple threads, and we protect access with db_lock.
    timeout=1 prevents threads from blocking too long on the file lock.
    
    Returns:
        sqlite3.Connection: A connection to the site's SQLite database
            with Row factory enabled for dict-like row access.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=1)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.row_factory = sqlite3.Row
    return conn


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint.
    
    Returns site status, protocol in use, and item count.
    Returns HTTP 503 if the site has been killed via /admin/kill.
    
    Returns:
        JSON response with site health information.
    """
    if not site_alive:
        return jsonify({'site': SITE_ID, 'status': 'DOWN', 'item_count': 0}), 503
    
    try:
        with db_lock:
            conn = get_db_connection()
            try:
                count = conn.execute('SELECT COUNT(*) FROM inventory').fetchone()[0]
            finally:
                conn.close()
        return jsonify({
            'site': SITE_ID,
            'status': 'OK',
            'protocol': PROTOCOL,
            'item_count': count
        })
    except Exception as e:
        return jsonify({'site': SITE_ID, 'status': 'ERROR', 'error': str(e)}), 500


@app.route('/transaction/begin', methods=['POST'])
def begin_transaction():
    """Begin a new transaction.
    
    For 2PL: just returns a txn_id (locks acquired on read/write).
    For TO: assigns a monotonically increasing timestamp and returns it.
    
    Request JSON:
        txn_id (str): Unique transaction identifier.
    
    Returns:
        JSON with txn_id, status, and timestamp (TO only).
    """
    if not site_alive:
        return jsonify({'error': 'Site is down'}), 503
    
    data = request.get_json()
    txn_id = data.get('txn_id')
    
    if not txn_id:
        return jsonify({'error': 'txn_id required'}), 400
    
    result = {'txn_id': txn_id, 'status': 'started'}
    
    if PROTOCOL == 'TO':
        # Assign a timestamp to the transaction
        timestamp = ts_manager.begin_transaction(txn_id)
        result['timestamp'] = timestamp
    
    return jsonify(result)


@app.route('/transaction/read', methods=['POST'])
def read_item():
    """Read an item's data within a transaction.
    
    For 2PL: acquires a shared lock first; returns 409 on deadlock/timeout.
    For TO: validates read against timestamp ordering; returns 409 if
            a younger transaction has already written the item.
    
    Request JSON:
        txn_id (str): Transaction identifier.
        item_id (int): ID of the inventory item to read.
    
    Returns:
        JSON with item data on success, or abort reason on failure.
    """
    if not site_alive:
        return jsonify({'error': 'Site is down'}), 503
    
    data = request.get_json()
    txn_id = data.get('txn_id')
    item_id = data.get('item_id')
    
    if not txn_id or item_id is None:
        return jsonify({'error': 'txn_id and item_id required'}), 400
        
    # ARTIFICIAL DELAY: Forces server threads to overlap in time,
    # ensuring true concurrency and generating the expected abort rates.
    import time
    time.sleep(0.01)
    
    if PROTOCOL == '2PL':
        # Acquire shared lock before reading
        if not lock_manager.acquire_lock(txn_id, item_id, 'S'):
            return jsonify({
                'txn_id': txn_id,
                'status': 'ABORTED',
                'reason': 'Could not acquire read lock (deadlock or timeout)'
            }), 409
    
    elif PROTOCOL == 'TO':
        # Validate read against timestamp ordering rules
        allowed, reason = ts_manager.validate_read(txn_id, item_id)
        if not allowed:
            return jsonify({
                'txn_id': txn_id,
                'status': 'ABORTED',
                'reason': reason
            }), 409
    
    # Read the actual data from SQLite
    # IMPORTANT: open, query, and close the connection all within db_lock
    # to prevent SQLite file-lock leaks that cause server hangs.
    try:
        with db_lock:
            conn = get_db_connection()
            try:
                row = conn.execute(
                    'SELECT * FROM inventory WHERE ItemID = ?', (item_id,)
                ).fetchone()
                # Extract data to a plain dict immediately while conn is open
                if row is not None:
                    item_data = {
                        'ItemID': row['ItemID'],
                        'ItemName': row['ItemName'],
                        'Category': row['Category'],
                        'Stock': row['Stock'],
                        'Price': row['Price'],
                        'Version': row['Version']
                    }
                else:
                    item_data = None
            finally:
                conn.close()
        
        if item_data is None:
            return jsonify({'error': f'Item {item_id} not found on this site'}), 404
        
        return jsonify({
            'txn_id': txn_id,
            'status': 'OK',
            'data': item_data
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/transaction/write', methods=['POST'])
def write_item():
    """Write (update stock) for an item within a transaction.
    
    For 2PL: acquires exclusive lock first; returns 409 on deadlock/timeout.
    For TO: validates write against timestamp ordering rules.
            May return SKIPPED if the Thomas Write Rule applies
            (a younger transaction has already written, so this
            write is obsolete and can be safely ignored).
    
    Request JSON:
        txn_id (str): Transaction identifier.
        item_id (int): ID of the inventory item to update.
        new_stock (int): New stock value to write.
    
    Returns:
        JSON with write confirmation, abort reason, or skip notification.
    """
    if not site_alive:
        return jsonify({'error': 'Site is down'}), 503
    
    data = request.get_json()
    txn_id = data.get('txn_id')
    item_id = data.get('item_id')
    new_stock = data.get('new_stock')
    
    if not txn_id or item_id is None or new_stock is None:
        return jsonify({'error': 'txn_id, item_id, and new_stock required'}), 400
    
    if PROTOCOL == '2PL':
        # Acquire exclusive lock before writing
        if not lock_manager.acquire_lock(txn_id, item_id, 'X'):
            return jsonify({
                'txn_id': txn_id,
                'status': 'ABORTED',
                'reason': 'Could not acquire write lock (deadlock or timeout)'
            }), 409
    
    elif PROTOCOL == 'TO':
        # Validate write against timestamp ordering rules
        result, reason = ts_manager.validate_write(txn_id, item_id)
        if result == 'ABORT':
            return jsonify({
                'txn_id': txn_id,
                'status': 'ABORTED',
                'reason': reason
            }), 409
        elif result == 'SKIP':
            # Thomas Write Rule: ignore obsolete write
            return jsonify({
                'txn_id': txn_id,
                'status': 'SKIPPED',
                'reason': reason
            })
    
    # Perform the actual write to SQLite
    # IMPORTANT: open, write, commit, and close all within db_lock
    try:
        with db_lock:
            conn = get_db_connection()
            try:
                conn.execute(
                    'UPDATE inventory SET Stock = ?, Version = Version + 1 WHERE ItemID = ?',
                    (new_stock, item_id)
                )
                conn.commit()
            finally:
                conn.close()
        
        return jsonify({
            'txn_id': txn_id,
            'status': 'OK',
            'item_id': item_id,
            'new_stock': new_stock
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/transaction/commit', methods=['POST'])
def commit_transaction():
    """Commit a transaction.
    
    For 2PL: releases all locks held by the transaction (shrinking phase).
    For TO: marks transaction as committed in the timestamp manager.
    
    Request JSON:
        txn_id (str): Transaction identifier to commit.
    
    Returns:
        JSON confirming the commit.
    """
    if not site_alive:
        return jsonify({'error': 'Site is down'}), 503
    
    data = request.get_json()
    txn_id = data.get('txn_id')
    
    if not txn_id:
        return jsonify({'error': 'txn_id required'}), 400
    
    if PROTOCOL == '2PL':
        lock_manager.release_locks(txn_id)
    elif PROTOCOL == 'TO':
        ts_manager.commit_transaction(txn_id)
    
    return jsonify({'txn_id': txn_id, 'status': 'COMMITTED'})


@app.route('/transaction/abort', methods=['POST'])
def abort_transaction():
    """Abort a transaction.
    
    For 2PL: releases all locks held by the transaction.
    For TO: aborts and restarts the transaction with a new (larger)
            timestamp so it can be retried without ordering conflicts.
    
    Request JSON:
        txn_id (str): Transaction identifier to abort.
    
    Returns:
        JSON confirming the abort, with new_timestamp for TO protocol.
    """
    if not site_alive:
        return jsonify({'error': 'Site is down'}), 503
    
    data = request.get_json()
    txn_id = data.get('txn_id')
    
    if not txn_id:
        return jsonify({'error': 'txn_id required'}), 400
    
    result = {'txn_id': txn_id, 'status': 'ABORTED'}
    
    if PROTOCOL == '2PL':
        lock_manager.release_locks(txn_id)
    elif PROTOCOL == 'TO':
        new_ts = ts_manager.abort_transaction(txn_id)
        result['new_timestamp'] = new_ts
    
    return jsonify(result)


@app.route('/admin/kill', methods=['POST'])
def kill_site():
    """Simulate site failure for demo purposes.
    
    Sets site_alive=False so all subsequent requests return 503.
    Uses a flag-based approach instead of os.kill() for Windows compatibility.
    
    If using 2PL, all held locks are released to prevent permanent deadlocks
    in the distributed system when other sites are still running.
    
    Returns:
        JSON confirming the site has been killed.
    """
    global site_alive
    site_alive = False
    logger.warning(f"Site {SITE_ID} has been killed (simulated failure)")
    
    # If using 2PL, release all locks to prevent permanent deadlocks
    if PROTOCOL == '2PL' and lock_manager:
        # Get all active transactions and release their locks
        for item_id in list(lock_manager.lock_table.keys()):
            entry = lock_manager.lock_table.get(item_id)
            if entry:
                for txn_id in list(entry['holders']):
                    lock_manager.release_locks(txn_id)
    
    return jsonify({
        'site': SITE_ID,
        'status': 'KILLED',
        'message': 'Site is now down. Restart process to recover.'
    })


@app.route('/admin/revive', methods=['POST'])
def revive_site():
    """Revive a killed site (for testing recovery).
    
    Resets the site_alive flag so the site starts accepting
    requests again. Does not restore any previously held locks
    or transaction state.
    
    Returns:
        JSON confirming the site has been revived.
    """
    global site_alive
    site_alive = True
    logger.info(f"Site {SITE_ID} has been revived")
    return jsonify({'site': SITE_ID, 'status': 'REVIVED'})


@app.route('/admin/reset', methods=['POST'])
def reset_site():
    """Reset concurrency control state (lock tables / timestamp tables).
    
    Called between benchmark runs to clear stale locks from failed
    transactions. Does NOT reset the database — only the in-memory
    concurrency control state.
    
    Returns:
        JSON confirming the reset.
    """
    global lock_manager, ts_manager, site_alive
    site_alive = True
    
    if PROTOCOL == '2PL':
        lock_manager = LockManager()
        logger.info(f"Site {SITE_ID}: Lock manager reset")
    elif PROTOCOL == 'TO':
        ts_manager = TimestampManager()
        logger.info(f"Site {SITE_ID}: Timestamp manager reset")
    
    return jsonify({'site': SITE_ID, 'status': 'RESET'})


def parse_args():
    """Parse command-line arguments for site configuration.
    
    Returns:
        argparse.Namespace: Parsed arguments with 'site' (int) and 'port' (int).
    """
    parser = argparse.ArgumentParser(description='Distributed Database Site Server')
    parser.add_argument('--site', type=int, required=True, choices=[1, 2, 3],
                        help='Site ID (1, 2, or 3)')
    parser.add_argument('--port', type=int, required=True,
                        help='Port number to listen on')
    return parser.parse_args()


def initialize(site_id, protocol):
    """Initialize the site with the appropriate concurrency control module.
    
    Sets up global state including database path, protocol selection,
    and the corresponding lock or timestamp manager instance.
    
    Args:
        site_id: Integer site identifier (1, 2, or 3).
        protocol: Concurrency control protocol string ('2PL' or 'TO').
    
    Raises:
        SystemExit: If the database file doesn't exist or the protocol is unknown.
    """
    global SITE_ID, PROTOCOL, DB_PATH, lock_manager, ts_manager
    
    SITE_ID = site_id
    PROTOCOL = protocol.upper()
    DB_PATH = os.path.join(BASE_DIR, 'data', f'site{site_id}.db')
    
    if not os.path.exists(DB_PATH):
        logger.error(f"Database not found: {DB_PATH}")
        logger.error("Run generate_data.py first!")
        sys.exit(1)
    
    if PROTOCOL == '2PL':
        lock_manager = LockManager()
        logger.info(f"Site {SITE_ID}: Initialized with 2-Phase Locking")
    elif PROTOCOL == 'TO':
        ts_manager = TimestampManager()
        logger.info(f"Site {SITE_ID}: Initialized with Timestamp Ordering")
    else:
        logger.error(f"Unknown protocol: {PROTOCOL}. Use '2PL' or 'TO'")
        sys.exit(1)


if __name__ == '__main__':
    args = parse_args()
    
    # Get protocol from environment variable, default to '2PL'
    protocol = os.environ.get('PROTOCOL', '2PL')
    
    initialize(args.site, protocol)
    
    logger.info(f"Starting Site {SITE_ID} on port {args.port} with protocol {PROTOCOL}")
    
    # Use threaded=True for handling concurrent requests
    # debug=False for production-like behavior in benchmarks
    app.run(host='127.0.0.1', port=args.port, threaded=True, debug=False)
