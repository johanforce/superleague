# -*- coding: utf-8 -*-
"""One-shot SQLite maintenance tool for Super League.

Run from the project root or src folder:
    py optimize_superleague_db.py
    py optimize_superleague_db.py --db ..\\memory\\super_league.db
    py optimize_superleague_db.py --dry-run

Only exact projection copies are removed. Canonical career/archive data,
player_season_stats, news_items, transfer history, medical records and all
unmatched legacy rows are preserved. A timestamped backup is created first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

TOOL_VERSION = "1.0.0"
COMPACTION_VERSION = "1"
TARGET_TABLES = (
    "matches",
    "finance_transactions",
    "player_career",
    "player_development",
    "awards",
)
PRESERVED_TABLES = (
    "player_season_stats",
    "news_items",
    "transfer_history",
    "medical_records",
    "season_archives",
    "career_state",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True, default=str)


def sha256_text(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def table_count(conn: sqlite3.Connection, table: str) -> int | None:
    if not table_exists(conn, table):
        return None
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def read_canonical_data(conn: sqlite3.Connection):
    state_row = conn.execute(
        "SELECT state_json FROM career_state WHERE id=1"
    ).fetchone()
    state = {}
    if state_row:
        try:
            parsed = json.loads(state_row["state_json"])
            if isinstance(parsed, dict):
                state = parsed
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"career_state.state_json bị lỗi JSON: {exc}") from exc

    archives = []
    invalid_archives = 0
    for row in conn.execute(
        "SELECT season, archive_json FROM season_archives ORDER BY season"
    ).fetchall():
        try:
            record = json.loads(row["archive_json"])
        except (TypeError, ValueError):
            invalid_archives += 1
            continue
        if isinstance(record, dict):
            archives.append(record)
        else:
            invalid_archives += 1

    if not state and not archives:
        raise RuntimeError(
            "Database không có career_state hoặc season_archives hợp lệ; "
            "tool dừng để tránh đụng nhầm database."
        )

    season_value = state.get("season")
    try:
        season = int(season_value or max(
            [int(a.get("season", 0) or 0) for a in archives] or [0]
        ))
    except (TypeError, ValueError):
        season = max([int(a.get("season", 0) or 0) for a in archives] or [0])
    return state, archives, season, invalid_archives


def remove_exact_projection_copies(conn: sqlite3.Connection,
                                   state: dict,
                                   archives: list[dict],
                                   season: int) -> dict[str, int]:
    """Same conservative equality rules as the project's storage migration.

    A row is removed only if its complete JSON payload and identity match a
    canonical record. Invalid JSON and unmatched legacy records are retained.
    """
    deleted: dict[str, int] = {}
    archive_rows = [row for row in archives if isinstance(row, dict)]

    # Matches: archive rows and current-season career matches are canonical.
    match_sources = set()
    for archive in archive_rows:
        try:
            year = int(archive.get("season", season) or season)
        except (TypeError, ValueError):
            continue
        for match in archive.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            match_id = str(match.get(
                "id", f"{match.get('date', '')}-{match.get('home', '')}-{match.get('away', '')}"
            ))
            match_sources.add((f"{year}:{match_id}", canonical_json(match)))
    for match in state.get("matches", []) or []:
        if not isinstance(match, dict):
            continue
        match_id = str(match.get(
            "id", f"{match.get('date', '')}-{match.get('home', '')}-{match.get('away', '')}"
        ))
        match_sources.add((f"{season}:{match_id}", canonical_json(match)))
    match_ids = []
    if table_exists(conn, "matches"):
        for row in conn.execute("SELECT match_id, match_json FROM matches").fetchall():
            try:
                payload = canonical_json(json.loads(row["match_json"]))
            except (TypeError, ValueError):
                continue
            if (str(row["match_id"]), payload) in match_sources:
                match_ids.append((row["match_id"],))
        if match_ids:
            conn.executemany("DELETE FROM matches WHERE match_id=?", match_ids)
    deleted["matches"] = len(match_ids)

    # Current career finance ledger: delete only exact mirror rows.
    finance_sources = {}
    for index, tx in enumerate(state.get("finance_ledger", []) or []):
        if not isinstance(tx, dict):
            continue
        txid = str(tx.get("id", tx.get("idempotencyKey", f"{season}-{index}")))
        finance_sources[txid] = canonical_json(tx)
    finance_ids = []
    if table_exists(conn, "finance_transactions"):
        for row in conn.execute(
            "SELECT transaction_id, transaction_json FROM finance_transactions"
        ).fetchall():
            try:
                payload = canonical_json(json.loads(row["transaction_json"]))
            except (TypeError, ValueError):
                continue
            if finance_sources.get(str(row["transaction_id"])) == payload:
                finance_ids.append((row["transaction_id"],))
        if finance_ids:
            conn.executemany(
                "DELETE FROM finance_transactions WHERE transaction_id=?", finance_ids
            )
    deleted["finance_transactions"] = len(finance_ids)

    # Player career: match player + season + club + full JSON.
    career_sources: dict[tuple[str, int, str], set[str]] = {}
    for player_id, rows in (state.get("player_career") or {}).items():
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            try:
                year = int(item.get("season", season) or season)
            except (TypeError, ValueError):
                year = season
            key = (str(player_id), year, str(item.get("club", "")))
            career_sources.setdefault(key, set()).add(canonical_json(item))
    for archive in archive_rows:
        try:
            year = int(archive.get("season", season) or season)
        except (TypeError, ValueError):
            continue
        for item in archive.get("career_player_stats", []) or []:
            if not isinstance(item, dict):
                continue
            player_id = str(item.get("player_id", ""))
            if player_id:
                key = (player_id, year, str(item.get("team", "")))
                career_sources.setdefault(key, set()).add(canonical_json(item))
    career_keys = []
    if table_exists(conn, "player_career"):
        for row in conn.execute(
            "SELECT player_id, season, club, career_json FROM player_career"
        ).fetchall():
            key = (str(row["player_id"]), int(row["season"]), str(row["club"]))
            try:
                payload = canonical_json(json.loads(row["career_json"]))
            except (TypeError, ValueError):
                continue
            if payload in career_sources.get(key, set()):
                career_keys.append(key)
        if career_keys:
            conn.executemany(
                "DELETE FROM player_career WHERE player_id=? AND season=? AND club=?",
                career_keys,
            )
    deleted["player_career"] = len(career_keys)

    # Player development: exact player + season + JSON matches only.
    development_sources: dict[tuple[str, int], set[str]] = {}
    for archive in archive_rows:
        try:
            year = int(archive.get("season", season) or season)
        except (TypeError, ValueError):
            continue
        for item in archive.get("development", []) or []:
            if isinstance(item, dict) and item.get("player_id"):
                development_sources.setdefault((str(item["player_id"]), year), set()).add(canonical_json(item))
    for item in state.get("last_development", []) or []:
        if isinstance(item, dict) and item.get("player_id"):
            development_sources.setdefault((str(item["player_id"]), season), set()).add(canonical_json(item))
    development_keys = []
    if table_exists(conn, "player_development"):
        for row in conn.execute(
            "SELECT player_id, season, development_json FROM player_development"
        ).fetchall():
            key = (str(row["player_id"]), int(row["season"]))
            try:
                payload = canonical_json(json.loads(row["development_json"]))
            except (TypeError, ValueError):
                continue
            if payload in development_sources.get(key, set()):
                development_keys.append(key)
        if development_keys:
            conn.executemany(
                "DELETE FROM player_development WHERE player_id=? AND season=?",
                development_keys,
            )
    deleted["player_development"] = len(development_keys)

    # Awards: remove only rows that are exact indexes of archive awards.
    award_sources = set()
    for archive in archive_rows:
        try:
            year = int(archive.get("season", season) or season)
        except (TypeError, ValueError):
            continue
        for award_type in (
            "season_awards", "competition_awards", "individual_awards",
            "team_honors", "team_awards",
        ):
            value = archive.get(award_type)
            if isinstance(value, dict):
                entries = list(value.items())
            elif isinstance(value, list):
                entries = [
                    (str(item.get("player", item.get("team", item.get("name", index)))), item)
                    for index, item in enumerate(value) if isinstance(item, dict)
                ]
            else:
                entries = []
            for recipient, award in entries:
                team = str(award.get("team", award.get("club", ""))) if isinstance(award, dict) else ""
                award_sources.add((year, award_type, str(recipient), team, canonical_json(award)))
    award_ids = []
    if table_exists(conn, "awards"):
        for row in conn.execute(
            "SELECT award_id, season, award_type, recipient, team, award_json FROM awards"
        ).fetchall():
            try:
                payload = canonical_json(json.loads(row["award_json"]))
            except (TypeError, ValueError):
                continue
            key = (
                int(row["season"] or 0), str(row["award_type"]),
                str(row["recipient"]), str(row["team"]), payload,
            )
            if key in award_sources:
                award_ids.append((row["award_id"],))
        if award_ids:
            conn.executemany("DELETE FROM awards WHERE award_id=?", award_ids)
    deleted["awards"] = len(award_ids)

    # Keep a version marker, but the tool deliberately rechecks exact matches
    # every time it runs, so rows copied back by an old app version can be cleaned.
    if table_exists(conn, "metadata"):
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('projection_compaction_version',?)",
            (COMPACTION_VERSION,),
        )
    return deleted


def snapshot_counts(conn: sqlite3.Connection) -> dict[str, int | None]:
    return {table: table_count(conn, table)
            for table in TARGET_TABLES + PRESERVED_TABLES}


def backup_database(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = backup_dir / f"{db_path.stem}.before_optimize_{stamp}{db_path.suffix or '.db'}"
    source = connect(db_path)
    try:
        destination = sqlite3.connect(str(backup_path), timeout=30)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return backup_path


def run_maintenance(db_path: Path, *, make_backup: bool,
                    backup_dir: Path | None, vacuum: bool = True) -> dict[str, Any]:
    before_size = db_path.stat().st_size
    conn = connect(db_path)
    backup_path = None
    try:
        check = [row[0] for row in conn.execute("PRAGMA quick_check").fetchall()]
        if check != ["ok"]:
            raise RuntimeError("PRAGMA quick_check không trả về 'ok': " + "; ".join(check[:5]))

        required = ("metadata", "career_state", "season_archives")
        missing = [name for name in required if not table_exists(conn, name)]
        if missing:
            raise RuntimeError("Database thiếu bảng bắt buộc: " + ", ".join(missing))

        if make_backup:
            backup_path = backup_database(db_path, backup_dir or (db_path.parent / "db_backups"))

        state, archives, season, invalid_archives = read_canonical_data(conn)
        before_counts = snapshot_counts(conn)
        state_hash_before = ""
        row = conn.execute("SELECT state_json FROM career_state WHERE id=1").fetchone()
        if row:
            state_hash_before = sha256_text(row[0])
        archive_hash_before = hashlib.sha256()
        for row in conn.execute(
            "SELECT season, archive_json FROM season_archives ORDER BY season"
        ).fetchall():
            archive_hash_before.update(str(row["season"]).encode("utf-8"))
            archive_hash_before.update((row["archive_json"] or "").encode("utf-8"))

        conn.execute("BEGIN IMMEDIATE")
        deleted = remove_exact_projection_copies(conn, state, archives, season)
        check_after = [row[0] for row in conn.execute("PRAGMA quick_check").fetchall()]
        if check_after != ["ok"]:
            raise RuntimeError("Kiểm tra database sau dọn không đạt; transaction sẽ rollback.")
        conn.commit()

        # Assert all canonical and explicitly preserved tables are untouched.
        after_counts = snapshot_counts(conn)
        for table in PRESERVED_TABLES:
            if before_counts.get(table) != after_counts.get(table):
                raise RuntimeError(f"Bảng cần giữ nguyên bị thay đổi ngoài dự kiến: {table}")
        row = conn.execute("SELECT state_json FROM career_state WHERE id=1").fetchone()
        state_hash_after = sha256_text(row[0] if row else None)
        if state_hash_before != state_hash_after:
            raise RuntimeError("career_state đã thay đổi ngoài dự kiến.")
        archive_hash_after = hashlib.sha256()
        for row in conn.execute(
            "SELECT season, archive_json FROM season_archives ORDER BY season"
        ).fetchall():
            archive_hash_after.update(str(row["season"]).encode("utf-8"))
            archive_hash_after.update((row["archive_json"] or "").encode("utf-8"))
        if archive_hash_before.digest() != archive_hash_after.digest():
            raise RuntimeError("season_archives đã thay đổi ngoài dự kiến.")

        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        free_pages_before_vacuum = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        reclaimable = page_size * free_pages_before_vacuum
        vacuum_done = False
        vacuum_note = ""
        if vacuum and reclaimable >= 1024 * 1024:
            free_bytes = shutil.disk_usage(db_path.parent).free
            if free_bytes < max(before_size, reclaimable) + 16 * 1024 * 1024:
                vacuum_note = (
                    "Không chạy VACUUM vì ổ đĩa còn trống không đủ an toàn; "
                    "các dòng trùng đã được dọn và có thể chạy lại tool khi đủ dung lượng."
                )
            else:
                try:
                    conn.execute("VACUUM")
                    vacuum_done = True
                except sqlite3.Error as exc:
                    vacuum_note = (
                        f"Các bản sao đã được dọn nhưng VACUUM không chạy được: {exc}. "
                        "Backup vẫn được giữ; đóng các ứng dụng đang truy cập DB và chạy lại tool."
                    )
        elif vacuum:
            vacuum_note = "Không cần VACUUM: không có từ 1 MiB trang trống để thu hồi."
        conn.execute("PRAGMA optimize")
        after_size = db_path.stat().st_size
        after_quick = [row[0] for row in conn.execute("PRAGMA quick_check").fetchall()]
        if after_quick != ["ok"]:
            raise RuntimeError("quick_check sau VACUUM không đạt.")

        return {
            "db_path": str(db_path.resolve()),
            "backup_path": str(backup_path.resolve()) if backup_path else None,
            "before_size": before_size,
            "after_size": after_size,
            "deleted": deleted,
            "before_counts": before_counts,
            "after_counts": snapshot_counts(conn),
            "freelist_reclaimable_bytes": reclaimable,
            "vacuum_done": vacuum_done,
            "vacuum_note": vacuum_note,
            "invalid_archives": invalid_archives,
            "integrity": "ok",
        }
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def find_default_database() -> Path | None:
    script_dir = Path(__file__).resolve().parent
    cwd = Path.cwd().resolve()
    candidates = [
        cwd / "memory" / "super_league.db",
        cwd.parent / "memory" / "super_league.db",
        script_dir / "memory" / "super_league.db",
        script_dir.parent / "memory" / "super_league.db",
    ]
    seen = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key not in seen and candidate.is_file():
            return candidate.resolve()
        seen.add(key)
    return None


def format_mib(value: int) -> str:
    return f"{value / (1024 * 1024):,.2f} MiB"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dọn bản sao dữ liệu an toàn và thu nhỏ SQLite database Super League."
    )
    parser.add_argument("--db", type=Path, help="Đường dẫn database; mặc định tự tìm memory/super_league.db.")
    parser.add_argument("--backup-dir", type=Path, help="Thư mục backup; mặc định memory/db_backups.")
    parser.add_argument("--dry-run", action="store_true", help="Chạy thử trên bản sao tạm; không sửa database thật.")
    parser.add_argument("--yes", action="store_true", help="Bỏ qua xác nhận tương tác.")
    parser.add_argument("--no-vacuum", action="store_true", help="Chỉ dọn bản ghi, không thu nhỏ file SQLite.")
    args = parser.parse_args()

    db_path = args.db.expanduser().resolve() if args.db else find_default_database()
    if not db_path or not db_path.is_file():
        print("KHÔNG TÌM THẤY memory/super_league.db.")
        print("Hãy chạy file này trong thư mục src của project hoặc truyền --db <đường_dẫn_db>.")
        return 2

    size = db_path.stat().st_size
    print("=" * 68)
    print(f"SUPER LEAGUE DATABASE MAINTENANCE TOOL v{TOOL_VERSION}")
    print(f"Database: {db_path}")
    print(f"Dung lượng hiện tại: {format_mib(size)}")
    print("News policy: KHÔNG ĐỤNG TỚI news_items.")
    print("Giữ nguyên: career_state, season_archives, player_season_stats, transfer_history, medical_records.")
    print("Chỉ xóa bản sao projection khi ID và toàn bộ JSON khớp dữ liệu nguồn.")
    print("=" * 68)

    if not args.yes and not args.dry_run:
        answer = input("Đóng game Super League trước. Tiếp tục sau khi đã backup? [y/N]: ").strip().lower()
        if answer not in {"y", "yes", "c", "co"}:
            print("Đã hủy. Database chưa bị thay đổi.")
            return 0

    try:
        if args.dry_run:
            with tempfile.TemporaryDirectory(prefix="superleague_db_dryrun_") as temp_dir:
                temp_db = Path(temp_dir) / db_path.name
                source = connect(db_path)
                destination = sqlite3.connect(str(temp_db))
                try:
                    source.backup(destination)
                finally:
                    source.close()
                    destination.close()
                result = run_maintenance(
                    temp_db, make_backup=False, backup_dir=None,
                    vacuum=not args.no_vacuum,
                )
            print("\nDRY RUN — database thật không bị thay đổi.")
        else:
            backup_dir = args.backup_dir.expanduser().resolve() if args.backup_dir else (db_path.parent / "db_backups")
            result = run_maintenance(
                db_path, make_backup=True, backup_dir=backup_dir,
                vacuum=not args.no_vacuum,
            )

        print("\nKẾT QUẢ")
        print(f"  Database:       {result['db_path']}")
        if result["backup_path"]:
            print(f"  Backup:         {result['backup_path']}")
        print(f"  Trước:          {format_mib(result['before_size'])}")
        print(f"  Sau:            {format_mib(result['after_size'])}")
        saved = result["before_size"] - result["after_size"]
        print(f"  Chênh lệch:     {format_mib(abs(saved))} {'giảm' if saved >= 0 else 'tăng'}")
        print("\nSỐ BẢN GHI TRÙNG ĐÃ XÓA")
        for table in TARGET_TABLES:
            print(f"  {table:24s}: {result['deleted'].get(table, 0):>8,}")
        print("\nBẢNG ĐƯỢC GIỮ NGUYÊN")
        for table in PRESERVED_TABLES:
            print(f"  {table:24s}: {result['after_counts'].get(table)} dòng")
        print(f"  news_items:              {result['after_counts'].get('news_items')} dòng (không chỉnh sửa)")
        print(f"\n  SQLite quick_check: {result['integrity']}")
        print(f"  VACUUM: {'đã chạy' if result['vacuum_done'] else 'không chạy'}")
        if result["vacuum_note"]:
            print(f"  Ghi chú: {result['vacuum_note']}")
        if result["invalid_archives"]:
            print(f"  Cảnh báo: {result['invalid_archives']} archive JSON không đọc được; các bản ghi liên quan được giữ nguyên.")
        if args.dry_run:
            print("\nKhông có thay đổi nào được ghi vào database thật.")
        else:
            print("\nHoàn tất. Backup được giữ nguyên trong thư mục db_backups.")
        return 0
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"\nLỖI: {exc}")
        print("Nếu lỗi do database đang được game sử dụng, hãy đóng game rồi chạy lại.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
