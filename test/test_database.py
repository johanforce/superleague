import tempfile
import unittest
from pathlib import Path

from sl_database import SuperLeagueDatabase


class SuperLeagueDatabaseTests(unittest.TestCase):
    def test_career_round_trip_keeps_canonical_data_without_duplicate_projections(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = SuperLeagueDatabase(Path(tmp) / "super_league.db")
            state = {
                "season": 2032,
                "players": {"p1": {"player_id": "p1", "overall_rating": 88}},
                "player_meta": {"p1": {"age": 24, "potential": 94, "peak_ovr": 88}},
                "rosters": {"Apollo FC": ["p1"]},
                "contracts": {"p1": {"club": "Apollo FC", "expiry_season": 2034}},
                "club_finances": {"Apollo FC": {"cash": 250.0, "debt": 5.0}},
                "matches": [{"id": 7, "status": "played", "competition": "Premier League", "hg": 2, "ag": 1}],
                "finance_ledger": [{"id": "tx-1", "season": 2032, "club": "Apollo FC", "type": "WAGE", "amount": -1.2}],
                "transfer_history": [{"player_id": "p1", "from_club": "Ares FC", "to_club": "Apollo FC"}],
                "history": [],
            }
            archive = {"season": 2031, "team_awards": [{"club": "Apollo FC", "competition": "Premier League"}]}
            blob = {"version": 1, "state": state, "rng_state": [1, [2, 3], None]}
            db.save_career(blob, source_players=[{"player_id": "p1", "club": "Apollo FC"}], season_archives=[archive])
            self.assertTrue(db.has_career())
            self.assertEqual(db.load_career()["state"], state)
            self.assertEqual(db.load_season_archives(), [archive])
            import sqlite3
            conn = sqlite3.connect(db.path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM players").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM player_metadata").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0], 1)
                # Full match and finance detail lives in career_state / archives.
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM transfer_history").fetchone()[0], 1)
                # Awards are read from the canonical season archive, not a second copy.
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_players").fetchone()[0], 1)
            finally:
                conn.close()

    def test_projection_compaction_removes_only_exact_duplicates_and_keeps_unmatched_legacy_rows(self):
        import json
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "super_league.db"
            db = SuperLeagueDatabase(path)
            current_match = {"id": 7, "date": "2032-01-01", "home": "A", "away": "B", "hg": 2, "ag": 1}
            archived_match = {"id": 3, "date": "2031-01-01", "home": "C", "away": "D", "hg": 1, "ag": 0}
            current_career = {"season": 2031, "club": "Apollo FC", "goals": 2, "apps": 10}
            archived_career = {"player_id": "p2", "team": "Boreal FC", "goals": 4, "apps": 12}
            current_development = {"player_id": "p1", "new_ovr": 88, "delta": 2}
            archived_development = {"player_id": "p2", "new_ovr": 90, "delta": 1}
            legacy_career = {"season": 2028, "club": "Old Club", "goals": 1, "apps": 5}
            legacy_development = {"player_id": "p3", "new_ovr": 77, "delta": 1}
            transaction = {"id": "tx-1", "season": 2032, "club": "Apollo FC", "type": "WAGE", "amount": -1.2}
            award = {"player": "Player One", "team": "Apollo FC"}
            archive = {
                "season": 2031,
                "matches": [archived_match],
                "career_player_stats": [archived_career],
                "development": [archived_development],
                "season_awards": {"golden_boy": award},
            }
            state = {
                "season": 2032,
                "matches": [current_match],
                "finance_ledger": [transaction],
                "player_career": {"p1": [current_career]},
                "last_development": [current_development],
            }
            with sqlite3.connect(path) as conn:
                conn.execute("INSERT INTO matches VALUES(?,?,?,?,?)",
                             ("2032:7", 2032, "played", "League", json.dumps(current_match)))
                conn.execute("INSERT INTO matches VALUES(?,?,?,?,?)",
                             ("2031:3", 2031, "played", "League", json.dumps(archived_match)))
                conn.execute("INSERT INTO finance_transactions VALUES(?,?,?,?,?,?,?)",
                             ("tx-1", 2032, "Apollo FC", "", "WAGE", -1.2, json.dumps(transaction)))
                conn.execute("INSERT INTO player_career VALUES(?,?,?,?)",
                             ("p1", 2031, "Apollo FC", json.dumps(current_career)))
                conn.execute("INSERT INTO player_career VALUES(?,?,?,?)",
                             ("p2", 2031, "Boreal FC", json.dumps(archived_career)))
                conn.execute("INSERT INTO player_career VALUES(?,?,?,?)",
                             ("p3", 2028, "Old Club", json.dumps(legacy_career)))
                conn.execute("INSERT INTO player_development VALUES(?,?,?)",
                             ("p1", 2032, json.dumps(current_development)))
                conn.execute("INSERT INTO player_development VALUES(?,?,?)",
                             ("p2", 2031, json.dumps(archived_development)))
                conn.execute("INSERT INTO player_development VALUES(?,?,?)",
                             ("p3", 2028, json.dumps(legacy_development)))
                conn.execute("INSERT INTO awards VALUES(?,?,?,?,?,?)",
                             ("2031:season_awards:golden_boy:0", 2031, "season_awards",
                              "golden_boy", "Apollo FC", json.dumps(award)))
                conn.commit()

            # Simulate a pre-compaction database where the version marker is absent.
            with sqlite3.connect(path) as conn:
                conn.execute("DELETE FROM metadata WHERE key='projection_compaction_version'")
                conn.commit()
            db.save_career({"version": 1, "state": state}, season_archives=[archive])

            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT player_id,season,club FROM player_career").fetchall(),
                                 [("p3", 2028, "Old Club")])
                self.assertEqual(conn.execute("SELECT player_id,season FROM player_development").fetchall(),
                                 [("p3", 2028)])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM season_archives").fetchone()[0], 1)

    def test_season_archive_upsert_preserves_unchanged_row_and_older_seasons(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "super_league.db"
            db = SuperLeagueDatabase(path)
            first = {"season": 2030, "champion_premier": "Apollo FC"}
            second = {"season": 2031, "champion_premier": "Boreal FC"}
            db.upsert_season_archives([first])
            with sqlite3.connect(path) as conn:
                before = conn.execute("SELECT rowid FROM season_archives WHERE season=2030").fetchone()[0]
            db.upsert_season_archives([first, second])
            with sqlite3.connect(path) as conn:
                after = conn.execute("SELECT rowid FROM season_archives WHERE season=2030").fetchone()[0]
                self.assertEqual(before, after, "Archive không đổi không được ghi lại")
            self.assertEqual([row["season"] for row in db.load_season_archives()], [2030, 2031])

    def test_news_projection_is_rebuilt_with_only_the_current_and_previous_weeks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = SuperLeagueDatabase(Path(tmp) / "super_league.db")
            state = {
                "season": 2026,
                "season_start_date": "2026-08-01",
                "slots": [{"date": "2026-10-30", "played": False}],
                "news": [
                    {"kind": "match_summary", "date": "2026-10-18", "match_id": 1},
                    {"kind": "match_summary", "date": "2026-10-19", "match_id": 2},
                    {"kind": "champion", "date": "2026-10-30", "headline": "Vô địch"},
                    {"kind": "competition_award", "date": "2026-11-01", "headline": "Trao giải"},
                    {"kind": "match_summary", "date": "2026-11-02", "match_id": 3},
                ],
                "last_news": ["Thông báo tạm thời"],
            }
            blob = {"version": 1, "state": state}
            db.save_career(blob)
            saved_news = db.load_career()["state"]["news"]
            self.assertEqual([row.get("date") for row in saved_news], [
                "2026-10-19", "2026-10-30", "2026-11-01",
            ])
            import sqlite3
            conn = sqlite3.connect(db.path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0], 3)
                conn.execute("INSERT INTO news_items VALUES('stale-row',2020,'{}')")
                conn.commit()
            finally:
                conn.close()
            db.save_career(blob)
            conn = sqlite3.connect(db.path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0], 3,
                                 "Mỗi lần save phải xóa projection tin cũ trước khi ghi lại")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM news_items WHERE season < 2026").fetchone()[0], 0)
            finally:
                conn.close()

    def test_schema_upgrade_requests_projection_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "super_league.db"
            db = SuperLeagueDatabase(path)
            blob = {"version": 1, "state": {"season": 2026, "players": {"p": {"player_id": "p"}}, "matches": []}}
            db.save_career(blob)
            import sqlite3
            conn = sqlite3.connect(path)
            try:
                conn.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")
                conn.commit()
            finally:
                conn.close()
            upgraded = SuperLeagueDatabase(path)
            self.assertTrue(upgraded.projection_rebuild_required)
            upgraded.save_career(blob)
            self.assertFalse(upgraded.projection_rebuild_required)
            self.assertEqual(upgraded.status()["schema_version"], 5)

    def test_imports_player_snapshots_and_reference_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "snapshots"
            snapshots.mkdir()
            (snapshots / "players_2026.csv").write_text("player_id,common_name,overall_rating\np1,Player One,88\n", encoding="utf-8")
            reference = root / "data_human.json"
            reference.write_text('{"first_names":["A"],"second_names":["B"]}', encoding="utf-8")
            db = SuperLeagueDatabase(root / "super_league.db")
            self.assertEqual(db.import_player_snapshots(snapshots), 1)
            self.assertTrue(db.import_reference_data("human_names", reference))
            import sqlite3
            conn = sqlite3.connect(db.path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM player_snapshots").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM reference_data").fetchone()[0], 1)
            finally:
                conn.close()

    def test_failed_snapshot_does_not_destroy_previous_career(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = SuperLeagueDatabase(Path(tmp) / "super_league.db")
            original = {"version": 1, "state": {"season": 2026, "players": {}, "matches": []}, "rng_state": None}
            db.save_career(original)
            with self.assertRaises(Exception):
                db.save_career({"version": 1, "state": {"season": 2027, "players": {}, "transfer_history": [None]}})
            self.assertEqual(db.load_career()["state"]["season"], 2026)


if __name__ == "__main__":
    unittest.main()
