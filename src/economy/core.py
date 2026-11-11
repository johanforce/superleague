# -*- coding: utf-8 -*-
"""Core economic services for Super League.

Money is stored in millions of euros (M€), rounded to 0.001 M€ (one thousand
EUR). All cash movements are posted through FinanceService so the ledger and
club balance cannot silently drift apart.
"""
from __future__ import annotations

import copy
import json
import math
import random
import zlib
from collections import Counter
from pathlib import Path
from typing import Any

ECONOMY_SCHEMA_VERSION = 2
MONEY_PRECISION = 3  # M€; 0.001 M€ = €1,000
VALID_TRANSACTION_TYPES = {
    "PRIZE", "MATCHDAY", "BROADCAST", "SPONSOR", "COMMERCIAL", "WAGE",
    "TRANSFER_IN", "TRANSFER_OUT", "SIGNING_BONUS", "AGENT_FEE",
    "TAMPERING_COMPENSATION", "CONTRACT_TERMINATION", "OWNER_INJECTION",
    "DEBT_REPAYMENT", "OTHER",
}

DEFAULT_ECONOMY_CONFIG: dict[str, Any] = {
    "feature_flags": {
        "ENABLE_ECONOMY": True,
        "ENABLE_TRANSFERS": False,
        "ENABLE_FFP": False,
        "ENABLE_YOUTH_INTAKE": False,
        "ENABLE_STADIUMS": False,
        "ENABLE_HOME_ADVANTAGE": False,
    },
    "tier_mapping": {"elite": "big", "middle": "medium", "standard": "small"},
    "initial_finance": {
        "variance": 0.10,
        "big": {"balance": 300.0, "transfer_budget": 110.0, "wage_budget": 230.0,
                "reputation": [85, 95], "fan_base": [5_000_000, 12_000_000]},
        "medium": {"balance": 200.0, "transfer_budget": 80.0, "wage_budget": 170.0,
                   "reputation": [60, 75], "fan_base": [2_000_000, 5_000_000]},
        "small": {"balance": 150.0, "transfer_budget": 55.0, "wage_budget": 125.0,
                  "reputation": [35, 55], "fan_base": [500_000, 2_000_000]},
    },
    "wages": {
        "base_multiplier": 0.10,
        "ovr_exponent": 0.17,
        "age_factors": {"u21": 0.85, "22_28": 1.0, "29_31": 1.05, "32_34": 0.85, "35_plus": 0.65},
        "potential_per_point": 0.015,
        "position_factors": {"GK": 0.85, "DEF": 0.95, "MID": 1.0, "FWD": 1.10},
        "club_factors": {"big": 1.10, "medium": 1.0, "small": 0.92},
        "star_factor": 1.10,
        "top_star_count": 3,
        "round_to": 0.05,
        "max_annual_wage": 35.0,
        "scale_down_threshold": 0.85,
        "scale_down_target": 0.80,
        "scale_up_threshold": 0.50,
        "max_scale_up": 1.15,
    },
    "market_value": {
        "model_version": 2,
        "base_coefficient": 0.03,
        "base_rating": 50.0,
        "attribute_delta_factor": 0.0075,
        "attribute_factor_min": 0.92,
        "attribute_factor_max": 1.08,
        "age_curve": [[16, 1.30], [18, 1.26], [20, 1.22], [22, 1.15],
                       [24, 1.06], [27, 1.00], [29, 0.90], [31, 0.76],
                       [33, 0.60], [35, 0.44], [38, 0.28], [40, 0.20]],
        "potential_premium": [
            {"max_age": 20, "per_point": 0.025, "max_bonus": 0.45},
            {"max_age": 23, "per_point": 0.020, "max_bonus": 0.30},
            {"max_age": 25, "per_point": 0.010, "max_bonus": 0.12}
        ],
        "min_value": 0.05,
        "max_value": 250.0,
    },
    "contracts": {
        "default_years": 3,
        "termination_compensation_pct": 50.0,
        "remaining_year_weights": {"1": 30, "2": 35, "3": 35},
        "uniform_initial_contract": False,
        "role_prospect_max_age": 21,
        "role_prospect_min_potential_gap": 8.0,
    },
    "squad_rules": {
        "min_size": 23, "max_size": 30,
        "minimum_groups": {"GK": 3, "DEF": 7, "MID": 7, "FWD": 4},
        "minimum_each_group": 2,
    },
    "transfer_windows": {"winter_months": [11, 12], "summer_months": [6, 7, 8],
                         "allow_free_agent_outside_window": True, "max_deals_per_window": 8},
    "prize_money_file": "prize_money.json",
    "club_finance_overrides_file": "club_finance_overrides.json",
}


