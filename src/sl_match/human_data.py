# -*- coding: utf-8 -*-
"""Reference names stored in the single SQLite database.

The runtime does not read or write data_human.json. Human names, managers and
referees are kept in reference_data inside memory/super_league.db.
"""
from __future__ import annotations

import json
import random
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Dict, Tuple


class HumanDataError(RuntimeError):
    """Thiếu hoặc sai cấu trúc kho tên trong SQLite."""


_DATABASE_PATH: Path | None = None


def configure_human_data_database(path: str | Path) -> None:
    global _DATABASE_PATH
    _DATABASE_PATH = Path(path).expanduser().resolve()
    _load_human_data_cached.cache_clear()


def _names(values, key, preserve_frequency=False):
    if not isinstance(values, list):
        raise HumanDataError(f"reference_data['human_names']: '{key}' phải là một mảng.")
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            continue
        name = value.strip()
        normalized = name.casefold()
        if not name:
            continue
        if preserve_frequency or normalized not in seen:
            result.append(name)
        seen.add(normalized)
    if not result:
        raise HumanDataError(f"reference_data['human_names']: '{key}' không có tên hợp lệ.")
    return tuple(result)


@lru_cache(maxsize=8)
def _load_human_data_cached(path_text: str, mtime_ns: int, size: int):
    path = Path(path_text)
    try:
        connection = sqlite3.connect(str(path), timeout=5)
        try:
            row = connection.execute(
                "SELECT data_json FROM reference_data WHERE data_key='human_names'"
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        raise HumanDataError(f"Không đọc được kho tên từ {path}: {exc}") from exc
    if not row:
        raise HumanDataError(
            f"Database {path} thiếu reference_data['human_names']; "
            "hãy dùng database Super League đã được khởi tạo đầy đủ."
        )
    try:
        raw = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise HumanDataError("Dữ liệu human_names trong SQLite không phải JSON hợp lệ.") from exc
    if not isinstance(raw, dict):
        raise HumanDataError("Dữ liệu human_names trong SQLite phải là object.")
    return {
        "referees": _names(raw.get("referees"), "referees"),
        "managers": _names(raw.get("managers"), "managers"),
        # Duplicates in first/last name pools intentionally remain as random weights.
        "first_names": _names(raw.get("first_names"), "first_names", preserve_frequency=True),
        "second_names": _names(raw.get("second_names"), "second_names", preserve_frequency=True),
    }


def human_data_path() -> Path:
    """Return the active single database path (kept for diagnostics compatibility)."""
    if _DATABASE_PATH is not None and _DATABASE_PATH.is_file():
        return _DATABASE_PATH
    project_root = Path(__file__).resolve().parents[2]
    return project_root / "memory" / "super_league.db"


def load_human_data(database_path: str | Path | None = None) -> Dict[str, Tuple[str, ...]]:
    path = Path(database_path).expanduser().resolve() if database_path else human_data_path()
    try:
        stat = path.stat()
    except OSError as exc:
        raise HumanDataError(f"Không truy cập được database {path}: {exc}") from exc
    return _load_human_data_cached(str(path), stat.st_mtime_ns, stat.st_size)


def assign_manager_names(teams, seed=2026):
    """Gán tên HLV không trùng nhau cho các CLB, ổn định theo seed và tên CLB."""
    data = load_human_data()
    manager_names = list(data["managers"])
    rng = random.Random(int(seed) ^ 0x4D414E47)
    rng.shuffle(manager_names)
    ordered_teams = sorted({str(team) for team in teams if str(team).strip()})
    return {team: manager_names[index % len(manager_names)] for index, team in enumerate(ordered_teams)}
