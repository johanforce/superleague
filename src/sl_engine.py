# -*- coding: utf-8 -*-
"""Động cơ trận đấu độc lập, lấy ý tưởng từ WNL engine do người dùng cung cấp."""
from __future__ import annotations
import math
import random

POS_GROUP = {
    "GK": "GK",
    "CB": "DEF", "LB": "DEF", "RB": "DEF",
    "CDM": "MID", "CM": "MID", "CAM": "MID", "RM": "MID", "LM": "MID",
    "ST": "FWD", "RW": "FWD", "LW": "FWD", "CF": "FWD",
}
FORMATIONS = [(4, 3, 3), (4, 4, 2), (3, 5, 2), (4, 5, 1), (5, 3, 2)]
DEFAULT_MATCH_CFG = {
    "base_xg": 1.22, "gap_scale": 28.0, "gap_cap": 0.72,
    "home_factor": 1.055, "away_factor": 0.945, "xg_min": 0.25, "xg_max": 2.65,
    "et_xg_factor": 0.23, "shots_per_xg": 8.2, "sot_ratio_min": 0.30, "sot_ratio_max": 0.43,
    "possession_gap_weight": 1.35, "possession_noise": 2.6,
    "assist_probability": 0.76, "shootout_home_probability": 0.76, "shootout_away_probability": 0.74,
    "form_effect": 2.1, "fit_threshold": 70.0, "fit_penalty_per_point": 0.035,
    "effective_min": -3.0, "effective_max": 2.5
}


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def poisson(lam: float, rng: random.Random) -> int:
    """Poisson sampler (Knuth), sufficient for football match event rates."""
    lam = max(0.001, float(lam))
    limit = math.exp(-lam)
    p, k = 1.0, 0
    while p > limit:
        k += 1
        p *= rng.random()
    return k - 1


def to_num(row, key, default=50.0):
    try:
        return float(row.get(key, default) or default)
    except (TypeError, ValueError):
        return float(default)


def player_attack(p):
    ovr = to_num(p, "_ovr_eff", to_num(p, "overall_rating"))
    skill = (to_num(p, "attacking_finishing") * 0.32 + to_num(p, "shooting") * 0.18 +
             to_num(p, "mentality_positioning") * 0.14 + to_num(p, "mentality_composure") * 0.12 +
             to_num(p, "dribbling") * 0.10 + to_num(p, "pace") * 0.14)
    styles = str(p.get("playstyles", "")).lower()
    bonus = 1.4 * sum(x in styles for x in ("finesse shot", "power shot", "chip shot", "low driven shot", "power header", "dead ball"))
    bonus += 0.8 * sum(x in styles for x in ("rapid", "quick step", "acrobatic"))
    return 0.62 * ovr + 0.38 * skill + min(3.0, bonus)


def player_midfield(p):
    ovr = to_num(p, "_ovr_eff", to_num(p, "overall_rating"))
    skill = (to_num(p, "passing") * 0.24 + to_num(p, "attacking_short_passing") * 0.20 +
             to_num(p, "skill_long_passing") * 0.15 + to_num(p, "mentality_vision") * 0.18 +
             to_num(p, "skill_ball_control") * 0.13 + to_num(p, "dribbling") * 0.10)
    styles = str(p.get("playstyles", "")).lower()
    bonus = 1.2 * sum(x in styles for x in ("incisive pass", "pinged pass", "tiki taka", "first touch", "technical", "flair"))
    return 0.62 * ovr + 0.38 * skill + min(3.0, bonus)


def player_defence(p):
    ovr = to_num(p, "_ovr_eff", to_num(p, "overall_rating"))
    skill = (to_num(p, "defending") * 0.28 + to_num(p, "defending_awareness") * 0.22 +
             to_num(p, "defending_standing_tackle") * 0.17 + to_num(p, "mentality_interceptions") * 0.13 +
             to_num(p, "physicality") * 0.12 + to_num(p, "attacking_heading_accuracy") * 0.08)
    styles = str(p.get("playstyles", "")).lower()
    bonus = 1.0 * sum(x in styles for x in ("intercept", "anticipate", "block", "jockey", "slide tackle", "tackle", "bruiser", "aerial"))
    return 0.62 * ovr + 0.38 * skill + min(3.0, bonus)


