# -*- coding: utf-8 -*-
"""Portable season archive interchange helpers.

The application persists active career and completed seasons inside SQLite.
These helpers are used only when a user explicitly imports or exports portable
season JSON files to a folder outside the project's memory directory.
Archived match logs keep result, scorers, red cards, AET and shootout score.
"""
from __future__ import annotations

import copy
import hashlib
from collections import Counter
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ARCHIVE_FORMAT = "SUPER_LEAGUE_SEASON_ARCHIVE"
ARCHIVE_VERSION = 2
SEASON_FILE_RE = re.compile(r"^(\d{4})-(\d{4})\.json$", re.IGNORECASE)
_RED_CODES = {"r", "yr", "red", "second_yellow", "second-yellow", "2y", "secondyellow"}


class SeasonArchiveError(ValueError):
    """Invalid season archive or unsafe import directory."""


def season_filename(record: dict) -> str:
    try:
        season = int(record.get("season"))
    except (TypeError, ValueError) as exc:
        raise SeasonArchiveError("Bản ghi mùa thiếu season hợp lệ.") from exc
    return f"{season}-{season + 1}.json"


def _player_names(player_state: dict | None) -> dict[str, str]:
    names: dict[str, str] = {}
    state = player_state if isinstance(player_state, dict) else {}
    for pid, row in (state.get("players") or {}).items():
        if isinstance(row, dict):
            names[str(pid)] = str(row.get("common_name") or row.get("player_name") or pid)
    for row in state.get("generated_players") or []:
        if isinstance(row, dict):
            pid = str(row.get("player_id") or row.get("id") or "")
            if pid:
                names.setdefault(pid, str(row.get("common_name") or row.get("player_name") or pid))
    for row in state.get("retired_players") or []:
        if isinstance(row, dict):
            pid = str(row.get("player_id") or "")
            if pid:
                names.setdefault(pid, str(row.get("player_name") or row.get("common_name") or pid))
    return names


def compact_match(match: dict, player_names: dict[str, str] | None = None) -> dict:
    """Return the deliberately small historical representation of one match."""
    names = player_names or {}
    # Only lightweight match identity plus the events explicitly requested for
    # season history. Replays, lineups, player match stats, yellow cards, VAR,
    # shot logs, lineups and duplicated detailed match state are intentionally
    # excluded. A small derived winner marker remains for bracket consumers.
    result = {
        "id": match.get("id"),
        "season": match.get("season"),
        "comp": match.get("comp", ""),
        "round": match.get("round", ""),
        "stage": match.get("stage", ""),
        "date": match.get("date", ""),
        "home": match.get("home", ""),
        "away": match.get("away", ""),
        "neutral": bool(match.get("neutral", False)),
        "played": bool(match.get("played", True)),
        "hg": match.get("hg"),
        "ag": match.get("ag"),
        # Keep the derived winner marker for existing bracket/table consumers.
        # It is a tiny summary field, not replay payload.
        "winner": match.get("winner"),
        "goals": [],
        "red_cards": [],
    }
    # Lightweight stadium facts are useful in the club's historical reports and
    # do not contain replay/player tracking payloads.
    if isinstance(match.get("matchday"), dict):
        result["matchday"] = copy.deepcopy(match["matchday"])
        result["attendance"] = match["matchday"].get("attendance")
        result["fill_rate"] = match["matchday"].get("fill_rate")
        result["match_importance"] = match["matchday"].get("match_importance")
    for event in match.get("goals", []) or []:
        if not isinstance(event, dict):
            continue
        scorer_id = str(event.get("scorer_id") or event.get("player_id") or "")
        scorer = event.get("scorer") or event.get("player") or names.get(scorer_id) or scorer_id or "Không rõ"
        goal = {
            "minute": event.get("minute", event.get("m")),
            "team": event.get("team", ""),
            "scorer_id": scorer_id or None,
            "scorer": scorer,
        }
        if event.get("pen"):
            goal["pen"] = True
        result["goals"].append(goal)

    # Existing compact records can be compacted again without losing red cards.
    existing_reds = match.get("red_cards")
    if isinstance(existing_reds, list) and existing_reds:
        for card in existing_reds:
            if not isinstance(card, dict):
                continue
            result["red_cards"].append({
                "minute": card.get("minute", card.get("m")),
                "player_id": card.get("player_id", card.get("p")),
                "player": card.get("player") or card.get("player_name") or names.get(str(card.get("player_id", card.get("p", "")))) or "Không rõ",
                "team": card.get("team", ""),
                "side": card.get("side", ""),
                "type": card.get("type") or card.get("c") or "red",
            })
    else:
        for side, team_key, cards_key in (("h", "home", "cards_h"), ("a", "away", "cards_a")):
            team = str(match.get(team_key, "") or "")
            for card in match.get(cards_key, []) or []:
                if not isinstance(card, dict):
                    continue
                code = str(card.get("c", "")).strip().lower()
                if code not in _RED_CODES:
                    continue
                pid = str(card.get("p") or card.get("player_id") or "")
                result["red_cards"].append({
                    "minute": card.get("m", card.get("minute")),
                    "player_id": pid or None,
                    "player": card.get("player") or names.get(pid) or pid or "Không rõ",
                    "team": team,
                    "side": side,
                    "type": "second_yellow" if code in {"yr", "second_yellow", "second-yellow", "2y", "secondyellow"} else "red",
                })
    result["red_cards"].sort(key=lambda row: (int(row.get("minute") or 0), str(row.get("team", ""))))

    aet = bool(match.get("aet", match.get("et", False)))
    if aet:
        result["aet"] = True
    pens = match.get("pens")
    if isinstance(pens, dict):
        # Keep only the shoot-out score, not individual kick events.
        if "home" in pens and "away" in pens:
            result["pens"] = {"home": pens.get("home"), "away": pens.get("away")}
    elif isinstance(pens, (list, tuple)) and len(pens) >= 2:
        result["pens"] = {"home": pens[0], "away": pens[1]}
    return result


