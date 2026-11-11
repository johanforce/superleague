# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path

from sl_match.human_data import load_human_data, assign_manager_names
from sl_match.context import make_refs
from sl_match.coach import make_coach
from sl_game import Game


PROJECT = Path(__file__).resolve().parents[1]


class HumanNamesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_human_data()

    def test_database_lists_are_loaded_without_losing_frequency_weights(self):
        self.assertEqual(len(self.data["referees"]), 38)
        self.assertEqual(len(self.data["managers"]), 25)
        self.assertEqual(len(self.data["first_names"]), 1000)
        self.assertEqual(len(self.data["second_names"]), 1000)
        self.assertEqual(len(set(self.data["first_names"])), 244)
        self.assertEqual(len(set(self.data["second_names"])), 250)

    def test_referees_use_exact_database_names(self):
        refs = make_refs(seed=1234)
        self.assertEqual(len(refs), 38)
        self.assertEqual([r["name"] for r in refs], list(self.data["referees"]))
        self.assertEqual(refs, make_refs(seed=1234))

    def test_manager_assignment_is_unique_for_24_clubs(self):
        teams = [f"Club {i:02d}" for i in range(24)]
        assigned = assign_manager_names(teams, seed=2026)
        self.assertEqual(len(assigned), 24)
        self.assertEqual(len(set(assigned.values())), 24)
        self.assertTrue(set(assigned.values()).issubset(set(self.data["managers"])))

    def test_make_coach_uses_manager_source(self):
        coach = make_coach("Example FC")
        self.assertIn(coach["name"], self.data["managers"])
        self.assertEqual(coach, make_coach("Example FC"))

    def test_game_youth_names_use_database_first_and_second_names(self):
        with tempfile.TemporaryDirectory() as temp:
            game = Game(
                data_dir=Path(temp),
                config_path=PROJECT / "config.json",
                fresh=True,
                seed=2026,
                enable_google_sheets=False,
            )
            first, second, full = game._new_name()
            self.assertIn(first, self.data["first_names"])
            self.assertIn(second, self.data["second_names"])
            self.assertEqual(full, f"{first} {second}")
            self.assertIn(full, game.s["used_names"])

    def test_legacy_save_names_are_migrated_once(self):
        with tempfile.TemporaryDirectory() as temp:
            game = Game(
                data_dir=Path(temp),
                config_path=PROJECT / "config.json",
                fresh=True,
                seed=2026,
                enable_google_sheets=False,
            )
            for index, coach in enumerate(game.s["coaches"].values()):
                coach["name"] = f"Legacy Coach {index}"
            game.s["human_names_version"] = 0
            game.s["match_refs"] = game.s["match_refs"][:24]
            for ref in game.s["match_refs"]:
                ref["name"] = f"Legacy Referee {ref['id']}"

            game._ensure_match_state()

            coach_names = {c["name"] for c in game.s["coaches"].values()}
            referee_names = {r["name"] for r in game.s["match_refs"]}
            self.assertEqual(game.s["human_names_version"], 1)
            self.assertTrue(coach_names.issubset(set(self.data["managers"])))
            self.assertEqual(len(coach_names), 24)
            self.assertTrue(referee_names.issubset(set(self.data["referees"])))
            self.assertEqual(len(referee_names), 38)


if __name__ == "__main__":
    unittest.main()