def player_keeper(p):
    ovr = to_num(p, "_ovr_eff", to_num(p, "overall_rating"))
    skill = (to_num(p, "goalkeeping_diving") * .22 + to_num(p, "goalkeeping_handling") * .20 +
             to_num(p, "goalkeeping_reflexes") * .28 + to_num(p, "goalkeeping_positioning") * .18 +
             to_num(p, "goalkeeping_kicking") * .12)
    styles = str(p.get("playstyles", "")).lower()
    bonus = 1.2 * sum(x in styles for x in ("far reach", "footwork", "cross claimer", "rush out"))
    return 0.58 * ovr + 0.42 * skill + min(3.0, bonus)


def choose_xi(squad):
    """Chọn GK + 10 cầu thủ theo sơ đồ, ưu tiên overall và độ phù hợp vị trí."""
    if len(squad) < 11:
        raise ValueError("Mỗi đội cần ít nhất 11 cầu thủ khả dụng.")
    gks = sorted((p for p in squad if POS_GROUP.get(p.get("position"), "MID") == "GK"),
                 key=lambda p: to_num(p, "overall_rating"), reverse=True)
    gk = gks[0] if gks else max(squad, key=lambda p: player_keeper(p))
    rest = [p for p in squad if p is not gk]
    pools = {
        "DEF": sorted((p for p in rest if POS_GROUP.get(p.get("position")) == "DEF"), key=lambda p: -to_num(p, "overall_rating")),
        "MID": sorted((p for p in rest if POS_GROUP.get(p.get("position")) == "MID"), key=lambda p: -to_num(p, "overall_rating")),
        "FWD": sorted((p for p in rest if POS_GROUP.get(p.get("position")) == "FWD"), key=lambda p: -to_num(p, "overall_rating")),
    }
    best_xi, best_score, best_formation = None, -1e9, FORMATIONS[0]
    for formation in FORMATIONS:
        need = dict(zip(("DEF", "MID", "FWD"), formation))
        chosen = []
        for group in ("DEF", "MID", "FWD"):
            chosen.extend(pools[group][:need[group]])
        if len(chosen) < 10:
            selected_ids = {p["player_id"] for p in chosen}
            extras = sorted((p for p in rest if p["player_id"] not in selected_ids),
                            key=lambda p: -to_num(p, "overall_rating"))[:10-len(chosen)]
            chosen.extend(extras)
        if len(chosen) >= 10:
            chosen = chosen[:10]
            score = sum(to_num(p, "overall_rating") for p in chosen)
            # Chút ưu tiên đội hình có chiều rộng và tiền đạo tốt.
            score += sum(player_attack(p) for p in chosen if POS_GROUP.get(p.get("position")) == "FWD") * .025
            if score > best_score:
                best_xi, best_score, best_formation = chosen, score, formation
    return [gk] + best_xi, best_formation


