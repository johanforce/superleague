# -*- coding: utf-8 -*-
"""Safe export/import for the single-file Super League SQLite database.

The portable TXT file is only an interchange envelope. At runtime the app keeps
all source catalogues, career state, projections and season archives in
memory/super_league.db; importing a bundle replaces that database atomically.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

FORMAT_NAME = "SUPER_LEAGUE_SINGLE_DATABASE"
FORMAT_VERSION = 3
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
DATABASE_RELATIVE_PATH = "memory/super_league.db"
REQUIRED_SCHEMA_VERSION = 5


class DataBundleError(ValueError):
    """Invalid or incompatible Super League database bundle."""


def _database_info(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise DataBundleError(f"Không tìm thấy database {path}.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DataBundleError(f"Database vượt giới hạn {MAX_FILE_BYTES:,} bytes.")
    try:
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise DataBundleError(f"SQLite integrity_check: {integrity}")
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            required = {"metadata", "source_players", "reference_data", "career_state", "season_archives"}
            missing = required - tables
            if missing:
                raise DataBundleError("Database thiếu bảng: " + ", ".join(sorted(missing)))
            version_row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            schema_version = int(version_row[0]) if version_row else 0
            if schema_version < REQUIRED_SCHEMA_VERSION:
                raise DataBundleError(
                    f"Schema database {schema_version} quá cũ; cần schema {REQUIRED_SCHEMA_VERSION} trở lên."
                )
            source_count = int(conn.execute("SELECT COUNT(*) FROM source_players").fetchone()[0])
            if source_count != 552:
                raise DataBundleError(f"Database có {source_count} cầu thủ nguồn; cần đúng 552.")
            human_row = conn.execute(
                "SELECT data_json FROM reference_data WHERE data_key='human_names'"
            ).fetchone()
            if not human_row:
                raise DataBundleError("Database thiếu reference_data['human_names'].")
            human = json.loads(human_row[0])
            expected = {"referees": 38, "managers": 25, "first_names": 1000, "second_names": 1000}
            for key, expected_count in expected.items():
                values = human.get(key) if isinstance(human, dict) else None
                if not isinstance(values, list) or len(values) != expected_count:
                    raise DataBundleError(
                        f"Kho human_names.{key} không hợp lệ; cần {expected_count} mục."
                    )
            career_row = conn.execute("SELECT state_json FROM career_state WHERE id=1").fetchone()
            career = json.loads(career_row[0]) if career_row else {}
            state = career if isinstance(career, dict) else {}
            season = state.get("season")
            matches = state.get("matches", [])
            players = state.get("players", {})
            archive_count = int(conn.execute("SELECT COUNT(*) FROM season_archives").fetchone()[0])
            return {
                "integrity": integrity,
                "schema_version": schema_version,
                "has_career": career_row is not None,
                "current_season": season,
                "current_match_count": len(matches) if isinstance(matches, list) else 0,
                "player_count": len(players) if isinstance(players, dict) else 0,
                "history_count": archive_count,
                "source_player_count": source_count,
                "reference_data_count": int(conn.execute("SELECT COUNT(*) FROM reference_data").fetchone()[0]),
                "file_count": 1,
                "total_size_bytes": path.stat().st_size,
                "paths": [DATABASE_RELATIVE_PATH],
            }
        finally:
            conn.close()
    except DataBundleError:
        raise
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise DataBundleError(f"Database SQLite không hợp lệ: {exc}") from exc


def _allowed_relative_path(raw_path: str) -> PurePosixPath:
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        raise DataBundleError("Đường dẫn file trong gói không hợp lệ.")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DataBundleError(f"Đường dẫn không an toàn trong gói: {raw_path!r}")
    if path.as_posix() != DATABASE_RELATIVE_PATH:
        raise DataBundleError(f"Gói chỉ được chứa {DATABASE_RELATIVE_PATH}.")
    return path


def _decode_entry(entry: dict[str, Any]) -> bytes:
    if not isinstance(entry, dict) or entry.get("path") != DATABASE_RELATIVE_PATH:
        raise DataBundleError("Gói chỉ được chứa một database super_league.db.")
    if entry.get("encoding") != "base64":
        raise DataBundleError("Database trong gói phải được mã hóa base64.")
    content = entry.get("content")
    if not isinstance(content, str):
        raise DataBundleError("Nội dung database trong gói không hợp lệ.")
    try:
        raw = base64.b64decode(content, validate=True)
    except Exception as exc:
        raise DataBundleError(f"Base64 database không hợp lệ: {exc}") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise DataBundleError(f"Database vượt giới hạn {MAX_FILE_BYTES:,} bytes.")
    if int(entry.get("size_bytes", -1)) != len(raw):
        raise DataBundleError("Kích thước database không khớp.")
    if hashlib.sha256(raw).hexdigest() != str(entry.get("sha256", "")):
        raise DataBundleError("Checksum database không khớp; file có thể đã bị sửa/hỏng.")
    return raw


def _inspect_bytes(raw: bytes) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="sl_db_bundle_check_") as temp:
        path = Path(temp) / "super_league.db"
        path.write_bytes(raw)
        return _database_info(path)


def build_bundle(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    database_path = root / DATABASE_RELATIVE_PATH
    info = _database_info(database_path)
    raw = database_path.read_bytes()
    entry = {
        "path": DATABASE_RELATIVE_PATH,
        "encoding": "base64",
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content": base64.b64encode(raw).decode("ascii"),
    }
    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "application": "Super League Simulator",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "metadata": info,
        "files": [entry],
    }


def write_bundle(project_root: str | Path, output_path: str | Path) -> Path:
    bundle = build_bundle(project_root)
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return output


def validate_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT_NAME:
        raise DataBundleError("Đây không phải gói dữ liệu SQLite Super League.")
    version = bundle.get("format_version")
    if version != FORMAT_VERSION:
        raise DataBundleError(
            f"Phiên bản gói {version!r} không được hỗ trợ. Cần phiên bản {FORMAT_VERSION}."
        )
    entries = bundle.get("files")
    if not isinstance(entries, list) or len(entries) != 1:
        raise DataBundleError("Gói phải chứa đúng một file memory/super_league.db.")
    _allowed_relative_path(entries[0].get("path") if isinstance(entries[0], dict) else "")
    raw = _decode_entry(entries[0])
    if len(raw) > MAX_TOTAL_BYTES:
        raise DataBundleError("Gói vượt giới hạn dung lượng tổng.")
    info = _inspect_bytes(raw)
    metadata = bundle.get("metadata") if isinstance(bundle.get("metadata"), dict) else {}
    # Metadata is descriptive only; database bytes and their own checks are authoritative.
    return {**info, "save_version": FORMAT_VERSION, "metadata": metadata}


def _validate_game_load(project_root: str | Path, staged_root: str | Path) -> dict[str, Any]:
    """Validate that a staged project has a complete, readable single database."""
    root = Path(staged_root)
    candidates = (root / DATABASE_RELATIVE_PATH, root / "super_league.db")
    database = next((path for path in candidates if path.is_file()), None)
    if database is None:
        raise DataBundleError("Gói không chứa memory/super_league.db.")
    return _database_info(database)


def preview_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = validate_bundle(bundle)
    season = summary.get("current_season")
    label = f"{season}-{int(season) + 1}" if season is not None else "Chưa khởi tạo career"
    return {
        "season_label": label,
        "match_count": summary["current_match_count"],
        "player_count": summary["player_count"],
        "history_count": summary["history_count"],
        "file_count": 1,
        "total_size_bytes": summary["total_size_bytes"],
        "save_version": summary["save_version"],
        "paths": summary["paths"],
    }


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def apply_bundle(project_root: str | Path, bundle: dict[str, Any]) -> None:
    """Atomically replace the one local database; keep runtime configs untouched."""
    root = Path(project_root).expanduser().resolve()
    validate_bundle(bundle)
    raw = _decode_entry(bundle["files"][0])
    memory = root / "memory"
    memory.mkdir(parents=True, exist_ok=True)

    # Stage on the same filesystem so os.replace is atomic. The old DB remains intact
    # until the fully verified new DB is ready; this staging file is always removed.
    staged = memory / f".super_league.{uuid.uuid4().hex}.tmp"
    try:
        staged.write_bytes(raw)
        _database_info(staged)
        os.replace(staged, memory / "super_league.db")
        # Single-store contract: remove all previous sidecar data and directories.
        for child in memory.iterdir():
            if child.name == "super_league.db":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
    except Exception as exc:
        if isinstance(exc, DataBundleError):
            raise
        raise DataBundleError(f"Không thể áp dụng database bundle: {exc}") from exc
    finally:
        staged.unlink(missing_ok=True)


def export_summary_text(metadata: dict[str, Any]) -> str:
    size_mb = metadata.get("total_size_bytes", 0) / (1024 * 1024)
    season = metadata.get("current_season")
    season_label = f"{season}-{int(season) + 1}" if season is not None else "Chưa có career"
    return (
        f"Career hiện tại: {season_label}\n"
        f"Số trận đang lưu: {metadata.get('current_match_count', 0)}\n"
        f"Số cầu thủ hiện tại: {metadata.get('player_count', 0)}\n"
        f"Số mùa lịch sử: {metadata.get('history_count', 0)}\n"
        "Số file dữ liệu: 1 (super_league.db)\n"
        f"Dung lượng: {size_mb:.2f} MB"
    )
