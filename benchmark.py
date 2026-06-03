# benchmark.py
# ═══════════════════════════════════════════════════════════
# Benchmark Runner — runs full experiment matrix
# ═══════════════════════════════════════════════════════════
#
# EXECUTION ORDER: Run after all 3 site servers are running
#
# Terminal:
#   python benchmark.py              (auto-start servers)
#   python benchmark.py --manual     (servers already running)
#   python benchmark.py --failure-test  (site failure demo)
#
# This will:
#   1. Start site servers (or verify they're running)
#   2. Run experiments: 5 conflict levels × 2 protocols
#   3. Save results to results/results.csv
#   4. Optionally run site failure simulation
# ═══════════════════════════════════════════════════════════

import os
import sys
import time
import subprocess
import csv
import requests
import logging
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from transaction_simulator import run_experiment, SITE_CONFIG

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ─── Experiment Configuration ─────────────────────────────
# These define the full experiment matrix
CONFLICT_LEVELS = [0.1, 0.3, 0.5, 0.7, 0.9]
PROTOCOLS = ['2PL', 'TO']
N_THREADS = 50    # Concurrent transactions per run
N_RUNS = 10       # Repetitions for statistical averaging

RESULTS_DIR = os.path.join(BASE_DIR, 'results')
RESULTS_CSV = os.path.join(RESULTS_DIR, 'results.csv')

# Site server ports — must match site_server.py defaults
SITE_PORTS = {1: 5001, 2: 5002, 3: 5003}


def check_sites_health():
    """Check that all 3 sites are running and healthy.

    Sends GET /health to each site and verifies the response.

    Returns:
        True if all 3 sites respond with status='OK', False otherwise.
    """
    all_ok = True
    for site_id, config in SITE_CONFIG.items():
        try:
            resp = requests.get(f"{config['url']}/health", timeout=5)
            data = resp.json()
            logger.info(
                f"  Site {site_id}: status={data.get('status')}, "
                f"items={data.get('item_count')}, "
                f"protocol={data.get('protocol')}"
            )
            if data.get('status') != 'OK':
                all_ok = False
        except requests.exceptions.ConnectionError:
            logger.error(f"  Site {site_id}: KHÔNG HOẠT ĐỘNG tại {config['url']}")
            all_ok = False
        except Exception as e:
            logger.error(f"  Máy chủ {site_id} không phản hồi! Lỗi: {str(e)}")
            return False
    return all_ok


def start_site_servers(protocol):
    """Start all 3 site servers as subprocesses with the given protocol.

    Each server runs in a separate process. On Windows, uses
    CREATE_NEW_PROCESS_GROUP for clean shutdown support.

    Args:
        protocol: '2PL' or 'TO' — set as PROTOCOL env variable.

    Returns:
        list: subprocess.Popen objects for the 3 server processes.
    """
    processes = []
    env = os.environ.copy()
    env['PROTOCOL'] = protocol
    # Remove Werkzeug internal variables that may leak from a parent Flask process
    # (e.g., when benchmark.py is launched from dashboard.py).
    # WERKZEUG_SERVER_FD would cause site_server.py to reuse the dashboard's socket
    # instead of binding its own port, silently failing to start.
    for key in list(env.keys()):
        if key.startswith('WERKZEUG_'):
            del env[key]

    for site_id, port in SITE_PORTS.items():
        cmd = [
            sys.executable,
            os.path.join(BASE_DIR, 'site_server.py'),
            '--site', str(site_id),
            '--port', str(port)
        ]

        logger.info(f"Đang khởi động Site {site_id} trên cổng {port} với {protocol}...")

        # CREATE_NEW_PROCESS_GROUP allows clean termination on Windows
        # Redirect stdout and stderr to files instead of DEVNULL to see what is failing
        log_file = open(f"site_{site_id}.log", "w")
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=log_file,
            stderr=log_file
        )
        processes.append(proc)

    # Wait for servers to initialize their Flask apps and bind to ports
    logger.info("Chờ các máy chủ khởi động (3 giây)...")
    time.sleep(3)

    return processes


def stop_site_servers(processes):
    """Stop all site server processes gracefully.

    Uses terminate() first (SIGTERM), falls back to kill() (SIGKILL)
    if the process doesn't stop within 5 seconds.

    Args:
        processes: List of subprocess.Popen objects to stop.
    """
    for proc in processes:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Process didn't stop gracefully — force kill
            proc.kill()
        except Exception:
            pass
    logger.info("Tất cả máy chủ đã được dừng.")


