# Điều khiển Đồng thời: 2-Phase Locking vs Timestamp Ordering

## Tổng quan Dự án

Dự án Bài thu hoạch — **So sánh Hiệu suất "Locking vs. Timestamps"**.

Dự án này mô phỏng một hệ thống cơ sở dữ liệu phân tán với 3 máy chủ (sites) nhằm so sánh hai giao thức điều khiển đồng thời (concurrency control) cơ bản: **2-Phase Locking (2PL)** và **Timestamp Ordering (TO)**. Mục tiêu là tìm ra **điểm giao cắt (crossover point)** - nơi mà tỷ lệ hủy giao dịch (abort rate) của TO vượt quá chi phí chờ đợi (waiting cost) của 2PL khi mật độ xung đột (conflict density) tăng lên.

## Sơ đồ Kiến trúc

```
                    ┌───────────────────────────────────┐
                    │        Benchmark Runner            │
                    │  (benchmark.py + simulator)        │
                    └──────┬────────┬────────┬──────────┘
                           │        │        │
                    HTTP/REST    HTTP/REST   HTTP/REST
                           │        │        │
              ┌────────────▼──┐ ┌───▼─────────┐ ┌──▼────────────┐
              │   Máy chủ 1   │ │   Máy chủ 2  │ │   Máy chủ 3   │
              │   :5001       │ │   :5002      │ │   :5003       │
              │  Điều phối    │ │              │ │               │
              │  ID: 1-333    │ │  ID: 334-666 │ │  ID: 667-1000 │
              │  Điện tử      │ │  Quần áo     │ │  Thực phẩm    │
              │               │ │              │ │               │
              │ ┌───────────┐ │ │ ┌──────────┐ │ │ ┌───────────┐ │
              │ │ LockMgr / │ │ │ │ LockMgr /│ │ │ │ LockMgr / │ │
              │ │ TimestMgr │ │ │ │ TimestMgr│ │ │ │ TimestMgr │ │
              │ └───────────┘ │ │ └──────────┘ │ │ └───────────┘ │
              │ ┌───────────┐ │ │ ┌──────────┐ │ │ ┌───────────┐ │
              │ │ site1.db  │ │ │ │ site2.db │ │ │ │ site3.db  │ │
              │ │ (SQLite)  │ │ │ │ (SQLite) │ │ │ │ (SQLite)  │ │
              │ └───────────┘ │ │ └──────────┘ │ │ └───────────┘ │
              └───────────────┘ └──────────────┘ └───────────────┘
                    ▲                                     ▲
                    │        SẢN PHẨM HOT (ID 1-10)       │
                    │◄── Vùng xung đột dữ liệu cao ───────│
```

## Dữ liệu (Dataset)

**Nguồn:** UCI Machine Learning Repository — Online Retail Dataset

- **Bản gốc:** 541,909 dòng lịch sử giao dịch từ một cửa hàng bán lẻ trực tuyến ở Anh (12/2010 – 12/2011)
- **Đã xử lý:** 1,000 sản phẩm độc nhất (unique) được trích xuất với tên và giá sản phẩm thực tế
- **Cấu trúc (Schema):** ItemID, ItemName, Category, Stock (tồn kho ngẫu nhiên 10-100), Price (giá thực), Version
- **Phân mảnh (Fragmentation):** Phân mảnh ngang theo Danh mục (Điện tử → Site 1, Quần áo → Site 2, Thực phẩm → Site 3)
- **Sản phẩm Hot:** Các sản phẩm có ItemID từ 1–10 được đánh dấu là "hot items", tỷ lệ truy cập vào các sản phẩm này được quyết định bởi hệ số `conflict_density`

*(Lưu ý: Nếu không thể tải dataset từ UCI do mất mạng, script sẽ tự động tạo dữ liệu tổng hợp giả lập).*

## Hướng dẫn Chạy Dự án

### Bước 1: Cài đặt thư viện

```bash
cd locking_vs_timestamps
pip install -r requirements.txt
```

### Bước 2: Khởi tạo Dữ liệu

```bash
python generate_data.py
```

Lệnh này sẽ tạo ra các file `data/site1.db`, `data/site2.db`, `data/site3.db` và `data/inventory.csv`.

### Bước 3: Chạy Benchmark Thực nghiệm (Tự động)

Quá trình benchmark sẽ tự động bật và tắt các máy chủ:

```bash
python benchmark.py
```

Các tham số tùy chọn:
```bash
python benchmark.py --threads 30 --runs 5      # Chạy nhanh hơn với ít luồng và ít vòng lặp hơn
python benchmark.py --failure-test              # Chạy thử nghiệm mô phỏng máy chủ bị sập
```

### Bước 3 (Cách khác): Chạy thủ công

Nếu bạn muốn tự bật các máy chủ:

**Trên Mac/Linux:**
```bash
# Terminal 1:
PROTOCOL=2PL python site_server.py --site 1 --port 5001
# Terminal 2:
PROTOCOL=2PL python site_server.py --site 2 --port 5002
# Terminal 3:
PROTOCOL=2PL python site_server.py --site 3 --port 5003
```

**Trên Windows CMD:**
```cmd
:: Terminal 1:
set PROTOCOL=2PL
python site_server.py --site 1 --port 5001
:: Terminal 2:
set PROTOCOL=2PL
python site_server.py --site 2 --port 5002
:: Terminal 3:
set PROTOCOL=2PL
python site_server.py --site 3 --port 5003
```

