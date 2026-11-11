#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Super League - cong cu tinh lai danh hieu cac mua cu.

Dat file trong `src/`; project root nam o thu muc cha (chua config.json va memory/).
Chay `python src/recalculate_awards.py` de mo GUI.

Tool chi tinh lai season_awards trong memory/super_league.db; khong mo phong lai tran,
khong sua ty so, lich, cau thu, RNG state hay config.json. Ghi ket qua truc tiep,
khong giu backup sau thao tac.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sl_database import SuperLeagueDatabase

TOOL_VERSION = "1.7"
DEFAULTS = {
    "min_appearances": 21,
    "require_premier_champion": True,
    "require_c1_semifinal": True,
    "c1_champion_bonus": 5.0,
    "premier_champion_bonus": 4.0,
    "c1_runner_up_bonus": 4.0,
    "premier_runner_up_bonus": 3.0,
    "major_cup_champion_bonus": 3.0,
    "weight_rating": 42.0,
    "weight_contribution": 28.0,
    "weight_motm": 20.0,
    "weight_minutes": 10.0,
}


def validate_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    out.update(settings or {})
    out["min_appearances"] = int(out["min_appearances"])
    if out["min_appearances"] < 1 or out["min_appearances"] > 80:
        raise ValueError("Số trận tối thiểu phải nằm trong khoảng 1–80.")
    for key in ("require_premier_champion", "require_c1_semifinal"):
        out[key] = bool(out[key])
    if not out["require_premier_champion"] and not out["require_c1_semifinal"]:
        # Cho phép bỏ cả hai điều kiện giải; khi đó chỉ áp dụng ngưỡng số trận.
        pass
    for key in ("c1_champion_bonus", "premier_champion_bonus", "c1_runner_up_bonus",
                "premier_runner_up_bonus", "major_cup_champion_bonus",
                "weight_rating", "weight_contribution", "weight_motm", "weight_minutes"):
        out[key] = float(out[key])
        if out[key] < 0 or out[key] > 100:
            raise ValueError(f"{key} phải nằm trong khoảng 0–100.")
    weights = sum(out[k] for k in ("weight_rating", "weight_contribution", "weight_motm", "weight_minutes"))
    if abs(weights - 100.0) > 0.01:
        raise ValueError(f"Tổng trọng số bình chọn phải bằng 100% (hiện tại {weights:.2f}%).")
    return out


def shared_settings_path(project_root: Path) -> Path:
    """Shared configuration consumed by both this tool and the main game."""
    return Path(project_root).expanduser().resolve() / "award_settings.json"


def load_shared_settings(project_root: Path) -> Tuple[dict, bool]:
    """Load settings saved for the main app; missing files simply use defaults."""
    path = shared_settings_path(project_root)
    if not path.is_file():
        return dict(DEFAULTS), False
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("award_settings.json không phải JSON object.")
    values = payload.get("settings", payload)
    return validate_settings(values), True


