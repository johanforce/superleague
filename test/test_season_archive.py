# -*- coding: utf-8 -*-
"""Tests for season/ folder archives and compact historical match records."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from season_archive import (
    ARCHIVE_FORMAT,
    ARCHIVE_VERSION,
    SeasonArchiveError,
    compact_match,
    compact_season_record,
    export_season_folder,
    load_season_archives,
    preview_season_folder,
    read_season_archive,
    season_folder_summary,
    write_season_archive,
)


def sample_match(season=2030, match_id=1):
    return {
        "id": match_id, "season": season, "comp": "Major Cup", "round": "Chung kết",
        "stage": "cup_final", "date": "2031-05-20", "home": "Team A", "away": "Team B",
        "played": True, "hg": 2, "ag": 2, "winner": "Team A",
        "goals": [
            {"minute": 12, "team": "Team A", "side": "home", "scorer_id": "p1",
             "scorer": "Player One", "assist": "Player Two", "period": "90m", "xg": .4},
            {"minute": 108, "team": "Team B", "side": "away", "scorer_id": "p2",
             "scorer": "Player Two", "period": "et1", "pen": True, "var": "checked"},
        ],
        "cards_h": [{"m": 10, "p": "p1", "c": "y"}, {"m": 88, "p": "p3", "c": "r"}],
        "cards_a": [{"m": 119, "p": "p4", "c": "yr"}],
        "et": True, "pens": {"home": 5, "away": 4, "kicks": [{"player": "ignored"}]},
        "player_stats": {"h": {"p1": {"min": 90}}, "a": {}},
        "live_bundle": {"replay": ["very large", "not kept"]},
        "advanced_stats": {"shots": 99}, "xi_h": ["p1"], "xi_a": ["p2"],
        "subs_h": [{"player": "p9"}], "var": [{"minute": 80, "decision": "ignored"}],
        "motm": [9.0, "p1"], "inj_h": ["p5"], "cards_detail": ["ignored"],
    }


def sample_record(season: int):
    return {
        "season": season, "label": f"{season}/{season+1}",
        "champion_premier": "Team A", "champion_cup": "Team B", "champion_c1": "Team C",
        "runner_up_cup": "Team D", "runner_up_c1": "Team E",
        "leagues": {"premier": [{"team": "Team A", "pts": 55}],
                    "fc1": [{"team": "Team B"}], "fc2": [{"team": "Team C"}]},
        "league_phase2": {"premier": {"champion_teams": ["Team A", "Team B"]}},
        "promoted": [], "relegated": [], "cup_bracket": {}, "c1": {"bracket": {}},
        "qualification_table": [], "top_scorers": [], "top_assists": [],
        "career_player_stats": [{"player_id": "p1", "player": "Player One", "team": "Team A",
                                  "position": "ST", "apps": 21, "minutes": 1800, "goals": 10,
                                  "assists": 3, "avg_rating": 7.8}],
        "competition_awards": {}, "season_awards": {"ballon_dor": {"player": "Player One"}},
        "development": [], "retired": [], "youth_promoted": [],
        "matches": [sample_match(season, 1)], "match_count": 1,
        "award_eligibility": {"premier_champion_players": ["p1"], "c1_semifinal_players": []},
    }


class CompactMatchTests(unittest.TestCase):
    def test_keeps_only_match_identity_score_scorers_red_aet_and_shootout_score(self):
        compact = compact_match(sample_match(), {"p3": "Player Three", "p4": "Player Four"})
        self.assertEqual(set(compact), {
            "id", "season", "comp", "round", "stage", "date", "home", "away", "neutral", "played",
            "hg", "ag", "winner", "goals", "red_cards", "aet", "pens",
        })
        self.assertEqual((compact["hg"], compact["ag"]), (2, 2))
        self.assertTrue(compact["aet"])
        self.assertEqual(compact["pens"], {"home": 5, "away": 4})
        self.assertEqual(len(compact["goals"]), 2)
        self.assertEqual(compact["goals"][0]["scorer"], "Player One")
        self.assertEqual(compact["goals"][1]["minute"], 108)
        self.assertEqual(len(compact["red_cards"]), 2)
        self.assertEqual(compact["red_cards"][0]["player"], "Player Three")
        self.assertEqual(compact["red_cards"][1]["type"], "second_yellow")
        self.assertNotIn("player_stats", compact)
        self.assertNotIn("live_bundle", compact)
        self.assertEqual(compact["winner"], "Team A")
        self.assertNotIn("cards_h", compact)

    def test_empty_legacy_red_cards_field_does_not_hide_card_events(self):
        compact = compact_match({
            "id": 12, "comp": "C1", "home": "Alpha", "away": "Beta", "hg": 1, "ag": 0,
            "red_cards": [],
            "cards_h": [{"m": 81, "p": "CB1", "c": "yr"}],
        }, {"CB1": "Sent Off"})
        self.assertEqual(len(compact["red_cards"]), 1)
        self.assertEqual(compact["red_cards"][0]["player"], "Sent Off")
        self.assertEqual(compact["red_cards"][0]["type"], "second_yellow")

    def test_preserves_only_aet_and_penalty_score_when_present(self):
        match = sample_match()
        match["et"] = False
        match["pens"] = None
        compact = compact_match(match)
        self.assertNotIn("aet", compact)
        self.assertNotIn("pens", compact)

    def test_compacting_an_already_compact_match_is_idempotent(self):
        once = compact_match(sample_match())
        twice = compact_match(once)
        self.assertEqual(once, twice)


class SeasonFolderTests(unittest.TestCase):
    def test_write_read_uses_version_two_compact_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_season_archive(Path(tmp), sample_record(2030))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["format"], ARCHIVE_FORMAT)
            self.assertEqual(payload["format_version"], ARCHIVE_VERSION)
            record = read_season_archive(path)
            self.assertEqual(len(record["matches"]), 1)
            self.assertNotIn("live_bundle", record["matches"][0])

    def test_loads_completed_seasons_from_sqlite_archives(self):
        from sl_game import Game
        from sl_database import SuperLeagueDatabase
        with tempfile.TemporaryDirectory() as tmp:
            database = SuperLeagueDatabase(Path(tmp) / "super_league.db")
            records = [sample_record(season) for season in range(2030, 2036)]
            database.replace_season_archives(records)
            game = object.__new__(Game)
            game.database = database
            game._history_migration_needed = False
            game.s = {"season": 2040, "history": [sample_record(2020)]}
            game._load_season_history()
            self.assertEqual([r["season"] for r in game.s["history"]], list(range(2030, 2036)))
            self.assertFalse(game._history_migration_needed)

    def test_clean_start_does_not_restore_or_create_legacy_backups(self):
        """Clean-start project has no career history and reset creates no backups."""
        import tempfile
        from sl_game import Game
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "memory"
            game = Game(
                data_dir=data_dir,
                config_path=project / "config.json",
                fresh=True,
                enable_google_sheets=False,
            )
            self.assertEqual(game.s.get("history", []), [])
            self.assertEqual(len(game.s["rosters"]), 24)
            self.assertEqual(len(game.s["players"]), 552)
            self.assertFalse((data_dir / "backups").exists())
            game.reset_simulation()
            self.assertEqual(game.s.get("history", []), [])
            self.assertFalse((data_dir / "backups").exists())

    def test_legacy_embedded_history_migrates_once_into_sqlite(self):
        from sl_game import Game
        from sl_database import SuperLeagueDatabase
        with tempfile.TemporaryDirectory() as tmp:
            database = SuperLeagueDatabase(Path(tmp) / "super_league.db")
            game = object.__new__(Game)
            game.database = database
            game._history_migration_needed = False
            game.s = {"season": 2040, "history": [sample_record(2030), sample_record(2031)]}
            game._load_season_history()
            self.assertEqual([r["season"] for r in game.s["history"]], [2030, 2031])
            self.assertEqual(len(database.load_season_archives()), 2)
            self.assertEqual(game.s["history_archive_mode"], "database")
            self.assertTrue(game._history_migration_needed)

    def test_export_mirrors_season_files_and_removes_only_stale_season_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "source", root / "output"
            for season in range(2030, 2036):
                write_season_archive(source, sample_record(season))
            target.mkdir()
            (target / "2020-2021.json").write_text("{}", encoding="utf-8")
            (target / "notes.txt").write_text("keep", encoding="utf-8")
            summary = export_season_folder(source, target)
            self.assertEqual(summary["season_count"], 6)
            self.assertFalse((target / "2020-2021.json").exists())
            self.assertTrue((target / "notes.txt").exists())
            self.assertEqual(len(list(target.glob("20??-20??.json"))), 6)

    def test_external_season_files_can_be_loaded_into_single_database(self):
        from sl_database import SuperLeagueDatabase
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "input"
            memory = root / "memory"
            memory.mkdir()
            database = SuperLeagueDatabase(memory / "super_league.db")
            for season in range(2030, 2036):
                write_season_archive(incoming, sample_record(season))
            preview = preview_season_folder(incoming)
            records, errors = load_season_archives(incoming, strict=True)
            self.assertFalse(errors)
            count = database.replace_season_archives(records)
            self.assertEqual(preview["season_count"], 6)
            self.assertEqual(count, 6)
            self.assertEqual([row["season"] for row in database.load_season_archives()], list(range(2030, 2036)))
            self.assertEqual([path.name for path in memory.iterdir()], ["super_league.db"])

    def test_bad_checksum_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_season_archive(Path(tmp), sample_record(2030))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["record"]["champion_premier"] = "tampered"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SeasonArchiveError):
                read_season_archive(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
