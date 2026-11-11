# -*- coding: utf-8 -*-
"""Offline tests for Award Recalculator historical Google Sheets push."""
import sys
import tempfile
import unittest
import types
import json
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recalculate_awards import (  # noqa: E402
    HISTORY_SHEET_HEADERS,
    build_history_sheet_payload,
    push_history_payload,
    push_history_records_to_google_sheets,
)


API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"


class FakeResponse:
    def __init__(self, data=None, status_code=200):
        self._data = data or {}
        self.status_code = status_code

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, existing_title=None):
        self.sheets = []
        self.next_id = 100
        self.calls = []
        self.writes = []
        if existing_title:
            self.sheets.append({"sheetId": 1, "title": existing_title})

    def request(self, method, url, timeout=90, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET" and url == f"{API_ROOT}/fake-spreadsheet":
            return FakeResponse({"sheets": [{"properties": row} for row in self.sheets]})
        if method == "POST" and url.endswith(":batchUpdate"):
            replies = []
            for request in kwargs.get("json", {}).get("requests", []):
                if "addSheet" in request:
                    props = request["addSheet"]["properties"]
                    row = {"sheetId": self.next_id, "title": props["title"]}
                    self.sheets.append(row)
                    replies.append({"addSheet": {"properties": row}})
                    self.next_id += 1
                elif "updateSheetProperties" in request:
                    props = request["updateSheetProperties"]["properties"]
                    for row in self.sheets:
                        if row["sheetId"] == props["sheetId"] and "title" in props:
                            row["title"] = props["title"]
                    replies.append({})
                elif "deleteSheet" in request:
                    sid = request["deleteSheet"]["sheetId"]
                    self.sheets = [row for row in self.sheets if row["sheetId"] != sid]
                    replies.append({})
                else:
                    replies.append({})
            return FakeResponse({"replies": replies})
        if method == "PUT" and "/values/" in url:
            body = kwargs.get("json", {})
            self.writes.append((url, kwargs.get("params", {}), body))
            return FakeResponse({"updatedRows": len(body.get("values", []))})
        raise AssertionError(f"Unexpected request {method} {url}")


class FakeSyncClient:
    spreadsheet_id = "fake-spreadsheet"

    def _request(self, session, method, url, **kwargs):
        response = session.request(method, url, timeout=90, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError("Mock Google Sheets API error")
        return response.json()


class HistorySheetExportTests(unittest.TestCase):
    def sample_record(self):
        return {
            "season": 2026,
            "label": "2026/2027",
            "champion_premier": "Team A",
            "champion_cup": "Team B",
            "champion_c1": "Team C",
            "competition_awards": {
                "premier": {"competition": "Premier League", "completed": True,
                             "champion": "Team A", "runner_up": "Team D",
                             "player_of_tournament": {"player": "Player A", "team": "Team A", "apps": 30}}
            },
            "leagues": {"premier": [{"team": "Team A", "p": 20, "w": 15, "d": 3, "l": 2,
                                      "gf": 40, "ga": 10, "gd": 30, "pts": 48, "elo": 1900}]},
            "qualification_table": [{"rank": 1, "team": "Team A", "total": 110, "league_points": 80,
                                     "cup_points": 30, "gd": 30, "elo": 1900}],
            "career_player_stats": [{"player_id": "p1", "player": "Player A", "team": "Team A",
                                     "position": "ST", "apps": 30, "minutes": 2500, "goals": 20,
                                     "assists": 6, "avg_rating": 8.1}],
            "top_scorers": [{"player": "Player A", "team": "Team A", "goals": 20}],
            "top_assists": [{"player": "Player B", "team": "Team D", "assists": 10}],
            "season_awards": {"ballon_dor": {"player": "Player A", "team": "Team A"},
                              "ballon_silver": {"player": "Player B", "team": "Team D"},
                              "ballon_bronze": {"player": "Player C", "team": "Team C"},
                              "young_player": {"player": "Player D", "team": "Team B"},
                              "best_xi_442": [{"player": "Player A", "role": "ST"}],
                              "top20_players": [{"player": "Player A", "rank": 1}]},
            "matches": [{"id": 1, "comp": "Premier League", "stage": "league_premier_1",
                         "date": "2027-01-01", "played": True, "home": "Team A", "away": "Team D",
                         "hg": 2, "ag": 1, "winner": "Team A",
                         "goals": [{"minute": 10, "scorer": "Player A"}],
                         "cards_h": [], "cards_a": [], "player_stats": {"h": {}, "a": {}}}],
            "development": [{"player": "Player A", "old_ovr": 80, "new_ovr": 82}],
            "retired": [{"player_name": "Veteran", "age": 38, "retirement_reason": "Theo tuổi"}],
            "youth_promoted": [{"player": "Youngster", "age": 17}],
            "promoted": [{"team": "Team B", "from": "FC1", "to": "Premier"}],
            "relegated": [{"team": "Team Z", "from": "Premier", "to": "FC1"}],
            "league_phase2": {"premier": {"champion_teams": ["Team A"], "challenge_teams": ["Team D"]}},
            "cup_bracket": {"final": [1]},
            "c1": {"bracket": {"final": [2]}},
            "extra_custom_field": {"deep": {"cards": ["red", "yellow"]}},
            "match_count": 1,
        }

    def test_payload_uses_exact_title_and_captures_all_top_level_fields(self):
        record = self.sample_record()
        payload = build_history_sheet_payload(record)
        self.assertEqual(payload["sheet_title"], "Lịch sử 2026-2027")
        self.assertEqual(len(payload["headers"]), len(HISTORY_SHEET_HEADERS))
        self.assertEqual(payload["match_count"], 1)
        raw_names = {row[5] for row in payload["rows"] if row[0] == "ARCHIVE_JSON"}
        self.assertEqual(raw_names, set(record))
        text = " ".join(str(cell) for row in payload["rows"] for cell in row)
        for expected in ("BẢNG XẾP HẠNG LEAGUE", "CHAMPIONS POINTS", "TOP GHI BÀN",
                         "TOP KIẾN TẠO", "Quả bóng Vàng", "ĐỘI HÌNH 4-4-2",
                         "LỊCH SỬ TỪNG TRẬN ĐẤU", "CẦU THỦ GIẢI NGHỆ"):
            self.assertIn(expected, text)
        self.assertTrue(all(len(str(cell)) < 50000 for row in payload["rows"] for cell in row))

    def test_text_push_creates_canonical_tab_without_apps_script_or_images(self):
        payload = build_history_sheet_payload(self.sample_record())
        client = FakeSyncClient()
        session = FakeSession()
        result = push_history_payload(client, payload, session=session)
        self.assertEqual(result["worksheet_title"], "Lịch sử 2026-2027")
        self.assertTrue(result["created"])
        self.assertEqual(result["rows_written"], len(payload["rows"]))
        self.assertEqual([item["title"] for item in session.sheets], ["Lịch sử 2026-2027"])
        self.assertTrue(session.writes)
        self.assertTrue(all(params.get("valueInputOption") == "RAW" for _, params, _ in session.writes))
        self.assertFalse(any("script.google.com" in url or "images" in url.lower()
                             for method, url, _ in session.calls))

    def test_existing_history_tab_is_replaced_after_staging(self):
        payload = build_history_sheet_payload(self.sample_record())
        client = FakeSyncClient()
        session = FakeSession(existing_title="Lịch sử 2026-2027")
        result = push_history_payload(client, payload, session=session)
        self.assertFalse(result["created"])
        self.assertEqual([item["title"] for item in session.sheets], ["Lịch sử 2026-2027"])
        self.assertTrue(any("_SL_TMP_History_" in str(call) for call in session.calls))


    def test_high_level_push_requires_service_account_but_not_apps_script(self):
        record = self.sample_record()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps({"google_sheets": {
                "enabled": True, "spreadsheet_id": "fake-spreadsheet",
                "service_account_file": "credentials/key.json"
            }}), encoding="utf-8")
            credentials = root / "credentials" / "key.json"
            credentials.parent.mkdir(parents=True)
            credentials.write_text("{}", encoding="utf-8")
            session = FakeSession()

            class FakeGoogleSheetsSync:
                def __init__(self, config, app_dir, data_dir):
                    self.spreadsheet_id = config["spreadsheet_id"]
                def credential_path(self):
                    return credentials
                def _make_session(self):
                    return session
                def _request(self, sess, method, url, **kwargs):
                    response = sess.request(method, url, timeout=90, **kwargs)
                    return response.json()

            fake_module = types.ModuleType("sl_google_sheets")
            fake_module.GoogleSheetsSync = FakeGoogleSheetsSync
            def col_letter(number):
                result = ""
                while number:
                    number, rem = divmod(number - 1, 26)
                    result = chr(65 + rem) + result
                return result
            fake_module._column_letter = col_letter
            second_record = self.sample_record()
            second_record["season"] = 2027
            second_record["label"] = "2027/2028"
            with patch.dict(sys.modules, {"sl_google_sheets": fake_module}):
                result = push_history_records_to_google_sheets(root, [second_record, record])
            self.assertEqual([item["worksheet_title"] for item in result],
                             ["Lịch sử 2026-2027", "Lịch sử 2027-2028"])
            self.assertEqual({item["title"] for item in session.sheets},
                             {"Lịch sử 2026-2027", "Lịch sử 2027-2028"})

    def test_full_season_payload_contains_all_match_logs_and_player_rows(self):
        from sl_game import Game
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261101, enable_google_sheets=False)
            game.simulate_full_season(persist_each_slot=False)
            record = game.s["history"][-1]
            payload = build_history_sheet_payload(record)
            self.assertEqual(payload["match_count"], 246)
            self.assertEqual(payload["player_count"], len(record["career_player_stats"]))
            match_rows = [row for row in payload["rows"] if row[0] == "MATCH"]
            stat_rows = [row for row in payload["rows"] if row[0] == "PLAYER_SEASON_STATS"]
            raw_fields = {row[5] for row in payload["rows"] if row[0] == "ARCHIVE_JSON"}
            self.assertEqual(len(match_rows), 246)
            self.assertEqual(len(stat_rows), len(record["career_player_stats"]))
            self.assertEqual(raw_fields, set(record))
            self.assertTrue(all(len(str(cell)) < 50000 for row in payload["rows"] for cell in row))
            client = FakeSyncClient()
            session = FakeSession()
            result = push_history_payload(client, payload, session=session)
            self.assertEqual(result["rows_written"], len(payload["rows"]))
            self.assertGreater(len(session.writes), 1)  # Large history is sent in bounded requests.
            self.assertTrue(all(len(str(body).encode("utf-8")) < 1_000_000
                                for _url, _params, body in session.writes))

    def test_long_archived_json_is_chunked_below_sheet_cell_limit(self):
        record = self.sample_record()
        record["large_blob"] = {"text": "x" * 120000}
        payload = build_history_sheet_payload(record)
        raw_rows = [row for row in payload["rows"] if row[0] == "ARCHIVE_JSON" and row[5] == "large_blob"]
        self.assertEqual(len(raw_rows), 4)
        self.assertTrue(all(len(row[25]) <= 32000 for row in raw_rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
