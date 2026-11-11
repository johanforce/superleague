# -*- coding: utf-8 -*-
"""Season-to-season player development model (version 2).

The model is deliberately deterministic apart from an explicitly supplied noise
sample. This makes the influences auditable and the formula straightforward to
unit-test. The game engine owns random-number generation and attribute mutation.
"""
from __future__ import annotations

from typing import Any

MODEL_VERSION = 2


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def age_curve(age: float) -> float:
    """Potential development curve, using age at the start of the season."""
    if age <= 19:
        return 4.0
    return {
        20: 3.0, 21: 2.5, 22: 2.0, 23: 1.2, 24: 0.8,
        25: 0.2, 26: 0.2, 27: 0.2, 28: 0.0, 29: -0.5,
        30: -1.0, 31: -1.5, 32: -2.0, 33: -2.5,
    }.get(int(age), -3.0 if age >= 34 else 0.2)


def calculate_season_development(
    *,
    age: float,
    overall: float,
    potential: float,
    minutes: int,
    average_rating: float | None,
    injury_matches: int = 0,
    injury_days: int = 0,
    training_days: int = 0,
    average_morale: float | None = None,
    noise: float = 0.0,
) -> dict:
    """Return a transparent development score and bounded integer OVR delta.

    Influences:
      - age controls the natural growth/decline curve;
      - potential gap gates growth and adds a modest young-player upside bonus;
      - minutes provide a capped match-exposure modifier;
      - average match rating represents sustained form (6.5 is neutral);
      - injury absences reduce the season's development opportunity;
      - noise is injected by the engine, never hidden inside this pure function.
    """
    age = _number(age, 26.0)
    overall = _number(overall, 50.0)
    potential = _number(potential, overall)
    minutes = max(0, int(_number(minutes, 0)))
    injury_matches = max(0, int(_number(injury_matches, 0)))
    injury_days = max(0, int(_number(injury_days, 0)))
    training_days = max(0, int(_number(training_days, 0)))
    morale = 65.0 if average_morale is None else _number(average_morale, 65.0)
    rating = 6.5 if average_rating is None else _number(average_rating, 6.5)

    gap = potential - overall
    potential_factor = _clamp(gap / 12.0, 0.0, 1.0)
    age_factor = age_curve(age)

    # Young players with a low ceiling still develop slowly; older players can
    # decline regardless of a high historical potential.
    if age <= 27:
        age_contribution = age_factor * (0.25 + 0.75 * potential_factor)
        potential_bonus = min(0.65, max(0.0, gap) * 0.035) if age <= 24 else 0.0
    else:
        age_contribution = age_factor
        potential_bonus = 0.0

    minutes_ratio = _clamp(minutes / 3420.0, 0.0, 1.0)
    minutes_factor = 0.9 * (minutes_ratio - 0.5)
    form_factor = _clamp((rating - 6.5) * 0.55, -0.9, 0.9)
    injury_factor = -min(1.4, injury_matches * 0.075 + injury_days * 0.003)
    training_factor = min(0.55, training_days / 365.0 * 0.55)
    morale_factor = _clamp((morale - 65.0) / 35.0 * 0.35, -0.35, 0.35)

    score = age_contribution + potential_bonus + minutes_factor + form_factor + injury_factor + training_factor + morale_factor
    raw_delta = int(round(score + _number(noise, 0.0)))
    delta = max(-5, min(6, raw_delta))
    return {
        "model_version": MODEL_VERSION,
        "age_factor": round(age_contribution, 3),
        "age_curve": round(age_factor, 3),
        "potential_gap": round(gap, 2),
        "potential_factor": round(potential_factor, 3),
        "potential_bonus": round(potential_bonus, 3),
        "minutes_ratio": round(minutes_ratio, 3),
        "minutes_factor": round(minutes_factor, 3),
        "form_factor": round(form_factor, 3),
        "injury_matches": injury_matches,
        "injury_days": injury_days,
        "injury_factor": round(injury_factor, 3),
        "training_days": training_days,
        "training_factor": round(training_factor, 3),
        "average_morale": round(morale, 2),
        "morale_factor": round(morale_factor, 3),
        "average_rating": round(rating, 2),
        "development_score": round(score, 3),
        "noise": round(_number(noise, 0.0), 3),
        "raw_delta": raw_delta,
        "delta": delta,
    }

