# -*- coding: utf-8 -*-
"""Player career/profile aggregation from the saved career and season archives."""
from __future__ import annotations

from collections import defaultdict


def _name(player, pid):
    return str(player.get("common_name") or player.get("player_name") or player.get("name") or pid)


def _avg(total, count):
    return round(float(total) / int(count), 2) if count else None


def record_all(game):
    """Refresh the current-season row in player_career; idempotent per player/season."""
    state = game.s
    career = state.setdefault("player_career", {})
    season = int(state.get("season", 0) or 0)
    for pid, stat in (state.get("season_stats") or {}).items():
        player = (state.get("players") or {}).get(str(pid), {})
        club = next((team for team, ids in (state.get("rosters") or {}).items()
                     if str(pid) in {str(x) for x in ids}), player.get("club", "—"))
        row = {
            "season": season, "club": str(club or "—"), "player": _name(player, pid),
            "position": player.get("position", "—"), "apps": int(stat.get("apps", 0) or 0),
            "starts": int(stat.get("starts", stat.get("apps", 0)) or 0),
            "sub_apps": int(stat.get("sub_apps", 0) or 0), "minutes": int(stat.get("mins", stat.get("minutes", 0)) or 0),
            "goals": int(stat.get("goals", 0) or 0), "assists": int(stat.get("assists", 0) or 0),
            "clean_sheets": int(stat.get("clean_sheets", 0) or 0), "yellow": int(stat.get("yellow", 0) or 0),
            "red": int(stat.get("red", 0) or 0),
            "avg_rating": _avg(stat.get("rating_sum", 0), stat.get("rating_count", 0)),
        }
        rows = career.setdefault(str(pid), [])
        existing = next((index for index, old in enumerate(rows)
                         if int(old.get("season", -1) or -1) == season), None)
        if existing is None:
            rows.append(row)
        else:
            rows[existing] = row


def _archive_rows(state, pid):
    rows = []
    for archive in state.get("history", []) or []:
        season = int(archive.get("season", 0) or 0)
        for item in archive.get("career_player_stats", []) or []:
            if str(item.get("player_id", "")) != str(pid):
                continue
            rows.append({
                "season": season, "club": item.get("team", "—"), "player": item.get("player", str(pid)),
                "position": item.get("position", "—"), "apps": int(item.get("apps", 0) or 0),
                "starts": int(item.get("starts", item.get("apps", 0)) or 0),
                "sub_apps": int(item.get("sub_apps", 0) or 0), "minutes": int(item.get("minutes", 0) or 0),
                "goals": int(item.get("goals", 0) or 0), "assists": int(item.get("assists", 0) or 0),
                "clean_sheets": int(item.get("clean_sheets", 0) or 0), "yellow": int(item.get("yellow", 0) or 0),
                "red": int(item.get("red", 0) or 0), "avg_rating": item.get("avg_rating"),
                "archive_compact": True,
            })
    return rows


def _award_rows(state, pid):
    """Read canonical season award ledgers and normalize legacy Golden Boy IDs."""
    individual, team = [], []
    seen_individual = set()
    seen_team = set()

    def add_individual(season, award_id, title, competition="Toàn mùa", club="—",
                       rank=None, value=None, unit="", detail=""):
        award_id = str(award_id or "")
        if award_id in {"young_player", "golden_boy"}:
            award_id, title = "golden_boy", "Golden Boy"
        token = (int(season or 0), award_id, str(competition or "Toàn mùa"))
        if not award_id or token in seen_individual:
            return
        seen_individual.add(token)
        individual.append({"season": int(season or 0), "award_id": award_id,
                           "title": str(title or award_id.replace("_", " ").title()),
                           "competition": str(competition or "Toàn mùa"), "club": str(club or "—"),
                           "rank": rank, "value": value, "unit": unit or "", "detail": detail or ""})

    for archive in state.get("history", []) or []:
        season = int(archive.get("season", 0) or 0)
        canonical = archive.get("individual_awards") or []
        if isinstance(canonical, list) and canonical:
            for item in canonical:
                if not isinstance(item, dict) or str(item.get("player_id", "")) != str(pid):
                    continue
                add_individual(season, item.get("award_id"), item.get("title"),
                               item.get("competition", "Toàn mùa"), item.get("club", "—"),
                               item.get("rank"), item.get("value"), item.get("unit", ""),
                               item.get("detail", ""))
        else:
            season_awards = archive.get("season_awards") or {}
            for award_id, item in season_awards.items() if isinstance(season_awards, dict) else []:
                if isinstance(item, dict) and str(item.get("player_id", "")) == str(pid):
                    title = "Golden Boy" if award_id in {"young_player", "golden_boy"} else award_id.replace("_", " ").title()
                    add_individual(season, award_id, title, item.get("competition", "Toàn mùa"),
                                   item.get("team", "—"), item.get("rank"), item.get("value"), item.get("unit", ""))
            comp_awards = archive.get("competition_awards") or {}
            if isinstance(comp_awards, dict):
                for comp, block in comp_awards.items():
                    if not isinstance(block, dict):
                        continue
                    for award_id, item in block.items():
                        if isinstance(item, dict) and str(item.get("player_id", "")) == str(pid):
                            add_individual(season, award_id, item.get("title") or award_id.replace("_", " ").title(),
                                           block.get("competition", comp), item.get("team", "—"),
                                           item.get("rank"), item.get("value"), item.get("unit", ""))

        for item in archive.get("team_awards", []) or []:
            if isinstance(item, dict) and str(item.get("player_id", "")) == str(pid):
                row = dict(item, season=season)
                token = (season, str(row.get("award_id") or row.get("title") or ""),
                         str(row.get("competition") or ""))
                if token not in seen_team:
                    seen_team.add(token)
                    team.append(row)
    individual.sort(key=lambda row: (row["season"], row["title"], row["competition"]))
    team.sort(key=lambda row: (int(row.get("season", 0) or 0), str(row.get("title", ""))))
    return individual, team