def save_shared_settings(project_root: Path, settings: dict) -> Tuple[Path, Optional[Path]]:
    """Atomically persist validated settings for the next main-app launch."""
    root = Path(project_root).expanduser().resolve()
    # Accept both the organized layout (src/sl_game.py) and legacy flat
    # project roots (sl_game.py). The config is shared at the project root.
    if not ((root / "src" / "sl_game.py").is_file() or (root / "sl_game.py").is_file()):
        raise FileNotFoundError(f"Không tìm thấy sl_game.py trong {root} hoặc {root / 'src'}")
    root.mkdir(parents=True, exist_ok=True)
    path = shared_settings_path(root)
    clean = validate_settings(settings)
    backup = None  # Clean-start edition does not retain previous settings files.
    payload = {
        "schema_version": 1,
        "saved_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "settings": clean,
        "note": "Được lưu từ Award Recalculator; app chính sẽ nạp khi khởi động.",
    }
    fd, temp_name = tempfile.mkstemp(prefix=".award_settings_", suffix=".tmp", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return path, backup


def project_paths(project_root: Path, save_override: Optional[Path] = None) -> Tuple[Path, Path, Path]:
    root = Path(project_root).expanduser().resolve()
    game_file = root / "src" / "sl_game.py"
    if not game_file.is_file():
        raise FileNotFoundError(f"Không tìm thấy {game_file}. Hãy chọn đúng thư mục gốc Super League.")
    database_path = Path(save_override).expanduser().resolve() if save_override else root / "memory" / "super_league.db"
    if not database_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy database sự nghiệp: {database_path}")
    return root, game_file, database_path


def load_blob(database_path: Path) -> Tuple[dict, tuple]:
    """Load career and completed seasons from the single SQLite data file."""
    stat = database_path.stat()
    database = SuperLeagueDatabase(database_path)
    blob = database.load_career()
    if not isinstance(blob, dict) or not isinstance(blob.get("state"), dict):
        raise ValueError("Database chưa có career_state hợp lệ.")
    state = blob["state"]
    archived = database.load_season_archives()
    state["history"] = archived
    state["history_archive_mode"] = "database"
    if not isinstance(state.get("history", []), list):
        raise ValueError("career_state.history không phải danh sách.")
    return blob, (stat.st_mtime_ns, stat.st_size)


def load_award_helper(project_root: Path, state: dict):
    """Load Game helper methods without constructing a game or triggering migrations."""
    root = Path(project_root).resolve()
    source_dir = root / "src"
    source_text = str(source_dir)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    game_file = source_dir / "sl_game.py"
    module_name = "_super_league_awards_tool_sl_game"
    spec = importlib.util.spec_from_file_location(module_name, game_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"Không thể nạp module {game_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    helper = object.__new__(module.Game)
    helper.s = state
    return helper


def _score_candidates(candidates: List[dict], settings: dict) -> List[dict]:
    """Reproduce the app's normalized ballot formula with editable weights."""
    rows = [dict(row) for row in candidates if int(row.get("apps", 0) or 0) > 0]
    impact_values = [int(r.get("goals", 0) or 0) + .7 * int(r.get("assists", 0) or 0) for r in rows]
    max_impact = max(impact_values, default=0.0)
    max_motm = max((int(r.get("motm_count", 0) or 0) for r in rows), default=0)
    max_minutes = max((int(r.get("minutes", 0) or 0) for r in rows), default=0)
    wr = settings["weight_rating"] / 100.0
    wc = settings["weight_contribution"] / 100.0
    wm = settings["weight_motm"] / 100.0
    wmin = settings["weight_minutes"] / 100.0
    for row in rows:
        rating = row.get("avg_rating")
        try:
            rating_score = max(0.0, min(100.0, ((float(rating) - 5.5) / 4.5) * 100.0)) if rating is not None else 55.0
        except (TypeError, ValueError):
            rating_score = 55.0
        try:
            row["avg_rating"] = float(rating) if rating is not None else None
        except (TypeError, ValueError):
            row["avg_rating"] = None
        impact = int(row.get("goals", 0) or 0) + .7 * int(row.get("assists", 0) or 0)
        contribution_score = min(100.0, impact / max_impact * 100.0) if max_impact else 0.0
        motm_score = (int(row.get("motm_count", 0) or 0) / max_motm * 100.0) if max_motm else 0.0
        minutes_score = (int(row.get("minutes", 0) or 0) / max_minutes * 100.0) if max_minutes else 0.0
        score = wr * rating_score + wc * contribution_score + wm * motm_score + wmin * minutes_score
        row["vote_score"] = round(score, 2)
        row["score_components"] = {
            "rating": round(rating_score, 1), "contribution": round(contribution_score, 1),
            "motm": round(motm_score, 1), "minutes": round(minutes_score, 1),
        }
    rows.sort(key=lambda r: (-r.get("vote_score", 0), -(r.get("avg_rating") or 0),
                             -(int(r.get("goals", 0) or 0) + int(r.get("assists", 0) or 0)),
                             -int(r.get("motm_count", 0) or 0), -int(r.get("minutes", 0) or 0),
                             str(r.get("player", "")).casefold()))
    return rows


def _make_eligibility_rule(settings: dict) -> str:
    paths = []
    if settings["require_premier_champion"]:
        paths.append("đã ra sân ít nhất một trận ở bảng Champion Premier League")
    if settings["require_c1_semifinal"]:
        paths.append("đã ra sân C1 cùng CLB vào bán kết trở lên")
    if not paths:
        path_text = "không yêu cầu điều kiện giải đấu"
    elif len(paths) == 2:
        path_text = paths[0] + " HOẶC " + paths[1]
    else:
        path_text = paths[0]
    return f"Ít nhất {settings['min_appearances']} trận; {path_text}."


def _award_player_row(awards: dict, key: str, podium_index: Optional[int] = None) -> Optional[dict]:
    """Read an individual award winner, with compatibility for older archives."""
    row = (awards or {}).get(key)
    if isinstance(row, dict):
        return row
    if podium_index is not None:
        podium = (awards or {}).get("ballon_podium") or []
        if len(podium) > podium_index and isinstance(podium[podium_index], dict):
            return podium[podium_index]
    return None


def award_display_rows(awards: dict) -> List[dict]:
    """Return the four individual season awards for GUI/CLI preview.

    Golden Boy is read from the saved young_player field (or legacy golden_boy).
    The recalculator deliberately does not invent a new youth winner when old
    archives do not contain enough potential/age data to reproduce that ballot.
    """
    awards = awards or {}
    young_winner = awards.get("young_player")
    if not isinstance(young_winner, dict):
        young_winner = awards.get("golden_boy")
    if not isinstance(young_winner, dict):
        young_winner = None
    definitions = [
        ("Quả bóng Vàng", _award_player_row(awards, "ballon_dor", 0), "vote_score"),
        ("Quả bóng Bạc", _award_player_row(awards, "ballon_silver", 1), "vote_score"),
        ("Quả bóng Đồng", _award_player_row(awards, "ballon_bronze", 2), "vote_score"),
        ("Golden Boy", young_winner, "young_vote_score"),
    ]
    result = []
    for title, row, score_key in definitions:
        row = row if isinstance(row, dict) else None
        if row is None:
            result.append({"award": title, "player": "Chưa có dữ liệu", "team": "—",
                           "apps": "—", "score": "—", "age": "—", "available": False})
            continue
        player = str(row.get("player") or row.get("player_name") or row.get("name") or "Không rõ tên")
        team = str(row.get("team") or row.get("club") or "—")
        apps = row.get("apps", "—")
        age = row.get("age", "—")
        score = row.get(score_key)
        if score is None and score_key == "young_vote_score":
            score = row.get("vote_score")
        try:
            score_text = f"{float(score):.2f}" if score is not None else "—"
        except (TypeError, ValueError):
            score_text = "—"
        try:
            apps = int(apps)
        except (TypeError, ValueError):
            apps = "—"
        try:
            age = f"{float(age):.1f}" if age is not None else "—"
        except (TypeError, ValueError):
            age = "—"
        result.append({"award": title, "player": player, "team": team,
                       "apps": apps, "score": score_text, "age": age, "available": True})
    return result


def _legacy_premier_runner_up(record: dict) -> Optional[str]:
    """Rebuild the Champion-group runner-up when an old archive lacks award snapshots."""
    phase2_root = record.get("league_phase2") if isinstance(record.get("league_phase2"), dict) else {}
    premier = phase2_root.get("premier") if isinstance(phase2_root.get("premier"), dict) else {}
    phase1_rows = premier.get("phase1_table") if isinstance(premier.get("phase1_table"), list) else []
    teams = list(premier.get("champion_teams") or [])
    if not teams and phase1_rows:
        teams = [str(row.get("team")) for row in phase1_rows[:4] if row.get("team")]
    if not teams:
        league_rows = (record.get("leagues", {}) or {}).get("premier", []) or []
        teams = [str(row.get("team")) for row in league_rows[:4] if row.get("team")]
    if len(teams) < 2:
        return None

    phase1_by_team = {str(row.get("team")): row for row in phase1_rows if row.get("team")}
    stats = {}
    for team in teams:
        base = phase1_by_team.get(str(team), {})
        stats[str(team)] = {
            "pts": int(base.get("pts", 0) or 0),
            "gf": int(base.get("gf", 0) or 0),
            "ga": int(base.get("ga", 0) or 0),
            "h2h": int(base.get("h2h", 0) or 0),
        }
    matches = record.get("matches") or []
    phase2_matches = [m for m in matches if isinstance(m, dict) and m.get("played", True)
                      and m.get("comp") == "Premier League"
                      and str(m.get("stage", "")).startswith("league_p2_premier_champion_")]
    if len(phase2_matches) < 12:
        return None
    for match in phase2_matches:
        if not isinstance(match, dict) or not match.get("played", True):
            continue
        if match.get("comp") != "Premier League" or not str(match.get("stage", "")).startswith("league_p2_premier_champion_"):
            continue
        home, away = str(match.get("home", "")), str(match.get("away", ""))
        if home not in stats or away not in stats:
            continue
        try:
            hg, ag = int(match.get("hg", 0)), int(match.get("ag", 0))
        except (TypeError, ValueError):
            continue
        stats[home]["gf"] += hg; stats[home]["ga"] += ag
        stats[away]["gf"] += ag; stats[away]["ga"] += hg
        if hg > ag:
            stats[home]["pts"] += 3; stats[home]["h2h"] += 3
        elif hg < ag:
            stats[away]["pts"] += 3; stats[away]["h2h"] += 3
        else:
            stats[home]["pts"] += 1; stats[away]["pts"] += 1
            stats[home]["h2h"] += 1; stats[away]["h2h"] += 1
    ordered = sorted(stats, key=lambda team: (
        -stats[team]["pts"],
        -(stats[team]["gf"] - stats[team]["ga"]),
        -stats[team]["gf"], -stats[team]["h2h"], team.casefold()))
    return ordered[1] if len(ordered) > 1 else None


def _season_honor_teams(helper, record: dict) -> dict:
    """Resolve champion and runner-up clubs from award snapshots and legacy fields."""
    teams = dict(helper._season_champion_teams(record) or {})
    awards = record.get("competition_awards") if isinstance(record.get("competition_awards"), dict) else {}
    premier = awards.get("premier") if isinstance(awards.get("premier"), dict) else {}
    c1 = awards.get("c1") if isinstance(awards.get("c1"), dict) else {}
    cup = awards.get("cup") if isinstance(awards.get("cup"), dict) else {}
    c1_record = record.get("c1") if isinstance(record.get("c1"), dict) else {}
    cup_record = record.get("cup") if isinstance(record.get("cup"), dict) else {}

    teams.update({
        "premier_runner_up": (_legacy_premier_runner_up(record) or premier.get("runner_up")
                              or record.get("runner_up_premier")
                              or ((record.get("league_phase2") or {}).get("premier", {}) or {}).get("runner_up")),
        "c1_runner_up": (c1.get("runner_up") or record.get("runner_up_c1") or c1_record.get("runner_up")),
        "major_cup": (cup.get("champion") or record.get("champion_cup")
                      or cup_record.get("winner") or cup_record.get("champion")),
    })
    # Normalize empty values so the selector only compares actual club names.
    for key in ("premier_runner_up", "c1_runner_up", "major_cup"):
        teams[key] = str(teams[key]) if teams.get(key) else None
    return teams


def _team_best_xi_bonus(team: Optional[str], teams: dict, settings: dict) -> Tuple[float, List[str]]:
    """Additive Best XI-only preferences for champions and runner-ups."""
    if not team:
        return 0.0, []
    bonus = 0.0
    honors = []
    rules = (
        ("c1", "c1_champion_bonus", "Vô địch Champions League"),
        ("premier", "premier_champion_bonus", "Vô địch Premier League"),
        ("c1_runner_up", "c1_runner_up_bonus", "Á quân Champions League"),
        ("premier_runner_up", "premier_runner_up_bonus", "Á quân Premier League"),
        ("major_cup", "major_cup_champion_bonus", "Vô địch Major Cup"),
    )
    for team_key, setting_key, label in rules:
        value = float(settings.get(setting_key, 0.0) or 0.0)
        if teams.get(team_key) and str(team) == str(teams[team_key]) and value:
            bonus += value
            honors.append(f"{label} (+{value:g})")
    return round(bonus, 2), honors


def build_top20_players(eligible_rows: Sequence[dict]) -> List[dict]:
    """Return the top 20 from the same eligible ballot as the Ballon d'Or podium.

    Ranks 1–3 explicitly carry the Gold/Silver/Bronze Ballon labels so the
    auxiliary app can show one consistent ordered list.
    """
    awards = {1: "Quả bóng Vàng", 2: "Quả bóng Bạc", 3: "Quả bóng Đồng"}
    result = []
    for rank, source in enumerate(list(eligible_rows)[:20], 1):
        row = dict(source)
        row["rank"] = rank
        row["ballon_award"] = awards.get(rank, "")
        result.append(row)
    return result


def recalculate_one_season(helper, record: dict, settings: dict) -> Tuple[Optional[dict], str]:
    """Return a new season_awards object without mutating record or save."""
    matches = [m for m in (record.get("matches") or []) if isinstance(m, dict) and m.get("played", True)]
    if not matches:
        return None, "Bỏ qua: mùa không có match log đã đá."
    lookup = helper._award_player_lookup()
    archived_rows = record.get("career_player_stats") or helper._award_rows_from_matches(matches, lookup)
    if not archived_rows:
        return None, "Bỏ qua: không có thống kê cầu thủ hoặc dữ liệu trận đủ để dựng lại."

    old_season = helper.s.get("season")
    old_award_settings = getattr(helper, "award_settings", None)
    helper.award_settings = dict(settings)
    settings_signature = None
    try:
        try:
            helper.s["season"] = int(record.get("season", old_season or 2026))
        except (TypeError, ValueError):
            helper.s["season"] = int(old_season or 2026)
        candidates = helper._season_award_candidates(
            matches=matches, archived_rows=archived_rows,
            eligibility_record=record.get("award_eligibility"))
        settings_signature = helper._award_settings_signature() if hasattr(helper, "_award_settings_signature") else None
    finally:
        if old_season is None:
            helper.s.pop("season", None)
        else:
            helper.s["season"] = old_season
        if old_award_settings is None:
            try:
                delattr(helper, "award_settings")
            except AttributeError:
                pass
        else:
            helper.award_settings = old_award_settings

    if not candidates:
        return None, "Bỏ qua: không tìm thấy cầu thủ có thống kê mùa."
    candidates = _score_candidates(candidates, settings)
    paths = helper._season_award_eligibility(matches, archived_eligibility=record.get("award_eligibility"))
    premier_ids = paths.get("premier_champion_players", set())
    c1_ids = paths.get("c1_semifinal_players", set())
    for row in candidates:
        pid = str(row.get("player_id", ""))
        apps = int(row.get("apps", 0) or 0)
        reasons = []
        if pid in premier_ids:
            reasons.append("Premier Champion")
        if pid in c1_ids:
            reasons.append("Champions League bán kết")
        required_paths = []
        if settings["require_premier_champion"]:
            required_paths.append(pid in premier_ids)
        if settings["require_c1_semifinal"]:
            required_paths.append(pid in c1_ids)
        competition_ok = (any(required_paths) if required_paths else True)
        row["award_eligible"] = bool(apps >= settings["min_appearances"] and competition_ok)
        row["eligibility_reasons"] = reasons
        if apps < settings["min_appearances"]:
            row["eligibility_note"] = f"Chưa đủ {settings['min_appearances']} trận"
        elif not competition_ok:
            row["eligibility_note"] = "Không đạt điều kiện Premier Champion hoặc C1 bán kết đã chọn"
        else:
            row["eligibility_note"] = "Đủ điều kiện: " + ("; ".join(reasons) if reasons else "đạt ngưỡng số trận")

    eligible = [dict(row) for row in candidates if row.get("award_eligible")]
    podium = eligible[:3]
    champion_teams = _season_honor_teams(helper, record)

    # Reuse the simulator's positional 4-4-2 selector, applying the editable club-honor
    # bonuses consistently with the main simulator's default rule.
    original_bonus = getattr(helper, "_best_xi_honor_bonus", None)
    def custom_bonus(team, honors_by_competition, _settings_override=None):
        return _team_best_xi_bonus(team, honors_by_competition, settings)
    helper._best_xi_honor_bonus = custom_bonus
    try:
        best_xi = helper._make_best_xi_442(candidates, champion_teams=champion_teams)
    finally:
        if original_bonus is None:
            try:
                delattr(helper, "_best_xi_honor_bonus")
            except AttributeError:
                pass
        else:
            helper._best_xi_honor_bonus = original_bonus

    old_awards = record.get("season_awards") if isinstance(record.get("season_awards"), dict) else {}
    new_awards = copy.deepcopy(old_awards)
    rule_text = _make_eligibility_rule(settings)
    weights_text = (f"{settings['weight_rating']:g}% rating, {settings['weight_contribution']:g}% bàn thắng/kiến tạo, "
                    f"{settings['weight_motm']:g}% MOTM, {settings['weight_minutes']:g}% phút thi đấu.")
    best_xi_text = (f"Điều kiện: {rule_text} Trong nhóm đủ điều kiện, điểm cá nhân theo trọng số {weights_text}. "
                    f"Ưu tiên CLB: vô địch C1 +{settings['c1_champion_bonus']:g}, "
                    f"vô địch Premier +{settings['premier_champion_bonus']:g}, "
                    f"á quân C1 +{settings['c1_runner_up_bonus']:g}, "
                    f"á quân Premier +{settings['premier_runner_up_bonus']:g}, "
                    f"vô địch Major Cup +{settings['major_cup_champion_bonus']:g} "
                    "(Major Cup bằng á quân Premier theo mặc định); có thể cộng dồn. "
                    "Chỉ áp dụng cho đội hình 4-4-2, không ảnh hưởng Quả bóng Vàng/Bạc/Đồng. "
                    "Đội hình giữ đúng 1 GK–4 DEF–4 MID–2 FWD; ưu tiên Top 20, nếu thiếu vị trí thì lấy cầu thủ cùng vị trí từ CLB vô địch C1 ngoài Top 20, vẫn phải đủ điều kiện và trên 20 trận; nếu không có thì để trống.")
    method_text = (f"Điều kiện Quả bóng Vàng/Bạc/Đồng: {rule_text} "
                   f"Cách tính: {weights_text}")
    new_awards.update({
        "ballon_dor": dict(podium[0]) if len(podium) > 0 else None,
        "ballon_silver": dict(podium[1]) if len(podium) > 1 else None,
        "ballon_bronze": dict(podium[2]) if len(podium) > 2 else None,
        "ballon_podium": [dict(row) for row in podium],
        "top20_players": build_top20_players(eligible),
        "top20_version": 1,
        "best_xi_442": best_xi,
        "best_xi_version": 8,
        "award_settings_signature": settings_signature,
        "recalculation_settings": dict(settings),
        "best_xi_fallback_rule": ("Ưu tiên cầu thủ trong Top 20; nếu thiếu vị trí, lấy cầu thủ cùng vị trí từ CLB vô địch C1 ngoài Top 20, "
                                   "vẫn phải đủ điều kiện và trên 20 trận; nếu không có thì để trống."),
        "award_recalc_version": TOOL_VERSION,
        "eligibility_rule": rule_text,
        "best_xi_method": best_xi_text,
        "method": method_text,
        "eligible_players": len(eligible),
        "recalculation_settings": {k: settings[k] for k in DEFAULTS},
        "recalculated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    # Golden Boy is intentionally preserved from the archived young_player
    # record: potential values are not always present in old season snapshots,
    # so the tool displays the saved winner rather than fabricating a new one.
    return new_awards, "OK"


def prepare_preview(project_root: Path, save_path: Path, settings: dict,
                    selected_seasons: Optional[Sequence[int]] = None) -> dict:
    settings = validate_settings(settings)
    blob, fingerprint = load_blob(save_path)
    state = blob["state"]
    helper = load_award_helper(project_root, state)
    selected = None if selected_seasons is None else {int(x) for x in selected_seasons}
    results = []
    for index, record in enumerate(state.get("history", []) or []):
        try:
            season = int(record.get("season"))
        except (TypeError, ValueError):
            season = -1
        if selected is not None and season not in selected:
            continue
        new_awards, message = recalculate_one_season(helper, record, settings)
        old = record.get("season_awards") if isinstance(record.get("season_awards"), dict) else {}
        if new_awards is None:
            results.append({"index": index, "season": season, "label": record.get("label", str(season)),
                            "status": message, "new_awards": None, "eligible": 0, "best_xi_count": 0,
                            "old_winner": _winner_name(old), "new_winner": "—",
                            # Even when a season cannot be recalculated, allow
                            # the user to inspect the awards already archived.
                            "display_awards": copy.deepcopy(old)})
            continue
        results.append({"index": index, "season": season, "label": record.get("label", str(season)),
                        "status": "Sẽ cập nhật", "new_awards": new_awards,
                        "eligible": int(new_awards.get("eligible_players", 0) or 0),
                        "best_xi_count": len(new_awards.get("best_xi_442", []) or []),
                        "old_winner": _winner_name(old), "new_winner": _winner_name(new_awards),
                        "display_awards": copy.deepcopy(new_awards)})
    return {"blob": blob, "fingerprint": fingerprint, "results": results,
            "settings": settings, "project_root": str(project_root), "save_path": str(save_path)}


def _winner_name(awards: dict) -> str:
    row = (awards or {}).get("ballon_dor") or {}
    return str(row.get("player") or row.get("player_name") or "Chưa có")


def apply_preview(preview: dict) -> None:
    database_path = Path(preview["save_path"])
    blob_now, fingerprint_now = load_blob(database_path)
    if fingerprint_now != preview["fingerprint"]:
        raise RuntimeError("Database đã thay đổi kể từ lúc xem trước. Hãy tải lại danh sách và chạy Preview lại.")
    state = blob_now["state"]
    history = list(state.get("history", []) or [])
    updated = 0
    for result in preview["results"]:
        if result.get("new_awards") is None:
            continue
        index = int(result["index"])
        if index < 0 or index >= len(history):
            raise RuntimeError("Danh sách archive mùa đã thay đổi. Hãy Preview lại.")
        history[index]["season_awards"] = copy.deepcopy(result["new_awards"])
        updated += 1
        if state.get("season_done") and int(history[index].get("season", -1)) == int(state.get("season", -2)):
            state["season_awards"] = copy.deepcopy(result["new_awards"])
    if not updated:
        raise RuntimeError("Không có mùa nào đủ dữ liệu để cập nhật.")

    # The SQLite transaction is the rollback boundary: no JSON mirror, archive
    # directory or persistent backup is created. All updated records commit together.
    state["history"] = []
    state["history_archive_mode"] = "database"
    database = SuperLeagueDatabase(database_path)
    database.save_career(blob_now, season_archives=history)
    return None


def _format_preview(results: Sequence[dict]) -> str:
    """Format season awards (all four individual titles) and the actual Best XI."""
    lines = ["MÙA       | ĐỦ ĐK | BEST XI | QUẢ BÓNG VÀNG CŨ → MỚI | TRẠNG THÁI",
             "----------+-------+---------+-----------------------+----------------"]
    for r in results:
        lines.append(f"{str(r['label']):9.9} | {r['eligible']:5d} | {r['best_xi_count']:7d} | "
                     f"{r['old_winner']} → {r['new_winner']} | {r['status']}")
    if not results:
        lines.append("Không có mùa nào được chọn hoặc save chưa có lịch sử.")
        return "\n".join(lines)

    for r in results:
        awards = r.get("display_awards") or r.get("new_awards") or {}
        lineup = awards.get("best_xi_442") or []
        lines.extend(["", f"DANH HIỆU CÁ NHÂN · MÙA {r.get('label', r.get('season', '?'))}"])
        for award in award_display_rows(awards):
            if award["available"]:
                lines.append(f"  {award['award']:<16} | {award['player']} ({award['team']}) | "
                             f"{award['apps']} trận | điểm {award['score']} | tuổi {award['age']}")
            else:
                lines.append(f"  {award['award']:<16} | Chưa có dữ liệu")
        lines.extend(["", f"ĐỘI HÌNH TIÊU BIỂU 4-4-2 · MÙA {r.get('label', r.get('season', '?'))} ({len(lineup)}/11)"])
        if not lineup:
            lines.append("  Chưa có cầu thủ đủ điều kiện hoặc mùa thiếu dữ liệu.")
            continue
        for index, row in enumerate(lineup, 1):
            role = str(row.get("role") or row.get("position") or "-")
            player = str(row.get("player") or row.get("player_name") or "Không rõ tên")
            team = str(row.get("team") or "Không rõ CLB")
            score = row.get("best_xi_score", row.get("vote_score", 0))
            bonus = row.get("best_xi_bonus", 0)
            honors = ", ".join(row.get("team_honors") or []) or "không có"
            try:
                score_text = f"{float(score):.2f} điểm"
            except (TypeError, ValueError):
                score_text = "— điểm"
            lines.append(f"  {index:>2}. {role:<3} | {player} ({team}) | {score_text} | thưởng +{float(bonus or 0):g} [{honors}]")
        if len(lineup) < 11:
            lines.append(f"  Còn thiếu {11-len(lineup)} vị trí: không có cầu thủ Top 20 hoặc cầu thủ đủ điều kiện cùng vị trí từ CLB vô địch C1.")
        top20 = awards.get("top20_players") or []
        if top20:
            lines.extend(["", f"TOP 20 CẦU THỦ XUẤT SẮC · MÙA {r.get('label', r.get('season', '?'))}"])
            for player_row in top20:
                rank = int(player_row.get("rank", 0) or 0)
                award = str(player_row.get("ballon_award") or "")
                player = str(player_row.get("player") or player_row.get("player_name") or "Không rõ tên")
                club = str(player_row.get("team") or "Không rõ CLB")
                score = float(player_row.get("vote_score", 0) or 0)
                position = str(player_row.get("position") or "—")
                apps = int(player_row.get("apps", 0) or 0)
                tag = f" · {award}" if award else ""
                lines.append(f"  {rank:>2}. {player} ({club}) · {position} · {apps} trận · {score:.2f} điểm{tag}")
    return "\n".join(lines)


def run_cli(args) -> int:
    root = Path(args.project).expanduser().resolve()
    save_override = Path(args.save).expanduser() if args.save else None
    try:
        root, _game_file, save_path = project_paths(root, save_override)
        settings = validate_settings({
            "min_appearances": args.min_appearances,
            "require_premier_champion": args.require_premier_champion,
            "require_c1_semifinal": args.require_c1_semifinal,
            "c1_champion_bonus": args.c1_bonus,
            "premier_champion_bonus": args.premier_bonus,
            "c1_runner_up_bonus": args.c1_runner_up_bonus,
            "premier_runner_up_bonus": args.premier_runner_up_bonus,
            "major_cup_champion_bonus": args.major_cup_champion_bonus,
            "weight_rating": args.weight_rating,
            "weight_contribution": args.weight_contribution,
            "weight_motm": args.weight_motm,
            "weight_minutes": args.weight_minutes,
        })
        if args.save_settings:
            settings_path, settings_backup = save_shared_settings(root, settings)
            print(f"Đã lưu thông số cho app chính: {settings_path}")
        selected = None
        if args.seasons and args.seasons.casefold() != "all":
            selected = [int(x.strip()) for x in args.seasons.split(",") if x.strip()]
        preview = prepare_preview(root, save_path, settings, selected)
        print(_format_preview(preview["results"]))
        if args.apply and args.dry_run:
            raise ValueError("Không thể dùng --apply cùng --dry-run.")
        if args.apply:
            if not args.yes:
                print("\nCẢNH BÁO: Hãy đóng Super League trước khi ghi database.")
                answer = input("Gõ APPLY để xác nhận cập nhật các mùa đã chọn: ").strip()
                if answer != "APPLY":
                    print("Đã hủy. Save không thay đổi.")
                    return 0
            apply_preview(preview)
            print("\nĐã cập nhật danh hiệu trực tiếp trong SQLite. Không tạo backup hoặc file phụ.")
        else:
            print("\nĐây chỉ là xem trước; database chưa bị thay đổi. Dùng --apply để ghi kết quả.")
        return 0
    except Exception as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 2


HISTORY_SHEET_HEADERS = [
    "Loại bản ghi", "Mùa", "Mùa giải", "Nhóm dữ liệu", "Giải đấu", "Giai đoạn",
    "STT", "CLB / Cầu thủ", "Đối thủ / Á quân", "Vị trí", "Số trận", "Thắng",
    "Hòa", "Thua", "Bàn thắng", "Bàn thua", "Hiệu số", "Điểm", "Bàn ghi",
    "Kiến tạo", "Số phút", "Rating TB", "MOTM", "Ngày", "Kết quả / Giá trị",
    "Chi tiết JSON", "Đồng bộ UTC",
]
HISTORY_JSON_CHUNK_SIZE = 32000  # Google Sheets cell limit is 50,000 characters.


def _history_season_identity(record: dict) -> Tuple[int, str, str]:
    season = int(record.get("season", 0) or 0)
    label = str(record.get("label") or f"{season}/{season + 1}")
    title = f"Lịch sử {season}-{season + 1}"
    return season, label, title


def build_history_sheet_payload(record: dict) -> dict:
    """Build a text-only, complete worksheet export from one archived season record.

    It writes friendly section tables first, then includes every top-level history
    field as chunked JSON so no nested historical data is silently omitted.
    """
    if not isinstance(record, dict):
        raise TypeError("Bản ghi lịch sử mùa phải là object.")
    season, label, title = _history_season_identity(record)
    synced_at = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    width = len(HISTORY_SHEET_HEADERS)
    rows: List[List[Any]] = []
    section_rows: List[int] = []
    header_rows: List[int] = []

    def add(**fields):
        row = [""] * width
        mapping = {
            "record_type": 0, "season": 1, "season_label": 2, "section": 3,
            "competition": 4, "phase": 5, "index": 6, "name": 7,
            "opponent": 8, "position": 9, "apps": 10, "won": 11,
            "drawn": 12, "lost": 13, "goals_for": 14, "goals_against": 15,
            "goal_difference": 16, "points": 17, "goals": 18, "assists": 19,
            "minutes": 20, "rating": 21, "motm": 22, "date": 23,
            "result": 24, "details": 25,
        }
        row[1], row[2], row[26] = season, label, synced_at
        for key, value in fields.items():
            if key in mapping:
                row[mapping[key]] = value
        rows.append(row)
        return len(rows)  # 1-based row index

    def section(name: str, *, with_headers: bool = True):
        row_num = add(record_type="SECTION", section=name, name=name)
        section_rows.append(row_num)
        if with_headers:
            header_num = add(record_type="HEADER", section=name,
                             details=" | ".join(HISTORY_SHEET_HEADERS))
            # Actual column names are repeated in the next row for easy filtering.
            rows[-1] = list(HISTORY_SHEET_HEADERS) + [""] * (width - len(HISTORY_SHEET_HEADERS))
            header_rows.append(header_num)
        return row_num

    # Cover / metadata.
    add(record_type="TITLE", section="LỊCH SỬ MÙA GIẢI", name=f"LỊCH SỬ {label}",
        result=f"{len(record.get('matches') or [])} trận · {len(record.get('career_player_stats') or [])} cầu thủ")
    add(record_type="INFO", section="Nguồn", name="Nguồn dữ liệu",
        result="memory/super_league.db → season_archives; chỉ đồng bộ text, không upload ảnh.")
    add(record_type="INFO", section="Đồng bộ", name="Cập nhật UTC", result=synced_at)
    rows.append([""] * width)

    section("TỔNG KẾT MÙA")
    summary_fields = (
        ("Mùa", label), ("Số trận trong lịch sử", len(record.get("matches") or [])),
        ("Nhà vô địch Premier League", record.get("champion_premier")),
        ("Nhà vô địch Major Cup", record.get("champion_cup")),
        ("Á quân Major Cup", record.get("runner_up_cup")),
        ("Nhà vô địch Champions League", record.get("champion_c1")),
        ("Á quân Champions League", record.get("runner_up_c1")),
    )
    for key, value in summary_fields:
        add(record_type="SUMMARY", section="TỔNG KẾT MÙA", name=key,
            result="" if value is None else str(value))

    # Competition champions, runners-up and player of the tournament.
    section("DANH HIỆU TỪNG GIẢI")
    competition_awards = record.get("competition_awards") or {}
    for comp_key, award in competition_awards.items():
        if not isinstance(award, dict):
            add(record_type="COMPETITION_AWARD", section="DANH HIỆU TỪNG GIẢI",
                competition=str(comp_key), result=str(award))
            continue
        pot = award.get("player_of_tournament") or {}
        add(record_type="COMPETITION_AWARD", section="DANH HIỆU TỪNG GIẢI",
            competition=award.get("competition") or comp_key, phase="Chung cuộc",
            name=award.get("champion") or "", opponent=award.get("runner_up") or "",
            apps=pot.get("apps", ""), goals=pot.get("goals", ""), assists=pot.get("assists", ""),
            minutes=pot.get("minutes", ""), rating=pot.get("avg_rating", ""),
            motm=pot.get("motm_count", ""), date=award.get("completed_date", ""),
            result=f"Vô địch: {award.get('champion') or '—'} · Á quân: {award.get('runner_up') or '—'}",
            details=json.dumps(award, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])
        if pot:
            add(record_type="PLAYER_OF_TOURNAMENT", section="DANH HIỆU TỪNG GIẢI",
                competition=award.get("competition") or comp_key, phase="Cầu thủ xuất sắc giải",
                name=pot.get("player") or pot.get("player_name") or "",
                position=pot.get("position", ""), apps=pot.get("apps", ""),
                goals=pot.get("goals", ""), assists=pot.get("assists", ""),
                minutes=pot.get("minutes", ""), rating=pot.get("avg_rating", ""),
                motm=pot.get("motm_count", ""), result=pot.get("team", ""),
                details=json.dumps(pot, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])

    # League standings by tier.
    section("BẢNG XẾP HẠNG LEAGUE")
    for tier_key, table in (record.get("leagues") or {}).items():
        competition = {"premier": "Premier League", "fc1": "FC1", "fc2": "FC2"}.get(str(tier_key), str(tier_key))
        for rank, item in enumerate(table or [], 1):
            if not isinstance(item, dict):
                continue
            add(record_type="STANDING", section="BẢNG XẾP HẠNG LEAGUE", competition=competition,
                phase="Bảng xếp hạng cuối giải", index=rank, name=item.get("team", ""),
                apps=item.get("p", item.get("played", "")), won=item.get("w", item.get("won", "")),
                drawn=item.get("d", item.get("drawn", "")), lost=item.get("l", item.get("lost", "")),
                goals_for=item.get("gf", item.get("goals_for", "")),
                goals_against=item.get("ga", item.get("goals_against", "")),
                goal_difference=item.get("gd", item.get("goal_difference", "")),
                points=item.get("pts", item.get("points", "")),
                result=f"Elo {item.get('elo', '')}",
                details=json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])

    # Promotion groups, qualification / Champions Points.
    section("GIAI ĐOẠN 2 & PLAY-OFF")
    phase2 = record.get("league_phase2") or {}
    premier_phase2 = phase2.get("premier", {}) if isinstance(phase2, dict) else {}
    if isinstance(premier_phase2, dict):
        for group_key, group_name in (("champion_teams", "Champion"), ("challenge_teams", "Challenge")):
            for idx, team in enumerate(premier_phase2.get(group_key) or [], 1):
                add(record_type="PHASE2_TEAM", section="GIAI ĐOẠN 2", competition="Premier League",
                    phase=group_name, index=idx, name=str(team))
    playoffs = phase2.get("promotion_playoffs", {}) if isinstance(phase2, dict) else {}
    for tier_key, entry in (playoffs or {}).items():
        if isinstance(entry, dict):
            add(record_type="PLAYOFF", section="GIAI ĐOẠN 2 & PLAY-OFF",
                competition=str(tier_key).upper(), phase="Thăng hạng",
                name=entry.get("winner", ""), result="Đội thắng play-off",
                details=json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])
    for key, values in (("promoted", record.get("promoted") or []),
                        ("relegated", record.get("relegated") or [])):
        for idx, item in enumerate(values, 1):
            team = item.get("team", "") if isinstance(item, dict) else str(item)
            add(record_type="PROMOTION" if key == "promoted" else "RELEGATION",
                section="GIAI ĐOẠN 2 & PLAY-OFF", phase=key, index=idx, name=team,
                result=(item.get("to") or item.get("from") or "") if isinstance(item, dict) else "",
                details=json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])

    section("CHAMPIONS POINTS")
    for rank, item in enumerate(record.get("qualification_table") or [], 1):
        if not isinstance(item, dict):
            continue
        add(record_type="CHAMPIONS_POINTS", section="CHAMPIONS POINTS", index=item.get("rank", rank),
            name=item.get("team", ""), phase=item.get("tier", ""), points=item.get("total", ""),
            goal_difference=item.get("gd", ""), result=f"League {item.get('league_points', 0)} + Cup {item.get('cup_points', 0)}",
            details=json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])

    # Player statistics and leaderboards.
    section("THỐNG KÊ CẦU THỦ CẢ MÙA")
    for item in record.get("career_player_stats") or []:
        if not isinstance(item, dict):
            continue
        add(record_type="PLAYER_SEASON_STATS", section="THỐNG KÊ CẦU THỦ CẢ MÙA",
            name=item.get("player", item.get("player_name", "")), phase="Tổng mùa",
            position=item.get("position", ""), apps=item.get("apps", ""),
            goals=item.get("goals", ""), assists=item.get("assists", ""),
            minutes=item.get("minutes", item.get("mins", "")), rating=item.get("avg_rating", ""),
            result=item.get("team", ""), details=json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])

    for key, title_text in (("top_scorers", "TOP GHI BÀN"), ("top_assists", "TOP KIẾN TẠO")):
        section(title_text)
        for rank, item in enumerate(record.get(key) or [], 1):
            if not isinstance(item, dict):
                continue
            add(record_type="LEADERBOARD", section=title_text, index=rank,
                name=item.get("player", ""), result=item.get("team", ""),
                position=item.get("position", ""), apps=item.get("apps", ""),
                goals=item.get("goals", ""), assists=item.get("assists", ""),
                minutes=item.get("minutes", ""), rating=item.get("avg_rating", ""),
                details=json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])

    # Individual awards, Top 20 and Best XI.
    section("DANH HIỆU CÁ NHÂN & ĐỘI HÌNH TIÊU BIỂU")
    annual = record.get("season_awards") or {}
    for rank, key, title_text in (
        (1, "ballon_dor", "Quả bóng Vàng"), (2, "ballon_silver", "Quả bóng Bạc"),
        (3, "ballon_bronze", "Quả bóng Đồng"), (0, "young_player", "Golden Boy"),
    ):
        player = annual.get(key) if isinstance(annual, dict) else None
        if isinstance(player, dict):
            add(record_type="INDIVIDUAL_AWARD", section="DANH HIỆU CÁ NHÂN", index=rank or "",
                phase=title_text, name=player.get("player", player.get("player_name", "")),
                result=player.get("team", ""), position=player.get("position", ""),
                apps=player.get("apps", ""), goals=player.get("goals", ""),
                assists=player.get("assists", ""), minutes=player.get("minutes", ""),
                rating=player.get("avg_rating", ""), motm=player.get("motm_count", ""),
                points=player.get("vote_score", player.get("young_vote_score", "")),
                details=json.dumps(player, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])
    for rank, player in enumerate(annual.get("top20_players") or [], 1):
        if not isinstance(player, dict):
            continue
        add(record_type="TOP20", section="TOP 20 CẦU THỦ", index=rank,
            phase=player.get("ballon_award", ""), name=player.get("player", ""),
            result=player.get("team", ""), position=player.get("position", ""),
            apps=player.get("apps", ""), goals=player.get("goals", ""),
            assists=player.get("assists", ""), minutes=player.get("minutes", ""),
            rating=player.get("avg_rating", ""), motm=player.get("motm_count", ""),
            points=player.get("vote_score", ""),
            details=json.dumps(player, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])
    for rank, player in enumerate(annual.get("best_xi_442") or [], 1):
        if not isinstance(player, dict):
            continue
        add(record_type="BEST_XI_442", section="ĐỘI HÌNH 4-4-2", index=rank,
            phase=player.get("role", ""), name=player.get("player", ""),
            result=player.get("team", ""), position=player.get("position", ""),
            apps=player.get("apps", ""), goals=player.get("goals", ""),
            assists=player.get("assists", ""), minutes=player.get("minutes", ""),
            rating=player.get("avg_rating", ""), points=player.get("best_xi_score", player.get("vote_score", "")),
            details=json.dumps(player, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])

    # Season-folder archives deliberately contain only the requested match summary.
    section("LỊCH SỬ TỪNG TRẬN ĐẤU")
    for rank, match in enumerate(record.get("matches") or [], 1):
        if not isinstance(match, dict):
            continue
        hg, ag = match.get("hg", match.get("home_goals", "")), match.get("ag", match.get("away_goals", ""))
        score = f"{hg} - {ag}" if hg != "" and ag != "" else "Không có tỷ số"
        if match.get("aet") or match.get("et"):
            score += " AET"
        pens = match.get("pens")
        if isinstance(pens, dict) and "home" in pens and "away" in pens:
            score += f" (pen {pens['home']}-{pens['away']})"
        winner = "Hòa"
        try:
            if int(hg) > int(ag):
                winner = match.get("home", "")
            elif int(ag) > int(hg):
                winner = match.get("away", "")
            elif isinstance(pens, dict):
                if int(pens.get("home")) > int(pens.get("away")):
                    winner = match.get("home", "")
                elif int(pens.get("away")) > int(pens.get("home")):
                    winner = match.get("away", "")
        except (TypeError, ValueError):
            pass
        details = json.dumps(match, ensure_ascii=False, separators=(",", ":"), default=str)
        add(record_type="MATCH", section="LỊCH SỬ TỪNG TRẬN ĐẤU",
            competition=match.get("comp", ""), phase=match.get("stage", ""), index=rank,
            name=match.get("home", ""), opponent=match.get("away", ""), date=match.get("date", ""),
            result=score, points=winner, details=details)

    # Player development, retirement and academy history.
    for key, title_text, record_type in (
        ("development", "PHÁT TRIỂN CẦU THỦ", "DEVELOPMENT"),
        ("retired", "CẦU THỦ GIẢI NGHỆ", "RETIREMENT"),
        ("youth_promoted", "CẦU THỦ HỌC VIỆN ĐƯỢC ĐÔN LÊN", "YOUTH_PROMOTION"),
    ):
        section(title_text)
        for rank, item in enumerate(record.get(key) or [], 1):
            if isinstance(item, dict):
                name = item.get("player", item.get("player_name", ""))
                team = item.get("team", item.get("club", ""))
                add(record_type=record_type, section=title_text, index=rank,
                    name=name, result=team, position=item.get("position", ""),
                    apps=item.get("appearances", item.get("apps", "")),
                    goals=item.get("goals", ""), assists=item.get("assists", ""),
                    minutes=item.get("minutes", ""), rating=item.get("avg_rating", ""),
                    details=json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)[:HISTORY_JSON_CHUNK_SIZE])
            else:
                add(record_type=record_type, section=title_text, index=rank, result=str(item))

    # Append a lossless archive representation for every key in the history record.
    # This captures every nested field, including match events, lineups, cards,
    # injuries, VAR, coach tactics, C1/Cup brackets and any fields added in future.
    section("DỮ LIỆU GỐC ĐẦY ĐỦ (JSON)")
    for key, value in record.items():
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        chunks = [serialized[i:i + HISTORY_JSON_CHUNK_SIZE]
                  for i in range(0, len(serialized), HISTORY_JSON_CHUNK_SIZE)] or [""]
        for part_no, chunk in enumerate(chunks, 1):
            add(record_type="ARCHIVE_JSON", section="DỮ LIỆU GỐC ĐẦY ĐỦ (JSON)",
                phase=str(key), index=f"{part_no}/{len(chunks)}", name=str(key),
                result=f"{len(serialized)} ký tự JSON", details=chunk)

    return {
        "season": season, "season_label": label, "sheet_title": title,
        "headers": HISTORY_SHEET_HEADERS, "rows": rows,
        "section_rows": section_rows, "header_rows": header_rows,
        "match_count": len(record.get("matches") or []),
        "player_count": len(record.get("career_player_stats") or []),
        "record_keys": len(record), "synced_at": synced_at,
    }