# The 6.1.x engine also reconciles attribute-derived OVR and individual training.
# These helpers remain pure so that the simulation owns all random decisions.
def estimate_overall_from_attributes(player: dict) -> float:
    """Estimate a position-aware OVR from the attributes available in a player row."""
    position = str(player.get("position", "CM")).upper()
    groups = {
        "GK": ("goalkeeping_diving", "goalkeeping_handling", "goalkeeping_kicking",
               "goalkeeping_positioning", "goalkeeping_reflexes", "goalkeeping_speed"),
        "DEF": ("defending_awareness", "defending_standing_tackle", "defending_sliding_tackle",
                "physicality", "movement_sprint_speed", "movement_acceleration", "attacking_short_passing"),
        "MID": ("attacking_short_passing", "mentality_vision", "skill_ball_control", "skill_dribbling",
                "movement_acceleration", "movement_sprint_speed", "mentality_interceptions", "skill_long_passing"),
        "FWD": ("attacking_finishing", "mentality_positioning", "movement_sprint_speed", "movement_acceleration",
                "skill_dribbling", "attacking_volleys", "attacking_heading_accuracy"),
    }
    group = "GK" if position == "GK" else ("DEF" if position in {"CB", "RB", "LB", "RWB", "LWB"} else
             ("FWD" if position in {"ST", "CF", "LW", "RW"} else "MID"))
    keys = groups[group]
    values = []
    for key in keys:
        value = player.get(key)
        if value is not None:
            try: values.append(max(1.0, min(99.0, float(value))))
            except (TypeError, ValueError): pass
    if not values:
        try: return float(player.get("overall_rating", 50))
        except (TypeError, ValueError): return 50.0
    return sum(values) / len(values)


def choose_training_focus(player: dict) -> str:
    """Choose one of the engine's supported training groups from its weakest area."""
    position = str(player.get("position", "CM")).upper()
    groups = {
        "goalkeeping": ("goalkeeping_diving", "goalkeeping_handling", "goalkeeping_kicking",
                        "goalkeeping_positioning", "goalkeeping_reflexes"),
        "pace": ("pace", "movement_acceleration", "movement_sprint_speed"),
        "finishing": ("shooting", "attacking_finishing", "attacking_volleys", "power_long_shots"),
        "passing": ("passing", "attacking_short_passing", "skill_long_passing", "mentality_vision"),
        "defending": ("defending", "defending_awareness", "defending_standing_tackle", "mentality_interceptions"),
        "physical": ("physicality", "power_stamina", "power_strength", "power_jumping"),
    }
    allowed = ("goalkeeping",) if position == "GK" else (
        ("defending", "pace", "physical", "passing") if position in {"CB", "RB", "LB", "RWB", "LWB"} else
        (("finishing", "pace", "passing", "physical") if position in {"ST", "CF", "LW", "RW"} else
         ("passing", "defending", "pace", "physical")))
    scores = []
    for group in allowed:
        values = []
        for key in groups[group]:
            try: values.append(float(player[key]))
            except (KeyError, TypeError, ValueError): pass
        if values: scores.append((sum(values) / len(values), group))
    return min(scores)[1] if scores else ("goalkeeping" if position == "GK" else "balanced")


def reconcile_overall_rating(player: dict, *, original_ovr: int, development_ovr: int,
                             calibration_offset: float = 0.0, attribute_weight: float = 0.35) -> dict:
    """Blend the engine's season delta with the attribute-derived rating, preserving calibration."""
    weight = _clamp(_number(attribute_weight, 0.35), 0.0, 1.0)
    estimated = estimate_overall_from_attributes(player) + _number(calibration_offset, 0.0)
    attribute_target = _clamp(estimated, 40.0, 99.0)
    blended = (1.0 - weight) * _number(development_ovr, original_ovr) + weight * attribute_target
    final = int(round(_clamp(blended, 40.0, 99.0)))
    return {"overall_rating": final, "calibration_offset": round(final - estimate_overall_from_attributes(player), 3),
            "attribute_estimate": round(estimate_overall_from_attributes(player), 3),
            "attribute_target": round(attribute_target, 3), "attribute_weight": weight}