def _money(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Giá trị tiền phải là số hữu hạn.")
    return round(number, MONEY_PRECISION)


def _stable_rng(seed: int, key: str) -> random.Random:
    key_seed = zlib.crc32(str(key).encode("utf-8")) & 0xFFFFFFFF
    return random.Random((int(seed) ^ key_seed) & 0xFFFFFFFF)


def load_json_object(path: Path, fallback: dict | None = None) -> dict:
    if not path.is_file():
        return copy.deepcopy(fallback or {})
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} phải là một JSON object.")
    return value


def load_economy_config(project_root: Path) -> tuple[dict, dict]:
    """Read economy settings and club overrides; return independent dict objects."""
    config_dir = Path(project_root) / "config"
    cfg = copy.deepcopy(DEFAULT_ECONOMY_CONFIG)
    supplied = load_json_object(config_dir / "economy.json", {})

    def merge(target: dict, source: dict) -> dict:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)
        return target

    merge(cfg, supplied)
    transfer_windows_file = config_dir / "transfer_windows.json"
    if transfer_windows_file.is_file():
        cfg["transfer_windows"] = load_json_object(transfer_windows_file, cfg.get("transfer_windows", {}))
    override_name = str(cfg.get("club_finance_overrides_file", "club_finance_overrides.json"))
    overrides = load_json_object(config_dir / override_name, {"clubs": {}})
    if "clubs" not in overrides or not isinstance(overrides["clubs"], dict):
        raise ValueError("club_finance_overrides.json cần object 'clubs'.")
    _validate_economy_config(cfg)
    return cfg, overrides


def _validate_economy_config(cfg: dict) -> None:
    flags = cfg.get("feature_flags", {})
    if not isinstance(flags, dict):
        raise ValueError("economy.feature_flags phải là object.")
    for tier in ("big", "medium", "small"):
        row = cfg.get("initial_finance", {}).get(tier)
        if not isinstance(row, dict):
            raise ValueError(f"Thiếu economy.initial_finance.{tier}.")
        for key in ("balance", "transfer_budget", "wage_budget"):
            if _money(row[key]) <= 0:
                raise ValueError(f"economy.initial_finance.{tier}.{key} phải > 0.")
        for key in ("reputation", "fan_base"):
            values = row[key]
            if not isinstance(values, list) or len(values) != 2 or float(values[0]) > float(values[1]):
                raise ValueError(f"economy.initial_finance.{tier}.{key} phải là [min, max].")
    rules = cfg.get("squad_rules", {})
    if int(rules.get("min_size", 0)) < 1 or int(rules.get("max_size", 0)) < int(rules.get("min_size", 0)):
        raise ValueError("economy.squad_rules min_size/max_size không hợp lệ.")
    windows = cfg.get("transfer_windows", {})
    for key in ("winter_months", "summer_months"):
        months = windows.get(key, [])
        if any(int(month) < 1 or int(month) > 12 for month in months):
            raise ValueError(f"economy.transfer_windows.{key} chỉ nhận tháng 1–12.")


def position_group(position: str) -> str:
    pos = str(position or "").strip().upper()
    if pos == "GK":
        return "GK"
    if pos in {"CB", "RB", "LB", "RWB", "LWB"}:
        return "DEF"
    if pos in {"CDM", "CM", "CAM", "RM", "LM", "DM", "AM"}:
        return "MID"
    if pos in {"ST", "CF", "RW", "LW", "RF", "LF"}:
        return "FWD"
    return "MID"


def _age_factor(age: float, factors: dict) -> float:
    if "u21" in factors and age <= 21:
        return float(factors["u21"])
    if "u20" in factors and age <= 20:
        return float(factors["u20"])
    if age <= 23 and "21_23" in factors:
        return float(factors["21_23"])
    if age <= 28 and "22_28" in factors:
        return float(factors["22_28"])
    if age <= 27:
        return float(factors.get("24_27", 1.0))
    if age <= 30:
        return float(factors.get("28_30", factors.get("29_31", 0.75)))
    if age <= 31:
        return float(factors.get("29_31", 0.75))
    if age <= 33:
        return float(factors.get("31_33", factors.get("32_34", 0.45)))
    if age <= 34:
        return float(factors.get("32_34", 0.2))
    return float(factors.get("34_plus", factors.get("35_plus", 0.2)))