def _dedupe_career_rows(rows):
    """Keep one career line per season, preferring the canonical season archive."""
    by_season = {}
    for row in rows:
        try:
            season = int(row.get("season", 0) or 0)
        except (TypeError, ValueError):
            season = 0
        current = by_season.get(season)
        if current is None or (bool(row.get("archive_compact")) and not bool(current.get("archive_compact"))):
            by_season[season] = dict(row)
    return [by_season[season] for season in sorted(by_season)]


def _dedupe_development_rows(rows):
    """Collapse archive/last_development overlap to a single development point per season."""
    by_season = {}
    for row in rows:
        try:
            season = int(row.get("season", 0) or 0)
        except (TypeError, ValueError):
            season = 0
        current = by_season.get(season)
        # Later rows are sourced from last_development and are the freshest point.
        if current is None or row.get("_latest"):
            clean = dict(row)
            clean.pop("_latest", None)
            by_season[season] = clean
    return [by_season[season] for season in sorted(by_season)]


def build_player_profile(state: dict, player_id: str, *, make_player=None,
                          team_form_row=None, season_label="", player=None, meta=None) -> dict:
    pid = str(player_id)
    players = state.get("players", {}) or {}
    player = dict(player or players.get(pid) or {})
    if not player:
        for collection in ("generated_players", "retired_players", "academy_alumni"):
            for row in state.get(collection, []) or []:
                if str(row.get("player_id", row.get("id", ""))) == pid:
                    player = dict(row); break
            if player: break
    meta = dict(meta or (state.get("player_meta", {}) or {}).get(pid) or {})
    club = next((team for team, ids in (state.get("rosters", {}) or {}).items()
                 if pid in {str(x) for x in ids}), player.get("club") or "—")
    contract = (state.get("contracts", {}) or {}).get(pid, {}) or {}
    dyn = (state.get("dyn", {}) or {}).get(pid, {}) or {}
    stat = (state.get("season_stats", {}) or {}).get(pid, {}) or {}
    position = str(player.get("position") or "—").upper()
    group = "Thủ môn" if position == "GK" else ("Hậu vệ" if position in {"CB", "LB", "RB", "LWB", "RWB"} else
            ("Tiền đạo" if position in {"ST", "CF", "LW", "RW"} else "Tiền vệ"))
    form = (team_form_row or {}).get("form", dyn.get("form"))
    fitness = (team_form_row or {}).get("fit", dyn.get("fit"))
    try: age = float(meta.get("age")) if meta.get("age") is not None else None
    except (TypeError, ValueError): age = None
    try: ovr = float(player.get("overall_rating", 50) or 50)
    except (TypeError, ValueError): ovr = 50.0
    try: potential = float(meta.get("potential")) if meta.get("potential") is not None else None
    except (TypeError, ValueError): potential = None

    medical_rows = [row for row in state.get("medical_records", []) or [] if str(row.get("player_id")) == pid]
    active_medical = next((row for row in reversed(medical_rows) if row.get("status") == "active"), None)
    days_to_recovery = 0
    if active_medical:
        try:
            from datetime import date
            end = date.fromisoformat(str(active_medical.get("expected_return")))
            current = date.fromisoformat(str(state.get("economy_last_date") or state.get("season_start_date") or "2026-08-01"))
            days_to_recovery = max(0, (end - current).days)
        except (TypeError, ValueError): pass
    status = (state.get("player_status", {}) or {}).get(pid, {}) or {}
    unavailable = []
    if int(status.get("inj", 0) or 0) > 0 or active_medical:
        unavailable.append(active_medical.get("injury_name", status.get("injury_name", "Chấn thương")) if active_medical else "Chấn thương")
    if int(status.get("susp", 0) or 0) > 0: unavailable.append("Treo giò")
    medical = {"active_injury": active_medical, "days_to_recovery": days_to_recovery,
               "episodes": len(medical_rows), "permanent_damage": (state.get("medical_damage", {}) or {}).get(pid, {})}

    attributes = []
    label_map = {
        "pace": "Tốc độ", "shooting": "Dứt điểm", "passing": "Chuyền", "dribbling": "Rê bóng",
        "defending": "Phòng ngự", "physicality": "Thể chất", "goalkeeping_diving": "Bay người",
        "goalkeeping_handling": "Bắt bóng", "goalkeeping_kicking": "Phát bóng",
        "goalkeeping_positioning": "Chọn vị trí", "goalkeeping_reflexes": "Phản xạ",
        "attacking_crossing": "Tạt bóng", "attacking_finishing": "Dứt điểm", "attacking_heading_accuracy": "Đánh đầu",
        "attacking_short_passing": "Chuyền ngắn", "attacking_volleys": "Vô-lê", "skill_dribbling": "Rê bóng kỹ thuật",
        "skill_curve": "Sút xoáy", "skill_fk_accuracy": "Đá phạt", "skill_long_passing": "Chuyền dài",
        "skill_ball_control": "Kiểm soát bóng", "movement_acceleration": "Tăng tốc", "movement_sprint_speed": "Tốc độ chạy",
        "movement_agility": "Khéo léo", "movement_reactions": "Phản ứng", "movement_balance": "Thăng bằng",
        "power_shot_power": "Lực sút", "power_jumping": "Bật nhảy", "power_stamina": "Thể lực",
        "power_strength": "Sức mạnh", "power_long_shots": "Sút xa", "mentality_aggression": "Quyết liệt",
        "mentality_interceptions": "Cắt bóng", "mentality_positioning": "Chọn vị trí", "mentality_vision": "Tầm nhìn",
        "mentality_penalties": "Penalty", "mentality_composure": "Bình tĩnh", "defending_awareness": "Phòng ngự",
        "defending_standing_tackle": "Xoạc đứng", "defending_sliding_tackle": "Xoạc trượt",
    }
    for key, value in player.items():
        if key in label_map:
            try: attributes.append((label_map[key], int(round(float(value)))))
            except (TypeError, ValueError): pass
    axes_keys = [("Tốc độ", "pace"), ("Dứt điểm", "shooting"), ("Chuyền", "passing"),
                 ("Rê bóng", "dribbling"), ("Phòng ngự", "defending"), ("Thể chất", "physicality")]
    if position == "GK":
        axes_keys = [("Bay người", "goalkeeping_diving"), ("Bắt bóng", "goalkeeping_handling"),
                     ("Phát bóng", "goalkeeping_kicking"), ("Chọn vị trí", "goalkeeping_positioning"),
                     ("Phản xạ", "goalkeeping_reflexes"), ("Thể chất", "physicality")]
    axes = []
    for label, key in axes_keys:
        try: value = float(player.get(key, ovr))
        except (TypeError, ValueError): value = ovr
        axes.append((label, int(round(max(1.0, min(99.0, value))))))

    # Keep the original source rows for the distinct-club count even though the
    # profile table intentionally renders only one season line. The compact
    # season archive is authoritative for that season's totals; the career
    # projection may still contain the player's previous club from a transfer.
    career_sources = _archive_rows(state, pid)
    career_sources.extend((state.get("player_career", {}) or {}).get(pid, []))
    career_clubs = sorted({str(row.get("club") or "").strip()
                           for row in career_sources
                           if str(row.get("club") or "").strip() not in {"", "—"}})
    career = _dedupe_career_rows(career_sources)
    current_season = int(state.get("season", 0) or 0)
    if stat:
        current_row = {"season": current_season, "club": club, "player": _name(player, pid),
                       "position": position, "apps": int(stat.get("apps", 0) or 0),
                       "starts": int(stat.get("starts", stat.get("apps", 0)) or 0),
                       "sub_apps": int(stat.get("sub_apps", 0) or 0), "minutes": int(stat.get("mins", 0) or 0),
                       "goals": int(stat.get("goals", 0) or 0), "assists": int(stat.get("assists", 0) or 0),
                       "clean_sheets": int(stat.get("clean_sheets", 0) or 0), "yellow": int(stat.get("yellow", 0) or 0),
                       "red": int(stat.get("red", 0) or 0),
                       "avg_rating": _avg(stat.get("rating_sum", 0), stat.get("rating_count", 0))}
        career = [row for row in career if int(row.get("season", -1) or -1) != current_season]
        career.append(current_row)
    career.sort(key=lambda row: (int(row.get("season", 0) or 0), str(row.get("club", ""))))
    totals = {key: int(round(sum(float(row.get(key, 0) or 0) for row in career)))
              for key in ("apps", "starts", "sub_apps", "minutes", "goals", "assists", "clean_sheets", "yellow", "red")}
    ratings = [(float(row["avg_rating"]), int(row.get("apps", 1) or 1)) for row in career if row.get("avg_rating") is not None]
    rating_count = sum(count for _, count in ratings)
    totals["avg_rating"] = _avg(sum(value * count for value, count in ratings), rating_count)
    individual_awards, team_awards = _award_rows(state, pid)
    development_timeline = []
    for archive in state.get("history", []) or []:
        for row in archive.get("development", []) or []:
            if str(row.get("player_id", "")) == pid:
                development_timeline.append({"season": archive.get("season"), "ovr": row.get("new_ovr"),
                                             "old_ovr": row.get("old_ovr"), "delta": row.get("delta"),
                                             "potential": row.get("potential"), "club": row.get("team"),
                                             "team": row.get("team"), "development_model": row.get("development_model"),
                                             "attribute_changes": row.get("attribute_changes", [])})
    for row in state.get("last_development", []) or []:
        if str(row.get("player_id", "")) == pid:
            development_timeline.append({"season": current_season, "ovr": row.get("new_ovr"),
                                         "old_ovr": row.get("old_ovr"), "delta": row.get("delta"),
                                         "potential": row.get("potential"), "club": row.get("team"),
                                         "team": row.get("team"), "development_model": row.get("development_model"),
                                         "attribute_changes": row.get("attribute_changes", []), "_latest": True})
    development_timeline = _dedupe_development_rows(development_timeline)
    ovr_values = [float(row["ovr"]) for row in development_timeline if row.get("ovr") is not None]
    potential_values = [float(row["potential"]) for row in development_timeline if row.get("potential") is not None]
    damage = medical.get("permanent_damage", {}) or {}
    attribute_changes = []
    for row in development_timeline:
        for change in row.get("attribute_changes", []) or []:
            if isinstance(change, dict):
                attribute_changes.append(dict(change, season=row.get("season"), club=row.get("club")))
    injury_history = []
    for record in medical_rows:
        try:
            from datetime import date
            start = date.fromisoformat(str(record.get("start_date")))
            recurrence = sum(1 for other in medical_rows
                             if other is not record and other.get("start_date") and
                             abs((start - date.fromisoformat(str(other.get("start_date")))).days) <= 365)
        except (TypeError, ValueError): recurrence = 0
        injury_history.append({"date": record.get("start_date", "—"),
                               "name": record.get("injury_name", "Chấn thương"),
                               "severity_label": record.get("severity", "—"),
                               "matches": record.get("missed_matches_estimate", 0),
                               "recovery_days_total": record.get("duration_days", "—"),
                               "recurrence_12m": recurrence,
                               "status": "Đang hồi phục" if record.get("status") == "active" else "Đã hồi phục"})
    development = {"timeline": development_timeline,
                   "ovr_change_total": (ovr_values[-1] - ovr_values[0]) if len(ovr_values) > 1 else 0.0,
                   "best_ovr": max(ovr_values) if ovr_values else ovr,
                   "best_potential": max(potential_values) if potential_values else potential,
                   "attribute_change_count": len(attribute_changes),
                   "attribute_changes": attribute_changes,
                   "history_note": "Các mùa lưu trữ chỉ hiển thị những mốc phát triển có dữ liệu gốc; không nội suy thuộc tính còn thiếu.",
                   "awards": individual_awards, "team_awards": team_awards,
                   "medical": {"injury_history": injury_history, "permanent_damage": damage},
                   "injuries": medical_rows, "permanent_damage": damage}

    transfer_rows = []
    for row in state.get("transfer_history", []) or []:
        if str(row.get("player_id", "")) != pid: continue
        transfer_rows.append({"date": row.get("date", row.get("transfer_date", "—")),
                              "from": row.get("from_club", row.get("from_team", row.get("from", "—"))),
                              "to": row.get("to_club", row.get("to_team", row.get("to", "—"))),
                              "fee": float(row.get("fee", row.get("fee_m_eur", 0)) or 0),
                              "status": row.get("status", "Hoàn tất"),
                              "wage": row.get("wage_per_year"), "years": row.get("years", row.get("contract_years"))})
    competition_totals = {}
    for match in state.get("matches", []) or []:
        if not match.get("played"):
            continue
        for side, team_key, stats_key in (("h", "home", "minutes_h"), ("a", "away", "minutes_a")):
            minutes_map = match.get(stats_key, {}) or {}
            if pid not in minutes_map:
                continue
            team_name = str(match.get(team_key, club) or club or "—")
            comp_name = str(match.get("comp", "—") or "—")
            key = (current_season, team_name, comp_name)
            row = competition_totals.setdefault(key, {
                "season": current_season, "club": team_name, "competition": comp_name,
                "apps": 0, "minutes": 0, "goals": 0, "assists": 0, "shots": 0,
                "shots_on_target": 0, "xg": 0.0, "key_passes": 0, "tackles": 0,
                "interceptions": 0, "saves": 0, "_rating_sum": 0.0, "_rating_count": 0,
            })
            ps = ((match.get("player_stats", {}) or {}).get(side, {}) or {}).get(pid, {}) or {}
            row["apps"] += 1
            row["minutes"] += int(minutes_map.get(pid, 0) or 0)
            for target, source in (("goals", "g"), ("assists", "a"), ("shots", "sh"),
                                   ("shots_on_target", "sot"), ("key_passes", "kp"),
                                   ("tackles", "tk"), ("interceptions", "int"), ("saves", "sv")):
                row[target] += int(ps.get(source, 0) or 0)
            try:
                row["xg"] += float(ps.get("xg", 0) or 0)
            except (TypeError, ValueError):
                pass
            if ps.get("rt") is not None:
                try:
                    row["_rating_sum"] += float(ps["rt"])
                    row["_rating_count"] += 1
                except (TypeError, ValueError):
                    pass
    competition_rows = []
    for row in competition_totals.values():
        count = row.pop("_rating_count")
        rating_sum = row.pop("_rating_sum")
        row["avg_rating"] = round(rating_sum / count, 2) if count else None
        row["xg"] = int(round(row["xg"]))
        competition_rows.append(row)
    competition_rows.sort(key=lambda row: (row["season"], row["club"], row["competition"]))

    bio = {"name": _name(player, pid), "position": position, "group_label": group, "age": age,
           "nationality": player.get("fifa_country", player.get("nationality")),
           "foot": player.get("preferred_foot"), "height": player.get("height_cm"), "weight": player.get("weight_kg"),
           "birthdate": player.get("birthdate"), "shirt": player.get("shirt_number", player.get("shirt")),
           "weak_foot": player.get("weak_foot"), "skill_moves": player.get("skill_moves"),
           "alternate_positions": player.get("alternate_positions"), "club": club,
           "playstyles": player.get("playstyles", ""), "ovr": ovr, "potential": potential,
           "superstar": ovr >= 90, "unavailable": unavailable, "market_value": float(player.get("market_value", 0) or 0),
           "wage": float(player.get("wage", contract.get("wage_per_year", 0)) or 0),
           "contract_years": contract.get("years_remaining"), "role": contract.get("squad_role", ""),
           "form": form, "fitness": fitness, "medical": medical}
    season = {"label": season_label, "apps": int(stat.get("apps", 0) or 0),
              "starts": int(stat.get("starts", stat.get("apps", 0)) or 0),
              "sub_apps": int(stat.get("sub_apps", 0) or 0), "minutes": int(stat.get("mins", 0) or 0),
              "goals": int(stat.get("goals", 0) or 0), "assists": int(stat.get("assists", 0) or 0),
              "clean_sheets": int(stat.get("clean_sheets", 0) or 0),
              "yellow": int(stat.get("yellow", 0) or 0), "red": int(stat.get("red", 0) or 0),
              "avg_rating": _avg(stat.get("rating_sum", 0), stat.get("rating_count", 0))}
    axes_approximate = not all(key in player for _label, key in axes_keys)
    return {"bio": bio, "season": season, "axes": axes, "axes_approximate": axes_approximate,
            "attributes": sorted(attributes, key=lambda row: row[0]), "career": career,
            "career_totals": totals, "career_clubs": career_clubs,
            "career_competitions": competition_rows, "transfers": transfer_rows,
            "development": development}
