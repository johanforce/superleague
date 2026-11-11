# -*- coding: utf-8 -*-
"""Editorial match news for the Super League simulator.

One completed fixture produces exactly one article.  The article keeps the
editorial angles from the earlier WNL news feed (big win, thriller, late winner,
hat-trick, VAR, red card, injury and upset), while combining supporting match
facts into one readable report instead of printing a raw data line.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any

NEWS_FORMAT_VERSION = 2


def recent_two_week_window(reference_date: date | str):
    """Return Monday-Sunday ranges for the previous and current simulation weeks."""
    if isinstance(reference_date, str):
        reference_date = date.fromisoformat(reference_date[:10])
    current_start = reference_date - timedelta(days=reference_date.weekday())
    previous_start = current_start - timedelta(days=7)
    previous_end = current_start - timedelta(days=1)
    current_end = current_start + timedelta(days=6)
    return {
        "previous": (previous_start, previous_end),
        "current": (current_start, current_end),
    }


def reference_date_from_state(state: dict[str, Any]) -> date:
    """Use the same simulation-calendar anchor as the fixtures dashboard."""
    slots = state.get("slots", []) or []
    pending_dates = []
    for slot in slots:
        if slot.get("played"):
            continue
        parsed = _as_date(slot.get("date"))
        if parsed:
            pending_dates.append(parsed)
    if pending_dates:
        return min(pending_dates)

    played_dates = [
        parsed for match in (state.get("matches", []) or [])
        if (parsed := _as_date(match.get("date"))) is not None
    ]
    if played_dates:
        return max(played_dates)

    season = int(state.get("season", 2026) or 2026)
    return _as_date(state.get("season_start_date")) or date(season, 8, 1)


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def is_in_recent_two_week_window(value, reference_date: date | str) -> bool:
    """Whether a dated record belongs to the current or immediately prior week."""
    item_date = _as_date(value)
    if item_date is None:
        return False
    windows = recent_two_week_window(reference_date)
    return windows["previous"][0] <= item_date <= windows["current"][1]


def retain_recent_news(items, reference_date: date | str):
    """Keep only dated news from the current and previous simulation weeks."""
    if isinstance(items, dict):
        items = list(items.values())
    result = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if is_in_recent_two_week_window(item.get("date"), reference_date):
            result.append(item)
    return result


def filter_recent_match_news(items, reference_date: date | str):
    """Keep match articles from last week and this week in the simulation calendar."""
    return [item for item in retain_recent_news(items, reference_date)
            if item.get("kind") == "match_summary"]


def unique_headlines(items):
    """Return shallow-copied articles with unique headlines inside the displayed feed."""
    seen = set()
    result = []
    for item in items or []:
        row = dict(item)
        headline = str(row.get("headline") or "Tin trận đấu").strip()
        key = headline.casefold()
        if key in seen:
            matchup = " – ".join(x for x in (str(row.get("home") or "").strip(),
                                               str(row.get("away") or "").strip()) if x)
            candidate = f"{headline} · {matchup}" if matchup else headline
            key = candidate.casefold()
            if key in seen:
                candidate = f"{candidate} · {row.get('date', '')} #{row.get('match_id', '')}"
                key = candidate.casefold()
            headline = candidate
        seen.add(key)
        row["headline"] = headline
        result.append(row)
    return result


def _headline(kind: str, text: str, teams=(), priority: float = 1.0, **extra):
    return {
        "kind": kind,
        "text": text,
        "teams": list(teams),
        "priority": round(float(priority), 1),
        **extra,
    }


def _player_name(game, player_id, fallback=None):
    player = game.s.get("players", {}).get(player_id, {}) if player_id else {}
    if player:
        return player.get("common_name") or " ".join(
            x for x in (str(player.get("first_name", "")).strip(),
                        str(player.get("last_name", "")).strip()) if x
        ) or fallback or str(player_id)
    return fallback or (str(player_id) if player_id else "Cầu thủ")


def _minute(value):
    if value is None:
        return "?"
    return str(value)


def _scoreline(match):
    return f"{int(match.get('hg', 0))}–{int(match.get('ag', 0))}"


def _goal_story(game, match):
    goals = sorted(match.get("goals") or [], key=lambda item: int(item.get("minute", 0)))
    if not goals:
        return "Trận đấu khép lại mà không có bàn thắng nào được ghi."
    bits = []
    for goal in goals[:6]:
        scorer = goal.get("scorer") or _player_name(game, goal.get("scorer_id"))
        team = goal.get("team")
        minute = _minute(goal.get("minute"))
        sentence = f"phút {minute}, {scorer}"
        if goal.get("pen"):
            sentence += " thực hiện thành công quả phạt đền"
        else:
            sentence += " lập công"
        if team:
            sentence += f" cho {team}"
        if goal.get("assist"):
            sentence += f" sau đường chuyền của {goal['assist']}"
        bits.append(sentence)
    if len(goals) > 6:
        bits.append(f"cùng {len(goals) - 6} bàn thắng khác")
    return "Các thời điểm đáng chú ý: " + "; ".join(bits) + "."


def _card_story(game, match):
    details = []
    for side, team in (("h", match.get("home", "Chủ nhà")), ("a", match.get("away", "Đội khách"))):
        for card in match.get(f"cards_{side}") or []:
            code = card.get("c")
            if code not in ("r", "yr"):
                continue
            player = _player_name(game, card.get("p"))
            minute = _minute(card.get("m"))
            card_text = "thẻ đỏ trực tiếp" if code == "r" else "thẻ đỏ sau hai thẻ vàng"
            details.append(f"phút {minute}, {player} ({team}) nhận {card_text}")
    return details


def _match_angles(game, match):
    """Return ranked editorial angles; caller selects one dominant story per match."""
    home, away = match.get("home", "Chủ nhà"), match.get("away", "Đội khách")
    hg, ag = int(match.get("hg", 0)), int(match.get("ag", 0))
    margin = abs(hg - ag)
    winner = match.get("winner")
    goals = sorted(match.get("goals") or [], key=lambda item: int(item.get("minute", 0)))
    angles = []

    scorer_counts = Counter()
    scorer_team = {}
    scorer_names = {}
    for goal in goals:
        pid = goal.get("scorer_id") or goal.get("scorer")
        if not pid:
            continue
        scorer_counts[pid] += 1
        scorer_team[pid] = goal.get("team")
        scorer_names[pid] = goal.get("scorer") or _player_name(game, pid)
    hat_tricks = [(pid, count) for pid, count in scorer_counts.items() if count >= 3]
    if hat_tricks:
        pid, count = max(hat_tricks, key=lambda row: row[1])
        name = scorer_names.get(pid) or _player_name(game, pid)
        team = scorer_team.get(pid) or ""
        angles.append((125 + count, "hat_trick", f"Hat-trick đưa {name} thành tâm điểm trận đấu",
                       (team,) if team else (home, away)))

    # Detect a genuine comeback from the chronological score, not merely a late goal.
    running = {home: 0, away: 0}
    ever_trailed = {home: False, away: False}
    comeback = None
    for goal in goals:
        team = goal.get("team")
        if team not in running:
            continue
        opponent = away if team == home else home
        if running[team] < running[opponent]:
            ever_trailed[team] = True
        running[team] += 1
        if winner == team and ever_trailed[team] and running[team] > running[opponent]:
            comeback = (team, opponent, goal)
    if comeback:
        team, opponent, goal = comeback
        angles.append((122, "comeback", f"{team} ngược dòng để giành chiến thắng trước {opponent}",
                       (team, opponent)))

    if winner and margin >= 4:
        loser = away if winner == home else home
        angles.append((110 + margin, "big_win", f"{winner} thắng đậm {loser} với tỷ số {_scoreline(match)}",
                       (winner, loser)))

    late_goals = [g for g in goals if int(g.get("minute", 0)) >= 85 and g.get("team") == winner]
    if winner and late_goals:
        decisive = late_goals[-1]
        angles.append((102, "late_winner",
                       f"Bàn thắng muộn giúp {winner} định đoạt trận đấu ở phút {_minute(decisive.get('minute'))}",
                       (winner, away if winner == home else home)))

    if hg + ag >= 6:
        angles.append((88 + hg + ag, "thriller",
                       f"Màn rượt đuổi tỷ số tạo nên trận cầu {hg + ag} bàn thắng",
                       (home, away)))

    if winner and match.get("match_engine") == "enhanced":
        expected_home = float(match.get("expected_home", 0.5))
        expected_winner = expected_home if winner == home else 1.0 - expected_home
        if expected_winner < 0.38:
            angles.append((84, "upset", f"Bất ngờ lớn: {winner} vượt qua đối thủ được đánh giá cao hơn",
                           (winner, away if winner == home else home)))

    red_cards = _card_story(game, match)
    if red_cards:
        angles.append((80, "red_card", "Thẻ đỏ trở thành bước ngoặt của trận đấu", (home, away)))

    var_events = match.get("var") or []
    if var_events:
        overturned = sum(1 for event in var_events
                         if "overturn" in str(event.get("k", "")) or event.get("res") == "overturned")
        angles.append((72 + len(var_events), "var",
                       "VAR trở thành tâm điểm với " + str(len(var_events)) + " tình huống được xem xét",
                       (home, away)))

    injuries = (match.get("inj_h") or []) + (match.get("inj_a") or [])
    if injuries:
        angles.append((62, "injury", "Chấn thương khiến trận đấu có thêm nốt trầm", (home, away)))

    if not angles:
        if not winner:
            title = f"{home} và {away} chia điểm sau trận hòa {_scoreline(match)}"
            kind = "draw"
        elif margin == 1:
            title = f"{winner} giành chiến thắng sít sao trong cuộc đấu cân tài"
            kind = "narrow_win"
        else:
            title = f"{winner} có khởi đầu thuận lợi với chiến thắng {_scoreline(match)}"
            kind = "result"
        angles.append((45, kind, title, (home, away)))
    return sorted(angles, key=lambda row: (-row[0], row[1]))


def match_summary(game, match: dict[str, Any]):
    """Build one editorial-style match article, preserving exactly one item per fixture."""
    home, away = match.get("home", "Chủ nhà"), match.get("away", "Đội khách")
    hg, ag = int(match.get("hg", 0)), int(match.get("ag", 0))
    winner = match.get("winner")
    comp = match.get("comp", "Giải đấu")
    round_label = match.get("round", match.get("stage", "Trận đấu"))
    date = str(match.get("date", ""))
    score = _scoreline(match)
    angles = _match_angles(game, match)
    priority, angle, headline, teams = angles[0]

    if winner:
        loser = away if winner == home else home
        if angle == "comeback":
            opening = f"{winner} đã tạo nên màn ngược dòng để đánh bại {loser} với tỷ số {score}."
        elif angle == "big_win":
            opening = f"{winner} áp đảo và khép lại trận đấu bằng chiến thắng đậm {score} trước {loser}."
        elif angle == "late_winner":
            opening = f"{winner} phải chờ tới những phút cuối mới khuất phục được {loser}, giành chiến thắng {score}."
        elif angle == "upset":
            opening = f"{winner} tạo bất ngờ khi vượt qua {loser} với tỷ số {score}, dù không được đánh giá cao trước giờ bóng lăn."
        elif angle == "hat_trick":
            opening = f"{winner} giành chiến thắng {score} trước {loser}, trong ngày một cá nhân đặc biệt tỏa sáng."
        else:
            opening = f"{winner} giành trọn ba điểm sau khi vượt qua {loser} với tỷ số {score}."
    else:
        opening = f"{home} và {away} bất phân thắng bại trong trận hòa {score}, mỗi đội rời sân với một điểm."

    paragraphs = [opening, _goal_story(game, match)]

    red_cards = _card_story(game, match)
    if red_cards:
        paragraphs.append("Trận đấu có biến cố về nhân sự: " + "; ".join(red_cards) + ".")

    var_events = match.get("var") or []
    if var_events:
        overturns = sum(1 for event in var_events
                        if "overturn" in str(event.get("k", "")) or event.get("res") == "overturned")
        paragraphs.append(f"Tổ VAR phải vào cuộc {len(var_events)} lần; {overturns} quyết định được thay đổi sau khi xem lại tình huống.")

    injuries = (match.get("inj_h") or []) + (match.get("inj_a") or [])
    if injuries:
        injury_bits = []
        for injury in injuries[:2]:
            name = _player_name(game, injury.get("p"))
            team = home if injury in (match.get("inj_h") or []) else away
            injury_bits.append(f"{name} ({team}) gặp {injury.get('name', 'chấn thương')}, dự kiến nghỉ {injury.get('n', 1)} trận")
        paragraphs.append("Đáng chú ý, " + "; ".join(injury_bits) + ".")

    pens = match.get("pens")
    if isinstance(pens, dict) and "home" in pens and "away" in pens:
        advance = f" {winner} giành quyền đi tiếp." if winner else ""
        paragraphs.append(f"Hai đội phải phân định thắng thua trên chấm luân lưu; tỷ số là {pens['home']}–{pens['away']}.{advance}")

    # Add one concise, meaningful statistical angle rather than dumping a stat sheet.
    stats = match.get("advanced_stats") or match.get("stats") or {}
    possession = stats.get("poss") or stats.get("possession")
    shots_on_target = stats.get("sot")
    xg = stats.get("xg")
    stat_bits = []
    if isinstance(possession, (list, tuple)) and len(possession) >= 2:
        stat_bits.append(f"kiểm soát bóng {possession[0]}%–{possession[1]}%")
    if isinstance(shots_on_target, (list, tuple)) and len(shots_on_target) >= 2:
        stat_bits.append(f"sút trúng đích {shots_on_target[0]}–{shots_on_target[1]}")
    if isinstance(xg, (list, tuple)) and len(xg) >= 2:
        try:
            stat_bits.append(f"xG {float(xg[0]):.2f}–{float(xg[1]):.2f}")
        except (TypeError, ValueError):
            pass
    if len(stat_bits) >= 2:
        paragraphs.append("Các con số đáng chú ý: " + ", ".join(stat_bits[:2]) + ".")

    motm = match.get("motm")
    motm_text = None
    if isinstance(motm, (list, tuple)) and len(motm) >= 2:
        side, player_id = motm[0], motm[1]
        rating = motm[2] if len(motm) >= 3 else None
        player = _player_name(game, player_id)
        team = home if side in ("h", "home", "A") else away
        motm_text = f"Danh hiệu Cầu thủ xuất sắc nhất trận thuộc về {player} ({team}"
        if rating is not None:
            motm_text += f", {float(rating):.1f} điểm"
        motm_text += ")."
        paragraphs.append(motm_text)

    body = " ".join(paragraphs)
    # Keep the text field for compatibility with older clients while exposing
    # headline/body separately for the richer news-card renderer.
    text = f"{date} · {comp} · {round_label}\n{home} {score} {away}\n{headline}. {body}"
    return _headline(
        "match_summary", text, teams, priority,
        headline=headline, body=body, angle=angle, format_version=NEWS_FORMAT_VERSION,
        match_id=int(match.get("id", 0)),
        season=int(match.get("season", game.s.get("season", 0))),
        date=date, comp=comp, round=round_label, score=score, home=home, away=away,
    )


def round_news(game, slot: dict[str, Any], matches: list[dict[str, Any]], limit: int | None = None):
    """Return exactly one editorial article per completed fixture in the slot."""
    # Deliberately do not cap by the legacy global limit: five fixtures = five articles.
    return [match_summary(game, match) for match in matches]


def season_news(game, record: dict[str, Any], limit: int = 14):
    """Generate a season wrap-up with title winners, promotions, awards and surprises."""
    items = []
    label = record.get("label", str(record.get("season", "")))
    if record.get("champion_premier"):
        items.append(_headline("champion", f"{record['champion_premier']} đăng quang Premier League mùa {label}.",
                               (record["champion_premier"],), 120))
    if record.get("champion_cup"):
        items.append(_headline("cup_winner", f"{record['champion_cup']} giành chức vô địch Cup mùa {label}.",
                               (record["champion_cup"],), 105))
    if record.get("champion_c1"):
        items.append(_headline("c1_winner", f"{record['champion_c1']} trở thành nhà vô địch C1 mùa {label}.",
                               (record["champion_c1"],), 110))
    scorers = record.get("top_scorers") or []
    if scorers:
        p = scorers[0]
        items.append(_headline("top_scorer", f"Vua phá lưới mùa {label}: {p['player']} ({p['team']}) với {p['goals']} bàn.",
                               (p["team"],), 90))
    assists = record.get("top_assists") or []
    if assists:
        p = assists[0]
        items.append(_headline("top_assist", f"Vua kiến tạo mùa {label}: {p['player']} ({p['team']}) với {p['assists']} lần kiến tạo.",
                               (p["team"],), 78))
    for x in record.get("promoted", []):
        items.append(_headline("promotion", f"{x['team']} giành quyền thăng hạng từ {x['from'].upper()} lên {x['to'].upper()}.",
                               (x["team"],), 72))
    for x in record.get("relegated", []):
        items.append(_headline("relegation", f"{x['team']} phải xuống hạng từ {x['from'].upper()} xuống {x['to'].upper()}.",
                               (x["team"],), 70))
    candidates = [p for p in scorers if p.get("apps", 0) >= 8 and p.get("avg_rating") is not None]
    if candidates:
        best = max(candidates, key=lambda p: (p["avg_rating"], p.get("minutes", 0), p["goals"]))
        items.append(_headline("player_of_season", f"Cầu thủ có điểm trung bình cao nhất: {best['player']} ({best['team']}) — {best['avg_rating']:.2f} sau {best['apps']} trận.",
                               (best["team"],), 86))
    items.sort(key=lambda x: (-x["priority"], x["kind"]))
    return items[:limit]


def potential_star_weekly_news(game, reference_date: date | str, limit: int = 3):
    """Create press stories for genuinely standout high-potential young players.

    The function only reads completed match logs. It reports the current
    simulation week when that week has completed fixtures; otherwise it falls
    back to the previous simulation week so a completed week's stories remain
    visible after the calendar advances. No candidate means no article.
    """
    if isinstance(reference_date, str):
        try:
            reference_date = date.fromisoformat(reference_date[:10])
        except ValueError:
            return []

    windows = recent_two_week_window(reference_date)
    completed = []
    for match in game.s.get("matches", []):
        if not match.get("played"):
            continue
        try:
            match_date = date.fromisoformat(str(match.get("date", ""))[:10])
        except ValueError:
            continue
        completed.append((match_date, match))

    current_start, current_end = windows["current"]
    previous_start, previous_end = windows["previous"]
    weekly_matches = [(d, m) for d, m in completed if current_start <= d <= current_end]
    if not weekly_matches:
        weekly_matches = [(d, m) for d, m in completed if previous_start <= d <= previous_end]
    if not weekly_matches:
        return []

    players = game.s.get("players", {})
    player_meta = game.s.get("player_meta", {})
    aggregate = {}
    competitions = {}

    for match_date, match in weekly_matches:
        teams_by_side = {"h": match.get("home", ""), "a": match.get("away", "")}
        for side, team in teams_by_side.items():
            side_stats = (match.get("player_stats") or {}).get(side, {}) or {}
            if not isinstance(side_stats, dict):
                continue
            for pid, stats in side_stats.items():
                if not isinstance(stats, dict):
                    continue
                try:
                    minutes = max(0, int(stats.get("min", 0) or 0))
                except (TypeError, ValueError):
                    minutes = 0
                if minutes <= 0:
                    continue
                row = aggregate.setdefault(str(pid), {
                    "apps": 0, "minutes": 0, "rating_sum": 0.0, "rating_minutes": 0,
                    "goals": 0, "assists": 0, "motm": 0, "team": team,
                    "matches": [],
                })
                row["apps"] += 1
                row["minutes"] += minutes
                row["team"] = team or row["team"]
                try:
                    rating = float(stats.get("rt", 6.0))
                except (TypeError, ValueError):
                    rating = 6.0
                row["rating_sum"] += rating * minutes
                row["rating_minutes"] += minutes
                row["goals"] += int(stats.get("g", 0) or 0)
                row["assists"] += int(stats.get("a", 0) or 0)
                row["matches"].append(match)
                comp = str(match.get("comp") or "Giải đấu")
                competitions.setdefault(str(pid), set()).add(comp)

        motm = match.get("motm")
        if isinstance(motm, (list, tuple)) and len(motm) >= 2 and motm[1]:
            pid = str(motm[1])
            if pid in aggregate:
                aggregate[pid]["motm"] += 1

    candidates = []
    for pid, row in aggregate.items():
        meta = player_meta.get(pid, {}) or {}
        player = players.get(pid, {}) or {}
        try:
            age = float(meta.get("age", player.get("age", 99)))
            potential = float(meta.get("potential", player.get("potential", 0)))
        except (TypeError, ValueError):
            continue
        if age > 23 or potential < 85 or row["minutes"] < 45:
            continue
        avg_rating = row["rating_sum"] / max(1, row["rating_minutes"])
        goal_contributions = row["goals"] + row["assists"]
        shining = (avg_rating >= 8.0 or row["motm"] > 0 or
                   (goal_contributions >= 1 and avg_rating >= 7.2))
        if not shining:
            continue
        score = (avg_rating * 10.0 + row["goals"] * 3.0 + row["assists"] * 2.2
                 + row["motm"] * 4.0 + max(0.0, potential - 85.0) * 0.15)
        name = (player.get("common_name") or
                " ".join(x for x in (str(player.get("first_name", "")).strip(),
                                     str(player.get("last_name", "")).strip()) if x) or pid)
        candidates.append({
            "player_id": pid, "player": name, "team": row["team"], "age": age,
            "potential": potential, "apps": row["apps"], "minutes": row["minutes"],
            "avg_rating": avg_rating, "goals": row["goals"], "assists": row["assists"],
            "motm": row["motm"], "score": score,
            "competitions": sorted(competitions.get(pid, set())),
        })

    candidates.sort(key=lambda item: (-item["score"], -item["potential"], item["player"].casefold()))
    start = min(d for d, _m in weekly_matches)
    end = max(d for d, _m in weekly_matches)
    week_label = (f"{start.strftime('%d/%m')} – {end.strftime('%d/%m/%Y')}"
                  if start != end else start.strftime("%d/%m/%Y"))

    articles = []
    for item in candidates[:max(0, int(limit))]:
        contributions = []
        if item["goals"]:
            contributions.append(f"{item['goals']} bàn")
        if item["assists"]:
            contributions.append(f"{item['assists']} kiến tạo")
        if item["motm"]:
            contributions.append(f"{item['motm']} lần Cầu thủ xuất sắc nhất trận")
        contribution_text = ", ".join(contributions) if contributions else "màn trình diễn nổi bật"
        comp_text = ", ".join(item["competitions"]) or "các đấu trường"
        headline = f"Tài năng trẻ {item['player']} tỏa sáng trong tuần"
        body = (
            f"Cầu thủ {item['player']} ({item['team']}) tiếp tục cho thấy tiềm năng phát triển "
            f"với {contribution_text}. Trong {item['apps']} trận và {item['minutes']} phút thi đấu "
            f"ở {comp_text}, cầu thủ này đạt điểm trung bình {item['avg_rating']:.2f}/10. "
            f"Ở tuổi {item['age']:.0f}, hồ sơ tiềm năng hiện ghi nhận mức {item['potential']:.1f}."
        )
        articles.append(_headline(
            "potential_star_weekly", headline, (item["team"],), item["score"],
            headline=headline, body=body, date=week_label, comp="Tổng hợp tuần",
            round="Tài năng trẻ", player_id=item["player_id"], player=item["player"],
            team=item["team"], age=item["age"], potential=item["potential"],
            apps=item["apps"], minutes=item["minutes"], avg_rating=round(item["avg_rating"], 2),
            goals=item["goals"], assists=item["assists"], motm=item["motm"],
            competitions=item["competitions"],
        ))
    return articles
