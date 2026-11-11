# -*- coding: utf-8 -*-
"""Google Sheets season overview export for Super League Simulator.

One canonical worksheet is maintained per season; subsequent syncs overwrite its data. It contains three league standing
tables and four screenshots captured from the real Major Cup/C1 child views.

Authentication uses a Google Cloud service account JSON stored outside source control.
"""
from __future__ import annotations

import copy
import io
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
EXPORT_MARKER = "SUPER_LEAGUE_SIMULATOR_EXPORT_V1"

HEADERS = [
    "Record Type", "Season", "Season Label", "Competition", "Phase", "Stage",
    "Round", "Match Date", "Status", "Match ID", "Home Team", "Away Team",
    "Home Goals", "Away Goals", "Extra Time", "Penalty Score", "Winner",
    "Team", "Rank", "Played", "Won", "Drawn", "Lost", "Goals For",
    "Goals Against", "Goal Difference", "Points", "Elo", "Player ID",
    "Player Name", "Position", "Apps", "Minutes", "Goals", "Assists",
    "Average Rating", "Details JSON 1", "Details JSON 2", "Details JSON 3",
    "Synced At UTC",
]

STANDING_HEADERS = ["Hạng", "CLB", "Trận", "Thắng", "Hòa", "Thua", "BT", "BB", "HS", "Điểm", "Elo"]
SHEET_COLUMNS = len(STANDING_HEADERS)
REQUIRED_SCREENSHOT_KEYS = ("cup_bracket", "c1_playin", "c1_groups", "c1_knockout")
MAX_SCREENSHOT_SIDE = 1200
MAX_SCREENSHOT_BYTES = 750_000  # Lower per-image cap keeps four-image requests compact.
SCREENSHOT_JPEG_QUALITY = 78


def compress_pil_image(image, max_side: int = MAX_SCREENSHOT_SIDE,
                        quality: int = SCREENSHOT_JPEG_QUALITY,
                        max_bytes: int = MAX_SCREENSHOT_BYTES) -> bytes:
    """Encode an image as a size-bounded JPEG, shrinking dimensions if needed.

    Unlike a quality-only loop, this function guarantees the byte limit: once
    quality reaches 40 it keeps reducing dimensions until the output fits.
    The caller's image object is not modified.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Thiếu Pillow. Chạy: pip install -r requirements-google-sheets.txt") from exc

    max_side = max(1, int(max_side))
    max_bytes = max(1, int(max_bytes))
    quality = max(40, min(95, int(quality)))
    img = ImageOps.exif_transpose(image).convert("RGB")
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    current_quality = quality

    while True:
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=current_quality, optimize=True, progressive=True)
        encoded = buffer.getvalue()
        if len(encoded) <= max_bytes:
            return encoded

        if current_quality > 40:
            current_quality = max(40, current_quality - 10)
            continue

        width, height = img.size
        if width <= 1 and height <= 1:
            raise RuntimeError(f"Không thể nén ảnh xuống dưới {max_bytes:,} byte.")
        # Quality alone is no longer enough: reduce both dimensions proportionally.
        next_size = (max(1, int(width * 0.85)), max(1, int(height * 0.85)))
        if next_size == img.size:
            next_size = (max(1, width - 1), max(1, height - 1))
        img = img.resize(next_size, Image.Resampling.LANCZOS)
        current_quality = quality


def compress_image_file(path: str | Path, max_side: int = MAX_SCREENSHOT_SIDE,
                        quality: int = SCREENSHOT_JPEG_QUALITY,
                        max_bytes: int = MAX_SCREENSHOT_BYTES) -> bytes:
    """Read a screenshot and return bounded JPEG bytes (also handles old cached files)."""
    from PIL import Image

    image_path = Path(path).expanduser()
    try:
        raw = image_path.read_bytes()
        with Image.open(image_path) as image:
            width, height = image.size
            already_safe = (
                image.format == "JPEG"
                and max(width, height) <= int(max_side)
                and len(raw) <= int(max_bytes)
            )
            if already_safe:
                return raw
            return compress_pil_image(image, max_side=max_side, quality=quality, max_bytes=max_bytes)
    except Exception as exc:
        if isinstance(exc, RuntimeError) and "nén ảnh" in str(exc).lower():
            raise
        raise RuntimeError(f"Không đọc/nén được screenshot {image_path.name}: {exc}") from exc


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _chunks(value: str, size: int = 43000) -> list[str]:
    """Split details to stay below Google Sheets' 50,000-char cell limit."""
    return [value[i:i + size] for i in range(0, len(value), size)] or [""]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _competition_name(comp: str) -> str:
    value = str(comp or "")
    if value == "Cup":
        return "Major Cup"
    if value in ("C1", "C1 Play-in"):
        return "Champions League"
    return value