def run_full_benchmark(auto_start=True):
    """Run the complete benchmark matrix: 2 protocols × 5 conflict levels.

    For each protocol:
    1. Start site servers (if auto_start=True)
    2. Verify all sites are healthy
    3. Run experiments for each conflict level
    4. Record results
    5. Stop servers

    Results are saved to results/results.csv.

    Args:
        auto_start: If True, automatically start/stop servers.
                    If False, assumes servers are already running.

    Returns:
        list: All result rows as dictionaries.
    """
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # CSV column names
    fieldnames = [
        'protocol', 'conflict_density', 'abort_rate', 'throughput',
        'avg_latency_ms', 'avg_restarts', 'timestamp'
    ]

    results = []

    for protocol in PROTOCOLS:
        processes = []

        if auto_start:
            # Start servers with this protocol
            processes = start_site_servers(protocol)

            # Verify servers are up and healthy
            logger.info("Kiểm tra trạng thái các máy chủ...")
            if not check_sites_health():
                logger.error("Không phải tất cả các site đều khỏe mạnh. Thử lại sau 3 giây...")
                time.sleep(3)
                if not check_sites_health():
                    logger.error("Các site vẫn không khỏe mạnh. Bỏ qua giao thức này.")
                    stop_site_servers(processes)
                    continue
        else:
            # Manual mode — servers should already be running
            logger.info(f"\nGiả định các máy chủ đang chạy với giao thức: {protocol}")
            if not check_sites_health():
                logger.error(
                    "Các máy chủ không hoạt động! Hãy khởi động chúng thủ công trước.\n"
                    "Xem README.md để biết hướng dẫn."
                )
                return results

        logger.info("\n" + "=" * 60)
        logger.info(f"Đang chạy thực nghiệm với giao thức: {protocol}")
        logger.info("=" * 60)

        for conflict_density in CONFLICT_LEVELS:
            logger.info(f"\n--- Mật độ Xung đột: {conflict_density} ({int(conflict_density*100)}%) ---")

            # Run the experiment (multiple runs, averaged)
            experiment_result = run_experiment(
                conflict_density=conflict_density,
                protocol=protocol,
                n_threads=N_THREADS,
                n_runs=N_RUNS
            )

            # Build result row
            row = {
                'protocol': protocol,
                'conflict_density': conflict_density,
                'abort_rate': experiment_result['abort_rate'],
                'throughput': experiment_result['throughput'],
                'avg_latency_ms': experiment_result['avg_latency_ms'],
                'avg_restarts': experiment_result['avg_restarts'],
                'timestamp': datetime.now().isoformat()
            }
            results.append(row)

            logger.info(
                f"  Kết quả: abort_rate={row['abort_rate']:.4f}, "
                f"throughput={row['throughput']:.2f} txn/s, "
                f"latency={row['avg_latency_ms']:.2f}ms, "
                f"restarts={row['avg_restarts']:.4f}"
            )

        if auto_start:
            stop_site_servers(processes)
            # Give ports time to free up before starting next protocol's servers
            time.sleep(2)

    # Save all results to CSV
    with open(RESULTS_CSV, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    logger.info("\n" + "=" * 60)
    logger.info(f"Tất cả kết quả đã được lưu tại {RESULTS_CSV}")
    logger.info(f"Tổng số thực nghiệm: {len(results)}")
    logger.info("=" * 60)

    return results


def simulate_site_failure():
    """Simulate a site failure during active transactions.

    This demo function:
    1. Launches 20 concurrent transactions
    2. Mid-flight, kills Site 2 via POST /admin/kill
    3. Logs how many in-flight transactions were affected
    4. For 2PL: locks held by Site 2 are released by /admin/kill
    5. For TO: aborted transactions get new timestamps on retry
    6. Records recovery time in milliseconds

    Uses requests.post() instead of os.kill() for Windows compatibility.
    """
    logger.info("\n" + "=" * 60)
    logger.info("SITE FAILURE SIMULATION")
    logger.info("=" * 60)

    # Check that sites are running
    if not check_sites_health():
        logger.error("Not all sites are running. Start them first.")
        return

    site2_url = SITE_CONFIG[2]['url']

    # Import here to avoid circular imports
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from transaction_simulator import simulate_purchase
    import uuid

    # Launch concurrent transactions
    logger.info("Launching 20 concurrent transactions...")

    futures = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        for i in range(20):
            txn_id = f"failure_test_{uuid.uuid4().hex[:8]}"
            # Use 0.0 conflict density so transactions spread across all sites
            # Some will hit Site 2 and be affected by the failure
            future = executor.submit(simulate_purchase, txn_id, 0.0)
            futures.append(future)

        # Wait briefly then kill Site 2
        time.sleep(0.5)
        logger.info(">>> KILLING Site 2...")
        kill_time = time.time()

        try:
            resp = requests.post(f"{site2_url}/admin/kill", timeout=5)
            logger.info(f"Kill response: {resp.json()}")
        except Exception as e:
            logger.error(f"Failed to kill Site 2: {e}")

        # Wait for all transactions to complete or fail
        affected = 0
        succeeded = 0
        for future in as_completed(futures):
            try:
                result = future.result()
                if not result['success']:
                    affected += 1
                else:
                    succeeded += 1
            except Exception:
                affected += 1

    recovery_time = (time.time() - kill_time) * 1000

    logger.info(f"\nFailure Simulation Results:")
    logger.info(f"  Transactions affected (failed): {affected}")
    logger.info(f"  Transactions succeeded: {succeeded}")
    logger.info(f"  Recovery time: {recovery_time:.2f}ms")

    # Try to revive Site 2
    try:
        resp = requests.post(f"{site2_url}/admin/revive", timeout=5)
        logger.info(f"  Site 2 revived: {resp.json()}")
    except Exception as e:
        logger.info(f"  Could not revive Site 2 (may need restart): {e}")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Run concurrency control benchmarks: 2PL vs TO'
    )
    parser.add_argument(
        '--auto', action='store_true', default=True,
        help='Automatically start/stop site servers (default: True)'
    )
    parser.add_argument(
        '--manual', action='store_true',
        help='Assume servers are already running manually'
    )
    parser.add_argument(
        '--failure-test', action='store_true',
        help='Run site failure simulation instead of benchmark'
    )
    parser.add_argument(
        '--threads', type=int, default=50,
        help='Number of concurrent threads per run (default: 50)'
    )
    parser.add_argument(
        '--runs', type=int, default=10,
        help='Number of runs per experiment for averaging (default: 10)'
    )
    args = parser.parse_args()

    # Update global config from CLI args
    N_THREADS = args.threads
    N_RUNS = args.runs

    if args.failure_test:
        simulate_site_failure()
    else:
        auto_start = not args.manual
        run_full_benchmark(auto_start=auto_start)