_MARKET_VALUE_SKILLS = {
    "GK": ("goalkeeping_diving", "goalkeeping_handling", "goalkeeping_kicking",
           "goalkeeping_positioning", "goalkeeping_reflexes"),
    "CB": ("defending_awareness", "defending_standing_tackle", "defending_sliding_tackle",
           "mentality_interceptions", "attacking_heading_accuracy", "power_strength", "power_jumping"),
    "FB": ("defending_awareness", "defending_standing_tackle", "movement_acceleration",
           "movement_sprint_speed", "attacking_crossing", "power_stamina", "movement_agility"),
    "CDM": ("mentality_interceptions", "defending_awareness", "defending_standing_tackle",
            "attacking_short_passing", "skill_long_passing", "power_stamina", "power_strength"),
    "CM": ("attacking_short_passing", "skill_long_passing", "mentality_vision",
           "skill_ball_control", "skill_dribbling", "power_stamina", "movement_reactions"),
    "CAM": ("mentality_vision", "attacking_short_passing", "skill_dribbling",
            "skill_ball_control", "mentality_positioning", "attacking_finishing"),
    "WING": ("movement_acceleration", "movement_sprint_speed", "skill_dribbling",
             "attacking_crossing", "attacking_finishing", "mentality_vision", "movement_agility"),
    "ST": ("attacking_finishing", "mentality_positioning", "power_shot_power",
           "attacking_volleys", "attacking_heading_accuracy", "movement_sprint_speed",
           "movement_acceleration"),
}


