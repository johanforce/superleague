# -*- coding: utf-8 -*-
"""Read-only career development and honours analytics for player profiles."""
from __future__ import annotations

from typing import Any

from .player_health import player_medical_summary


_ATTRIBUTE_LABELS = {
    "pace": "Tốc độ", "shooting": "Dứt điểm", "passing": "Chuyền", "dribbling": "Rê bóng",
    "defending": "Phòng ngự", "physicality": "Thể chất", "attacking_crossing": "Tạt bóng",
    "attacking_finishing": "Dứt điểm", "attacking_heading_accuracy": "Đánh đầu",
    "attacking_short_passing": "Chuyền ngắn", "attacking_volleys": "Vô-lê",
    "skill_dribbling": "Rê bóng kỹ thuật", "skill_curve": "Sút xoáy",
    "skill_fk_accuracy": "Đá phạt", "skill_long_passing": "Chuyền dài",
    "skill_ball_control": "Kiểm soát bóng", "movement_acceleration": "Tăng tốc",
    "movement_sprint_speed": "Tốc độ chạy", "movement_agility": "Nhanh nhẹn",
    "movement_reactions": "Phản ứng", "movement_balance": "Thăng bằng",
    "power_shot_power": "Lực sút", "power_jumping": "Sức bật", "power_stamina": "Thể lực",
    "power_strength": "Sức mạnh", "power_long_shots": "Sút xa",
    "mentality_aggression": "Quyết đoán", "mentality_interceptions": "Cắt bóng",
    "mentality_positioning": "Chọn vị trí", "mentality_vision": "Tầm nhìn",
    "mentality_penalties": "Penalty", "mentality_composure": "Bình tĩnh",
    "defending_awareness": "Kèm người", "defending_standing_tackle": "Xoạc đứng",
    "defending_sliding_tackle": "Xoạc bóng", "goalkeeping_diving": "Bay người",
    "goalkeeping_handling": "Bắt bóng", "goalkeeping_kicking": "Phát bóng",
    "goalkeeping_positioning": "Chọn vị trí", "goalkeeping_reflexes": "Phản xạ",
}