def team_metrics(squad, dyn=None, engine_cfg=None):
    dyn = dyn or {}
    cfg = {**DEFAULT_MATCH_CFG, **(engine_cfg or {})}
    adjusted = []
    for p in squad:
        q = dict(p)
        d = dyn.get(p["player_id"], {})
        form = float(d.get("form", 0.0))
        fit = float(d.get("fit", 100.0))
        # Giới hạn tác động phong độ/thể lực để chỉ số trong trận chỉ đổi nhẹ.
        bump = clamp(form * cfg["form_effect"] - max(0, cfg["fit_threshold"] - fit) * cfg["fit_penalty_per_point"],
                     cfg["effective_min"], cfg["effective_max"])
        q["_ovr_eff"] = to_num(p, "overall_rating") + bump
        adjusted.append(q)
    xi, formation = choose_xi(adjusted)
    gk = xi[0]
    defs = [p for p in xi if POS_GROUP.get(p.get("position")) == "DEF"]
    mids = [p for p in xi if POS_GROUP.get(p.get("position")) == "MID"]
    fwds = [p for p in xi if POS_GROUP.get(p.get("position")) == "FWD"]
    avg = lambda xs, fn, default=80.0: sum(fn(p) for p in xs)/len(xs) if xs else default
    atk = .53*avg(fwds, player_attack) + .31*avg(mids, player_attack) + .16*avg(defs, player_midfield)
    deff = .52*avg(defs, player_defence) + .25*avg(mids, player_defence) + .23*player_keeper(gk)
    control = .50*avg(mids, player_midfield) + .30*avg(defs, player_midfield) + .20*avg(fwds, player_midfield)
    return {"xi": xi, "formation": formation, "attack": atk, "defence": deff,
            "control": control, "gk": gk}


def _pick_weighted(candidates, weights, rng):
    if not candidates:
        return None
    total = sum(max(.01, w) for w in weights)
    target = rng.random() * total
    cur = 0.0
    for candidate, weight in zip(candidates, weights):
        cur += max(.01, weight)
        if cur >= target:
            return candidate
    return candidates[-1]


def _scorer(xi, rng):
    outfield = xi[1:]
    weights = []
    for p in outfield:
        group = POS_GROUP.get(p.get("position"), "MID")
        posw = {"FWD": 1.55, "MID": .65, "DEF": .20}.get(group, .5)
        weights.append(posw * max(0.3, player_attack(p) / 80))
    return _pick_weighted(outfield, weights, rng)


def _assister(xi, scorer, rng):
    candidates = [p for p in xi[1:] if p["player_id"] != scorer["player_id"]]
    weights = [max(1.0, .5*to_num(p, "passing") + .3*to_num(p, "mentality_vision") + .2*to_num(p, "attacking_short_passing")) for p in candidates]
    return _pick_weighted(candidates, weights, rng)


def _shootout(rng, home_probability=0.76, away_probability=0.74):
    h, a = 0, 0
    # Mỗi đội sút tối đa 5 quả; độ chính xác cố định có nhiễu nhỏ.
    for _ in range(5):
        h += int(rng.random() < home_probability)
        a += int(rng.random() < away_probability)
    kicks = 5
    while h == a and kicks < 12:
        h += int(rng.random() < home_probability)
        a += int(rng.random() < away_probability)
        kicks += 1
    if h == a:
        if rng.random() < .5: h += 1
        else: a += 1
    return {"home": h, "away": a}