def _market_value_skill_index(player: dict) -> float | None:
    """Average the attributes most relevant to the player's primary position."""
    position = str(player.get("position", "CM") or "CM").strip().upper()
    if position == "GK":
        keys = _MARKET_VALUE_SKILLS["GK"]
    elif position == "CB":
        keys = _MARKET_VALUE_SKILLS["CB"]
    elif position in {"RB", "LB", "RWB", "LWB"}:
        keys = _MARKET_VALUE_SKILLS["FB"]
    elif position in {"CDM", "DM"}:
        keys = _MARKET_VALUE_SKILLS["CDM"]
    elif position in {"CAM", "AM"}:
        keys = _MARKET_VALUE_SKILLS["CAM"]
    elif position in {"LW", "RW", "LM", "RM", "LF", "RF"}:
        keys = _MARKET_VALUE_SKILLS["WING"]
    elif position in {"ST", "CF"}:
        keys = _MARKET_VALUE_SKILLS["ST"]
    else:
        keys = _MARKET_VALUE_SKILLS["CM"]

    values = []
    for key in keys:
        try:
            value = float(player.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(max(1.0, min(99.0, value)))
    return sum(values) / len(values) if values else None


def _market_age_multiplier(age: float, curve: list) -> float:
    """Interpolate the age curve so a birthday never causes a sharp price cliff."""
    points = []
    for row in curve if isinstance(curve, list) else []:
        try:
            points.append((float(row[0]), float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    points.sort(key=lambda item: item[0])
    if not points:
        return 1.0
    if age <= points[0][0]:
        return points[0][1]
    if age >= points[-1][0]:
        return points[-1][1]
    for (age_a, value_a), (age_b, value_b) in zip(points, points[1:]):
        if age_a <= age <= age_b:
            ratio = (age - age_a) / max(0.001, age_b - age_a)
            return value_a + (value_b - value_a) * ratio
    return points[-1][1]


class WageService:
    """Calculate initial wages and market values from the current player model."""

    def __init__(self, config: dict):
        self.config = config
        self.wage_cfg = config.get("wages", {})
        self.value_cfg = config.get("market_value", {})

    def base_annual_wage(self, player: dict, club_tier: str, is_star: bool = False) -> float:
        ovr = max(1.0, float(player.get("overall_rating", 60) or 60))
        age = max(15.0, float(player.get("age", player.get("_age", 26)) or 26))
        potential = max(ovr, float(player.get("potential", ovr) or ovr))
        base = float(self.wage_cfg.get("base_multiplier", 0.10)) * math.exp(
            float(self.wage_cfg.get("ovr_exponent", 0.17)) * (ovr - 60.0))
        age_factor = _age_factor(age, self.wage_cfg.get("age_factors", {}))
        potential_factor = 1.0 + max(0.0, potential - ovr) * float(self.wage_cfg.get("potential_per_point", 0.015))
        pos = position_group(player.get("position", ""))
        position_factor = float(self.wage_cfg.get("position_factors", {}).get(pos, 1.0))
        club_factor = float(self.wage_cfg.get("club_factors", {}).get(club_tier, 1.0))
        star_factor = float(self.wage_cfg.get("star_factor", 1.10)) if is_star else 1.0
        raw = base * age_factor * potential_factor * position_factor * club_factor * star_factor
        raw = min(float(self.wage_cfg.get("max_annual_wage", 35.0)), max(0.05, raw))
        return self._round_wage(raw)

    def _round_wage(self, amount: float) -> float:
        step = max(0.001, float(self.wage_cfg.get("round_to", 0.05)))
        return _money(round(float(amount) / step) * step)

    def market_value(self, player: dict) -> float:
        """Estimate a player's transfer-market value in M€.

        The curve rewards current ability quadratically, then adjusts for relevant
        positional attributes, a strictly age-limited potential premium and a
        smoothly interpolated age/retirement discount. It deliberately avoids the
        old exponential curve, which pushed a large share of strong players into
        the maximum-value ceiling.
        """
        try:
            ovr = float(player.get("overall_rating", 60) or 60)
        except (TypeError, ValueError):
            ovr = 60.0
        ovr = max(1.0, min(99.0, ovr))
        try:
            age = float(player.get("age", player.get("_age", 26)) or 26)
        except (TypeError, ValueError):
            age = 26.0
        age = max(15.0, min(45.0, age))
        try:
            potential = float(player.get("potential", ovr) or ovr)
        except (TypeError, ValueError):
            potential = ovr
        potential = max(ovr, min(99.0, potential))

        base_rating = float(self.value_cfg.get("base_rating", 50.0))
        coefficient = max(0.0, float(self.value_cfg.get("base_coefficient", 0.03)))
        base_value = coefficient * max(0.0, ovr - base_rating) ** 2

        skill_index = _market_value_skill_index(player)
        if skill_index is None:
            attribute_factor = 1.0
        else:
            delta = skill_index - ovr
            delta_factor = float(self.value_cfg.get("attribute_delta_factor", 0.0075))
            raw_factor = 1.0 + delta * delta_factor
            attribute_factor = max(float(self.value_cfg.get("attribute_factor_min", 0.92)),
                                   min(float(self.value_cfg.get("attribute_factor_max", 1.08)), raw_factor))

        potential_bonus = 0.0
        gap = max(0.0, potential - ovr)
        for band in self.value_cfg.get("potential_premium", []):
            try:
                if age <= float(band.get("max_age", -1)):
                    potential_bonus = min(float(band.get("max_bonus", 0.0)),
                                          gap * float(band.get("per_point", 0.0)))
                    break
            except (TypeError, ValueError, AttributeError):
                continue
        potential_factor = 1.0 + max(0.0, potential_bonus)
        age_factor = _market_age_multiplier(age, self.value_cfg.get("age_curve", []))
        value = base_value * attribute_factor * potential_factor * age_factor
        minimum = max(0.001, float(self.value_cfg.get("min_value", 0.05)))
        maximum = max(minimum, float(self.value_cfg.get("max_value", 250.0)))
        return _money(min(maximum, max(minimum, value)))

    def assign_team_wages(self, players: dict, roster_ids: list[str], club_tier: str,
                          wage_budget: float) -> tuple[dict[str, float], float]:
        ordered = sorted(roster_ids, key=lambda pid: (
            -float(players[pid].get("overall_rating", 0)), str(players[pid].get("common_name", pid)).casefold()))
        top_count = int(self.wage_cfg.get("top_star_count", 3))
        stars = set(ordered[:top_count])
        raw = {pid: self.base_annual_wage(players[pid], club_tier, pid in stars) for pid in roster_ids}
        total = sum(raw.values())
        budget = _money(wage_budget)
        if total > budget * float(self.wage_cfg.get("scale_down_threshold", 0.85)) and total > 0:
            scale = budget * float(self.wage_cfg.get("scale_down_target", 0.80)) / total
            raw = {pid: self._round_wage(value * scale) for pid, value in raw.items()}
        elif total < budget * float(self.wage_cfg.get("scale_up_threshold", 0.50)) and total > 0:
            target = budget * float(self.wage_cfg.get("scale_up_threshold", 0.50))
            scale = min(float(self.wage_cfg.get("max_scale_up", 1.15)), target / total)
            raw = {pid: self._round_wage(value * scale) for pid, value in raw.items()}
        return raw, _money(sum(raw.values()))


class FinanceService:
    """The single supported write path for club cash balance."""

    @staticmethod
    def post(state: dict, team_id: str, tx_type: str, amount: float, *, season: int,
             month: int, description: str, related_id: str | None = None,
             date: str | None = None, idempotency_key: str | None = None) -> dict:
        if tx_type not in VALID_TRANSACTION_TYPES:
            raise ValueError(f"Loại giao dịch không hợp lệ: {tx_type}")
        finance = state.get("club_finance", {}).get(team_id)
        if not isinstance(finance, dict):
            raise KeyError(f"CLB chưa có ví tài chính: {team_id}")
        value = _money(amount)
        if value == 0:
            raise ValueError("Giao dịch 0 M€ không được ghi vào ledger.")
        ledger = state.setdefault("finance_ledger", [])
        # Idempotency keys protect event-driven accounting from duplicate cash
        # movements if a save is interrupted between posting and marking an
        # event/month as processed. Legacy ledgers are indexed lazily.
        if idempotency_key:
            key = str(idempotency_key)
            index = state.setdefault("finance_idempotency", {})
            tx_id = index.get(key)
            if tx_id:
                existing = next((row for row in ledger if row.get("id") == tx_id), None)
                if existing is not None:
                    if (existing.get("teamId") != str(team_id) or existing.get("type") != tx_type
                            or _money(existing.get("amount", 0)) != value):
                        raise ValueError(f"Idempotency key bị tái sử dụng với nội dung khác: {key}")
                    return existing
            existing = next((row for row in ledger if row.get("idempotencyKey") == key), None)
            if existing is not None:
                if (existing.get("teamId") != str(team_id) or existing.get("type") != tx_type
                        or _money(existing.get("amount", 0)) != value):
                    raise ValueError(f"Idempotency key bị tái sử dụng với nội dung khác: {key}")
                index[key] = existing.get("id")
                return existing
        seq = int(state.get("finance_transaction_seq", 0)) + 1
        state["finance_transaction_seq"] = seq
        tx = {
            "id": f"TX-{int(season)}-{seq:08d}",
            "date": date or f"{int(season):04d}-{int(month):02d}-01",
            "season": int(season), "month": int(month), "teamId": str(team_id),
            "type": tx_type, "amount": value,
            "description": str(description), "relatedId": related_id,
        }
        if idempotency_key:
            tx["idempotencyKey"] = str(idempotency_key)
        finance["balance"] = _money(float(finance.get("balance", 0.0)) + value)
        finance.setdefault("history", []).append(tx["id"])
        ledger.append(tx)
        if idempotency_key:
            state.setdefault("finance_idempotency", {})[str(idempotency_key)] = tx["id"]
        return tx


class ContractService:
    """Initial contract roles and migration-safe contract records."""

    def __init__(self, config: dict):
        self.config = config

    def squad_role(self, player: dict, rank: int, meta: dict) -> str:
        cfg = self.config.get("contracts", {})
        age = float(meta.get("age", 26) or 26)
        ovr = float(player.get("overall_rating", 60) or 60)
        pot = float(meta.get("potential", ovr) or ovr)
        if (age <= float(cfg.get("role_prospect_max_age", 21)) and
                pot - ovr >= float(cfg.get("role_prospect_min_potential_gap", 8.0))):
            return "PROSPECT"
        if rank <= 3:
            return "STAR"
        if rank <= 11:
            return "FIRST_TEAM"
        if rank <= 18:
            return "ROTATION"
        return "BACKUP"

    def initial_years_remaining(self, seed: int, player_id: str) -> int:
        cfg = self.config.get("contracts", {})
        total_years = int(cfg.get("default_years", 3))
        if bool(cfg.get("uniform_initial_contract", False)):
            return total_years
        weights = cfg.get("remaining_year_weights", {"1": 30, "2": 35, "3": 35})
        values, probabilities = [], []
        for value, weight in weights.items():
            years, probability = int(value), float(weight)
            if years < 1 or years > total_years or probability < 0:
                continue
            values.append(years); probabilities.append(probability)
        if not values or sum(probabilities) <= 0:
            values, probabilities = list(range(1, total_years + 1)), [1.0] * total_years
        return _stable_rng(seed, f"contract:{player_id}").choices(values, weights=probabilities, k=1)[0]


def club_finance_tier(team: str, team_tier: str, profile_group: str,
                      overrides: dict, config: dict) -> str:
    clubs = overrides.get("clubs", {}) if isinstance(overrides, dict) else {}
    item = clubs.get(team, {}) if isinstance(clubs, dict) else {}
    explicit = item.get("tier") if isinstance(item, dict) else None
    if explicit in ("big", "medium", "small"):
        return explicit
    # In the current Super League data, Premier/FC1/FC2 are the reliable
    # competitive-strength tiers. Academy talent profiles are independent and
    # must not accidentally classify a strong Premier club as a small club.
    division_tier = config.get("division_tier_mapping", {}).get(team_tier)
    if division_tier in ("big", "medium", "small"):
        return division_tier
    return str(config.get("tier_mapping", {}).get(profile_group, "small"))


def init_club_finance(state: dict, team: str, team_tier: str, profile_group: str,
                      config: dict, overrides: dict, seed: int, season: int,
                      finance_service: type[FinanceService] = FinanceService) -> dict:
    """Create one club wallet once, with deterministic +/- variance and overrides."""
    current = state.setdefault("club_finance", {}).get(team)
    if isinstance(current, dict):
        return current
    tier = club_finance_tier(team, team_tier, profile_group, overrides, config)
    base = copy.deepcopy(config["initial_finance"][tier])
    club_override = overrides.get("clubs", {}).get(team, {})
    if isinstance(club_override, dict):
        for field in ("balance", "transfer_budget", "wage_budget", "reputation", "fan_base", "ticket_price"):
            if field in club_override:
                base[field] = copy.deepcopy(club_override[field])
    rng = _stable_rng(seed, f"finance:{team}")
    variance = float(config["initial_finance"].get("variance", 0.10))
    factor = rng.uniform(1.0 - variance, 1.0 + variance)
    # Explicit numeric overrides are exact; the random deviation only applies
    # to values inherited from the tier defaults.
    balance = _money(float(base["balance"]) * (1.0 if "balance" in club_override else factor))
    transfer_budget = _money(float(base["transfer_budget"]) *
                             (1.0 if "transfer_budget" in club_override else factor))
    wage_budget = _money(float(base["wage_budget"]) *
                         (1.0 if "wage_budget" in club_override else factor))
    reputation_range = base.get("reputation", [50, 50])
    if isinstance(reputation_range, (int, float)):
        reputation = float(reputation_range)
    else:
        reputation = round(rng.uniform(float(reputation_range[0]), float(reputation_range[1])), 1)
    fan_range = base.get("fan_base", [1_000_000, 1_000_000])
    if isinstance(fan_range, (int, float)):
        fan_base = int(fan_range)
    else:
        fan_base = int(round(rng.uniform(float(fan_range[0]), float(fan_range[1]))))
    finance = {
        "teamId": team, "tier": tier, "balance": 0.0,
        "transfer_budget": transfer_budget, "wage_budget": wage_budget,
        "debt": 0.0, "reputation": reputation, "fan_base": fan_base,
        "stadium_id": None, "ticket_price": float(base.get("ticket_price", 50.0)),
        "history": [], "initialized_season": int(season),
    }
    state["club_finance"][team] = finance
    start_date = str(config.get("season_start_date", f"{int(season)}-08-01"))
    try:
        month = int(start_date[5:7])
    except (ValueError, TypeError):
        month = 8
    finance_service.post(state, team, "OTHER", balance, season=season, month=month,
                         description=f"Vốn tài chính khởi tạo · hạng {tier.upper()}",
                         related_id=f"finance-init:{team}", date=start_date)
    return finance


def bootstrap_economy(state: dict, *, rosters: dict[str, list[str]], players: dict[str, dict],
                      player_meta: dict[str, dict], team_profiles: dict[str, str],
                      team_tiers: dict[str, str], config: dict, overrides: dict,
                      seed: int, season: int) -> bool:
    """Idempotently seed wallets, wages, market values and active contracts.

    This deliberately does not charge wages or run transfers yet; those are later
    roadmap phases and must not alter the existing match simulation clock.
    """
    if not config.get("feature_flags", {}).get("ENABLE_ECONOMY", False):
        return False
    previous_schema = int(state.get("economy_schema_version", 0) or 0)
    before = (len(state.get("finance_ledger", [])), len(state.get("contracts", {})),
              len(state.get("club_finance", {})), previous_schema,
              sum(1 for player in players.values() if "wage" in player),
              sum(1 for player in players.values() if "market_value" in player),
              int(state.get("market_value_model_version", 0) or 0))
    state.setdefault("finance_ledger", [])
    state.setdefault("finance_transaction_seq", len(state["finance_ledger"]))
    state.setdefault("club_finance", {})
    state.setdefault("contracts", {})
    state.setdefault("free_agents", [])
    state.setdefault("transfer_history", [])
    state.setdefault("economy_schema_version", 0)

    for team in rosters:
        profile = team_profiles.get(team, "standard")
        init_club_finance(state, team, team_tiers.get(team, "fc1"), profile, config,
                          overrides, seed, season)

    wage_service = WageService(config)
    contract_service = ContractService(config)

    # Keep the tier budgets meaningful for this project's unusually strong
    # starting squads. Earlier P1 saves raised the budget to the full raw wage
    # sum (sometimes 3–4x the tier table), which made every club structurally
    # loss-making. Schema v2 caps that one-time adjustment at +10%, then uses
    # the specified 80%-of-budget normalization. Existing P1 player/contract
    # wages are migrated once; schema v2 saves remain untouched.
    for team, ids in rosters.items():
        finance = state["club_finance"][team]
        missing_wage_ids = [pid for pid in ids if "wage" not in players[pid]]
        ranked = sorted(ids, key=lambda pid: -float(players[pid].get("overall_rating", 0)))
        top_ids = set(ranked[:int(config.get("wages", {}).get("top_star_count", 3))])
        wage_players = {}
        for pid in ids:
            source = dict(players[pid])
            meta = player_meta.get(pid, {})
            source["age"] = float(meta.get("age", 26) or 26)
            source["potential"] = float(meta.get("potential", source.get("overall_rating", 60)) or 60)
            wage_players[pid] = source

        if previous_schema < 2:
            tier_defaults = config.get("initial_finance", {}).get(finance.get("tier", "small"), {})
            team_override = overrides.get("clubs", {}).get(team, {})
            rng = _stable_rng(seed, f"finance:{team}")
            variance = float(config.get("initial_finance", {}).get("variance", 0.10))
            seeded_factor = rng.uniform(1.0 - variance, 1.0 + variance)
            seeded_budget = float(team_override.get("wage_budget", tier_defaults.get("wage_budget", finance.get("wage_budget", 0))))
            if "wage_budget" not in team_override:
                seeded_budget *= seeded_factor
            raw_total = sum(wage_service.base_annual_wage(wage_players[pid], finance["tier"],
                                                          pid in top_ids) for pid in ids)
            bump_cap = max(1.0, float(config.get("wages", {}).get("initial_budget_bump_cap", 1.10)))
            if raw_total > seeded_budget * 1.10:
                seeded_budget = min(raw_total / 0.80, seeded_budget * bump_cap)
            finance["wage_budget"] = _money(seeded_budget)
            calculated, _total = wage_service.assign_team_wages(
                wage_players, ids, finance["tier"], float(finance["wage_budget"]))
            for pid in ids:
                players[pid]["wage"] = calculated[pid]
                existing_contract = state.get("contracts", {}).get(pid)
                if isinstance(existing_contract, dict) and existing_contract.get("team_id") == team:
                    existing_contract["wage_per_year"] = calculated[pid]
                    existing_contract["status"] = existing_contract.get("status", "ACTIVE")
        elif missing_wage_ids:
            calculated, _total = wage_service.assign_team_wages(
                wage_players, ids, finance["tier"], float(finance["wage_budget"]))
            for pid in missing_wage_ids:
                players[pid]["wage"] = calculated[pid]

    value_model_version = int(config.get("market_value", {}).get("model_version", 2) or 2)
    refresh_market_values = int(state.get("market_value_model_version", 0) or 0) < value_model_version
    if refresh_market_values:
        # One-time recalculation for existing careers whenever the valuation model
        # changes. Revalue every registered player, not only today's roster, and
        # keep the active contract's reference value in sync with the player card.
        for pid, player in players.items():
            if not isinstance(player, dict):
                continue
            meta = player_meta.get(pid, {})
            value_input = dict(player)
            value_input["age"] = float(meta.get("age", player.get("age", 26)) or 26)
            value_input["potential"] = float(meta.get("potential", player.get("potential", player.get("overall_rating", 60)))
                                             or player.get("overall_rating", 60))
            player["market_value"] = wage_service.market_value(value_input)
            contract = state.get("contracts", {}).get(pid)
            if isinstance(contract, dict):
                contract["market_value"] = player["market_value"]
        for _team, entries in state.get("academy", {}).items():
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or not isinstance(entry.get("player"), dict):
                    continue
                academy_player = entry["player"]
                academy_meta = entry.get("meta", {}) if isinstance(entry.get("meta"), dict) else {}
                value_input = dict(academy_player)
                value_input["age"] = float(academy_meta.get("age", 18) or 18)
                value_input["potential"] = float(academy_meta.get("potential", value_input.get("overall_rating", 60))
                                                 or value_input.get("overall_rating", 60))
                academy_player["market_value"] = wage_service.market_value(value_input)
        state["market_value_model_version"] = value_model_version
    else:
        for pid, player in players.items():
            if not isinstance(player, dict) or "market_value" in player:
                continue
            meta = player_meta.get(pid, {})
            value_input = dict(player)
            value_input["age"] = float(meta.get("age", player.get("age", 26)) or 26)
            value_input["potential"] = float(meta.get("potential", player.get("potential", player.get("overall_rating", 60)))
                                             or player.get("overall_rating", 60))
            player["market_value"] = wage_service.market_value(value_input)
        for _team, entries in state.get("academy", {}).items():
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or not isinstance(entry.get("player"), dict):
                    continue
                academy_player = entry["player"]
                if "market_value" not in academy_player:
                    academy_meta = entry.get("meta", {}) if isinstance(entry.get("meta"), dict) else {}
                    value_input = dict(academy_player)
                    value_input["age"] = float(academy_meta.get("age", 18) or 18)
                    value_input["potential"] = float(academy_meta.get("potential", value_input.get("overall_rating", 60))
                                                     or value_input.get("overall_rating", 60))
                    academy_player["market_value"] = wage_service.market_value(value_input)

    # All current registered players receive one active deal, unless a legacy active
    # contract already exists. Contracts are keyed by player ID for idempotent upgrades.
    current_season = int(season)
    for team, ids in rosters.items():
        ranked = sorted(ids, key=lambda pid: -float(players[pid].get("overall_rating", 0)))
        ranks = {pid: index + 1 for index, pid in enumerate(ranked)}
        for pid in ids:
            old = state["contracts"].get(pid)
            if isinstance(old, dict) and old.get("status") == "ACTIVE" and old.get("team_id") == team:
                continue
            meta = player_meta.get(pid, {})
            player = players[pid]
            years_remaining = contract_service.initial_years_remaining(seed, pid)
            years_total = int(config.get("contracts", {}).get("default_years", 3))
            years_remaining = min(years_total, years_remaining)
            start_season = current_season - (years_total - years_remaining)
            age = float(meta.get("age", 26) or 26)
            role = contract_service.squad_role(player, ranks[pid], meta)
            contract_id = f"CT-{current_season}-{pid}"
            state["contracts"][pid] = {
                "id": contract_id, "player_id": pid, "team_id": team,
                "start_season": start_season, "end_season": start_season + years_total - 1,
                "years_total": years_total, "years_remaining": years_remaining,
                "wage_per_year": _money(player.get("wage", 0.05)),
                "signing_bonus": 0.0, "release_clause": None,
                "squad_role": role, "status": "ACTIVE",
                "market_value": _money(player.get("market_value", 0.05)),
                "initialized": True,
            }

    state["market_value_model_version"] = max(
        int(state.get("market_value_model_version", 0) or 0),
        int(config.get("market_value", {}).get("model_version", 2) or 2))
    state["economy_schema_version"] = ECONOMY_SCHEMA_VERSION
    after = (len(state.get("finance_ledger", [])), len(state.get("contracts", {})),
             len(state.get("club_finance", {})), int(state.get("economy_schema_version", 0) or 0),
             sum(1 for player in players.values() if "wage" in player),
             sum(1 for player in players.values() if "market_value" in player),
             int(state.get("market_value_model_version", 0) or 0))
    return before != after


class SquadRulesService:
    """Validate roster size and positional coverage without changing the roster."""

    def __init__(self, config: dict):
        self.config = config

    def validate(self, player_ids: list[str], players: dict[str, dict]) -> dict:
        cfg = self.config.get("squad_rules", {})
        errors = []
        count = len(player_ids)
        minimum = int(cfg.get("min_size", 23)); maximum = int(cfg.get("max_size", 30))
        if count < minimum:
            errors.append(f"Đội hình có {count} cầu thủ; tối thiểu {minimum}.")
        if count > maximum:
            errors.append(f"Đội hình có {count} cầu thủ; tối đa {maximum}.")
        groups = Counter(position_group(players.get(pid, {}).get("position", "")) for pid in player_ids)
        for group, required in cfg.get("minimum_groups", {}).items():
            if groups.get(group, 0) < int(required):
                errors.append(f"Thiếu {group}: hiện có {groups.get(group, 0)}, cần ít nhất {required}.")
        if int(cfg.get("minimum_each_group", 0)) > 0:
            for group in ("GK", "DEF", "MID", "FWD"):
                if groups.get(group, 0) < int(cfg["minimum_each_group"]):
                    errors.append(f"Nhóm {group} cần ít nhất {cfg['minimum_each_group']} cầu thủ.")
        return {"ok": not errors, "errors": errors, "size": count, "groups": dict(groups)}


class TransferWindowService:
    def __init__(self, config: dict):
        self.config = config.get("transfer_windows", config)

    def is_open(self, month: int) -> bool:
        month = int(month)
        return month in {int(x) for x in self.config.get("winter_months", [11, 12]) +
                         self.config.get("summer_months", [6, 7, 8])}

    def window_name(self, month: int) -> str | None:
        month = int(month)
        if month in set(map(int, self.config.get("winter_months", [11, 12]))):
            return "WINTER"
        if month in set(map(int, self.config.get("summer_months", [6, 7, 8]))):
            return "SUMMER"
        return None


class PrizeMoneyService:
    """Configuration reader for the competition prize ladder; payouts hook in P2."""

    def __init__(self, config_path: Path):
        self.data = load_json_object(Path(config_path), {})

    def competition(self, key: str) -> dict:
        return copy.deepcopy(self.data.get(str(key).upper(), {}))
