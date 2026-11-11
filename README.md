# Super League Simulator — Clean Start

## Bản khởi tạo trắng — Single Database

Đây là bản Clean Start dùng **duy nhất một file dữ liệu**: `memory/super_league.db`.
File SQLite đã chứa sẵn danh mục 552 cầu thủ/24 CLB cùng kho tên gồm trọng tài, HLV, tên và họ. Khi mở app lần đầu, sự nghiệp mới được tạo và lưu ngay trong chính file đó.

Trong `memory/` sẽ không còn `super_league.csv`, `data_human.json`, `save.json`, CSV cầu thủ sinh thêm, archive JSON, snapshot, backup hay file trạng thái đồng bộ. Dữ liệu sự nghiệp, trận đấu, tài chính, hợp đồng, chuyển nhượng, học viện, giải nghệ, danh hiệu và lịch sử mùa đều được lưu trong các bảng SQLite.

**Reset sự nghiệp** xóa dữ liệu career và archive trong database, nhưng giữ danh mục cầu thủ và kho tên để tạo career mới. Không tạo backup. Thao tác reset không thể hoàn tác.

Các file `config.json`, `config/` và `award_settings.json` ở ngoài `memory/` là cấu hình ứng dụng, không phải kho dữ liệu sự nghiệp. Chúng vẫn được giữ để người dùng tùy chỉnh luật mô phỏng, Economy và đồng bộ Google Sheets.

> Nên giải nén vào thư mục mới. Nếu cập nhật thủ công một thư mục đã dùng trước đây, cần đóng app và chạy `python src/main.py --fresh` để dọn các sidecar cũ trong `memory/`.

## Kiểm kê dữ liệu trong `memory/`

Bản ZIP chỉ chứa `memory/super_league.db`. Kiểm kê chi tiết các bảng SQLite và những file bị loại nằm trong [`docs/MEMORY_AUDIT.md`](docs/MEMORY_AUDIT.md).

## Lưu ý cập nhật từ bản cũ

Không giải nén chồng lên thư mục Super League cũ. Hãy đóng app, đổi tên thư mục cũ (ví dụ `superleague_old`) rồi giải nén bản này thành một thư mục mới. Bản Single Database hiện tại nạp 552 cầu thủ nguồn bằng `SuperLeagueDatabase.load_source_players()`; `src/sl_game.py` không gọi `load_source_csv()` và không cần `memory/super_league.csv`. Nếu traceback vẫn báo thiếu `memory/super_league.csv`, bạn đang chạy mã nguồn cũ từ thư mục cũ chứ không phải bản này.

Kiểm tra đúng bản đang chạy bằng lệnh từ thư mục gốc:

```bash
python - <<'PYCODE'
from pathlib import Path
p = Path("src/sl_game.py")
s = p.read_text(encoding="utf-8")
print("SQLite source loader:", "load_source_players" in s)
print("Legacy CSV loader:", "load_source_csv" in s)
print("Source file:", p.resolve())
PYCODE
python src/main.py --db-status
```

Kết quả mong đợi: `SQLite source loader: True`, `Legacy CSV loader: False`, và đường dẫn DB trỏ đến thư mục hiện tại.

## Chạy ứng dụng

### Windows

Chạy `START_SUPER_LEAGUE.bat`. Script tạo `.venv`, cài các thư viện trong `requirements.txt` và mở Control Center.

### macOS

Chạy `RUN_SUPER_LEAGUE.command`. Nếu macOS chặn file lần đầu, cấp quyền thực thi rồi chạy lại:

```bash
chmod +x RUN_SUPER_LEAGUE.command
./RUN_SUPER_LEAGUE.command
```

### Linux / chạy trực tiếp