def compact_season_record(record: dict, player_state: dict | None = None,
                          award_eligibility: dict | None = None) -> dict:
    """Copy a finished season record and replace verbose match replays with summaries."""
    if not isinstance(record, dict):
        raise SeasonArchiveError("Bản ghi mùa không phải JSON object.")
    compact = copy.deepcopy(record)
    names = _player_names(player_state)
    raw_matches = compact.get("matches") or []
    motm_counts = Counter()
    for match in raw_matches:
        if isinstance(match, dict):
            motm = match.get("motm")
            if isinstance(motm, (list, tuple)) and len(motm) >= 2 and motm[1]:
                motm_counts[str(motm[1])] += 1
    compact["matches"] = [compact_match(match, names) for match in raw_matches if isinstance(match, dict)]
    compact["match_count"] = int(record.get("match_count", len(raw_matches)) or len(raw_matches))
    # Keep season aggregates required by award recalculation after detailed match
    # player_stats/MOTM records are removed from each archived fixture.
    player_meta = (player_state or {}).get("player_meta", {}) if isinstance(player_state, dict) else {}
    for row in compact.get("career_player_stats", []) or []:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("player_id") or "")
        row.setdefault("motm_count", int(motm_counts.get(pid, 0)))
        meta = player_meta.get(pid, {}) if isinstance(player_meta, dict) else {}
        if row.get("age") is None and isinstance(meta, dict) and meta.get("age") is not None:
            row["age"] = meta.get("age")
        if row.get("potential") is None and isinstance(meta, dict) and meta.get("potential") is not None:
            row["potential"] = meta.get("potential")
    if award_eligibility is not None:
        compact["award_eligibility"] = {
            "premier_champion_players": sorted(str(x) for x in award_eligibility.get("premier_champion_players", set()) or []),
            "c1_semifinal_players": sorted(str(x) for x in award_eligibility.get("c1_semifinal_players", set()) or []),
        }
    else:
        existing = compact.get("award_eligibility")
        if isinstance(existing, dict):
            compact["award_eligibility"] = {
                "premier_champion_players": sorted(str(x) for x in existing.get("premier_champion_players", []) or []),
                "c1_semifinal_players": sorted(str(x) for x in existing.get("c1_semifinal_players", []) or []),
            }
    return compact


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def build_archive_payload(record: dict) -> dict:
    compact = compact_season_record(record)
    raw_record = _canonical_json_bytes(compact)
    season = int(compact["season"])
    return {
        "format": ARCHIVE_FORMAT,
        "format_version": ARCHIVE_VERSION,
        "season": season,
        "label": compact.get("label") or f"{season}/{season + 1}",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "record_sha256": hashlib.sha256(raw_record).hexdigest(),
        "record": compact,
    }


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def write_season_archive(season_dir: str | Path, record: dict) -> Path:
    folder = Path(season_dir).expanduser().resolve()
    compact = compact_season_record(record)
    payload = build_archive_payload(compact)
    path = folder / season_filename(compact)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write(path, raw)
    return path


