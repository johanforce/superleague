# Khắc phục lỗi thiếu memory/super_league.csv

Nếu traceback có dạng:

```text
load_source_csv(self.csv_path)
FileNotFoundError: ... memory/super_league.csv
```

thì chương trình đang chạy bản mã nguồn cũ. Bản Single Database này không gọi `load_source_csv`; trong `Game.__init__` nó đọc danh mục bằng `self.database.load_source_players()` từ `memory/super_league.db`.

## Cách thay đúng bản

1. Đóng hoàn toàn Super League và cửa sổ Python đang chạy.
2. Đổi tên thư mục đang lỗi, không giải nén chồng lên đó. Ví dụ `superleague` thành `superleague_old`.
3. Giải nén ZIP này thành thư mục mới.
4. Mở Terminal và `cd` vào thư mục mới vừa giải nén.
5. Chạy:

```bash
python - <<'PY'
from pathlib import Path
p = Path("src/sl_game.py")
s = p.read_text(encoding="utf-8")
print("SQLite loader:", "load_source_players" in s)
print("Legacy CSV loader:", "load_source_csv" in s)
print("Project:", Path.cwd())
print("Source:", p.resolve())
PY
python src/main.py --db-status
```

Kỳ vọng `SQLite loader: True`, `Legacy CSV loader: False`, integrity `ok`, `source_players` = 552 và DB path nằm trong thư mục mới.

6. Mở ứng dụng bằng `python src/main.py` hoặc launcher đi kèm.

**Không tạo lại `super_league.csv` để chữa lỗi.** Điều đó sẽ đưa kiến trúc cũ trở lại. Cần chạy đúng mã nguồn Single Database cùng database được đóng gói.
