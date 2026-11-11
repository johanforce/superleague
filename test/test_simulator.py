# -*- coding: utf-8 -*-
import tempfile
import json
import unittest
import sl_game as sl_game_module
from collections import Counter
from pathlib import Path
from datetime import date, timedelta

from economy.core import SquadRulesService
from sl_game import (Game, load_source_database, POSITION_TEMPLATE, make_double_round_robin, TIERS,
                     load_award_settings_file, AWARD_SETTINGS_DEFAULTS)
from sl_database import SuperLeagueDatabase
import recalculate_awards as award_tool
from main import run_engine_calibration, _assert_match_schedule
from sl_match.adapter import restore_live_bundle, report_for_match
from sl_match.live import LiveMatch
from sl_match.live_ui import ConcurrentFixtureTracker, LivePage, concurrent_fixture_detail_text
from sl_match import stats as wnl_stats
from sl_match import engine as match_engine
from sl_match.news import (
    round_news as build_round_news,
    filter_recent_match_news,
    recent_two_week_window,
    retain_recent_news,
    reference_date_from_state,
    unique_headlines,
)
from sl_gui import BG, SIDEBAR, SURFACE, SURFACE_2, RAISED, SELECT, TEXT, MUTED, DIM, _UpcomingMatchGameProxy
from sl_match.report_ui import EVENT_STYLES

ROOT = Path(__file__).resolve().parents[1]


class LiveMatchDetailTests(unittest.TestCase):
    def test_fixture_details_show_each_scorer_but_hide_empty_red_card_section(self):
        row = {
            "comp": "Premier League", "home": "Alpha", "away": "Beta",
            "score": "2–1", "status": "56' · LIVE",
            "goal_home": [
                {"player": "Home Scorer", "minute": "12'", "pen": False},
                {"player": "Home Penalty", "minute": "45+2'", "pen": True},
            ],
            "goal_away": [{"player": "Away Scorer", "minute": "51'", "pen": False}],
            "red_home": [], "red_away": [],
        }
        text = concurrent_fixture_detail_text(row)
        self.assertIn("Alpha: 12' Home Scorer, 45+2' Home Penalty (pen)", text)
        self.assertIn("Beta: 51' Away Scorer", text)
        self.assertNotIn("THẺ ĐỎ", text)
        self.assertNotIn("Chưa có thẻ đỏ", text)

        row["red_away"] = [{"player": "Sent Off", "minute": "53'"}]
        row["red_home"] = ["Home Sent Off 70'"]  # legacy saved-scoreboard representation
        text_with_red = concurrent_fixture_detail_text(row)
        self.assertIn("THẺ ĐỎ", text_with_red)
        self.assertIn("Đội khách: Sent Off (53')", text_with_red)
        self.assertIn("Chủ nhà: Home Sent Off 70'", text_with_red)


class LiveStandingsUiTests(unittest.TestCase):
    def test_phase_two_live_tab_renders_both_champion_and_challenge_tables(self):
        class FakeTree:
            def __init__(self):
                self.rows = []
            def winfo_exists(self):
                return True
            def get_children(self):
                return [str(index) for index, _row in enumerate(self.rows)]
            def delete(self, *_items):
                self.rows = []
            def insert(self, _parent, _index, values, tags=()):
                self.rows.append((tuple(values), tuple(tags)))
                return str(len(self.rows))

        class FakeLabel:
            def __init__(self):
                self.value = ""
            def configure(self, **kwargs):
                self.value = kwargs.get("text", self.value)

        class FakeGame:
            companion_previews = []
            def __init__(self):
                self.received = None
            def live_league_table(self, preview, provisional):
                self.received = (preview, provisional)
                return {
                    "available": True,
                    "title": "BXH LIVE · PREMIER · CHAMPION & CHALLENGE",
                    "note": "Bảo lưu điểm giai đoạn 1 và cộng tỷ số tạm thời giai đoạn 2.",
                    "sections": [
                        {"key": "champion", "title": "CHAMPION", "live_teams": {"Alpha"},
                         "rows": [{"team": "Alpha", "p": 2, "w": 1, "d": 1, "l": 0,
                                   "gf": 3, "ga": 1, "gd": 2, "pts": 4}]},
                        {"key": "challenge", "title": "CHALLENGE", "live_teams": set(),
                         "rows": [{"team": "Beta", "p": 2, "w": 0, "d": 1, "l": 1,
                                   "gf": 1, "ga": 2, "gd": -1, "pts": 1}]},
                    ],
                }

        page = LivePage.__new__(LivePage)
        page.entry = {"replay": False}
        page.committed = False
        page.m = {"comp": "Premier League", "stage": "league_p2_premier_champion_2",
                  "home": "Alpha", "away": "Gamma", "_meta": {"group": "champion"}}
        page.score = [2, 1]
        page.T = 55.0
        page.g = FakeGame()
        page.concurrent_trackers = {}
        page.live_standings_tree = FakeTree()
        page.live_standings_title = FakeLabel()
        page.live_standings_note = FakeLabel()

        page.refresh_live_standings()

        self.assertEqual(page.g.received[1][0]["hg"], 2)
        self.assertEqual(page.g.received[1][0]["ag"], 1)
        section_titles = [row[0][1] for row in page.live_standings_tree.rows if "section" in row[1]]
        self.assertEqual(section_titles, ["CHAMPION", "CHALLENGE"])
        live_rows = [row for row in page.live_standings_tree.rows if "live" in row[1]]
        self.assertEqual(len(live_rows), 1)
        self.assertIn("Alpha", live_rows[0][0][1])