def _rename_google_sheet(sync_client, session, sheet_id: int, title: str) -> None:
    sync_client._request(session, "POST", f"https://sheets.googleapis.com/v4/spreadsheets/{sync_client.spreadsheet_id}:batchUpdate",
                         json={"requests": [{"updateSheetProperties": {
                             "properties": {"sheetId": int(sheet_id), "title": str(title)},
                             "fields": "title"}}]})


def push_history_payload(sync_client, payload: dict, session=None) -> dict:
    """Publish a text-only season history tab with staged replacement.

    Data is written to a temporary worksheet first. Existing canonical history
    tabs remain untouched until the new tab is fully written and formatted.
    No Apps Script endpoint or screenshot/image path is called.
    """
    from urllib.parse import quote

    if not getattr(sync_client, "spreadsheet_id", ""):
        raise RuntimeError("Thiếu google_sheets.spreadsheet_id trong config.json.")
    session = session or sync_client._make_session()
    api_root = "https://sheets.googleapis.com/v4/spreadsheets"
    title = str(payload["sheet_title"])
    rows = payload.get("rows") or []
    headers = payload.get("headers") or HISTORY_SHEET_HEADERS
    if not rows:
        raise RuntimeError(f"Không có dữ liệu lịch sử để push cho {title}.")

    meta = sync_client._request(session, "GET", f"{api_root}/{sync_client.spreadsheet_id}",
                                params={"fields": "sheets.properties(sheetId,title)"})
    sheets = [item.get("properties", {}) for item in meta.get("sheets", [])]
    existing = next((item for item in sheets if str(item.get("title", "")) == title), None)

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    temp_title = f"_SL_TMP_History_{payload['season']}_{stamp}"[:95]
    add_response = sync_client._request(
        session, "POST", f"{api_root}/{sync_client.spreadsheet_id}:batchUpdate",
        json={"requests": [{"addSheet": {"properties": {
            "title": temp_title,
            "gridProperties": {"rowCount": max(100, len(rows) + 20),
                               "columnCount": len(headers), "frozenRowCount": 3,
                               "hideGridlines": True},
        }}}]})
    properties = (((add_response.get("replies") or [{}])[0].get("addSheet") or {}).get("properties") or {})
    temp_sheet_id = int(properties.get("sheetId"))
    published = False
    backup_title = f"_SL_OLD_History_{payload['season']}_{stamp}"[:95]
    old_renamed = False
    try:
        # Send manageable request chunks to stay well below typical API payload limits.
        from sl_google_sheets import _column_letter
        last_col = _column_letter(len(headers))
        cursor = 0
        batch_rows: List[List[Any]] = []
        batch_bytes = 0
        max_batch_bytes = 900_000

        def flush_batch():
            nonlocal cursor, batch_rows, batch_bytes
            if not batch_rows:
                return
            first_row = cursor + 1
            last_row = cursor + len(batch_rows)
            range_name = f"'{temp_title}'!A{first_row}:{last_col}{last_row}"
            encoded = quote(range_name, safe="!:$'")
            sync_client._request(
                session, "PUT", f"{api_root}/{sync_client.spreadsheet_id}/values/{encoded}",
                params={"valueInputOption": "RAW"},
                json={"majorDimension": "ROWS", "values": batch_rows})
            cursor += len(batch_rows)
            batch_rows, batch_bytes = [], 0

        for row in rows:
            normalized = list(row[:len(headers)]) + [""] * max(0, len(headers) - len(row))
            encoded_size = len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if batch_rows and batch_bytes + encoded_size > max_batch_bytes:
                flush_batch()
            batch_rows.append(normalized)
            batch_bytes += encoded_size
        flush_batch()

        # Apply readable formatting. Images are deliberately not part of this workflow.
        format_requests = [
            {"updateSheetProperties": {"properties": {"sheetId": temp_sheet_id,
                 "gridProperties": {"frozenRowCount": 3, "hideGridlines": True}},
                 "fields": "gridProperties.frozenRowCount,gridProperties.hideGridlines"}},
            {"repeatCell": {"range": {"sheetId": temp_sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                                       "startColumnIndex": 0, "endColumnIndex": len(headers)},
                "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.05, "green": 0.10, "blue": 0.15},
                    "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True, "fontSize": 16},
                    "verticalAlignment": "MIDDLE"}}, "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": {"sheetId": temp_sheet_id, "startRowIndex": 1, "endRowIndex": 3,
                                       "startColumnIndex": 0, "endColumnIndex": len(headers)},
                "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.91, "green": 0.94, "blue": 0.96},
                    "textFormat": {"foregroundColor": {"red": 0.20, "green": 0.24, "blue": 0.28}, "italic": True},
                    "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat"}},
            {"autoResizeDimensions": {"dimensions": {"sheetId": temp_sheet_id, "dimension": "COLUMNS",
                                                       "startIndex": 0, "endIndex": len(headers)}}},
        ]
        for row_number in payload.get("section_rows", []):
            format_requests.append({"repeatCell": {"range": {"sheetId": temp_sheet_id,
                "startRowIndex": row_number - 1, "endRowIndex": row_number,
                "startColumnIndex": 0, "endColumnIndex": len(headers)},
                "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.12, "green": 0.25, "blue": 0.20},
                    "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True, "fontSize": 12}}},
                "fields": "userEnteredFormat"}})
        for row_number in payload.get("header_rows", []):
            format_requests.append({"repeatCell": {"range": {"sheetId": temp_sheet_id,
                "startRowIndex": row_number - 1, "endRowIndex": row_number,
                "startColumnIndex": 0, "endColumnIndex": len(headers)},
                "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.16, "green": 0.29, "blue": 0.24},
                    "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True}}},
                "fields": "userEnteredFormat"}})
        # Keep long JSON payloads readable without forcing all other columns to wrap.
        if "Chi tiết JSON" in headers:
            json_col = headers.index("Chi tiết JSON")
            format_requests.append({"repeatCell": {"range": {"sheetId": temp_sheet_id,
                "startRowIndex": 0, "endRowIndex": len(rows),
                "startColumnIndex": json_col, "endColumnIndex": json_col + 1},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment"}})
        sync_client._request(session, "POST", f"{api_root}/{sync_client.spreadsheet_id}:batchUpdate",
                             json={"requests": format_requests})

        # Publish the staged sheet. If replacing an existing history tab, keep its
        # data under a backup name until the new sheet has the canonical title.
        if existing:
            _rename_google_sheet(sync_client, session, int(existing.get("sheetId")), backup_title)
            old_renamed = True
        try:
            _rename_google_sheet(sync_client, session, temp_sheet_id, title)
            published = True
        except Exception:
            if old_renamed:
                _rename_google_sheet(sync_client, session, int(existing.get("sheetId")), title)
                old_renamed = False
            raise

        cleanup_pending = False
        if old_renamed:
            try:
                sync_client._request(session, "POST", f"{api_root}/{sync_client.spreadsheet_id}:batchUpdate",
                                     json={"requests": [{"deleteSheet": {"sheetId": int(existing.get("sheetId"))}}]})
            except Exception:
                cleanup_pending = True
        return {"worksheet_title": title, "sheet_id": temp_sheet_id,
                "created": not bool(existing), "rows_written": len(rows),
                "match_count": payload.get("match_count", 0),
                "player_count": payload.get("player_count", 0),
                "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{sync_client.spreadsheet_id}/edit",
                "cleanup_pending": cleanup_pending}
    except Exception:
        if not published:
            try:
                sync_client._request(session, "POST", f"{api_root}/{sync_client.spreadsheet_id}:batchUpdate",
                                     json={"requests": [{"deleteSheet": {"sheetId": temp_sheet_id}}]})
            except Exception:
                pass
        raise


def push_history_records_to_google_sheets(project_root: Path, records: Sequence[dict]) -> List[dict]:
    """Push selected archived seasons to canonical `Lịch sử YYYY-YYYY` tabs.

    Only Google Sheets API text/value writes are used. Apps Script, images and the
    main game's regular `Mùa YYYY-YYYY` worksheet flow are not invoked.
    """
    root = Path(project_root).expanduser().resolve()
    config_path = root / "config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy cấu hình Google Sheets: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    sheets_config = config.get("google_sheets") or {}
    if not sheets_config.get("enabled", False):
        raise RuntimeError("Google Sheets đang tắt. Hãy bật google_sheets.enabled trong config.json.")
    if not str(sheets_config.get("spreadsheet_id", "")).strip():
        raise RuntimeError("Thiếu google_sheets.spreadsheet_id trong config.json.")
    if not records:
        raise ValueError("Chưa chọn mùa lịch sử để push.")

    source_dir = str(root / "src")
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)
    try:
        from sl_google_sheets import GoogleSheetsSync
    except ImportError as exc:
        raise RuntimeError("Không tìm thấy sl_google_sheets.py trong thư mục project.") from exc
    sync_client = GoogleSheetsSync(sheets_config, root, root / "memory")
    credential = sync_client.credential_path()
    if not credential.is_file():
        raise FileNotFoundError(
            f"Chưa có Service Account JSON tại {credential}. Chỉ cần Service Account cho đồng bộ text; không cần Apps Script/ảnh.")

    session = sync_client._make_session()
    results = []
    for record in sorted(records, key=lambda item: int(item.get("season", 0) or 0)):
        payload = build_history_sheet_payload(record)
        results.append(push_history_payload(sync_client, payload, session=session))
    return results


class AwardsRecalcGUI:
    def __init__(self, root: Any, project_root: Path):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = root
        self.root.title("Super League – Tính lại danh hiệu mùa cũ")
        self.root.geometry("1320x900")
        self.root.minsize(1120, 760)
        self.project_var = tk.StringVar(value=str(project_root))
        try:
            initial_settings, settings_loaded = load_shared_settings(project_root)
        except Exception:
            initial_settings, settings_loaded = dict(DEFAULTS), False
        self.settings_loaded_from_file = settings_loaded
        self.settings_vars = {
            key: tk.StringVar(value=str(initial_settings[key]))
            for key in ("min_appearances", "c1_champion_bonus", "premier_champion_bonus",
                        "c1_runner_up_bonus", "premier_runner_up_bonus", "major_cup_champion_bonus",
                        "weight_rating", "weight_contribution", "weight_motm", "weight_minutes")
        }
        self.require_premier = tk.BooleanVar(value=initial_settings["require_premier_champion"])
        self.require_c1 = tk.BooleanVar(value=initial_settings["require_c1_semifinal"])
        self.season_items: List[dict] = []
        self.preview = None
        self.preview_settings = None
        self.preview_seasons = None
        self.preview_result_by_iid: Dict[str, dict] = {}
        self._build()
        self.load_seasons(show_errors=False)

    def _build(self):
        tk, ttk = self.tk, self.ttk
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="CÔNG CỤ TÍNH LẠI DANH HIỆU CÁC MÙA CŨ",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        ttk.Label(container, text="Lưu thông số sẽ tạo award_settings.json để app chính tự nạp khi chạy. Áp dụng mùa cũ cập nhật trực tiếp, không giữ backup.",
                  foreground="#8a4b08").pack(anchor="w", pady=(3, 10))

        path_row = ttk.Frame(container)
        path_row.pack(fill="x", pady=(0, 8))
        ttk.Label(path_row, text="Thư mục project:").pack(side="left")
        ttk.Entry(path_row, textvariable=self.project_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(path_row, text="Chọn…", command=self.choose_project).pack(side="left")
        ttk.Button(path_row, text="Tải lại mùa", command=self.load_seasons).pack(side="left", padx=(6, 0))

        body = ttk.Frame(container)
        body.pack(fill="both", expand=True)
        left = ttk.LabelFrame(body, text="Mùa cần tính lại", padding=8)
        left.pack(side="left", fill="y", padx=(0, 8))
        self.season_list = tk.Listbox(left, selectmode="extended", width=24, height=18, exportselection=False)
        self.season_list.pack(fill="y", expand=True)
        controls = ttk.Frame(left)
        controls.pack(fill="x", pady=(8, 0))
        ttk.Button(controls, text="Chọn tất cả", command=self.select_all).pack(side="left", fill="x", expand=True)
        ttk.Button(controls, text="Bỏ chọn", command=lambda: self.season_list.selection_clear(0, "end")).pack(side="left", fill="x", expand=True, padx=(5, 0))

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        rules = ttk.LabelFrame(right, text="Điều kiện bình chọn", padding=8)
        rules.pack(fill="x")
        row = ttk.Frame(rules); row.pack(fill="x", pady=2)
        ttk.Label(row, text="Số trận tối thiểu:").pack(side="left")
        ttk.Entry(row, textvariable=self.settings_vars["min_appearances"], width=7).pack(side="left", padx=5)
        ttk.Label(row, text="(21 = phải ra sân trên 20 trận)").pack(side="left")
        ttk.Checkbutton(rules, text="Phải ra sân ít nhất 1 trận tại bảng Champion Premier League",
                        variable=self.require_premier, command=self.invalidate_preview).pack(anchor="w", pady=2)
        ttk.Checkbutton(rules, text="Phải ra sân C1 và CLB vào bán kết trở lên",
                        variable=self.require_c1, command=self.invalidate_preview).pack(anchor="w", pady=2)

        bonuses = ttk.LabelFrame(right, text="Điểm ưu tiên riêng cho đội hình 4-4-2", padding=8)
        bonuses.pack(fill="x", pady=(8, 0))
        bonus_fields = (
            ("Vô địch C1:", "c1_champion_bonus"),
            ("Vô địch Premier:", "premier_champion_bonus"),
            ("Á quân C1:", "c1_runner_up_bonus"),
            ("Á quân Premier:", "premier_runner_up_bonus"),
            ("Vô địch Major Cup:", "major_cup_champion_bonus"),
        )
        for start in range(0, len(bonus_fields), 3):
            row = ttk.Frame(bonuses); row.pack(fill="x", pady=2)
            for label, key in bonus_fields[start:start + 3]:
                ttk.Label(row, text=label).pack(side="left")
                ttk.Entry(row, textvariable=self.settings_vars[key], width=6).pack(side="left", padx=(4, 12))
        ttk.Label(bonuses, text="Mặc định: Major Cup +3 = á quân Premier +3. Điểm cộng có thể cộng dồn và chỉ dùng cho Best XI 4-4-2.",
                  foreground="#555555").pack(anchor="w", pady=(4, 0))

        weights = ttk.LabelFrame(right, text="Trọng số bình chọn (tổng phải bằng 100%)", padding=8)
        weights.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(weights); row.pack(fill="x")
        for label, key in (("Rating", "weight_rating"), ("Bàn thắng/kiến tạo", "weight_contribution"),
                           ("MOTM", "weight_motm"), ("Phút", "weight_minutes")):
            ttk.Label(row, text=label).pack(side="left")
            ttk.Entry(row, textvariable=self.settings_vars[key], width=5).pack(side="left", padx=(4, 10))

        preview_frame = ttk.LabelFrame(right, text="Tổng quan kết quả tính lại · chọn một mùa để xem đủ danh hiệu và đội hình", padding=6)
        preview_frame.pack(fill="both", expand=True, pady=(8, 0))
        columns = ("season", "eligible", "bestxi", "old", "new", "status")
        self.tree = ttk.Treeview(preview_frame, columns=columns, show="headings", height=5, selectmode="browse")
        titles = {"season": "Mùa", "eligible": "Đủ ĐK", "bestxi": "Best XI", "old": "Quả bóng cũ", "new": "Quả bóng mới", "status": "Trạng thái"}
        widths = {"season": 80, "eligible": 65, "bestxi": 65, "old": 140, "new": 140, "status": 180}
        for col in columns:
            self.tree.heading(col, text=titles[col])
            self.tree.column(col, width=widths[col], anchor="w" if col in ("season", "old", "new", "status") else "center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.show_selected_awards)

        details_notebook = ttk.Notebook(right)
        details_notebook.pack(fill="both", expand=True, pady=(8, 0))

        awards_frame = ttk.Frame(details_notebook, padding=6)
        details_notebook.add(awards_frame, text="Danh hiệu cá nhân")
        ttk.Label(awards_frame,
                  text="Quả bóng Vàng, Bạc, Đồng được tính lại theo tham số hiện tại; Golden Boy hiển thị danh hiệu đã lưu của mùa.",
                  foreground="#555555", wraplength=850).pack(anchor="w", pady=(0, 6))
        award_columns = ("award", "player", "club", "apps", "score", "age")
        self.award_tree = ttk.Treeview(awards_frame, columns=award_columns, show="headings", height=5)
        award_titles = {"award": "Danh hiệu", "player": "Cầu thủ", "club": "CLB",
                        "apps": "Trận", "score": "Điểm", "age": "Tuổi"}
        award_widths = {"award": 155, "player": 190, "club": 170, "apps": 65, "score": 75, "age": 55}
        for col in award_columns:
            self.award_tree.heading(col, text=award_titles[col])
            self.award_tree.column(col, width=award_widths[col],
                                   anchor="w" if col in ("award", "player", "club") else "center")
        self.award_tree.pack(fill="both", expand=True)
        self.award_hint_var = tk.StringVar(value="Chọn một mùa sau khi Xem trước để xem đủ 4 danh hiệu cá nhân.")
        ttk.Label(awards_frame, textvariable=self.award_hint_var, foreground="#555555",
                  wraplength=900).pack(anchor="w", pady=(5, 0))

        xi_frame = ttk.Frame(details_notebook, padding=6)
        details_notebook.add(xi_frame, text="Đội hình tiêu biểu 4-4-2")
        xi_columns = ("role", "player", "club", "position", "apps", "rating", "score", "bonus", "honors")
        self.xi_tree = ttk.Treeview(xi_frame, columns=xi_columns, show="headings", height=10)
        xi_titles = {"role": "Vị trí", "player": "Cầu thủ", "club": "CLB", "position": "Sở trường",
                     "apps": "Trận", "rating": "Rating", "score": "Điểm chọn", "bonus": "Ưu tiên", "honors": "Thành tích CLB"}
        xi_widths = {"role": 62, "player": 145, "club": 130, "position": 72, "apps": 48,
                     "rating": 62, "score": 72, "bonus": 65, "honors": 230}
        for col in xi_columns:
            self.xi_tree.heading(col, text=xi_titles[col])
            self.xi_tree.column(col, width=xi_widths[col], anchor="w" if col in ("player", "club") else "center")
        self.xi_tree.pack(fill="both", expand=True)
        self.xi_hint_var = tk.StringVar(value="Chạy Xem trước, sau đó chọn một mùa ở bảng phía trên để xem đầy đủ danh sách cầu thủ.")
        ttk.Label(xi_frame, textvariable=self.xi_hint_var, foreground="#555555").pack(anchor="w", pady=(5, 0))

        top20_frame = ttk.Frame(details_notebook, padding=6)
        details_notebook.add(top20_frame, text="Top 20 cầu thủ")
        ttk.Label(top20_frame,
                  text="Chỉ hiển thị trong app phụ. Hạng 1/2/3 lần lượt là Quả bóng Vàng/Bạc/Đồng; các hạng sau dùng cùng điểm bình chọn và bộ lọc đủ điều kiện.",
                  foreground="#555555", wraplength=900).pack(anchor="w", pady=(0, 6))
        top20_columns = ("rank", "award", "player", "club", "position", "apps", "goals", "assists", "rating", "motm", "score")
        self.top20_tree = ttk.Treeview(top20_frame, columns=top20_columns, show="headings", height=12)
        top20_titles = {"rank": "#", "award": "Danh hiệu", "player": "Cầu thủ", "club": "CLB",
                        "position": "Vị trí", "apps": "Trận", "goals": "Bàn", "assists": "Kiến tạo",
                        "rating": "Rating", "motm": "MOTM", "score": "Điểm bình chọn"}
        top20_widths = {"rank": 42, "award": 105, "player": 155, "club": 135, "position": 62,
                        "apps": 48, "goals": 48, "assists": 62, "rating": 65, "motm": 55, "score": 82}
        for col in top20_columns:
            self.top20_tree.heading(col, text=top20_titles[col])
            self.top20_tree.column(col, width=top20_widths[col],
                                   anchor="w" if col in ("award", "player", "club") else "center")
        top20_scroll = ttk.Scrollbar(top20_frame, orient="vertical", command=self.top20_tree.yview)
        self.top20_tree.configure(yscrollcommand=top20_scroll.set)
        self.top20_tree.pack(side="left", fill="both", expand=True)
        top20_scroll.pack(side="right", fill="y")
        self.top20_hint_var = tk.StringVar(value="Chọn mùa và bấm Xem trước để xem Top 20 cầu thủ đủ điều kiện.")
        ttk.Label(top20_frame, textvariable=self.top20_hint_var, foreground="#555555").pack(side="bottom", anchor="w", pady=(5, 0))

        self.status_var = tk.StringVar(value="Sẵn sàng. Hãy chọn mùa, chỉnh tham số và bấm Xem trước.")
        ttk.Label(container, textvariable=self.status_var, wraplength=980).pack(anchor="w", pady=(8, 5))
        buttons = ttk.Frame(container); buttons.pack(fill="x")
        ttk.Button(buttons, text="Xem trước", command=self.preview_changes).pack(side="left")
        ttk.Button(buttons, text="Lưu thông số cho app chính", command=self.save_settings_for_main_app).pack(side="left", padx=6)
        self.apply_button = ttk.Button(buttons, text="Áp dụng danh hiệu mùa cũ", command=self.apply_changes, state="disabled")
        self.apply_button.pack(side="left", padx=6)
        self.push_button = ttk.Button(buttons, text="Push lịch sử lên Google Sheets",
                                      command=self.push_selected_history_to_sheets)
        self.push_button.pack(side="left", padx=6)
        ttk.Button(buttons, text="Đóng", command=self.root.destroy).pack(side="right")

        for var in self.settings_vars.values():
            var.trace_add("write", lambda *_: self.invalidate_preview())

    def invalidate_preview(self):
        self.preview = None
        self.apply_button.configure(state="disabled")

    def choose_project(self):
        from tkinter import filedialog, messagebox
        chosen = filedialog.askdirectory(title="Chọn thư mục gốc Super League", initialdir=self.project_var.get())
        if chosen:
            self.project_var.set(chosen)
            self.load_saved_settings_for_project(show_errors=True)
            self.load_seasons()

    def load_saved_settings_for_project(self, show_errors=False):
        from tkinter import messagebox
        try:
            settings, loaded = load_shared_settings(Path(self.project_var.get()).expanduser().resolve())
            for key, variable in self.settings_vars.items():
                variable.set(str(settings[key]))
            self.require_premier.set(settings["require_premier_champion"])
            self.require_c1.set(settings["require_c1_semifinal"])
            self.settings_loaded_from_file = loaded
            if hasattr(self, "status_var"):
                suffix = "Đã nạp thông số đã lưu." if loaded else "Chưa có file thông số; đang dùng mặc định."
                self.status_var.set(suffix)
            self.invalidate_preview()
        except Exception as exc:
            if show_errors:
                messagebox.showerror("Không đọc được thông số", str(exc))
            raise

    def save_settings_for_main_app(self):
        from tkinter import messagebox
        try:
            settings = self._settings()
            root = Path(self.project_var.get()).expanduser().resolve()
            path, backup = save_shared_settings(root, settings)
            self.settings_loaded_from_file = True
            self.status_var.set(f"Đã lưu thông số cho app chính: {path}. App chính sẽ dùng khi khởi động lần tiếp theo.")
            messagebox.showinfo("Đã lưu thông số", f"Đã lưu cấu hình bình chọn.\n\n{path}\n\nHãy đóng và chạy lại app chính để nạp thông số mới. Không tạo backup.")
        except Exception as exc:
            messagebox.showerror("Không thể lưu thông số", str(exc))

    def load_seasons(self, show_errors=True):
        from tkinter import messagebox
        self.invalidate_preview()
        try:
            root = Path(self.project_var.get()).expanduser().resolve()
            root, _game_file, save_path = project_paths(root)
            blob, _fingerprint = load_blob(save_path)
            self.season_items = list(enumerate(blob["state"].get("history", []) or []))
            self.season_list.delete(0, "end")
            for index, record in self.season_items:
                season = record.get("season", "?")
                label = record.get("label", "")
                self.season_list.insert("end", f"{season}  ·  {label}  ·  {len(record.get('matches', []) or [])} trận")
            if self.season_items:
                self.season_list.selection_set(0, "end")
                self.status_var.set(f"Đã nạp {len(self.season_items)} mùa từ {save_path}.")
            else:
                self.status_var.set("Database hiện chưa có mùa đã hoàn tất trong lịch sử.")
        except Exception as exc:
            self.season_items = []
            self.season_list.delete(0, "end")
            self.status_var.set(f"Không nạp được save: {exc}")
            if show_errors:
                messagebox.showerror("Không đọc được save", str(exc))

    def select_all(self):
        self.season_list.selection_set(0, "end")
        self.invalidate_preview()

    def _settings(self):
        values = {key: value.get() for key, value in self.settings_vars.items()}
        values["require_premier_champion"] = self.require_premier.get()
        values["require_c1_semifinal"] = self.require_c1.get()
        return validate_settings(values)

    def _selected_seasons(self):
        indices = self.season_list.curselection()
        return [int(self.season_items[i][1].get("season")) for i in indices]

    def preview_changes(self):
        from tkinter import messagebox
        try:
            settings = self._settings()
            seasons = self._selected_seasons()
            if not seasons:
                raise ValueError("Hãy chọn ít nhất một mùa.")
            root = Path(self.project_var.get()).expanduser().resolve()
            _, _, save_path = project_paths(root)
            self.preview = prepare_preview(root, save_path, settings, seasons)
            self.preview_settings = copy.deepcopy(settings)
            self.preview_seasons = list(seasons)
            for item in self.tree.get_children():
                self.tree.delete(item)
            for item in self.xi_tree.get_children():
                self.xi_tree.delete(item)
            for item in self.top20_tree.get_children():
                self.top20_tree.delete(item)
            self.preview_result_by_iid = {}
            for r in self.preview["results"]:
                iid = str(r["index"])
                self.preview_result_by_iid[iid] = r
                self.tree.insert("", "end", iid=iid, values=(r["label"], r["eligible"], r["best_xi_count"],
                                  r["old_winner"], r["new_winner"], r["status"]))
            first = self.tree.get_children()
            if first:
                self.tree.selection_set(first[0])
                self.tree.focus(first[0])
                self.show_selected_awards()
            else:
                self.xi_hint_var.set("Không có mùa nào đủ dữ liệu để hiển thị đội hình.")
            count = sum(1 for r in self.preview["results"] if r.get("new_awards") is not None)
            self.status_var.set(f"Preview: {count}/{len(self.preview['results'])} mùa có đủ dữ liệu để tính lại. Save chưa thay đổi.")
            self.apply_button.configure(state="normal" if count else "disabled")
        except Exception as exc:
            self.invalidate_preview()
            messagebox.showerror("Không thể xem trước", str(exc))

    def show_selected_awards(self, event=None):
        """Display Ballon podium and the archived Golden Boy for selected season."""
        for item in self.award_tree.get_children():
            self.award_tree.delete(item)
        selection = self.tree.selection()
        if not selection:
            self.award_hint_var.set("Chọn một mùa trong bảng tổng quan để xem danh hiệu cá nhân.")
            self.show_best_xi_details()
            self.show_top20_details()
            return
        result = self.preview_result_by_iid.get(str(selection[0]), {})
        awards = result.get("display_awards") or result.get("new_awards") or {}
        season_label = result.get("label", result.get("season", "?"))
        available = 0
        for row in award_display_rows(awards):
            self.award_tree.insert("", "end", values=(row["award"], row["player"], row["team"],
                                                         row["apps"], row["score"], row["age"]))
            available += int(bool(row["available"]))
        if available == 4:
            message = (f"Mùa {season_label}: hiển thị đủ 4 danh hiệu. Quả bóng Vàng/Bạc/Đồng là kết quả Preview mới; "
                       "Golden Boy được đọc từ hồ sơ đã lưu và không tự tính lại khi thiếu dữ liệu tiềm năng cũ.")
        else:
            missing = 4 - available
            message = (f"Mùa {season_label}: có {available}/4 danh hiệu có dữ liệu; còn {missing} danh hiệu chưa có bản ghi "
                       "trong lịch sử mùa.")
        self.award_hint_var.set(message)
        self.show_best_xi_details()
        self.show_top20_details()

    def show_top20_details(self, event=None):
        """Show up to 20 eligible players in the auxiliary app, with podium labels."""
        for item in self.top20_tree.get_children():
            self.top20_tree.delete(item)
        selection = self.tree.selection()
        if not selection:
            self.top20_hint_var.set("Chọn một mùa để xem danh sách Top 20.")
            return
        result = self.preview_result_by_iid.get(str(selection[0]), {})
        awards = result.get("new_awards") or result.get("display_awards") or {}
        rows = awards.get("top20_players") or []
        season_label = result.get("label", result.get("season", "?"))
        for fallback_rank, row in enumerate(rows, 1):
            rank = int(row.get("rank", fallback_rank) or fallback_rank)
            award = str(row.get("ballon_award") or {1: "Vàng", 2: "Bạc", 3: "Đồng"}.get(rank, ""))
            player = str(row.get("player") or row.get("player_name") or "Không rõ")
            club = str(row.get("team") or "—")
            position = str(row.get("position") or "—")
            apps = int(row.get("apps", 0) or 0)
            goals = int(row.get("goals", 0) or 0)
            assists = int(row.get("assists", 0) or 0)
            motm = int(row.get("motm_count", 0) or 0)
            rating = row.get("avg_rating")
            score = row.get("vote_score", 0)
            try:
                rating_text = f"{float(rating):.2f}" if rating is not None else "—"
            except (TypeError, ValueError):
                rating_text = "—"
            try:
                score_text = f"{float(score):.2f}"
            except (TypeError, ValueError):
                score_text = "—"
            self.top20_tree.insert("", "end", values=(rank, award, player, club, position,
                                   apps, goals, assists, rating_text, motm, score_text))
        if rows:
            self.top20_hint_var.set(f"Mùa {season_label}: {len(rows)} cầu thủ đủ điều kiện trong Top 20. Hạng 1/2/3 tương ứng Quả bóng Vàng/Bạc/Đồng.")
        else:
            self.top20_hint_var.set(f"Mùa {season_label}: chưa có Top 20 lưu trong preview; hãy chạy Xem trước để tính lại.")

    def show_best_xi_details(self, event=None):
        """Display the actual eleven selected players for the highlighted season."""
        for item in self.xi_tree.get_children():
            self.xi_tree.delete(item)
        selection = self.tree.selection()
        if not selection:
            self.xi_hint_var.set("Chọn một mùa trong bảng tổng quan để xem danh sách 4-4-2.")
            return
        result = self.preview_result_by_iid.get(str(selection[0]), {})
        awards = result.get("new_awards") or {}
        lineup = awards.get("best_xi_442") or []
        season_label = result.get("label", result.get("season", "?"))
        for row in lineup:
            role = str(row.get("role") or "—")
            player = str(row.get("player") or row.get("player_name") or "Không rõ")
            club = str(row.get("team") or "—")
            position = str(row.get("position") or "—")
            apps = int(row.get("apps", 0) or 0)
            rating = row.get("avg_rating")
            try:
                rating_text = f"{float(rating):.2f}" if rating is not None else "—"
            except (TypeError, ValueError):
                rating_text = "—"
            score = row.get("best_xi_score", row.get("vote_score", 0))
            bonus = row.get("best_xi_bonus", 0)
            honors = ", ".join(row.get("team_honors") or []) or "—"
            try:
                score_text = f"{float(score):.2f}"
                bonus_text = f"+{float(bonus):g}"
            except (TypeError, ValueError):
                score_text, bonus_text = "—", "—"
            self.xi_tree.insert("", "end", values=(role, player, club, position, apps, rating_text,
                                                     score_text, bonus_text, honors))
        count = len(lineup)
        if count == 11:
            fallbacks = sum(1 for row in lineup if row.get("best_xi_fallback"))
            message = f"Mùa {season_label}: đủ 11/11 cầu thủ; {fallbacks} vị trí được bổ sung từ CLB vô địch C1 ngoài Top 20. Danh sách trên sẽ được lưu khi bấm Áp dụng."
        elif count:
            fallbacks = sum(1 for row in lineup if row.get("best_xi_fallback"))
            message = f"Mùa {season_label}: {count}/11 cầu thủ; {fallbacks} vị trí được bổ sung từ CLB vô địch C1 ngoài Top 20. Vị trí còn thiếu không có ứng viên cùng vị trí đủ chuẩn."
        else:
            message = f"Mùa {season_label}: không có đội hình mới (thiếu dữ liệu hoặc chưa có cầu thủ đủ điều kiện)."
        self.xi_hint_var.set(message)


    def push_selected_history_to_sheets(self):
        from tkinter import messagebox
        try:
            selected = set(self._selected_seasons())
            if not selected:
                raise ValueError("Hãy chọn ít nhất một mùa ở danh sách bên trái.")
            root = Path(self.project_var.get()).expanduser().resolve()
            _, _, save_path = project_paths(root)
            blob, _fingerprint = load_blob(save_path)
            records = [copy.deepcopy(record) for record in (blob["state"].get("history") or [])
                       if int(record.get("season", -1)) in selected]
            found = {int(record.get("season", -1)) for record in records}
            missing = sorted(selected - found)
            if missing:
                raise ValueError("Không tìm thấy bản ghi lịch sử cho mùa: " + ", ".join(map(str, missing)))
            config_path = root / "config.json"
            if not config_path.is_file():
                raise FileNotFoundError(f"Không tìm thấy {config_path}")
            project_config = json.loads(config_path.read_text(encoding="utf-8"))
            gs_cfg = project_config.get("google_sheets") or {}
            if not gs_cfg.get("enabled", False):
                raise RuntimeError("Google Sheets đang tắt. Bật google_sheets.enabled trong config.json trước.")
            if not str(gs_cfg.get("spreadsheet_id", "")).strip():
                raise RuntimeError("Thiếu spreadsheet_id trong config.json.")

            match_count = sum(len(record.get("matches") or []) for record in records)
            season_names = ", ".join(_history_season_identity(record)[2] for record in records)
            preview_note = ""
            if self.preview and any(result.get("new_awards") is not None for result in self.preview.get("results", [])):
                preview_note = ("\n\nLưu ý: nếu bạn vừa chạy Preview nhưng chưa bấm Áp dụng mùa cũ, "
                                "các danh hiệu preview chưa nằm trong save và sẽ chưa được push.")
            confirm = (
                f"Push {len(records)} mùa ({match_count} trận) lên Google Sheets?\n\n{season_names}\n\n"
                "Mỗi mùa sẽ được ghi vào tab 'Lịch sử YYYY-YYYY'. Nếu tab đã tồn tại, nội dung tab đó sẽ được thay thế; "
                "các tab khác không bị ảnh hưởng. Chỉ dữ liệu text/JSON được gửi, không upload ảnh hay screenshot."
                f"{preview_note}\n\nHãy chắc chắn Super League đã đóng để tránh save thay đổi trong lúc push."
            )
            if not messagebox.askyesno("Xác nhận Push lịch sử", confirm):
                return

            self.push_button.configure(state="disabled")
            self.status_var.set(f"Đang push {len(records)} mùa lịch sử lên Google Sheets (text-only)…")
            worker = threading.Thread(target=self._push_history_worker,
                                      args=(root, records), daemon=True,
                                      name="AwardHistoryGoogleSheetsPush")
            worker.start()
        except Exception as exc:
            messagebox.showerror("Không thể Push lịch sử", str(exc))

    def _push_history_worker(self, project_root: Path, records: Sequence[dict]):
        try:
            results = push_history_records_to_google_sheets(project_root, records)
            error = None
        except Exception as exc:
            results, error = [], str(exc)
        try:
            self.root.after(0, lambda: self._finish_history_push(results, error))
        except Exception:
            pass

    def _finish_history_push(self, results: Sequence[dict], error: Optional[str]):
        from tkinter import messagebox
        self.push_button.configure(state="normal")
        if error:
            self.status_var.set(f"Push lịch sử thất bại: {error}")
            messagebox.showerror("Push Google Sheets thất bại", error)
            return
        lines = []
        for item in results:
            line = (f"{item['worksheet_title']}: {item['rows_written']:,} dòng, "
                    f"{item.get('match_count', 0)} trận, {item.get('player_count', 0)} cầu thủ")
            if item.get("cleanup_pending"):
                line += " (tab backup cũ chưa xóa được)"
            lines.append(line)
        spreadsheet_url = results[0].get("spreadsheet_url") if results else ""
        self.status_var.set("Đã Push lịch sử: " + " · ".join(item["worksheet_title"] for item in results))
        message = "Đã push thành công dữ liệu lịch sử (text-only):\n\n" + "\n".join(lines)
        if spreadsheet_url:
            message += "\n\nGoogle Sheets:\n" + spreadsheet_url
        messagebox.showinfo("Push lịch sử hoàn tất", message)

    def apply_changes(self):
        from tkinter import messagebox
        try:
            if not self.preview:
                raise RuntimeError("Hãy bấm Xem trước trước khi áp dụng.")
            if self._settings() != self.preview_settings or self._selected_seasons() != self.preview_seasons:
                raise RuntimeError("Tham số hoặc mùa đã thay đổi. Hãy chạy Preview lại.")
            count = sum(1 for r in self.preview["results"] if r.get("new_awards") is not None)
            if not messagebox.askyesno("Xác nhận cập nhật", f"Cập nhật danh hiệu cho {count} mùa đã chọn?\n\nThay đổi được ghi trực tiếp, không giữ bản backup. Hãy chắc chắn game đã đóng."):
                return
            apply_preview(self.preview)
            self.preview = None
            self.apply_button.configure(state="disabled")
            self.status_var.set("Đã cập nhật danh hiệu; không tạo backup.")
            messagebox.showinfo("Hoàn tất", f"Đã cập nhật danh hiệu cho {count} mùa. Không tạo backup.")
            self.load_seasons(show_errors=False)
        except Exception as exc:
            messagebox.showerror("Không thể áp dụng", str(exc))


def build_parser():
    parser = argparse.ArgumentParser(description="Tính lại danh hiệu cá nhân và đội hình tiêu biểu ở các mùa cũ.")
    parser.add_argument("--project", default=str(Path(__file__).resolve().parents[1]), help="Thư mục gốc project Super League")
    parser.add_argument("--save", default=None, help="Đường dẫn SQLite database nếu không dùng memory/super_league.db")
    parser.add_argument("--seasons", default="all", help="all hoặc danh sách năm, ví dụ 2026,2027")
    parser.add_argument("--min-appearances", type=int, default=21, help="Số lần ra sân tối thiểu (mặc định 21)")
    parser.add_argument("--require-premier-champion", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--require-c1-semifinal", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--c1-bonus", type=float, default=5.0)
    parser.add_argument("--premier-bonus", type=float, default=4.0)
    parser.add_argument("--c1-runner-up-bonus", type=float, default=4.0)
    parser.add_argument("--premier-runner-up-bonus", type=float, default=3.0)
    parser.add_argument("--major-cup-champion-bonus", type=float, default=3.0)
    parser.add_argument("--weight-rating", type=float, default=42.0)
    parser.add_argument("--weight-contribution", type=float, default=28.0)
    parser.add_argument("--weight-motm", type=float, default=20.0)
    parser.add_argument("--weight-minutes", type=float, default=10.0)
    parser.add_argument("--save-settings", action="store_true", help="Lưu thông số vào award_settings.json để app chính nạp khi khởi động")
    parser.add_argument("--apply", action="store_true", help="Ghi kết quả đã tính vào save; không tạo backup")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ xem trước; không ghi save (mặc định nếu thiếu --apply)")
    parser.add_argument("--yes", action="store_true", help="Không hỏi xác nhận trong CLI; chỉ dùng khi đã kiểm tra kỹ")
    parser.add_argument("--self-test", action="store_true", help="Chạy kiểm tra nội bộ với dữ liệu giả lập")
    return parser


def _run_self_test(project_root: Path) -> bool:
    """Small synthetic end-to-end test of eligibility, bonus and historical update."""
    # Use synthetic groups: Champion, Challenge-only, and a C1 semi-finalist.
    players = {}
    rows = []
    positions = ["GK", "LB", "CB", "CB", "RB", "LM", "CM", "CM", "RM", "ST", "ST"]
    teams = ["Premier Champion A", "Premier Champion B", "Challenge Only", "C1 Semi"]
    for ti, team in enumerate(teams):
        for idx, pos in enumerate(positions):
            pid = f"{ti}-{idx}"
            players[pid] = {"player_id": pid, "common_name": f"Player {pid}", "position": pos,
                            "club": team, "birthdate": "01-01-2000", "overall_rating": 80}
            rows.append({"player_id": pid, "player": f"Player {pid}", "team": team, "position": pos,
                         "apps": 20 if (ti == 3 and idx == 0) else 21,
                         "minutes": 1890, "goals": 10 - idx, "assists": idx,
                         "avg_rating": 7.5 + min(idx, 3) * .1})
    matches = []
    for i in range(21):
        minutes_home = {f"0-{j}": 90 for j in range(11)}
        minutes_away = {f"1-{j}": 90 for j in range(11)}
        matches.append({"id": i + 1, "played": True, "comp": "Premier League",
                        "stage": f"league_p2_premier_champion_{(i % 6) + 1}",
                        "round": f"Champion · {i+1}", "home": "Premier Champion A", "away": "Premier Champion B",
                        "minutes_h": minutes_home, "minutes_a": minutes_away,
                        "player_stats": {"h": {}, "a": {}}, "goals": [], "motm": None})
    for i in range(21):
        matches.append({"id": 30 + i, "played": True, "comp": "Premier League",
                        "stage": f"league_p2_premier_challenge_{(i % 6) + 1}",
                        "round": f"Challenge · {i+1}", "home": "Challenge Only", "away": "Other Challenge",
                        "minutes_h": {f"2-{j}": 90 for j in range(11)}, "minutes_a": {},
                        "player_stats": {"h": {}, "a": {}}, "goals": [], "motm": None})
    # C1 team appears only in its semifinal: those players meet the path condition;
    # their overall archived apps meet the season minimum.
    matches.append({"id": 99, "played": True, "comp": "C1", "stage": "c1_semi1_leg1", "round": "Bán kết",
                    "home": "C1 Semi", "away": "Opponent", "minutes_h": {f"3-{j}": 90 for j in range(11)},
                    "minutes_a": {}, "player_stats": {"h": {}, "a": {}}, "goals": [], "motm": None})
    state = {"season": 2026, "players": players, "retired_players": [], "rosters": {}, "player_meta": {},
             "history": [], "season_done": False}
    helper = load_award_helper(project_root, state)
    record = {"season": 2026, "label": "2026/2027", "matches": matches,
              "career_player_stats": rows,
              "champion_premier": "Premier Champion", "champion_c1": "C1 Semi",
              "competition_awards": {
                  "premier": {"champion": "Premier Champion", "runner_up": "Premier Runner-up"},
                  "c1": {"champion": "C1 Semi", "runner_up": "C1 Runner-up"},
                  "cup": {"champion": "Major Winner", "runner_up": "Major Runner-up"},
              },
              "season_awards": {"young_player": {"player_id": "golden-boy-1", "player": "Golden Boy Demo",
                                                   "team": "Premier Champion A", "apps": 24,
                                                   "age": 19, "young_vote_score": 88.4}}}
    awards, status = recalculate_one_season(helper, record, validate_settings(DEFAULTS))
    assert status == "OK" and awards is not None
    assert awards["eligible_players"] == 32, awards["eligible_players"]  # 22 Premier Champion + 10 C1; Challenge excluded, 20-app C1 player excluded
    assert len(awards["best_xi_442"]) == 11, len(awards["best_xi_442"])
    assert len({str(row.get("player_id")) for row in awards["best_xi_442"]}) == 11
    assert all(row.get("role") for row in awards["best_xi_442"])
    assert all(row.get("player") and row.get("team") for row in awards["best_xi_442"])
    xi_roles = [row.get("role") for row in awards["best_xi_442"]]
    assert xi_roles.count("GK") == 1
    assert sum(role in {"LB", "CB", "RB"} for role in xi_roles) == 4
    assert sum(role in {"LM", "CM", "RM"} for role in xi_roles) == 4
    assert xi_roles.count("ST") == 2
    top20 = awards.get("top20_players") or []
    assert len(top20) == 20, len(top20)
    assert [row.get("rank") for row in top20] == list(range(1, 21))
    assert [row.get("player_id") for row in top20[:3]] == [row.get("player_id") for row in awards["ballon_podium"]]
    assert [row.get("ballon_award") for row in top20[:3]] == ["Quả bóng Vàng", "Quả bóng Bạc", "Quả bóng Đồng"]
    assert all(row["team"] != "Challenge Only" for row in awards["ballon_podium"])
    assert awards.get("ballon_dor") and awards.get("ballon_silver") and awards.get("ballon_bronze")
    display_awards = award_display_rows(awards)
    assert [row["award"] for row in display_awards] == ["Quả bóng Vàng", "Quả bóng Bạc", "Quả bóng Đồng", "Golden Boy"]
    assert display_awards[0]["available"] and display_awards[1]["available"] and display_awards[2]["available"]
    assert display_awards[3]["available"] and display_awards[3]["player"] == "Golden Boy Demo"
    formatted = _format_preview([{"label": "2026/2027", "eligible": awards["eligible_players"],
                                 "best_xi_count": len(awards["best_xi_442"]), "old_winner": "Old",
                                 "new_winner": _winner_name(awards), "status": "Sẽ cập nhật",
                                 "new_awards": awards}])
    assert "Quả bóng Bạc" in formatted and "Quả bóng Đồng" in formatted and "Golden Boy Demo" in formatted
    assert "TOP 20 CẦU THỦ XUẤT SẮC" in formatted and "Quả bóng Vàng" in formatted
    assert all(row["player_id"] != "3-0" for row in awards["ballon_podium"] + awards["best_xi_442"])
    # Verify all five club-honor bonuses; Major Cup winner equals Premier runner-up.
    honor_teams = _season_honor_teams(helper, record)
    expected = {
        "Premier Champion": 4.0,
        "C1 Semi": 5.0,
        "Premier Runner-up": 3.0,
        "C1 Runner-up": 4.0,
        "Major Winner": 3.0,
    }
    for club, value in expected.items():
        amount, labels = _team_best_xi_bonus(club, honor_teams, validate_settings(DEFAULTS))
        assert amount == value, (club, amount, value)
        assert labels
    assert expected["Premier Runner-up"] == expected["Major Winner"]
    assert awards["best_xi_version"] == 8
    assert awards["top20_version"] == 1
    # A save-like archive should only receive updates on apply_preview; preview is read-only.
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        try:
            project = Path(args.project).expanduser().resolve()
            ok = _run_self_test(project)
            print("Self-test award recalculation: PASS" if ok else "Self-test: FAIL")
            return 0 if ok else 1
        except Exception as exc:
            print(f"Self-test: FAIL - {exc}", file=sys.stderr)
            return 1
    if argv:
        return run_cli(args)
    try:
        import tkinter as tk
        root = tk.Tk()
        AwardsRecalcGUI(root, Path(__file__).resolve().parents[1])
        root.mainloop()
        return 0
    except Exception as exc:
        print(f"Không mở được giao diện đồ họa ({exc}). Chạy với --help để dùng chế độ dòng lệnh.", file=sys.stderr)
        parser.print_help()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
