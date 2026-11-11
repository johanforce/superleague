# -*- coding: utf-8 -*-
"""Deterministic tests for the compact season overview Google Sheets export."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sl_game import Game
from sl_database import SuperLeagueDatabase
from season_archive import write_season_archive
from sl_google_sheets import (
    API_ROOT,
    MAX_SCREENSHOT_BYTES,
    compress_pil_image,
    EXPORT_MARKER,
    REQUIRED_SCREENSHOT_KEYS,
    SHEET_COLUMNS,
    STANDING_HEADERS,
    GoogleSheetsSync,
    _column_letter,
    build_season_payload,
)


class FakeResponse:
    def __init__(self, data=None, status_code=200):
        self._data = data or {}
        self.status_code = status_code

    def json(self):
        return self._data


class FakeSheetsSession:
    """Minimal fake for Sheets API calls; never accesses the network."""
    def __init__(self, spreadsheet_id, existing_title=None, marker=None, fail_status=0,
                 existing_sheets=None, identities=None):
        self.spreadsheet_id = spreadsheet_id
        self.fail_status = fail_status
        self.calls = []
        self.next_sheet_id = 918273
        self.sheets = []
        self.identities = dict(identities or {})
        if existing_sheets:
            self.sheets = [dict(item) for item in existing_sheets]
        elif existing_title:
            self.sheets = [{"sheetId": 1234, "title": existing_title}]
            self.identities.setdefault(existing_title, [marker or "", "MÙA 2026/2027"])
        self.existing_title = existing_title

    def request(self, method, url, timeout=30, **kwargs):
        from urllib.parse import unquote
        self.calls.append((method, url, kwargs))
        if self.fail_status:
            return FakeResponse({"error": {"message": "mock API failure"}}, self.fail_status)
        if method == "GET" and url == f"{API_ROOT}/{self.spreadsheet_id}":
            return FakeResponse({"sheets": [{"properties": item} for item in self.sheets]})
        if method == "GET" and "/values/" in url:
            expression = unquote(url.rsplit("/values/", 1)[1])
            title = expression.split("!", 1)[0].strip("'").replace("''", "'")
            identity = self.identities.get(title, [])
            return FakeResponse({"values": [identity] if identity else []})
        if method == "POST" and url.endswith(":batchUpdate"):
            requests = kwargs.get("json", {}).get("requests", [])
            replies = []
            for request in requests:
                if "addSheet" in request:
                    title = request["addSheet"]["properties"]["title"]
                    self.sheets.append({"sheetId": self.next_sheet_id, "title": title})
                    self.existing_title = title
                    replies.append({"addSheet": {"properties": {
                        "sheetId": self.next_sheet_id, "title": title
                    }}})
                    self.next_sheet_id += 1
                elif "updateSheetProperties" in request:
                    props = request["updateSheetProperties"]["properties"]
                    if "title" in props:
                        for item in self.sheets:
                            if item["sheetId"] == props["sheetId"]:
                                old_title = item["title"]
                                item["title"] = props["title"]
                                if old_title in self.identities:
                                    self.identities[props["title"]] = self.identities.pop(old_title)
                                self.existing_title = props["title"]
                    replies.append({})
                elif "deleteSheet" in request:
                    sheet_id = request["deleteSheet"]["sheetId"]
                    removed = [item["title"] for item in self.sheets if item["sheetId"] == sheet_id]
                    self.sheets = [item for item in self.sheets if item["sheetId"] != sheet_id]
                    for title in removed:
                        self.identities.pop(title, None)
                    replies.append({})
                else:
                    replies.append({})
            return FakeResponse({"replies": replies})
        if method == "POST" and ":clear" in url:
            return FakeResponse({"clearedRange": "mock"})
        if method == "PUT" and "/values/" in url:
            body = kwargs.get("json", {})
            return FakeResponse({"updatedRows": len(body.get("values", [])),
                                 "updatedColumns": len(body.get("values", [[]])[0]) if body.get("values") else 0})
        raise AssertionError(f"Unexpected mock API call: {method} {url}")


class GoogleSheetsTests(unittest.TestCase):
    def make_game_payload(self, tmp):
        game = Game(data_dir=tmp, fresh=True, seed=20261030, enable_google_sheets=False)
        game.advance_slot(persist=False)
        return game, build_season_payload(game)

    def test_column_letter_conversion(self):
        self.assertEqual(_column_letter(1), "A")
        self.assertEqual(_column_letter(26), "Z")
        self.assertEqual(_column_letter(27), "AA")
        self.assertEqual(_column_letter(40), "AN")

    def test_payload_is_one_season_overview_with_three_league_tables_and_four_image_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            _game, payload = self.make_game_payload(tmp)
            self.assertEqual(payload["sheet_title"], "Mùa 2026-2027")
            self.assertEqual(payload["headers"], STANDING_HEADERS)
            self.assertTrue(all(len(row) == SHEET_COLUMNS for row in payload["rows"]))
            self.assertEqual(set(payload["image_rows"]), set(REQUIRED_SCREENSHOT_KEYS))
            self.assertEqual(len(payload["section_rows"]), 7)
            text = " ".join(str(cell) for row in payload["rows"] for cell in row)
            for label in ("PREMIER LEAGUE", "FOOTBALL CHAMPIONSHIP 1", "FOOTBALL CHAMPIONSHIP 2",
                          "MAJOR CUP", "CHAMPIONS LEAGUE · PLAY-IN", "CHAMPIONS LEAGUE · GROUP STATE",
                          "CHAMPIONS LEAGUE · KNOCKOUT"):
                self.assertIn(label, text)
            self.assertEqual(payload["match_count"], 4)
            self.assertEqual(len(payload["table_ranges"]), 3)

    def test_payload_contains_only_standings_and_image_placeholders_not_match_player_dump(self):
        with tempfile.TemporaryDirectory() as tmp:
            _game, payload = self.make_game_payload(tmp)
            self.assertLess(len(payload["rows"]), 60)
            self.assertTrue(all(not str(cell).startswith("MATCH") for row in payload["rows"] for cell in row))
            self.assertEqual(payload["record_count"], 24)

    def test_full_season_stays_one_worksheet_per_season(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030, enable_google_sheets=False)
            game.simulate_full_season(persist_each_slot=False)
            payload = build_season_payload(game)
            self.assertEqual(payload["match_count"], 246)
            self.assertEqual(payload["sheet_title"], "Mùa 2026-2027")
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["super_league.db"])
            game.next_season()
            next_payload = build_season_payload(game)
            self.assertNotEqual(payload["sheet_title"], next_payload["sheet_title"])
            self.assertEqual(next_payload["season"], payload["season"] + 1)
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["super_league.db"])

    def test_each_persisted_match_batch_queues_a_google_sheets_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030, enable_google_sheets=True)
            queued = []
            game._queue_google_sheets_sync = lambda reason="match_saved": queued.append(reason)
            game.advance_slot(persist=True)
            self.assertEqual(queued, ["career_saved"])
            self.assertTrue((Path(tmp) / "super_league.db").is_file())
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["super_league.db"])

    def test_sync_creates_a_new_season_worksheet_and_writes_compact_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            _game, payload = self.make_game_payload(tmp)
            sync = GoogleSheetsSync({"enabled": True, "spreadsheet_id": "sheet-id"}, Path(tmp), Path(tmp) / "data")
            session = FakeSheetsSession("sheet-id")
            result = sync.sync_payload(payload, session=session)
            self.assertTrue(result["created"])
            self.assertEqual(result["worksheet_title"], payload["sheet_title"])
            self.assertEqual(result["rows_written"], len(payload["rows"]))
            self.assertEqual(result["columns_written"], SHEET_COLUMNS)
            self.assertEqual(result["image_count"], 0)  # Screenshots are supplied by the GUI pipeline.
            self.assertTrue(any(method == "POST" and url.endswith(":batchUpdate") for method, url, _ in session.calls))
            self.assertTrue(any(method == "POST" and ":clear" in url for method, url, _ in session.calls))
            updates = [call for call in session.calls if call[0] == "PUT" and "/values/" in call[1]]
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0][2]["params"]["valueInputOption"], "USER_ENTERED")
            self.assertEqual(session.existing_title, payload["sheet_title"])

    def test_sync_updates_existing_app_owned_tab_without_creating_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _game, payload = self.make_game_payload(tmp)
            sync = GoogleSheetsSync({"enabled": True, "spreadsheet_id": "sheet-id"}, Path(tmp), Path(tmp) / "data")
            session = FakeSheetsSession("sheet-id", existing_title=payload["sheet_title"], marker=EXPORT_MARKER)
            result = sync.sync_payload(payload, session=session)
            self.assertFalse(result["created"])
            self.assertEqual(result["sheet_id"], 1234)
            self.assertFalse(any("addSheet" in request
                                 for call in session.calls if call[0] == "POST" and call[1].endswith(":batchUpdate")
                                 for request in call[2].get("json", {}).get("requests", [])))

    def test_existing_canonical_tab_is_reused_and_overwritten_even_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            _game, payload = self.make_game_payload(tmp)
            sync = GoogleSheetsSync({"enabled": True, "spreadsheet_id": "sheet-id"}, Path(tmp), Path(tmp) / "data")
            session = FakeSheetsSession("sheet-id", existing_title=payload["sheet_title"], marker="USER_DATA")
            result = sync.sync_payload(payload, session=session)
            self.assertFalse(result["created"])
            self.assertEqual(result["worksheet_title"], payload["sheet_title"])
            self.assertEqual(result["sheet_id"], 1234)
            self.assertTrue(any(method == "POST" and ":clear" in url for method, url, _ in session.calls))
            self.assertTrue(any(method == "PUT" and "/values/" in url for method, url, _ in session.calls))
            self.assertFalse(any("addSheet" in request
                                 for method, url, kwargs in session.calls if method == "POST" and url.endswith(":batchUpdate")
                                 for request in kwargs.get("json", {}).get("requests", [])))

    def test_legacy_suffixed_season_tab_is_renamed_instead_of_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            _game, payload = self.make_game_payload(tmp)
            sync = GoogleSheetsSync({"enabled": True, "spreadsheet_id": "sheet-id"}, Path(tmp), Path(tmp) / "data")
            legacy_title = payload["sheet_title"] + " - Super League"
            session = FakeSheetsSession("sheet-id", existing_title=legacy_title, marker=EXPORT_MARKER)
            result = sync.sync_payload(payload, session=session)
            self.assertFalse(result["created"])
            self.assertEqual(result["worksheet_title"], payload["sheet_title"])
            self.assertEqual(result["sheet_id"], 1234)
            self.assertEqual([item["title"] for item in session.sheets], [payload["sheet_title"]])
            self.assertTrue(any("updateSheetProperties" in request
                                for method, url, kwargs in session.calls if method == "POST" and url.endswith(":batchUpdate")
                                for request in kwargs.get("json", {}).get("requests", [])))
            self.assertFalse(any("addSheet" in request
                                 for method, url, kwargs in session.calls if method == "POST" and url.endswith(":batchUpdate")
                                 for request in kwargs.get("json", {}).get("requests", [])))

    def test_duplicate_cleanup_deletes_only_owned_tabs_for_same_season(self):
        with tempfile.TemporaryDirectory() as tmp:
            _game, payload = self.make_game_payload(tmp)
            sync = GoogleSheetsSync({"enabled": True, "spreadsheet_id": "sheet-id"}, Path(tmp), Path(tmp) / "data")
            canonical = payload["sheet_title"]
            other_season = "Mùa 2027-2028 - Super League"
            session = FakeSheetsSession(
                "sheet-id",
                existing_sheets=[
                    {"sheetId": 10, "title": canonical},
                    {"sheetId": 11, "title": canonical + " - Super League"},
                    {"sheetId": 12, "title": other_season},
                ],
                identities={
                    canonical: ["USER_DATA", ""],
                    canonical + " - Super League": [EXPORT_MARKER, "MÙA 2026/2027"],
                    other_season: [EXPORT_MARKER, "MÙA 2027-2028"],
                },
            )
            removed = sync._cleanup_duplicate_season_worksheets(session, payload, keep_sheet_id=10)
            self.assertEqual(removed, 1)
            self.assertEqual([item["title"] for item in session.sheets], [canonical, other_season])

    def test_missing_apps_script_or_credentials_returns_setup_required_without_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync = GoogleSheetsSync({"enabled": True, "spreadsheet_id": "sheet-id",
                                     "service_account_file": "missing.json"}, Path(tmp), Path(tmp) / "data")
            result = sync.enqueue({"season": 2026, "sheet_title": "Mùa 2026-2027", "match_count": 0})
            self.assertEqual(result["status"], "setup_required")
            self.assertIsNone(sync._worker)

    def test_api_http_error_is_reported_to_caller(self):
        with tempfile.TemporaryDirectory() as tmp:
            sync = GoogleSheetsSync({"enabled": True, "spreadsheet_id": "sheet-id"}, Path(tmp), Path(tmp) / "data")
            session = FakeSheetsSession("sheet-id", fail_status=403)
            with self.assertRaisesRegex(RuntimeError, "mock API failure"):
                sync._sheet_metadata(session)

    def test_worker_coalesces_newer_snapshot_and_keeps_final_status_current(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "service-account.json"
            credential.write_text("{}", encoding="utf-8")
            sync = GoogleSheetsSync({
                "enabled": True, "spreadsheet_id": "sheet-id",
                "service_account_file": str(credential), "apps_script_url": "https://example.invalid/exec",
                "apps_script_token": "test-token", "debounce_seconds": 0,
                "retry_delays_seconds": [1],
            }, root, root / "data")
            first_started = threading.Event()
            release_first = threading.Event()
            processed = []

            def fake_sync(payload):
                processed.append(payload["season"])
                if payload["season"] == 2026:
                    first_started.set()
                    release_first.wait(3)
                return {"worksheet_title": payload["sheet_title"]}

            sync.sync_payload = fake_sync
            sync.enqueue({"season": 2026, "sheet_title": "Mùa 2026-2027", "match_count": 4})
            self.assertTrue(first_started.wait(2), "Worker did not start the first snapshot")
            sync.enqueue({"season": 2027, "sheet_title": "Mùa 2027-2028", "match_count": 8})
            release_first.set()
            self.assertTrue(sync.wait_until_idle(5), "Worker did not finish the latest snapshot")
            self.assertEqual(processed, [2026, 2027])
            self.assertEqual(sync.status().get("status"), "synced")
            self.assertEqual(sync.status().get("season"), 2027)
            self.assertEqual(sync.status().get("worksheet_title"), "Mùa 2027-2028")
            sync.stop(timeout=1)


    def test_screenshot_compression_guarantees_max_bytes_and_max_side(self):
        from PIL import Image

        # A high-detail image forces the encoder to lower quality and/or dimensions.
        source = Image.effect_noise((2600, 1800), 100).convert("RGB")
        encoded = compress_pil_image(source, max_side=1400, quality=80, max_bytes=MAX_SCREENSHOT_BYTES)
        self.assertLessEqual(len(encoded), MAX_SCREENSHOT_BYTES)
        from io import BytesIO
        with Image.open(BytesIO(encoded)) as result:
            self.assertLessEqual(max(result.size), 1400)
            self.assertEqual(result.format, "JPEG")

    def test_four_screenshots_are_compressed_before_apps_script_bridge(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            _game, payload = self.make_game_payload(tmp)
            screenshots = {}
            for key in REQUIRED_SCREENSHOT_KEYS:
                path = Path(tmp) / f"{key}.jpg"
                # Existing/legacy screenshot files may be larger than the new limit.
                Image.effect_noise((2400, 1600), 80).convert("RGB").save(path, "JPEG", quality=95)
                screenshots[key] = str(path)
            payload["screenshots"] = screenshots
            calls = {}

            class FakeBridgeResponse:
                status_code = 200
                text = ""

                @staticmethod
                def json():
                    return {"ok": True, "image_count": 4, "cleanup_pending": False}

            def fake_post(url, json, timeout, allow_redirects):
                calls.update({"url": url, "json": json, "timeout": timeout, "allow_redirects": allow_redirects})
                return FakeBridgeResponse()

            fake_requests = SimpleNamespace(post=fake_post, RequestException=Exception)
            sync = GoogleSheetsSync({"enabled": True, "spreadsheet_id": "sheet-id",
                                     "apps_script_url": "https://script.example/exec",
                                     "apps_script_token": "secret-token"}, Path(tmp), Path(tmp) / "data")
            with patch.dict("sys.modules", {"requests": fake_requests}):
                result = sync._insert_screenshots(payload, payload["sheet_title"])

            self.assertEqual(result["image_count"], 4)
            self.assertFalse(result["cleanup_pending"])
            self.assertEqual(calls["url"], "https://script.example/exec")
            self.assertGreaterEqual(calls["timeout"], 90)
            self.assertLessEqual(result.get("request_body_bytes", 0), 4_300_000)
            self.assertEqual(calls["json"]["token"], "secret-token")
            self.assertEqual(calls["json"]["sheet_title"], payload["sheet_title"])
            sent_images = calls["json"]["images"]
            self.assertEqual({item["key"] for item in sent_images}, set(REQUIRED_SCREENSHOT_KEYS))
            self.assertTrue(all(item["row"] in payload["image_rows"].values() for item in sent_images))
            self.assertTrue(all(item["mime_type"] == "image/jpeg" for item in sent_images))
            self.assertTrue(all(item["size_bytes"] <= MAX_SCREENSHOT_BYTES for item in sent_images))
            self.assertTrue(all(len(item["data_base64"]) < 2_000_100 for item in sent_images))

    def test_permanent_error_stops_retry_and_later_manual_sync_can_reuse_worker(self):
        import threading
        from sl_google_sheets import GoogleSheetsApiError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "service-account.json"
            credential.write_text("{}", encoding="utf-8")
            sync = GoogleSheetsSync({
                "enabled": True, "spreadsheet_id": "sheet-id",
                "service_account_file": str(credential),
                "apps_script_url": "https://example.invalid/exec",
                "apps_script_token": "test-token", "debounce_seconds": 0,
                "retry_delays_seconds": [1],
            }, root, root / "data")
            attempts = []
            failed_once = threading.Event()

            def fake_sync(payload):
                attempts.append(payload["season"])
                if len(attempts) == 1:
                    failed_once.set()
                    raise GoogleSheetsApiError("permission denied", status_code=403)
                return {"worksheet_title": payload["sheet_title"], "image_count": 4}

            sync.sync_payload = fake_sync
            first = {"season": 2026, "sheet_title": "Mùa 2026-2027", "match_count": 4}
            sync.enqueue(first)
            self.assertTrue(failed_once.wait(2))
            self.assertTrue(sync.wait_until_idle(3))
            self.assertEqual(sync.status().get("status"), "error")
            self.assertTrue(sync._worker.is_alive(), "Worker should remain available for a manual retry")

            second = {"season": 2026, "sheet_title": "Mùa 2026-2027", "match_count": 5}
            sync.enqueue(second)
            self.assertTrue(sync.wait_until_idle(3))
            self.assertEqual(attempts, [2026, 2026])
            self.assertEqual(sync.status().get("status"), "synced")
            self.assertEqual(sync.status().get("image_count"), 4)
            sync.stop(timeout=1)

    def test_build_season_history_sync_payload_reads_all_archive_files_and_names_tabs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = root / "memory"
            game = Game(data_dir=memory, fresh=True, seed=20261030, enable_google_sheets=False)
            records = []
            for season in (2026, 2027):
                records.append({
                    "season": season,
                    "label": f"{season}/{season + 1}",
                    "matches": [{"id": season, "played": True}],
                })
            SuperLeagueDatabase(memory / "super_league.db").replace_season_archives(records)
            payload = game.build_season_history_sync_payload()
            self.assertEqual([item["file_name"] for item in payload],
                             ["2026-2027.json", "2027-2028.json"])
            self.assertEqual([item["sheet_title"] for item in payload],
                             ["Mùa 2026-2027", "Mùa 2027-2028"])
            self.assertEqual([item["season"]["season"] for item in payload], [2026, 2027])
            self.assertEqual(sorted(path.name for path in memory.iterdir()), ["super_league.db"])

    def test_sync_season_history_posts_archive_records_to_apps_script(self):
        calls = {}

        class FakeBridgeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {
                    "ok": True,
                    "mode": "season_history",
                    "season_count": 2,
                    "seasons": [
                        {"season": 2026, "sheet_title": "Mùa 2026-2027", "row_count": 10},
                        {"season": 2027, "sheet_title": "Mùa 2027-2028", "row_count": 10},
                    ],
                }

        def fake_post(url, json, timeout, allow_redirects):
            calls.update({"url": url, "json": json, "timeout": timeout,
                          "allow_redirects": allow_redirects})
            return FakeBridgeResponse()

        fake_requests = SimpleNamespace(post=fake_post, RequestException=Exception)
        sync = GoogleSheetsSync({
            "enabled": True,
            "spreadsheet_id": "sheet-id",
            "apps_script_url": "https://script.example/exec",
            "apps_script_token": "unit-test-token",
            "timeout_seconds": 30,
        }, Path("."), Path("."))
        seasons = [
            {"file_name": "2026-2027.json", "sheet_title": "Mùa 2026-2027",
             "season": {"season": 2026, "label": "2026/2027", "matches": []}},
            {"file_name": "2027-2028.json", "sheet_title": "Mùa 2027-2028",
             "season": {"season": 2027, "label": "2027/2028", "matches": []}},
        ]
        with patch.dict("sys.modules", {"requests": fake_requests}):
            result = sync.sync_season_history(seasons)
        self.assertEqual(result["season_count"], 2)
        self.assertEqual(calls["url"], "https://script.example/exec")
        self.assertEqual(calls["json"]["mode"], "season_history")
        self.assertEqual(calls["json"]["token"], "unit-test-token")
        self.assertEqual(len(calls["json"]["seasons"]), 2)
        self.assertGreaterEqual(calls["timeout"], 90)

    def test_history_queue_worker_reports_season_count(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "service-account.json"
            # History-only Apps Script sync does not require this file.
            sync = GoogleSheetsSync({
                "enabled": True, "spreadsheet_id": "sheet-id",
                "apps_script_url": "https://example.invalid/exec",
                "apps_script_token": "test-token", "debounce_seconds": 0,
                "retry_delays_seconds": [1],
            }, root, root / "data")
            started = threading.Event()

            def fake_history(seasons):
                started.set()
                return {"season_count": len(seasons), "seasons": [
                    {"sheet_title": item["sheet_title"]} for item in seasons
                ]}

            sync.sync_season_history = fake_history
            result = sync.enqueue_history([
                {"sheet_title": "Mùa 2026-2027"},
                {"sheet_title": "Mùa 2027-2028"},
            ])
            self.assertEqual(result["status"], "queued")
            self.assertTrue(started.wait(2))
            self.assertTrue(sync.wait_until_idle(3))
            status = sync.status()
            self.assertEqual(status["status"], "synced")
            self.assertEqual(status["sync_type"], "season_history")
            self.assertEqual(status["history_count"], 2)
            sync.stop(timeout=1)

    def test_local_save_payload_is_independent_from_remote_auth_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261030)
            game.advance_slot(persist=True)
            self.assertTrue((Path(tmp) / "super_league.db").is_file())
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["super_league.db"])
            self.assertEqual(game.google_sheets_status().get("status"), "test_mode")


if __name__ == "__main__":
    unittest.main()
