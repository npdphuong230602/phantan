"""
Fault Tolerance Live Demo Runner
---------------------------------
Starts 3 site servers and continuously sends transactions.
The user can Kill/Revive sites via the Dashboard at any time.
Output is streamed to the Dashboard terminal in real-time.
"""
import os
import sys
import time
import random
import subprocess
import uuid
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_PORTS = {1: 5001, 2: 5002, 3: 5003}
SITE_URLS = {i: f"http://127.0.0.1:{p}" for i, p in SITE_PORTS.items()}

# Item ranges per site (must match generate_data.py)
SITE_RANGES = {
    1: (1, 333),
    2: (334, 666),
    3: (667, 1000),
}


def get_site_for_item(item_id):
    """Determine which site owns a given item ID."""
    for site_id, (lo, hi) in SITE_RANGES.items():
        if lo <= item_id <= hi:
            return site_id
    return 1


def start_servers(protocol='2PL'):
    """Start all 3 site servers."""
    processes = []
    env = os.environ.copy()
    env['PROTOCOL'] = protocol
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
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        processes.append(proc)
        print(f"INFO: Đã khởi động Site {site_id} trên cổng {port} ({protocol})")

    print("INFO: Chờ máy chủ khởi động (3 giây)...")
    time.sleep(3)
    return processes


def check_health():
    """Check all sites and print status."""
    all_ok = True
    for site_id, url in SITE_URLS.items():
        try:
            r = requests.get(f'{url}/health', timeout=2)
            if r.status_code == 200:
                print(f"INFO: Site {site_id}: ONLINE")
            else:
                print(f"ERROR: Site {site_id}: OFFLINE (status={r.status_code})")
                all_ok = False
        except Exception:
            print(f"ERROR: Site {site_id}: OFFLINE (không phản hồi)")
            all_ok = False
    return all_ok


def run_single_transaction(txn_num):
    """
    Run one complete purchase transaction matching site_server.py's API.
    Returns (success: bool, message: str)
    """
    # Generate a unique transaction ID (same format as transaction_simulator)
    txn_id = f"demo-{txn_num}-{uuid.uuid4().hex[:8]}"

    # Pick a random item — spread across all 3 sites
    site_choice = random.choices([1, 2, 3], weights=[40, 40, 20])[0]
    lo, hi = SITE_RANGES[site_choice]
    item_id = random.randint(lo, hi)

    target_site = site_choice
    target_url = SITE_URLS[target_site]

    try:
        # Step 1: BEGIN transaction on the target site
        r = requests.post(
            f'{target_url}/transaction/begin',
            json={'txn_id': txn_id},
            timeout=2
        )
        if r.status_code != 200:
            error = r.json().get('error', r.text) if r.text else 'Unknown'
            return False, f"Begin thất bại trên Site {target_site}: {error}"

        # Step 2: READ item from target site
        r = requests.post(
            f'{target_url}/transaction/read',
            json={'txn_id': txn_id, 'item_id': item_id},
            timeout=2
        )
        if r.status_code != 200:
            reason = r.json().get('reason', 'Unknown') if r.text else 'Unknown'
            # Try to abort
            try:
                requests.post(f'{target_url}/transaction/abort',
                              json={'txn_id': txn_id}, timeout=1)
            except Exception:
                pass
            return False, f"Read Item {item_id} bị ABORT trên Site {target_site}: {reason}"

        # Get current stock
        item_data = r.json().get('data', {})
        current_stock = item_data.get('Stock', 0)
        item_name = item_data.get('ItemName', f'Item-{item_id}')

        # Step 3: WRITE (decrease stock by 1)
        new_stock = max(0, current_stock - 1)
        r = requests.post(
            f'{target_url}/transaction/write',
            json={'txn_id': txn_id, 'item_id': item_id, 'new_stock': new_stock},
            timeout=2
        )
        if r.status_code != 200:
            reason = r.json().get('reason', 'Unknown') if r.text else 'Unknown'
            try:
                requests.post(f'{target_url}/transaction/abort',
                              json={'txn_id': txn_id}, timeout=1)
            except Exception:
                pass
            return False, f"Write Item {item_id} bị ABORT trên Site {target_site}: {reason}"

        # Step 4: COMMIT
        r = requests.post(
            f'{target_url}/transaction/commit',
            json={'txn_id': txn_id},
            timeout=2
        )
        if r.status_code != 200:
            return False, f"Commit thất bại trên Site {target_site}"

        return True, f"Mua '{item_name}' (ID={item_id}) từ Site {target_site} — Stock: {current_stock}→{new_stock}"

    except requests.exceptions.ConnectionError:
        return False, f"KẾT NỐI BỊ TỪ CHỐI — Site {target_site} ĐANG SẬP!"
    except Exception as e:
        return False, f"Lỗi: {str(e)}"


def main():
    print("=" * 60)
    print("CHẾ ĐỘ LIVE DEMO — FAULT TOLERANCE")
    print("=" * 60)
    print("")

    processes = start_servers('2PL')

    print("")
    if not check_health():
        print("ERROR: Không thể khởi động đủ 3 site. Hủy demo.")
        for p in processes:
            p.terminate()
        return

    print("")
    print("=" * 60)
    print("Đang gửi giao dịch liên tục...")
    print("Hãy bấm KILL / REVIVE trên Dashboard để thử nghiệm!")
    print("=" * 60)
    print("")

    txn_count = 0
    success_count = 0
    fail_count = 0

    try:
        while True:
            txn_count += 1
            ok, msg = run_single_transaction(txn_count)

            if ok:
                success_count += 1
                print(f"Giao dịch #{txn_count}: THÀNH CÔNG — {msg}")
            else:
                fail_count += 1
                print(f"Giao dịch #{txn_count}: THẤT BẠI — {msg}")

            # Print stats every 10 transactions
            if txn_count % 10 == 0:
                rate = (success_count / txn_count) * 100
                print(f"")
                print(f"THỐNG KÊ: {txn_count} giao dịch | "
                      f"{success_count} thành công | "
                      f"{fail_count} thất bại | "
                      f"Tỷ lệ thành công: {rate:.1f}%")
                print(f"")

            time.sleep(0.4)  # Delay between transactions for readability

    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        print("")
        print("INFO: Đang dừng tất cả máy chủ...")
        for proc in processes:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        print("INFO: Demo kết thúc.")


if __name__ == '__main__':
    main()
