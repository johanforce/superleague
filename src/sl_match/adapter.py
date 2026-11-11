# -*- coding: utf-8 -*-
"""Adapter between Super League's save/roster model and the portable WNL match stack.

The WNL demo's tournament calendar is intentionally NOT imported.  Only the generic
match model (players, lineups, context, coaching, VAR, detailed stats and replay) is reused.
"""
from __future__ import annotations

import copy
import datetime as _dt
import random
import base64
import json
import zlib
from typing import Any

from . import coach, context, engine, stats


def _player_name(player: dict[str, Any]) -> str:
    return (player.get("common_name") or
            (str(player.get("first_name", "")) + " " + str(player.get("last_name", ""))).strip() or
            str(player.get("player_id", "Unknown")))


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(v) for v in value), key=str)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _restore_live_sets(prep: dict[str, Any]) -> dict[str, Any]:
    restored = copy.deepcopy(prep)
    for key in ("A", "B"):
        team = restored.get(key) or {}
        if "out" in team and not isinstance(team["out"], set):
            team["out"] = set(team["out"])
    return restored


def _pack_live_bundle(prep: dict[str, Any]) -> str:
    """Compress replay input to keep multi-season career saves reasonably small."""
    raw = json.dumps(_json_safe(prep), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = base64.b64encode(zlib.compress(raw, level=6)).decode("ascii")
    return "zlib-json:v1:" + payload


def restore_live_bundle(bundle: dict[str, Any] | str | None) -> dict[str, Any] | None:
    """Restore compressed or legacy JSON match prep for the WNL live replay renderer."""
    if not bundle:
        return None
    if isinstance(bundle, str) and bundle.startswith("zlib-json:v1:"):
        payload = base64.b64decode(bundle.split(":", 2)[2].encode("ascii"))
        bundle = json.loads(zlib.decompress(payload).decode("utf-8"))
    if not isinstance(bundle, dict):
        raise ValueError("Dữ liệu phát lại trận đấu không hợp lệ.")
    return _restore_live_sets(bundle)


def simulate_fixture(*, home: str, away: str, home_squad: list[dict], away_squad: list[dict],
                     home_dyn: dict, away_dyn: dict, home_status: dict, away_status: dict,
                     team_momentum: dict, coaches: dict, refs: list[dict], team_ctx: dict,
                     last_match: dict, seed: int, match_date: str, neutral: bool = False,
                     importance: float = 0.4, rivalry: float = 0.0,
                     knockout: bool = False, aggregate_before=None) -> dict:
    """Return a JSON-ready Super League match record plus its exact live replay bundle."""
    old_rng_state = random.getstate()
    random.seed(int(seed) & 0xFFFFFFFF)
    try:
        home_players = [engine.make_player(dict(p)) for p in home_squad]
        away_players = [engine.make_player(dict(p)) for p in away_squad]

        def pack_players(team, source, dyn, status):
            dyn_p = {}
            unavailable = set()
            for p in source:
                pid = p["pid"]
                d = dyn.get(pid, {}) if isinstance(dyn, dict) else {}
                dyn_p[pid] = (float(d.get("form", 0.0)), float(d.get("fit", 100.0)))
                st = status.get(pid, {}) if isinstance(status, dict) else {}
                if int(st.get("inj", 0)) > 0 or int(st.get("susp", 0)) > 0:
                    unavailable.add(pid)
            td = team_momentum.get(team, {}) if isinstance(team_momentum, dict) else {}
            c = coaches.get(team) or coach.make_coach(team)
            return engine.prepare_team(team, source, unavailable, dyn_p, td, coach=c)

        A = pack_players(home, home_players, home_dyn, home_status)
        B = pack_players(away, away_players, away_dyn, away_status)
        # venue_last được chọn theo đúng vai trò của trận hiện tại; neutral không dùng form sân.
        if isinstance(team_momentum, dict):
            if not neutral:
                A['venue_form'] = engine.recent_form_score((team_momentum.get(home) or {}).get('home_last', []), engine.CFG['venue_form_window'])
                B['venue_form'] = engine.recent_form_score((team_momentum.get(away) or {}).get('away_last', []), engine.CFG['venue_form_window'])
            else:
                A['venue_form'] = B['venue_form'] = 0.0
        pre_strength = [engine.team_strength(A), engine.team_strength(B)]
        try:
            mdate = _dt.date.fromisoformat(str(match_date))
        except ValueError:
            mdate = _dt.date.today()
        ctx = context.make_ctx(int(seed) ^ 0x5A17, home, away, mdate.month, importance=importance,
                               neutral=neutral, host=None, rivalry=rivalry, refs=refs,
                               team_ctx=team_ctx, match_date=mdate, last_match=last_match,
                               ko=knockout)
        r = engine.simulate(A, B, neutral=neutral, knockout=knockout, ctx=ctx, refs=refs,
                            aggregate_before=aggregate_before)
        det = stats.detail_stats(r, A, B)
        ctx_txt = context.describe(ctx, refs)

        agg = aggregate_before or (0, 0)
        total_h, total_a = r["hg"] + int(agg[0]), r["ag"] + int(agg[1])
        pens = r.get("pens")
        if total_h > total_a:
            winner = home
        elif total_h < total_a:
            winner = away
        elif pens:
            winner = home if pens[0] > pens[1] else away
        else:
            winner = None

        goals = []
        for g in r.get("goals", []):
            side = g["t"]
            team_name = home if side == "h" else away
            pl = A["players"] if side == "h" else B["players"]
            scorer = pl.get(g.get("s"), {})
            assister = pl.get(g.get("a"), {}) if g.get("a") else {}
            minute = int(g.get("m", 0))
            goals.append({
                "minute": minute, "team": team_name, "side": "home" if side == "h" else "away",
                "scorer_id": g.get("s"), "scorer": scorer.get("name") or str(g.get("s")),
                "assist_id": g.get("a"), "assist": assister.get("name") if assister else None,
                "period": "ET" if minute > 90 else "90m", "pen": bool(g.get("pen")),
                "xg": g.get("xg"), "var": g.get("var")
            })

        raw_stats = r["stats"]
        adv_stats = det.get("t", {})
        target_stats = {
            "shots": list(raw_stats["shots"]), "sot": list(raw_stats["sot"]),
            "xg": list(raw_stats["xg"]), "possession": list(raw_stats["poss"])
        }
        pens_out = {"home": int(pens[0]), "away": int(pens[1])} if pens else None
        forms = (tuple(r.get("form_h", (4, 3, 3))), tuple(r.get("form_a", (4, 3, 3))))
        formation_h = "-".join(str(x) for x in forms[0])
        formation_a = "-".join(str(x) for x in forms[1])

        # Keep complete, exact inputs so the live replay never rolls another result.
        prep = {"A": A, "B": B, "r": r, "det": det, "ctx": ctx, "ctx_txt": ctx_txt}
        live_bundle = _pack_live_bundle(prep)
        match_data = {
            "home": home, "away": away, "hg": int(r["hg"]), "ag": int(r["ag"]),
            "et": bool(r.get("et")), "pens": pens_out, "winner": winner,
            "goals": goals, "xi_h": list(r["xi_h"]), "xi_a": list(r["xi_a"]),
            "formation_h": formation_h, "formation_a": formation_a,
            "stats": target_stats,
            "ratings": {"attack": [round(sum(p["att"] for p in A["xi"]) / 11, 1),
                                     round(sum(p["att"] for p in B["xi"]) / 11, 1)],
                        "defence": [round(sum(p["dfn"] for p in A["xi"]) / 11, 1),
                                    round(sum(p["dfn"] for p in B["xi"]) / 11, 1)]},
            "aggregate_before": list(agg), "match_engine": "enhanced",
            "ctx": ctx, "ctx_text": ctx_txt, "var": r.get("var", []),
            "tactics": r.get("tactics", []), "ments": r.get("ments", []),
            "coach": list(r.get("coach", [])),
            "subs_h": r.get("subs_h", []), "subs_a": r.get("subs_a", []),
            "sub_why": r.get("sub_why", {}), "cards_h": r.get("cards_h", []),
            "cards_a": r.get("cards_a", []), "inj_h": r.get("inj_h", []),
            "inj_a": r.get("inj_a", []), "minutes_h": r.get("mins_h", {}),
            "minutes_a": r.get("mins_a", {}), "ga_h": r.get("ga_h", {}),
            "ga_a": r.get("ga_a", {}), "advanced_stats": adv_stats,
            "player_stats": det.get("p", {}), "motm": stats.best_player(det),
            "engine_factors": {
                "home": {**pre_strength[0], "momentum": round(A.get("mom", 0.0), 3),
                         "recent_form": round(A.get("recent_form", 0.0), 3),
                         "venue_form": round(A.get("venue_form", 0.0), 3),
                         "initial_tactic": (r.get("ment") or ["balanced", "balanced"])[0]},
                "away": {**pre_strength[1], "momentum": round(B.get("mom", 0.0), 3),
                         "recent_form": round(B.get("recent_form", 0.0), 3),
                         "venue_form": round(B.get("venue_form", 0.0), 3),
                         "initial_tactic": (r.get("ment") or ["balanced", "balanced"])[1]},
            },
            "live_bundle": live_bundle
        }
        return match_data
    finally:
        random.setstate(old_rng_state)


def _name(players: dict, pid: str) -> str:
    return str(players.get(pid, {}).get("common_name") or
               (str(players.get(pid, {}).get("first_name", "")) + " " +
                str(players.get(pid, {}).get("last_name", ""))).strip() or pid)


def _report_side(side: Any, team: Any, home: str, away: str) -> str:
    """Resolve an event side from either an explicit side or a compact team name."""
    value = str(side or "").strip().casefold()
    if value in {"h", "home", "host"}:
        return "h"
    if value in {"a", "away", "visitor"}:
        return "a"
    team_name = str(team or "").strip().casefold()
    if team_name and team_name == str(home or "").strip().casefold():
        return "h"
    if team_name and team_name == str(away or "").strip().casefold():
        return "a"
    # Keep the historical fallback for malformed legacy events, but valid compact
    # match records always carry a team name and are resolved above.
    return "a"


def _is_red_card_code(value: Any) -> bool:
    return str(value or "").strip().casefold().replace(" ", "_") in {
        "r", "red", "yr", "second_yellow", "second-yellow", "2y", "secondyellow"
    }


def format_report_result(match: dict) -> tuple[str, str]:
    """Return the score suffix and concise knockout result note for a match report."""
    if not isinstance(match, dict):
        return "", ""
    score_suffix = " AET" if bool(match.get("aet", match.get("et", False))) else ""
    pens = match.get("pens")
    penalty_score = None
    if isinstance(pens, dict) and "home" in pens and "away" in pens:
        penalty_score = (pens.get("home"), pens.get("away"))
    elif isinstance(pens, (list, tuple)) and len(pens) >= 2:
        penalty_score = (pens[0], pens[1])
    if penalty_score is None:
        return score_suffix, ""
    try:
        home_score, away_score = int(penalty_score[0]), int(penalty_score[1])
    except (TypeError, ValueError, OverflowError):
        return score_suffix, ""
    note = f"Luân lưu {home_score}–{away_score}"
    winner = match.get("winner")
    if not winner and home_score != away_score:
        winner = match.get("home") if home_score > away_score else match.get("away")
    if winner:
        note += f" · {winner} thắng"
    return score_suffix, note


def report_for_match(match: dict, players: dict, refs=None) -> dict:
    """Transform either a full result or a compact archived result into report UI data.

    Cup/C1 archives intentionally retain only scorers, red cards, AET and the
    penalty-shootout score. Their four report tabs remain available, while the
    detailed-statistics and player-statistics tabs explain that those details
    are not part of the compact competition record.
    """
    h, a = str(match.get("home", "")), str(match.get("away", ""))
    comp_key = str(match.get("comp", "")).strip().casefold()
    summary_only = comp_key in {
        "cup", "major cup", "c1", "c1 play-in", "champions league",
        "champions cup", "continental cup"
    }
    det = match.get("player_stats") or {}
    rp = {
        "summary_only": summary_only,
        "goals": [], "events": [],
        "stats": None if summary_only else match.get("advanced_stats"),
        "form": None if summary_only else [match.get("formation_h", ""), match.get("formation_a", "")],
        "coach": None if summary_only else match.get("coach"),
        "ctx": "" if summary_only else match.get("ctx_text", ""),
        "var": [], "tactics": [], "players": {"h": [], "a": []}, "tl": []
    }

    for goal in match.get("goals", []) or []:
        if not isinstance(goal, dict):
            continue
        side = _report_side(goal.get("side"), goal.get("team"), h, a)
        minute = goal.get("minute", goal.get("m", 0))
        try:
            minute = int(minute or 0)
        except (TypeError, ValueError, OverflowError):
            minute = 0
        scorer = str(goal.get("scorer") or goal.get("player") or goal.get("scorer_id") or "Cầu thủ")
        assist = "" if summary_only else str(goal.get("assist") or "")
        pen = bool(goal.get("pen"))
        rp["goals"].append([minute, side, scorer, assist, 1 if pen else 0,
                            None if summary_only else goal.get("xg")])
        txt = f"{scorer}{' (pen)' if pen else ''}"
        if not summary_only and assist:
            txt += " - kiến tạo: " + assist
        if not summary_only and goal.get("xg") is not None:
            try:
                txt += "  (xG %.2f)" % float(goal.get("xg"))
            except (TypeError, ValueError):
                pass
        rp["tl"].append((f"{minute}'", "goal", side,
                         f"{scorer} ghi bàn{' (phạt đền)' if pen else ''}"))

    def add_red_card(side: str, minute: Any, player_name: Any, card_type: Any = "red") -> None:
        try:
            minute_value = int(minute or 0)
        except (TypeError, ValueError, OverflowError):
            minute_value = 0
        code = str(card_type or "red").strip().casefold().replace(" ", "_")
        kind = "yr" if code in {"yr", "second_yellow", "second-yellow", "2y", "secondyellow"} else "r"
        name = str(player_name or "Không rõ")
        label = "Thẻ đỏ (2 vàng)" if kind == "yr" else "Thẻ đỏ"
        rp["events"].append([minute_value, side, kind, name, "", ""])
        rp["tl"].append((f"{minute_value}'", "red", side, f"{label}: {name}"))

    if summary_only:
        # Season archives already contain normalized red_cards. Prefer those, and
        # support pre-archive/full records that still store cards_h/cards_a.
        compact_reds = match.get("red_cards")
        if isinstance(compact_reds, list) and compact_reds:
            for card in compact_reds:
                if not isinstance(card, dict):
                    continue
                side = _report_side(card.get("side"), card.get("team"), h, a)
                pid = str(card.get("player_id") or card.get("p") or "")
                name = card.get("player") or card.get("player_name")
                if not name:
                    name = _name(players, pid) if pid else "Không rõ"
                add_red_card(side, card.get("minute", card.get("m", 0)), name,
                             card.get("type") or card.get("c") or "red")
        else:
            for side in ("h", "a"):
                for card in match.get(f"cards_{side}", []) or []:
                    if not isinstance(card, dict) or not _is_red_card_code(card.get("c", card.get("type"))):
                        continue
                    pid = str(card.get("p") or card.get("player_id") or "")
                    name = card.get("player") or _name(players, pid)
                    add_red_card(side, card.get("m", card.get("minute", 0)), name,
                                 card.get("c", card.get("type")))
        # Do not expose yellow cards, substitutions, injuries, VAR, tactical
        # changes, formations, MOTM or player-level stats in these two cups.
        rp["events"].sort(key=lambda e: e[0])
        rp["tl"].sort(key=lambda e: (int(str(e[0]).replace("'", "") or 0),
                                     0 if e[1] == "goal" else 1))
        return rp

    side_team = {"h": h, "a": a}

    def add_event(side, minute, kind, pid, extra="", why=""):
        name = _name(players, pid)
        try:
            minute = int(minute or 0)
        except (TypeError, ValueError, OverflowError):
            minute = 0
        rp["events"].append([minute, side, kind, name, extra, why])
        label = {"y": "Thẻ vàng", "yr": "Thẻ vàng thứ hai", "r": "Thẻ đỏ", "inj": "Chấn thương",
                 "sub": "Thay người"}.get(kind, kind)
        rp["tl"].append((f"{minute}'", "sub" if kind == "sub" else ("injury" if kind == "inj" else
                         "yellow" if kind == "y" else "red"), side,
                         (f"{label}: {name}") + ((" → " + extra) if extra else "")))

    for side in ("h", "a"):
        for card in match.get("cards_" + side, []) or []:
            add_event(side, card.get("m", 0), card.get("c", "y"), card.get("p", ""))
        for inj in match.get("inj_" + side, []) or []:
            add_event(side, inj.get("m", 0), "inj", inj.get("p", ""),
                      "%s, nghỉ khoảng %d trận" % (inj.get("name", "Chấn thương"), inj.get("n", 1)))
        why_map = (match.get("sub_why") or {}).get(side, {})
        for minute, out_pid, in_pid in match.get("subs_" + side, []) or []:
            add_event(side, minute, "sub", out_pid, _name(players, in_pid) if in_pid else "", why_map.get(out_pid, ""))

    for event in match.get("var", []) or []:
        side = _report_side(event.get("t"), None, h, a)
        name = _name(players, event.get("p", "")) if event.get("p") else ""
        text = context.var_text(event, name)
        rp["var"].append([event.get("m", 0), side, event.get("k", "no_change"), text])
        rp["tl"].append((f"{event.get('m', 0)}'", "var_check", side, text))
    for minute, side_value, formation, why in match.get("tactics", []) or []:
        side = _report_side(side_value, None, h, a)
        form_text = coach.formation_text(formation)
        rp["tactics"].append([minute, side, form_text, why])
        rp["tl"].append((f"{minute}'", "tactic", side,
                         "Đổi sang sơ đồ %s (%s)" % (form_text, coach.REASON_TXT.get(why, why))))

    motm = match.get("motm")
    if motm:
        side, pid, rating = motm
        rp["motm"] = [_name(players, pid), side, rating]

    for side, team in (("h", h), ("a", a)):
        player_map = det.get(side, {}) if isinstance(det, dict) else {}
        subs = match.get("subs_" + side, []) or []
        off = {out: minute for minute, out, inn in subs}
        on = {inn: minute for minute, out, inn in subs if inn}
        order = list(match.get("xi_" + side, [])) + [inn for _, _, inn in subs if inn]
        seen = set()
        rows = []
        for pid in order:
            if not pid or pid in seen or pid not in player_map:
                continue
            seen.add(pid)
            d = dict(player_map[pid])
            player = players.get(pid, {})
            rows.append({
                "name": _name(players, pid), "pos": player.get("position", ""),
                "min": d.get("min", 0), "rt": d.get("rt", 6.0),
                "g": d.get("g", 0), "a": d.get("a", 0), "sh": d.get("sh", 0),
                "sot": d.get("sot", 0), "xg": d.get("xg", 0), "pok": d.get("pok", 0),
                "pass": d.get("pass", 0), "kp": d.get("kp", 0), "tkl": d.get("tkl", 0),
                "int": d.get("int", 0), "clr": d.get("clr", 0), "sv": d.get("sv", 0),
                "fl": d.get("fl", 0), "yc": d.get("yc", 0), "rc": d.get("rc", 0),
                "st": pid in match.get("xi_" + side, []), "son": on.get(pid), "soff": off.get(pid)
            })
        rp["players"][side] = rows

    for tag, text in (("kickoff", "Bắt đầu trận đấu"), ("ft", "Kết thúc trận đấu")):
        rp["tl"].append(("0'" if tag == "kickoff" else "90'", tag, "h", text))
    rp["tl"].sort(key=lambda e: (int(str(e[0]).replace("'", "").split("+", 1)[0] or 0),
                                 {"goal": 0, "red": 1, "yellow": 1, "injury": 2, "sub": 3, "var_check": 4}.get(e[1], 9)))
    return rp
