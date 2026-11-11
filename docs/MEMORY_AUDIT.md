# Kiểm kê kho dữ liệu — Single Database

Ngày kiểm tra: 03-11-2026  
Bản nguồn: `SuperLeague_Clean_Start_MARKET_VALUE_V2_MEMORY_AUDITED.zip` sau khi chuyển sang Single Database.

## 1. File dữ liệu duy nhất được đóng gói

| Đường dẫn | Nội dung | Trạng thái |
|---|---|---|
| `memory/super_league.db` | SQLite schema v5; danh mục 552 cầu thủ nguồn thuộc 24 CLB; kho dữ liệu người gồm 38 trọng tài, 25 HLV, 1.000 tên và 1.000 họ; các bảng career/season archive ban đầu trống | **Giữ — file duy nhất** |

Các bảng quan trọng bên trong database:

- `source_players`: danh mục cầu thủ gốc để dựng 24 đội bóng ban đầu.
- `reference_data`: kho tên, họ, HLV và trọng tài (`human_names`).
- `career_state`: trạng thái sự nghiệp đang chơi và RNG state.
- `players`, `player_metadata`, `contracts`, `club_finances`, `clubs`: projection phục vụ truy vấn nhanh.
- `career_state`: nguồn chuẩn của mùa đang chơi, bao gồm trận đấu, sổ tài chính, hồ sơ sự nghiệp và trạng thái RNG.
- `season_archives`: nguồn chuẩn của các mùa đã hoàn tất, bao gồm log trận, cầu thủ, danh hiệu và báo cáo tài chính.
- `player_season_stats`: giữ riêng vì có các trường thống kê chi tiết theo mùa (morale, chấn thương, GK...) không nằm đầy đủ trong bản tóm tắt archive.
- `player_career` và `player_development`: chỉ giữ lại các bản ghi lịch sử cũ không tìm thấy bản trùng chính xác trong career/archive; không tiếp tục ghi bản sao mới.
- `matches`, `finance_transactions`, `awards`: bảng tương thích schema; không còn được dùng làm bản sao của career/archive. Dữ liệu chuẩn vẫn đọc từ `career_state` và `season_archives`.
- `academy_players`, `medical_records`, `transfer_history`, `news_items`: projection/dữ liệu phát sinh theo sự nghiệp.

## 2. File cũ không còn được đóng gói hoặc sử dụng khi chạy

| File/thư mục cũ | Xử lý |
|---|---|
| `super_league.csv` | Dữ liệu cầu thủ đã được nạp vào `source_players` trong SQLite; xóa file CSV |
| `data_human.json` | Dữ liệu đã được nạp vào `reference_data`; xóa file JSON |
| `save.json` | Career lưu trực tiếp trong `career_state`; không tạo JSON save |
| `generated_players.csv`, `player_meta.csv`, `retired_players.csv` | Dữ liệu cầu thủ/metadata/giải nghệ nằm trong `career_state` và các bảng projection |
| `season/*.json` | Archive mùa nằm trong `season_archives` |
| `snapshots/*.csv` | Snapshot/projection được lưu trong SQLite |
| `backups/`, `recovery/`, `backups.zip` | Không đóng gói, không tự tạo backup lâu dài |
| `google_sheets_status.json` | Trạng thái sync chỉ giữ trong bộ nhớ tiến trình |
| `google_sheets_captures/` | Ảnh phục vụ sync lưu ở thư mục tạm hệ điều hành khi cần |
| `gui_startup.log` | Log lỗi GUI lưu trong thư mục tạm hệ điều hành |

## 3. Dữ liệu cấu hình còn nằm ngoài `memory/`

`config.json`, `config/*.json` và `award_settings.json` là cấu hình ứng dụng (seed, engine, Economy, luật danh hiệu, Google Sheets), không phải kho dữ liệu sự nghiệp. Chúng được giữ riêng để có thể điều chỉnh mà không sửa nội dung career database.

## 4. Hành vi khởi tạo và reset

- Khởi chạy lần đầu đọc `source_players` và `reference_data` từ cùng SQLite rồi tạo career mới.
- `--fresh` và nút reset xóa các bảng career, archive, projections và dữ liệu theo mùa; giữ `source_players` và `reference_data`.
- Không tạo `save.json`, CSV, JSON archive, snapshot hoặc backup lâu dài.
- Google Sheets, Award Recalculator và Control Center đều đọc/ghi dữ liệu sự nghiệp qua `memory/super_league.db`.
- Chức năng xuất mùa/CSV chỉ tạo file ngoài project khi người dùng chủ động chọn nơi xuất.

## 4.1. Chính sách chống lưu trùng sau mỗi mùa

- `save_career()` upsert archive theo mùa; archive không đổi sẽ không bị ghi lại, mùa cũ không bị xóa chỉ vì caller truyền danh sách archive chưa đầy đủ.
- `replace_season_archives()` chỉ dùng cho thao tác nhập/thay thế archive có chủ đích từ Control Center.
- `matches` không còn được sao chép sang bảng SQL thứ hai; trận mùa hiện tại nằm trong `career_state`, trận mùa đã xong nằm trong `season_archives`.
- `finance_transactions` và `awards` không còn được nhân bản; sổ giao dịch nằm trong `career_state.finance_ledger`, danh hiệu nằm trong `season_archives`.
- `player_career`/`player_development` chỉ được compact các dòng khớp chính xác với nguồn chuẩn. Dòng không đối chiếu được được giữ nguyên để tránh mất dữ liệu legacy.
- `player_season_stats` tiếp tục lưu vì giữ các trường thống kê chi tiết không có đầy đủ trong archive tóm tắt.
- Migration `projection_compaction_version=1` chạy một lần trong lần lưu career kế tiếp; sau đó các bản sao mới không được ghi thêm.

## 5. Kiểm thử xác nhận

- `PRAGMA integrity_check` phải trả `ok`.
- `source_players` phải có 552 dòng; `reference_data['human_names']` có đủ 4 nhóm đúng số lượng.
- Sau khởi tạo/reset, thư mục `memory/` chỉ có `super_league.db`.
- Sau khi lưu trận và tạo archive mùa, thư mục `memory/` vẫn chỉ có `super_league.db`.