class SimulatorTests(unittest.TestCase):
    def test_source_csv_is_exactly_24_squads(self):
        from sl_database import SuperLeagueDatabase
        fields, rows, clubs = load_source_database(SuperLeagueDatabase(ROOT / "memory" / "super_league.db"))
        self.assertEqual(len(fields), 61)
        self.assertEqual(len(rows), 552)
        self.assertEqual(len(clubs), 24)
        for team, squad in clubs.items():
            self.assertEqual(len(squad), 23, team)
            self.assertEqual(dict(Counter(p["position"] for p in squad)), POSITION_TEMPLATE, team)

    def test_double_round_robin(self):
        teams = ["A", "B", "C", "D", "E", "F", "G", "H"]
        rounds = make_double_round_robin(teams, seed=123)
        self.assertEqual(len(rounds), 14)
        pairs = Counter()
        venue = {t: [] for t in teams}
        for round_matches in rounds:
            self.assertEqual(len(round_matches), 4)
            seen = set()
            for home, away in round_matches:
                self.assertNotIn(home, seen); self.assertNotIn(away, seen)
                seen.update((home, away))
                pairs[tuple(sorted((home, away)))] += 1
                venue[home].append("H"); venue[away].append("A")
            self.assertEqual(len(seen), 8)
        self.assertEqual(len(pairs), 28)
        self.assertTrue(all(count == 2 for count in pairs.values()))
        for team, seq in venue.items():
            self.assertFalse(any(seq[i] == seq[i+1] == seq[i+2] for i in range(len(seq)-2)), team)

    def test_champions_points_hierarchy_and_playoff_awards(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261031)

            # FC1 champion is deliberately level with Premier 7th; FC2 champion
            # remains above FC1 7th under the current qualification ladder.
            self.assertEqual(game._league_points("fc1", 1), game._league_points("premier", 7))
            self.assertGreater(game._league_points("fc2", 1), game._league_points("fc1", 7))

            # A play-off winner receives the promoted second-place award even
            # when it originally finished 3rd/4th; a defeated 2nd-placed team
            # does not get that award.
            phase2 = game.s["league_phase2"]
            phase2["promotion_playoffs"]["fc1"]["winner"] = "FC1 Playoff Winner"
            phase2["promotion_playoffs"]["fc2"]["winner"] = "FC2 Playoff Winner"
            self.assertEqual(game._league_points("fc1", 3, "FC1 Playoff Winner"), 74)
            self.assertEqual(game._league_points("fc2", 4, "FC2 Playoff Winner"), 34)
            self.assertGreater(game._league_points("fc1", 3, "FC1 Playoff Winner"),
                               game._league_points("premier", 8))
            self.assertGreater(game._league_points("fc2", 4, "FC2 Playoff Winner"),
                               game._league_points("fc1", 8))
            self.assertLess(game._league_points("fc1", 2, "FC1 Runner-up Loser"),
                            game._league_points("premier", 8))
            self.assertLess(game._league_points("fc2", 2, "FC2 Runner-up Loser"),
                            game._league_points("fc1", 8))

            # Cup bonuses remain a separate addition to league points.
            self.assertEqual(game.config["points"]["cup"],
                             {"winner": 40, "final": 30, "sf": 22, "qf": 14, "r16": 8, "r1": 3})

    def test_best_xi_prioritizes_champions_league_and_premier_winners(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261101)
            game.s["competition_awards"] = {
                "premier": {"completed": True, "champion": "Premier Winner"},
                "c1": {"completed": True, "champion": "C1 Winner"},
            }
            roles = [
                ("GK", "GK"), ("LB", "LB"), ("CB1", "CB"), ("CB2", "CB"), ("RB", "RB"),
                ("LM", "LM"), ("CM1", "CM"), ("CM2", "CM"), ("RM", "RM"),
                ("ST1", "ST"), ("ST2", "ST"),
            ]
            candidates = []
            for index, (name, position) in enumerate(roles):
                candidates.append({"player_id": f"base-{index}", "player": f"Base {name}",
                                   "team": "Neutral Club", "position": position, "apps": 30,
                                   "minutes": 2400, "avg_rating": 8.0, "vote_score": 80.0,
                                   "award_eligible": True})
            candidates.extend([
                {"player_id": "c1-gk", "player": "C1 keeper", "team": "C1 Winner", "position": "GK",
                 "apps": 30, "minutes": 2400, "avg_rating": 8.0, "vote_score": 76.0,
                 "award_eligible": True},
                {"player_id": "pl-st", "player": "Premier striker", "team": "Premier Winner", "position": "ST",
                 "apps": 30, "minutes": 2400, "avg_rating": 8.0, "vote_score": 77.0,
                 "award_eligible": True},
            ])
            xi = game._make_best_xi_442(candidates)
            keeper = next(row for row in xi if row["role"] == "GK")
            striker_ids = {row["player_id"] for row in xi if row["role"] == "ST"}
            self.assertEqual(keeper["player_id"], "c1-gk")
            self.assertIn("pl-st", striker_ids)
            self.assertEqual(keeper["best_xi_bonus"], 5.0)
            self.assertEqual(next(row for row in xi if row["player_id"] == "pl-st")["best_xi_bonus"], 4.0)
            self.assertEqual(len(xi), 11)
            self.assertEqual(len({row["player_id"] for row in xi}), 11)

    def test_best_xi_adds_runner_up_and_major_cup_honors(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261101)
            game.s["competition_awards"] = {
                "premier": {"completed": True, "champion": "Premier Winner", "runner_up": "Premier Runner"},
                "c1": {"completed": True, "champion": "C1 Winner", "runner_up": "C1 Runner"},
                "cup": {"completed": True, "champion": "Major Winner", "runner_up": "Major Runner"},
            }
            honors = game._season_champion_teams()
            expected = {
                "Premier Winner": 4.0,
                "C1 Winner": 5.0,
                "C1 Runner": 4.0,
                "Premier Runner": 3.0,
                "Major Winner": 3.0,
            }
            for club, expected_bonus in expected.items():
                bonus, labels = game._best_xi_honor_bonus(club, honors)
                self.assertEqual(bonus, expected_bonus, club)
                self.assertTrue(labels, club)
            self.assertEqual(expected["Premier Runner"], expected["Major Winner"])

            # Legacy seasons without competition_awards must derive the runner-up
            # from the final Champion table, not the phase-one 2nd place.
            pair_results = [
                ("Phase2 Champion", "Phase2 Runner", 0, 1),
                ("Phase2 Champion", "Third", 0, 1),
                ("Phase2 Champion", "Fourth", 0, 1),
                ("Phase2 Runner", "Third", 2, 0),
                ("Phase2 Runner", "Fourth", 2, 0),
                ("Third", "Fourth", 2, 0),
            ]
            legacy_matches = []
            for idx, (home, away, hg, ag) in enumerate(pair_results * 2, 1):
                legacy_matches.append({"id": idx, "played": True, "comp": "Premier League",
                                       "stage": f"league_p2_premier_champion_{(idx - 1) // 2 + 1}",
                                       "home": home, "away": away, "hg": hg, "ag": ag})
            legacy_record = {
                "champion_premier": "Phase2 Runner",
                "league_phase2": {"premier": {
                    "champion_teams": ["Phase2 Champion", "Phase2 Runner", "Third", "Fourth"],
                    "phase1_table": [
                        {"team": "Phase2 Champion", "pts": 20, "gf": 22, "ga": 8, "gd": 14},
                        {"team": "Phase2 Runner", "pts": 18, "gf": 20, "ga": 9, "gd": 11},
                        {"team": "Third", "pts": 13, "gf": 14, "ga": 11, "gd": 3},
                        {"team": "Fourth", "pts": 10, "gf": 11, "ga": 14, "gd": -3},
                    ]}},
                "matches": legacy_matches,
            }
            legacy_honors = game._season_champion_teams(legacy_record)
            self.assertEqual(legacy_honors["premier_runner_up"], "Third")

            slots = [("GK", "GK"), ("LB", "LB"), ("CB1", "CB"), ("CB2", "CB"),
                     ("RB", "RB"), ("LM", "LM"), ("CM1", "CM"), ("CM2", "CM"),
                     ("RM", "RM"), ("ST1", "ST"), ("ST2", "ST")]
            candidates = [{"player_id": f"base-{i}", "player": f"Base {name}", "team": "Neutral",
                           "position": pos, "apps": 30, "minutes": 2400, "avg_rating": 8.0,
                           "vote_score": 80.0, "award_eligible": True}
                          for i, (name, pos) in enumerate(slots)]
            candidates.extend([
                {"player_id": "major-gk", "player": "Major keeper", "team": "Major Winner",
                 "position": "GK", "apps": 30, "minutes": 2400, "avg_rating": 8.0,
                 "vote_score": 77.5, "award_eligible": True},
                {"player_id": "c1-runner-lb", "player": "C1 runner LB", "team": "C1 Runner",
                 "position": "LB", "apps": 30, "minutes": 2400, "avg_rating": 8.0,
                 "vote_score": 76.5, "award_eligible": True},
                {"player_id": "prem-runner-st", "player": "Premier runner ST", "team": "Premier Runner",
                 "position": "ST", "apps": 30, "minutes": 2400, "avg_rating": 8.0,
                 "vote_score": 77.5, "award_eligible": True},
            ])
            xi = game._make_best_xi_442(candidates)
            picked = {row["player_id"]: row for row in xi}
            self.assertIn("major-gk", picked)
            self.assertIn("c1-runner-lb", picked)
            self.assertIn("prem-runner-st", picked)
            self.assertEqual(picked["major-gk"]["best_xi_bonus"], 3.0)
            self.assertEqual(picked["c1-runner-lb"]["best_xi_bonus"], 4.0)
            self.assertEqual(picked["prem-runner-st"]["best_xi_bonus"], 3.0)

    def test_best_xi_442_never_cross_fills_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261101)
            # Deliberately omit a goalkeeper and a right-back. A highly-rated
            # midfielder/forward must never be used to fill either vacancy.
            position_rows = [
                ("LB", "LB"), ("CB1", "CB"), ("CB2", "CB"),
                ("LM", "LM"), ("CM1", "CM"), ("CM2", "CDM"), ("RM", "RM"),
                ("ST1", "ST"), ("ST2", "CF"),
                ("Extra CAM", "CAM"), ("Extra RW", "RW"),
            ]
            candidates = [{
                "player_id": f"p-{index}", "player": name, "team": "Eligible Club",
                "position": position, "apps": 30, "minutes": 2400,
                "avg_rating": 9.5 if name.startswith("Extra") else 8.0,
                "vote_score": 99.0 if name.startswith("Extra") else 80.0,
                "award_eligible": True,
            } for index, (name, position) in enumerate(position_rows)]
            xi = game._make_best_xi_442(candidates)
            roles = [row["role"] for row in xi]
            self.assertEqual(roles.count("GK"), 0, "Không được lấy cầu thủ ngoài sân làm GK")
            self.assertEqual(roles.count("RB"), 0, "Không được lấy cầu thủ khác vị trí làm RB")
            allowed = {
                "GK": {"GK", "G"}, "LB": {"LB", "LWB"}, "CB": {"CB", "SW"},
                "RB": {"RB", "RWB"}, "LM": {"LM", "LW"},
                "CM": {"CM", "CDM", "CAM", "DM", "AM", "LCM", "RCM"},
                "RM": {"RM", "RW"}, "ST": {"ST", "CF", "FW"},
            }
            self.assertTrue(all(row["position"].upper() in allowed[row["role"]] for row in xi))

    def test_best_xi_uses_c1_champion_same_position_as_fallback_outside_top20(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261101)
            champion_teams = {"c1": "C1 Winner"}
            # The Ballon Top 20 has no goalkeeper. It otherwise contains enough
            # outfield players to fill the other ten positions correctly.
            top_positions = [
                "LB", "CB", "CB", "RB", "LM", "CM", "CM", "RM", "ST", "ST",
                "LB", "CB", "RB", "LM", "CM", "RM", "ST", "CB", "LB", "RB",
            ]
            candidates = []
            for i, position in enumerate(top_positions):
                candidates.append({"player_id": f"top-{i}", "player": f"Top {i}",
                                   "team": "Other Club", "position": position,
                                   "apps": 30, "minutes": 2400, "avg_rating": 8.0,
                                   "vote_score": 100.0 - i, "award_eligible": True})
            # A higher-scoring goalkeeper at another club must not be used as fallback.
            candidates.append({"player_id": "neutral-gk", "player": "Neutral Keeper",
                               "team": "Neutral Club", "position": "GK", "apps": 30,
                               "minutes": 2400, "avg_rating": 8.5, "vote_score": 70.0,
                               "award_eligible": True})
            # This C1-winning-club goalkeeper is outside the Top 20 but meets the
            # same >20 appearances and annual competition eligibility gates.
            candidates.append({"player_id": "c1-winner-gk", "player": "C1 Winner Keeper",
                               "team": "C1 Winner", "position": "GK", "apps": 24,
                               "minutes": 1900, "avg_rating": 7.4, "vote_score": 10.0,
                               "award_eligible": True})
            xi = game._make_best_xi_442(candidates, champion_teams=champion_teams)
            keeper = next(row for row in xi if row["role"] == "GK")
            self.assertEqual(keeper["player_id"], "c1-winner-gk")
            self.assertTrue(keeper["best_xi_fallback"])
            self.assertEqual(keeper["best_xi_selection_source"], "c1_champion_fallback")
            self.assertIn("CLB vô địch Champions League", " ".join(keeper["team_honors"]))
            self.assertNotIn("neutral-gk", {row["player_id"] for row in xi})
            self.assertEqual(len(xi), 11)
            self.assertEqual(len({row["player_id"] for row in xi}), 11)

    def test_best_xi_c1_fallback_still_requires_annual_eligibility_and_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261101)
            roles = [("LB", "LB"), ("CB1", "CB"), ("CB2", "CB"), ("RB", "RB"),
                     ("LM", "LM"), ("CM1", "CM"), ("CM2", "CM"), ("RM", "RM"),
                     ("ST1", "ST"), ("ST2", "ST")]
            candidates = [{"player_id": f"top-{i}", "player": f"Top {name}", "team": "Other",
                           "position": pos, "apps": 30, "minutes": 2400, "avg_rating": 8.0,
                           "vote_score": 100.0 - i, "award_eligible": True}
                          for i, (name, pos) in enumerate(roles)]
            # Neither candidate may fill GK: one is below 21 apps, the other is
            # positionally wrong despite being at the C1-winning club.
            candidates.extend([
                {"player_id": "c1-young-gk", "player": "C1 GK 20 apps", "team": "C1 Winner",
                 "position": "GK", "apps": 20, "minutes": 1700, "vote_score": 20.0,
                 "award_eligible": False},
                {"player_id": "c1-cb", "player": "C1 Winner CB", "team": "C1 Winner",
                 "position": "CB", "apps": 25, "minutes": 2200, "vote_score": 19.0,
                 "award_eligible": True},
            ])
            xi = game._make_best_xi_442(candidates, champion_teams={"c1": "C1 Winner"})
            self.assertFalse(any(row["role"] == "GK" for row in xi))
            self.assertNotIn("c1-young-gk", {row["player_id"] for row in xi})
            self.assertTrue(all(row["award_eligible"] and row["apps"] > 20 for row in xi))

    def test_annual_award_eligibility_requires_21_apps_and_premier_champion_or_c1_semi(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261101)
            # This test verifies the canonical 21-appearance boundary, independent
            # of the user's saved award_settings.json (which may intentionally differ).
            game.award_settings = dict(AWARD_SETTINGS_DEFAULTS)
            players = {
                "prem-21": {"common_name": "Premier eligible", "position": "ST", "overall_rating": 80},
                "prem-20": {"common_name": "Only twenty", "position": "ST", "overall_rating": 80},
                "challenge": {"common_name": "Challenge only", "position": "ST", "overall_rating": 80},
                "c1-eligible": {"common_name": "C1 semi eligible", "position": "CM", "overall_rating": 80},
                "c1-nonsemi": {"common_name": "C1 group only", "position": "CM", "overall_rating": 80},
                "plain": {"common_name": "No route", "position": "GK", "overall_rating": 80},
                "prem-phase1": {"common_name": "Premier phase one", "position": "RM", "overall_rating": 80},
                "c1-no-appearance": {"common_name": "C1 no appearance", "position": "LB", "overall_rating": 80},
                "c1-playin-only": {"common_name": "C1 play-in only", "position": "RW", "overall_rating": 80},
            }
            game.s["players"].update(players)
            game.s["rosters"].setdefault("Premier Champion Club", []).append("prem-21")
            game.s["rosters"].setdefault("Premier Champion Club", []).append("prem-20")
            game.s["rosters"].setdefault("Challenge Club", []).append("challenge")
            game.s["rosters"].setdefault("C1 Semi Club", []).append("c1-eligible")
            game.s["rosters"].setdefault("C1 Group Club", []).append("c1-nonsemi")

            def played_match(comp, stage, round_label, home, away, home_ids=(), away_ids=(), match_id=1):
                return {
                    "id": match_id, "played": True, "comp": comp, "stage": stage,
                    "round": round_label, "home": home, "away": away, "hg": 1, "ag": 0,
                    "minutes_h": {pid: 90 for pid in home_ids},
                    "minutes_a": {pid: 90 for pid in away_ids},
                    "xi_h": list(home_ids), "xi_a": list(away_ids),
                    "player_stats": {"h": {pid: {"min": 90, "rt": 7.5} for pid in home_ids},
                                     "a": {pid: {"min": 90, "rt": 7.0} for pid in away_ids}},
                    "goals": [], "cards_h": [], "cards_a": [], "subs_h": [], "subs_a": [],
                }

            matches = [
                played_match("Premier League", "league_p2_premier_champion_1", "Champion · Vòng 1",
                             "Premier Champion Club", "Other Premier", ("prem-21", "prem-20"), match_id=1),
                played_match("Premier League", "league_p2_premier_challenge_1", "Challenge · Vòng 1",
                             "Challenge Club", "Other Challenge", ("challenge",), match_id=2),
                played_match("C1", "c1_group_A_1", "Bảng A - Vòng 1",
                             "C1 Semi Club", "C1 Group Club", ("c1-eligible",), ("c1-nonsemi",), match_id=3),
                played_match("C1", "c1_semi1_leg1", "Bán kết 1 - Lượt đi",
                             "C1 Semi Club", "C1 Opponent", (), (), match_id=4),
                played_match("Premier League", "league_premier_14", "Vòng 14",
                             "Phase One Club", "Other Phase One", ("prem-phase1",), match_id=5),
                played_match("C1 Play-in", "c1_pi_X_r1", "Play-in - Vòng 1",
                             "C1 Semi Club", "C1 Qualifier", ("c1-playin-only",), match_id=6),
            ]
            stats = {
                "prem-21": {"apps": 21, "mins": 1800, "goals": 10, "assists": 2, "rating_sum": 150, "rating_count": 21},
                "prem-20": {"apps": 20, "mins": 1800, "goals": 10, "assists": 2, "rating_sum": 150, "rating_count": 20},
                "challenge": {"apps": 30, "mins": 2500, "goals": 15, "assists": 3, "rating_sum": 225, "rating_count": 30},
                "c1-eligible": {"apps": 25, "mins": 2100, "goals": 8, "assists": 5, "rating_sum": 190, "rating_count": 25},
                "c1-nonsemi": {"apps": 28, "mins": 2300, "goals": 9, "assists": 3, "rating_sum": 195, "rating_count": 28},
                "plain": {"apps": 35, "mins": 3000, "goals": 20, "assists": 8, "rating_sum": 280, "rating_count": 35},
                "prem-phase1": {"apps": 32, "mins": 2800, "goals": 10, "assists": 8, "rating_sum": 250, "rating_count": 32},
                "c1-no-appearance": {"apps": 30, "mins": 2600, "goals": 12, "assists": 6, "rating_sum": 235, "rating_count": 30},
                "c1-playin-only": {"apps": 25, "mins": 2100, "goals": 7, "assists": 4, "rating_sum": 190, "rating_count": 25},
            }
            candidates = {row["player_id"]: row for row in game._season_award_candidates(stats=stats, matches=matches)}
            self.assertTrue(candidates["prem-21"]["award_eligible"])
            self.assertFalse(candidates["prem-20"]["award_eligible"], "20 trận không được tính là trên 20")
            self.assertFalse(candidates["challenge"]["award_eligible"], "Challenge không thỏa Premier Champion")
            self.assertTrue(candidates["c1-eligible"]["award_eligible"], "Cầu thủ có đá C1 với CLB vào bán kết")
            self.assertFalse(candidates["c1-nonsemi"]["award_eligible"], "CLB không vào bán kết")
            self.assertFalse(candidates["plain"]["award_eligible"], "Không có nhánh Premier Champion/C1 bán kết")
            self.assertFalse(candidates["prem-phase1"]["award_eligible"], "Chỉ đá Premier giai đoạn 1 là chưa đủ")
            self.assertFalse(candidates["c1-no-appearance"]["award_eligible"], "Chỉ thuộc CLB vào bán kết nhưng không ra sân C1 là chưa đủ")
            self.assertFalse(candidates["c1-playin-only"]["award_eligible"], "Chỉ đá C1 Play-in nhưng không ra sân C1 chính thức là chưa đủ")

            # Strict eligibility is also enforced by Best XI; never fill a missing place with an ineligible player.
            xi = game._make_best_xi_442(list(candidates.values()))
            self.assertTrue(all(row["award_eligible"] and row["apps"] > 20 for row in xi))
            self.assertLess(len(xi), 11)

    def test_ballondor_podium_uses_only_eligible_annual_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102)
            game.s["players"].update({
                "eligible": {"common_name": "Eligible", "position": "ST", "overall_rating": 80},
                "ineligible": {"common_name": "Ineligible", "position": "ST", "overall_rating": 90},
            })
            game.s["rosters"].setdefault("Premier Champion Club", []).append("eligible")
            game.s["rosters"].setdefault("Challenge Club", []).append("ineligible")
            match = {
                "id": 10, "played": True, "comp": "Premier League", "stage": "league_p2_premier_champion_1",
                "round": "Champion · Vòng 1", "home": "Premier Champion Club", "away": "Other",
                "hg": 1, "ag": 0, "minutes_h": {"eligible": 90}, "minutes_a": {},
                "xi_h": ["eligible"], "xi_a": [],
                "player_stats": {"h": {"eligible": {"min": 90, "rt": 8.5}}, "a": {}},
                "goals": [], "cards_h": [], "cards_a": [], "subs_h": [], "subs_a": [],
            }
            game.s["season_stats"] = {
                "eligible": {"apps": 21, "mins": 1890, "goals": 20, "assists": 5, "rating_sum": 180, "rating_count": 21},
                "ineligible": {"apps": 38, "mins": 3300, "goals": 35, "assists": 10, "rating_sum": 350, "rating_count": 38},
            }
            game.s["matches"] = [match]
            awards = game._calculate_season_awards()
            self.assertEqual(awards["ballon_dor"]["player_id"], "eligible")
            self.assertEqual(awards["eligible_players"], 1)
            self.assertEqual(awards["ballon_silver"], None)

    def test_champions_points_config_rejects_broken_tier_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261031)
            invalid = json.loads(json.dumps(game.config))
            invalid["points"]["fc1"]["positions"][0] = 84
            with self.assertRaisesRegex(ValueError, "phải bằng hạng 7 premier"):
                Game._validate_config(invalid)
            invalid["points"]["fc1"]["positions"][0] = 76
            invalid["points"]["fc1"]["promotion_playoff_winner"] = 70
            with self.assertRaisesRegex(ValueError, "cao hơn hạng 8 premier"):
                Game._validate_config(invalid)

    def test_club_player_history_keeps_former_players_and_contributions_after_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261111)
            team, other_team = list(game.s["rosters"])[:2]
            scorer_id, assist_id = game.s["rosters"][team][:2]
            scorer_name = game.s["players"][scorer_id]["common_name"]
            assist_name = game.s["players"][assist_id]["common_name"]
            match = {
                "id": 99991, "season": game.s["season"], "date": f"{game.s['season']}-08-20",
                "comp": "Premier League", "home": team, "away": other_team, "hg": 2, "ag": 0,
                "xi_h": [scorer_id, assist_id], "xi_a": [], "subs_h": [], "subs_a": [],
                "minutes_h": {scorer_id: 90, assist_id: 90}, "minutes_a": {},
                "player_stats": {
                    "h": {scorer_id: {"min": 90, "g": 2, "a": 0},
                          assist_id: {"min": 90, "g": 0, "a": 1}},
                    "a": {},
                },
                "goals": [
                    {"side": "home", "team": team, "scorer_id": scorer_id, "scorer": scorer_name,
                     "assist_id": assist_id, "assist": assist_name},
                    {"side": "home", "team": team, "scorer_id": scorer_id, "scorer": scorer_name},
                ],
            }
            game._update_club_season_player_stats(match)

            # Both players leave the club before the season is finalized.
            game.s["rosters"][team].remove(scorer_id)
            game.s["rosters"][team].remove(assist_id)
            game.s["rosters"][other_team].extend([scorer_id, assist_id])
            game.s["players"][scorer_id]["club"] = other_team
            game.s["players"][assist_id]["club"] = other_team

            legends = game.career_player_legends(team=team, limit=1000)
            scorer = next(row for row in legends["goals"] if row["player_id"] == scorer_id)
            assister = next(row for row in legends["assists"] if row["player_id"] == assist_id)
            self.assertEqual((scorer["apps"], scorer["goals"]), (1, 2))
            self.assertEqual((assister["apps"], assister["assists"]), (1, 1))
            self.assertEqual(scorer["team"], team)
            self.assertEqual(assister["team"], team)

            club_data = game.club_history_analytics(team)
            former_ids = {row["player_id"] for row in club_data["former_players"]}
            current_ids = {row["player_id"] for row in club_data["current_players"]}
            self.assertTrue({scorer_id, assist_id}.issubset(former_ids))
            self.assertTrue({scorer_id, assist_id}.isdisjoint(current_ids))

            # Completed-season club snapshots must remain readable after active stats reset.
            archived_rows = [dict(row) for row in game.s["club_season_player_stats"][team].values()]
            game.s["history"].append({
                "season": int(game.s["season"]) - 1,
                "club_player_stats": archived_rows,
            })
            game.s["club_season_player_stats"] = {}
            archived_legends = game.career_player_legends(team=team, limit=1000)
            archived_scorer = next(row for row in archived_legends["goals"] if row["player_id"] == scorer_id)
            self.assertEqual((archived_scorer["apps"], archived_scorer["goals"]), (1, 2))

    def test_legacy_archive_goal_events_restore_the_original_club(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261112)
            original_club, current_club = list(game.s["rosters"])[:2]
            player_id = game.s["rosters"][original_club][0]
            player_name = game.s["players"][player_id]["common_name"]
            game.s["rosters"][original_club].remove(player_id)
            game.s["rosters"][current_club].append(player_id)
            game.s["players"][player_id]["club"] = current_club
            game.s["history"] = [{
                "season": int(game.s["season"]) - 1,
                "career_player_stats": [{
                    "player_id": player_id, "player": player_name, "team": current_club,
                    "position": game.s["players"][player_id]["position"],
                    "apps": 5, "goals": 4, "assists": 1, "minutes": 450,
                }],
                "matches": [{
                    "id": 1, "season": int(game.s["season"]) - 1, "played": True,
                    "date": "2025-08-01", "comp": "Premier League",
                    "home": original_club, "away": current_club, "hg": 1, "ag": 0,
                    "goals": [
                        {"team": original_club, "scorer_id": player_id, "scorer": player_name},
                        {"team": current_club, "scorer_id": player_id, "scorer": player_name},
                        {"team": current_club, "scorer_id": player_id, "scorer": player_name},
                        {"team": current_club, "scorer_id": player_id, "scorer": player_name},
                    ],
                }],
            }]

            former_club_rows = game.career_player_legends(team=original_club, limit=100)
            recovered = next(row for row in former_club_rows["goals"] if row["player_id"] == player_id)
            self.assertEqual((recovered["team"], recovered["goals"]), (original_club, 1))
            current_club_rows = game.career_player_legends(team=current_club, limit=100)
            retained = next(row for row in current_club_rows["goals"] if row["player_id"] == player_id)
            self.assertEqual((retained["apps"], retained["goals"], retained["assists"]),
                             (5, 3, 1))

    def test_full_season_and_roster_invariants(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True)
            self.assertEqual(game.relegation_playoff_summary()["teams"], {})
            # Keep this regression deterministic and independent from a user's
            # local award_settings.json (which may intentionally customize apps).
            game.award_settings = dict(AWARD_SETTINGS_DEFAULTS)
            record = game.simulate_full_season(persist_each_slot=False)
            self.assertEqual(record["match_count"], 248)
            self.assertEqual(len(game.s["matches"]), 248)
            comps = Counter(m["comp"] for m in game.s["matches"])
            self.assertEqual(comps["Premier League"], 80)  # 56 phase 1 + 12 Champion + 12 Challenge
            self.assertEqual(comps["Football Championship 1"], 60)  # 56 + two promotion + two relegation play-offs
            self.assertEqual(comps["Football Championship 2"], 58)  # 56 + two promotion play-offs
            self.assertEqual(comps["Cup"], 23)
            self.assertEqual(comps["C1 Play-in"] + comps["C1"], 27)
            expected_weekdays = {
                "Premier League": 5,                 # Saturday
                "Football Championship 1": 6,       # Sunday
                "Football Championship 2": 4,       # Friday
                "Cup": 1,                           # Tuesday
                "C1 Play-in": 2,                    # Wednesday
                "C1": 2,                            # Wednesday
            }
            for match in game.s["matches"]:
                self.assertEqual(date.fromisoformat(match["date"]).weekday(),
                                 expected_weekdays[match["comp"]],
                                 f"{match['comp']} phải đá đúng thứ: {match['date']}")
            phase2 = game.s["league_phase2"]
            self.assertTrue(phase2["initialized"])
            premier_p2 = phase2["premier"]
            self.assertEqual(len(premier_p2["champion_teams"]), 4)
            self.assertEqual(len(premier_p2["challenge_teams"]), 4)
            self.assertEqual(len(premier_p2["champion_rounds"]), 6)
            self.assertEqual(len(premier_p2["challenge_rounds"]), 6)
            self.assertEqual(sum(len(x) for x in premier_p2["match_ids"]["champion"]), 12)
            self.assertEqual(sum(len(x) for x in premier_p2["match_ids"]["challenge"]), 12)
            p2_tables = game.premier_phase2_tables()
            for group_key in ("champion", "challenge"):
                self.assertEqual(len(p2_tables[group_key]), 4)
                for row in p2_tables[group_key]:
                    self.assertEqual(row["p"], 20)
                    self.assertGreaterEqual(row["pts"], row["phase1_pts"])
                    self.assertGreaterEqual(row["phase2_pts"], 0)
            for tier in ("fc1", "fc2"):
                playoff = phase2["promotion_playoffs"][tier]
                self.assertEqual(playoff["semi_match"] is not None, True)
                self.assertEqual(playoff["final_match"] is not None, True)
                self.assertIn(playoff["winner"], (playoff["teams"]["second"], game.match_by_id(playoff["semi_match"])["winner"]))
            relegation = phase2["relegation_playoff"]
            leg1 = game.match_by_id(relegation["leg1_match"])
            leg2 = game.match_by_id(relegation["leg2_match"])
            self.assertEqual((leg1["home"], leg1["away"]),
                             (relegation["teams"]["seventh"], relegation["teams"]["sixth"]))
            self.assertEqual((leg2["home"], leg2["away"]),
                             (relegation["teams"]["sixth"], relegation["teams"]["seventh"]))
            self.assertIn(relegation["winner"], (relegation["teams"]["sixth"], relegation["teams"]["seventh"]))
            self.assertIn(relegation["loser"], (relegation["teams"]["sixth"], relegation["teams"]["seventh"]))
            self.assertNotEqual(relegation["winner"], relegation["loser"])
            fc1_table = game.league_phase1_table("fc1")
            relegated_fc1 = {row["team"] for row in record["relegated"] if row["from"] == "fc1"}
            self.assertEqual(relegated_fc1, {fc1_table[-1]["team"], relegation["loser"]})
            self.assertEqual(Counter(game.s["team_tiers"].values()), Counter({"premier":8,"fc1":8,"fc2":8}))
            club_data = game.club_history_analytics(relegation["teams"]["sixth"])
            self.assertEqual(club_data["stats"]["matches"],
                             club_data["stats"]["wins"] + club_data["stats"]["draws"] + club_data["stats"]["losses"])
            self.assertTrue(club_data["stats"]["first_match"])
            for result_key in ("biggest_win", "biggest_loss"):
                result = club_data["stats"][result_key]
                self.assertIsNotNone(result, result_key)
                self.assertEqual(result["comp"], result["competition"])
                self.assertIsInstance(result["hg"], int)
                self.assertIsInstance(result["ag"], int)
                self.assertTrue(result["home"])
                self.assertTrue(result["away"])
                self.assertTrue(result["date"])
            self.assertLessEqual(len(club_data["top11"]), 11)
            self.assertEqual(len(record["qualification_table"]), 24)
            self.assertEqual(len(game.s["history"]), 1)
            self.assertIn("career_player_stats", record)
            self.assertTrue(record["career_player_stats"])
            self.assertIn("club_player_stats", record)
            self.assertTrue(record["club_player_stats"])
            self.assertTrue({"player_id", "player", "team", "apps", "goals", "assists", "minutes"}.issubset(record["career_player_stats"][0]))
            self.assertTrue({"player_id", "player", "team", "apps", "goals", "assists", "minutes"}.issubset(record["club_player_stats"][0]))
            annual = record["season_awards"]
            self.assertEqual(annual.get("best_xi_version"), 8)
            top20 = annual.get("top20_players", [])
            self.assertEqual(len(top20), min(20, annual.get("eligible_players", 0)))
            self.assertEqual([row.get("rank") for row in top20], list(range(1, len(top20) + 1)))
            self.assertEqual([row.get("player_id") for row in top20[:3]],
                             [row.get("player_id") for row in annual.get("ballon_podium", [])])
            self.assertEqual([row.get("ballon_award") for row in top20[:3]],
                             ["Quả bóng Vàng", "Quả bóng Bạc", "Quả bóng Đồng"][:len(top20[:3])])
            self.assertGreater(annual.get("eligible_players", 0), 0)
            for winner in annual.get("ballon_podium", []):
                self.assertGreater(winner.get("apps", 0), 20)
                self.assertTrue(winner.get("award_eligible"))
            for player in annual.get("best_xi_442", []):
                self.assertGreater(player.get("apps", 0), 20)
                self.assertTrue(player.get("award_eligible"))
            xi_rows = annual.get("best_xi_442", [])
            self.assertLessEqual(len(xi_rows), 11)
            xi_roles = [row.get("role") for row in xi_rows]
            # The formation never cross-fills a position. A missing slot can be
            # filled only from the C1-winning club when that same-position player
            # also passes the annual eligibility and appearance requirements.
            self.assertIn(xi_roles.count("GK"), (0, 1))
            # A line may be short only when no same-position eligible player is
            # available in the Top 20 or from the Champions League winner.
            self.assertLessEqual(sum(role in {"LB", "CB", "RB"} for role in xi_roles), 4)
            self.assertLessEqual(sum(role in {"LM", "CM", "RM"} for role in xi_roles), 4)
            self.assertLessEqual(xi_roles.count("ST"), 2)
            c1_winner = game._season_champion_teams(record).get("c1")
            top20_ids = {str(row.get("player_id")) for row in top20}
            fallback_ids = {str(row.get("player_id")) for row in xi_rows if row.get("best_xi_fallback")}
            self.assertTrue(all(row.get("team") == c1_winner and row.get("award_eligible")
                                and int(row.get("apps", 0)) > 20 for row in xi_rows if row.get("best_xi_fallback")))
            self.assertTrue(fallback_ids.isdisjoint(top20_ids))
            for row in xi_rows:
                pos = str(row.get("position", "")).upper()
                role = row.get("role")
                allowed = {
                    "GK": {"GK", "G"}, "LB": {"LB", "LWB"}, "CB": {"CB", "SW"},
                    "RB": {"RB", "RWB"}, "LM": {"LM", "LW"},
                    "CM": {"CM", "CDM", "CAM", "DM", "AM", "LCM", "RCM"},
                    "RM": {"RM", "RW"}, "ST": {"ST", "CF", "FW"},
                }
                self.assertIn(pos, allowed[role])

            # Historical migration must replace an old ballot that did not enforce eligibility.
            annual["best_xi_version"] = 2
            annual["ballon_dor"] = {"player_id": "legacy-ineligible-placeholder"}
            annual["best_xi_442"] = [{"player_id": "legacy-ineligible-placeholder"}]
            game._backfill_history_awards()
            migrated = record["season_awards"]
            self.assertEqual(migrated.get("best_xi_version"), 8)
            self.assertTrue(migrated.get("top20_players"))
            self.assertEqual([row.get("player_id") for row in migrated["top20_players"][:3]],
                             [row.get("player_id") for row in migrated.get("ballon_podium", [])])
            self.assertNotEqual((migrated.get("ballon_dor") or {}).get("player_id"), "legacy-ineligible-placeholder")
            self.assertTrue(all(row.get("award_eligible") and int(row.get("apps", 0)) > 20
                                for row in migrated.get("ballon_podium", [])))
            self.assertTrue(all(row.get("award_eligible") and int(row.get("apps", 0)) > 20
                                for row in migrated.get("best_xi_442", [])))
            self.assertEqual(len(game.s["slots"]), 68)
            self.assertTrue(game.s["calendar_validation"]["valid"])
            self.assertEqual(len(record["promoted"]), 4)
            self.assertEqual(len(record["relegated"]), 4)
            self.assertEqual(Counter(x["to"] for x in record["promoted"]), Counter({"premier":2,"fc1":2}))
            self.assertEqual(Counter(x["from"] for x in record["relegated"]), Counter({"premier":2,"fc1":2}))
            _assert_match_schedule(game.s["matches"], 3)
            self.assertEqual(len(record["c1"]["playin" ]), 2)
            for name, entry in game.s["c1"]["playin"].items():
                self.assertEqual(len(entry["r1"]), 2)
                self.assertIsNotNone(entry["winner_match"])
                self.assertIsNotNone(entry["loser_match"])
                self.assertIsNotNone(entry["decider"])
            # AI transfers, loans and contract expiry may legitimately change
            # exact positions; the supported invariant is roster size plus
            # configured positional-group coverage, not the original seed template.
            squad_rules = SquadRulesService(game.economy_config)
            for team, ids in game.s["rosters"].items():
                validation = squad_rules.validate(ids, game.s["players"])
                self.assertTrue(validation["ok"], f"{team}: {validation['errors']}")
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["super_league.db"])
            db_status = game.database.status()
            self.assertGreater(db_status["tables"]["players"], 0)
            self.assertGreater(db_status["tables"]["player_metadata"], 0)
            self.assertEqual(db_status["integrity"], "ok")
            game.next_season()
            self.assertEqual(Counter(game.s["team_tiers"].values()), Counter({"premier":8,"fc1":8,"fc2":8}))
            self.assertEqual(len(game.s["league_fixtures"]["premier"]), 14)
            self.assertEqual(len(game.s["slots"]), 68)
            self.assertEqual(game.s["season"], 2027)

    def test_league_phase2_format_and_promotion_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030)
            game.simulate_full_season(persist_each_slot=False)
            record = game.s["history"][-1]
            phase2 = record["league_phase2"]
            p2 = phase2["premier"]
            champion = set(p2["champion_teams"])
            challenge = set(p2["challenge_teams"])
            self.assertEqual(len(champion), 4)
            self.assertEqual(len(challenge), 4)
            self.assertFalse(champion & challenge)
            self.assertEqual(champion | challenge, set(row["team"] for row in p2["phase1_table"]))
            # The Premier champion must be first in the Champion table.
            self.assertEqual(record["champion_premier"], game.premier_phase2_tables()["champion"][0]["team"])
            # Relegation is the bottom two of the Challenge group.
            expected_relegated = {r["team"] for r in game.premier_phase2_tables()["challenge"][-2:]}
            actual_relegated = {r["team"] for r in record["relegated"] if r["from"] == "premier"}
            self.assertEqual(actual_relegated, expected_relegated)
            # FC1/FC2 top team gets direct promotion, the playoff winner takes the second ticket.
            for tier, destination in (("fc1", "premier"), ("fc2", "fc1")):
                table = game.league_phase1_table(tier)
                playoff = phase2["promotion_playoffs"][tier]
                promoted = [r["team"] for r in record["promoted"] if r["from"] == tier]
                self.assertEqual(set(promoted), {table[0]["team"], playoff["winner"]})
                direct = next(r for r in record["promoted"] if r["team"] == table[0]["team"])
                playoff_path = next(r for r in record["promoted"] if r["team"] == playoff["winner"])
                self.assertEqual(direct["path"], "Vô địch GĐ1")
                self.assertEqual(playoff_path["path"], "Thắng play-off")
                semi = game.match_by_id(playoff["semi_match"])
                final = game.match_by_id(playoff["final_match"])
                self.assertEqual({semi["home"], semi["away"]}, {playoff["teams"]["third"], playoff["teams"]["fourth"]})
                self.assertEqual({final["home"], final["away"]}, {playoff["teams"]["second"], semi["winner"]})

            # End-to-end: final Champions ranking reflects direct promotion and
            # play-off promotion awards, not just the regular-season position.
            qualification_points = {row["team"]: row["league_points"]
                                    for row in record["qualification_table"]}
            premier_final = game.league_table("premier")
            fc1_final = game.league_table("fc1")
            fc2_final = game.league_table("fc2")
            self.assertEqual(qualification_points[fc1_final[0]["team"]],
                             qualification_points[premier_final[6]["team"]])
            self.assertGreater(qualification_points[phase2["promotion_playoffs"]["fc1"]["winner"]],
                               qualification_points[premier_final[7]["team"]])
            self.assertGreater(qualification_points[fc2_final[0]["team"]],
                               qualification_points[fc1_final[6]["team"]])
            self.assertGreater(qualification_points[phase2["promotion_playoffs"]["fc2"]["winner"]],
                               qualification_points[fc1_final[7]["team"]])

    def test_legacy_in_progress_save_gets_phase2_slots_without_losing_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Game(data_dir=tmp, fresh=True, seed=777)
            original.advance_slot(persist=False)
            previous_matches = [dict(match) for match in original.s["matches"]]
            original.s.pop("league_phase2", None)
            original.s.pop("calendar_schedule_version", None)
            original.s["slots"] = [slot for slot in original.s["slots"] if slot.get("kind") != "league_phase2"]
            original.save()
            upgraded = Game(data_dir=tmp, seed=777)
            self.assertEqual(upgraded.s["matches"], json.loads(json.dumps(previous_matches)))
            phase2_slots = [slot for slot in upgraded.s["slots"] if slot.get("kind") == "league_phase2"]
            self.assertEqual(len(phase2_slots), 12)
            self.assertEqual(len(upgraded.s["slots"]), 68)
            self.assertTrue(upgraded.s["calendar_validation"]["valid"])
            self.assertEqual(upgraded.s["calendar_validation"]["slots"], 68)
            latest_played = max(match["date"] for match in upgraded.s["matches"])
            self.assertGreaterEqual(
                (date.fromisoformat(upgraded.next_slot()["date"]) - date.fromisoformat(latest_played)).days, 3
            )

    def test_save_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            one = Game(data_dir=tmp, fresh=True)
            one.advance_slot()
            two = Game(data_dir=tmp)
            self.assertEqual(len(two.s["matches"]), 4)
            self.assertTrue(two.s["slots"][0]["played"])
            self.assertEqual(two.s["season"], 2026)

    def test_calendar_honors_weekdays_and_rest_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            cfg["calendar"]["start_date"] = "2027-08-02"  # Sunday; game season/date should follow config year.
            cfg["calendar"]["league_day"] = "sat"
            cfg["calendar"]["midweek_day"] = "wed"
            config_path = Path(tmp) / "custom_config.json"
            config_path.write_text(json.dumps(cfg), encoding="utf-8")
            game = Game(data_dir=Path(tmp) / "data", config_path=config_path, fresh=True)
            self.assertEqual(game.s["season"], 2027)
            self.assertEqual(date.fromisoformat(game.s["season_start_date"]).year, 2027)
            expected_tier_weekdays = {"fc2": 4, "premier": 5, "fc1": 6}
            self.assertEqual(len(game.s["slots"]), 68)
            dates = [date.fromisoformat(slot["date"]) for slot in game.s["slots"]]
            self.assertEqual(len(dates), len(set(dates)))
            for slot in game.s["slots"]:
                weekday = date.fromisoformat(slot["date"]).weekday()
                if slot["kind"] == "league":
                    self.assertEqual(weekday, expected_tier_weekdays[slot["tier"]], slot)
                elif slot["kind"] == "league_phase2":
                    self.assertEqual(weekday, expected_tier_weekdays[slot["phase2_tier"]], slot)
                elif slot["kind"].startswith("cup_"):
                    self.assertEqual(weekday, 1, slot)  # Tuesday
                elif slot["kind"].startswith("c1_"):
                    self.assertEqual(weekday, 2, slot)  # Wednesday
            self.assertTrue(game.s["calendar_validation"]["valid"])
            self.assertEqual(game.s["calendar_schedule_version"], 5)
            slots = {slot["id"]: slot for slot in game.s["slots"]}
            anchor = date.fromisoformat(game.s["season_start_date"])
            self.assertEqual([date.fromisoformat(slots[f"C1{i}"]["date"]) for i in range(1, 10)],
                             [anchor + timedelta(weeks=week, days=-3)
                              for week in (1, 3, 6, 11, 16, 20, 37, 39, 41)])
            self.assertEqual(slots["P2FC1R04"]["kind"], "league_phase2")
            self.assertEqual(slots["P2FC1R06"]["kind"], "league_phase2")
            self.assertEqual(game.s["calendar_validation"]["fc2_weekday"], "fri")
            self.assertEqual(game.s["calendar_validation"]["league_weekday"], "sat")
            self.assertEqual(game.s["calendar_validation"]["fc1_weekday"], "sun")
            self.assertEqual(game.s["calendar_validation"]["cup_weekday"], "tue")
            self.assertEqual(game.s["calendar_validation"]["midweek_day"], "wed")
            self.assertTrue(all(d >= date.fromisoformat(cfg["calendar"]["start_date"]) for d in dates))

    def test_v4_calendar_migration_adds_fc1_relegation_legs_and_interleaves_c1(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=99881)
            anchor = date.fromisoformat(game.s["season_start_date"])
            game.s["slots"] = [slot for slot in game.s["slots"]
                               if slot["id"] not in ("P2FC1R04", "P2FC1R06")]
            old_c1_offsets = [1, 3, 6, 36, 37, 38, 39, 40, 41]
            for index, week in enumerate(old_c1_offsets, 1):
                slot = next(item for item in game.s["slots"] if item["id"] == f"C1{index}")
                slot["date"] = (anchor + timedelta(weeks=week, days=-3)).isoformat()
            game.s["calendar_schedule_version"] = 4
            game.save()

            migrated = Game(data_dir=tmp, seed=99881)
            slots = {slot["id"]: slot for slot in migrated.s["slots"]}
            self.assertEqual(migrated.s["calendar_schedule_version"], 5)
            self.assertEqual(len(migrated.s["slots"]), 68)
            self.assertEqual(slots["P2FC1R04"]["label"], "FC1 · Play-off trụ hạng · Lượt đi")
            self.assertEqual(slots["P2FC1R06"]["label"], "FC1 · Play-off trụ hạng · Lượt về")
            self.assertEqual(date.fromisoformat(slots["C14"]["date"]),
                             anchor + timedelta(weeks=11, days=-3))
            self.assertEqual(slots["C19"]["date"], max(slot["date"] for slot in migrated.s["slots"]))
            self.assertTrue(migrated.s["calendar_validation"]["valid"])

    def test_schedule_v2_save_retimes_unplayed_fixtures_to_current_weekday_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=778812)
            game.advance_slot(persist=False)  # Preserve a real completed FC2 Friday slot.
            prior_matches = json.loads(json.dumps(game.s["matches"]))
            played_slot = next(slot for slot in game.s["slots"] if slot["id"] == "L01_FC2")
            played_slot_date = played_slot["date"]
            # Simulate a version-2 save where unplayed Cup R16 still points at week 11.
            cup_round_2 = next(slot for slot in game.s["slots"] if slot["id"] == "CUP2")
            anchor = date.fromisoformat(game.s["season_start_date"])
            cup_round_2["date"] = (anchor + timedelta(weeks=11, days=1 - anchor.weekday())).isoformat()
            game.s["calendar_schedule_version"] = 2
            game.save()

            resumed = Game(data_dir=tmp, seed=778812)
            migrated = next(slot for slot in resumed.s["slots"] if slot["id"] == "CUP2")
            resumed_played_slot = next(slot for slot in resumed.s["slots"] if slot["id"] == "L01_FC2")
            expected = anchor + timedelta(weeks=game.config["calendar"]["cup_week_offsets"][1],
                                                days=1 - anchor.weekday())
            self.assertEqual(date.fromisoformat(migrated["date"]), expected)
            self.assertTrue(resumed_played_slot["played"])
            self.assertEqual(resumed_played_slot["date"], played_slot_date)
            self.assertEqual(resumed.s["matches"], prior_matches)
            self.assertEqual(resumed.s["calendar_schedule_version"], 5)
            self.assertTrue(resumed.s["calendar_validation"]["valid"])

    def test_c1_playin_cup_final_and_c1_final_ordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261031)
            slots = {slot["id"]: slot for slot in game.s["slots"]}
            anchor = date.fromisoformat(game.s["season_start_date"])

            # C1 Play-in rounds are interleaved between the first-leg Premier rounds.
            expected_playin_weeks = [1, 3, 6]
            for index, week in enumerate(expected_playin_weeks, 1):
                playin_day = date.fromisoformat(slots[f"C1{index}"]["date"])
                self.assertEqual(playin_day.weekday(), 2)  # Wednesday
                self.assertEqual(playin_day, anchor + timedelta(weeks=week, days=-3))
            premier_offsets = game.config["calendar"]["league_week_offsets"]
            for playin_week, before_round in ((1, 1), (3, 3), (6, 5)):
                playin_day = date.fromisoformat(slots[f"C1{expected_playin_weeks.index(playin_week)+1}"]["date"])
                previous_round = anchor + timedelta(weeks=premier_offsets[before_round-1])
                next_round = anchor + timedelta(weeks=premier_offsets[before_round])
                self.assertLess(previous_round, playin_day)
                self.assertLess(playin_day, next_round)

            # FC2 skips the weeks occupied by C1 Play-in to preserve 3-day rest.
            fc2_offsets = game.config["calendar"]["fc2_week_offsets"]
            self.assertTrue(set(expected_playin_weeks).isdisjoint(fc2_offsets))
            for round_no, week in enumerate(fc2_offsets, 1):
                slot_day = date.fromisoformat(slots[f"L{round_no:02d}_FC2"]["date"])
                self.assertEqual(slot_day.weekday(), 4)
                self.assertEqual(slot_day, anchor + timedelta(weeks=week, days=-1))

            # Major Cup Final is exactly three days after Premier Phase 2 Round 3.
            premier_r3 = date.fromisoformat(slots["P2R03"]["date"])
            cup_final = date.fromisoformat(slots["CUP5"]["date"])
            self.assertEqual((cup_final - premier_r3).days, 3)
            self.assertEqual(cup_final.weekday(), 1)  # Tuesday

            # C1 Final is the last scheduled slot of the season.
            final_slot = slots["C19"]
            self.assertEqual(final_slot["kind"], "c1_final")
            self.assertEqual(final_slot["date"], max(slot["date"] for slot in game.s["slots"]))
            self.assertEqual(final_slot["date"], "2027-05-19")
            self.assertTrue(game.s["calendar_validation"]["valid"])

    def test_schedule_v3_migration_interleaves_playin_and_moves_finals(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=86420)
            anchor = date.fromisoformat(game.s["season_start_date"])
            # Restore the former v3 C1/Cup dates, then ask loader to migrate.
            old_c1_offsets = [20,21,22,23,24,25,26,27,28]
            for index, week in enumerate(old_c1_offsets, 1):
                slot = next(x for x in game.s["slots"] if x["id"] == f"C1{index}")
                slot["date"] = (anchor + timedelta(weeks=week, days=-3)).isoformat()
            cup_final = next(x for x in game.s["slots"] if x["id"] == "CUP5")
            cup_final["date"] = (anchor + timedelta(weeks=19, days=-4)).isoformat()
            game.s["calendar_schedule_version"] = 3
            game.save()

            migrated = Game(data_dir=tmp, seed=86420)
            slots = {slot["id"]: slot for slot in migrated.s["slots"]}
            self.assertEqual(migrated.s["calendar_schedule_version"], 5)
            self.assertEqual(date.fromisoformat(slots["C11"]["date"]), anchor + timedelta(weeks=1, days=-3))
            self.assertEqual(date.fromisoformat(slots["C12"]["date"]), anchor + timedelta(weeks=3, days=-3))
            self.assertEqual(date.fromisoformat(slots["C13"]["date"]), anchor + timedelta(weeks=6, days=-3))
            self.assertEqual(
                (date.fromisoformat(slots["CUP5"]["date"]) - date.fromisoformat(slots["P2R03"]["date"])).days,
                3,
            )
            self.assertEqual(slots["C19"]["date"], max(slot["date"] for slot in migrated.s["slots"]))
            self.assertTrue(migrated.s["calendar_validation"]["valid"])

    def test_in_progress_v3_migration_preserves_played_match_and_moves_pending_slots_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=86421)
            game.advance_slot(persist=False)
            played = json.loads(json.dumps(game.s["matches"]))
            played_date = played[-1]["date"]
            game.s["calendar_schedule_version"] = 3
            game.save()

            migrated = Game(data_dir=tmp, seed=86421)
            self.assertEqual(migrated.s["matches"], played)
            next_slot = migrated.next_slot()
            self.assertIsNotNone(next_slot)
            self.assertGreaterEqual(
                (date.fromisoformat(next_slot["date"]) - date.fromisoformat(played_date)).days, 3
            )
            self.assertTrue(migrated.s["calendar_validation"]["valid"])
            self.assertEqual(migrated.s["calendar_schedule_version"], 5)

    def test_reset_simulation_starts_clean_career_without_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=24680)
            game.advance_slot(persist=True)
            self.assertEqual(len(game.s["matches"]), 4)
            game.reset_simulation()
            self.assertEqual(game.s["history"], [])
            self.assertEqual(game.s["matches"], [])
            self.assertEqual(game.s["slot_cursor"], 0)
            self.assertFalse(game.s["season_done"])
            self.assertTrue(game.save_path.exists())
            self.assertEqual(len(game.s["slots"]), 68)
            self.assertFalse((Path(tmp) / "backups").exists())
            self.assertEqual(list((Path(tmp) / "season").glob("*.json")), [])

    def test_rejects_unsafe_calendar_and_unsupported_tier_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            cfg["calendar"]["min_rest_days"] = 4  # Default schedule contains 3-day gaps.
            path = Path(tmp) / "too_tight.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Lịch vi phạm thời gian nghỉ"):
                Game(data_dir=Path(tmp) / "data", config_path=path, fresh=True)

            cfg["calendar"]["min_rest_days"] = 3
            cfg["teams"]["premier"] = 7
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "8-8-8"):
                Game(data_dir=Path(tmp) / "data2", config_path=path, fresh=True)

            cfg["teams"]["premier"] = 8
            cfg["teams"]["promotions"] = 1
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "promotions.*bằng 2"):
                Game(data_dir=Path(tmp) / "data3", config_path=path, fresh=True)

    def test_cup_bracket_has_correct_byes_and_advancement(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=445566)
            game.simulate_full_season(persist_each_slot=False)
            cup = game.s["cup"]
            seeds = cup["seed_order"]
            bracket = cup["bracket"]
            expected_counts = {"r1": 8, "r16": 8, "qf": 4, "sf": 2, "final": 1}
            self.assertEqual({key: len(bracket.get(key, [])) for key in expected_counts}, expected_counts)
            self.assertEqual(sum(expected_counts.values()), 23)

            r1 = [game.match_by_id(mid) for mid in bracket["r1"]]
            r1_teams = [team for match in r1 for team in (match["home"], match["away"])]
            self.assertEqual(len(r1_teams), 16)
            self.assertEqual(len(set(r1_teams)), 16)
            self.assertEqual(set(r1_teams), set(seeds[8:]))
            self.assertTrue(all(match["winner"] in (match["home"], match["away"]) for match in r1))

            round_pairs = [("r1", "r16"), ("r16", "qf"), ("qf", "sf"), ("sf", "final")]
            for previous_key, next_key in round_pairs:
                previous_matches = [game.match_by_id(mid) for mid in bracket[previous_key]]
                next_matches = [game.match_by_id(mid) for mid in bracket[next_key]]
                expected_participants = {match["winner"] for match in previous_matches}
                actual_participants = {team for match in next_matches for team in (match["home"], match["away"])}
                if previous_key == "r1":
                    expected_participants |= set(seeds[:8])  # 8 đội hạt giống được miễn vòng 1
                self.assertEqual(actual_participants, expected_participants, f"{previous_key} -> {next_key}")
                self.assertEqual(len(actual_participants), len([team for match in next_matches for team in (match["home"], match["away"])]))

            final = game.match_by_id(bracket["final"][0])
            self.assertTrue(final["neutral"])
            self.assertEqual(cup["winner"], final["winner"])
            self.assertEqual(cup["runner_up"], final["away"] if final["winner"] == final["home"] else final["home"])

    def test_c1_playin_groups_main_stage_and_two_leg_semis(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=98765)
            game.simulate_full_season(persist_each_slot=False)
            c1 = game.s["c1"]
            seeds = c1["seed_order"]
            self.assertEqual(c1["direct_teams"], seeds[:4])
            self.assertEqual(c1["playin_seed_teams"], seeds[4:12])
            self.assertEqual(set(c1["playin"].keys()), {"X", "Y"})

            all_playin_r1_teams = set()
            for group_name, entry in c1["playin"].items():
                self.assertEqual(len(entry["teams"]), 4)
                self.assertEqual(len(entry["r1"]), 2)
                round1 = [game.match_by_id(mid) for mid in entry["r1"]]
                winners = {m["winner"] for m in round1}
                losers = {m["away"] if m["winner"] == m["home"] else m["home"] for m in round1}
                all_playin_r1_teams.update(team for m in round1 for team in (m["home"], m["away"]))
                winner_match = game.match_by_id(entry["winner_match"])
                loser_match = game.match_by_id(entry["loser_match"])
                decider = game.match_by_id(entry["decider"])
                self.assertEqual({winner_match["home"], winner_match["away"]}, winners)
                self.assertEqual({loser_match["home"], loser_match["away"]}, losers)
                wb_loser = winner_match["away"] if winner_match["winner"] == winner_match["home"] else winner_match["home"]
                self.assertEqual({decider["home"], decider["away"]}, {wb_loser, loser_match["winner"]})
                self.assertEqual(entry["winner_qualifier"], winner_match["winner"])
                self.assertEqual(entry["decider_qualifier"], decider["winner"])
            self.assertEqual(all_playin_r1_teams, set(seeds[4:12]))

            self.assertEqual(set(c1["groups"]), {"A", "B"})
            seen_playin_team_group = {}
            all_group_match_ids = []
            for label in ("A", "B"):
                group = c1["groups"][label]
                self.assertEqual(len(group["teams"]), 4)
                self.assertEqual(len(group["match_ids"]), 3)
                self.assertTrue(all(len(round_ids) == 2 for round_ids in group["match_ids"]))
                ids = [mid for round_ids in group["match_ids"] for mid in round_ids]
                all_group_match_ids.extend(ids)
                appearances = Counter(team for mid in ids for team in (game.match_by_id(mid)["home"], game.match_by_id(mid)["away"]))
                self.assertEqual(appearances, Counter({team: 3 for team in group["teams"]}))
                for team in group["teams"]:
                    if team in seeds[4:12]: seen_playin_team_group[team] = label
            self.assertEqual(len(all_group_match_ids), 12)
            self.assertNotEqual(seen_playin_team_group[c1["playin"]["X"]["winner_qualifier"]], seen_playin_team_group[c1["playin"]["X"]["decider_qualifier"]])
            self.assertNotEqual(seen_playin_team_group[c1["playin"]["Y"]["winner_qualifier"]], seen_playin_team_group[c1["playin"]["Y"]["decider_qualifier"]])

            self.assertEqual(len(c1["semi_ties"]), 2)
            self.assertEqual(len(c1["bracket"]["semis"]), 4)
            for tie in c1["semi_ties"]:
                leg1, leg2 = game.match_by_id(tie["leg1"]), game.match_by_id(tie["leg2"])
                self.assertEqual((leg1["home"], leg1["away"]), (tie["group_runner"], tie["group_winner"]))
                self.assertEqual((leg2["home"], leg2["away"]), (tie["group_winner"], tie["group_runner"]))
                winner_total = (leg1["ag"] if tie["group_winner"] == leg1["away"] else leg1["hg"]) + (leg2["hg"] if tie["group_winner"] == leg2["home"] else leg2["ag"])
                loser_total = (leg1["hg"] if tie["group_runner"] == leg1["home"] else leg1["ag"]) + (leg2["hg"] if tie["group_runner"] == leg2["home"] else leg2["ag"])
                if winner_total != loser_total:
                    self.assertEqual(tie["group_winner"] if winner_total > loser_total else tie["group_runner"], leg2["winner"])
                else:
                    self.assertIsNotNone(leg2["pens"], "Nếu tổng tỷ số vẫn hòa sau hiệp phụ thì phải có luân lưu.")

    def test_same_seed_reproduces_match_results(self):
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            one = Game(data_dir=tmp1, fresh=True, seed=20261030)
            two = Game(data_dir=tmp2, fresh=True, seed=20261030)
            one.simulate_full_season(persist_each_slot=False)
            two.simulate_full_season(persist_each_slot=False)
            project = lambda game: [
                (m["id"], m["date"], m["comp"], m["round"], m["home"], m["hg"], m["ag"], m["away"], m["winner"], m.get("pens"))
                for m in game.s["matches"]
            ]
            self.assertEqual(project(one), project(two))
            self.assertEqual(one.s["cup"]["winner"], two.s["cup"]["winner"])
            self.assertEqual(one.s["c1"]["champion"], two.s["c1"]["champion"])

    def test_overview_news_has_exactly_one_bullet_per_completed_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030)
            result = game.advance_slot(persist=False)
            matches = result.get("matches") or game.s["matches"]
            summaries = [item for item in game.s.get("news", []) if item.get("kind") == "match_summary"]
            self.assertEqual(len(summaries), len(matches))
            self.assertEqual(len({(item.get("season"), item.get("match_id")) for item in summaries}), len(matches))
            for match in matches:
                item = next(row for row in summaries if row.get("match_id") == match["id"])
                self.assertIn(f"{match['home']} {match['hg']}–{match['ag']} {match['away']}", item["text"])
                self.assertTrue(item["text"].startswith(match["date"]))
                self.assertTrue(item.get("headline"), "Mỗi trận phải có tiêu đề bản tin riêng")
                self.assertTrue(item.get("body"), "Mỗi trận phải có phần tường thuật")
                self.assertEqual(item.get("format_version"), 2)
                self.assertIn(item.get("angle"), {"hat_trick", "comeback", "big_win", "late_winner", "thriller",
                                                   "upset", "red_card", "var", "injury", "draw", "narrow_win", "result"})

    def test_five_fixtures_produce_five_editorial_articles_even_with_legacy_limit(self):
        from types import SimpleNamespace
        game = SimpleNamespace(s={"players": {}})
        fixtures = [
            {"id": 1, "season": 2026, "date": "2026-08-01", "comp": "Premier League", "round": "Vòng 1",
             "home": "A", "away": "B", "hg": 5, "ag": 0, "winner": "A", "goals": [], "match_engine": "enhanced"},
            {"id": 2, "season": 2026, "date": "2026-08-01", "comp": "Premier League", "round": "Vòng 1",
             "home": "C", "away": "D", "hg": 3, "ag": 3, "winner": None,
             "goals": [{"minute": 12, "team": "C", "scorer": "Player C"}]},
            {"id": 3, "season": 2026, "date": "2026-08-01", "comp": "Premier League", "round": "Vòng 1",
             "home": "E", "away": "F", "hg": 1, "ag": 0, "winner": "E",
             "goals": [{"minute": 90, "team": "E", "scorer": "Player E"}]},
            {"id": 4, "season": 2026, "date": "2026-08-01", "comp": "Premier League", "round": "Vòng 1",
             "home": "G", "away": "H", "hg": 0, "ag": 1, "winner": "H", "goals": [],
             "cards_a": [{"m": 70, "p": "P1", "c": "r"}]},
            {"id": 5, "season": 2026, "date": "2026-08-01", "comp": "Premier League", "round": "Vòng 1",
             "home": "I", "away": "J", "hg": 0, "ag": 0, "winner": None, "goals": [], "var": [{"m": 50, "res": "confirmed"}]},
        ]
        articles = build_round_news(game, {"label": "Vòng 1"}, fixtures, limit=2)
        self.assertEqual(len(articles), 5)
        self.assertEqual({article["match_id"] for article in articles}, {1, 2, 3, 4, 5})
        self.assertTrue(all(article.get("headline") and article.get("body") for article in articles))
        self.assertIn("big_win", {article["angle"] for article in articles})
        self.assertIn("red_card", {article["angle"] for article in articles})
        self.assertIn("var", {article["angle"] for article in articles})

    def test_overview_news_is_limited_to_previous_and_current_simulation_week(self):
        windows = recent_two_week_window("2026-10-30")  # Friday, current simulation week.
        self.assertEqual(windows["previous"], (date(2026, 10, 19), date(2026, 10, 25)))
        self.assertEqual(windows["current"], (date(2026, 10, 26), date(2026, 11, 1)))
        entries = [
            {"kind": "match_summary", "date": day, "match_id": idx}
            for idx, day in enumerate(("2026-10-18", "2026-10-19", "2026-10-25",
                                       "2026-10-26", "2026-11-01", "2026-11-02"), 1)
        ]
        entries.append({"kind": "champion", "date": "2026-10-30", "headline": "Vô địch"})
        visible = filter_recent_match_news(entries, "2026-10-30")
        self.assertEqual([row["match_id"] for row in visible], [2, 3, 4, 5])

    def test_news_retention_keeps_only_current_and_previous_simulation_weeks(self):
        entries = [
            {"kind": "match_summary", "date": "2026-10-18", "match_id": 1},
            {"kind": "match_summary", "date": "2026-10-19", "match_id": 2},
            {"kind": "champion", "date": "2026-10-30", "headline": "Vô địch"},
            {"kind": "competition_award", "date": "2026-11-01", "headline": "Trao giải"},
            {"kind": "match_summary", "date": "2026-11-02", "match_id": 3},
            {"kind": "legacy", "headline": "Không có ngày"},
        ]
        retained = retain_recent_news(entries, "2026-10-30")
        self.assertEqual([(row["kind"], row.get("match_id")) for row in retained], [
            ("match_summary", 2), ("champion", None), ("competition_award", None),
        ])
        self.assertEqual(len(entries), 6, "Hàm dọn tin không được mutate danh sách save gốc")

    def test_startup_sync_does_not_recreate_news_from_older_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030)
            game.advance_slot(persist=False)
            old_match = dict(game.s["matches"][0])
            old_match["id"] = 999999
            old_match["date"] = "2026-07-01"
            old_news = {"kind": "match_summary", "date": "2026-07-01", "season": 2026,
                        "match_id": 999999, "headline": "Tin cũ", "text": "Tin cũ"}
            game.s["matches"].append(old_match)
            game.s["news"] = [old_news]
            game._sync_match_news()
            self.assertFalse(any(item.get("match_id") == 999999 for item in game.s["news"]))
            reference_date = reference_date_from_state(game.s)
            self.assertEqual(game.s["news"], retain_recent_news(game.s["news"], reference_date))

    def test_overview_news_headlines_are_unique_within_visible_feed(self):
        entries = [
            {"headline": "Thẻ đỏ trở thành bước ngoặt", "home": "Black Sam", "away": "Hunter", "match_id": 1},
            {"headline": "Thẻ đỏ trở thành bước ngoặt", "home": "Fury", "away": "Knight", "match_id": 2},
            {"headline": "Thẻ đỏ trở thành bước ngoặt", "home": "Fury", "away": "Knight", "match_id": 3,
             "date": "2026-10-30"},
        ]
        unique = unique_headlines(entries)
        titles = [row["headline"] for row in unique]
        self.assertEqual(len(titles), len(set(titles)))
        self.assertEqual(entries[0]["headline"], "Thẻ đỏ trở thành bước ngoặt", "Helper không mutate save data")

    def test_future_slot_fixture_projection_is_read_only_and_does_not_enable_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030)
            current = game.next_slot()
            future = next(slot for slot in game.s["slots"]
                          if not slot.get("played") and slot.get("id") != current.get("id"))
            before_matches = json.dumps(game.s["matches"], ensure_ascii=False, sort_keys=True)
            self.assertEqual(game.slot_fixture_specs(future), [],
                             "Future slot must remain unavailable to interactive simulation")
            projected = game.slot_fixture_specs(future, allow_future=True)
            self.assertTrue(projected, "Calendar overview should be able to preview future fixtures")
            self.assertEqual(json.dumps(game.s["matches"], ensure_ascii=False, sort_keys=True), before_matches)
            self.assertEqual(game.next_slot().get("id"), current.get("id"))

    def test_match_news_detects_comeback_after_equalizing_before_taking_lead(self):
        from types import SimpleNamespace
        from sl_match.news import match_summary
        game = SimpleNamespace(s={"players": {}})
        match = {
            "id": 91, "season": 2026, "date": "2026-08-01", "comp": "Premier League", "round": "Vòng 1",
            "home": "Home FC", "away": "Away FC", "hg": 3, "ag": 2, "winner": "Home FC",
            "goals": [
                {"minute": 10, "team": "Away FC", "scorer": "Away One"},
                {"minute": 30, "team": "Home FC", "scorer": "Home One"},
                {"minute": 55, "team": "Away FC", "scorer": "Away Two"},
                {"minute": 70, "team": "Home FC", "scorer": "Home Two"},
                {"minute": 88, "team": "Home FC", "scorer": "Home Three"},
            ],
        }
        article = match_summary(game, match)
        self.assertEqual(article["angle"], "comeback")
        self.assertIn("ngược dòng", article["headline"])

    def test_overview_news_is_recorded_immediately_for_partial_live_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030)
            slot = game.next_slot()
            spec = game.slot_fixture_specs(slot)[0]
            preview = game.preview_slot_fixture(spec["spec_key"])
            match, _ = game.commit_slot_fixture(preview, persist=False)
            summaries = [item for item in game.s.get("news", []) if item.get("kind") == "match_summary"]
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["match_id"], match["id"])
            self.assertEqual(game.slot_progress()[0], 0)  # Slot is not closed until all fixtures are played.

    def test_old_save_backfills_one_overview_news_item_per_played_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030)
            game.advance_slot(persist=False)
            match_count = len(game.s["matches"])
            game.save()
            database = SuperLeagueDatabase(Path(tmp) / "super_league.db")
            blob = database.load_career()
            blob["state"]["news"] = [item for item in blob["state"].get("news", [])
                                      if item.get("kind") != "match_summary"]
            database.save_career(blob, season_archives=database.load_season_archives())
            resumed = Game(data_dir=tmp)
            summaries = [item for item in resumed.s.get("news", []) if item.get("kind") == "match_summary"]
            self.assertEqual(len(summaries), match_count)
            # Re-sync must be idempotent: it must not duplicate existing summaries.
            resumed._sync_match_news()
            self.assertEqual(len([item for item in resumed.s["news"] if item.get("kind") == "match_summary"]), match_count)

    def test_startup_pruning_is_persisted_to_career_state_and_sqlite_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030)
            game.advance_slot(persist=True)
            database = SuperLeagueDatabase(Path(tmp) / "super_league.db")
            blob = database.load_career()
            state = blob["state"]
            state.setdefault("news", []).append({
                "kind": "match_summary", "date": "2020-01-01", "season": 2020,
                "match_id": 999999, "headline": "Tin rất cũ", "text": "Tin rất cũ",
            })
            import sqlite3
            conn = sqlite3.connect(database.path)
            try:
                conn.execute("UPDATE career_state SET state_json=? WHERE id=1",
                             (json.dumps(state, ensure_ascii=False),))
                conn.execute("INSERT OR REPLACE INTO news_items VALUES(?,?,?)",
                             ("stale-news-row", 2020, json.dumps(state["news"][-1])))
                conn.commit()
            finally:
                conn.close()
            resumed = Game(data_dir=tmp)
            self.assertFalse(any(item.get("match_id") == 999999 for item in resumed.s["news"]))
            cleaned = database.load_career()["state"]["news"]
            self.assertFalse(any(item.get("match_id") == 999999 for item in cleaned))
            conn = sqlite3.connect(database.path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM news_items WHERE season < 2026").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM news_items WHERE news_id='stale-news-row'").fetchone()[0], 0)
            finally:
                conn.close()

    def test_old_plain_match_news_is_upgraded_in_place_to_editorial_article(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030)
            game.advance_slot(persist=False)
            game.save()
            database = SuperLeagueDatabase(Path(tmp) / "super_league.db")
            blob = database.load_career()
            summaries = [item for item in blob["state"].get("news", []) if item.get("kind") == "match_summary"]
            self.assertTrue(summaries)
            for item in summaries:
                item.pop("headline", None)
                item.pop("body", None)
                item.pop("format_version", None)
                item["text"] = "2026-08-07 · Premier League: Home 1–0 Away. Bàn thắng: 20' Player."
            database.save_career(blob, season_archives=database.load_season_archives())

            resumed = Game(data_dir=tmp)
            upgraded = [item for item in resumed.s.get("news", []) if item.get("kind") == "match_summary"]
            self.assertEqual(len(upgraded), len(summaries), "Nâng cấp tại chỗ, không sinh tin trùng")
            self.assertTrue(all(item.get("format_version") == 2 for item in upgraded))
            self.assertTrue(all(item.get("headline") and item.get("body") for item in upgraded))
            resumed._sync_match_news()
            self.assertEqual(len([item for item in resumed.s["news"] if item.get("kind") == "match_summary"]), len(summaries))

    def test_enhanced_match_model_report_and_read_only_replay_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=24680)
            result = game.advance_slot()
            self.assertEqual(len(result["matches"]), 4)
            match = result["matches"][0]
            self.assertEqual(match.get("match_engine"), "enhanced")
            self.assertIn("ctx_text", match)
            self.assertIn("weather", match.get("ctx", {}))
            self.assertIn("player_stats", match)
            self.assertIn("poss", match.get("advanced_stats", {}))
            expected_team_metrics = {row[0] for row in wnl_stats.STAT_ROWS}
            self.assertEqual(len(expected_team_metrics), 24)
            self.assertTrue(expected_team_metrics.issubset(match.get("advanced_stats", {})))
            for side in ("h", "a"):
                self.assertTrue(match["player_stats"][side])
                first_player_stats = next(iter(match["player_stats"][side].values()))
                self.assertTrue({"min", "g", "a", "sh", "sot", "xg", "pass", "pok", "kp", "tkl", "int", "clr", "sv", "fl", "yc", "rc", "rt"}.issubset(first_player_stats))
            self.assertTrue(match.get("xi_h"))
            self.assertTrue(match.get("xi_a"))
            self.assertTrue(match.get("minutes_h"))
            self.assertTrue(match.get("minutes_a"))

            packed = match.get("live_bundle")
            self.assertIsInstance(packed, str)
            self.assertTrue(packed.startswith("zlib-json:v1:"))
            prep = restore_live_bundle(packed)
            self.assertIsInstance(prep["A"]["out"], set)
            live = LiveMatch(match["home"], match["away"], prep, neutral=match["neutral"],
                             knockout=bool(match.get("winner")))
            self.assertTrue(live.beats)
            self.assertEqual(live.r["hg"], match["hg"])
            self.assertEqual(live.r["ag"], match["ag"])

            report = report_for_match(match, game.s["players"], game.s.get("match_refs"))
            self.assertIn("stats", report)
            self.assertTrue(report["players"]["h"])
            self.assertTrue(report["players"]["a"])
            self.assertEqual(len(game.s["team_elo_history"]), 2)
            self.assertTrue(game.s.get("news") or game.s.get("last_news"))

            game.save()
            resumed = Game(data_dir=tmp)
            saved_match = resumed.s["matches"][0]
            self.assertTrue(restore_live_bundle(saved_match["live_bundle"]))

    def test_team_recent_form_and_momentum_are_bounded_and_distinct(self):
        self.assertEqual(match_engine.recent_form_score(["D"] * 8), 0.0)
        self.assertEqual(match_engine.recent_form_score(["W"] * 8), 1.0)
        self.assertEqual(match_engine.recent_form_score(["L"] * 8), -1.0)
        # Trận mới có trọng số cao hơn: đảo thứ tự phải đổi điểm form.
        self.assertGreater(
            match_engine.recent_form_score(["L", "L", "L", "W", "W", "W"]),
            match_engine.recent_form_score(["W", "W", "W", "L", "L", "L"]),
        )
        hot = {"mom": 1.0, "recent_form": 1.0, "venue_form": 1.0}
        cold = {"mom": -1.0, "recent_form": -1.0, "venue_form": -1.0}
        self.assertGreater(match_engine.team_form_multiplier(hot), 1.0)
        self.assertLess(match_engine.team_form_multiplier(cold), 1.0)
        self.assertLessEqual(match_engine.team_form_multiplier(hot), 1.10)
        self.assertGreaterEqual(match_engine.team_form_multiplier(cold), 0.90)
        self.assertLess(match_engine.decay_mom(0.8, 30), 0.8)

    def test_enhanced_match_exposes_strength_form_tactic_and_venue_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=13579)
            result = game.advance_slot(persist=False)
            match = result["matches"][0]
            factors = match.get("engine_factors", {})
            self.assertEqual(set(factors), {"home", "away"})
            for side in ("home", "away"):
                self.assertTrue({"attack", "control", "defence", "gk", "momentum",
                                 "recent_form", "venue_form", "initial_tactic"}.issubset(factors[side]))
                self.assertIn(factors[side]["initial_tactic"], {"attack", "balanced", "defend"})
                self.assertGreater(factors[side]["attack"], 0)
                self.assertGreater(factors[side]["defence"], 0)
            hs = game.s["team_momentum"][match["home"]]
            aws = game.s["team_momentum"][match["away"]]
            self.assertEqual(len(hs["last"]), 1)
            self.assertEqual(len(aws["last"]), 1)
            self.assertEqual(len(hs["home_last"]), 1)
            self.assertEqual(len(aws["away_last"]), 1)
            self.assertEqual(len(hs["away_last"]), 0)
            self.assertEqual(len(aws["home_last"]), 0)
            self.assertEqual(hs["date"], match["date"])
            self.assertEqual(aws["date"], match["date"])

    def test_cup_fixture_can_be_played_individually_without_duplicating_slot_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=123)
            while game.next_slot()["kind"] != "cup_r1":
                game.advance_slot(persist=False)

            slot = game.next_slot()
            specs = game.slot_fixture_specs(slot)
            self.assertEqual(len(specs), 8)
            selected = specs[0]
            before = len(game.s["matches"])
            preview = game.preview_slot_fixture(selected["spec_key"])
            self.assertEqual(len(game.s["matches"]), before)
            self.assertTrue(preview["data"].get("live_bundle"))

            committed, _message = game.commit_slot_fixture(preview)
            self.assertEqual(len(game.s["matches"]), before + 1)
            self.assertEqual(committed["home"], selected["home"])
            self.assertEqual(len(game.slot_fixture_specs(slot)), 7)
            self.assertFalse(slot["played"])
            self.assertEqual(len(game.s["cup"]["bracket"]["r1"]), 1)

            result = game.advance_slot(persist=False)
            self.assertTrue(slot["played"])
            self.assertEqual(len(result["matches"]), 8)
            ids = game.s["cup"]["bracket"]["r1"]
            self.assertEqual(len(ids), 8)
            self.assertEqual(len(set(ids)), 8)
            self.assertEqual(len(game.s["matches"]), before + 8)

    def test_c1_double_elimination_and_group_slots_accept_individual_live_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=987)
            while game.next_slot()["kind"] != "c1_pi_r1":
                game.advance_slot(persist=False)

            playin_slot = game.next_slot()
            specs = game.slot_fixture_specs(playin_slot)
            self.assertEqual(len(specs), 4)
            selected = specs[0]
            before = len(game.s["matches"])
            preview = game.preview_slot_fixture(selected["spec_key"])
            self.assertEqual(len(game.s["matches"]), before)
            self.assertTrue(preview["data"].get("live_bundle"))
            game.commit_slot_fixture(preview)
            self.assertEqual(len(game.s["matches"]), before + 1)
            self.assertEqual(len(game.slot_fixture_specs(playin_slot)), 3)

            result = game.advance_slot(persist=False)
            self.assertTrue(playin_slot["played"])
            self.assertEqual(len(result["matches"]), 4)
            for group_name in ("X", "Y"):
                entry = game.s["c1"]["playin"][group_name]
                self.assertEqual(len(entry["r1"]), 2)
                self.assertTrue(all(entry["r1"]))

            # Resolve both double-elimination branches and reach the group stage.
            while game.next_slot()["kind"] != "c1_group1":
                game.advance_slot(persist=False)
            group_slot = game.next_slot()
            group_specs = game.slot_fixture_specs(group_slot)
            self.assertEqual(len(group_specs), 4)
            first_group_match = group_specs[0]
            group_preview = game.preview_slot_fixture(first_group_match["spec_key"])
            matches_before_group = len(game.s["matches"])
            game.commit_slot_fixture(group_preview)
            group_a_ids = game.s["c1"]["groups"]["A"]["match_ids"][0]
            self.assertEqual(len(group_a_ids), 1)
            self.assertEqual(len(game.s["matches"]), matches_before_group + 1)

            # The slot action fills only the remaining three fixtures.
            group_result = game.advance_slot(persist=False)
            self.assertTrue(group_slot["played"])
            self.assertEqual(len(group_result["matches"]), 4)
            self.assertEqual(len(game.s["c1"]["groups"]["A"]["match_ids"][0]), 2)
            self.assertEqual(len(game.s["c1"]["groups"]["B"]["match_ids"][0]), 2)

            while game.next_slot()["kind"] != "c1_group3":
                game.advance_slot(persist=False)
            game.advance_slot(persist=False)
            for group_name in ("A", "B"):
                table = game.s["c1"]["group_tables"][group_name]
                self.assertEqual(len(table), 4)
                self.assertTrue(all(row["p"] == 3 for row in table))
                for metric in ("w", "d", "l", "gf", "ga", "gd", "pts", "elo"):
                    self.assertTrue(all(metric in row for row in table))

    def test_single_fixture_live_preview_only_commits_selected_next_round_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=24681)
            slot = game.next_slot()
            self.assertEqual(slot["kind"], "league")
            fixtures = game.league_slot_fixtures(slot)
            self.assertEqual(len(fixtures), 4)
            self.assertEqual({fixture["tier"] for fixture in fixtures}, {"fc2"})
            selected = fixtures[0]

            # Opening the live window prepares a deterministic match but must not write it.
            preview = game.preview_league_fixture(selected["tier"], selected["home"], selected["away"])
            self.assertEqual(len(game.s["matches"]), 0)
            self.assertEqual(game.s["next_match_id"], 1)
            prep = restore_live_bundle(preview["data"]["live_bundle"])
            self.assertIsNotNone(prep)
            live = LiveMatch(preview["home"], preview["away"], prep)
            self.assertTrue(live.beats)

            # Full-time commits exactly that fixture. The rest of the slot remains pending.
            committed, _message = game.commit_league_fixture(preview)
            self.assertEqual(len(game.s["matches"]), 1)
            self.assertEqual(committed["home"], selected["home"])
            self.assertEqual(committed["away"], selected["away"])
            self.assertFalse(slot["played"])
            self.assertEqual(len(game.league_slot_fixtures(slot)), 3)
            self.assertEqual(game.next_slot()["id"], slot["id"])

            # A past fixture and a fixture from a later round cannot be opened again.
            with self.assertRaisesRegex(RuntimeError, "slot gần nhất|đã đá"):
                game.preview_league_fixture(selected["tier"], selected["home"], selected["away"])
            later = game.s["league_fixtures"][selected["tier"]][1][0]
            with self.assertRaisesRegex(RuntimeError, "slot gần nhất"):
                game.preview_league_fixture(selected["tier"], later[0], later[1])

            # The ordinary slot action safely simulates only the other 3 fixtures
            # from this tier/day, without duplicating the manually played fixture.
            result = game.advance_slot()
            self.assertTrue(slot["played"])
            self.assertEqual(len(result["matches"]), 4)
            self.assertEqual(len(game.s["matches"]), 4)
            self.assertEqual(len({m["id"] for m in game.s["matches"]}), 4)
            selected_records = [m for m in game.s["matches"]
                                if m["stage"] == selected["stage"] and
                                m["home"] == selected["home"] and m["away"] == selected["away"]]
            self.assertEqual(len(selected_records), 1)

    def test_academy_migration_keeps_every_youth_between_16_and_20_without_advancing_rng(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=778899)
            for team, entries in game.s["academy"].items():
                for index, entry in enumerate(entries):
                    entry["meta"]["age"] = 21.0 + (index % 5)
            game.save()
            expected_rng_state = game.rng.getstate()

            resumed = Game(data_dir=tmp, seed=778899)
            self.assertEqual(resumed.rng.getstate(), expected_rng_state,
                             "Migration học viện không được làm lệch chuỗi RNG của career")
            self.assertEqual(len(resumed.s.get("academy_alumni", [])), 24 * 6)
            all_academy_ids = []
            for team, entries in resumed.s["academy"].items():
                self.assertEqual(len(entries), 6, team)
                for entry in entries:
                    age = float(entry["meta"]["age"])
                    self.assertGreaterEqual(age, 16.0)
                    self.assertLessEqual(age, 20.0)
                    all_academy_ids.append(entry["player"]["player_id"])
            self.assertEqual(len(all_academy_ids), len(set(all_academy_ids)))
            alumni_ids = {x["player"]["player_id"] for x in resumed.s["academy_alumni"]}
            self.assertFalse(alumni_ids.intersection(all_academy_ids))
            self.assertEqual(len(resumed.s["rosters"]), 24)
            self.assertTrue(all(len(ids) == 23 for ids in resumed.s["rosters"].values()))

    def test_academy_talent_groups_match_requested_clubs_and_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=220026)
            elite_names = ["BLack Sam", "BlackSam", "Black Hunter", "Hunter", "AssassinS FC",
                           "AssassinS", "Fury Fighter", "Fury", "Knight Castle", "Knight"]
            middle_names = ["Yellow Thunder FC", "Yellow", "Poseidon FC", "Poseidon",
                            "Savior FC", "Savior", "Silver Moon", "Silver"]
            for team in elite_names:
                self.assertEqual(game.academy_talent_profile(team)["group"], "elite", team)
                self.assertEqual(game.academy_talent_profile(team)["superstar_probability"], .18)
            for team in middle_names:
                self.assertEqual(game.academy_talent_profile(team)["group"], "middle", team)
                self.assertEqual(game.academy_talent_profile(team)["superstar_probability"], .10)
            self.assertEqual(game.academy_talent_profile("Apollo FC")["group"], "standard")
            self.assertEqual(game.academy_talent_profile("Apollo FC")["superstar_probability"], .025)

    def test_academy_superstar_rates_and_attributes_follow_club_groups(self):
        class FixedChanceRandom:
            """Delegate all random work except the single superstar probability roll."""
            def __init__(self, source, chance_roll):
                self.source = source
                self.chance_roll = chance_roll
            def random(self):
                return self.chance_roll
            def __getattr__(self, name):
                return getattr(self.source, name)

        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030)
            scenarios = [
                ("BLack Sam", (90, 99), (78, 86)),
                ("Yellow Thunder FC", (87, 98), (74, 83)),
                ("Apollo FC", (85, 96), (70, 80)),
            ]
            for index, (team, expected_potential, expected_ovr) in enumerate(scenarios):
                profile = game.academy_talent_profile(team)
                self.assertGreater(profile["superstar_probability"], 0)
                # A roll just below the configured rate must create a superstar.
                original_rng = game.rng
                game.rng = FixedChanceRandom(original_rng, max(0.0, profile["superstar_probability"] - 0.0001))
                row, meta = game._make_youth_row(team, "ST", 2027,
                                                 f"TALENT_SUCCESS_{index}", academy=True)
                game.rng = original_rng
                self.assertTrue(meta["is_superstar"])
                self.assertEqual(meta["academy_talent_group"], profile["group"])
                self.assertGreaterEqual(meta["potential"], expected_potential[0])
                self.assertLessEqual(meta["potential"], expected_potential[1])
                self.assertGreaterEqual(row["overall_rating"], expected_ovr[0])
                self.assertLessEqual(row["overall_rating"], expected_ovr[1])
                self.assertGreaterEqual(meta["age"], 16)
                self.assertLessEqual(meta["age"], 20)

                # A roll above the rate must stay on the regular development path.
                game.rng = FixedChanceRandom(original_rng, min(.9999, profile["superstar_probability"] + 0.0001))
                _row, regular_meta = game._make_youth_row(team, "ST", 2027,
                                                          f"TALENT_REGULAR_{index}", academy=True)
                game.rng = original_rng
                self.assertFalse(regular_meta["is_superstar"])
                self.assertEqual(regular_meta["academy_talent_group"], profile["group"])


    def test_academy_talent_config_rejects_invalid_probability(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            cfg["players"]["academy_talent"]["profiles"]["elite"]["superstar_probability"] = 1.25
            config_path = Path(tmp) / "invalid_academy.json"
            config_path.write_text(json.dumps(cfg), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "superstar_probability"):
                Game(data_dir=Path(tmp) / "data", config_path=config_path, fresh=True, seed=9)

    def test_competition_scorer_and_assist_tables_are_separate_by_competition(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=1313)
            teams = list(game.s["rosters"])
            t1, t2, t3, t4 = teams[:4]
            p1, p2 = game.s["rosters"][t1][:2]
            p3 = game.s["rosters"][t2][0]
            p4 = game.s["rosters"][t3][0]
            p5 = game.s["rosters"][t4][0]

            def match(comp, home, away, scorer, assist=None, scorer_again=False):
                goals = [{"scorer_id": scorer, "scorer": game.s["players"][scorer]["common_name"],
                          "assist_id": assist, "assist": game.s["players"][assist]["common_name"] if assist else None,
                          "team": home, "side": "home", "minute": 15}]
                if scorer_again:
                    goals.append({"scorer_id": scorer, "scorer": game.s["players"][scorer]["common_name"],
                                  "assist_id": None, "assist": None, "team": home, "side": "home", "minute": 62})
                return {"played": True, "comp": comp, "home": home, "away": away,
                        "xi_h": [scorer, assist] if assist else [scorer], "xi_a": [],
                        "subs_h": [], "subs_a": [], "goals": goals}

            game.s["matches"] = [
                match("Premier League", t1, t2, p1, p2, scorer_again=True),
                match("Football Championship 1", t3, t4, p4, p5),
                match("C1 Play-in", t1, t3, p3, p1),
                match("C1", t2, t4, p5, p4),
            ]
            premier = game.competition_player_leaderboards("Premier League")
            self.assertEqual(premier["scorers"][0]["player_id"], p1)
            self.assertEqual(premier["scorers"][0]["goals"], 2)
            self.assertEqual(premier["assists"][0]["player_id"], p2)
            self.assertEqual(premier["assists"][0]["assists"], 1)
            fc1 = game.competition_player_leaderboards("Football Championship 1")
            self.assertEqual(fc1["scorers"][0]["player_id"], p4)
            c1 = game.competition_player_leaderboards("C1")
            c1_scorers = {row["player_id"]: row["goals"] for row in c1["scorers"]}
            self.assertEqual(c1_scorers, {p3: 1, p5: 1})
            c1_assists = {row["player_id"]: row["assists"] for row in c1["assists"]}
            self.assertEqual(c1_assists, {p1: 1, p4: 1})

    def test_dark_theme_text_and_event_colors_meet_wcag_aa_contrast(self):
        def luminance(color):
            channels = [int(color[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
            return sum(c * w for c, w in zip(linear, (0.2126, 0.7152, 0.0722)))

        def ratio(fg, bg):
            high, low = sorted((luminance(fg), luminance(bg)), reverse=True)
            return (high + 0.05) / (low + 0.05)

        for fg in (TEXT, MUTED, DIM):
            for bg in (BG, SIDEBAR, SURFACE, SURFACE_2, RAISED):
                self.assertGreaterEqual(ratio(fg, bg), 4.5, f"{fg} on {bg}")
        self.assertGreaterEqual(ratio("#ffffff", SELECT), 4.5)
        for name, (background, foreground) in EVENT_STYLES.items():
            self.assertGreaterEqual(ratio(foreground, background), 4.5, name)

    def test_single_fixture_then_simulate_same_competition_in_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=43121)
            slot = game.next_slot()
            specs = game.slot_fixture_specs(slot, include_played=False)
            self.assertEqual(len(specs), 4)
            competition = specs[0]["comp"]
            self.assertEqual(sum(1 for item in specs if item["comp"] == competition), 4)
            preview = game.preview_slot_fixture(specs[0]["spec_key"])
            first, _message = game.commit_slot_fixture(preview)
            self.assertEqual(len(game.s["matches"]), 1)
            self.assertEqual(first["id"], 1)
            self.assertFalse(slot.get("played"))
            self.assertEqual(len(game.slot_fixture_specs(slot, include_played=False)), 3)

            grouped = game.simulate_slot_competition(slot["id"], competition)
            self.assertEqual(len(grouped["matches"]), 3)
            self.assertTrue(slot.get("played"), "Cả 4 trận của giải trong ngày phải hoàn tất slot riêng của giải")
            self.assertEqual(len(game.s["matches"]), 4)
            self.assertTrue(all(m["comp"] == competition for m in game.s["matches"]))
            self.assertTrue(grouped.get("competition_complete"))
            self.assertEqual(game.next_slot().get("tier"), "premier")
            ids = [m["id"] for m in game.s["matches"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(sum(1 for m in game.s["matches"] if m["home"] == first["home"] and m["away"] == first["away"]), 1)

    def test_same_kickoff_scoreboard_tracks_companion_matches_and_commits_them_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=551100)
            slot = game.next_slot()
            specs = game.slot_fixture_specs(slot, include_played=False)
            selected_spec = specs[0]
            preview = game.preview_slot_fixture(selected_spec["spec_key"])
            companions = game.preview_slot_companions(preview)

            self.assertEqual(len(companions), 3)
            self.assertEqual([item["match_id"] for item in companions], list(range(2, 5)))
            self.assertTrue(all(item["comp"] == selected_spec["comp"] for item in companions))
            self.assertEqual(len(game.s["matches"]), 0, "Preview cùng giờ không được ghi vào career")

            board_before = game.slot_concurrent_scoreboard(preview)
            self.assertEqual(len(board_before), 3)
            self.assertTrue(all(row["comp"] == selected_spec["comp"] for row in board_before))
            self.assertNotIn(selected_spec["spec_key"], {row["spec_key"] for row in board_before})
            self.assertTrue(all(row["status"] == "Chưa đá" for row in board_before))

            # All companion score timelines advance with the selected match clock.
            trackers = [ConcurrentFixtureTracker(item) for item in companions]
            # Explicitly verify that an in-progress companion updates goal and red-card
            # indicators at the same simulated minute as the selected Live Match.
            probe = ConcurrentFixtureTracker(companions[0])
            home_pid = probe.lm.P["h"]["xi_ids"][0]
            away_pid = probe.lm.P["a"]["xi_ids"][0]
            probe.lm.beats = [
                {"T": 10.0, "k": "goal", "side": "h", "frm": home_pid},
                {"T": 20.0, "k": "red", "side": "a", "frm": away_pid},
                {"T": 30.0, "k": "end"},
            ]
            live_snapshot = probe.update_to(22.0)
            self.assertEqual(live_snapshot["score"], "1–0")
            self.assertEqual((live_snapshot["live_hg"], live_snapshot["live_ag"]), (1, 0))
            self.assertEqual(len(live_snapshot["goal_home"]), 1)
            self.assertEqual(live_snapshot["goal_home"][0]["player"], probe.lm.name("h", home_pid))
            self.assertEqual(live_snapshot["goal_home"][0]["minute"], "10'")
            self.assertEqual(live_snapshot["goal_away"], [])
            self.assertEqual(len(live_snapshot["red_away"]), 1)
            self.assertIn("LIVE", live_snapshot["status"])

            # A goal waiting for VAR must disappear from both the score and scorer
            # list when the decision overturns it.
            var_probe = ConcurrentFixtureTracker(companions[1])
            var_pid = var_probe.lm.P["h"]["xi_ids"][0]
            var_probe.lm.beats = [
                {"T": 10.0, "k": "goal", "side": "h", "frm": var_pid, "var": "pending"},
                {"T": 11.0, "k": "var_decision", "side": "h", "goal": True, "overturn": True},
            ]
            overturned = var_probe.update_to(12.0)
            self.assertEqual((overturned["live_hg"], overturned["live_ag"]), (0, 0))
            self.assertEqual(overturned["goal_home"], [])

            for tracker, companion in zip(trackers, companions):
                snapshot = tracker.update_to(9999, force_finish=True)
                self.assertEqual(snapshot["score"],
                                 f"{companion['data']['hg']}–{companion['data']['ag']}" +
                                 (f" (P {companion['data']['pens']['home']}–{companion['data']['pens']['away']})"
                                  if companion['data'].get("pens") else ""))
                self.assertEqual(len(snapshot["red_home"]), sum(
                    1 for card in companion["data"].get("cards_h", []) if card.get("c") in ("r", "yr")
                ))
                self.assertEqual(len(snapshot["red_away"]), sum(
                    1 for card in companion["data"].get("cards_a", []) if card.get("c") in ("r", "yr")
                ))
                self.assertEqual(len(snapshot["goal_home"]), companion["data"]["hg"])
                self.assertEqual(len(snapshot["goal_away"]), companion["data"]["ag"])

            selected_match, _ = game.commit_slot_fixture(preview)
            self.assertEqual(selected_match["id"], 1)
            batch = game.commit_prepared_slot_fixtures(companions)
            self.assertEqual(len(batch["matches"]), 3)
            self.assertTrue(slot["played"], "Bốn trận cùng giải đã hoàn tất slot của ngày đó")
            self.assertEqual(len(game.s["matches"]), 4)
            self.assertEqual([m["id"] for m in game.s["matches"]], list(range(1, 5)))
            self.assertEqual({m["comp"] for m in game.s["matches"]}, {selected_spec["comp"]})
            self.assertEqual(game.next_slot().get("tier"), "premier")

            board_after = game.slot_concurrent_scoreboard(preview)
            self.assertEqual(len(board_after), 3)
            self.assertTrue(all(row["status"] == "FT" for row in board_after))
            by_key = {item["spec_key"]: item for item in companions}
            for row in board_after:
                companion = by_key[row["spec_key"]]
                self.assertEqual(row["score"], f"{companion['data']['hg']}–{companion['data']['ag']}")
            self.assertEqual(len({m["id"] for m in game.s["matches"]}), 4)

    def test_live_league_table_projects_selected_and_companion_scores_without_saving(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=551130)
            slot = game.next_slot()
            specs = game.slot_fixture_specs(slot, include_played=False)
            selected = specs[0]
            preview = game.preview_slot_fixture(selected["spec_key"])
            companions = game.preview_slot_companions(preview)
            self.assertEqual(len(companions), 3)

            provisional = [{
                "comp": preview["comp"], "stage": preview["stage"],
                "home": preview["home"], "away": preview["away"],
                "hg": 2, "ag": 1,
            }]
            for index, companion in enumerate(companions):
                provisional.append({
                    "comp": companion["comp"], "stage": companion["stage"],
                    "home": companion["home"], "away": companion["away"],
                    "hg": 0 if index % 2 == 0 else 3,
                    "ag": 0 if index % 2 == 0 else 1,
                })

            before_count = len(game.s["matches"])
            elo_before = dict(game.s["elo"])
            snapshot = game.live_league_table(preview, provisional)
            self.assertTrue(snapshot["available"])
            self.assertEqual(len(snapshot["rows"]), 8)
            self.assertEqual(len(game.s["matches"]), before_count,
                             "Live projection must not commit fixtures or mutate career matches")
            self.assertEqual(game.s["elo"], elo_before,
                             "Provisional Elo calculations must remain read-only")
            by_team = {row["team"]: row for row in snapshot["rows"]}
            home = by_team[preview["home"]]
            away = by_team[preview["away"]]
            self.assertEqual((home["p"], home["w"], home["pts"], home["gf"], home["ga"]),
                             (1, 1, 3, 2, 1))
            self.assertEqual((away["p"], away["l"], away["pts"], away["gf"], away["ga"]),
                             (1, 1, 0, 1, 2))
            self.assertEqual(snapshot["live_teams"],
                             {team for item in provisional for team in (item["home"], item["away"])})
            self.assertNotEqual(home["elo"], game.s["elo"][preview["home"]],
                                "Live table must provisionally apply the existing Elo tiebreak update")

            # Once the very same previews are committed, the regular saved table
            # must equal the projected live table, with no double-counted fixtures.
            committed_projection = [{
                "comp": preview["comp"], "stage": preview["stage"],
                "home": preview["home"], "away": preview["away"],
                "hg": preview["data"]["hg"], "ag": preview["data"]["ag"],
            }]
            committed_projection.extend({
                "comp": item["comp"], "stage": item["stage"],
                "home": item["home"], "away": item["away"],
                "hg": item["data"]["hg"], "ag": item["data"]["ag"],
            } for item in companions)
            projected_final = game.live_league_table(preview, committed_projection)
            game.commit_slot_fixture(preview, persist=False)
            game.commit_prepared_slot_fixtures(companions, persist=False)
            saved_final = game.live_league_table(preview, [])
            fields = ("p", "w", "d", "l", "gf", "ga", "gd", "pts", "elo")
            self.assertEqual(
                [(row["team"],) + tuple(row[key] for key in fields) for row in projected_final["rows"]],
                [(row["team"],) + tuple(row[key] for key in fields) for row in saved_final["rows"]],
                "After commit, saved standings must match the projected full-time standings exactly",
            )

    def test_live_premier_phase2_table_is_group_specific_and_carries_phase1_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=551131)
            premier_teams = [team for team, tier in game.s["season_tiers"].items() if tier == "premier"]
            champion = premier_teams[:4]
            challenge = premier_teams[4:]
            game.s["league_phase2"] = {
                "initialized": True,
                "premier": {
                    "champion_teams": champion,
                    "challenge_teams": challenge,
                },
            }
            game.s["matches"] = [
                {"id": 1, "played": True, "comp": "Premier League", "stage": "league_premier_1",
                 "home": champion[0], "away": challenge[0], "hg": 2, "ag": 0},
                {"id": 2, "played": True, "comp": "Premier League", "stage": "league_p2_premier_champion_1",
                 "home": champion[1], "away": champion[2], "hg": 1, "ag": 0},
                {"id": 3, "played": True, "comp": "Premier League", "stage": "league_p2_premier_challenge_1",
                 "home": challenge[0], "away": challenge[1], "hg": 4, "ag": 0},
                {"id": 4, "played": True, "comp": "Cup", "stage": "cup_r1",
                 "home": champion[3], "away": champion[2], "hg": 8, "ag": 0},
            ]
            preview = {
                "comp": "Premier League", "stage": "league_p2_premier_champion_2",
                "home": champion[0], "away": champion[1],
                "_meta": {"group": "champion"},
            }
            provisional = [
                {"comp": "Premier League", "stage": preview["stage"],
                 "home": champion[0], "away": champion[1], "hg": 1, "ag": 1},
                {"comp": "Premier League", "stage": "league_p2_premier_challenge_2",
                 "home": challenge[2], "away": challenge[3], "hg": 5, "ag": 0},
            ]
            snapshot = game.live_league_table(preview, provisional)
            self.assertTrue(snapshot["available"])
            self.assertIn("CHAMPION", snapshot["title"])
            self.assertEqual({row["team"] for row in snapshot["rows"]}, set(champion))
            by_team = {row["team"]: row for row in snapshot["rows"]}
            # Phase-1 points are carried into Champion; the live 1-1 adds one point
            # to each of the two Champion teams. Cup and Challenge matches are ignored.
            self.assertEqual((by_team[champion[0]]["p"], by_team[champion[0]]["pts"]), (2, 4))
            self.assertEqual((by_team[champion[1]]["p"], by_team[champion[1]]["pts"]), (2, 4))
            self.assertEqual(by_team[champion[3]]["p"], 0)

            # The Live Match standings tab must expose both phase-2 groups at once,
            # regardless of whether the selected fixture belongs to Champion.
            sections = {section["key"]: section for section in snapshot.get("sections", [])}
            self.assertEqual(set(sections), {"champion", "challenge"})
            self.assertEqual(len(sections["champion"]["rows"]), 4)
            self.assertEqual(len(sections["challenge"]["rows"]), 4)
            self.assertEqual({row["team"] for row in sections["champion"]["rows"]}, set(champion))
            self.assertEqual({row["team"] for row in sections["challenge"]["rows"]}, set(challenge))
            challenge_by_team = {row["team"]: row for row in sections["challenge"]["rows"]}
            # Challenge gets its own provisional 5-0; it is not leaked into Champion.
            self.assertEqual((challenge_by_team[challenge[2]]["p"],
                              challenge_by_team[challenge[2]]["pts"],
                              challenge_by_team[challenge[2]]["gd"]), (1, 3, 5))
            self.assertEqual((challenge_by_team[challenge[3]]["p"],
                              challenge_by_team[challenge[3]]["pts"]), (1, 0))
            self.assertEqual(sections["challenge"]["live_teams"], {challenge[2], challenge[3]})

            # Selecting a Challenge fixture still returns both tables, with the
            # legacy top-level rows pointing to the selected Challenge table.
            challenge_preview = {
                "comp": "Premier League", "stage": "league_p2_premier_challenge_3",
                "home": challenge[0], "away": challenge[1],
                "_meta": {"group": "challenge"},
            }
            challenge_snapshot = game.live_league_table(challenge_preview, provisional)
            self.assertEqual({section["key"] for section in challenge_snapshot["sections"]},
                             {"champion", "challenge"})
            self.assertEqual({row["team"] for row in challenge_snapshot["rows"]}, set(challenge))

    def test_non_league_live_match_returns_clear_standings_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=551132)
            preview = {"comp": "Cup", "stage": "cup_r1", "home": "A", "away": "B"}
            snapshot = game.live_league_table(preview, [{
                "comp": "Cup", "stage": "cup_r1", "home": "A", "away": "B", "hg": 4, "ag": 0,
            }])
            self.assertFalse(snapshot["available"])
            self.assertEqual(snapshot["rows"], [])
            self.assertIn("League", snapshot["note"])

    def test_same_kickoff_scoreboard_includes_previously_played_other_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=551103)
            slot = game.next_slot()
            specs = game.slot_fixture_specs(slot, include_played=False)
            first = game.preview_slot_fixture(specs[0]["spec_key"])
            first_match, _ = game.commit_slot_fixture(first)
            red_pid = game.s["rosters"][first_match["home"]][0]
            first_match["cards_h"] = [{"p": red_pid, "c": "r", "m": 67}]

            next_preview = game.preview_slot_fixture(specs[1]["spec_key"])
            rows = game.slot_concurrent_scoreboard(next_preview)
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row["comp"] == next_preview["comp"] for row in rows))
            first_row = next(row for row in rows if row["spec_key"] == specs[0]["spec_key"])
            self.assertEqual(first_row["status"], "FT")
            self.assertNotEqual(first_row["score"], "—")
            self.assertIsInstance(first_row["goal_home"], list)
            self.assertIsInstance(first_row["goal_away"], list)
            self.assertEqual(len(first_row["red_home"]), 1)
            self.assertIn("67'", first_row["red_home"][0])
            self.assertNotIn(specs[1]["spec_key"], {row["spec_key"] for row in rows})

    def test_postmatch_report_tabs_include_previously_played_matches_of_same_competition(self):
        class DummyRoot:
            @staticmethod
            def after_idle(callback):
                callback()

        class DummyApp:
            def __init__(self, game):
                self.game = game
                self.root = DummyRoot()
            def refresh_all(self):
                pass
            def toast(self, _message):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=551109)
            slot = game.next_slot()
            specs = game.slot_fixture_specs(slot, include_played=False)
            competition = specs[0]["comp"]
            first_preview = game.preview_slot_fixture(specs[0]["spec_key"])
            first_match, _ = game.commit_slot_fixture(first_preview, persist=False)

            # Open another fixture from that same competition after the first one is FT.
            selected_spec = next(item for item in game.slot_fixture_specs(slot, include_played=False)
                                 if item["comp"] == competition)
            preview = game.preview_slot_fixture(selected_spec["spec_key"])
            companions = game.preview_slot_companions(preview)
            app = DummyApp(game)
            proxy = _UpcomingMatchGameProxy(app, preview, simulate_remaining=True,
                                            companion_previews=companions)
            selected_result = {}
            proxy.commit_live(selected_result, None)

            reports = proxy.reports_after_live(selected_result)
            self.assertEqual(len(reports), 4)
            self.assertEqual({item["comp"] for item in reports}, {competition})
            self.assertIn(first_match["id"], {item["id"] for item in reports})
            self.assertEqual({item["id"] for item in reports}, {1, 2, 3, 4})

    def test_same_kickoff_batch_for_selected_fc1_does_not_simulate_premier_or_fc2(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=551108)
            # FC2 and Premier have separate Friday/Saturday slots; FC1 is Sunday.
            game.advance_slot(persist=False)
            game.advance_slot(persist=False)
            slot = game.next_slot()
            self.assertEqual(slot.get("tier"), "fc1")
            specs = game.slot_fixture_specs(slot, include_played=False)
            selected_spec = next(item for item in specs if item["comp"] == "Football Championship 1")
            preview = game.preview_slot_fixture(selected_spec["spec_key"])
            companions = game.preview_slot_companions(preview)
            self.assertEqual(len(companions), 3)
            self.assertTrue(all(item["comp"] == "Football Championship 1" for item in companions))
            self.assertTrue(all(item["comp"] != "Premier League" for item in companions))
            self.assertTrue(all(item["comp"] != "Football Championship 2" for item in companions))

            matches_before = len(game.s["matches"])
            game.commit_slot_fixture(preview, persist=False)
            result = game.commit_prepared_slot_fixtures(companions, persist=False)
            self.assertEqual(len(result["matches"]), 3)
            fc1_matches = [item for item in game.s["matches"][matches_before:]
                           if item["comp"] == "Football Championship 1"]
            self.assertEqual(len(fc1_matches), 4)
            self.assertEqual({item["comp"] for item in fc1_matches},
                             {"Football Championship 1"})
            self.assertTrue(slot["played"])
            scoreboard = game.slot_concurrent_scoreboard(preview)
            self.assertEqual(len(scoreboard), 3)
            self.assertTrue(all(item["comp"] == "Football Championship 1" for item in scoreboard))

    def test_simultaneous_batch_keeps_cup_and_c1_brackets_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=Path(tmp) / "cup", fresh=True, seed=551105)
            game.config["match_engine"] = "legacy"
            while game.next_slot() and game.next_slot().get("kind") != "cup_r1":
                game.advance_slot(persist=False)
            game.config["match_engine"] = "enhanced"
            slot = game.next_slot()
            specs = game.slot_fixture_specs(slot, include_played=False)
            self.assertEqual(len(specs), 8)
            selected = game.preview_slot_fixture(specs[0]["spec_key"])
            companions = game.preview_slot_companions(selected)
            game.commit_slot_fixture(selected, persist=False)
            result = game.commit_prepared_slot_fixtures(companions, persist=False)
            self.assertEqual(len(result["matches"]), 7)
            self.assertTrue(slot["played"])
            self.assertEqual(len(game.s["cup"]["bracket"]["r1"]), 8)
            self.assertEqual(len([m for m in game.s["matches"] if m.get("stage") == "cup_r1"]), 8)

        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=Path(tmp) / "c1", fresh=True, seed=551106)
            game.config["match_engine"] = "legacy"
            while game.next_slot() and game.next_slot().get("kind") != "c1_pi_r1":
                game.advance_slot(persist=False)
            game.config["match_engine"] = "enhanced"
            slot = game.next_slot()
            specs = game.slot_fixture_specs(slot, include_played=False)
            self.assertEqual(len(specs), 4)
            selected = game.preview_slot_fixture(specs[0]["spec_key"])
            companions = game.preview_slot_companions(selected)
            game.commit_slot_fixture(selected, persist=False)
            result = game.commit_prepared_slot_fixtures(companions, persist=False)
            self.assertEqual(len(result["matches"]), 3)
            self.assertTrue(slot["played"])
            for group in ("X", "Y"):
                self.assertEqual(len(game.s["c1"]["playin"][group]["r1"]), 2)
                self.assertTrue(all(game.s["c1"]["playin"][group]["r1"]))

    def test_live_match_proxy_commits_all_same_kickoff_companions_after_full_time(self):
        class DummyRoot:
            @staticmethod
            def after_idle(callback):
                callback()

        class DummyApp:
            def __init__(self, game):
                self.game = game
                self.root = DummyRoot()
                self.refresh_count = 0
                self.toast_message = ""
            def refresh_all(self):
                self.refresh_count += 1
            def toast(self, message):
                self.toast_message = message

        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=551104)
            slot = game.next_slot()
            spec = game.slot_fixture_specs(slot, include_played=False)[0]
            preview = game.preview_slot_fixture(spec["spec_key"])
            companions = game.preview_slot_companions(preview)
            app = DummyApp(game)
            proxy = _UpcomingMatchGameProxy(app, preview, simulate_remaining=True,
                                            companion_previews=companions)
            live_result = {}
            message = proxy.commit_live(live_result, None)
            self.assertEqual(len(game.s["matches"]), 4)
            self.assertTrue(slot["played"])
            self.assertEqual(live_result["id"], 1)
            self.assertIn("3 trận cùng giải", message)
            self.assertEqual(len(proxy.completed_matches), 4)
            self.assertEqual(len(proxy.reports_after_live(live_result)), 4)
            self.assertEqual({match["comp"] for match in proxy.reports_after_live(live_result)},
                             {preview["comp"]})
            self.assertEqual(app.refresh_count, 1)
            self.assertEqual(app.toast_message, message)

    def test_batch_simulation_stays_inside_selected_cup_or_c1_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=Path(tmp) / "cup", fresh=True, seed=551101)
            game.config["match_engine"] = "legacy"
            while game.next_slot() and game.next_slot().get("kind") != "cup_r1":
                game.advance_slot(persist=False)
            game.config["match_engine"] = "enhanced"
            slot = game.next_slot()
            self.assertEqual(slot["kind"], "cup_r1")
            specs = game.slot_fixture_specs(slot, include_played=False)
            self.assertEqual(len(specs), 8)
            preview = game.preview_slot_fixture(specs[0]["spec_key"])
            game.commit_slot_fixture(preview)
            batch = game.simulate_slot_competition(slot["id"], "Cup", persist=False)
            self.assertEqual(len(batch["matches"]), 7)
            self.assertTrue(slot["played"])
            self.assertEqual(len(game.s["cup"]["bracket"]["r1"]), 8)

        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=Path(tmp) / "c1", fresh=True, seed=551102)
            game.config["match_engine"] = "legacy"
            while game.next_slot() and game.next_slot().get("kind") != "c1_pi_r1":
                game.advance_slot(persist=False)
            game.config["match_engine"] = "enhanced"
            slot = game.next_slot()
            self.assertEqual(slot["kind"], "c1_pi_r1")
            specs = game.slot_fixture_specs(slot, include_played=False)
            self.assertEqual(len(specs), 4)
            preview = game.preview_slot_fixture(specs[0]["spec_key"])
            game.commit_slot_fixture(preview)
            batch = game.simulate_slot_competition(slot["id"], "C1 Play-in", persist=False)
            self.assertEqual(len(batch["matches"]), 3)
            self.assertTrue(slot["played"])
            self.assertEqual(len(game.s["c1"]["playin"]["X"]["r1"]), 2)
            self.assertEqual(len(game.s["c1"]["playin"]["Y"]["r1"]), 2)

    def test_career_trophies_and_all_time_legend_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=43122)
            teams = list(game.s["rosters"])
            premier, cup, c1, fc1, fc2 = teams[:5]
            p1, p2, p3, p4 = "legend-p1", "legend-p2", "legend-p3", "legend-p4"
            game.s["players"].update({
                p1: {"common_name": "Player One", "position": "ST", "club": premier},
                p2: {"common_name": "Player Two", "position": "CM", "club": premier},
                p3: {"common_name": "Player Three", "position": "CB", "club": fc1},
                p4: {"common_name": "Player Four", "position": "GK", "club": fc1},
            })
            game.s["season_stats"].update({
                p1: {"apps": 1, "mins": 90, "goals": 1, "assists": 0},
                p2: {"apps": 1, "mins": 90, "goals": 0, "assists": 1},
                p3: {"apps": 1, "mins": 90, "goals": 0, "assists": 0},
                p4: {"apps": 1, "mins": 90, "goals": 0, "assists": 0},
            })
            old_match = {
                "id": 1, "season": 2026, "played": True, "date": "2026-08-01", "comp": "Premier League",
                "home": premier, "away": fc1, "xi_h": [p1, p2], "xi_a": [p3, p4],
                "goals": [{"side": "home", "team": premier, "scorer_id": p1, "assist_id": p2}],
            }
            enhanced_match = {
                "id": 1, "season": 2027, "played": True, "date": "2027-08-01", "comp": "Cup",
                "home": premier, "away": fc2, "xi_h": [p1, p2], "xi_a": [p3, p4],
                "subs_h": [], "subs_a": [],
                "player_stats": {
                    "h": {p1: {"min": 90, "g": 1, "a": 0}, p2: {"min": 90, "g": 0, "a": 1}},
                    "a": {p3: {"min": 90, "g": 0, "a": 0}, p4: {"min": 90, "g": 0, "a": 0}},
                },
                "goals": [{"side": "home", "team": premier, "scorer_id": p1, "assist_id": p2}],
            }
            game.s["history"] = [{
                "season": 2026, "label": "2026/2027", "champion_premier": premier,
                "champion_cup": cup, "champion_c1": c1,
                "leagues": {"premier": [{"team": premier}], "fc1": [{"team": fc1}], "fc2": [{"team": fc2}]},
                "matches": [old_match],
            }]
            game.s["matches"] = [enhanced_match]

            trophies = {row["team"]: row for row in game.career_trophy_table()}
            self.assertEqual(trophies[premier]["premier"], 1)
            self.assertEqual(trophies[fc1]["fc1"], 1)
            self.assertEqual(trophies[fc2]["fc2"], 1)
            self.assertEqual(trophies[cup]["cup"], 1)
            self.assertEqual(trophies[c1]["c1"], 1)
            self.assertEqual(trophies[premier]["total"], 1)

            legends = game.career_player_legends(limit=11)
            appearance_map = {row["player_id"]: row for row in legends["appearances"]}
            goals_map = {row["player_id"]: row for row in legends["goals"]}
            assists_map = {row["player_id"]: row for row in legends["assists"]}
            self.assertEqual(appearance_map[p1]["apps"], 2)
            self.assertEqual(goals_map[p1]["goals"], 2)
            self.assertEqual(assists_map[p2]["assists"], 2)
            club_legends = game.career_player_legends(team=premier, limit=11)
            self.assertEqual({row["player_id"] for row in club_legends["appearances"]}, {p1, p2})
            self.assertEqual(club_legends["goals"][0]["team"], premier)

    def test_legacy_match_engine_can_still_be_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            cfg["match_engine"] = "legacy"
            config_path = Path(tmp) / "legacy.json"
            config_path.write_text(json.dumps(cfg), encoding="utf-8")
            game = Game(data_dir=Path(tmp) / "data", config_path=config_path, fresh=True, seed=77)
            game.advance_slot()
            self.assertEqual(len(game.s["matches"]), 4)
            self.assertTrue(all(m.get("match_engine") != "enhanced" for m in game.s["matches"]))

    def test_missing_database_rebuilds_from_packaged_static_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            Game(data_dir=data_dir, fresh=True, seed=123)
            (data_dir / "super_league.db").unlink()
            recovered = Game(data_dir=data_dir, seed=123)
            self.assertEqual(recovered.s["season"], 2026)
            self.assertEqual(len(recovered.s["players"]), 552)
            self.assertEqual(sorted(path.name for path in data_dir.iterdir()), ["super_league.db"])

    def test_fresh_mode_removes_old_season_snapshots_and_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            Game(data_dir=data_dir, fresh=True, seed=123)
            (data_dir / "season").mkdir(exist_ok=True)
            (data_dir / "season" / "2020-2021.json").write_text('{"old":true}', encoding="utf-8")
            (data_dir / "snapshots").mkdir(exist_ok=True)
            (data_dir / "snapshots" / "players_2020.csv").write_text("player_id\nold-player\n", encoding="utf-8")
            for name in ("generated_players.csv", "player_meta.csv", "retired_players.csv"):
                (data_dir / name).write_text("old-data", encoding="utf-8")
            (data_dir / "save.broken.json").write_text("old-corrupt-save", encoding="utf-8")
            for dirname in ("backups", "recovery", "google_sheets_captures"):
                folder = data_dir / dirname
                folder.mkdir(exist_ok=True)
                (folder / "old-file.dat").write_text("old-runtime-data", encoding="utf-8")
            (data_dir / "backups.zip").write_bytes(b"old-backups")

            fresh = Game(data_dir=data_dir, seed=123, fresh=True)
            self.assertEqual(fresh.s["history"], [])
            self.assertEqual(sorted(path.name for path in data_dir.iterdir()), ["super_league.db"])
            db_status = fresh.database.status()
            self.assertEqual(db_status["tables"]["source_players"], 552)
            self.assertEqual(db_status["tables"]["reference_data"], 1)
            self.assertEqual(db_status["tables"]["season_archives"], 0)
            self.assertEqual(db_status["integrity"], "ok")
            self.assertEqual(sorted(path.name for path in data_dir.iterdir()), ["super_league.db"])
            with fresh.database._session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM player_snapshots").fetchone()[0], 0)
            self.assertFalse((data_dir / "backups").exists())

    def test_engine_calibration_meets_m0_targets(self):
        result = run_engine_calibration(matches=2000, seed=20261029, quiet=True)
        self.assertTrue(result["passed"], result)
        self.assertGreaterEqual(result["goals_per_match"], 2.3)
        self.assertLessEqual(result["goals_per_match"], 2.8)
        self.assertGreaterEqual(result["strong_win_rate"], 0.60)
        self.assertLessEqual(result["strong_win_rate"], 0.75)


