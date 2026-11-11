# -*- coding: utf-8 -*-
"""Small persisted medical-history service used by the match engine."""
from __future__ import annotations
from datetime import date, timedelta


def _records(state):
    return state.setdefault("medical_records", [])


def effective_status(state: dict, player_id: str, as_of_date: str | None = None) -> dict:
    status = dict((state.get("player_status") or {}).get(str(player_id), {}) or {})
    day = None
    try: day = date.fromisoformat(str(as_of_date)) if as_of_date else None
    except ValueError: pass
    active_days = 0
    injury_name = status.get("injury_name")
    for record in _records(state):
        if str(record.get("player_id")) != str(player_id) or record.get("status") == "recovered":
            continue
        try:
            start = date.fromisoformat(str(record.get("start_date")))
            end = date.fromisoformat(str(record.get("expected_return")))
        except (TypeError, ValueError):
            continue
        if day is None or start <= day < end:
            active_days = max(active_days, max(0, (end - (day or start)).days))
            injury_name = record.get("injury_name") or injury_name
    status["injury_days"] = active_days
    if injury_name: status["injury_name"] = injury_name
    return status


def register_injury(state: dict, player_id: str, injury: dict, match_date: str) -> dict:
    try: start = date.fromisoformat(str(match_date))
    except ValueError: start = date.today()
    matches = max(1, int(injury.get("n", 1) or 1))
    # Injury duration from the match engine is expressed in missed fixtures;
    # map it to a conservative calendar recovery estimate, preserving the source.
    days = max(3, matches * 7)
    row = {"id": f"{player_id}-{start.isoformat()}-{len(_records(state)) + 1}",
           "player_id": str(player_id), "injury_name": str(injury.get("name", "Chấn thương")),
           "start_date": start.isoformat(), "expected_return": (start + timedelta(days=days)).isoformat(),
           "duration_days": days, "missed_matches_estimate": matches, "status": "active"}
    _records(state).append(row)
    return row


def advance_medical_day(state: dict, day_text: str) -> int:
    try: day = date.fromisoformat(str(day_text))
    except ValueError: return 0
    advanced = 0
    for record in _records(state):
        if record.get("status") != "active":
            continue
        try:
            start = date.fromisoformat(str(record.get("start_date")))
            end = date.fromisoformat(str(record.get("expected_return")))
        except (TypeError, ValueError):
            continue
        if start <= day < end:
            stat = state.setdefault("season_stats", {}).setdefault(str(record.get("player_id")), {})
            stat["injury_days"] = int(stat.get("injury_days", 0) or 0) + 1
            advanced += 1
        elif day >= end:
            record["status"] = "recovered"
            record["actual_return"] = day.isoformat()
    return advanced