def _phase_name(match: dict[str, Any]) -> str:
    comp = str(match.get("comp", ""))
    stage = str(match.get("stage", ""))
    if comp == "C1 Play-in" or "c1_pi_" in stage:
        return "Play-in"
    if comp == "C1" or stage.startswith("c1_"):
        if "group" in stage:
            return "Main Stage - Group"
        if "semi" in stage:
            return "Main Stage - Semi-final"
        if "final" in stage:
            return "Main Stage - Final"
        return "Main Stage"
    if comp == "Cup":
        return "Knockout"
    if stage.startswith("league_"):
        return "League"
    if stage.startswith("phase2_") or "phase2" in stage:
        return "League - Phase 2"
    return ""


def _add_row(rows: list[list[Any]], *, record_type: str, season: int, season_label: str,
             synced_at: str, competition: str = "", phase: str = "", stage: str = "",
             round_label: str = "", match_date: str = "", status: str = "", match_id: Any = "",
             home: str = "", away: str = "", home_goals: Any = "", away_goals: Any = "",
             extra_time: Any = "", penalties: Any = "", winner: str = "", team: str = "",
             rank: Any = "", played: Any = "", won: Any = "", drawn: Any = "", lost: Any = "",
             gf: Any = "", ga: Any = "", gd: Any = "", points: Any = "", elo: Any = "",
             player_id: str = "", player_name: str = "", position: str = "", apps: Any = "",
             minutes: Any = "", goals: Any = "", assists: Any = "", average_rating: Any = "",
             details: Any = None) -> None:
    detail_parts = _chunks(_json(details)) if details is not None else ["", "", ""]
    detail_parts = (detail_parts + ["", "", ""])[:3]
    rows.append([
        record_type, season, season_label, competition, phase, stage, round_label, match_date,
        status, match_id, home, away, home_goals, away_goals, extra_time, penalties, winner,
        team, rank, played, won, drawn, lost, gf, ga, gd, points, elo, player_id,
        player_name, position, apps, minutes, goals, assists, average_rating,
        detail_parts[0], detail_parts[1], detail_parts[2], synced_at,
    ])


def build_season_payload(game: Any) -> dict[str, Any]:
    """Build one season overview: three league tables and four captured UI views."""
    state = game.s
    season = _safe_int(state.get("season", 0))
    label = game.season_label()
    synced_at = _now_utc()
    title = f"Mùa {label.replace('/', '-') }"
    title = re.sub(r"[\[\]:*?/\\]", "-", title).strip(" '")[:90]

    rows: list[list[Any]] = []
    section_rows: list[int] = []
    header_rows: list[int] = []
    table_ranges: list[tuple[int, int]] = []
    image_rows: dict[str, int] = {}

    def append(values: list[Any] | None = None) -> int:
        row = list(values or [])[:SHEET_COLUMNS]
        row.extend([""] * (SHEET_COLUMNS - len(row)))
        rows.append(row)
        return len(rows)  # 1-based worksheet row

    append([EXPORT_MARKER, f"MÙA {label}"])
    append(["SUPER LEAGUE · SEASON OVERVIEW"])
    append([f"Bảng xếp hạng ba hạng liên đoàn và screenshot giao diện cúp · Cập nhật UTC: {synced_at}"])
    append([])

    for tier, competition, subtitle in (
        ("premier", "PREMIER LEAGUE", "Giai đoạn 1"),
        ("fc1", "FOOTBALL CHAMPIONSHIP 1", "Bảng xếp hạng"),
        ("fc2", "FOOTBALL CHAMPIONSHIP 2", "Bảng xếp hạng"),
    ):
        section_rows.append(append([f"{competition} · {subtitle}"]))
        header_rows.append(append(STANDING_HEADERS))
        try:
            table = game.league_phase1_table(tier)
        except Exception:
            table = []
        first_data_row = len(rows) + 1
        for rank, item in enumerate(table, 1):
            append([
                rank, item.get("team", ""), item.get("p", 0), item.get("w", 0),
                item.get("d", 0), item.get("l", 0), item.get("gf", 0),
                item.get("ga", 0), item.get("gd", 0), item.get("pts", 0),
                round(float(item.get("elo", 0) or 0)),
            ])
        table_ranges.append((first_data_row, len(rows)))
        append([])
        append([])

    section_rows.append(append(["MAJOR CUP · SƠ ĐỒ NHÁNH ĐẤU"]))
    image_rows["cup_bracket"] = append([])
    append([])

    section_rows.append(append(["CHAMPIONS LEAGUE · PLAY-IN"]))
    image_rows["c1_playin"] = append([])
    append([])

    section_rows.append(append(["CHAMPIONS LEAGUE · GROUP STATE"]))
    image_rows["c1_groups"] = append([])
    append([])

    section_rows.append(append(["CHAMPIONS LEAGUE · KNOCKOUT"]))
    image_rows["c1_knockout"] = append([])

    matches = [m for m in state.get("matches", []) if m.get("played", True)]
    return {
        "sheet_title": title,
        "season": season,
        "season_label": label,
        "synced_at": synced_at,
        "headers": STANDING_HEADERS,
        "rows": rows,
        "section_rows": section_rows,
        "header_rows": header_rows,
        "table_ranges": table_ranges,
        "image_rows": image_rows,
        "image_keys": list(REQUIRED_SCREENSHOT_KEYS),
        "match_count": len(matches),
        "record_count": sum(max(0, end - start + 1) for start, end in table_ranges),
    }


class GoogleSheetsApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GoogleSheetsSync:
    """Asynchronous, debounced and retrying Google Sheets API writer."""

    def __init__(self, config: dict[str, Any], app_dir: Path, data_dir: Path):
        self.config = dict(config or {})
        self.app_dir = Path(app_dir)
        self.data_dir = Path(data_dir)
        # Status is runtime telemetry, not career data; keep it in process memory only.
        self.status_path = None
        self.enabled = bool(self.config.get("enabled", False))
        self.spreadsheet_id = str(self.config.get("spreadsheet_id", "")).strip()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._latest_payload: dict[str, Any] | None = None
        self._worker: threading.Thread | None = None
        self._last_status: dict[str, Any] = self._read_status()

    def credential_path(self) -> Path:
        env_path = os.environ.get("GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE", "").strip()
        raw = env_path or str(self.config.get("service_account_file", "credentials/google_service_account.json"))
        path = Path(raw).expanduser()
        return path if path.is_absolute() else self.app_dir / path

    def _read_status(self) -> dict[str, Any]:
        return {"status": "not_synced", "message": "Chưa đồng bộ Google Sheets."}

    def _write_status(self, status: str, message: str, **extra: Any) -> dict[str, Any]:
        value = {"status": status, "message": message, "updated_at": _now_utc(), **extra}
        with self._lock:
            self._last_status = value
        return value

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_status)

    def configuration_problem(self, require_images: bool = False,
                              require_history: bool = False) -> str | None:
        """Validate only the services required by the requested sync operation."""
        if not self.enabled:
            return "Google Sheets sync đang tắt trong config.json."
        if not self.spreadsheet_id:
            return "Thiếu google_sheets.spreadsheet_id trong config.json."
        if require_images or require_history:
            if not str(self.config.get("apps_script_url", "")).strip():
                return "Thiếu google_sheets.apps_script_url. Hãy triển khai src/apps_script/Code.gs thành Web App."
            if not str(self.config.get("apps_script_token", "")).strip():
                return "Thiếu google_sheets.apps_script_token. Hãy đặt SUPER_LEAGUE_SYNC_TOKEN trong Script Properties và chép cùng token vào config.json."
        # History and screenshots use Apps Script directly, not the Sheets API service account.
        if require_history and not require_images:
            return None
        path = self.credential_path()
        if not path.is_file():
            return (f"Chưa có Service Account JSON tại {path}. Tạo Service Account, bật Google Sheets API, "
                    "rồi chia sẻ spreadsheet cho địa chỉ client_email trong JSON với quyền Editor.")
        return None

    def enqueue(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return self._write_status("disabled", "Google Sheets sync đang tắt trong config.json.")
        if not self.spreadsheet_id:
            return self._write_status("configuration_error", "Thiếu spreadsheet_id.")
        include_images = bool(payload.get("include_images", False))
        problem = self.configuration_problem(require_images=include_images)
        if problem:
            return self._write_status("setup_required", problem,
                                      spreadsheet_id=self.spreadsheet_id,
                                      credential_file=str(self.credential_path()))
        # Keep queue state and status transition under one lock. This prevents a fast
        # worker response from being overwritten by a stale "queued" status.
        with self._lock:
            self._latest_payload = copy.deepcopy(payload)
            self._idle.clear()
            status = self._write_status(
                "queued", "Đã đưa dữ liệu mùa giải vào hàng đợi đồng bộ.",
                spreadsheet_id=self.spreadsheet_id,
                season=payload.get("season"),
                sheet_title=payload.get("sheet_title"),
                match_count=payload.get("match_count"),
            )
            if self._worker is None or not self._worker.is_alive():
                self._stop.clear()
                self._worker = threading.Thread(target=self._worker_main,
                                                name="GoogleSheetsSync", daemon=True)
                self._worker.start()
            self._wake.set()
            return status

    def enqueue_history(self, seasons: list[dict[str, Any]]) -> dict[str, Any]:
        """Queue an archive-only sync through Apps Script; no screenshots/service account needed."""
        if not isinstance(seasons, list) or not seasons:
            return self._write_status("configuration_error", "Không có file JSON mùa giải nào để đồng bộ.")
        problem = self.configuration_problem(require_history=True)
        if problem:
            return self._write_status("setup_required", problem,
                                      spreadsheet_id=self.spreadsheet_id,
                                      history_count=len(seasons))
        payload = {"sync_history_only": True, "season_history": copy.deepcopy(seasons)}
        with self._lock:
            self._latest_payload = payload
            self._idle.clear()
            status = self._write_status(
                "queued", f"Đã đưa {len(seasons)} mùa giải cũ vào hàng đợi đồng bộ.",
                spreadsheet_id=self.spreadsheet_id,
                sync_type="season_history",
                history_count=len(seasons),
            )
            if self._worker is None or not self._worker.is_alive():
                self._stop.clear()
                self._worker = threading.Thread(target=self._worker_main,
                                                name="GoogleSheetsSync", daemon=True)
                self._worker.start()
            self._wake.set()
            return status

    def _worker_main(self) -> None:
        retry_delays = self.config.get("retry_delays_seconds", [5, 15, 60, 180, 300])
        retry_delays = [max(1.0, float(value)) for value in retry_delays] or [5.0]
        last_payload = None
        attempt = 0
        while not self._stop.is_set():
            wait_seconds = retry_delays[min(attempt, len(retry_delays) - 1)] if last_payload else None
            self._wake.wait(timeout=wait_seconds)
            self._wake.clear()
            if self._stop.is_set():
                break
            # Coalesce several quick saves into the newest full-season snapshot.
            debounce = max(0.0, min(2.0, float(self.config.get("debounce_seconds", 1.0))))
            if debounce and self._stop.wait(debounce):
                break
            with self._lock:
                if self._latest_payload is not None:
                    last_payload = copy.deepcopy(self._latest_payload)
                    self._latest_payload = None
                # The wake signal may correspond to the payload just captured
                # during debounce. Clear it under the same lock as enqueue so a
                # later enqueue cannot be accidentally swallowed.
                self._wake.clear()
            if last_payload is None:
                continue
            try:
                history_only = bool(last_payload.get("sync_history_only"))
                payload_title = last_payload.get("sheet_title")
                payload_season = last_payload.get("season")
                payload_match_count = last_payload.get("match_count")
                payload_record_count = last_payload.get("record_count")
                if history_only:
                    result = self.sync_season_history(last_payload.get("season_history") or [])
                else:
                    result = self.sync_payload(last_payload)
                last_payload = None
                attempt = 0
                with self._lock:
                    # Do not let a completed older request overwrite the queued
                    # status of a newer snapshot that arrived while it was running.
                    if self._latest_payload is None and not self._wake.is_set():
                        if history_only:
                            count = int(result.get("season_count", 0))
                            success_message = f"Đã đồng bộ lịch sử {count} mùa giải lên Google Sheets."
                        elif result.get("image_count", 0):
                            success_message = "Đã đồng bộ dữ liệu text và ảnh mùa giải lên Google Sheets."
                        else:
                            success_message = "Đã tự động đồng bộ dữ liệu text mùa giải lên Google Sheets."
                        if result.get("image_cleanup_pending"):
                            success_message += " Bộ ảnh mới đã được đăng; một số ảnh cũ chưa dọn được và sẽ được thử dọn ở lần đồng bộ sau."
                        self._write_status("synced", success_message,
                                           spreadsheet_id=self.spreadsheet_id,
                                           sync_type="season_history" if history_only else "season_overview",
                                           history_count=result.get("season_count", 0) if history_only else 0,
                                           seasons=result.get("seasons", []) if history_only else [],
                                           sheet_title=payload_title,
                                           season=payload_season,
                                           match_count=payload_match_count,
                                           record_count=payload_record_count,
                                           image_count=result.get("image_count", 0),
                                           image_sizes_bytes=result.get("image_sizes_bytes", {}),
                                           request_body_bytes=result.get("request_body_bytes", 0),
                                           image_cleanup_pending=bool(result.get("image_cleanup_pending", False)),
                                           spreadsheet_url=f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit",
                                           worksheet_title=result.get("worksheet_title"))
                        self._idle.set()
            except Exception as exc:  # Keep the local save authoritative; retry transient failures only.
                attempt += 1
                status_code = getattr(exc, "status_code", None)
                message = str(exc).lower()
                permanent_text = any(token in message for token in (
                    "token không hợp lệ", "không tìm thấy worksheet", "permission denied",
                    "insufficient permissions", "invalid_grant", "invalid credentials",
                    "missing", "thiếu ", "chưa cấu hình", "không có quyền",
                ))
                permanent_http = (status_code is not None and 400 <= int(status_code) < 500
                                  and int(status_code) not in (408, 429))
                # If another save arrived during the request, process the newest
                # snapshot rather than surfacing a stale error.
                with self._lock:
                    has_newer_payload = self._latest_payload is not None or self._wake.is_set()
                    if not has_newer_payload:
                        target_status = "error" if permanent_http or permanent_text else "retrying"
                        self._write_status(target_status, f"Google Sheets chưa đồng bộ được: {exc}",
                                           spreadsheet_id=self.spreadsheet_id,
                                           sheet_title=last_payload.get("sheet_title"),
                                           season=last_payload.get("season"),
                                           attempts=attempt)
                        if target_status == "error":
                            last_payload = None
                            self._idle.set()
                if has_newer_payload:
                    continue
                if permanent_http or permanent_text:
                    # Keep the worker alive and idle so a later manual retry can
                    # enqueue a fresh payload without racing a dying thread.
                    attempt = 0
                    last_payload = None
                    continue
                if attempt >= len(retry_delays):
                    attempt = len(retry_delays) - 1

    def wait_until_idle(self, timeout: float = 30.0) -> bool:
        return self._idle.wait(max(0.0, float(timeout)))

    def stop(self, timeout: float = 1.0) -> None:
        # Give an in-flight snapshot a chance to finish before closing. If the
        # network is unavailable, the local save remains authoritative and the
        # next application start will enqueue a fresh full snapshot.
        self.wait_until_idle(timeout=max(0.0, timeout))
        self._stop.set()
        self._wake.set()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=max(0.0, timeout))

    def _make_session(self):
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as exc:
            raise RuntimeError(
                "Thiếu thư viện Google Auth. Chạy: python -m pip install -r requirements-google-sheets.txt"
            ) from exc
        credentials = service_account.Credentials.from_service_account_file(
            str(self.credential_path()), scopes=SCOPES
        )
        return AuthorizedSession(credentials)

    @staticmethod
    def _response_json(response) -> dict[str, Any]:
        try:
            data = response.json()
        except Exception:
            data = {}
        if getattr(response, "status_code", 200) >= 400:
            message = ((data.get("error") or {}).get("message") if isinstance(data, dict) else None)
            raise GoogleSheetsApiError(message or f"Google Sheets API HTTP {response.status_code}",
                                       status_code=int(response.status_code))
        return data if isinstance(data, dict) else {}

    def _request(self, session, method: str, url: str, **kwargs):
        timeout = max(90, int(self.config.get("timeout_seconds", 90)))
        response = session.request(method, url, timeout=timeout, **kwargs)
        return self._response_json(response)

    def _sheet_metadata(self, session) -> list[dict[str, Any]]:
        data = self._request(session, "GET", f"{API_ROOT}/{self.spreadsheet_id}",
                             params={"fields": "sheets.properties(sheetId,title)"})
        return [item.get("properties", {}) for item in data.get("sheets", [])]

    def _read_export_identity(self, session, title: str) -> tuple[str, str]:
        """Read the ownership marker and season label stored in A1:B1."""
        range_name = f"'{title.replace(chr(39), chr(39) * 2)}'!A1:B1"
        encoded = quote(range_name, safe="!:$'")
        data = self._request(session, "GET", f"{API_ROOT}/{self.spreadsheet_id}/values/{encoded}")
        values = data.get("values", [])
        row = values[0] if values else []
        marker = str(row[0]) if len(row) > 0 else ""
        season_label = str(row[1]) if len(row) > 1 else ""
        return marker, season_label

    def _ensure_worksheet(self, session, payload: dict[str, Any]) -> tuple[int, str, bool]:
        """Resolve exactly one canonical worksheet for this season.

        The canonical title is authoritative: if it already exists, sync overwrites
        that worksheet even when it predates the export marker. This avoids creating
        a second tab such as "Mùa 2026-2027 - Super League" for the same season.
        Older app-owned suffixed tabs are adopted (renamed) when the canonical tab
        does not exist.
        """
        requested_title = str(payload["sheet_title"])
        expected_season_cell = f"MÙA {payload.get('season_label', '')}"
        sheets = self._sheet_metadata(session)
        by_title = {str(item.get("title", "")): item for item in sheets}

        # The season's canonical tab always wins, including tabs created by an
        # earlier version that did not write EXPORT_MARKER to cell A1.
        if requested_title in by_title:
            return int(by_title[requested_title].get("sheetId")), requested_title, False

        # Migrate a previous integration-owned suffixed tab back to the canonical
        # season title instead of creating another worksheet.
        matching_owned = []
        for item in sheets:
            old_title = str(item.get("title", ""))
            marker, season_cell = self._read_export_identity(session, old_title)
            if marker == EXPORT_MARKER and season_cell == expected_season_cell:
                matching_owned.append(item)

        if matching_owned:
            # Prefer the conventional suffix generated by older releases; otherwise
            # preserve the first matching app-owned worksheet and its existing images.
            chosen = next((item for item in matching_owned
                           if str(item.get("title", "")).endswith(" - Super League")), matching_owned[0])
            sheet_id = int(chosen.get("sheetId"))
            self._request(session, "POST", f"{API_ROOT}/{self.spreadsheet_id}:batchUpdate", json={"requests": [{
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "title": requested_title},
                    "fields": "title"
                }
            }]})
            return sheet_id, requested_title, False

        add = self._request(session, "POST", f"{API_ROOT}/{self.spreadsheet_id}:batchUpdate",
                            json={"requests": [{"addSheet": {"properties": {
                                "title": requested_title,
                                "gridProperties": {"rowCount": max(2000, len(payload.get("rows", [])) + 20),
                                                   "columnCount": len(HEADERS), "frozenRowCount": 1,
                                                   "hideGridlines": True}
                            }}}]})
        props = (((add.get("replies") or [{}])[0].get("addSheet") or {}).get("properties") or {})
        sheet_id = int(props.get("sheetId"))
        return sheet_id, requested_title, True

    def _cleanup_duplicate_season_worksheets(self, session, payload: dict[str, Any],
                                             keep_sheet_id: int) -> int:
        """Delete only confirmed app-owned duplicate tabs for the same season."""
        expected_season_cell = f"MÙA {payload.get('season_label', '')}"
        duplicates = []
        for item in self._sheet_metadata(session):
            try:
                sheet_id = int(item.get("sheetId"))
            except (TypeError, ValueError):
                continue
            if sheet_id == int(keep_sheet_id):
                continue
            title = str(item.get("title", ""))
            marker, season_cell = self._read_export_identity(session, title)
            if marker == EXPORT_MARKER and season_cell == expected_season_cell:
                duplicates.append(sheet_id)

        if not duplicates:
            return 0
        requests = [{"deleteSheet": {"sheetId": sheet_id}} for sheet_id in duplicates]
        self._request(session, "POST", f"{API_ROOT}/{self.spreadsheet_id}:batchUpdate",
                      json={"requests": requests})
        return len(duplicates)

    def _format_worksheet(self, session, sheet_id: int, payload: dict[str, Any], row_count: int) -> None:
        """Format the compact one-column-stack season overview and replace old merges."""
        requests = [
            {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2,
                                        "startColumnIndex": 0, "endColumnIndex": SHEET_COLUMNS},
                            "mergeType": "MERGE_ALL"}},
            {"mergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": 3,
                                        "startColumnIndex": 0, "endColumnIndex": SHEET_COLUMNS},
                            "mergeType": "MERGE_ALL"}},
        ]
        for row_number in payload.get("section_rows", []):
            requests.append({"mergeCells": {"range": {
                "sheetId": sheet_id, "startRowIndex": row_number - 1, "endRowIndex": row_number,
                "startColumnIndex": 0, "endColumnIndex": SHEET_COLUMNS,
            }, "mergeType": "MERGE_ALL"}})
        for row_number in payload.get("image_rows", {}).values():
            requests.append({"mergeCells": {"range": {
                "sheetId": sheet_id, "startRowIndex": row_number - 1, "endRowIndex": row_number,
                "startColumnIndex": 0, "endColumnIndex": SHEET_COLUMNS,
            }, "mergeType": "MERGE_ALL"}})
        requests.extend([
            {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                                       "startColumnIndex": 0, "endColumnIndex": SHEET_COLUMNS},
                             "cell": {"userEnteredFormat": {
                                 "backgroundColor": {"red": 0.05, "green": 0.10, "blue": 0.15},
                                 "textFormat": {"foregroundColor": {"red": 0.95, "green": 0.96, "blue": 0.98},
                                                "bold": True, "fontSize": 14},
                                 "verticalAlignment": "MIDDLE"}},
                             "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)"}},
            {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                                       "startColumnIndex": 0, "endColumnIndex": 1},
                             "cell": {"userEnteredFormat": {"textFormat": {
                                 "foregroundColor": {"red": 0.05, "green": 0.10, "blue": 0.15},
                                 "fontSize": 1}, "horizontalAlignment": "LEFT"}},
                             "fields": "userEnteredFormat.textFormat,userEnteredFormat.horizontalAlignment"}},
            {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2,
                                       "startColumnIndex": 0, "endColumnIndex": SHEET_COLUMNS},
                             "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.10, "green": 0.20, "blue": 0.17},
                                                              "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                                                             "bold": True, "fontSize": 16},
                                                              "verticalAlignment": "MIDDLE"}},
                             "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)"}},
            {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": 3,
                                       "startColumnIndex": 0, "endColumnIndex": SHEET_COLUMNS},
                             "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.09, "green": 0.14, "blue": 0.19},
                                                              "textFormat": {"foregroundColor": {"red": 0.75, "green": 0.80, "blue": 0.85},
                                                                             "fontSize": 10},
                                                              "wrapStrategy": "WRAP"}},
                             "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                        "startIndex": 0, "endIndex": 1},
                                            "properties": {"pixelSize": 58}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                        "startIndex": 1, "endIndex": 2},
                                            "properties": {"pixelSize": 260}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                        "startIndex": 2, "endIndex": SHEET_COLUMNS},
                                            "properties": {"pixelSize": 86}, "fields": "pixelSize"}},
            {"updateSheetProperties": {"properties": {"sheetId": sheet_id,
                                                         "gridProperties": {"frozenRowCount": 1, "hideGridlines": True}},
                                         "fields": "gridProperties.frozenRowCount,gridProperties.hideGridlines"}},
        ])
        for row_number in payload.get("section_rows", []):
            requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": row_number - 1,
                                                       "endRowIndex": row_number, "startColumnIndex": 0,
                                                       "endColumnIndex": SHEET_COLUMNS},
                             "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.12, "green": 0.25, "blue": 0.20},
                                                              "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                                                             "bold": True, "fontSize": 13},
                                                              "verticalAlignment": "MIDDLE"}},
                             "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)"}})
            requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
                                                                        "startIndex": row_number - 1, "endIndex": row_number},
                                                 "properties": {"pixelSize": 32}, "fields": "pixelSize"}})
        for row_number in payload.get("header_rows", []):
            requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": row_number - 1,
                                                       "endRowIndex": row_number, "startColumnIndex": 0,
                                                       "endColumnIndex": SHEET_COLUMNS},
                             "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.16, "green": 0.29, "blue": 0.24},
                                                              "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                                                             "bold": True},
                                                              "horizontalAlignment": "CENTER"}},
                             "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"}})
        for start_row, end_row in payload.get("table_ranges", []):
            if end_row >= start_row:
                requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": start_row - 1,
                                                           "endRowIndex": end_row, "startColumnIndex": 0,
                                                           "endColumnIndex": SHEET_COLUMNS},
                                 "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE"}},
                                 "fields": "userEnteredFormat.verticalAlignment"}})
        # Images are anchored over each merged row; reserve a readable area below each section.
        for row_number in payload.get("image_rows", {}).values():
            requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
                                                                        "startIndex": row_number - 1, "endIndex": row_number},
                                                 "properties": {"pixelSize": 520}, "fields": "pixelSize"}})
        self._request(session, "POST", f"{API_ROOT}/{self.spreadsheet_id}:batchUpdate", json={"requests": requests})

    def _apps_script_url(self) -> str:
        return str(self.config.get("apps_script_url", "")).strip()

    def sync_season_history(self, seasons: list[dict[str, Any]]) -> dict[str, Any]:
        """Send validated season archive records to the Apps Script history handler."""
        if not isinstance(seasons, list) or not seasons:
            raise RuntimeError("Không có dữ liệu lịch sử mùa giải để đồng bộ.")
        problem = self.configuration_problem(require_history=True)
        if problem:
            raise RuntimeError(problem)
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("Thiếu requests. Chạy: python -m pip install -r requirements-google-sheets.txt") from exc

        bridge_url = self._apps_script_url()
        body = {
            "token": str(self.config.get("apps_script_token", "")),
            "spreadsheet_id": self.spreadsheet_id,
            "mode": "season_history",
            "seasons": seasons,
        }
        request_body_bytes = len(_json(body).encode("utf-8"))
        timeout = max(90, int(self.config.get("timeout_seconds", 90)))
        try:
            response = requests.post(bridge_url, json=body, timeout=timeout, allow_redirects=True)
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Không gọi được Apps Script để đồng bộ lịch sử: {exc}. "
                f"Payload khoảng {request_body_bytes:,} byte; timeout={timeout}s."
            ) from exc
        try:
            result = response.json()
        except ValueError:
            result = {"ok": False, "error": (response.text or "Apps Script trả về phản hồi không phải JSON")[:700]}
        if response.status_code >= 400:
            raise GoogleSheetsApiError(str(result.get("error") or f"Apps Script HTTP {response.status_code}"),
                                       status_code=response.status_code)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Apps Script từ chối yêu cầu đồng bộ lịch sử."))
        returned = result.get("seasons") or []
        expected_count = len(seasons)
        actual_count = int(result.get("season_count", len(returned)) or 0)
        if actual_count != expected_count or len(returned) != expected_count:
            raise RuntimeError(
                f"Apps Script báo đã ghi {actual_count}/{expected_count} mùa; phản hồi không đầy đủ."
            )
        return {
            "season_count": actual_count,
            "seasons": returned,
            "request_body_bytes": request_body_bytes,
        }

    def _insert_screenshots(self, payload: dict[str, Any], worksheet_title: str) -> dict[str, Any]:
        """Send four bounded JPEG screenshots to the staged Apps Script image bridge."""
        image_paths = payload.get("screenshots") or {}
        if not image_paths:
            return {"image_count": 0, "skipped": True}
        missing = [key for key in REQUIRED_SCREENSHOT_KEYS if not image_paths.get(key)]
        if missing:
            raise RuntimeError("Thiếu screenshot view: " + ", ".join(missing))
        try:
            import base64
            import requests
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Thiếu requests/Pillow. Chạy pip install -r requirements-google-sheets.txt") from exc

        images = []
        for key in REQUIRED_SCREENSHOT_KEYS:
            path = Path(str(image_paths[key])).expanduser()
            if not path.is_file():
                raise RuntimeError(f"Không tìm thấy ảnh chụp {key}: {path}")
            image_row = int(payload.get("image_rows", {}).get(key, 0))
            if image_row < 1:
                raise RuntimeError(f"Thiếu vị trí worksheet cho ảnh {key}.")

            # Re-compress legacy cached screenshots too, not just new GUI captures.
            raw = compress_image_file(path)
            if len(raw) > MAX_SCREENSHOT_BYTES:
                raise RuntimeError(
                    f"Ảnh {key} vẫn vượt giới hạn sau khi nén: {len(raw):,} byte "
                    f"> {MAX_SCREENSHOT_BYTES:,} byte."
                )
            with Image.open(io.BytesIO(raw)) as image:
                source_width, source_height = image.size
            display_width = 1000
            display_height = max(220, round(source_height * display_width / max(1, source_width)))
            images.append({
                "key": key,
                "row": image_row,
                "filename": f"{path.stem}.jpg",
                "mime_type": "image/jpeg",
                "size_bytes": len(raw),
                "data_base64": base64.b64encode(raw).decode("ascii"),
                "display_width": display_width,
                "display_height": display_height,
                "description": f"Super League · {payload.get('season_label', '')} · {key}",
            })

        bridge_url = self._apps_script_url()
        if not bridge_url:
            raise RuntimeError("Chưa cấu hình google_sheets.apps_script_url trong config.json.")
        body = {
            "token": str(self.config.get("apps_script_token", "")),
            "spreadsheet_id": self.spreadsheet_id,
            "sheet_title": worksheet_title,
            "images": images,
        }
        # Four base64 images can make the JSON body about one third larger than the
        # original JPEGs. Record the exact request size so a failed sync is diagnosable.
        request_body_bytes = len(_json(body).encode("utf-8"))
        timeout = max(90, int(self.config.get("timeout_seconds", 90)))
        try:
            response = requests.post(bridge_url, json=body, timeout=timeout, allow_redirects=True)
        except requests.RequestException as exc:
            size_summary = ", ".join(
                f"{item['key']}={item['size_bytes']:,}B" for item in images
            )
            raise RuntimeError(
                f"Không gọi được Apps Script để chèn ảnh: {exc}. "
                f"Payload khoảng {request_body_bytes:,} byte; {size_summary}; timeout={timeout}s."
            ) from exc
        try:
            result = response.json()
        except ValueError:
            result = {"ok": False, "error": (response.text or "Apps Script trả về phản hồi không phải JSON")[:500]}
        if response.status_code >= 400:
            raise GoogleSheetsApiError(str(result.get("error") or f"Apps Script HTTP {response.status_code}"),
                                       status_code=response.status_code)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Apps Script từ chối yêu cầu chèn ảnh."))
        return {
            "image_count": int(result.get("image_count", len(images))),
            "image_sizes_bytes": {item["key"]: item["size_bytes"] for item in images},
            "request_body_bytes": request_body_bytes,
            "skipped": False,
            "cleanup_pending": bool(result.get("cleanup_pending", False)),
        }

    def sync_payload(self, payload: dict[str, Any], session=None) -> dict[str, Any]:
        """Write text data; insert screenshots only for an explicit manual sync."""
        if not self.spreadsheet_id:
            raise RuntimeError("Thiếu spreadsheet_id.")
        session = session or self._make_session()
        sheet_id, title, created = self._ensure_worksheet(session, payload)
        rows = payload.get("rows", [])
        if not rows:
            raise RuntimeError("Không có dữ liệu để đồng bộ.")
        # A previous upload leaves merged image rows. Unmerge before clearing/updating
        # values, otherwise Sheets may reject writes into the old merged cells.
        self._request(session, "POST", f"{API_ROOT}/{self.spreadsheet_id}:batchUpdate", json={"requests": [{
            "unmergeCells": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
                                        "endRowIndex": max(2000, len(rows) + 20),
                                        "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}}
        }]})
        last_col = _column_letter(len(HEADERS))
        clear_rows = max(2000, len(rows) + 20)
        clear_range = f"'{title.replace(chr(39), chr(39) * 2)}'!A1:{last_col}{clear_rows}"
        encoded_clear = quote(clear_range, safe="!:$'")
        self._request(session, "POST", f"{API_ROOT}/{self.spreadsheet_id}/values/{encoded_clear}:clear", json={})
        output_col = _column_letter(SHEET_COLUMNS)
        update_range = f"'{title.replace(chr(39), chr(39) * 2)}'!A1:{output_col}{len(rows)}"
        encoded_update = quote(update_range, safe="!:$'")
        self._request(session, "PUT", f"{API_ROOT}/{self.spreadsheet_id}/values/{encoded_update}",
                      params={"valueInputOption": "USER_ENTERED"},
                      json={"majorDimension": "ROWS", "values": rows})
        self._format_worksheet(session, sheet_id, payload, len(rows) + 2)

        # Automatic saves/startup sync text only. Existing floating images on the
        # worksheet are intentionally left untouched. A full image replacement is
        # performed only when the user explicitly presses the Google Sheets Sync button.
        image_result = {
            "image_count": 0, "image_sizes_bytes": {}, "request_body_bytes": 0,
            "skipped": True, "cleanup_pending": False,
        }
        duplicate_sheets_removed = 0
        if bool(payload.get("include_images", False)):
            image_result = self._insert_screenshots(payload, title)
            # Remove legacy duplicate tabs only after a complete manual image set lands.
            if image_result.get("image_count", 0) == len(REQUIRED_SCREENSHOT_KEYS):
                duplicate_sheets_removed = self._cleanup_duplicate_season_worksheets(
                    session, payload, keep_sheet_id=sheet_id
                )
        return {"sheet_id": sheet_id, "worksheet_title": title, "created": created,
                "rows_written": len(rows), "columns_written": SHEET_COLUMNS,
                "image_count": image_result.get("image_count", 0),
                "image_cleanup_pending": bool(image_result.get("cleanup_pending", False)),
                "image_sizes_bytes": image_result.get("image_sizes_bytes", {}),
                "request_body_bytes": image_result.get("request_body_bytes", 0),
                "duplicate_sheets_removed": duplicate_sheets_removed}


def _column_letter(number: int) -> str:
    result = ""
    number = int(number)
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result
