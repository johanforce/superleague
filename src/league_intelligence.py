# -*- coding: utf-8 -*-
"""Read-only Monte Carlo projections for the current Super League season."""
from __future__ import annotations

import math
import random
from collections import defaultdict


def _poisson(rng: random.Random, lam: float) -> int:
    limit = math.exp(-max(0.01, lam))
    product = 1.0
    count = 0
    while product > limit:
        count += 1
        product *= rng.random()
    return count - 1


def _table(stats: dict, teams: list[str]) -> list[dict]:
    rows = []
    for team in teams:
        row = stats[team]
        rows.append({"team": team, "points": row["points"], "gd": row["gf"] - row["ga"],
                     "gf": row["gf"], "ga": row["ga"], "wins": row["wins"], "draws": row["draws"]})
    return sorted(rows, key=lambda r: (-r["points"], -r["gd"], -r["gf"], r["team"]))


def forecast_season(game, simulations: int = 1200) -> dict:
    """Forecast remaining league fixtures without changing career state, RNG or saves.

    The simulator uses current saved results as fixed facts and samples only future
    domestic league fixtures. Cup points already earned are retained for C1 ranking.
    """
    simulations = max(1, min(10000, int(simulations)))
    state = game.s
    teams = list(state.get("rosters", {}).keys())
    tiers = dict(state.get("season_tiers", state.get("team_tiers", {})))
    if not teams:
        return {"season": state.get("season"), "simulations": simulations, "seed": 0, "teams": [],
                "note": "Không có dữ liệu CLB để dự báo."}

    seed = (int(game.seed) ^ (int(state.get("season", 0)) * 1_000_003) ^
            (len(state.get("matches", [])) * 97_409) ^ simulations) & 0xFFFFFFFF
    rng = random.Random(seed)
    base_stats = {team: {"points": 0, "gf": 0, "ga": 0, "wins": 0, "draws": 0, "played": 0}
                  for team in teams}
    pending = []
    # TIER_LABELS is a module constant in sl_game; use the public game config mapping.
    from sl_game import TIER_LABELS
    competition_by_tier = dict(TIER_LABELS)

    for match in state.get("matches", []) or []:
        tier = next((key for key, label in competition_by_tier.items() if match.get("comp") == label), None)
        if tier is None or match.get("home") not in base_stats or match.get("away") not in base_stats:
            continue
        if not match.get("played"):
            continue
        home, away = match["home"], match["away"]
        hg, ag = int(match.get("hg", 0) or 0), int(match.get("ag", 0) or 0)
        for team, gf, ga in ((home, hg, ag), (away, ag, hg)):
            row = base_stats[team]
            row["gf"] += gf; row["ga"] += ga; row["played"] += 1
            if gf > ga: row["points"] += 3; row["wins"] += 1
            elif gf == ga: row["points"] += 1; row["draws"] += 1

    # The calendar already knows future league fixtures. Project only domestic league slots;
    # no cup/C1 knockout outcome is invented by this forecast.
    for slot in state.get("slots", []) or []:
        if slot.get("played") or slot.get("kind") not in {"league", "league_phase2"}:
            continue
        try:
            specs = game.slot_fixture_specs(slot, include_played=False, allow_future=True)
        except Exception:
            specs = []
        for spec in specs:
            tier = next((key for key, label in competition_by_tier.items() if spec.get("comp") == label), None)
            if tier and spec.get("home") in base_stats and spec.get("away") in base_stats:
                pending.append((tier, spec["home"], spec["away"]))

    # De-duplicate any fixtures surfaced both in a legacy match list and the slot calendar.
    seen = set()
    unique_pending = []
    for tier, home, away in pending:
        key = (tier, home, away)
        if key not in seen:
            seen.add(key); unique_pending.append(key)
    pending = unique_pending

    elo = state.get("elo", {}) or {}
    cup_points = game._cup_points() if hasattr(game, "_cup_points") else {}
    previous = state.get("previous_total_points", {}) or {}
    history_weight = float(game.config.get("points", {}).get("history_weight", 0.0) or 0.0)
    counts = {team: {"champion": 0, "direct": 0, "playoff": 0, "relegation": 0,
                     "c1_direct": 0, "c1_playin": 0, "c1_rank_sum": 0.0} for team in teams}
    promotion_places = max(1, int(game.config.get("teams", {}).get("promotions", 2) or 2))

    for _ in range(simulations):
        stats = {team: dict(row) for team, row in base_stats.items()}
        for tier, home, away in pending:
            diff = float(elo.get(home, 1200)) - float(elo.get(away, 1200))
            base_xg = float(game.config.get("engine", {}).get("base_xg", 1.22) or 1.22)
            home_xg = max(0.18, min(3.2, base_xg * math.exp(max(-1.0, min(1.0, diff / 650.0)) + 0.10)))
            away_xg = max(0.18, min(3.2, base_xg * math.exp(max(-1.0, min(1.0, -diff / 650.0)) - 0.10)))
            hg, ag = _poisson(rng, home_xg), _poisson(rng, away_xg)
            for team, gf, ga in ((home, hg, ag), (away, ag, hg)):
                row = stats[team]
                row["gf"] += gf; row["ga"] += ga; row["played"] += 1
                if gf > ga: row["points"] += 3; row["wins"] += 1
                elif gf == ga: row["points"] += 1; row["draws"] += 1

        tables = {}
        positions = {}
        for tier in ("premier", "fc1", "fc2"):
            members = [team for team in teams if tiers.get(team) == tier]
            table = _table(stats, members)
            tables[tier] = table
            positions.update({row["team"]: index for index, row in enumerate(table, 1)})
            if table:
                counts[table[0]["team"]]["champion"] += 1
            if tier in ("fc1", "fc2"):
                if table: counts[table[0]["team"]]["direct"] += 1
                if len(table) > 1: counts[table[1]["team"]]["playoff"] += 1
            if tier in ("premier", "fc1"):
                for row in table[-promotion_places:]:
                    counts[row["team"]]["relegation"] += 1

        qualification = []
        for team in teams:
            tier = tiers.get(team, "fc2")
            pos = positions.get(team, len(teams))
            try: league_pts = float(game._league_points(tier, pos, team))
            except Exception: league_pts = float(stats[team]["points"])
            total = league_pts + float(cup_points.get(team, 0) or 0) + history_weight * float(previous.get(team, 0) or 0)
            qualification.append((team, total, league_pts, stats[team]["gf"] - stats[team]["ga"], float(elo.get(team, 1200))))
        qualification.sort(key=lambda row: (-row[1], -row[2], -row[3], -row[4], row[0]))
        for rank, row in enumerate(qualification, 1):
            team = row[0]
            counts[team]["c1_rank_sum"] += rank
            if rank <= 4: counts[team]["c1_direct"] += 1
            elif rank <= 12: counts[team]["c1_playin"] += 1

    rows = []
    for team in teams:
        values = counts[team]
        tier = tiers.get(team, "fc2")
        rows.append({
            "team": team, "tier": tier,
            "champion_probability": 100.0 * values["champion"] / simulations,
            "direct_promotion_probability": 100.0 * values["direct"] / simulations,
            "playoff_promotion_probability": 100.0 * values["playoff"] / simulations,
            "relegation_probability": 100.0 * values["relegation"] / simulations,
            "c1_direct_probability": 100.0 * values["c1_direct"] / simulations,
            "c1_playin_probability": 100.0 * values["c1_playin"] / simulations,
            "average_c1_rank": values["c1_rank_sum"] / simulations,
            "clinching": "Chưa chắc chắn",
        })
    rows.sort(key=lambda row: (row["tier"], -row["champion_probability"], row["team"]))
    return {"season": state.get("season"), "simulations": simulations, "seed": seed,
            "teams": rows,
            "note": f"Đã mô phỏng {len(pending)} trận league còn lại; kết quả hiện tại được giữ cố định. "
                    "Cup/C1 chưa đá không được giả lập."}