def read_season_archive(path: str | Path) -> dict:
    archive_path = Path(path)
    try:
        payload = json.loads(archive_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SeasonArchiveError(f"Không đọc được {archive_path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SeasonArchiveError(f"{archive_path.name}: nội dung phải là JSON object.")
    if payload.get("format") == ARCHIVE_FORMAT:
        version = int(payload.get("format_version", 0))
        if version not in (1, ARCHIVE_VERSION):
            raise SeasonArchiveError(f"{archive_path.name}: phiên bản archive không được hỗ trợ.")
        record = payload.get("record")
        if not isinstance(record, dict):
            raise SeasonArchiveError(f"{archive_path.name}: thiếu record.")
        expected = str(payload.get("record_sha256") or "")
        actual = hashlib.sha256(_canonical_json_bytes(record)).hexdigest()
        if expected and expected != actual:
            raise SeasonArchiveError(f"{archive_path.name}: checksum record không khớp.")
    else:
        # Backward compatibility for plain season-record JSON files.
        record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
        if not isinstance(record, dict) or "season" not in record:
            raise SeasonArchiveError(f"{archive_path.name}: không phải season archive hợp lệ.")
        record = compact_season_record(record)
    filename_match = SEASON_FILE_RE.match(archive_path.name)
    if filename_match:
        expected_season = int(filename_match.group(1))
        if int(record.get("season", -1)) != expected_season:
            raise SeasonArchiveError(f"{archive_path.name}: năm trong tên file không khớp record.")
    if not isinstance(record.get("matches", []), list):
        raise SeasonArchiveError(f"{archive_path.name}: matches phải là danh sách.")
    return compact_season_record(record)


def load_season_archives(season_dir: str | Path, strict: bool = False) -> tuple[list[dict], list[str]]:
    folder = Path(season_dir)
    if not folder.is_dir():
        return [], []
    records: list[dict] = []
    errors: list[str] = []
    for path in sorted(folder.glob("*.json")):
        if not SEASON_FILE_RE.match(path.name):
            continue
        try:
            records.append(read_season_archive(path))
        except SeasonArchiveError as exc:
            errors.append(str(exc))
            if strict:
                raise
    by_season: dict[int, dict] = {}
    for record in records:
        season = int(record["season"])
        if season in by_season:
            raise SeasonArchiveError(f"Có nhiều file cho mùa {season}/{season + 1}.")
        by_season[season] = record
    return [by_season[key] for key in sorted(by_season)], errors


def season_folder_summary(season_dir: str | Path) -> dict:
    folder = Path(season_dir).expanduser().resolve()
    records, errors = load_season_archives(folder, strict=False)
    file_names = [season_filename(row) for row in records]
    total_bytes = sum((folder / name).stat().st_size for name in file_names if (folder / name).is_file())
    return {
        "folder": str(folder), "season_count": len(records),
        "seasons": [{"season": int(row["season"]), "label": row.get("label", f"{row['season']}/{int(row['season'])+1}"),
                     "match_count": int(row.get("match_count", len(row.get("matches") or []))),
                     "player_count": len(row.get("career_player_stats") or []),
                     "file": season_filename(row)} for row in records],
        "total_bytes": total_bytes, "errors": errors,
    }


def export_season_folder(source_dir: str | Path, output_dir: str | Path) -> dict:
    """Mirror season JSON files from source to output; remove stale season files only."""
    source = Path(source_dir).expanduser().resolve()
    target = Path(output_dir).expanduser().resolve()
    records, errors = load_season_archives(source, strict=True)
    if not records:
        raise SeasonArchiveError(f"Không tìm thấy file mùa hợp lệ trong {source}.")
    target.mkdir(parents=True, exist_ok=True)
    if source != target:
        names = {season_filename(row) for row in records}
        stale = [p for p in target.glob("*.json") if SEASON_FILE_RE.match(p.name) and p.name not in names]
        for path in stale:
            path.unlink(missing_ok=True)
        for record in records:
            source_file = source / season_filename(record)
            target_file = target / source_file.name
            raw = source_file.read_bytes()
            # Validate again immediately before copying.
            read_season_archive(source_file)
            _atomic_write(target_file, raw)
    return season_folder_summary(target)


def preview_season_folder(input_dir: str | Path) -> dict:
    summary = season_folder_summary(input_dir)
    if summary["errors"]:
        raise SeasonArchiveError("Có file mùa không hợp lệ:\n" + "\n".join(summary["errors"]))
    if not summary["season_count"]:
        raise SeasonArchiveError("Thư mục không có file mùa dạng YYYY-YYYY.json.")
    return summary
