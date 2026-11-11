import unittest

from season_archive import compact_match
from sl_match.adapter import format_report_result, report_for_match


class CompetitionMatchReportTests(unittest.TestCase):
    def _players(self):
        return {
            "P_HOME": {"common_name": "Home Scorer", "position": "ST"},
            "P_AWAY": {"common_name": "Away Scorer", "position": "ST"},
            "P_RED": {"common_name": "Sent Off", "position": "CB"},
        }

    def test_c1_compact_archive_keeps_goal_side_red_card_aet_and_penalties(self):
        raw = {
            "id": 900, "season": 2026, "comp": "C1", "round": "Chung kết",
            "home": "Alpha FC", "away": "Beta FC", "hg": 2, "ag": 2,
            "winner": "Alpha FC", "et": True,
            "pens": {"home": 5, "away": 4},
            "goals": [
                {"minute": 43, "team": "Alpha FC", "scorer_id": "P_HOME", "scorer": "Home Scorer"},
                {"minute": 108, "team": "Beta FC", "scorer_id": "P_AWAY", "scorer": "Away Scorer"},
            ],
            "red_cards": [{"minute": 116, "team": "Beta FC", "side": "a",
                           "player_id": "P_RED", "player": "Sent Off", "type": "second_yellow"}],
            # These detailed values must not leak into the compact C1 report.
            "advanced_stats": {"poss": [61, 39]},
            "cards_h": [{"m": 20, "p": "P_HOME", "c": "y"}],
        }
        archived = compact_match(raw, {"P_HOME": "Home Scorer", "P_AWAY": "Away Scorer"})
        report = report_for_match(archived, self._players())

        self.assertTrue(report["summary_only"])
        self.assertEqual([goal[1] for goal in report["goals"]], ["h", "a"])
        self.assertEqual([goal[2] for goal in report["goals"]], ["Home Scorer", "Away Scorer"])
        self.assertEqual(len(report["events"]), 1)
        self.assertEqual(report["events"][0][1:4], ["a", "yr", "Sent Off"])
        self.assertIsNone(report["stats"])
        self.assertEqual(report["players"], {"h": [], "a": []})
        self.assertEqual([event[1] for event in report["tl"]], ["goal", "goal", "red"])
        suffix, note = format_report_result(archived)
        self.assertEqual(suffix, " AET")
        self.assertIn("Luân lưu 5–4", note)
        self.assertIn("Alpha FC", note)

    def test_major_cup_compact_archive_handles_home_goal_and_no_red_cards(self):
        raw = {
            "id": 901, "season": 2026, "comp": "Cup", "round": "Chung kết",
            "home": "North", "away": "South", "hg": 1, "ag": 0,
            "winner": "North", "goals": [
                {"minute": 89, "team": "North", "scorer": "Winner"},
            ],
            "red_cards": [],
        }
        archived = compact_match(raw)
        report = report_for_match(archived, {})

        self.assertTrue(report["summary_only"])
        self.assertEqual(report["goals"][0][1:3], ["h", "Winner"])
        self.assertEqual(report["events"], [])
        self.assertEqual([event[1] for event in report["tl"]], ["goal"])
        self.assertEqual(format_report_result(archived), ("", ""))

    def test_summary_mode_filters_yellows_substitutions_and_injuries(self):
        raw = {
            "comp": "Champions League", "home": "A", "away": "B", "hg": 0, "ag": 0,
            "goals": [], "cards_h": [
                {"m": 12, "p": "yellow_player", "c": "y"},
                {"m": 77, "p": "P_RED", "c": "r"},
            ],
            "inj_h": [{"m": 33, "p": "P_RED", "name": "Injury", "n": 2}],
            "subs_h": [[70, "P_RED", "P_AWAY"]],
            "var": [{"m": 80, "t": "h", "k": "no_change"}],
        }
        report = report_for_match(raw, self._players())
        self.assertEqual(len(report["events"]), 1)
        self.assertEqual(report["events"][0][2], "r")
        self.assertEqual([event[1] for event in report["tl"]], ["red"])

    def test_penalty_result_accepts_legacy_list_shape(self):
        suffix, note = format_report_result({
            "home": "A", "away": "B", "et": True, "pens": [4, 3]
        })
        self.assertEqual(suffix, " AET")
        self.assertIn("Luân lưu 4–3", note)
        self.assertIn("A thắng", note)


if __name__ == "__main__":
    unittest.main()


class PastMatchNavigationTests(unittest.TestCase):
    def test_saved_match_replay_entrypoint_is_report_only(self):
        from unittest.mock import Mock
        from sl_gui import SuperLeagueApp

        app = object.__new__(SuperLeagueApp)
        app._show_match_report = Mock()
        match = {"id": 12, "home": "Alpha", "away": "Beta"}
        app._open_saved_match_replay(match)
        app._show_match_report.assert_called_once_with(match)

    def test_fixture_double_click_routes_to_report_not_replay(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from sl_gui import SuperLeagueApp

        match = {"date": "2027-08-01", "comp": "Premier League", "round": "Vòng 1",
                 "home": "Alpha", "away": "Beta", "played": True}

        class FakeTree:
            def selection(self):
                return ["row-1"]

        app = object.__new__(SuperLeagueApp)
        app.fixture_tree = FakeTree()
        app.fixture_rows_by_iid = {"row-1": {**match, "_played": True}}
        app.game = SimpleNamespace(s={"matches": [match]})
        app._show_match_report = Mock()
        app._open_saved_match_replay = Mock(side_effect=AssertionError("replay must not open"))
        app.on_match_detail()
        app._show_match_report.assert_called_once_with(match)
        app._open_saved_match_replay.assert_not_called()

    def test_cup_match_click_routes_to_report_not_replay(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from sl_gui import SuperLeagueApp

        match = {"id": 99, "home": "Alpha", "away": "Beta", "played": True}

        class FakeTree:
            def identify_row(self, _y):
                return "cup-match"

        app = object.__new__(SuperLeagueApp)
        app.cup_tree = FakeTree()
        app.cup_match_rows = {"cup-match": match}
        app._show_match_report = Mock()
        app._open_saved_match_replay = Mock(side_effect=AssertionError("replay must not open"))
        app.on_cup_match_click(SimpleNamespace(y=4))
        app._show_match_report.assert_called_once_with(match)
        app._open_saved_match_replay.assert_not_called()
