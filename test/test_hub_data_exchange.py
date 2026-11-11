"""Tests for one-file SQLite export/import bundles."""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sl_data_exchange as exchange
from sl_database import SuperLeagueDatabase

PROJECT = Path(__file__).resolve().parents[1]


def make_project(root: Path, *, season=2032, marker="source"):
    memory = root / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT / "memory" / "super_league.db", memory / "super_league.db")
    db = SuperLeagueDatabase(memory / "super_league.db")
    db.save_career({
        "version": 1,
        "state": {
            "season": season,
            "season_done": False,
            "players": {"p1": {"name": "Test Player", "marker": marker}},
            "matches": [{"id": 1, "home": "A", "away": "B", "hg": 2, "ag": 1}],
            "history": [],
        },
    })
    (root / "config.json").write_text(json.dumps({
        "seed": 1,
        "google_sheets": {
            "enabled": False,
            "spreadsheet_id": "LOCAL_SHEET_ID",
            "apps_script_url": "https://local.invalid",
            "apps_script_token": "LOCAL_SECRET",
        },
    }), encoding="utf-8")
    (root / "award_settings.json").write_text('{"settings":{}}', encoding="utf-8")
    return db


class DataExchangeTests(unittest.TestCase):
    def test_export_contains_exactly_one_database_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            make_project(root)
            bundle = exchange.build_bundle(root)
            self.assertEqual(bundle["format"], exchange.FORMAT_NAME)
            self.assertEqual(bundle["format_version"], 3)
            self.assertEqual([item["path"] for item in bundle["files"]], ["memory/super_league.db"])
            summary = exchange.validate_bundle(bundle)
            self.assertEqual(summary["integrity"], "ok")
            self.assertEqual(summary["source_player_count"], 552)
            self.assertEqual(summary["reference_data_count"], 1)
            self.assertEqual(summary["current_season"], 2032)
            self.assertEqual(summary["file_count"], 1)

            output = root / "database_export.txt"
            exchange.write_bundle(root, output)
            parsed = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exchange.validate_bundle(parsed)["integrity"], "ok")

    def test_import_replaces_only_database_and_cleans_all_memory_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            target = base / "target"
            make_project(source, season=2034, marker="incoming")
            make_project(target, season=2020, marker="existing")
            local_config_before = (target / "config.json").read_bytes()

            memory = target / "memory"
            (memory / "save.json").write_text('{"old":true}', encoding="utf-8")
            (memory / "generated_players.csv").write_text("old", encoding="utf-8")
            (memory / "season").mkdir()
            (memory / "season" / "2020-2021.json").write_text("{}", encoding="utf-8")
            (memory / "backups").mkdir()
            (memory / "backups" / "old.zip").write_bytes(b"old")

            exchange.apply_bundle(target, exchange.build_bundle(source))
            imported = SuperLeagueDatabase(memory / "super_league.db").load_career()
            self.assertEqual(imported["state"]["season"], 2034)
            self.assertEqual(imported["state"]["players"]["p1"]["marker"], "incoming")
            self.assertEqual([path.name for path in memory.iterdir()], ["super_league.db"])
            self.assertEqual((target / "config.json").read_bytes(), local_config_before)
            self.assertTrue((target / "award_settings.json").is_file())

    def test_rejects_path_traversal_and_extra_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            make_project(root)
            bundle = exchange.build_bundle(root)
            bad_path = json.loads(json.dumps(bundle))
            bad_path["files"][0]["path"] = "../../outside.db"
            with self.assertRaises(exchange.DataBundleError):
                exchange.validate_bundle(bad_path)
            extra = json.loads(json.dumps(bundle))
            extra["files"].append(json.loads(json.dumps(extra["files"][0])))
            with self.assertRaises(exchange.DataBundleError):
                exchange.validate_bundle(extra)

    def test_rejects_checksum_tampering_and_non_database_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            make_project(root)
            bundle = exchange.build_bundle(root)
            tampered = json.loads(json.dumps(bundle))
            tampered["files"][0]["content"] += "x"
            with self.assertRaises(exchange.DataBundleError):
                exchange.validate_bundle(tampered)
            for forbidden in ("credentials/key.json", "memory/data_human.json", "memory/super_league.csv"):
                candidate = json.loads(json.dumps(bundle))
                candidate["files"][0]["path"] = forbidden
                with self.assertRaises(exchange.DataBundleError):
                    exchange.validate_bundle(candidate)

    def test_rejects_malformed_sqlite_before_touching_live_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            original_db = make_project(root)
            before = original_db.load_career()["state"]["season"]
            bundle = exchange.build_bundle(root)
            bad_raw = b"not a sqlite database"
            entry = bundle["files"][0]
            entry["content"] = base64.b64encode(bad_raw).decode("ascii")
            entry["size_bytes"] = len(bad_raw)
            entry["sha256"] = hashlib.sha256(bad_raw).hexdigest()
            with self.assertRaises(exchange.DataBundleError):
                exchange.apply_bundle(root, bundle)
            self.assertEqual(SuperLeagueDatabase(root / "memory" / "super_league.db").load_career()["state"]["season"], before)

    def test_failed_atomic_replace_leaves_original_database_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            target = base / "target"
            make_project(source, season=2035)
            target_db = make_project(target, season=2021)
            bundle = exchange.build_bundle(source)
            with mock.patch("sl_data_exchange.os.replace", side_effect=OSError("mock replace failure")):
                with self.assertRaises(exchange.DataBundleError):
                    exchange.apply_bundle(target, bundle)
            self.assertEqual(SuperLeagueDatabase(target / "memory" / "super_league.db").load_career()["state"]["season"], 2021)
            self.assertTrue(target_db.has_career())


if __name__ == "__main__":
    unittest.main(verbosity=2)