def _number(value: Any, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _record(row: dict, season: int, label: str) -> dict:
    season_label = label or (f"{season}/{season + 1}" if season else "—")
    changes = []
    for raw_change in row.get("attribute_changes", []) or []:
        if not isinstance(raw_change, dict):
            continue
        key = str(raw_change.get("attribute") or raw_change.get("key") or "")
        if not key:
            continue
        old = _number(raw_change.get("old"))
        new = _number(raw_change.get("new"))
        delta = _number(raw_change.get("delta"), (new - old) if old is not None and new is not None else None)
        changes.append({
            "season_no": season, "season": season_label,
            "club": row.get("team") or row.get("club") or "—",
            "attribute": key, "label": _ATTRIBUTE_LABELS.get(key, key.replace("_", " ").title()),
            "old": old, "new": new, "delta": delta,
        })
    return {
        "season_no": season,
        "season": season_label,
        "club": row.get("team") or row.get("club") or "—",
        "old_ovr": _number(row.get("old_ovr")),
        "ovr": _number(row.get("new_ovr", row.get("ovr"))),
        "delta": _number(row.get("delta")),
        "age": _number(row.get("age")),
        "age_end": _number(row.get("age_end")),
        "potential_before": _number(row.get("potential_before")),
        "potential": _number(row.get("potential")),
        "minutes": int(_number(row.get("minutes"), 0) or 0),
        "avg_rating": _number(row.get("avg_rating")),
        "injury_matches": int(_number(row.get("injury_matches"), 0) or 0),
        "injury_days": int(_number(row.get("injury_days"), 0) or 0),
        "training_days": int(_number(row.get("training_days"), 0) or 0),
        "average_morale": _number(row.get("average_morale")),
        "training_focus": row.get("training_focus"),
        "suspension_matches": int(_number(row.get("suspension_matches"), 0) or 0),
        "attribute_changes": changes,
        "development_model": dict(row.get("development_model") or {}),
    }


def build_player_development(state: dict, player_id: str) -> dict:
    """Build historical OVR progression and awards without mutating state.

    Older seasons without a development row are deliberately omitted rather than
    interpolated. Current injury/suspension state is returned separately because
    the compact season archive does not retain a reliable historical injury log.
    """
    pid = str(player_id)
    rows = []
    seen = set()
    history = list(state.get("history", []) or [])
    history.sort(key=lambda item: int(item.get("season", 0) or 0))
    for archive in history:
        season = int(archive.get("season", 0) or 0)
        label = archive.get("label") or (f"{season}/{season + 1}" if season else "—")
        for raw in archive.get("development", []) or []:
            if not isinstance(raw, dict) or str(raw.get("player_id", "")) != pid:
                continue
            item = _record(raw, season, label)
            key = (season, item["club"])
            if key not in seen:
                rows.append(item)
                seen.add(key)
    current_season = int(state.get("season", 0) or 0)
    for raw in state.get("last_development", []) or []:
        if not isinstance(raw, dict) or str(raw.get("player_id", "")) != pid:
            continue
        item = _record(raw, current_season, f"{current_season}/{current_season + 1}" if current_season else "—")
        key = (current_season, item["club"])
        if key not in seen:
            rows.append(item)
            seen.add(key)
    rows.sort(key=lambda item: (item["season_no"], item["club"]))
    attribute_changes = [change for item in rows for change in item.get("attribute_changes", [])]
    attribute_changes.sort(key=lambda item: (item["season_no"], item["club"], item["label"]))
    attribute_totals = {}
    for change in attribute_changes:
        key = change["attribute"]
        item = attribute_totals.setdefault(key, {
            "attribute": key, "label": change["label"], "delta": 0,
            "gains": 0, "losses": 0, "events": 0,
        })
        delta = _number(change.get("delta"), 0.0) or 0.0
        item["delta"] += delta
        item["gains"] += max(0.0, delta)
        item["losses"] += min(0.0, delta)
        item["events"] += 1
    attribute_totals = sorted(attribute_totals.values(), key=lambda item: (-abs(item["delta"]), item["label"]))
    for item in attribute_totals:
        item["delta"] = round(item["delta"], 1)
        item["gains"] = round(item["gains"], 1)
        item["losses"] = round(item["losses"], 1)

    awards = []
    award_seen = set()
    team_awards = []
    team_award_seen = set()

    def add_team_award(archive, season, label, row):
        if not isinstance(row, dict) or str(row.get("player_id") or "") != pid:
            return
        club = str(row.get("club") or "—")
        award_id = str(row.get("award_id") or "team_honor")
        competition = str(row.get("competition") or "—")
        token = (season, award_id, competition, club)
        if token in team_award_seen:
            return
        team_award_seen.add(token)
        team_awards.append({
            "season": str(archive.get("label") or label), "season_no": season,
            "title": str(row.get("title") or ("Á quân" if row.get("achievement") == "runner_up" else "Vô địch")),
            "award_id": award_id, "achievement": str(row.get("achievement") or "champion"),
            "competition": competition, "club": club,
            "rank": row.get("rank"),
            "appearances": int(_number(row.get("appearances"), 0) or 0),
            "minutes": int(_number(row.get("minutes"), 0) or 0),
        })

    def add_award(archive, season, label, row, award_id, title, category,
                  competition="Toàn mùa", rank=None, value=None, unit="", detail=""):
        if not isinstance(row, dict) or str(row.get("player_id") or "") != pid:
            return
        club = str(row.get("club") or row.get("team") or "—")
        token = (season, str(award_id), str(competition), club)
        if token in award_seen:
            return
        award_seen.add(token)
        awards.append({
            "season": str(archive.get("label") or label), "season_no": season,
            "title": str(title), "award_id": str(award_id),
            "category": str(category), "competition": str(competition or "Toàn mùa"),
            "club": club, "rank": rank if rank is not None else row.get("rank"),
            "value": value if value is not None else row.get("value"),
            "unit": str(unit or row.get("unit") or ""),
            "detail": str(detail or row.get("detail") or row.get("role") or row.get("position") or ""),
        })

    for archive in history:
        season = int(archive.get("season", 0) or 0)
        label = archive.get("label") or (f"{season}/{season + 1}" if season else "—")
        canonical_team = archive.get("team_awards")
        if isinstance(canonical_team, list) and int(archive.get("team_awards_version", 0) or 0) >= 1:
            for item in canonical_team:
                add_team_award(archive, season, label, item)
        canonical = archive.get("individual_awards")
        if isinstance(canonical, list) and int(archive.get("individual_awards_version", 0) or 0) >= 1:
            for item in canonical:
                if not isinstance(item, dict):
                    continue
                add_award(
                    archive, season, label, item, item.get("award_id") or "individual_award",
                    item.get("title") or "Danh hiệu cá nhân", item.get("category") or "season",
                    competition=item.get("competition") or "Toàn mùa", rank=item.get("rank"),
                    value=item.get("value"), unit=item.get("unit"), detail=item.get("detail"),
                )
            # The versioned canonical ledger is authoritative, including an empty list.
            continue

        # Backward-compatible extraction for archives created before the individual
        # awards ledger. Only facts present in the old archive are surfaced.
        annual = archive.get("season_awards") or {}
        podium = annual.get("ballon_podium") or []
        podium_titles = (("ballon_dor", "Quả bóng Vàng"),
                         ("ballon_silver", "Quả bóng Bạc"),
                         ("ballon_bronze", "Quả bóng Đồng"))
        if podium:
            for rank, winner in enumerate(podium[:3], 1):
                award_id, title = podium_titles[rank - 1]
                add_award(archive, season, label, winner, award_id, title, "season",
                          "Ballon d'Or", rank=rank)
        else:
            for rank, (key, title) in enumerate(podium_titles, 1):
                add_award(archive, season, label, annual.get(key), key, title, "season",
                          "Ballon d'Or", rank=rank)
        add_award(archive, season, label, annual.get("young_player") or annual.get("golden_boy"),
                  "golden_boy", "Golden Boy", "season")
        add_award(archive, season, label, annual.get("golden_glove"),
                  "golden_glove", "Găng tay Vàng", "season")
        for row in annual.get("best_xi_442", []) or []:
            add_award(archive, season, label, row, "best_xi", "Đội hình tiêu biểu", "season",
                      detail=row.get("role") or row.get("position") or "")

        # The legacy top_scorers/top_assists arrays represent a season-wide total.
        # They cannot recover old per-competition winners after compacting match logs.
        top_scorers = archive.get("top_scorers") or []
        if top_scorers:
            add_award(archive, season, label, top_scorers[0], "golden_boot",
                      "Chiếc giày Vàng", "season", "Tất cả giải đấu", rank=1,
                      value=top_scorers[0].get("goals"), unit="bàn")
        top_assists = archive.get("top_assists") or []
        if top_assists:
            add_award(archive, season, label, top_assists[0], "season_assist_leader",
                      "Vua kiến tạo mùa giải", "season", "Tất cả giải đấu", rank=1,
                      value=top_assists[0].get("assists"), unit="kiến tạo")

        for key, value in (archive.get("competition_awards") or {}).items():
            if not isinstance(value, dict):
                continue
            competition = value.get("competition") or key
            for row in value.get("top_scorers", []) or []:
                add_award(archive, season, label, row, f"top_scorer:{key}",
                          "Vua phá lưới", "competition", competition, rank=1,
                          value=row.get("goals"), unit="bàn")
            for row in value.get("top_assists", []) or []:
                add_award(archive, season, label, row, f"top_assist:{key}",
                          "Vua kiến tạo", "competition", competition, rank=1,
                          value=row.get("assists"), unit="kiến tạo")
            winner = value.get("player_of_tournament") or {}
            add_award(archive, season, label, winner, f"player_of_tournament:{key}",
                      f"Cầu thủ xuất sắc · {competition}", "competition", competition)
    awards.sort(key=lambda item: (item["season_no"], item["category"],
                                  item["competition"], item["title"]))
    team_awards.sort(key=lambda item: (item["season_no"], item["competition"].casefold(),
                                       item["rank"] or 9, item["club"].casefold()))
    team_award_summary = {
        "championships": sum(1 for item in team_awards if item.get("achievement") == "champion"),
        "runner_ups": sum(1 for item in team_awards if item.get("achievement") == "runner_up"),
        "competitions": sorted({item["competition"] for item in team_awards
                                 if item.get("achievement") == "champion"}),
        "clubs": sorted({item["club"] for item in team_awards if item.get("club") and item.get("club") != "—"}),
    }
    deltas = [item["delta"] for item in rows if item["delta"] is not None]
    status = (state.get("player_status", {}) or {}).get(pid, {}) or {}
    medical = player_medical_summary(state, pid)
    return {
        "timeline": rows,
        "attribute_changes": attribute_changes,
        "attribute_totals": attribute_totals,
        "attribute_change_count": len(attribute_changes),
        "attribute_gain_total": sum(max(0.0, _number(item.get("delta"), 0.0) or 0.0) for item in attribute_changes),
        "attribute_loss_total": sum(min(0.0, _number(item.get("delta"), 0.0) or 0.0) for item in attribute_changes),
        "awards": awards,
        "team_awards": team_awards,
        "team_award_summary": team_award_summary,
        "ovr_change_total": sum(deltas) if deltas else None,
        "best_ovr": max((item["ovr"] for item in rows if item["ovr"] is not None), default=None),
        "best_potential": max((item["potential"] for item in rows if item["potential"] is not None), default=None),
        "current_unavailability": {
            "injury_matches": int(_number(status.get("inj"), 0) or 0),
            "suspension_matches": int(_number(status.get("susp"), 0) or 0),
            "days_to_recovery": int(medical.get("days_to_recovery", 0) or 0),
        },
        "medical": medical,
        "history_note": (
            "Mô hình 5.8 ghi nhận trực tiếp từng thuộc tính và các yếu tố tuổi, Potential, phút thi đấu, "
            "điểm trung bình, ngày chấn thương, ngày tập luyện, tinh thần và hồ sơ phục hồi. Mùa cũ không có log thuộc tính "
            "được để trống, không nội suy."
        ),
    }