def simulate_match(home_name, away_name, home_squad, away_squad, rng=None,
                   neutral=False, force_winner=False, aggregate_before=None,
                   home_dyn=None, away_dyn=None, engine_cfg=None):
    """Mô phỏng 90 phút, tuỳ chọn hiệp phụ/luân lưu cho trận loại trực tiếp."""
    rng = rng or random.Random()
    cfg = {**DEFAULT_MATCH_CFG, **(engine_cfg or {})}
    H = team_metrics(home_squad, home_dyn, cfg)
    A = team_metrics(away_squad, away_dyn, cfg)
    gap_h = clamp((H["attack"] - A["defence"]) / cfg["gap_scale"], -cfg["gap_cap"], cfg["gap_cap"])
    gap_a = clamp((A["attack"] - H["defence"]) / cfg["gap_scale"], -cfg["gap_cap"], cfg["gap_cap"])
    home_factor, away_factor = (1.0, 1.0) if neutral else (cfg["home_factor"], cfg["away_factor"])
    xgh = cfg["base_xg"] * math.exp(gap_h) * home_factor
    xga = cfg["base_xg"] * math.exp(gap_a) * away_factor
    # Chặn đầu xa để đội chênh lệch cực lớn vẫn có thể tạo bất ngờ.
    xgh, xga = clamp(xgh, cfg["xg_min"], cfg["xg_max"]), clamp(xga, cfg["xg_min"], cfg["xg_max"])
    hg, ag = poisson(xgh, rng), poisson(xga, rng)
    goals = []

    def add_goals(count, side, lo, hi):
        xi = H["xi"] if side == "home" else A["xi"]
        team = home_name if side == "home" else away_name
        for _ in range(count):
            scorer = _scorer(xi, rng)
            assister = _assister(xi, scorer, rng) if rng.random() < cfg["assist_probability"] else None
            goals.append({"minute": rng.randint(lo, hi), "team": team, "side": side,
                          "scorer_id": scorer["player_id"], "scorer": scorer.get("common_name") or scorer.get("first_name", "") + " " + scorer.get("last_name", ""),
                          "assist_id": assister["player_id"] if assister else None,
                          "assist": (assister.get("common_name") or (assister.get("first_name", "") + " " + assister.get("last_name", ""))) if assister else None,
                          "period": "ET" if lo > 90 else "90m"})

    add_goals(hg, "home", 1, 90)
    add_goals(ag, "away", 1, 90)
    et = False
    pens = None
    base_aggregate = aggregate_before or (0, 0)
    if force_winner and (hg + base_aggregate[0]) == (ag + base_aggregate[1]):
        et = True
        eh, ea = poisson(xgh * cfg["et_xg_factor"], rng), poisson(xga * cfg["et_xg_factor"], rng)
        hg += eh; ag += ea
        add_goals(eh, "home", 91, 120)
        add_goals(ea, "away", 91, 120)
        if (hg + base_aggregate[0]) == (ag + base_aggregate[1]):
            pens = _shootout(rng, cfg["shootout_home_probability"], cfg["shootout_away_probability"])
    if (hg + base_aggregate[0]) > (ag + base_aggregate[1]):
        winner = home_name
    elif (hg + base_aggregate[0]) < (ag + base_aggregate[1]):
        winner = away_name
    elif pens:
        winner = home_name if pens["home"] > pens["away"] else away_name
    else:
        winner = None

    shots_h = max(hg, poisson(max(1.0, xgh * cfg["shots_per_xg"]), rng))
    shots_a = max(ag, poisson(max(1.0, xga * cfg["shots_per_xg"]), rng))
    sot_h = min(shots_h, max(hg, round(shots_h * (cfg["sot_ratio_min"] + rng.random()*(cfg["sot_ratio_max"]-cfg["sot_ratio_min"])))))
    sot_a = min(shots_a, max(ag, round(shots_a * (cfg["sot_ratio_min"] + rng.random()*(cfg["sot_ratio_max"]-cfg["sot_ratio_min"])))))
    possession_h = int(clamp(50 + (H["control"]-A["control"])*cfg["possession_gap_weight"] + rng.gauss(0, cfg["possession_noise"]), 32, 68))
    return {
        "home": home_name, "away": away_name, "hg": hg, "ag": ag, "et": et,
        "pens": pens, "winner": winner, "goals": sorted(goals, key=lambda g: g["minute"]),
        "xi_h": [p["player_id"] for p in H["xi"]], "xi_a": [p["player_id"] for p in A["xi"]],
        "formation_h": "%d-%d-%d" % H["formation"], "formation_a": "%d-%d-%d" % A["formation"],
        "stats": {"shots": [shots_h, shots_a], "sot": [sot_h, sot_a], "xg": [round(xgh, 2), round(xga, 2)],
                  "possession": [possession_h, 100-possession_h]},
        "ratings": {"attack": [round(H["attack"], 1), round(A["attack"], 1)],
                    "defence": [round(H["defence"], 1), round(A["defence"], 1)]}
    }
