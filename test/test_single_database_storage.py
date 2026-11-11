"""End-to-end checks for the one-file local data contract."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sl_game import Game

PROJECT = Path(__file__).resolve().parents[1]


class SingleDatabaseStorageTests(unittest.TestCase):
    def assert_single_database(self, memory: Path):
        self.assertEqual(sorted(path.name for path in memory.iterdir()), ["super_league.db"])

    def test_normal_startup_removes_legacy_sidecars_without_resetting_career(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = Path(temp) / "memory"
            game = Game(data_dir=memory, config_path=PROJECT / "config.json", fresh=True,
                        seed=20261103, enable_google_sheets=False)
            game.advance_slot(persist=True)
            saved_season = game.s["season"]
            (memory / "save.json").write_text('{"legacy":true}', encoding="utf-8")
            (memory / "generated_players.csv").write_text("legacy", encoding="utf-8")
            (memory / "season").mkdir()
            (memory / "season" / "2020-2021.json").write_text("{}", encoding="utf-8")

            resumed = Game(data_dir=memory, config_path=PROJECT / "config.json",
                           enable_google_sheets=False)
            self.assert_single_database(memory)
            self.assertEqual(resumed.s["season"], saved_season)
            self.assertEqual(resumed.slot_progress()[0], game.slot_progress()[0])
            self.assertTrue(resumed.database.has_career())
            self.assertEqual(resumed.database.status()["integrity"], "ok")
            game.close_google_sheets_sync(timeout=1.0)
            resumed.close_google_sheets_sync(timeout=1.0)

    def test_full_lifecycle_keeps_only_one_database_file(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = Path(temp) / "memory"
            game = Game(
                data_dir=memory,
                config_path=PROJECT / "config.json",
                fresh=True,
                seed=20261103,
                enable_google_sheets=False,
            )
            self.assert_single_database(memory)
            status = game.database.status()
            self.assertEqual(status["integrity"], "ok")
            self.assertEqual(status["tables"]["source_players"], 552)
            self.assertEqual(status["tables"]["reference_data"], 1)
            self.assertEqual(len(game.s["rosters"]), 24)
            self.assertEqual(len(game.s["players"]), 552)

            game.advance_slot(persist=True)
            self.assert_single_database(memory)

            game.simulate_full_season(persist_each_slot=False)
            self.assert_single_database(memory)
            status = game.database.status()
            self.assertEqual(status["integrity"], "ok")
            self.assertEqual(status["tables"]["season_archives"], 1)

            # Simulate residue from an older install; explicit reset must remove it.
            (memory / "save.json").write_text('{"old":true}', encoding="utf-8")
            (memory / "generated_players.csv").write_text("old", encoding="utf-8")
            (memory / "season").mkdir()
            (memory / "season" / "2020-2021.json").write_text("{}", encoding="utf-8")
            game.reset_simulation()
            self.assert_single_database(memory)
            status = game.database.status()
            self.assertEqual(status["integrity"], "ok")
            self.assertEqual(status["tables"]["source_players"], 552)
            self.assertEqual(status["tables"]["reference_data"], 1)
            self.assertEqual(status["tables"]["season_archives"], 0)
            self.assertEqual(game.s["history"], [])
            self.assertEqual(len(game.s["rosters"]), 24)
            self.assertEqual(len(game.s["players"]), 552)
            game.close_google_sheets_sync(timeout=1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