**Trên Windows PowerShell:**
```powershell
# Terminal 1:
$env:PROTOCOL="2PL"
python site_server.py --site 1 --port 5001
# Terminal 2:
$env:PROTOCOL="2PL"
python site_server.py --site 2 --port 5002
# Terminal 3:
$env:PROTOCOL="2PL"
python site_server.py --site 3 --port 5003
```

Sau đó chạy script benchmark:
```bash
python benchmark.py --manual
```

### Bước 4: Vẽ Biểu đồ

```bash
python visualizer.py
```

Các biểu đồ sẽ được lưu vào thư mục `results/`.

## Kết quả Mong đợi

### Giả thuyết về Điểm giao cắt (Crossover Point)

- **Mật độ xung đột thấp (10-30%):** TO hoạt động tốt hơn vì không tốn chi phí quản lý Lock. Ít xung đột đồng nghĩa với việc ít giao dịch bị hủy (abort).
- **Mật độ xung đột trung bình (40-60%):** Điểm giao cắt dự kiến sẽ nằm ở khu vực này. Tỷ lệ abort của TO bắt đầu tăng mạnh do nhiều giao dịch cùng truy cập vào các sản phẩm Hot gây ra vi phạm quy tắc Timestamp.
- **Mật độ xung đột cao (70-90%):** 2PL tỏ ra hiệu quả hơn. Dù tốn chi phí khóa dữ liệu (Locking) và rủi ro Deadlock, việc các giao dịch "chờ đợi một cách có trật tự" vẫn tốt hơn so với vòng lặp Abort-Restart liên tục của TO.

### Các Chỉ số Chính

| Chỉ số | Hành vi của 2PL | Hành vi của TO |
|--------|-------------|-------------|
| Tỷ lệ Hủy (Abort Rate) | Thấp (Chỉ khi xảy ra Deadlock) | Tăng tỷ lệ thuận với mức độ xung đột |
| Thông lượng (Throughput) | Giảm do thời gian chờ đợi khóa | Giảm do phải thực hiện lại giao dịch |
| Độ trễ (Latency) | Tăng do thời gian chờ Lock | Tăng do thời gian Retry |

## Mô tả Cấu trúc File

| File | Chức năng |
|------|-------------|
| `generate_data.py` | Tải dữ liệu từ UCI và tạo 3 CSDL SQLite |
| `lock_manager.py` | Quản lý 2-Phase Locking với tính năng dò Deadlock (DFS trên wait-for graph) |
| `timestamp_manager.py` | Quản lý Timestamp Ordering với quy tắc Thomas Write Rule |
| `site_server.py` | Máy chủ Flask REST API (đại diện cho một site) |
| `transaction_simulator.py` | Trình tạo các truy vấn giao dịch đồng thời |
| `benchmark.py` | Trình chạy thực nghiệm (5 mức độ xung đột × 2 thuật toán) |
| `visualizer.py` | Vẽ biểu đồ (abort rate, throughput, latency heatmap) |
| `requirements.txt` | Các thư viện Python cần cài đặt |

## Các Thuật toán Điều khiển Đồng thời

### 2-Phase Locking (2PL)

Giao dịch phải lấy khóa (Lock) trước khi truy cập dữ liệu và chỉ giải phóng toàn bộ khóa khi Commit hoặc Abort.

**Ma trận Tương thích Khóa:**

|          | Yêu cầu S | Yêu cầu X |
|----------|:---------:|:---------:|
| **Đang giữ S** | ✅ Tương thích | ❌ Không tương thích |
| **Đang giữ X** | ❌ Không tương thích | ❌ Không tương thích |

- **Growing Phase:** Lấy khóa khi cần
- **Shrinking Phase:** Giải phóng toàn bộ khóa cùng lúc ở cuối giao dịch (Strict 2PL)
- **Deadlock:** Phát hiện qua đồ thị Wait-for (DFS), giải quyết bằng cách hủy giao dịch "trẻ nhất" (youngest)

### Timestamp Ordering (TO)

Mỗi giao dịch được cấp một Timestamp duy nhất khi bắt đầu. Các thao tác Đọc/Ghi được xác thực dựa trên read_ts và write_ts của từng dữ liệu.

**Quy tắc:**

| Thao tác | Điều kiện | Hành động |
|-----------|-----------|--------|
| ĐỌC | `txn_ts < write_ts[item]` | ABORT (Quá muộn) |
| ĐỌC | `txn_ts ≥ write_ts[item]` | Cho phép, cập nhật `read_ts` |
| GHI | `txn_ts < read_ts[item]` | ABORT (Quá muộn) |
| GHI | `txn_ts < write_ts[item]` | BỎ QUA (Thomas Write Rule) |
| GHI | `txn_ts ≥ write_ts[item]` | Cho phép, cập nhật `write_ts` |

Khi bị ABORT: Giao dịch sẽ được khởi động lại với một **Timestamp mới (hiện tại)** lớn hơn để tránh xung đột.

## Trích dẫn Dữ liệu

```
Chen, D. (2015). Online Retail [Dataset]. UCI Machine Learning Repository.
https://doi.org/10.24432/C5BW33
License: CC BY 4.0 (Creative Commons Attribution 4.0 International)
```

## Yêu cầu Hệ thống

- Python 3.9+
- Chỉ dùng SQLite (không cần cài CSDL ngoài)
- Giao tiếp giữa các Site bằng HTTP/REST
- Không cần dùng Docker — chạy trực tiếp trên máy chủ cục bộ (localhost)