Cần Python 3.10+ và Tkinter:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python src/super_league_hub.py
```

Chạy trực tiếp Simulator thay vì Control Center:

```bash
python src/main.py
```

Tạo mới hoàn toàn từ command line:

```bash
python src/main.py --fresh
```

Chạy một mùa ở chế độ CLI:

```bash
python src/main.py --sim 1 --fresh
```

Kiểm tra database hiện tại:

```bash
python src/main.py --db-status
```

## Định giá thị trường cầu thủ

Giá trị thị trường được lưu theo **triệu euro (M€)** và tự tính lại theo từng cầu thủ:

- **Năng lực hiện tại:** OVR là nền định giá chính; các chỉ số chuyên môn quan trọng theo vị trí (ví dụ GK dùng bộ chỉ số thủ môn, CB dùng tranh chấp/phòng ngự, ST dùng dứt điểm/tốc độ) điều chỉnh thêm tối đa khoảng ±8%.
- **Tiềm năng:** cầu thủ tối đa 20 tuổi có thể được cộng tối đa 45%; 21–23 tuổi tối đa 30%; 24–25 tuổi tối đa 12%. Từ 26 tuổi không còn phụ phí tiềm năng.
- **Tuổi:** hệ số tuổi được nội suy mượt, đạt đỉnh quanh 27 tuổi rồi giảm dần; từ 32 tuổi mức khấu hao rõ rệt và tiếp tục giảm khi tiến gần tuổi giải nghệ.
- **Cập nhật:** toàn bộ cầu thủ hiện có và cầu thủ học viện được định giá lại một lần khi nâng mô hình; giá tiếp tục được cập nhật sau mỗi mùa và khi tạo cầu thủ trẻ mới. Giá tham chiếu trong hợp đồng được đồng bộ với giá cầu thủ.

Mô hình định giá phiên bản 2 được lưu bằng `market_value_model_version`; việc nâng mô hình không thay đổi OVR, Potential, lương hay kết quả thi đấu.

## Báo cáo trận Major Cup / C1

Báo cáo trận Major Cup và C1 giữ nguyên 4 tab giao diện: **Tổng quan**, **Thống kê**, **Cầu thủ** và **Diễn biến chính**. Vì lịch sử các giải này được lưu ở dạng gọn, nội dung tập trung vào cầu thủ ghi bàn, thẻ đỏ (nếu có), dấu **AET** và tỷ số luân lưu. Hai tab Thống kê và Cầu thủ vẫn được giữ lại nhưng sẽ giải thích rằng dữ liệu chi tiết không nằm trong bản lưu gọn, thay vì hiển thị số liệu giả.

## Lưu trữ dữ liệu

`memory/super_league.db` là **nguồn dữ liệu duy nhất**. Mọi dữ liệu phát sinh gồm cầu thủ, đội hình, trận đấu, lịch thi đấu, tài chính, hợp đồng, chuyển nhượng, phát triển cầu thủ, chấn thương, danh hiệu, tin tức, tên người và season archive đều nằm trong cùng file SQLite.

Không có JSON/CSV mirror và không ghi archive vào thư mục riêng. Google Sheets đọc dữ liệu trực tiếp từ SQLite; ảnh chụp phục vụ đồng bộ (khi người dùng chủ động bật) được lưu trong thư mục tạm của hệ điều hành, không nằm trong `memory/`. Chức năng xuất bảng CSV và xuất archive mùa, nếu được người dùng chủ động gọi, chỉ tạo file ở thư mục ngoài do họ chọn.

SQLite dùng thư viện `sqlite3` đi kèm Python, không cần cài máy chủ database. Reset và Clean Start không tạo backup lâu dài. Các thao tác cập nhật an toàn có thể dùng file staging tạm thời trong lúc thực thi và xóa staging sau khi hoàn tất.

## Kiểm tra project

Từ thư mục gốc project:

```bash
python -m unittest test.test_database test.test_season_archive test.test_hub_data_exchange -v
python -m unittest test.test_economy -v
python -m unittest test.test_simulator -v
```

Smoke test lịch thi đấu và engine:

```bash
python src/main.py --check
```

## Google Sheets (tùy chọn)

Google Sheets không bắt buộc để chạy app. Nếu cần đồng bộ, cài thêm:

```bash
python -m pip install -r requirements-google-sheets.txt
```

Sau đó cấu hình Apps Script URL / credential trong `config.json` theo hướng dẫn tích hợp của project. Không đưa credential cá nhân vào gói chia sẻ.
"# superleague" 