class AwardSettingsIntegrationTests(unittest.TestCase):
    def test_award_recalculator_save_settings_is_readable_by_main_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sl_game.py").write_text("# project marker\n", encoding="utf-8")
            custom = dict(AWARD_SETTINGS_DEFAULTS)
            custom.update({
                "min_appearances": 26,
                "require_premier_champion": False,
                "require_c1_semifinal": True,
                "c1_champion_bonus": 8.5,
                "weight_rating": 50.0,
                "weight_contribution": 20.0,
                "weight_motm": 20.0,
                "weight_minutes": 10.0,
            })
            path, backup = award_tool.save_shared_settings(root, custom)
            self.assertEqual(path, root / "award_settings.json")
            self.assertIsNone(backup)
            loaded = load_award_settings_file(path)
            for key, value in custom.items():
                self.assertEqual(loaded[key], value, key)

            # The main game loader and the auxiliary tool read the exact same JSON schema.
            tool_settings, exists = award_tool.load_shared_settings(root)
            self.assertTrue(exists)
            self.assertEqual(tool_settings, loaded)

            updated = dict(custom)
            updated["c1_champion_bonus"] = 9.0
            path2, backup2 = award_tool.save_shared_settings(root, updated)
            self.assertEqual(path2, path)
            self.assertIsNone(backup2)
            self.assertEqual(list(root.glob("award_settings.before_*.json")), [])
            self.assertEqual(load_award_settings_file(path)["c1_champion_bonus"], 9.0)

    def test_main_game_uses_saved_award_weights_and_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261101)
            game.s["players"].update({
                "prem-21": {"common_name": "Premier 21", "position": "ST", "overall_rating": 80},
                "prem-26": {"common_name": "Premier 26", "position": "CM", "overall_rating": 80},
            })
            game.s["rosters"].setdefault("Champion Club", []).extend(["prem-21", "prem-26"])
            game.award_settings = dict(AWARD_SETTINGS_DEFAULTS)
            game.award_settings.update({
                "min_appearances": 26,
                "require_premier_champion": True,
                "require_c1_semifinal": False,
                "weight_rating": 100.0,
                "weight_contribution": 0.0,
                "weight_motm": 0.0,
                "weight_minutes": 0.0,
            })
            match = {
                "id": 1, "played": True, "comp": "Premier League",
                "stage": "league_p2_premier_champion_1", "round": "Champion · Vòng 1",
                "home": "Champion Club", "away": "Opponent", "hg": 1, "ag": 0,
                "minutes_h": {"prem-21": 90, "prem-26": 90}, "minutes_a": {},
                "xi_h": ["prem-21", "prem-26"], "xi_a": [],
                "player_stats": {"h": {"prem-21": {"min": 90, "rt": 8.0},
                                         "prem-26": {"min": 90, "rt": 7.0}}, "a": {}},
                "goals": [], "motm": None,
            }
            stats = {
                "prem-21": {"apps": 21, "mins": 1890, "goals": 30, "assists": 15,
                            "rating_sum": 168, "rating_count": 21},
                "prem-26": {"apps": 26, "mins": 2340, "goals": 1, "assists": 0,
                            "rating_sum": 182, "rating_count": 26},
            }
            candidates = {r["player_id"]: r for r in game._season_award_candidates(
                stats=stats, matches=[match])}
            self.assertFalse(candidates["prem-21"]["award_eligible"])
            self.assertTrue(candidates["prem-26"]["award_eligible"])
            # With rating-only weights, the 8.0-rated player ranks above the high scorer.
            self.assertGreater(candidates["prem-21"]["vote_score"], candidates["prem-26"]["vote_score"])

    def test_main_game_startup_reads_award_settings_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            custom = dict(AWARD_SETTINGS_DEFAULTS)
            custom.update({"min_appearances": 29, "c1_champion_bonus": 9.0,
                           "weight_rating": 50.0, "weight_contribution": 20.0,
                           "weight_motm": 20.0, "weight_minutes": 10.0})
            (project_root / "award_settings.json").write_text(
                json.dumps({"schema_version": 1, "settings": custom}), encoding="utf-8")
            previous_app_dir = sl_game_module.APP_DIR
            sl_game_module.APP_DIR = project_root
            try:
                game = Game(config_path=ROOT / "config.json",
                            data_dir=project_root / "memory", fresh=True,
                            enable_google_sheets=False)
                self.assertEqual(game.award_settings["min_appearances"], 29)
                self.assertEqual(game.award_settings["c1_champion_bonus"], 9.0)
                self.assertEqual(game.award_settings["weight_rating"], 50.0)
            finally:
                sl_game_module.APP_DIR = previous_app_dir

    def test_main_backfills_legacy_season_using_saved_award_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261103)
            game.s["players"].update({
                "p21": {"common_name": "Twenty One Apps", "position": "ST", "overall_rating": 80},
                "p22": {"common_name": "Twenty Two Apps", "position": "CM", "overall_rating": 80},
            })
            game.s["history"] = [{
                "season": 2026, "label": "2026/2027",
                "matches": [{
                    "id": 1, "played": True, "comp": "Premier League",
                    "stage": "league_p2_premier_champion_1", "round": "Champion · Vòng 1",
                    "home": "Champion Club", "away": "Opponent", "hg": 1, "ag": 0,
                    "minutes_h": {"p21": 90, "p22": 90}, "minutes_a": {},
                    "xi_h": ["p21", "p22"], "xi_a": [],
                    "player_stats": {"h": {"p21": {"min": 90, "rt": 9.5},
                                             "p22": {"min": 90, "rt": 7.5}}, "a": {}},
                    "goals": [], "motm": None,
                }],
                "career_player_stats": [
                    {"player_id": "p21", "player": "Twenty One Apps", "team": "Champion Club",
                     "position": "ST", "apps": 21, "minutes": 1890, "goals": 20, "assists": 5, "avg_rating": 9.5},
                    {"player_id": "p22", "player": "Twenty Two Apps", "team": "Champion Club",
                     "position": "CM", "apps": 22, "minutes": 1980, "goals": 5, "assists": 3, "avg_rating": 7.5},
                ],
                "competition_awards": {"premier": {"champion": "Champion Club"}},
                "season_awards": {"best_xi_version": 7, "top20_version": 1,
                                  "award_settings_signature": "old-settings",
                                  "ballon_dor": {"player": "Stale Winner"},
                                  "young_player": {"player": "Preserved Golden Boy"}},
            }]
            game.award_settings = dict(AWARD_SETTINGS_DEFAULTS)
            game.award_settings.update({
                "min_appearances": 22, "require_premier_champion": True,
                "require_c1_semifinal": False, "weight_rating": 100.0,
                "weight_contribution": 0.0, "weight_motm": 0.0, "weight_minutes": 0.0,
            })
            game._backfill_history_awards()
            annual = game.s["history"][0]["season_awards"]
            self.assertEqual(annual["ballon_dor"]["player_id"], "p22")
            self.assertEqual(annual["eligible_players"], 1)
            self.assertEqual(annual["young_player"]["player"], "Preserved Golden Boy")
            signature = annual["award_settings_signature"]
            game._backfill_history_awards()
            self.assertEqual(game.s["history"][0]["season_awards"]["award_settings_signature"], signature)

    def test_main_best_xi_uses_saved_club_bonus_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102)
            settings = dict(AWARD_SETTINGS_DEFAULTS)
            settings.update({"c1_champion_bonus": 11.0, "premier_champion_bonus": 2.5})
            clubs = {"c1": "C1 Winner", "premier": "Premier Winner"}
            self.assertEqual(game._best_xi_honor_bonus("C1 Winner", clubs, settings)[0], 11.0)
            self.assertEqual(game._best_xi_honor_bonus("Premier Winner", clubs, settings)[0], 2.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
