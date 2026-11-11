# -*- coding: utf-8 -*-
"""Quản lý giải đấu Super League: lịch, 3 hạng, Cup, C1, lưu lịch sử và cầu thủ."""
from __future__ import annotations
import csv, json, math, os, random, re, shutil, zlib, unicodedata, tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import cmp_to_key
from pathlib import Path

import sl_engine as eng
from sl_match import adapter as match_adapter
from sl_match import context as match_context
from sl_match import coach as match_coach
from sl_match import engine as match_engine
from sl_match import news as match_news
from sl_match.human_data import assign_manager_names, load_human_data, configure_human_data_database
from sl_google_sheets import GoogleSheetsSync, build_season_payload
from season_archive import compact_season_record, compact_match
from economy import (EventBus, TransferWindowService, EconomyRuntime, WageService, bootstrap_economy,
                     load_economy_config, position_group)
from sl_database import SuperLeagueDatabase
from economy.player_development_model import (calculate_season_development, estimate_overall_from_attributes,
                                               reconcile_overall_rating, choose_training_focus)
from economy import player_health

APP_DIR = Path(__file__).resolve().parents[1]
MEMORY_DIR = APP_DIR / "memory"


# Shared award settings are application configuration, separate from career data.
# Changing ballot rules never mutates the SQLite career database.
AWARD_SETTINGS_DEFAULTS = {
    "min_appearances": 21,
    "require_premier_champion": True,
    "require_c1_semifinal": True,
    "c1_champion_bonus": 5.0,
    "premier_champion_bonus": 4.0,
    "c1_runner_up_bonus": 4.0,
    "premier_runner_up_bonus": 3.0,
    "major_cup_champion_bonus": 3.0,
    "weight_rating": 42.0,
    "weight_contribution": 28.0,
    "weight_motm": 20.0,
    "weight_minutes": 10.0,
}


def load_award_settings_file(path=None):
    """Load award rules shared with the Award Recalculator; safely fall back to defaults."""
    settings = dict(AWARD_SETTINGS_DEFAULTS)
    settings_path = Path(path) if path else (APP_DIR / "award_settings.json")
    if not settings_path.is_file():
        return settings
    try:
        with settings_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("award_settings.json phải là JSON object")
        values = payload.get("settings", payload)
        if not isinstance(values, dict):
            raise ValueError("award_settings.json thiếu object settings")
        settings.update({key: values[key] for key in AWARD_SETTINGS_DEFAULTS if key in values})
        settings["min_appearances"] = int(settings["min_appearances"])
        if not 1 <= settings["min_appearances"] <= 80:
            raise ValueError("min_appearances phải nằm trong khoảng 1–80")
        for key in ("require_premier_champion", "require_c1_semifinal"):
            value = settings[key]
            if isinstance(value, str):
                settings[key] = value.strip().casefold() in ("1", "true", "yes", "on")
            else:
                settings[key] = bool(value)
        for key in ("c1_champion_bonus", "premier_champion_bonus", "c1_runner_up_bonus",
                    "premier_runner_up_bonus", "major_cup_champion_bonus", "weight_rating",
                    "weight_contribution", "weight_motm", "weight_minutes"):
            settings[key] = float(settings[key])
            if not 0 <= settings[key] <= 100:
                raise ValueError(f"{key} phải nằm trong khoảng 0–100")
        weight_total = sum(settings[key] for key in
                           ("weight_rating", "weight_contribution", "weight_motm", "weight_minutes"))
        if abs(weight_total - 100.0) > 0.01:
            raise ValueError(f"Tổng trọng số bình chọn phải bằng 100% (hiện tại {weight_total:.2f}%)")
        return settings
    except Exception as exc:
        print(f"Cảnh báo: không đọc được {settings_path} ({exc}); dùng thông số danh hiệu mặc định.")
        return dict(AWARD_SETTINGS_DEFAULTS)
POSITION_TEMPLATE = {"GK": 3, "CB": 3, "CM": 3, "RB": 2, "LB": 2, "CDM": 2,
                     "ST": 2, "CAM": 1, "RM": 1, "LM": 1, "RW": 1, "LW": 1, "CF": 1}
TIERS = ("premier", "fc1", "fc2")
TIER_LABELS = {"premier": "Premier League", "fc1": "Football Championship 1", "fc2": "Football Championship 2"}
TIER_KEY_BY_LABEL = {v: k for k, v in TIER_LABELS.items()}

NUMERIC_FIELDS = {
    "overall_rating", "skill_moves", "weak_foot", "height_cm", "weight_kg", "pace", "shooting", "passing",
    "dribbling", "defending", "physicality", "attacking_crossing", "attacking_finishing",
    "attacking_heading_accuracy", "attacking_short_passing", "attacking_volleys", "skill_dribbling",
    "skill_curve", "skill_fk_accuracy", "skill_long_passing", "skill_ball_control", "movement_acceleration",
    "movement_sprint_speed", "movement_agility", "movement_reactions", "movement_balance", "power_shot_power",
    "power_jumping", "power_stamina", "power_strength", "power_long_shots", "mentality_aggression",
    "mentality_interceptions", "mentality_positioning", "mentality_vision", "mentality_penalties",
    "mentality_composure", "defending_awareness", "defending_standing_tackle", "defending_sliding_tackle",
    "goalkeeping_diving", "goalkeeping_handling", "goalkeeping_kicking", "goalkeeping_positioning",
    "goalkeeping_reflexes"
}
ATTRIBUTE_FIELDS = sorted(NUMERIC_FIELDS - {"overall_rating", "skill_moves", "weak_foot", "height_cm", "weight_kg"})
PHYSICAL_FIELDS = ["pace", "movement_acceleration", "movement_sprint_speed", "power_stamina", "power_strength", "power_jumping"]
MENTAL_FIELDS = ["mentality_composure", "mentality_vision", "mentality_positioning", "defending_awareness", "movement_reactions"]


def _tupleize(value):
    if isinstance(value, list): return tuple(_tupleize(x) for x in value)
    return value


def _age_from_birthdate(birthdate, on_date=date(2026, 10, 29)):
    try:
        d, m, y = [int(x) for x in str(birthdate).split("-")]
        born = date(y, m, d)
        return round((on_date - born).days / 365.25, 1)
    except Exception:
        return 26.0


def _safe_date(raw):
    try: return date.fromisoformat(str(raw))
    except Exception: return date(2026, 8, 1)


_WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _weekday_number(value, key_name):
    key = str(value or "").strip().lower()
    if key not in _WEEKDAYS:
        raise ValueError(f"config.calendar.{key_name} phải là thứ trong tuần (mon…sun). Giá trị hiện tại: {value!r}")
    return _WEEKDAYS[key]


def _on_or_after(day, weekday):
    """Trả về ngày có weekday chỉ định, không sớm hơn day."""
    return day + timedelta(days=(weekday - day.weekday()) % 7)


def _date_in_week(anchor, week_offset, weekday):
    """Lấy ngày trong tuần lịch có mốc Premier (anchor) và offset tuần."""
    return anchor + timedelta(weeks=int(week_offset), days=int(weekday) - anchor.weekday())


def _slot_rest_group(slot):
    """Nhóm slot có tập CLB có thể tham dự.

    FC2/Premier/FC1 là các nhóm CLB rời nhau nên được xếp lần lượt T6/T7/CN.
    Cup và C1 có thể có CLB trùng với mọi hạng nên vẫn phải kiểm tra nghỉ chéo.
    """
    kind = str(slot.get("kind", ""))
    if kind == "league" and slot.get("tier") in TIERS:
        return f"tier:{slot['tier']}"
    if kind == "league_phase2" and slot.get("phase2_tier") in TIERS:
        return f"tier:{slot['phase2_tier']}"
    if kind.startswith("cup_"):
        return "competition:cup"
    if kind.startswith("c1_"):
        return "competition:c1"
    return "competition:unknown"


def validate_calendar_slots(slots, min_rest_days):
    """Validate slot IDs/dates and rest gaps for potentially shared clubs."""
    try:
        minimum = int(min_rest_days)
    except (TypeError, ValueError):
        raise ValueError("calendar.min_rest_days phải là số nguyên dương.")
    if minimum < 1:
        raise ValueError("calendar.min_rest_days phải >= 1.")
    parsed = []
    seen_ids = set()
    seen_dates = {}
    for slot in slots:
        slot_id = str(slot.get("id", ""))
        if not slot_id or slot_id in seen_ids:
            raise ValueError(f"Lịch có slot id rỗng hoặc trùng: {slot_id!r}")
        seen_ids.add(slot_id)
        try:
            day = date.fromisoformat(str(slot["date"]))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Slot {slot_id} có ngày không hợp lệ: {slot.get('date')!r}")
        if day in seen_dates:
            raise ValueError(f"Lịch bị trùng ngày {day.isoformat()}: {seen_dates[day]} và {slot.get('label', slot_id)} ({slot_id}).")
        seen_dates[day] = f"{slot.get('label', slot_id)} ({slot_id})"
        parsed.append((day, slot_id, str(slot.get("label", slot_id)), _slot_rest_group(slot)))
    parsed.sort()
    for index, (date_a, id_a, label_a, group_a) in enumerate(parsed):
        for date_b, id_b, label_b, group_b in parsed[index + 1:]:
            gap = (date_b - date_a).days
            if gap >= minimum:
                break
            # Chỉ bỏ qua khoảng nghỉ giữa hai hạng có CLB không giao nhau.
            if group_a.startswith("tier:") and group_b.startswith("tier:") and group_a != group_b:
                continue
            raise ValueError(
                f"Lịch vi phạm thời gian nghỉ tối thiểu {minimum} ngày: {label_a} ({date_a.isoformat()}) "
                f"→ {label_b} ({date_b.isoformat()}), cách nhau {gap} ngày. "
                "Hãy đổi calendar.*_week_offsets hoặc min_rest_days trong config.json."
            )
    return True


def _team_code(team):
    parts = re.findall(r"[A-Za-z0-9]+", team)
    if not parts: return "CLB"
    # Dùng nhiều ký tự đầu của tên để tránh trùng kiểu Apollo FC / Ares FC.
    code = (parts[0][:4] + "".join(p[0] for p in parts[1:]))[:6].upper()
    return code or "CLB"


def _normal_row(row):
    out = dict(row)
    for k in NUMERIC_FIELDS:
        try: out[k] = int(float(out.get(k) or 50))
        except (ValueError, TypeError): out[k] = 50
    if not out.get("common_name"):
        out["common_name"] = (str(out.get("first_name", "")) + " " + str(out.get("last_name", ""))).strip()
    return out


def _validate_source_players(rows, source_label="danh mục SQLite"):
    rows = [_normal_row(row) for row in rows]
    fields = list(dict.fromkeys(key for row in rows for key in row))
    if not fields:
        raise ValueError(f"{source_label} không có trường dữ liệu.")
    if "club" not in fields or "player_id" not in fields or "position" not in fields:
        raise ValueError(f"{source_label} cần có các trường player_id, club và position.")
    grouped = defaultdict(list)
    for player in rows:
        grouped[player["club"]].append(player)
    if len(rows) != 552 or len(grouped) != 24:
        raise ValueError(f"{source_label} hiện có {len(rows)} cầu thủ / {len(grouped)} CLB; yêu cầu 552 cầu thủ và 24 CLB.")
    errors = []
    for team, players in grouped.items():
        if len(players) != 23:
            errors.append(f"{team}: {len(players)} cầu thủ")
        positions = Counter(player["position"] for player in players)
        if dict(positions) != POSITION_TEMPLATE:
            errors.append(f"{team}: cơ cấu vị trí không khớp ({dict(positions)})")
    if errors:
        raise ValueError(f"{source_label} không đúng cấu trúc đội hình 23 người:\n" + "\n".join(errors[:8]))
    ids = [player["player_id"] for player in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{source_label} có player_id bị trùng.")
    return fields, rows, dict(grouped)

def load_source_database(database):
    """Load and validate the immutable source catalogue stored in SQLite."""
    rows = database.load_source_players()
    if not rows:
        raise ValueError("Database thiếu danh mục cầu thủ nguồn (source_players).")
    return _validate_source_players(rows, "Danh mục cầu thủ trong SQLite")


def average_top11(players):
    values = sorted((int(p["overall_rating"]) for p in players), reverse=True)[:11]
    return sum(values) / max(1, len(values))


def make_double_round_robin(teams, seed=0):
    """Tạo lịch 2 lượt, mỗi cặp gặp đúng hai lần và tránh 3 sân nhà/khách liên tiếp."""
    if len(teams) < 2 or len(teams) % 2:
        raise ValueError("Lịch vòng tròn yêu cầu số đội chẵn từ 2 trở lên.")
    n = len(teams)
    base = list(range(n))
    base_rounds = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = base[i], base[n - 1 - i]
            if (r + i) % 2: a, b = b, a
            pairs.append((teams[a], teams[b]))
        base_rounds.append(pairs)
        base = [base[0]] + [base[-1]] + base[1:-1]
    rng = random.Random(seed)

    def valid(rounds):
        venue = {t: [] for t in teams}
        for rd in rounds:
            for h, a in rd:
                venue[h].append("H"); venue[a].append("A")
        return all(not (seq[i] == seq[i+1] == seq[i+2]) for seq in venue.values() for i in range(len(seq)-2))

    count = n - 1
    for _ in range(12000):
        order = list(range(count)); rng.shuffle(order)
        first = []
        for idx in order:
            rd = list(base_rounds[idx])
            if rng.random() < .5: rd = [(a, h) for h, a in rd]
            first.append(rd)
        full = first + [[(a, h) for h, a in rd] for rd in reversed(first)]
        if valid(full): return full
    # Fallback deterministic: still ensures every pair is met twice with reversed venues.
    first = base_rounds
    return first + [[(a, h) for h, a in rd] for rd in reversed(first)]


class Game:
    SAVE_VERSION = 1

    def __init__(self, data_dir=None, config_path=None, fresh=False, seed=None, enable_google_sheets=True):
        self.data_dir = Path(data_dir) if data_dir else MEMORY_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(config_path) if config_path else APP_DIR / "config.json"
        self.config = self._load_config()
        self.economy_config, self.club_finance_overrides = load_economy_config(APP_DIR)
        self.economy_config["season_start_date"] = self.config.get("calendar", {}).get("start_date", "2026-08-01")
        self.economy_events = EventBus()
        self.transfer_windows = TransferWindowService(self.economy_config)
        self.award_settings = load_award_settings_file(APP_DIR / "award_settings.json")
        if seed is not None: self.config["seed"] = int(seed)
        self.seed = int(self.config.get("seed", 20261029))
        self.database_path = self.data_dir / "super_league.db"
        self.database = SuperLeagueDatabase(self.database_path)
        # All local simulator data lives in this one SQLite file.
        self.snapshot_dir = None
        self._history_migration_needed = False
        self._database_had_career = self.database.has_career()
        # Enforce the single-file contract on every launch, including upgrades
        # from folders that still contain legacy JSON/CSV/backup sidecars.
        self._clear_generated_outputs()

        # The shipped app reads its seed catalogue only from SQLite.
        source_rows = self.database.load_source_players()

        # Temporary test instances use the immutable catalogue from the packaged DB.
        if not source_rows and self.database_path.resolve() != (MEMORY_DIR / "super_league.db").resolve():
            packaged = SuperLeagueDatabase(MEMORY_DIR / "super_league.db")
            source_rows = packaged.load_source_players()
            if source_rows:
                self.database.set_source_players(source_rows)
            if self.database.load_reference_data("human_names") is None:
                human_ref = packaged.load_reference_data("human_names")
                if human_ref is not None:
                    self.database.set_reference_data("human_names", human_ref)
        if not source_rows:
            raise ValueError("Không tìm thấy danh mục cầu thủ trong memory/super_league.db. Database nền chưa được đóng gói đầy đủ.")
        self.csv_fields, self.source_rows, self.source_by_team = _validate_source_players(source_rows)

        # Reference names are also embedded in the same SQLite file.
        configure_human_data_database(self.database_path)
        self.human_data = load_human_data(self.database_path)
        self.first_names = list(self.human_data["first_names"])
        self.last_names = list(self.human_data["second_names"])
        self.source_players_by_pos = defaultdict(list)
        for player in self.source_rows:
            self.source_players_by_pos[player["position"]].append(player)

        if fresh:
            self.database.reset_career()

        self.google_sheets_sync = GoogleSheetsSync(self.config.get("google_sheets", {}), APP_DIR, self.data_dir)
        self.google_sheets_autostart = bool(enable_google_sheets)
        self.google_sheets_screenshot_provider = None
        self._initializing = True
        self.rng = random.Random(self.seed)
        self.s = None
        if not fresh and self._database_had_career:
            try:
                self._load()
                if self._history_migration_needed:
                    self.save()
            except Exception as exc:
                raise RuntimeError(f"Không thể nạp sự nghiệp từ {self.database_path}: {exc}") from exc
        else:
            self.new_game()
        economy_migrated = self._ensure_economy_state()
        self.economy_runtime = EconomyRuntime(self, APP_DIR)
        runtime_migrated = self.economy_runtime.attach()
        if economy_migrated or runtime_migrated or not self._database_had_career or self.database.projection_rebuild_required:
            self.save()
        self._initializing = False
        # GUI startup sync is deferred until SuperLeagueApp has built, refreshed,
        # and displayed the views used for the screenshot export.
        self.google_sheets_startup_sync_pending = bool(
            self.google_sheets_autostart
            and self.config.get("google_sheets", {}).get("sync_on_startup", True)
        )

    def _ensure_economy_state(self):
        """Idempotently add economy P1 data to fresh or legacy careers.

        The bootstrap only creates opening finance, initial wages, market values and
        contracts. It deliberately does not charge salaries or alter match results.
        """
        if not isinstance(self.s, dict):
            return False
        profile_groups = {}
        for team in self.s.get("rosters", {}):
            try:
                profile_groups[team] = self.academy_talent_profile(team).get("group", "standard")
            except Exception:
                profile_groups[team] = "standard"
        return bootstrap_economy(
            self.s,
            rosters=self.s.get("rosters", {}),
            players=self.s.get("players", {}),
            player_meta=self.s.get("player_meta", {}),
            team_profiles=profile_groups,
            team_tiers=self.s.get("team_tiers", {}),
            config=self.economy_config,
            overrides=self.club_finance_overrides,
            seed=self.seed,
            season=int(self.s.get("season", self.seed)),
        )

    def subscribe_economy_event(self, event, callback):
        """Register a future financial/transfer service on the simulation event bus."""
        return self.economy_events.subscribe(event, callback)

    def _clear_generated_outputs(self):
        """Remove legacy sidecar data so memory/ contains only super_league.db."""
        keep = {self.database_path.resolve()}
        if not self.data_dir.exists():
            return
        for child in self.data_dir.iterdir():
            try:
                if child.resolve() in keep:
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError(f"Không thể dọn dữ liệu phụ {child}: {exc}") from exc

    def _load_config(self):
        base = {}
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f: base = json.load(f)
        if not isinstance(base, dict):
            raise ValueError("config.json phải chứa một JSON object ở cấp cao nhất.")
        # Các mặc định bắt buộc để sửa config thiếu khoá vẫn có thể chạy.
        defaults = {"seed": 20261029, "match_engine": "enhanced", "teams": {"premier": 8, "fc1": 8, "fc2": 8, "promotions": 2},
            "cup": {"teams": 24, "legs": 1, "final_neutral": True, "home_rule": "lower_seed"},
            "c1": {"direct": 4, "playin": 8, "playin_groups": 2, "playin_qualify_per_group": 2, "main_group_legs": 1, "semis_legs": 2, "final_neutral": True},
            "points": {
                # League points reward final position, while a successful promotion
                # play-off receives the explicit points shown below.
                "premier": {"positions": [100, 96, 92, 88, 84, 80, 76, 72]},
                "fc1": {"positions": [76, 68, 62, 56, 50, 44, 38, 32], "promotion_playoff_winner": 74},
                "fc2": {"positions": [46, 24, 20, 16, 12, 8, 4, 2], "promotion_playoff_winner": 34},
                "cup": {"winner": 40, "final": 30, "sf": 22, "qf": 14, "r16": 8, "r1": 3},
                "history_weight": 0.0},
            "calendar": {"start_date": "2026-08-01", "league_day": "sat", "fc2_day": "fri", "fc1_day": "sun", "cup_day": "tue", "midweek_day": "wed", "min_rest_days": 3, "league_week_offsets": [0,1,2,3,5,6,7,8,10,11,12,13,15,17], "fc2_week_offsets": [0,2,4,5,7,8,9,10,12,13,14,15,17,19], "cup_week_offsets": [5,10,15,17,33], "c1_week_offsets": [1,3,6,11,16,20,37,39,41], "league_phase2_week_offsets": [30,31,32,33,34,35], "cup_final_after_premier_round": 3},
            "engine": {"base_xg":1.22,"gap_scale":28.0,"gap_cap":.72,"home_factor":1.055,"away_factor":.945,
                       "xg_min":.25,"xg_max":2.65,"et_xg_factor":.23,"shots_per_xg":8.2,"sot_ratio_min":.30,
                       "sot_ratio_max":.43,"possession_gap_weight":1.35,"possession_noise":2.6,"assist_probability":.76,
                       "shootout_home_probability":.76,"shootout_away_probability":.74,"form_effect":2.1,"fit_threshold":70,
                       "fit_penalty_per_point":.035,"effective_min":-3,"effective_max":2.5,"elo_k":18,"elo_home_advantage":45,
                       "elo_margin_bonus":.08,"elo_margin_cap":1.5},
            "players": {"squad_size": 23, "academy_size": 6, "soft_cap_from": 85, "potential_range": [68,94],
                        "retire_table_outfield": {"33":.03,"34":.12,"35":.30,"36":.55,"37":.80,"38":1.0},
                        "retire_table_gk": {"33":0,"34":.04,"35":.12,"36":.30,"37":.55,"38":.85,"40":1.0},
                        "youth_age": [16,20],
                        "academy_talent": {
                            "elite_teams": ["BLack Sam", "Black Hunter", "AssassinS FC", "Fury Fighter", "Knight Castle"],
                            "middle_teams": ["Yellow Thunder FC", "Poseidon FC", "Savior FC", "Silver Moon"],
                            "profiles": {
                                "elite": {"superstar_probability": .18, "potential_range": [90,99], "overall_range": [78,86]},
                                "middle": {"superstar_probability": .10, "potential_range": [87,98], "overall_range": [74,83]},
                                "standard": {"superstar_probability": .025, "potential_range": [85,96], "overall_range": [70,80]}}}}}
        def merge(a, b):
            for k, v in b.items():
                if isinstance(v, dict) and isinstance(a.get(k), dict): merge(a[k], v)
                else: a[k] = v
            return a
        config = merge(defaults, base)
        mode = str(config.get("match_engine", "enhanced")).lower()
        if mode not in ("enhanced", "legacy"):
            raise ValueError("config.match_engine phải là 'enhanced' hoặc 'legacy'.")
        config["match_engine"] = mode
        self._validate_config(config)
        return config

    @staticmethod
    def _validate_config(config):
        """Fail fast khi config nằm ngoài thể thức đang được code hỗ trợ.

        Bản này cố định 24 CLB theo đội hình CSV, 8-8-8 và thể thức Cup/C1
        đã chốt trong đặc tả. Các ngày, lịch tuần, seed, điểm và engine vẫn
        có thể thay đổi trong config.json.
        """
        try:
            teams = config["teams"]
            sizes = [int(teams[k]) for k in ("premier", "fc1", "fc2")]
            if sizes != [8, 8, 8]:
                raise ValueError("config.teams phải là 8-8-8 (24 CLB trong CSV hiện tại).")
            promotions = int(teams["promotions"])
            if promotions != 2:
                raise ValueError("config.teams.promotions hiện phải bằng 2 theo thể thức lên hạng đã chốt.")

            # Validate the C1 qualification scoring ladder so later-division
            # champions and successful play-off promotees cannot fall below
            # the required positions in the division above.
            points_cfg = config.get("points", {})
            ladders = {}
            for tier in TIERS:
                tier_cfg = points_cfg.get(tier, {})
                ladder = tier_cfg.get("positions")
                if not isinstance(ladder, list) or len(ladder) != 8:
                    raise ValueError(f"config.points.{tier}.positions phải có đúng 8 mức điểm.")
                values = [float(value) for value in ladder]
                if any(value < 0 for value in values) or any(a <= b for a, b in zip(values, values[1:])):
                    raise ValueError(f"config.points.{tier}.positions phải giảm dần và không âm.")
                ladders[tier] = values
            for upper, lower in (("premier", "fc1"), ("fc1", "fc2")):
                lower_cfg = points_cfg[lower]
                playoff_award = float(lower_cfg.get("promotion_playoff_winner", -1))
                champion_points = ladders[lower][0]
                upper_seventh = ladders[upper][6]
                if lower == "fc1":
                    # FC1 champion is intentionally calibrated to the same level as
                    # 7th place in Premier League (not above it).
                    if not math.isclose(champion_points, upper_seventh, rel_tol=0.0, abs_tol=1e-9):
                        raise ValueError("Điểm vô địch fc1 phải bằng hạng 7 premier theo thang Champions Points.")
                elif champion_points <= upper_seventh:
                    raise ValueError(f"Điểm vô địch {lower} phải cao hơn hạng 7 {upper}.")
                if playoff_award <= ladders[upper][7]:
                    raise ValueError(f"Điểm thắng play-off {lower} phải cao hơn hạng 8 {upper}.")
                if playoff_award >= ladders[lower][0]:
                    raise ValueError(f"Điểm thắng play-off {lower} phải thấp hơn điểm vô địch {lower}.")

            cup = config["cup"]
            if int(cup["teams"]) != 24 or int(cup["legs"]) != 1:
                raise ValueError("Thể thức hiện hỗ trợ Cup 24 CLB, mỗi vòng một lượt (cup.teams=24, cup.legs=1).")
            c1 = config["c1"]
            required_c1 = {"direct": 4, "playin": 8, "playin_groups": 2,
                           "playin_qualify_per_group": 2, "main_groups": 2,
                           "main_group_legs": 1, "advance_per_group": 2, "semis_legs": 2}
            for key, expected in required_c1.items():
                if int(c1.get(key, expected)) != expected:
                    raise ValueError(f"config.c1.{key} hiện phải bằng {expected} theo thể thức C1 đã chốt.")

            calendar = config["calendar"]
            date.fromisoformat(str(calendar["start_date"]))
            weekday_values = {
                "fc2_day": _weekday_number(calendar.get("fc2_day", "fri"), "fc2_day"),
                "league_day": _weekday_number(calendar.get("league_day", "sat"), "league_day"),
                "fc1_day": _weekday_number(calendar.get("fc1_day", "sun"), "fc1_day"),
                "cup_day": _weekday_number(calendar.get("cup_day", "tue"), "cup_day"),
                "midweek_day": _weekday_number(calendar.get("midweek_day", "wed"), "midweek_day"),
            }
            if len(set(weekday_values.values())) != len(weekday_values):
                raise ValueError("config.calendar: FC2, Premier, FC1, Cup và C1 phải nằm ở các thứ khác nhau.")
            min_rest = int(calendar.get("min_rest_days", 3))
            if min_rest < 1:
                raise ValueError("config.calendar.min_rest_days phải >= 1.")
            for key, expected_len in (("league_week_offsets", 14), ("fc2_week_offsets", 14), ("cup_week_offsets", 5), ("c1_week_offsets", 9), ("league_phase2_week_offsets", 6)):
                vals = calendar.get(key)
                if not isinstance(vals, list) or len(vals) != expected_len:
                    raise ValueError(f"config.calendar.{key} phải có đúng {expected_len} giá trị tuần.")
                try: nums = [int(v) for v in vals]
                except (TypeError, ValueError):
                    raise ValueError(f"config.calendar.{key} chỉ được chứa số nguyên không âm.")
                if any(v < 0 for v in nums) or any(a >= b for a, b in zip(nums, nums[1:])):
                    raise ValueError(f"config.calendar.{key} phải tăng dần, không âm và không trùng.")

            players = config["players"]
            if int(players.get("squad_size", 23)) != 23:
                raise ValueError("config.players.squad_size phải là 23 vì CSV có khuôn đội hình cố định.")
            lo, hi = players.get("potential_range", [68, 94])
            if float(lo) > float(hi):
                raise ValueError("config.players.potential_range: giá trị nhỏ nhất phải <= giá trị lớn nhất.")
            youth_age = players.get("youth_age", [16, 20])
            if (not isinstance(youth_age, (list, tuple)) or len(youth_age) != 2 or
                    int(youth_age[0]) < 16 or int(youth_age[1]) > 20 or
                    int(youth_age[0]) > int(youth_age[1])):
                raise ValueError("config.players.youth_age phải nằm trong khoảng 16–20 (ví dụ [16, 20]).")
            talent_cfg = players.get("academy_talent", {})
            if not isinstance(talent_cfg, dict):
                raise ValueError("config.players.academy_talent phải là object.")
            elite_teams = talent_cfg.get("elite_teams", [])
            middle_teams = talent_cfg.get("middle_teams", [])
            if not isinstance(elite_teams, list) or not isinstance(middle_teams, list):
                raise ValueError("config.players.academy_talent.elite_teams/middle_teams phải là danh sách.")
            norm = lambda value: re.sub(r"[^a-z0-9]", "", str(value).casefold())
            elite_keys = {norm(name) for name in elite_teams}
            middle_keys = {norm(name) for name in middle_teams}
            if elite_keys.intersection(middle_keys):
                raise ValueError("Một CLB không thể đồng thời thuộc nhóm elite và middle trong academy_talent.")
            profiles = talent_cfg.get("profiles", {})
            if not isinstance(profiles, dict):
                raise ValueError("config.players.academy_talent.profiles phải là object.")
            for group in ("elite", "middle", "standard"):
                profile = profiles.get(group)
                if not isinstance(profile, dict):
                    raise ValueError(f"Thiếu config.players.academy_talent.profiles.{group}.")
                probability = float(profile.get("superstar_probability", -1))
                if not 0.0 <= probability <= 1.0:
                    raise ValueError(f"academy_talent.{group}.superstar_probability phải nằm trong [0,1].")
                for range_key in ("potential_range", "overall_range"):
                    value_range = profile.get(range_key)
                    if (not isinstance(value_range, (list, tuple)) or len(value_range) != 2 or
                            int(value_range[0]) < 60 or int(value_range[1]) > 99 or
                            int(value_range[0]) > int(value_range[1])):
                        raise ValueError(f"academy_talent.{group}.{range_key} phải là khoảng hợp lệ trong [60,99].")
                if int(profile["potential_range"][0]) < int(profile["overall_range"][1]):
                    raise ValueError(f"academy_talent.{group}: Potential tối thiểu phải lớn hơn OVR tối đa.")
            engine_cfg = config["engine"]
            if float(engine_cfg.get("base_xg", 0)) <= 0 or float(engine_cfg.get("gap_scale", 0)) <= 0:
                raise ValueError("config.engine.base_xg và gap_scale phải lớn hơn 0.")
            if float(engine_cfg.get("xg_min", 0)) <= 0 or float(engine_cfg.get("xg_min", 0)) > float(engine_cfg.get("xg_max", 0)):
                raise ValueError("config.engine cần thỏa 0 < xg_min <= xg_max.")
        except KeyError as exc:
            raise ValueError(f"Thiếu cấu hình bắt buộc: {exc}.") from exc
        except (TypeError, ValueError) as exc:
            if str(exc).startswith("config.") or "config" in str(exc): raise
            raise ValueError(f"config.json không hợp lệ: {exc}") from exc

    def _load(self):
        """Load the sole career snapshot from SQLite."""
        blob = self.database.load_career()
        if blob is None:
            raise ValueError("Database chưa có career_state.")
        if blob.get("version") != self.SAVE_VERSION:
            raise ValueError("Phiên bản career trong SQLite không tương thích.")
        self.s = blob["state"]
        # Backfill partial season-stat rows from older/partially migrated saves.
        # Some long-running careers can contain metadata-only rows (e.g. injury_days)
        # without the core scoring fields; season finalization must treat those as 0.
        if isinstance(self.s.get("season_stats"), dict):
            stat_defaults = self._empty_stat()
            for player_id, stat_row in list(self.s["season_stats"].items()):
                if not isinstance(stat_row, dict):
                    stat_row = {}
                    self.s["season_stats"][player_id] = stat_row
                for stat_key, default_value in stat_defaults.items():
                    stat_row.setdefault(stat_key, default_value)
        self._ensure_match_state()
        if blob.get("rng_state"):
            self.rng.setstate(_tupleize(blob["rng_state"]))
        simulation_rng = self.rng
        migration_seed = (self.seed ^ (int(self.s.get("season", 2026)) * 1_000_003) ^
                          (len(self.s.get("academy_alumni", [])) * 97_409)) & 0xFFFFFFFF
        self.rng = random.Random(migration_seed)
        try:
            self._maintain_academy_age_range()
        finally:
            self.rng = simulation_rng
        self._migrate_league_phase2_schedule()
        self._rebuild_last_match_dates()
        self._check_state()
        self._load_season_history()
        awards_changed = self._backfill_history_awards()
        if awards_changed:
            self.database.upsert_season_archives(self.s.get("history", []) or [])
        if self.s.get("season_done") and self.s.get("history"):
            latest = self.s["history"][-1]
            if int(latest.get("season", -1) or -1) == int(self.s.get("season", -2) or -2):
                latest_awards = latest.get("season_awards")
                if latest_awards:
                    self.s["season_awards"] = latest_awards
        self._refresh_completed_competition_awards(announce=False)
        self._sync_match_news()
        self._prune_news_history()
        # Persist the pruning immediately on startup; old news must not survive
        # in career_state/news_items until the next manual or matchday save.
        self.database.sync_news_window(self.s)

    def _load_season_history(self):
        """Load completed seasons exclusively from the SQLite season_archives table."""
        legacy = list(self.s.get("history", []) or [])
        archived = self.database.load_season_archives()
        migrated = False
        if not archived and legacy:
            # One-time compatibility for a pre-single-file career imported into DB.
            compacted = []
            for record in legacy:
                if not isinstance(record, dict):
                    continue
                try:
                    int(record.get("season"))
                except (TypeError, ValueError):
                    continue
                eligibility = self._season_award_eligibility(record.get("matches") or [])
                compacted.append(compact_season_record(record, self.s, eligibility))
            if compacted:
                self.database.upsert_season_archives(compacted)
                archived = compacted
                migrated = True
        self.s["history"] = archived
        self.s["history_archive_mode"] = "database"
        self._history_migration_needed = migrated

    def _rebuild_last_match_dates(self):
        last = {}
        for match in sorted(self.s.get("matches", []), key=lambda x: (str(x.get("date", "")), int(x.get("id", 0)))):
            dt = str(match.get("date", ""))
            if not dt: continue
            for team in (match.get("home"), match.get("away")):
                if team: last[team] = dt
        self.s["last_match_date"] = last

    def save(self):
        """Persist the complete career and season history to one SQLite file."""
        self._ensure_match_state()
        self._prune_news_history()
        disk_state = dict(self.s)
        disk_state["history"] = []
        disk_state["history_archive_mode"] = "database"
        blob = {"version": self.SAVE_VERSION, "state": disk_state, "rng_state": self.rng.getstate()}
        self.database.save_career(blob, source_players=None, season_archives=self.s.get("history", []) or [])
        sheets_config = self.config.get("google_sheets", {})
        if (not getattr(self, "_initializing", False)
                and self.google_sheets_autostart
                and sheets_config.get("sync_after_each_save", True)):
            self._queue_google_sheets_sync(reason="career_saved")

    def _queue_google_sheets_sync(self, reason="match_saved"):
        """Queue the latest complete season snapshot only for the real app data folder."""
        if not getattr(self, "google_sheets_sync", None):
            return {"status": "disabled", "message": "Google Sheets sync chưa được khởi tạo."}
        if reason != "manual_sync" and not getattr(self, "google_sheets_autostart", True):
            return {"status": "disabled", "message": "Đồng bộ tự động đang tắt cho phiên chạy này."}
        # Temporary directories used by tests, benchmarks and previews never contact Google.
        try:
            if self.data_dir.resolve() != (APP_DIR / "memory").resolve():
                return {"status": "test_mode", "message": "Bỏ qua Google Sheets trong thư mục kiểm thử."}
        except OSError:
            return {"status": "test_mode", "message": "Bỏ qua Google Sheets trong thư mục kiểm thử."}
        if not self.config.get("google_sheets", {}).get("enabled", False):
            return self.google_sheets_sync.status()
        include_images = (reason == "manual_sync")
        problem = self.google_sheets_sync.configuration_problem(require_images=include_images)
        if problem:
            return self.google_sheets_sync._write_status("setup_required", problem,
                                                          spreadsheet_id=self.google_sheets_sync.spreadsheet_id,
                                                          credential_file=str(self.google_sheets_sync.credential_path()))
        try:
            payload = build_season_payload(self)
            payload["sync_reason"] = reason
            payload["include_images"] = include_images

            # Images are deliberately excluded from automatic save/startup syncs.
            # Only an explicit manual Sync action captures and uploads screenshots.
            if include_images:
                season = int(self.s.get("season", 0) or 0)
                label = self.season_label()
                provider = self.google_sheets_screenshot_provider
                if callable(provider):
                    screenshots = provider(season, label)
                else:
                    # CLI --sheets-sync may reuse screenshots captured by the last GUI session.
                    capture_dir = Path(tempfile.gettempdir()) / "SuperLeague_google_sheets_captures"
                    screenshots = {
                        key: str(capture_dir / f"season_{season}_{key}.jpg")
                        for key in ("cup_bracket", "c1_playin", "c1_groups", "c1_knockout")
                    }
                missing = [key for key, path in screenshots.items() if not Path(path).is_file()]
                if missing or len(screenshots) != 4:
                    raise RuntimeError(
                        "Chưa có đủ 4 ảnh chụp view Cup/C1. Hãy mở giao diện Super League, "
                        "để cửa sổ hiện trên màn hình rồi bấm Đồng bộ Google Sheets. "
                        "Ảnh cần có: Major Cup, C1 Play-in, C1 Group State, C1 Knockout."
                    )
                payload["screenshots"] = dict(screenshots)

            return self.google_sheets_sync.enqueue(payload)
        except Exception as exc:
            try:
                return self.google_sheets_sync._write_status("snapshot_error",
                    f"Không tạo được dữ liệu đồng bộ: {exc}", season=self.s.get("season"))
            except Exception:
                return {"status": "snapshot_error", "message": str(exc)}

    def build_season_history_sync_payload(self):
        """Load all completed seasons directly from the canonical database."""
        records = self.database.load_season_archives()
        if not records:
            raise ValueError("Database chưa có mùa giải hoàn tất để đồng bộ.")
        payload = []
        for record in records:
            season = int(record["season"])
            payload.append({
                "file_name": f"{season}-{season + 1}.json",
                "sheet_title": f"Mùa {season}-{season + 1}",
                "season": record,
            })
        return payload

    def sync_season_history_to_google_sheets(self, wait=False, timeout=90):
        """Manually sync all archived seasons without the screenshot capture workflow."""
        if not getattr(self, "google_sheets_sync", None):
            return {"status": "disabled", "message": "Google Sheets sync chưa được khởi tạo."}
        try:
            if self.data_dir.resolve() != (APP_DIR / "memory").resolve():
                return {"status": "test_mode", "message": "Bỏ qua Google Sheets trong thư mục kiểm thử."}
        except OSError:
            return {"status": "test_mode", "message": "Bỏ qua Google Sheets trong thư mục kiểm thử."}
        try:
            if not self.config.get("google_sheets", {}).get("enabled", False):
                return self.google_sheets_sync._write_status(
                    "disabled", "Google Sheets sync đang tắt trong config.json.")
            problem = self.google_sheets_sync.configuration_problem(require_history=True)
            if problem:
                return self.google_sheets_sync._write_status("setup_required", problem,
                    spreadsheet_id=self.google_sheets_sync.spreadsheet_id)
            seasons = self.build_season_history_sync_payload()
            result = self.google_sheets_sync.enqueue_history(seasons)
            if wait and result.get("status") not in ("setup_required", "configuration_error", "disabled", "test_mode"):
                self.google_sheets_sync.wait_until_idle(timeout)
                return self.google_sheets_sync.status()
            return result
        except Exception as exc:
            return self.google_sheets_sync._write_status(
                "snapshot_error", f"Không đọc được lịch sử mùa giải: {exc}",
                database_path=str(self.database_path))

    def sync_text_to_google_sheets(self, wait=False, timeout=45):
        """Queue a text-only sync. Used by startup and automatic local saves."""
        result = self._queue_google_sheets_sync(reason="startup_sync")
        if wait and result.get("status") not in ("setup_required", "configuration_error", "disabled", "test_mode"):
            self.google_sheets_sync.wait_until_idle(timeout)
            return self.google_sheets_sync.status()
        return result

    def sync_to_google_sheets(self, wait=False, timeout=45):
        """Manually request a full text + image sync."""
        result = self._queue_google_sheets_sync(reason="manual_sync")
        if wait and result.get("status") not in ("setup_required", "configuration_error", "disabled", "test_mode"):
            self.google_sheets_sync.wait_until_idle(timeout)
            return self.google_sheets_sync.status()
        return result

    def google_sheets_status(self):
        if not getattr(self, "google_sheets_sync", None):
            return {"status": "disabled", "message": "Google Sheets sync chưa được khởi tạo."}
        try:
            if self.data_dir.resolve() != (APP_DIR / "memory").resolve():
                return {"status": "test_mode", "message": "Đồng bộ Google Sheets bị vô hiệu hóa trong thư mục kiểm thử."}
        except OSError:
            return {"status": "test_mode", "message": "Đồng bộ Google Sheets bị vô hiệu hóa trong thư mục kiểm thử."}
        status = self.google_sheets_sync.status()
        if status.get("status") == "not_synced":
            problem = self.google_sheets_sync.configuration_problem()
            if problem:
                return {"status": "setup_required", "message": problem,
                        "spreadsheet_id": self.google_sheets_sync.spreadsheet_id,
                        "credential_file": str(self.google_sheets_sync.credential_path())}
        return status

    def close_google_sheets_sync(self, timeout=2.0):
        if getattr(self, "google_sheets_sync", None):
            self.google_sheets_sync.stop(timeout=timeout)

    def _check_state(self):
        if set(self.s.get("rosters", {})) != set(self.source_by_team):
            raise ValueError("Save không khớp danh sách 24 CLB trong CSV.")
        seen_players = set()
        for team, ids in self.s["rosters"].items():
            if not 23 <= len(ids) <= 30:
                raise ValueError(f"Save bị lỗi: {team} có {len(ids)} người; đội hình phải từ 23 đến 30.")
            if len(set(ids)) != len(ids): raise ValueError(f"Save bị lỗi: {team} có player_id lặp trong đội hình.")
            duplicate = seen_players.intersection(ids)
            if duplicate: raise ValueError(f"Save bị lỗi: player_id xuất hiện ở nhiều CLB: {sorted(duplicate)[:3]}.")
            seen_players.update(ids)
            missing = [pid for pid in ids if pid not in self.s.get("players", {})]
            if missing: raise ValueError(f"Save bị lỗi: {team} thiếu dữ liệu cầu thủ {missing[:3]}.")
            groups = Counter(position_group(self.s["players"][pid].get("position", "")) for pid in ids)
            minimums = {"GK": 3, "DEF": 7, "MID": 7, "FWD": 4}
            shortages = [f"{group}={groups.get(group, 0)}/{needed}" for group, needed in minimums.items()
                         if groups.get(group, 0) < needed]
            if shortages:
                raise ValueError(f"Save bị lỗi: {team} thiếu vị trí: {', '.join(shortages)}.")
        if set(self.s.get("team_tiers", {})) != set(self.source_by_team):
            raise ValueError("Save lỗi: danh sách phân hạng không khớp 24 CLB.")
        tier_counts = Counter(self.s["team_tiers"].values())
        if tier_counts != Counter({"premier": 8, "fc1": 8, "fc2": 8}):
            raise ValueError(f"Save lỗi: phân bổ hạng không phải 8-8-8 ({dict(tier_counts)}).")

    def _empty_league_phase2(self):
        return {
            "initialized": False,
            "premier": {"champion_teams": [], "challenge_teams": [], "champion_rounds": [],
                        "challenge_rounds": [], "match_ids": {"champion": [[] for _ in range(6)],
                                                                  "challenge": [[] for _ in range(6)]},
                        "phase1_table": []},
            "promotion_playoffs": {tier: {"teams": {}, "semi_match": None, "final_match": None, "winner": None}
                                   for tier in ("fc1", "fc2")},
            "relegation_playoff": {"teams": {}, "leg1_match": None, "leg2_match": None,
                                   "winner": None, "loser": None},
        }

    def _shift_pending_calendar_after_played_matches(self):
        """Move the pending part of an upgraded career beyond its latest result.

        Early C1 Play-in dates are intentionally moved into the first half of the
        season. A legacy save can already contain later match results, so pending
        slots are shifted by whole weeks instead of being placed in the past.
        """
        slots = self.s.get("slots", [])
        matches_by_id = {int(match.get("id", -1)): match for match in self.s.get("matches", [])}
        latest_played = max((_safe_date(match.get("date")) for match in self.s.get("matches", [])
                             if match.get("played", True) and match.get("date")), default=None)
        if latest_played is None:
            return
        fixed = set()
        pending = []
        for slot in slots:
            has_results = bool(slot.get("played") or slot.get("fixture_result_ids"))
            linked_results = any(int(mid) in matches_by_id for mid in (slot.get("match_ids") or []))
            if has_results or linked_results:
                fixed.add(str(slot.get("id")))
            else:
                pending.append(slot)
        if not pending:
            return
        earliest = min(_safe_date(slot.get("date")) for slot in pending)
        minimum_target = latest_played + timedelta(days=int(self.config["calendar"].get("min_rest_days", 3)))
        if earliest < minimum_target:
            weeks = ((minimum_target - earliest).days + 6) // 7
            shift = timedelta(weeks=weeks)
            for slot in pending:
                slot["date"] = (_safe_date(slot.get("date")) + shift).isoformat()

        # Re-anchor an unplayed Major Cup Final to the actual/migrated Premier
        # Phase 2 Round 3 date so it remains exactly three days afterward.
        p2_round = int(self.config["calendar"].get("cup_final_after_premier_round", 3))
        premier_slot = next((slot for slot in slots if slot.get("kind") == "league_phase2"
                             and slot.get("phase2_tier") == "premier"
                             and int(slot.get("round", 0)) == p2_round), None)
        cup_final = next((slot for slot in slots if slot.get("kind") == "cup_final"), None)
        if premier_slot and cup_final and str(cup_final.get("id")) not in fixed:
            final_date = _safe_date(premier_slot.get("date")) + timedelta(days=3)
            if latest_played:
                minimum_target = latest_played + timedelta(days=int(self.config["calendar"].get("min_rest_days", 3)))
                while final_date < minimum_target:
                    final_date += timedelta(days=7)
            cup_weekday = _weekday_number(self.config["calendar"].get("cup_day", "tue"), "cup_day")
            while final_date.weekday() != cup_weekday:
                final_date += timedelta(days=1)
            cup_final["date"] = final_date.isoformat()
            cup_final["label"] = "Major Cup - Chung kết"

    def _migrate_v3_calendar_to_v4(self):
        """Retiming migration for v3 careers.

        Fresh seasons receive the new intended chronology. For an in-progress
        career, already-played fixtures retain their original dates/results; all
        pending slots are moved forward together when the new early C1 Play-in
        dates would otherwise fall behind completed matches.
        """
        calendar_cfg = self.config["calendar"]
        min_rest = int(calendar_cfg.get("min_rest_days", 3))
        premier_wd = _weekday_number(calendar_cfg.get("league_day", "sat"), "league_day")
        fc2_wd = _weekday_number(calendar_cfg.get("fc2_day", "fri"), "fc2_day")
        fc1_wd = _weekday_number(calendar_cfg.get("fc1_day", "sun"), "fc1_day")
        cup_wd = _weekday_number(calendar_cfg.get("cup_day", "tue"), "cup_day")
        c1_wd = _weekday_number(calendar_cfg.get("midweek_day", "wed"), "midweek_day")
        anchor = _safe_date(self.s.get("season_start_date") or calendar_cfg.get("start_date"))
        anchor = _on_or_after(anchor, premier_wd)

        league_offsets = calendar_cfg.get("league_week_offsets", [0,1,2,3,5,6,7,8,10,11,12,13,15,17])
        fc2_offsets = calendar_cfg.get("fc2_week_offsets", [0,2,4,5,7,8,9,10,12,13,14,15,17,19])
        cup_offsets = calendar_cfg.get("cup_week_offsets", [5,10,15,17,33])
        c1_offsets = calendar_cfg.get("c1_week_offsets", [1,3,6,11,16,20,37,39,41])
        phase2_offsets = calendar_cfg.get("league_phase2_week_offsets", [30,31,32,33,34,35])
        tier_weekdays = {"fc2": fc2_wd, "premier": premier_wd, "fc1": fc1_wd}
        matches_by_id = {int(m.get("id", -1)): m for m in self.s.get("matches", [])}
        slots = list(self.s.get("slots", []))
        fixed_ids = set()
        latest_played = max((_safe_date(m.get("date")) for m in self.s.get("matches", [])
                             if m.get("played", True) and m.get("date")), default=None)

        def target_date(slot):
            kind = str(slot.get("kind", ""))
            round_no = int(slot.get("round", 1))
            if kind == "league":
                tier = slot.get("tier", "premier")
                offsets = fc2_offsets if tier == "fc2" else league_offsets
                return _date_in_week(anchor, offsets[round_no - 1], tier_weekdays[tier])
            if kind == "league_phase2":
                tier = slot.get("phase2_tier", "premier")
                return _date_in_week(anchor, phase2_offsets[round_no - 1], tier_weekdays[tier])
            if kind.startswith("cup_"):
                if kind == "cup_final":
                    p2_round = int(calendar_cfg.get("cup_final_after_premier_round", 3))
                    p2_date = _date_in_week(anchor, phase2_offsets[p2_round - 1], premier_wd)
                    return p2_date + timedelta(days=3)
                return _date_in_week(anchor, cup_offsets[round_no - 1], cup_wd)
            if kind.startswith("c1_"):
                return _date_in_week(anchor, c1_offsets[round_no - 1], c1_wd)
            return _safe_date(slot.get("date"))

        targets = {}
        for slot in slots:
            result_ids = slot.get("fixture_result_ids") or {}
            linked_played = any(int(mid) in matches_by_id for mid in (slot.get("match_ids") or []))
            is_fixed = bool(slot.get("played") or result_ids or linked_played)
            if is_fixed:
                fixed_ids.add(str(slot.get("id")))
            else:
                targets[str(slot.get("id"))] = target_date(slot)

        for slot in slots:
            slot_id = str(slot.get("id"))
            if slot_id in targets:
                slot["date"] = targets[slot_id].isoformat()

        # Major Cup Final is always three days after Premier Phase 2 Round 3.
        p2r3 = next((slot for slot in slots if slot.get("kind") == "league_phase2"
                     and slot.get("phase2_tier") == "premier" and int(slot.get("round", 0)) == 3), None)
        cup_final = next((slot for slot in slots if slot.get("kind") == "cup_final"), None)
        if p2r3 and cup_final and str(cup_final.get("id")) not in fixed_ids:
            final_date = _safe_date(p2r3.get("date")) + timedelta(days=3)
            if latest_played:
                minimum_target = latest_played + timedelta(days=min_rest)
                while final_date < minimum_target:
                    final_date += timedelta(days=7)
            # Keep the Cup weekday even when a progressed legacy career needs a
            # forward-only migration after historic results.
            while final_date.weekday() != cup_wd:
                final_date += timedelta(days=1)
            cup_final["date"] = final_date.isoformat()
            cup_final["label"] = "Major Cup - Chung kết"

        self.s["slots"] = slots
        self._shift_pending_calendar_after_played_matches()
        self.s["slots"] = sorted(slots, key=lambda item: (str(item.get("date", "")), str(item.get("id", ""))))
        self.s["calendar_schedule_version"] = 4
        validate_calendar_slots(self.s["slots"], min_rest)
        self._update_calendar_validation_metadata()

    def _migrate_v4_calendar_to_v5(self):
        """Add FC1 survival legs and interleave C1 rounds without rewriting played results."""
        if self.s.get("season_done"):
            self.s["calendar_schedule_version"] = 5
            return
        cfg = self.config["calendar"]
        anchor = _safe_date(self.s.get("season_start_date") or cfg.get("start_date"))
        c1_wd = _weekday_number(cfg.get("midweek_day", "wed"), "midweek_day")
        fc1_wd = _weekday_number(cfg.get("fc1_day", "sun"), "fc1_day")
        c1_offsets = cfg.get("c1_week_offsets", [1, 3, 6, 11, 16, 20, 37, 39, 41])
        slots = list(self.s.get("slots", []))
        by_id = {str(slot.get("id")): slot for slot in slots}
        for index, week in enumerate(c1_offsets, 1):
            slot = by_id.get(f"C1{index}")
            if slot and not slot.get("played") and not slot.get("fixture_result_ids"):
                slot["date"] = _date_in_week(anchor, week, c1_wd).isoformat()
        phase2_offsets = cfg.get("league_phase2_week_offsets", [30, 31, 32, 33, 34, 35])
        for round_no, week_index, leg_label in ((4, 3, "Lượt đi"), (6, 5, "Lượt về")):
            slot_id = f"P2FC1R{round_no:02d}"
            if slot_id in by_id:
                continue
            slots.append({
                "id": slot_id,
                "date": _date_in_week(anchor, phase2_offsets[week_index], fc1_wd).isoformat(),
                "kind": "league_phase2", "phase2_tier": "fc1", "round": round_no,
                "label": f"FC1 · Play-off trụ hạng · {leg_label}",
                "played": False, "fixture_result_ids": {}, "match_ids": [],
            })
        self.s["slots"] = slots
        self._shift_pending_calendar_after_played_matches()
        self.s["slots"] = sorted(slots, key=lambda item: (str(item.get("date", "")), str(item.get("id", ""))))
        self.s["calendar_schedule_version"] = 5
        validate_calendar_slots(self.s["slots"], int(cfg.get("min_rest_days", 3)))
        self._update_calendar_validation_metadata()

    def _migrate_league_phase2_schedule(self):
        """Migrate an in-progress legacy career to competition-specific weekdays.

        Old saves had one Saturday slot containing all three leagues and a single
        Saturday phase-two slot. New saves split those into FC2 Friday, Premier
        Saturday and FC1 Sunday slots while preserving played fixture IDs/results.
        """
        if not isinstance(self.s, dict):
            return
        self.s.setdefault("league_phase2", self._empty_league_phase2())
        if self.s.get("season_done"):
            # Completed seasons are historical records; never rewrite their dates/results.
            self.s["calendar_schedule_version"] = 4
            return
        schedule_version = int(self.s.get("calendar_schedule_version", 0) or 0)
        if schedule_version >= 5:
            self._update_calendar_validation_metadata()
            if not self.s.get("league_phase2", {}).get("initialized"):
                self._initialize_league_phase2_if_ready()
            return
        if schedule_version == 4:
            self._migrate_v4_calendar_to_v5()
            if not self.s.get("league_phase2", {}).get("initialized"):
                self._initialize_league_phase2_if_ready()
            return
        if schedule_version == 3:
            self._migrate_v3_calendar_to_v4()
            self._migrate_v4_calendar_to_v5()
            if not self.s.get("league_phase2", {}).get("initialized"):
                self._initialize_league_phase2_if_ready()
            return

        calendar_cfg = self.config["calendar"]
        min_rest = int(calendar_cfg.get("min_rest_days", 3))
        premier_wd = _weekday_number(calendar_cfg.get("league_day", "sat"), "league_day")
        fc2_wd = _weekday_number(calendar_cfg.get("fc2_day", "fri"), "fc2_day")
        fc1_wd = _weekday_number(calendar_cfg.get("fc1_day", "sun"), "fc1_day")
        cup_wd = _weekday_number(calendar_cfg.get("cup_day", "tue"), "cup_day")
        c1_wd = _weekday_number(calendar_cfg.get("midweek_day", "wed"), "midweek_day")
        try:
            anchor = date.fromisoformat(str(self.s.get("season_start_date") or calendar_cfg["start_date"]))
        except ValueError:
            anchor = _on_or_after(_safe_date(calendar_cfg["start_date"]), premier_wd)
        anchor = _on_or_after(anchor, premier_wd)
        # A Friday fixture must not be placed before the configured season start.
        season_start = _safe_date(calendar_cfg["start_date"])
        if not self.s.get("matches") and _date_in_week(anchor, 0, fc2_wd) < season_start:
            anchor += timedelta(days=7)
        self.s["season_start_date"] = anchor.isoformat()

        league_offsets = calendar_cfg.get("league_week_offsets", [0,1,2,3,5,6,7,8,10,11,12,13,15,17])
        fc2_offsets = calendar_cfg.get("fc2_week_offsets", [0,2,4,5,7,8,9,10,12,13,14,15,17,19])
        cup_offsets = calendar_cfg.get("cup_week_offsets", [5,10,15,17,33])
        c1_offsets = calendar_cfg.get("c1_week_offsets", [1,3,6,11,16,20,37,39,41])
        phase2_offsets = calendar_cfg.get("league_phase2_week_offsets", [30,31,32,33,34,35])
        day_by_tier = {"fc2": fc2_wd, "premier": premier_wd, "fc1": fc1_wd}
        week_offsets_by_tier = {"fc2": fc2_offsets, "premier": league_offsets, "fc1": league_offsets}
        comp_by_tier = {tier: TIER_LABELS[tier] for tier in TIERS}
        old_slots = list(self.s.get("slots", []))
        migrated = []
        covered_league_rounds = set()
        covered_phase2 = set()
        matches_by_id = {int(m.get("id", -1)): m for m in self.s.get("matches", [])}

        def fixture_ids_for_old_slot(old_slot, prefix, *, match_filter=None):
            values = {str(k): int(v) for k, v in (old_slot.get("fixture_result_ids") or {}).items()
                      if str(k).startswith(prefix)}
            if values or not match_filter:
                return values
            # Older saves may have match_ids but no per-fixture key map.
            result = {}
            for mid in old_slot.get("match_ids", []):
                match = matches_by_id.get(int(mid))
                if match and match_filter(match):
                    result[f"legacy:{prefix}:{len(result)}"] = int(mid)
            return result

        def partition_match_ids(old_slot, result_ids, *, match_filter=None):
            ids = list(dict.fromkeys(int(value) for value in result_ids.values()))
            if ids:
                return ids
            if not match_filter:
                return []
            return [int(mid) for mid in old_slot.get("match_ids", [])
                    if int(mid) in matches_by_id and match_filter(matches_by_id[int(mid)])]

        def adjusted_date(week, weekday, old_slot, has_results):
            target = _date_in_week(anchor, week, weekday)
            if has_results:
                try:
                    old_day = date.fromisoformat(str(old_slot.get("date", target.isoformat())))
                    if target < old_day:
                        target = _on_or_after(old_day, weekday)
                except ValueError:
                    pass
            return target.isoformat()

        def clone_slot(old_slot, *, slot_id, slot_date, label, extra, result_ids, match_ids, expected):
            item = dict(old_slot)
            item.update(extra)
            item["id"] = slot_id
            item["date"] = slot_date
            item["label"] = label
            item["fixture_result_ids"] = dict(result_ids)
            item["match_ids"] = list(match_ids)
            item["matches_played"] = len(match_ids)
            item["played"] = bool(expected and len(result_ids) >= expected)
            item["interactive"] = bool(result_ids) and not item["played"]
            return item

        for old_slot in old_slots:
            kind = str(old_slot.get("kind", ""))
            if kind == "league" and old_slot.get("tier") in TIERS:
                tier = old_slot["tier"]
                week_index = int(old_slot.get("round", 1)) - 1
                if not old_slot.get("played"):
                    old_slot = dict(old_slot)
                    old_slot["date"] = adjusted_date(week_offsets_by_tier[tier][week_index], day_by_tier[tier], old_slot,
                                                      bool(old_slot.get("fixture_result_ids")))
                migrated.append(old_slot)
                covered_league_rounds.add((week_index + 1, tier))
                continue

            if kind == "league":
                round_no = int(old_slot.get("round", 1))
                week_index = round_no - 1
                if old_slot.get("played"):
                    # Completed legacy rounds retain their historic result/date as one audit slot.
                    migrated.append(old_slot)
                    covered_league_rounds.update((round_no, tier) for tier in TIERS)
                    continue
                for tier in ("fc2", "premier", "fc1"):
                    prefix = f"league:{tier}:"
                    result_ids = fixture_ids_for_old_slot(
                        old_slot, prefix,
                        match_filter=lambda m, t=tier, r=round_no: m.get("comp") == comp_by_tier[t]
                        and m.get("stage") == f"league_{t}_{r}")
                    mids = partition_match_ids(
                        old_slot, result_ids,
                        match_filter=lambda m, t=tier, r=round_no: m.get("comp") == comp_by_tier[t]
                        and m.get("stage") == f"league_{t}_{r}")
                    dt = adjusted_date(week_offsets_by_tier[tier][week_index], day_by_tier[tier], old_slot, bool(result_ids))
                    migrated.append(clone_slot(
                        old_slot, slot_id=f"L{round_no:02d}_{tier.upper()}", slot_date=dt,
                        label=f"{comp_by_tier[tier]} · Vòng {round_no}", extra={"tier": tier},
                        result_ids=result_ids, match_ids=mids, expected=4))
                    covered_league_rounds.add((round_no, tier))
                continue

            if kind == "league_phase2":
                round_no = int(old_slot.get("round", 1))
                if old_slot.get("phase2_tier") in TIERS:
                    tier = old_slot["phase2_tier"]
                    if not old_slot.get("played") and 1 <= round_no <= len(phase2_offsets):
                        old_slot = dict(old_slot)
                        old_slot["date"] = adjusted_date(
                            phase2_offsets[round_no - 1], day_by_tier[tier], old_slot,
                            bool(old_slot.get("fixture_result_ids")))
                    migrated.append(old_slot)
                    covered_phase2.add((round_no, tier))
                    continue
                if old_slot.get("played"):
                    migrated.append(old_slot)
                    covered_phase2.update((round_no, tier) for tier in ("premier", "fc1", "fc2"))
                    continue
                phase_tiers = ["fc2", "premier", "fc1"] if round_no <= 2 else ["premier"]
                for tier in phase_tiers:
                    if tier == "premier":
                        prefix = "league_p2:premier:"
                        comp = comp_by_tier["premier"]
                        stage_filter = lambda m: m.get("comp") == comp and str(m.get("stage", "")).startswith("league_p2_premier_")
                        expected = 4
                        slot_id = f"P2R{round_no:02d}"
                        label = f"Premier · Giai đoạn 2 · Vòng {round_no}"
                    else:
                        prefix = f"promotion:{tier}:"
                        comp = comp_by_tier[tier]
                        stage_filter = lambda m, t=tier: m.get("comp") == comp_by_tier[t] and str(m.get("stage", "")).startswith(f"promotion_{t}_")
                        expected = 1
                        slot_id = f"P2{tier.upper()}R{round_no:02d}"
                        label = f"{comp} · Play-off thăng hạng · Vòng {round_no}"
                    result_ids = fixture_ids_for_old_slot(old_slot, prefix, match_filter=stage_filter)
                    mids = partition_match_ids(old_slot, result_ids, match_filter=stage_filter)
                    dt = adjusted_date(phase2_offsets[round_no - 1], day_by_tier[tier], old_slot, bool(result_ids))
                    migrated.append(clone_slot(
                        old_slot, slot_id=slot_id, slot_date=dt, label=label,
                        extra={"phase2_tier": tier}, result_ids=result_ids, match_ids=mids, expected=expected))
                    covered_phase2.add((round_no, tier))
                continue

            # Re-date unplayed Cup/C1 slots to their configured weekdays; played results stay untouched.
            if kind.startswith("cup_") and not old_slot.get("played"):
                round_no = int(old_slot.get("round", 1))
                if 1 <= round_no <= len(cup_offsets):
                    old_slot = dict(old_slot)
                    if kind == "cup_final":
                        p2r3 = next((x for x in old_slots if x.get("kind") == "league_phase2"
                                     and x.get("phase2_tier") == "premier" and int(x.get("round", 0)) == 3), None)
                        p2r3_date = _date_in_week(anchor, phase2_offsets[2], premier_wd)
                        old_slot["date"] = (p2r3_date + timedelta(days=3)).isoformat()
                        old_slot["label"] = "Major Cup - Chung kết"
                    else:
                        old_slot["date"] = _date_in_week(anchor, cup_offsets[round_no - 1], cup_wd).isoformat()
            elif kind.startswith("c1_") and not old_slot.get("played"):
                round_no = int(old_slot.get("round", 1))
                if 1 <= round_no <= len(c1_offsets):
                    old_slot = dict(old_slot)
                    old_slot["date"] = _date_in_week(anchor, c1_offsets[round_no - 1], c1_wd).isoformat()
            migrated.append(old_slot)

        # Ensure each regular round has one slot per league, including missing legacy rows.
        existing_ids = {str(slot.get("id")) for slot in migrated}
        legacy_completed_rounds = {int(slot.get("round", 0)) for slot in migrated
                                   if slot.get("kind") == "league" and slot.get("played") and not slot.get("tier")}
        for round_no, week in enumerate(league_offsets, 1):
            for tier in ("fc2", "premier", "fc1"):
                slot_id = f"L{round_no:02d}_{tier.upper()}"
                if (round_no, tier) in covered_league_rounds or round_no in legacy_completed_rounds or slot_id in existing_ids:
                    continue
                target_week = week_offsets_by_tier[tier][round_no - 1]
                dt = _date_in_week(anchor, target_week, day_by_tier[tier]).isoformat()
                migrated.append({"id": slot_id, "date": dt, "kind": "league", "tier": tier,
                                 "round": round_no, "label": f"{comp_by_tier[tier]} · Vòng {round_no}",
                                 "played": False, "fixture_result_ids": {}, "match_ids": []})
                existing_ids.add(slot_id)

        # Ensure six Premier phase-two Saturdays and two playoff dates for each lower tier.
        for round_no, week in enumerate(phase2_offsets, 1):
            for tier in (("fc2", "premier", "fc1") if round_no <= 2 else ("premier",)):
                slot_id = f"P2R{round_no:02d}" if tier == "premier" else f"P2{tier.upper()}R{round_no:02d}"
                if (round_no, tier) in covered_phase2 or slot_id in existing_ids:
                    continue
                dt = _date_in_week(anchor, week, day_by_tier[tier]).isoformat()
                label = (f"Premier · Giai đoạn 2 · Vòng {round_no}" if tier == "premier"
                         else f"{comp_by_tier[tier]} · Play-off thăng hạng · Vòng {round_no}")
                migrated.append({"id": slot_id, "date": dt, "kind": "league_phase2", "phase2_tier": tier,
                                 "round": round_no, "label": label, "played": False,
                                 "fixture_result_ids": {}, "match_ids": []})
                existing_ids.add(slot_id)

        # Ensure Cup/C1 slots exist if a very old or incomplete save omitted them.
        existing_ids = {str(slot.get("id")) for slot in migrated}
        cup_kinds = ["cup_r1", "cup_r16", "cup_qf", "cup_sf", "cup_final"]
        cup_labels = ["Vòng 1", "Vòng 16", "Tứ kết", "Bán kết", "Chung kết"]
        premier_round3_date = _date_in_week(anchor, phase2_offsets[2], premier_wd)
        cup_final_date = premier_round3_date + timedelta(days=3)
        for index, (week, kind, label) in enumerate(zip(cup_offsets, cup_kinds, cup_labels), 1):
            slot_id = f"CUP{index}"
            target_date = cup_final_date if kind == "cup_final" else _date_in_week(anchor, week, cup_wd)
            if slot_id not in existing_ids:
                migrated.append({"id": slot_id, "date": target_date.isoformat(),
                                 "kind": kind, "round": index,
                                 "label": ("Major Cup - Chung kết" if kind == "cup_final" else f"Cup - {label}"),
                                 "played": False})
        c1_kinds = ["c1_pi_r1", "c1_pi_mid", "c1_pi_decider", "c1_group1", "c1_group2", "c1_group3", "c1_semi1", "c1_semi2", "c1_final"]
        c1_labels = ["C1 - Play-in vòng 1", "C1 - Nhánh thắng/thua", "C1 - Trận quyết định", "C1 - Bảng vòng 1",
                     "C1 - Bảng vòng 2", "C1 - Bảng vòng 3", "C1 - Bán kết lượt đi", "C1 - Bán kết lượt về", "C1 - Chung kết"]
        for index, (week, kind, label) in enumerate(zip(c1_offsets, c1_kinds, c1_labels), 1):
            slot_id = f"C1{index}"
            if slot_id not in existing_ids:
                migrated.append({"id": slot_id, "date": _date_in_week(anchor, week, c1_wd).isoformat(),
                                 "kind": kind, "round": index, "label": label, "played": False})

        self.s["slots"] = migrated
        self._shift_pending_calendar_after_played_matches()
        self.s["slots"] = sorted(migrated, key=lambda item: (str(item.get("date", "")), str(item.get("id", ""))))
        self.s["calendar_schedule_version"] = 4
        validate_calendar_slots(self.s["slots"], min_rest)
        self._update_calendar_validation_metadata()
        self._migrate_v4_calendar_to_v5()
        if not self.s.get("league_phase2", {}).get("initialized"):
            self._initialize_league_phase2_if_ready()

    def _update_calendar_validation_metadata(self):
        calendar_cfg = self.config["calendar"]
        min_rest = int(calendar_cfg.get("min_rest_days", 3))
        validate_calendar_slots(self.s.get("slots", []), min_rest)
        self.s["calendar_validation"] = {
            "valid": True,
            "slots": len(self.s.get("slots", [])),
            "min_rest_days": min_rest,
            "league_weekday": str(calendar_cfg.get("league_day", "sat")),
            "fc1_weekday": str(calendar_cfg.get("fc1_day", "sun")),
            "fc2_weekday": str(calendar_cfg.get("fc2_day", "fri")),
            "cup_weekday": str(calendar_cfg.get("cup_day", "tue")),
            "midweek_day": str(calendar_cfg.get("midweek_day", "wed")),
            "schedule_version": 5,
        }

    def _initialize_league_phase2_if_ready(self):
        phase2 = self.s.setdefault("league_phase2", self._empty_league_phase2())
        if phase2.get("initialized"):
            self._fc1_relegation_playoff_entry()
            return True
        regular_matches = {tier: [m for m in self.s.get("matches", [])
                                  if m.get("played") and m.get("comp") == TIER_LABELS[tier]
                                  and str(m.get("stage", "")).startswith(f"league_{tier}_")]
                           for tier in TIERS}
        if any(len(regular_matches[tier]) < 56 for tier in TIERS):
            return False
        tables = {}
        for tier in TIERS:
            tables[tier] = self._standings_for_teams(
                [team for team, value in self.s["season_tiers"].items() if value == tier],
                [match["id"] for match in regular_matches[tier]])
        premier = phase2.setdefault("premier", {})
        champion_teams = [row["team"] for row in tables["premier"][:4]]
        challenge_teams = [row["team"] for row in tables["premier"][4:]]
        premier.update({
            "champion_teams": champion_teams,
            "challenge_teams": challenge_teams,
            "phase1_table": [dict(row) for row in tables["premier"]],
            "champion_rounds": make_double_round_robin(
                champion_teams, self.seed + int(self.s["season"]) * 701 + 1),
            "challenge_rounds": make_double_round_robin(
                challenge_teams, self.seed + int(self.s["season"]) * 701 + 2),
            "match_ids": {"champion": [[] for _ in range(6)], "challenge": [[] for _ in range(6)]},
        })
        playoffs = phase2.setdefault("promotion_playoffs", {})
        for tier in ("fc1", "fc2"):
            rows = tables[tier]
            playoffs[tier] = {
                "teams": {"first": rows[0]["team"], "second": rows[1]["team"],
                          "third": rows[2]["team"], "fourth": rows[3]["team"]},
                "semi_match": None, "final_match": None, "winner": None,
            }
        phase2["initialized"] = True
        phase2["initialized_season"] = int(self.s["season"])
        self._fc1_relegation_playoff_entry(tables.get("fc1"))
        return True

    def _fc1_relegation_playoff_entry(self, phase1_table=None):
        """Return the FC1 6th-v-7th relegation tie, creating its participants once known."""
        phase2 = self.s.setdefault("league_phase2", self._empty_league_phase2())
        entry = phase2.setdefault("relegation_playoff", {
            "teams": {}, "leg1_match": None, "leg2_match": None, "winner": None, "loser": None,
        })
        entry.setdefault("teams", {})
        for key in ("leg1_match", "leg2_match", "winner", "loser"):
            entry.setdefault(key, None)
        if not entry.get("teams") and phase2.get("initialized"):
            rows = phase1_table if phase1_table is not None else self.league_phase1_table("fc1")
            if len(rows) >= 8:
                entry["teams"] = {"sixth": rows[5]["team"], "seventh": rows[6]["team"]}
        return entry

    def _phase2_fixture_specs(self, slot, include_played=False):
        """Build phase-two Premier and promotion-playoff fixtures for a phase-two slot."""
        if not self._initialize_league_phase2_if_ready():
            return []
        phase2 = self.s["league_phase2"]
        round_index = int(slot.get("round", 1)) - 1
        if not 0 <= round_index < 6:
            return []
        result_ids = slot.get("fixture_result_ids", {})
        specs = []

        def add(key, comp, round_label, home, away, *, force_winner=False,
                aggregate_before=None, stage, **meta):
            if not include_played and key in result_ids:
                return
            specs.append({
                "spec_key": key, "slot_id": slot["id"], "season": int(self.s["season"]),
                "date": slot["date"], "comp": comp, "round": round_label,
                "home": home, "away": away, "neutral": False,
                "force_winner": bool(force_winner), "aggregate_before": aggregate_before,
                "stage": stage, "_meta": meta,
            })

        premier = phase2["premier"]
        phase2_tier = slot.get("phase2_tier")  # None is accepted for legacy combined slots.
        if phase2_tier in (None, "premier"):
            for group_key, label, rounds_key in (("champion", "Champion", "champion_rounds"),
                                                  ("challenge", "Challenge", "challenge_rounds")):
                rounds = premier.get(rounds_key, [])
                if round_index >= len(rounds):
                    continue
                for index, (home, away) in enumerate(rounds[round_index]):
                    add(f"league_p2:premier:{group_key}:{round_index}:{index}",
                        TIER_LABELS["premier"], f"{label} · Vòng {round_index + 1}", home, away,
                        stage=f"league_p2_premier_{group_key}_{round_index + 1}",
                        bucket="premier_phase2", group=group_key, round_index=round_index, index=index)

        # FC1/FC2 promotion play-offs: 3rd vs 4th, then 2nd vs winner.
        if round_index in (0, 1) and phase2_tier in (None, "fc1", "fc2"):
            playoff_tiers = ("fc1", "fc2") if phase2_tier is None else (phase2_tier,)
            for tier in playoff_tiers:
                entry = phase2.get("promotion_playoffs", {}).get(tier, {})
                teams = entry.get("teams", {})
                if round_index == 0:
                    home, away = teams.get("third"), teams.get("fourth")
                    phase_label, stage_key = "Play-off · Bán kết (3 vs 4)", "semi"
                else:
                    semi_id = entry.get("semi_match")
                    if not semi_id:
                        continue
                    semi = self.match_by_id(semi_id)
                    winner = semi.get("winner")
                    home, away = teams.get("second"), winner
                    phase_label, stage_key = "Play-off · Chung kết (2 vs thắng 3/4)", "final"
                if home and away:
                    add(f"promotion:{tier}:{stage_key}", TIER_LABELS[tier], phase_label, home, away,
                        force_winner=True, stage=f"promotion_{tier}_{stage_key}",
                        bucket="promotion_playoff", tier=tier, stage_key=stage_key)

        # FC1 relegation play-off: 7th hosts leg one, 6th hosts leg two.
        if phase2_tier == "fc1" and round_index in (3, 5):
            entry = self._fc1_relegation_playoff_entry()
            teams = entry.get("teams", {})
            sixth, seventh = teams.get("sixth"), teams.get("seventh")
            if round_index == 3 and sixth and seventh:
                add("relegation:fc1:leg1", TIER_LABELS["fc1"],
                    "Play-off trụ hạng · Lượt đi", seventh, sixth,
                    stage="relegation_fc1_leg1", bucket="fc1_relegation", leg_key="leg1")
            elif round_index == 5 and sixth and seventh and entry.get("leg1_match"):
                first = self.match_by_id(entry["leg1_match"])
                aggregate_before = (int(first.get("ag", 0) or 0), int(first.get("hg", 0) or 0))
                add("relegation:fc1:leg2", TIER_LABELS["fc1"],
                    "Play-off trụ hạng · Lượt về", sixth, seventh,
                    force_winner=True, aggregate_before=aggregate_before,
                    stage="relegation_fc1_leg2", bucket="fc1_relegation", leg_key="leg2")
        return specs

    def future_phase2_fixtures(self, slot):
        """Calendar-only, read-only phase-two fixture projection for any future slot."""
        if not slot or slot.get("kind") != "league_phase2":
            return []
        return self._phase2_fixture_specs(slot, include_played=False)

    def premier_phase2_tables(self):
        """Return Champion and Challenge tables with all phase-one points carried over."""
        phase2 = self.s.get("league_phase2", {})
        if phase2.get("initialized"):
            premier = phase2["premier"]
            base_match_ids = [m["id"] for m in self.s["matches"]
                              if m.get("played") and m.get("comp") == TIER_LABELS["premier"]
                              and str(m.get("stage", "")).startswith("league_premier_")]
            result = {}
            for group_key, teams in (("champion", premier["champion_teams"]),
                                     ("challenge", premier["challenge_teams"])):
                phase2_ids = [mid for group in premier.get("match_ids", {}).get(group_key, []) for mid in group]
                rows = self._standings_for_teams(teams, base_match_ids + phase2_ids,
                                                 apply_ffp_deductions=True)
                phase1_rows = self._standings_for_teams(teams, base_match_ids, apply_ffp_deductions=False)
                phase1_by_team = {row["team"]: row for row in phase1_rows}
                phase2_rows = self._standings_for_teams(teams, phase2_ids, apply_ffp_deductions=False)
                phase2_by_team = {row["team"]: row for row in phase2_rows}
                for row in rows:
                    row["phase1_pts"] = phase1_by_team[row["team"]]["pts"]
                    row["phase2_pts"] = phase2_by_team[row["team"]]["pts"]
                    row["group"] = "Champion" if group_key == "champion" else "Challenge"
                result[group_key] = rows
            return result
        table = self.league_phase1_table("premier")
        return {"champion": [dict(row, group="Champion", phase1_pts=row["pts"], phase2_pts=0) for row in table[:4]],
                "challenge": [dict(row, group="Challenge", phase1_pts=row["pts"], phase2_pts=0) for row in table[4:]]}

    def promotion_playoff_summary(self, tier):
        phase2 = self.s.get("league_phase2", {})
        entry = phase2.get("promotion_playoffs", {}).get(tier, {})
        teams = entry.get("teams", {})
        matches = {}
        for key, mid in (("semi", entry.get("semi_match")), ("final", entry.get("final_match"))):
            matches[key] = self.match_by_id(mid) if mid else None
        return {"teams": teams, "matches": matches, "winner": entry.get("winner"),
                "ready": bool(phase2.get("initialized"))}

    def relegation_playoff_summary(self):
        """Return FC1 two-leg survival tie and its aggregate score."""
        entry = self._fc1_relegation_playoff_entry()
        matches = {}
        for key, field in (("leg1", "leg1_match"), ("leg2", "leg2_match")):
            mid = entry.get(field)
            matches[key] = self.match_by_id(mid) if mid else None
        teams = dict(entry.get("teams") or {})
        aggregate = None
        if matches["leg1"] and matches["leg2"]:
            first, second = matches["leg1"], matches["leg2"]
            aggregate = {
                "sixth": int(first.get("ag", 0) or 0) + int(second.get("hg", 0) or 0),
                "seventh": int(first.get("hg", 0) or 0) + int(second.get("ag", 0) or 0),
            }
        winner = entry.get("winner") or (matches["leg2"].get("winner") if matches["leg2"] else None)
        loser = entry.get("loser")
        if winner and not loser:
            loser = teams.get("seventh") if winner == teams.get("sixth") else teams.get("sixth")
        return {"teams": teams, "matches": matches, "aggregate": aggregate,
                "winner": winner, "loser": loser, "ready": bool(teams)}

    def league_phase1_table(self, tier):
        if tier not in TIERS: tier = TIER_KEY_BY_LABEL.get(tier, "premier")
        teams = [t for t, tr in self.s["season_tiers"].items() if tr == tier]
        ms = [m for m in self.s["matches"] if m["comp"] == TIER_LABELS[tier] and m.get("played")
              and str(m.get("stage", "")).startswith(f"league_{tier}_")]
        return self._standings_for_teams(teams, [m["id"] for m in ms],
                                         apply_ffp_deductions=True)

    def new_game(self):
        self.rng = random.Random(self.seed)
        initial_season = _safe_date(self.config["calendar"].get("start_date", "2026-08-01")).year
        all_players = {p["player_id"]: dict(p) for p in self.source_rows}
        team_names = list(self.source_by_team)
        strength = {t: average_top11(self.source_by_team[t]) for t in team_names}
        sorted_teams = sorted(team_names, key=lambda t: (-strength[t], team_names.index(t)))
        tier_members = {"premier": sorted_teams[:8], "fc1": sorted_teams[8:16], "fc2": sorted_teams[16:24]}
        team_tiers = {team: tier for tier, teams in tier_members.items() for team in teams}
        meta, dyn = {}, {}
        for p in self.source_rows:
            pid = p["player_id"]
            age = _age_from_birthdate(p.get("birthdate"))
            ovr = int(p["overall_rating"])
            potential = min(99.0, ovr + max(0.0, 27.0-age) * self.rng.uniform(.5, 1.6))
            meta[pid] = {"potential": round(potential, 1), "status": "active", "age": age,
                         "retire_season": None, "peak_ovr": ovr, "seasons_played": 0}
            dyn[pid] = {"form": 0.0, "fit": 100.0}
        self.s = {
            "version": self.SAVE_VERSION, "season": initial_season, "season_done": False,
            "players": all_players, "rosters": {t: [p["player_id"] for p in self.source_by_team[t]] for t in team_names},
            "team_tiers": team_tiers, "season_tiers": dict(team_tiers), "strength_seed": strength,
            "player_meta": meta, "dyn": dyn, "academy": {t: [] for t in team_names}, "generated_players": [],
            "retired_players": [], "used_names": sorted({p.get("common_name", "") for p in self.source_rows if p.get("common_name")}),
            "elo": {t: round(1200 + strength[t]*8, 2) for t in team_names},
            "previous_total_points": {}, "previous_league_points": {}, "previous_ranks": {},
            "seed_order": list(sorted_teams), "season_seed_order": list(sorted_teams),
            "matches": [], "slots": [], "slot_cursor": 0, "next_match_id": 1,
            "league_fixtures": {}, "league_phase2": {},
            "cup": {"bracket": {}, "winner": None, "runner_up": None},
            "c1": {}, "season_stats": {}, "history": [], "last_development": [],
            "competition_awards": {}, "season_awards": None,
            "last_promotions": {"promoted": [], "relegated": []}, "last_news": [], "last_match_date": {}
        }
        # Completed-season history is kept in SQLite alongside the active career.
        self._load_season_history()
        for team in team_names:
            self.s["season_stats"].update({pid: self._empty_stat() for pid in self.s["rosters"][team]})
        self._ensure_match_state()
        self._create_academies()
        self._setup_season(first=True)
        self._write_initial_csvs()
        self._ensure_economy_state()
        self.save()

    def reset_simulation(self):
        """Start a new career without creating backups of the previous career."""
        self._clear_generated_outputs()
        self.database.reset_career()
        self.new_game()
        return None

    def _ensure_match_state(self):
        """Bổ sung state trận đấu và migrate tên HLV/trọng tài từ reference_data SQLite."""
        if not isinstance(self.s, dict):
            return
        s = self.s
        s.setdefault("match_engine_version", 1)
        s.setdefault("team_ctx", {})
        s.setdefault("last_match_context", {})
        s.setdefault("coaches", {})
        s.setdefault("player_status", {})
        s.setdefault("player_medical", {})
        s.setdefault("player_medical_last_day", "")
        s.setdefault("player_last_played", {})
        s.setdefault("team_momentum", {})
        s.setdefault("news", [])
        s.setdefault("team_elo_history", [])
        # Award registries are additive fields so older career saves remain compatible.
        s.setdefault("competition_awards", {})
        s.setdefault("season_awards", None)
        for stat in (s.get("season_stats", {}) or {}).values():
            if isinstance(stat, dict):
                stat.setdefault("injury_days", 0)
                stat.setdefault("training_days", 0)
                stat.setdefault("morale_sum", 0.0)
                stat.setdefault("morale_count", 0)

        # Migration một lần cho save cũ: thay tên HLV và trọng tài hard-code
        # bằng dữ liệu người dùng cung cấp, giữ nguyên thuộc tính kỹ thuật.
        human_version = int(s.get("human_names_version", 0) or 0)
        if human_version < 1:
            new_refs = match_context.make_refs(seed=self.seed)
            old_refs = {
                int(ref.get("id", index)): ref
                for index, ref in enumerate(s.get("match_refs", []) or [])
                if isinstance(ref, dict)
            }
            for ref in new_refs:
                old = old_refs.get(ref["id"])
                if old:
                    for key in ("strict", "home_bias", "var_use"):
                        if key in old:
                            ref[key] = old[key]
            s["match_refs"] = new_refs

            manager_names = assign_manager_names(s.get("rosters", {}).keys(), self.seed)
            for team, manager_name in manager_names.items():
                coach = s["coaches"].setdefault(
                    team, match_coach.make_coach(team, manager_name=manager_name)
                )
                if isinstance(coach, dict):
                    coach["name"] = manager_name

            s["human_names_version"] = 1
        else:
            s.setdefault("match_refs", match_context.make_refs(seed=self.seed))

        if not s["team_elo_history"] and s.get("elo"):
            s["team_elo_history"].append({
                "season": int(s.get("season", 2026)),
                "date": str(s.get("season_start_date", "")),
                "label": "Mốc đầu kỳ / khôi phục save",
                "elo": dict(s.get("elo", {})),
            })

        manager_names = assign_manager_names(s.get("rosters", {}).keys(), self.seed)
        for team in s.get("rosters", {}):
            s["team_ctx"].setdefault(team, match_context.default_team_ctx(team))
            s["coaches"].setdefault(
                team,
                match_coach.make_coach(team, manager_name=manager_names.get(team))
            )
            s["team_momentum"].setdefault(team, {"mom": 0.0, "last": [], "home_last": [], "away_last": []})

    def _enhanced_match_data(self, home, away, match_date, neutral, force_winner, aggregate_before, match_id, comp):
        self._ensure_match_state()
        s = self.s
        mode = self.config.get("match_engine", "enhanced")
        if mode != "enhanced":
            return None
        seed = self.seed + int(s["season"]) * 1000003 + int(match_id) * 9176
        home_status = {pid: player_health.effective_status(s, pid, as_of_date=str(match_date))
                       for pid in s["rosters"][home]}
        away_status = {pid: player_health.effective_status(s, pid, as_of_date=str(match_date))
                       for pid in s["rosters"][away]}
        rank_gap = abs(float(s["elo"].get(home, 1200)) - float(s["elo"].get(away, 1200)))
        rivalry = max(0.0, min(1.0, 1.0 - rank_gap / 500.0))
        comp_low = str(comp).lower()
        importance = 0.95 if "chung kết" in comp_low else (0.80 if "bán kết" in comp_low else
                     0.65 if comp in ("Cup", "C1", "C1 Play-in") else 0.40)
        return match_adapter.simulate_fixture(
            home=home, away=away, home_squad=self.team_players(home), away_squad=self.team_players(away),
            home_dyn=self._current_dyn(home), away_dyn=self._current_dyn(away),
            home_status=home_status, away_status=away_status,
            team_momentum=s["team_momentum"], coaches=s["coaches"], refs=s["match_refs"],
            team_ctx=s["team_ctx"], last_match=s["last_match_context"], seed=seed,
            match_date=str(match_date), neutral=bool(neutral), importance=importance, rivalry=rivalry,
            knockout=bool(force_winner), aggregate_before=aggregate_before)

    def _update_enhanced_match_state(self, match):
        """Apply player ratings, fatigue, availability, coach momentum and home-advantage drift."""
        s = self.s
        home, away = match["home"], match["away"]
        teams = (("h", home, "minutes_h", "ga_h", "cards_h", "inj_h"),
                 ("a", away, "minutes_a", "ga_a", "cards_a", "inj_a"))
        side_result = {"h": 1 if match["hg"] > match["ag"] else (0 if match["hg"] == match["ag"] else -1),
                       "a": 1 if match["ag"] > match["hg"] else (0 if match["hg"] == match["ag"] else -1)}
        date_value = date.fromisoformat(str(match["date"]))
        goals_by = Counter()
        assists_by = Counter()
        for goal in match.get("goals", []):
            if goal.get("scorer_id"):
                goals_by[goal["scorer_id"]] += 1
            if goal.get("assist_id"):
                assists_by[goal["assist_id"]] += 1

        # Status counters age by one appearance of the club, not by a calendar slot.
        for side, team, mins_key, conceded_key, cards_key, injuries_key in teams:
            roster = list(s["rosters"].get(team, []))
            unavailable_before_fixture = {}
            for pid in roster:
                st = s["player_status"].setdefault(pid, {"inj": 0, "susp": 0, "yc": 0})
                effective = player_health.effective_status(s, pid, as_of_date=str(match.get("date", "")))
                unavailable_before_fixture[pid] = {
                    "injured": int(effective.get("inj", 0) or 0) > 0,
                    "suspended": int(effective.get("susp", 0) or 0) > 0,
                }
                if st.get("inj", 0) > 0: st["inj"] -= 1
                if st.get("susp", 0) > 0: st["susp"] -= 1
                if st.get("inj", 0) <= 0 and st.get("susp", 0) <= 0 and st.get("yc", 0) <= 0:
                    s["player_status"].pop(pid, None)

            minutes = match.get(mins_key, {}) or {}
            conceded = match.get(conceded_key, {}) or {}
            pstats = (match.get("player_stats", {}).get(side, {}) or {})
            cards = match.get(cards_key, []) or []
            yellows, reds = Counter(), Counter()
            for card in cards:
                pid, kind = card.get("p"), card.get("c")
                if kind == "y":
                    yellows[pid] += 1
                elif kind in ("r", "yr"):
                    reds[pid] += 1
            for pid, mins in minutes.items():
                raw = s["players"].get(pid)
                if not raw:
                    continue
                stat = s["season_stats"].setdefault(pid, self._empty_stat())
                pd = pstats.get(pid, {})
                goals = int(pd.get("g", goals_by[pid]))
                assists = int(pd.get("a", assists_by[pid]))
                rating = float(pd.get("rt", 6.3))
                stat["apps"] += 1
                stat["mins"] += int(mins)
                stat["goals"] += goals
                stat["assists"] += assists
                stat["rating_sum"] += rating
                stat["rating_count"] += 1
                stat["yellow"] += int(pd.get("yc", yellows[pid]))
                stat["red"] += int(pd.get("rc", 1 if reds[pid] else 0))
                if int(conceded.get(pid, 0)) == 0 and int(mins) >= 60:
                    stat["clean_sheets"] += 1
                if str(raw.get("position", "")).upper() == "GK":
                    stat["gk_apps"] = int(stat.get("gk_apps", 0) or 0) + 1
                    stat["gk_minutes"] = int(stat.get("gk_minutes", 0) or 0) + int(mins)
                    stat["gk_goals_conceded"] = int(stat.get("gk_goals_conceded", 0) or 0) + max(0, int(conceded.get(pid, 0) or 0))
                    if int(conceded.get(pid, 0) or 0) == 0 and int(mins) >= 60:
                        stat["gk_clean_sheets"] = int(stat.get("gk_clean_sheets", 0) or 0) + 1
                    stat["gk_saves"] = int(stat.get("gk_saves", 0) or 0) + max(0, int(pd.get("sv", 0) or 0))
                base = match_engine.make_player(raw)
                dyn = s["dyn"].setdefault(pid, {"form": 0.0, "fit": 100.0, "morale": 65.0})
                dyn["form"] = match_engine.next_form(float(dyn.get("form", 0.0)), rating, int(mins))
                dyn["fit"] = max(0.0, float(dyn.get("fit", 100.0)) - match_engine.match_fit_loss(int(mins), base["sta"], base["age"]))
                morale = float(dyn.get("morale", 65.0))
                morale_target = max(25.0, min(95.0, 65.0 + (rating - 6.5) * 15.0))
                dyn["morale"] = round(max(20.0, min(100.0, morale * 0.72 + morale_target * 0.28)), 2)
                stat["morale_sum"] = float(stat.get("morale_sum", 0.0)) + dyn["morale"]
                stat["morale_count"] = int(stat.get("morale_count", 0) or 0) + 1
                s["player_last_played"][pid] = date_value.isoformat()

            # Recover unused players using actual rest days since their last appearance.
            for pid in roster:
                if pid in minutes:
                    continue
                unavailable = unavailable_before_fixture.get(pid, {})
                if unavailable.get("injured") or unavailable.get("suspended"):
                    stat = s["season_stats"].setdefault(pid, self._empty_stat())
                    if unavailable.get("injured"):
                        stat["injury_matches"] = int(stat.get("injury_matches", 0) or 0) + 1
                    if unavailable.get("suspended"):
                        stat["suspension_matches"] = int(stat.get("suspension_matches", 0) or 0) + 1
                dyn = s["dyn"].setdefault(pid, {"form": 0.0, "fit": 100.0})
                previous = s.get("player_last_played", {}).get(pid)
                days = 3
                if previous:
                    try: days = max(0, (date_value - date.fromisoformat(previous)).days)
                    except ValueError: pass
                dyn["fit"] = match_engine.recover_fit(float(dyn.get("fit", 100.0)), min(days, 30))
                dyn["form"] = match_engine.decay_form(float(dyn.get("form", 0.0)), min(days, 30))

            for pid in set(yellows) | set(reds):
                st = s["player_status"].setdefault(pid, {"inj": 0, "susp": 0, "yc": 0})
                st["yc"] = int(st.get("yc", 0)) + yellows[pid]
                if reds[pid]:
                    st["susp"] = max(int(st.get("susp", 0)), 1 if any(c.get("p") == pid and c.get("c") == "yr" for c in cards) else self.rng.choices([1, 2, 3], [0.7, 0.2, 0.1])[0])
                    st["yc"] = 0
                elif st["yc"] >= 2:
                    st["susp"] = max(int(st.get("susp", 0)), 1)
                    st["yc"] -= 2
            for inj in match.get(injuries_key, []) or []:
                pid = str(inj.get("p") or "")
                if not pid:
                    continue
                st = s["player_status"].setdefault(pid, {"inj": 0, "susp": 0, "yc": 0})
                st["inj"] = max(int(st.get("inj", 0)), int(inj.get("n", 1)))
                st["injury_name"] = inj.get("name", "Chấn thương")
                medical_record = player_health.register_injury(s, pid, inj, str(match.get("date", "")))
                inj["medical_record_id"] = medical_record.get("id")

        # Team momentum and slow-moving home advantage.
        expected_home = float(match.get("expected_home", 0.5))
        if match.get("pens"):
            home_result, away_result = ((0.75, 0.5) if match["pens"]["home"] > match["pens"]["away"] else (0.5, 0.75))
        else:
            home_result = 1.0 if match["hg"] > match["ag"] else (0.0 if match["hg"] < match["ag"] else 0.5)
            away_result = 1.0 - home_result if home_result != 0.5 else 0.5
        for team, result, expected, venue_key in ((home, home_result, expected_home, "home_last"),
                                                        (away, away_result, 1.0 - expected_home, "away_last")):
            td = s["team_momentum"].setdefault(
                team, {"mom": 0.0, "last": [], "home_last": [], "away_last": []})
            # Momentum phản ánh vượt/kém kỳ vọng nhưng phải nguội dần theo thời gian nghỉ.
            prior_mom = float(td.get("mom", 0.0))
            previous_date = td.get("date")
            if previous_date:
                try:
                    days = max(0, (date_value - date.fromisoformat(str(previous_date))).days)
                    prior_mom = match_engine.decay_mom(prior_mom, min(days, 60))
                except ValueError:
                    pass
            td["mom"] = match_engine.next_mom(prior_mom, result, expected)
            label = "W" if result > 0.5 else ("L" if result < 0.5 else "D")
            # Lưu 10 trận để engine dùng cửa sổ form 8 trận mà vẫn tương thích save cũ.
            td["last"] = (list(td.get("last", [])) + [label])[-10:]
            td.setdefault("home_last", [])
            td.setdefault("away_last", [])
            td[venue_key] = (list(td.get(venue_key, [])) + [label])[-6:]
            td["date"] = date_value.isoformat()
        ctx = match.get("ctx")
        if ctx and not match.get("neutral"):
            match_context.update_home_adv(s["team_ctx"], home, home_result, expected_home)
        played_minutes = 120 if match.get("et") else 90
        for team, away_flag in ((home, bool(match.get("neutral"))), (away, True)):
            s["last_match_context"][team] = {"date": date_value.isoformat(), "min": played_minutes, "away": away_flag}

        # Remove empty status entries to keep career saves compact.
        for pid in list(s["player_status"]):
            st = s["player_status"][pid]
            if not any(int(st.get(k, 0)) for k in ("inj", "susp", "yc")):
                s["player_status"].pop(pid, None)

    def _empty_stat(self):
        return {"apps": 0, "mins": 0, "goals": 0, "assists": 0, "rating_sum": 0.0,
                "rating_count": 0, "clean_sheets": 0, "yellow": 0, "red": 0,
                "gk_apps": 0, "gk_minutes": 0, "gk_clean_sheets": 0,
                "gk_goals_conceded": 0, "gk_saves": 0,
                "injury_matches": 0, "suspension_matches": 0, "injury_days": 0,
                "training_days": 0, "morale_sum": 0.0, "morale_count": 0}

    def _all_known_names(self):
        names = set(self.s.get("used_names", []))
        for row in self.s["players"].values(): names.add(row.get("common_name", ""))
        for team_acad in self.s.get("academy", {}).values():
            for entry in team_acad: names.add(entry["player"].get("common_name", ""))
        for p in self.s.get("retired_players", []): names.add(p.get("player_name", ""))
        return {n for n in names if n}

    def _new_name(self):
        """Sinh tên cầu thủ trẻ từ kho first_names/second_names trong SQLite."""
        if not self.first_names or not self.last_names:
            raise RuntimeError(
                "Kho tên cầu thủ đang trống. Kiểm tra reference_data['human_names'] "
                "(first_names và second_names)."
            )
        used = self._all_known_names()
        for _ in range(3000):
            first = self.rng.choice(self.first_names)
            last = self.rng.choice(self.last_names)
            name = f"{first} {last}"
            if name not in used:
                self.s.setdefault("used_names", []).append(name)
                return first, last, name

        # Nếu trùng lặp hiếm khi xảy ra sau nhiều lần thử, vẫn dùng tên từ JSON
        # và thêm hậu tố để bảo đảm player name không bị trùng.
        first = self.rng.choice(self.first_names)
        last = self.rng.choice(self.last_names)
        base = f"{first} {last}"
        suffix = 2
        while f"{base} {suffix}" in used:
            suffix += 1
        name = f"{base} {suffix}"
        self.s.setdefault("used_names", []).append(name)
        return first, last, name

    @staticmethod
    def _normalize_club_name(value):
        return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())

    def academy_talent_profile(self, team):
        """Return the configured academy quality profile for a club.

        Elite aliases match the user's short names (BlackSam/Hunter/AssassinS/Fury/Knight)
        as well as the full club names stored in the CSV. Middle aliases work the same way.
        """
        cfg = self.config["players"].get("academy_talent", {})
        team_key = self._normalize_club_name(team)
        elite_keys = {self._normalize_club_name(name) for name in cfg.get("elite_teams", [])}
        middle_keys = {self._normalize_club_name(name) for name in cfg.get("middle_teams", [])}
        aliases = {
            "blacksam": "elite", "blackhunter": "elite", "hunter": "elite",
            "assassins": "elite", "assassinsfc": "elite", "fury": "elite",
            "furyfighter": "elite", "knight": "elite", "knightcastle": "elite",
            "yellow": "middle", "yellowthunder": "middle", "yellowthunderfc": "middle",
            "poseidon": "middle", "poseidonfc": "middle", "savior": "middle",
            "saviorfc": "middle", "silver": "middle", "silvermoon": "middle",
        }
        if team_key in elite_keys or aliases.get(team_key) == "elite":
            group = "elite"
        elif team_key in middle_keys or aliases.get(team_key) == "middle":
            group = "middle"
        else:
            group = "standard"
        profile = dict(cfg.get("profiles", {}).get(group, {}))
        # Baseline academy prospects must be good enough to sustain a 20+ season
        # career, not only the rare "superstar" roll. Older versions applied the
        # configured profile ranges only to the superstar branch and generated
        # nearly every ordinary prospect from the generic 55–82 OVR formula.
        # That caused league-wide talent and payroll to collapse after the first
        # generation retired. Keep these defaults in code so existing user
        # config.json files gain the correction without being overwritten.
        baseline = {
            "elite": {"development_potential_range": [86, 96],
                      "development_overall_range": [74, 84]},
            "middle": {"development_potential_range": [83, 94],
                       "development_overall_range": [70, 80]},
            "standard": {"development_potential_range": [78, 91],
                         "development_overall_range": [66, 77]},
        }.get(group, {})
        for key, value in baseline.items():
            profile.setdefault(key, value)
        return {"group": group, **profile}

    def _make_youth_row(self, team, position, season, player_id, potential=None, academy=False):
        templates = self.source_players_by_pos.get(position) or self.source_rows
        template = self.rng.choice(templates)
        row = dict(template)
        first, last, common = self._new_name()
        row["player_id"] = player_id
        row["common_name"] = common
        row["first_name"] = first
        row["last_name"] = last
        row["club"] = team
        row["position"] = position
        row["alternate_positions"] = ""
        age_min, age_max = (int(v) for v in self.config["players"].get("youth_age", [16, 20]))
        age = self.rng.randint(age_min, age_max)
        ref = _safe_date(self.config["calendar"]["start_date"]).replace(year=min(max(1900, int(season)), 2100))
        born_year = ref.year - age
        try:
            born = ref.replace(year=born_year)
        except ValueError:
            born = ref.replace(year=born_year, day=28)
        row["birthdate"] = born.strftime("%d-%m-%Y")
        row["snapshot_date"] = ref.strftime("%d-%m-%Y")

        league_tier = self.s["team_tiers"].get(team, "fc1")
        talent = self.academy_talent_profile(team)
        talent_group = talent["group"]
        is_superstar = potential is None and self.rng.random() < float(talent["superstar_probability"])
        if potential is None and is_superstar:
            potential_low, potential_high = talent["potential_range"]
            potential = round(self.rng.uniform(float(potential_low), float(potential_high)), 1)
            ovr_low, ovr_high = talent["overall_range"]
            target_ovr = self.rng.randint(int(ovr_low), int(ovr_high))
        else:
            low, high = talent.get("development_potential_range", self.config["players"].get("potential_range", [68, 94]))
            if potential is None:
                league_bonus = 1.5 if league_tier == "premier" else (-1.0 if league_tier == "fc2" else 0.0)
                pot = self.rng.uniform(float(low), float(high)) + league_bonus
                potential = round(min(99.0, max(60.0, pot)), 1)
            ovr_low, ovr_high = talent.get("development_overall_range", [55, 82])
            generated_ovr = self.rng.randint(int(ovr_low), int(ovr_high))
            # Retain a meaningful growth runway for ordinary prospects too;
            # don't create a 78 OVR youngster with a 68 POT ceiling.
            target_ovr = int(round(min(generated_ovr, max(55.0, float(potential) - 6.0))))

        source_ovr = max(1, int(template.get("overall_rating", 75)))
        factor = target_ovr / source_ovr
        for key in ATTRIBUTE_FIELDS:
            raw = int(template.get(key, 60))
            value = int(round(raw * factor + self.rng.gauss(0, 2.0)))
            row[key] = int(max(35, min(99, value)))
        row["overall_rating"] = target_ovr
        row["skill_moves"] = self.rng.randint(1, 4)
        row["weak_foot"] = self.rng.randint(2, 5)
        row["height_cm"] = int(max(160, min(205, round(float(template.get("height_cm", 178)) + self.rng.gauss(0, 5)))))
        row["weight_kg"] = int(max(52, min(105, round(float(template.get("weight_kg", 75)) + self.rng.gauss(0, 4)))))
        row["fifa_rank"] = ""
        row["is_default"] = "FALSE"
        market_input = dict(row)
        market_input.update({"age": float(age), "potential": float(potential)})
        row["market_value"] = WageService(self.economy_config).market_value(market_input)
        return row, {
            "potential": float(potential), "status": "academy" if academy else "active", "age": float(age),
            "retire_season": None, "peak_ovr": target_ovr, "seasons_played": 0,
            "academy_talent_group": talent_group, "is_superstar": bool(is_superstar),
        }

    def _create_academies(self):
        academy_size = int(self.config["players"].get("academy_size", 6))
        season = int(self.s.get("season", 2026))
        # Sinh dữ liệu dự phòng cho mọi CLB ngay từ mùa khởi tạo; cầu thủ chưa được tính vào đội một.
        for team in self.s["rosters"]:
            positions = list(POSITION_TEMPLATE)
            self.s["academy"][team] = []
            for i in range(academy_size):
                position = self.rng.choice(positions)
                pid = f"ACD{season}_{_team_code(team)}_{i+1:02d}"
                row, meta = self._make_youth_row(team, position, season, pid, academy=True)
                self.s["academy"][team].append({"player": row, "meta": meta})

    def _maintain_academy_age_range(self, season=None):
        """Giữ đúng quy mô học viện và chỉ hiển thị cầu thủ trẻ 16–20 tuổi.

        Cầu thủ quá tuổi học viện được lưu trong academy_alumni, không bị xóa khỏi
        dữ liệu career; academy được bổ sung lứa mới để duy trì số lượng cấu hình.
        """
        if not isinstance(self.s, dict) or not self.s.get("rosters"):
            return {"archived": 0, "created": 0}
        cfg = self.config.get("players", {})
        age_min, age_max = (int(v) for v in cfg.get("youth_age", [16, 20]))
        academy_size = max(0, int(cfg.get("academy_size", 6)))
        season = int(season if season is not None else self.s.get("season", 2026))
        academy = self.s.setdefault("academy", {})
        alumni = self.s.setdefault("academy_alumni", [])
        archive_count = created_count = 0
        known_ids = set(self.s.get("players", {}))
        known_ids.update(str(x.get("player_id")) for x in self.s.get("generated_players", []))
        known_ids.update(str(x.get("player", {}).get("player_id", "")) for x in alumni)
        for team in self.s.get("rosters", {}):
            entries = list(academy.get(team, []))
            retained = []
            for entry in entries:
                player = entry.get("player", {})
                meta = entry.get("meta", {})
                try:
                    age = float(meta.get("age", age_min))
                except (TypeError, ValueError):
                    age = float(age_min - 1)
                if age_min <= age <= age_max:
                    # Existing prospects keep their original attributes; tag them as legacy
                    # rather than retroactively rolling a superstar outcome.
                    meta.setdefault("academy_talent_group", self.academy_talent_profile(team)["group"])
                    meta.setdefault("is_superstar", False)
                    retained.append(entry)
                    known_ids.add(str(player.get("player_id", "")))
                else:
                    archived_id = str(player.get("player_id", ""))
                    if archived_id:
                        known_ids.add(archived_id)
                    alumni.append({
                        "team": team,
                        "player": dict(player),
                        "meta": dict(meta),
                        "archived_season": season,
                        "reason": "academy_age_limit",
                    })
                    archive_count += 1
            # Giữ đủ số prospect, ưu tiên vị trí đang thiếu trong academy.
            pos_counts = Counter(e.get("player", {}).get("position") for e in retained)
            serial = 1
            while len(retained) < academy_size:
                position = min(POSITION_TEMPLATE, key=lambda pos: (pos_counts[pos], list(POSITION_TEMPLATE).index(pos)))
                while True:
                    pid = f"ACD{season}_{_team_code(team)}_{serial:02d}"
                    serial += 1
                    if pid not in known_ids:
                        break
                row, meta = self._make_youth_row(team, position, season, pid, academy=True)
                retained.append({"player": row, "meta": meta})
                known_ids.add(pid)
                pos_counts[position] += 1
                created_count += 1
            academy[team] = retained
        return {"archived": archive_count, "created": created_count}

    def _setup_season(self, first=False):
        s = self.s
        s["season_done"] = False
        s["competition_awards"] = {}
        s["season_awards"] = None
        s["matches"] = []
        s["last_match_date"] = {}
        s["next_match_id"] = 1
        s["season_tiers"] = dict(s["team_tiers"])
        s["season_seed_order"] = list(s["seed_order"])
        s["season_stats"] = {pid: self._empty_stat() for ids in s["rosters"].values() for pid in ids}
        for team in s["rosters"]:
            for pid in s["rosters"][team]:
                s["dyn"].setdefault(pid, {"form": 0.0, "fit": 100.0})
        s["cup"] = {"bracket": {}, "winner": None, "runner_up": None, "seed_order": list(s["season_seed_order"])}
        seeds = list(s["season_seed_order"])
        s["cup"]["seed_rank"] = {team: i+1 for i, team in enumerate(seeds)}
        s["c1"] = {"seed_order": seeds, "playin": {}, "groups": {}, "semi_ties": [], "champion": None,
                   "runner_up": None, "playin_qualified": {}, "group_tables": {}, "bracket": {}}
        slots = []
        base = _safe_date(self.config["calendar"].get("start_date", "2026-08-01"))
        year_delta = int(s["season"]) - base.year
        try: base = base.replace(year=base.year + year_delta)
        except ValueError: base = base.replace(year=base.year + year_delta, day=28)
        calendar_cfg = self.config["calendar"]
        premier_weekday = _weekday_number(calendar_cfg.get("league_day", "sat"), "league_day")
        fc2_weekday = _weekday_number(calendar_cfg.get("fc2_day", "fri"), "fc2_day")
        fc1_weekday = _weekday_number(calendar_cfg.get("fc1_day", "sun"), "fc1_day")
        cup_weekday = _weekday_number(calendar_cfg.get("cup_day", "tue"), "cup_day")
        c1_weekday = _weekday_number(calendar_cfg.get("midweek_day", "wed"), "midweek_day")
        league_anchor = _on_or_after(base, premier_weekday)
        # Round one includes a Friday FC2 match. If that Friday falls before
        # start_date (e.g. start_date is Saturday), move the whole first cycle
        # forward one week so no fixture predates the configured season start.
        if _date_in_week(league_anchor, 0, fc2_weekday) < base:
            league_anchor += timedelta(days=7)
        s["season_start_date"] = league_anchor.isoformat()
        league_offsets = calendar_cfg.get("league_week_offsets", [0,1,2,3,5,6,7,8,10,11,12,13,15,17])
        fc2_offsets = calendar_cfg.get("fc2_week_offsets", [0,2,4,5,7,8,9,10,12,13,14,15,17,19])
        cup_offsets = calendar_cfg.get("cup_week_offsets", [5,10,15,17,33])
        c1_offsets = calendar_cfg.get("c1_week_offsets", [1,3,6,11,16,20,37,39,41])
        phase2_offsets = calendar_cfg.get("league_phase2_week_offsets", [30,31,32,33,34,35])
        tier_weekdays = {"fc2": fc2_weekday, "premier": premier_weekday, "fc1": fc1_weekday}
        tier_week_offsets = {"fc2": fc2_offsets, "premier": league_offsets, "fc1": league_offsets}
        tier_labels = {"fc2": TIER_LABELS["fc2"], "premier": TIER_LABELS["premier"], "fc1": TIER_LABELS["fc1"]}
        # Separate calendar slots let the three domestic leagues own their
        # requested weekly kick-off days rather than sharing one Saturday slot.
        for round_no in range(1, 15):
            for tier in ("fc2", "premier", "fc1"):
                week = tier_week_offsets[tier][round_no - 1]
                dt = _date_in_week(league_anchor, week, tier_weekdays[tier])
                slots.append({"id": f"L{round_no:02d}_{tier.upper()}", "date": dt.isoformat(),
                              "kind": "league", "tier": tier, "round": round_no,
                              "label": f"{tier_labels[tier]} · Vòng {round_no}", "played": False,
                              "fixture_result_ids": {}, "match_ids": []})
        cup_labels = ["Vòng 1", "Vòng 16", "Tứ kết", "Bán kết", "Chung kết"]
        cup_kinds = ["cup_r1", "cup_r16", "cup_qf", "cup_sf", "cup_final"]
        premier_r3_date = _date_in_week(league_anchor, phase2_offsets[2], premier_weekday)
        cup_final_date = premier_r3_date + timedelta(days=3)
        if cup_final_date.weekday() != cup_weekday:
            raise ValueError("Major Cup Final phải nằm đúng cup_day và cách Premier GĐ2 vòng 3 ba ngày.")
        for i, (week, kind, label) in enumerate(zip(cup_offsets, cup_kinds, cup_labels), 1):
            dt = cup_final_date if kind == "cup_final" else _date_in_week(league_anchor, week, cup_weekday)
            slots.append({"id": f"CUP{i}", "date": dt.isoformat(), "kind": kind, "round": i,
                          "label": ("Major Cup - Chung kết" if kind == "cup_final" else f"Cup - {label}"),
                          "played": False})
        c1_kinds = ["c1_pi_r1", "c1_pi_mid", "c1_pi_decider", "c1_group1", "c1_group2", "c1_group3", "c1_semi1", "c1_semi2", "c1_final"]
        c1_labels = ["C1 - Play-in vòng 1", "C1 - Nhánh thắng/thua", "C1 - Trận quyết định", "C1 - Bảng vòng 1",
                     "C1 - Bảng vòng 2", "C1 - Bảng vòng 3", "C1 - Bán kết lượt đi", "C1 - Bán kết lượt về", "C1 - Chung kết"]
        for i, (week, kind, label) in enumerate(zip(c1_offsets, c1_kinds, c1_labels), 1):
            slots.append({"id": f"C1{i}", "date": _date_in_week(league_anchor, week, c1_weekday).isoformat(),
                          "kind": kind, "round": i, "label": label, "played": False})
        for i, week in enumerate(phase2_offsets, 1):
            slots.append({"id": f"P2R{i:02d}", "date": _date_in_week(league_anchor, week, premier_weekday).isoformat(),
                          "kind": "league_phase2", "phase2_tier": "premier", "round": i,
                          "label": f"Premier · Giai đoạn 2 · Vòng {i}", "played": False,
                          "fixture_result_ids": {}, "match_ids": []})
            if i <= 2:
                for tier in ("fc2", "fc1"):
                    slots.append({"id": f"P2{tier.upper()}R{i:02d}",
                                  "date": _date_in_week(league_anchor, week, tier_weekdays[tier]).isoformat(),
                                  "kind": "league_phase2", "phase2_tier": tier, "round": i,
                                  "label": f"{tier_labels[tier]} · Play-off thăng hạng · Vòng {i}",
                                  "played": False, "fixture_result_ids": {}, "match_ids": []})
        for round_no, week_index, leg_label in ((4, 3, "Lượt đi"), (6, 5, "Lượt về")):
            week = phase2_offsets[week_index]
            slots.append({"id": f"P2FC1R{round_no:02d}",
                          "date": _date_in_week(league_anchor, week, fc1_weekday).isoformat(),
                          "kind": "league_phase2", "phase2_tier": "fc1", "round": round_no,
                          "label": f"FC1 · Play-off trụ hạng · {leg_label}", "played": False,
                          "fixture_result_ids": {}, "match_ids": []})
        s["slots"] = sorted(slots, key=lambda x: (x["date"], x["id"]))
        s["calendar_schedule_version"] = 5
        # C1 Final is intentionally the final dated slot of the season.
        if max(s["slots"], key=lambda item: item["date"]).get("kind") != "c1_final":
            raise ValueError("C1 Final phải là trận cuối cùng trong lịch mùa giải.")
        validate_calendar_slots(s["slots"], calendar_cfg.get("min_rest_days", 3))
        self._update_calendar_validation_metadata()
        s["slot_cursor"] = 0
        s["league_fixtures"] = {}
        s["league_phase2"] = {
            "initialized": False,
            "premier": {"champion_teams": [], "challenge_teams": [], "champion_rounds": [],
                        "challenge_rounds": [], "match_ids": {"champion": [[] for _ in range(6)],
                                                                  "challenge": [[] for _ in range(6)]},
                        "phase1_table": []},
            "promotion_playoffs": {tier: {"teams": {}, "semi_match": None, "final_match": None, "winner": None}
                                   for tier in ("fc1", "fc2")},
            "relegation_playoff": {"teams": {}, "leg1_match": None, "leg2_match": None,
                                   "winner": None, "loser": None},
        }
        for tier in TIERS:
            teams = sorted([t for t, tr in s["season_tiers"].items() if tr == tier], key=lambda t: seeds.index(t))
            s["league_fixtures"][tier] = make_double_round_robin(teams, self.seed + s["season"]*7 + TIERS.index(tier))
        # Seed C1 cho mùa mới lấy từ thành tích mùa trước; mùa đầu lấy rating.
        direct_n = int(self.config["c1"].get("direct", 4))
        playin_n = int(self.config["c1"].get("playin", 8))
        s["c1"]["direct_teams"] = seeds[:direct_n]
        s["c1"]["playin_seed_teams"] = seeds[direct_n:direct_n+playin_n]
        s["c1"]["active_competition"] = ""
        s["last_news"] = [f"Mùa {s['season']}/{s['season']+1}: lịch mới đã được tạo."]

    def _current_dyn(self, team):
        return {pid: self.s["dyn"].get(pid, {"form": 0.0, "fit": 100.0}) for pid in self.s["rosters"][team]}

    def team_players(self, team):
        return [self.s["players"][pid] for pid in self.s["rosters"][team] if pid in self.s["players"]]

    def _next_rng(self, match_id):
        seed = self.seed + int(self.s["season"])*1000003 + int(match_id)*9176
        return random.Random(seed)

    def _assert_match_rest(self, home, away, match_date):
        """Chặn lịch thực tế nếu một CLB phải đá sát hơn ngưỡng trong config."""
        try:
            current = date.fromisoformat(str(match_date))
        except ValueError as exc:
            raise ValueError(f"Ngày trận đấu không hợp lệ: {match_date!r}") from exc
        # Dùng quy tắc đã gắn với lịch đang chạy; sửa config giữa mùa không
        # được âm thầm thay đổi điều kiện của một lịch đã được tạo trước đó.
        minimum = int(self.s.get("calendar_validation", {}).get(
            "min_rest_days", self.config["calendar"].get("min_rest_days", 3)))
        last_dates = self.s.setdefault("last_match_date", {})
        for team in (home, away):
            previous_raw = last_dates.get(team)
            if not previous_raw:
                continue
            previous = date.fromisoformat(str(previous_raw))
            gap = (current - previous).days
            if gap < minimum:
                raise RuntimeError(
                    f"Lịch xung đột: {team} đá ngày {previous.isoformat()} rồi lại đá {current.isoformat()} "
                    f"(cách {gap} ngày, tối thiểu {minimum}). Không mô phỏng trận này; hãy điều chỉnh config.json."
                )

    def _append_match_data(self, comp, round_label, home, away, match_date, data, match_id,
                           neutral=False, stage=None):
        """Commit an already-calculated result to career state exactly once."""
        s = self.s
        self._assert_match_rest(home, away, match_date)
        match_id = int(match_id)
        if match_id != int(s["next_match_id"]):
            raise RuntimeError("Dữ liệu trận xem trước đã cũ; hãy mở lại trận để mô phỏng.")
        s["next_match_id"] = match_id + 1
        match = {"id": match_id, "season": s["season"], "comp": comp, "round": round_label, "stage": stage or round_label,
                 "date": match_date, "neutral": bool(neutral), "played": True, **data}
        # Internal Elo: phụ trợ xếp hạng nội bộ, không thay thế điểm liên đoàn/Cup.
        elo = s["elo"]
        eh, ea = float(elo[home]), float(elo[away])
        elo_cfg = self.config.get("engine", {})
        expected_home = 1.0 / (1.0 + 10 ** (-((eh + (0 if neutral else float(elo_cfg.get("elo_home_advantage",45)))) - ea) / 400.0))
        match["expected_home"] = expected_home
        expected = 1.0 / (1.0 + 10 ** (-((eh + (0 if neutral else float(elo_cfg.get("elo_home_advantage",45)))) - ea) / 400.0))
        if data["hg"] > data["ag"]: actual = 1.0
        elif data["hg"] < data["ag"]: actual = 0.0
        else: actual = 0.5
        margin = min(float(elo_cfg.get("elo_margin_cap",1.5)), 1.0 + float(elo_cfg.get("elo_margin_bonus",.08))*abs(data["hg"]-data["ag"]))
        change = float(elo_cfg.get("elo_k",18.0)) * margin * (actual - expected)
        elo[home] = round(eh + change, 2); elo[away] = round(ea - change, 2)
        if match.get("match_engine") == "enhanced":
            self._update_enhanced_match_state(match)
        else:
            self._update_player_match_stats(match)
        s["matches"].append(match)
        s["last_match_date"][home] = str(match_date)
        s["last_match_date"][away] = str(match_date)
        return match

    def _record_match(self, comp, round_label, home, away, match_date, neutral=False, force_winner=False,
                      aggregate_before=None, stage=None):
        s = self.s
        self._assert_match_rest(home, away, match_date)
        match_id = int(s["next_match_id"])
        rng = self._next_rng(match_id)
        data = self._enhanced_match_data(home, away, match_date, neutral, force_winner,
                                         aggregate_before, match_id, comp)
        if data is None:
            data = eng.simulate_match(home, away, self.team_players(home), self.team_players(away), rng=rng,
                                      neutral=neutral, force_winner=force_winner, aggregate_before=aggregate_before,
                                      home_dyn=self._current_dyn(home), away_dyn=self._current_dyn(away),
                                      engine_cfg=self.config.get("engine", {}))
        return self._append_match_data(comp, round_label, home, away, match_date, data, match_id,
                                       neutral=neutral, stage=stage)

    def league_slot_fixtures(self, slot=None):
        """Return the unplayed fixtures scheduled in one league slot."""
        slot = slot or self.next_slot()
        if not slot or slot.get("kind") != "league":
            return []
        round_index = int(slot["round"]) - 1
        fixtures = []
        existing = {(m.get("stage"), m.get("home"), m.get("away"))
                    for m in self.s.get("matches", []) if m.get("played")}
        tiers = (slot["tier"],) if slot.get("tier") in TIERS else TIERS
        for tier in tiers:
            stage = f"league_{tier}_{round_index + 1}"
            comp = TIER_LABELS[tier]
            for home, away in self.s["league_fixtures"][tier][round_index]:
                if (stage, home, away) in existing:
                    continue
                fixtures.append({
                    "slot_id": slot["id"], "season": int(self.s["season"]), "tier": tier,
                    "date": slot["date"], "comp": comp, "round": f"Vòng {round_index + 1}",
                    "stage": stage, "home": home, "away": away, "neutral": False,
                    "force_winner": False,
                })
        return fixtures

    def _build_c1_groups(self):
        """Build C1 group data without mutating career state (used by live previews)."""
        c1 = self.s["c1"]
        quals = c1.get("playin_qualified", {})
        seed = c1["seed_order"]
        A = [seed[0], seed[3], quals.get("X", {}).get("winner"), quals.get("Y", {}).get("decider")]
        B = [seed[1], seed[2], quals.get("Y", {}).get("winner"), quals.get("X", {}).get("decider")]
        if any(t is None for t in A + B):
            raise RuntimeError("Thiếu đội vượt qua play-in để tạo bảng C1.")
        result = {}
        for label, teams in (("A", A), ("B", B)):
            rounds = make_double_round_robin(
                teams, self.seed + int(self.s["season"]) * 199 + (0 if label == "A" else 1)
            )[:3]
            result[label] = {"teams": teams, "rounds": rounds, "match_ids": [[], [], []]}
        return result

    def slot_fixture_specs(self, slot=None, include_played=False, *, allow_future=False):
        """Return read-only fixture projections for a scheduled slot.

        By default this remains restricted to the next slot, preserving the live-match
        safety boundary. ``allow_future=True`` is intended only for calendar/overview
        displays; it never previews or commits a future fixture.
        """
        slot = slot or self.next_slot()
        current = self.next_slot()
        if not slot or slot.get("played"):
            return []
        if not allow_future and (not current or slot.get("id") != current.get("id")):
            return []
        kind = slot["kind"]
        result_ids = slot.get("fixture_result_ids", {})
        specs = []

        def add(key, comp, round_label, home, away, *, neutral=False, force_winner=False,
                stage=None, aggregate_before=None, **meta):
            if not include_played and key in result_ids:
                return
            specs.append({
                "spec_key": key, "slot_id": slot["id"], "season": int(self.s["season"]),
                "date": slot["date"], "comp": comp, "round": round_label,
                "home": home, "away": away, "neutral": bool(neutral),
                "force_winner": bool(force_winner), "aggregate_before": aggregate_before,
                "stage": stage or round_label, "_meta": meta,
            })

        # League: each of the 12 fixtures can be opened and committed independently.
        if kind == "league":
            round_index = int(slot["round"]) - 1
            tiers = (slot["tier"],) if slot.get("tier") in TIERS else TIERS
            for tier in tiers:
                stage = f"league_{tier}_{round_index + 1}"
                for index, (home, away) in enumerate(self.s["league_fixtures"][tier][round_index]):
                    add(f"league:{tier}:{index}", TIER_LABELS[tier], f"Vòng {round_index + 1}",
                        home, away, stage=stage, tier=tier, index=index)
            return specs
        if kind == "league_phase2":
            return self._phase2_fixture_specs(slot, include_played=include_played)

        # Domestic Cup.
        if kind.startswith("cup_"):
            cup = self.s["cup"]
            seeds = cup["seed_order"]
            if kind == "cup_r1":
                pairs = [(9, 24), (10, 23), (11, 22), (12, 21), (13, 20), (14, 19), (15, 18), (16, 17)]
                bracket_key, comp, label, force, neutral = "r1", "Cup", "Vòng 1", True, False
                teams = [(seeds[a-1], seeds[b-1]) for a, b in pairs]
            elif kind == "cup_r16":
                winners = self._cup_winners("r1")
                pairs = [(1, winners[7]), (8, winners[0]), (4, winners[4]), (5, winners[3]),
                         (3, winners[5]), (6, winners[2]), (7, winners[1]), (2, winners[6])]
                bracket_key, comp, label, force, neutral = "r16", "Cup", "Vòng 16", True, False
                teams = [(seeds[rank-1], opponent) for rank, opponent in pairs]
            elif kind == "cup_qf":
                winners = self._cup_winners("r16")
                bracket_key, comp, label, force, neutral = "qf", "Cup", "Tứ kết", True, False
                teams = [(winners[i], winners[j]) for i, j in ((0, 1), (2, 3), (4, 5), (6, 7))]
            elif kind == "cup_sf":
                winners = self._cup_winners("qf")
                bracket_key, comp, label, force, neutral = "sf", "Cup", "Bán kết", True, False
                teams = [(winners[i], winners[j]) for i, j in ((0, 1), (2, 3))]
            elif kind == "cup_final":
                winners = self._cup_winners("sf")
                bracket_key, comp, label, force = "final", "Cup", "Chung kết", True
                neutral = bool(self.config["cup"].get("final_neutral", True))
                teams = [(winners[0], winners[1])] if len(winners) == 2 else []
            else:
                teams = []
                bracket_key, comp, label, force, neutral = "", "Cup", slot["label"], True, False
            counts = {"r1": 8, "r16": 8, "qf": 4, "sf": 2, "final": 1}
            for index, (team_a, team_b) in enumerate(teams):
                home, away = (self._seeded_home_away(team_a, team_b)
                              if not neutral else (team_a, team_b))
                add(f"cup:{bracket_key}:{index}", comp, label, home, away, neutral=neutral,
                    force_winner=force, stage=f"cup_{bracket_key}", bucket="cup",
                    bracket_key=bracket_key, index=index, expected_count=counts.get(bracket_key, len(teams)))
            return specs

        # Continental Cup (C1).
        c1 = self.s["c1"]
        seed = c1["seed_order"]
        if kind == "c1_pi_r1":
            groups = c1.get("playin_groups") or {
                "X": [seed[4], seed[7], seed[8], seed[11]],
                "Y": [seed[5], seed[6], seed[9], seed[10]],
            }
            for group_name in ("X", "Y"):
                teams = groups[group_name]
                for index, (team_a, team_b) in enumerate(((teams[0], teams[3]), (teams[1], teams[2]))):
                    home, away = self._seeded_home_away(team_a, team_b)
                    add(f"c1:pi:{group_name}:r1:{index}", "C1 Play-in", f"Bảng {group_name} - Vòng 1",
                        home, away, force_winner=True, stage=f"c1_pi_{group_name}_r1",
                        bucket="c1_pi_r1", group=group_name, index=index, teams=list(teams))
        elif kind == "c1_pi_mid":
            for group_name in ("X", "Y"):
                entry = c1.get("playin", {}).get(group_name)
                if not entry or len(entry.get("r1", [])) != 2:
                    raise RuntimeError(f"Thiếu kết quả vòng mở màn C1 bảng {group_name}.")
                first = [self.match_by_id(mid) for mid in entry["r1"]]
                winners = [m["winner"] for m in first]
                losers = [m["away"] if m["winner"] == m["home"] else m["home"] for m in first]
                for field, stage_key, pair, label in (
                    ("winner_match", "wb", winners, "Nhánh thắng"),
                    ("loser_match", "lb", losers, "Nhánh thua"),
                ):
                    home, away = self._seeded_home_away(pair[0], pair[1])
                    add(f"c1:pi:{group_name}:mid:{stage_key}", "C1 Play-in",
                        f"Bảng {group_name} - {label}", home, away, force_winner=True,
                        stage=f"c1_pi_{group_name}_{stage_key}", bucket="c1_pi_mid",
                        group=group_name, field=field)
        elif kind == "c1_pi_decider":
            for group_name in ("X", "Y"):
                entry = c1.get("playin", {}).get(group_name, {})
                if not entry.get("winner_match") or not entry.get("loser_match"):
                    raise RuntimeError(f"Thiếu kết quả nhánh thắng/thua C1 bảng {group_name}.")
                winner_match = self.match_by_id(entry["winner_match"])
                loser_match = self.match_by_id(entry["loser_match"])
                wb_loser = (winner_match["away"] if winner_match["winner"] == winner_match["home"]
                            else winner_match["home"])
                lb_winner = loser_match["winner"]
                home, away = self._seeded_home_away(wb_loser, lb_winner)
                add(f"c1:pi:{group_name}:decider", "C1 Play-in", f"Bảng {group_name} - Trận quyết định",
                    home, away, force_winner=True, stage=f"c1_pi_{group_name}_decider",
                    bucket="c1_pi_decider", group=group_name)
        elif kind in ("c1_group1", "c1_group2", "c1_group3"):
            round_index = int(kind[-1]) - 1
            groups = c1.get("groups") or self._build_c1_groups()
            for group_name in ("A", "B"):
                group = groups[group_name]
                for index, (home, away) in enumerate(group["rounds"][round_index]):
                    add(f"c1:group:{group_name}:{round_index + 1}:{index}", "C1",
                        f"Bảng {group_name} - Vòng {round_index + 1}", home, away,
                        stage=f"c1_group_{group_name}_{round_index + 1}", bucket="c1_group",
                        group=group_name, round_index=round_index, index=index)
        elif kind == "c1_semi1":
            table_a, table_b = self.c1_group_table("A"), self.c1_group_table("B")
            if len(table_a) < 2 or len(table_b) < 2:
                raise RuntimeError("Bảng C1 chưa hoàn tất.")
            pairs = [(table_a[0]["team"], table_b[1]["team"]),
                     (table_b[0]["team"], table_a[1]["team"])]
            for index, (winner_group, runner_group) in enumerate(pairs):
                add(f"c1:semi1:{index}", "C1", f"Bán kết {index + 1} - Lượt đi",
                    runner_group, winner_group, stage=f"c1_semi{index + 1}_leg1",
                    bucket="c1_semi1", index=index, group_winner=winner_group,
                    group_runner=runner_group)
        elif kind == "c1_semi2":
            for index, tie in enumerate(c1.get("semi_ties", [])):
                if not tie.get("leg1"):
                    raise RuntimeError(f"Thiếu lượt đi bán kết C1 số {index + 1}.")
                leg1 = self.match_by_id(tie["leg1"])
                aggregate = (leg1["ag"], leg1["hg"])
                add(f"c1:semi2:{index}", "C1", f"Bán kết {index + 1} - Lượt về",
                    tie["group_winner"], tie["group_runner"], force_winner=True,
                    aggregate_before=aggregate, stage=f"c1_semi{index + 1}_leg2",
                    bucket="c1_semi2", index=index)
        elif kind == "c1_final":
            winners = []
            for tie in c1.get("semi_ties", []):
                if not tie.get("leg2"):
                    raise RuntimeError("Bán kết C1 chưa hoàn tất.")
                winners.append(self.match_by_id(tie["leg2"])["winner"])
            if len(winners) == 2:
                neutral = bool(self.config["c1"].get("final_neutral", True))
                add("c1:final:0", "C1", "Chung kết", winners[0], winners[1],
                    neutral=neutral, force_winner=True, stage="c1_final",
                    bucket="c1_final", index=0)
        return specs

    def preview_slot_fixture(self, spec_key, match_id=None):
        """Prepare a read-only Live Match preview for one unplayed fixture in the next slot.

        ``match_id`` may be supplied for a fixture prepared as part of a simultaneous
        kickoff batch. It must be the next predicted ID; no career state is mutated.
        """
        slot = self.next_slot()
        pending = self.slot_fixture_specs(slot, include_played=False)
        spec = next((item for item in pending if item["spec_key"] == spec_key), None)
        if spec is None:
            raise RuntimeError("Chỉ có thể mở trận chưa đá thuộc slot gần nhất.")
        self._assert_match_rest(spec["home"], spec["away"], spec["date"])
        match_id = int(self.s["next_match_id"] if match_id is None else match_id)
        if match_id < int(self.s["next_match_id"]):
            raise RuntimeError("ID xem trước trận không hợp lệ.")
        data = self._enhanced_match_data(spec["home"], spec["away"], spec["date"],
                                         spec["neutral"], spec["force_winner"],
                                         spec.get("aggregate_before"), match_id, spec["comp"])
        if not data or not data.get("live_bundle"):
            raise RuntimeError("Live Match yêu cầu match_engine = 'enhanced' trong config.json.")
        return {
            **spec,
            "match_id": match_id,
            "data": data,
            "slot_fixtures": self.slot_fixture_specs(slot, include_played=True),
            "slot_result_ids": dict(slot.get("fixture_result_ids", {})),
        }

    def preview_slot_companions(self, selected_preview):
        """Prepare unplayed fixtures of the same competition in the selected kickoff slot.

        Other competitions can share a calendar slot, but are not part of this live
        simultaneous batch. Companion IDs are reserved in deterministic order.
        """
        slot = self.next_slot()
        if not slot or slot.get("id") != selected_preview.get("slot_id"):
            raise RuntimeError("Slot đã thay đổi; không thể chuẩn bị các trận cùng giờ.")
        if int(selected_preview.get("match_id", -1)) != int(self.s["next_match_id"]):
            raise RuntimeError("Preview trận chính đã cũ; hãy mở lại trận.")
        pending = self.slot_fixture_specs(slot, include_played=False)
        if not any(x["spec_key"] == selected_preview.get("spec_key") for x in pending):
            raise RuntimeError("Trận chính không còn thuộc slot đang chờ.")
        # Chỉ các fixture thuộc đúng giải của trận được chọn mới chạy đồng thời.
        # Các hạng đấu khác có thể dùng chung slot lịch nhưng không cùng loạt trận.
        selected_competition = selected_preview.get("comp")
        companions = []
        predicted_id = int(selected_preview["match_id"]) + 1
        for spec in pending:
            if spec["spec_key"] == selected_preview["spec_key"]:
                continue
            if spec.get("comp") != selected_competition:
                continue
            companions.append(self.preview_slot_fixture(spec["spec_key"], match_id=predicted_id))
            predicted_id += 1
        return companions

    def slot_concurrent_scoreboard(self, preview):
        """Return same-competition fixtures sharing the selected kickoff slot.

        Other competitions may share the calendar slot, but are intentionally excluded.
        Previously played fixtures in this competition remain visible with their FT score.
        """
        selected_competition = preview.get("comp")
        specs = [
            spec for spec in (preview.get("slot_fixtures") or [])
            if spec.get("comp") == selected_competition
        ]
        result_ids = preview.get("slot_result_ids") or {}
        matches_by_key = {
            (m.get("stage"), str(m.get("date", "")), m.get("home"), m.get("away")): m
            for m in self.s.get("matches", []) if m.get("played")
        }
        rows = []
        players = self.s.get("players", {})

        def goal_details(match, side):
            details = []
            wanted = "home" if side == "h" else "away"
            for goal in (match.get("goals") or []):
                if goal.get("side") != wanted:
                    continue
                details.append({
                    "player": goal.get("scorer") or "Cầu thủ",
                    "minute": f"{int(goal.get('minute', 0))}'",
                    "pen": bool(goal.get("pen")),
                })
            return details

        def red_details(match, key):
            details = []
            for card in (match.get(key) or []):
                if card.get("c") not in ("r", "yr"):
                    continue
                pid = card.get("p")
                player = players.get(pid, {})
                name = (player.get("common_name") or
                        (str(player.get("first_name", "") + " " + player.get("last_name", "")).strip()) or
                        str(pid or "Cầu thủ"))
                minute = card.get("m")
                details.append(f"{name} {minute}'" if minute is not None else name)
            return details

        for spec in specs:
            if spec.get("spec_key") == preview.get("spec_key"):
                continue  # The active match is already shown in the main scoreboard.
            result_id = result_ids.get(spec.get("spec_key"))
            match = None
            if result_id is not None:
                try:
                    match = self.match_by_id(result_id)
                except (StopIteration, TypeError, ValueError):
                    match = None
            if match is None:
                key = (spec.get("stage"), str(spec.get("date", "")), spec.get("home"), spec.get("away"))
                match = matches_by_key.get(key)
            if match:
                rows.append({
                    **spec, "played": True,
                    "score": f"{int(match.get('hg', 0))}–{int(match.get('ag', 0))}",
                    "goal_home": goal_details(match, "h"),
                    "goal_away": goal_details(match, "a"),
                    "red_home": red_details(match, "cards_h"),
                    "red_away": red_details(match, "cards_a"),
                    "status": "FT",
                })
            else:
                rows.append({
                    **spec, "played": False, "score": "—",
                    "goal_home": [], "goal_away": [],
                    "red_home": [], "red_away": [], "status": "Chưa đá",
                })
        return rows

    def _register_slot_fixture(self, slot, spec, match):
        """Attach a committed single-match result to its Cup/C1 bracket or league slot."""
        key = spec["spec_key"]
        result_ids = slot.setdefault("fixture_result_ids", {})
        if key in result_ids:
            raise RuntimeError("Trận này đã được ghi nhận trong slot.")
        result_ids[key] = int(match["id"])
        slot.setdefault("match_ids", []).append(int(match["id"]))
        meta = spec.get("_meta", {})
        bucket = meta.get("bucket")

        if bucket == "premier_phase2":
            premier = self.s["league_phase2"]["premier"]
            ids = premier["match_ids"][meta["group"]][int(meta["round_index"])]
            if int(match["id"]) not in ids:
                ids.append(int(match["id"]))

        elif bucket == "promotion_playoff":
            entry = self.s["league_phase2"]["promotion_playoffs"][meta["tier"]]
            if meta["stage_key"] == "semi":
                entry["semi_match"] = int(match["id"])
            elif meta["stage_key"] == "final":
                entry["final_match"] = int(match["id"])
                entry["winner"] = match["winner"]

        elif bucket == "fc1_relegation":
            entry = self._fc1_relegation_playoff_entry()
            if meta["leg_key"] == "leg1":
                entry["leg1_match"] = int(match["id"])
            elif meta["leg_key"] == "leg2":
                entry["leg2_match"] = int(match["id"])
                entry["winner"] = match.get("winner")
                teams = entry.get("teams", {})
                entry["loser"] = (teams.get("seventh") if entry["winner"] == teams.get("sixth")
                                  else teams.get("sixth")) if entry["winner"] else None

        elif bucket == "cup":
            cup = self.s["cup"]
            bracket_key = meta["bracket_key"]
            count = int(meta["expected_count"])
            ordered = [result_ids.get(f"cup:{bracket_key}:{index}") for index in range(count)]
            cup["bracket"][bracket_key] = [int(mid) for mid in ordered if mid is not None]
            if bracket_key == "final":
                cup["winner"] = match["winner"]
                cup["runner_up"] = (match["away"] if match["winner"] == match["home"] else match["home"])

        elif bucket == "c1_pi_r1":
            c1 = self.s["c1"]
            group_name, index = meta["group"], int(meta["index"])
            groups = c1.setdefault("playin_groups", {
                "X": [c1["seed_order"][4], c1["seed_order"][7], c1["seed_order"][8], c1["seed_order"][11]],
                "Y": [c1["seed_order"][5], c1["seed_order"][6], c1["seed_order"][9], c1["seed_order"][10]],
            })
            entry = c1["playin"].setdefault(group_name, {
                "teams": list(meta["teams"]), "r1": [None, None], "winner_match": None,
                "loser_match": None, "decider": None, "winner_qualifier": None,
                "decider_qualifier": None,
            })
            r1_ids = list(entry.get("r1", [None, None]))
            while len(r1_ids) < 2:
                r1_ids.append(None)
            r1_ids[index] = int(match["id"])
            entry["r1"] = r1_ids

        elif bucket == "c1_pi_mid":
            entry = self.s["c1"]["playin"][meta["group"]]
            entry[meta["field"]] = int(match["id"])

        elif bucket == "c1_pi_decider":
            c1 = self.s["c1"]
            entry = c1["playin"][meta["group"]]
            entry["decider"] = int(match["id"])
            winner_match = self.match_by_id(entry["winner_match"])
            entry["winner_qualifier"] = winner_match["winner"]
            entry["decider_qualifier"] = match["winner"]
            if all(c1["playin"].get(name, {}).get("decider") for name in ("X", "Y")):
                c1["playin_qualified"] = {
                    name: {"winner": c1["playin"][name]["winner_qualifier"],
                           "decider": c1["playin"][name]["decider_qualifier"]}
                    for name in ("X", "Y")
                }

        elif bucket == "c1_group":
            c1 = self.s["c1"]
            if not c1.get("groups"):
                self._setup_c1_groups()
            group = c1["groups"][meta["group"]]
            ids = group["match_ids"][int(meta["round_index"])]
            if int(match["id"]) not in ids:
                ids.append(int(match["id"]))

        elif bucket == "c1_semi1":
            c1 = self.s["c1"]
            if not c1.get("semi_ties"):
                all_specs = self.slot_fixture_specs(slot, include_played=True)
                by_index = sorted((x for x in all_specs if x.get("_meta", {}).get("bucket") == "c1_semi1"),
                                  key=lambda x: x["_meta"]["index"])
                c1["semi_ties"] = [{"group_winner": item["_meta"]["group_winner"],
                                    "group_runner": item["_meta"]["group_runner"],
                                    "leg1": None, "leg2": None} for item in by_index]
            c1["semi_ties"][int(meta["index"])]["leg1"] = int(match["id"])
            bracket = c1.setdefault("bracket", {})
            bracket["semis"] = [result_ids.get(f"c1:semi1:{i}") for i in range(2)
                                if result_ids.get(f"c1:semi1:{i}") is not None]

        elif bucket == "c1_semi2":
            c1 = self.s["c1"]
            c1["semi_ties"][int(meta["index"])]["leg2"] = int(match["id"])
            if all(tie.get("leg2") for tie in c1["semi_ties"]):
                bracket = c1.setdefault("bracket", {})
                bracket["semis"] = ([tie["leg1"] for tie in c1["semi_ties"]] +
                                    [tie["leg2"] for tie in c1["semi_ties"]])

        elif bucket == "c1_final":
            c1 = self.s["c1"]
            c1.setdefault("bracket", {})["final"] = int(match["id"])
            c1["champion"] = match["winner"]
            c1["runner_up"] = (match["away"] if match["winner"] == match["home"] else match["home"])

    def simulate_slot_competition(self, slot_id, competition, persist=True):
        """Quick-simulate the remaining fixtures of one competition in the current slot.

        This intentionally does not spill into other league tiers/competitions sharing
        the same calendar slot. It is used by the GUI's "match + this round" option.
        """
        slot = self.next_slot()
        if not slot or slot.get("id") != slot_id:
            raise RuntimeError("Slot đã thay đổi; không thể mô phỏng loạt trận đã chọn.")
        specs = [item for item in self.slot_fixture_specs(slot, include_played=False)
                 if item.get("comp") == competition]
        if not specs:
            raise RuntimeError(f"Không còn trận chưa đá thuộc {competition} trong slot này.")
        slot["interactive"] = True
        created = []
        for spec in specs:
            self._assert_match_rest(spec["home"], spec["away"], spec["date"])
            match = self._record_match(spec["comp"], spec["round"], spec["home"], spec["away"],
                                       spec["date"], neutral=spec["neutral"],
                                       force_winner=spec["force_winner"],
                                       aggregate_before=spec.get("aggregate_before"), stage=spec["stage"])
            self._register_slot_fixture(slot, spec, match)
            created.append(match)
        remaining = self.slot_fixture_specs(slot, include_played=False)
        if not remaining:
            if slot["kind"] == "c1_group3":
                self.s["c1"]["group_tables"] = {g: self.c1_group_table(g) for g in ("A", "B")}
            result = self._complete_slot(slot, persist=persist)
            result["matches"] = created
            result["competition_complete"] = True
            return result
        if persist:
            self.save()
        remaining_competitions = sorted({item.get("comp", "") for item in remaining})
        message = (f"Đã mô phỏng {len(created)} trận còn lại của {competition} · "
                   f"còn {len(remaining)} trận ở slot này thuộc: {', '.join(remaining_competitions)}.")
        return {"finished": False, "slot": slot, "matches": created,
                "competition_complete": True, "remaining": remaining, "message": message}

    def commit_slot_fixture(self, preview, persist=True):
        """Commit exactly one next-slot match after Live Match reaches full time."""
        slot = self.next_slot()
        if not slot or slot.get("id") != preview.get("slot_id"):
            raise RuntimeError("Lịch đã thay đổi; trận này không còn thuộc slot kế tiếp.")
        if int(preview.get("season", -1)) != int(self.s["season"]):
            raise RuntimeError("Mùa giải đã thay đổi; hãy mở lại trận.")
        if int(preview.get("match_id", -1)) != int(self.s["next_match_id"]):
            raise RuntimeError("Một trận khác vừa được ghi nhận; hãy mở lại trận này.")
        spec = next((item for item in self.slot_fixture_specs(slot, include_played=False)
                     if item["spec_key"] == preview.get("spec_key")), None)
        if spec is None:
            raise RuntimeError("Trận này đã đá hoặc không thuộc slot gần nhất.")
        data = preview.get("data")
        if not isinstance(data, dict) or not data.get("live_bundle"):
            raise ValueError("Thiếu dữ liệu Live Match để lưu kết quả.")
        match = self._append_match_data(
            spec["comp"], spec["round"], spec["home"], spec["away"], spec["date"],
            data, preview["match_id"], neutral=spec["neutral"], stage=spec["stage"])
        # Settle matchday/prize accounting as soon as a user-played match reaches
        # full time. _complete_slot may publish it again after concurrent fixtures;
        # the economy runtime deduplicates by match ID.
        self.economy_events.publish("MATCH_FINISHED", game=self, match=match)
        slot["interactive"] = True
        self._register_slot_fixture(slot, spec, match)
        self._record_match_news(match)
        all_specs = self.slot_fixture_specs(slot, include_played=True)
        remaining = self.slot_fixture_specs(slot, include_played=False)
        if not remaining and len(slot.get("fixture_result_ids", {})) >= len(all_specs):
            if slot["kind"] == "c1_group3":
                self.s["c1"]["group_tables"] = {g: self.c1_group_table(g) for g in ("A", "B")}
            self._complete_slot(slot, persist=persist)
            message = f"Đã hoàn tất trận {match['home']} {match['hg']}–{match['ag']} {match['away']} và chốt {slot['label']}."
        else:
            self.s["last_news"] = [f"{match['home']} {match['hg']}–{match['ag']} {match['away']} · "
                                    f"Đã đá {len(slot.get('fixture_result_ids', {}))}/{len(all_specs)} trận của {slot['label']}; "
                                    f"còn {len(remaining)} trận. Những trận khác vẫn chưa đá."]
            if persist:
                self.save()
            message = self.s["last_news"][0]
        return match, message

    def commit_prepared_slot_fixtures(self, previews, persist=True):
        """Commit deterministic companion fixtures after their synchronized live timelines.

        Previews must be ordered by predicted match ID. The caller commits the selected
        interactive match first, then passes its remaining same-kickoff previews here.
        The whole companion batch is saved once at the end.
        """
        previews = list(previews or [])
        if not previews:
            return {"matches": [], "message": "Không có trận cùng giờ nào cần ghi nhận."}
        slot = self.next_slot()
        if not slot or any(item.get("slot_id") != slot.get("id") for item in previews):
            raise RuntimeError("Các trận cùng giờ không còn thuộc slot hiện tại.")
        pending = {item["spec_key"] for item in self.slot_fixture_specs(slot, include_played=False)}
        expected_id = int(self.s["next_match_id"])
        for item in previews:
            if item.get("spec_key") not in pending:
                raise RuntimeError("Một trận cùng giờ đã được ghi nhận hoặc không còn chờ.")
            if int(item.get("match_id", -1)) != expected_id:
                raise RuntimeError("Thứ tự ID các trận cùng giờ không hợp lệ; hãy mở lại trận.")
            if not isinstance(item.get("data"), dict) or not item["data"].get("live_bundle"):
                raise RuntimeError("Thiếu dữ liệu mô phỏng của một trận cùng giờ.")
            expected_id += 1
            pending.remove(item["spec_key"])
        committed = []
        message = ""
        for item in previews:
            match, message = self.commit_slot_fixture(item, persist=False)
            committed.append(match)
        if persist:
            self.save()
        return {"matches": committed, "message": message,
                "competition_complete": True, "slot_complete": bool(slot.get("played"))}

    # Backward-compatible wrappers for the earlier league-only live-match API.
    def preview_league_fixture(self, tier, home, away):
        slot = self.next_slot()
        spec = next((item for item in self.slot_fixture_specs(slot, include_played=False)
                     if item.get("_meta", {}).get("tier") == tier and
                     item["home"] == home and item["away"] == away), None)
        if not spec:
            raise RuntimeError("Chỉ có thể mô phỏng trận chưa đá thuộc slot gần nhất.")
        return self.preview_slot_fixture(spec["spec_key"])

    def commit_league_fixture(self, preview):
        return self.commit_slot_fixture(preview)

    def _prune_news_history(self):
        """Keep only the current and immediately previous simulation week's news."""
        reference_date = match_news.reference_date_from_state(self.s)
        news = self.s.get("news", [])
        before = len(news) if isinstance(news, (list, dict)) else 0
        retained = match_news.retain_recent_news(news, reference_date)
        self.s["news"] = retained
        return before - len(retained)

    def _record_match_news(self, match):
        """Add one Overview article only while the fixture belongs to retained weeks."""
        if not isinstance(match, dict) or not match.get("played"):
            return None
        reference_date = match_news.reference_date_from_state(self.s)
        if not match_news.is_in_recent_two_week_window(match.get("date"), reference_date):
            return None
        news = self.s.setdefault("news", [])
        if isinstance(news, dict):
            news = list(news.values())
        season = int(match.get("season", self.s.get("season", 0)))
        match_id = int(match.get("id", 0))
        existing_index = next((index for index, item in enumerate(news)
                               if isinstance(item, dict)
                               and item.get("kind") == "match_summary"
                               and int(item.get("season", -1)) == season
                               and int(item.get("match_id", -1)) == match_id), None)
        if existing_index is not None:
            existing = news[existing_index]
            # Upgrade the previous plain scoreline-style recap to the editorial
            # one-article-per-match format without adding a duplicate entry.
            if int(existing.get("format_version", 1)) >= match_news.NEWS_FORMAT_VERSION:
                return existing
            item = match_news.match_summary(self, match)
            news[existing_index] = item
            self.s["news"] = match_news.retain_recent_news(news, reference_date)
            return item
        item = match_news.match_summary(self, match)
        news.append(item)
        self.s["news"] = match_news.retain_recent_news(news, reference_date)
        return item

    def _sync_match_news(self):
        """Backfill only currently retained weeks; never recreate deleted old news."""
        added = []
        reference_date = match_news.reference_date_from_state(self.s)
        for match in sorted(self.s.get("matches", []), key=lambda row: (str(row.get("date", "")), int(row.get("id", 0)))):
            if not match_news.is_in_recent_two_week_window(match.get("date"), reference_date):
                continue
            item = self._record_match_news(match)
            if item:
                added.append(item)
        self._prune_news_history()
        if added:
            self.s["last_news"] = [item["text"] for item in added[-8:]][::-1]

    def _publish_economy_daily_ticks(self, target_date: str, current_slot=None):
        """Publish one DATE tick for every calendar day since the last slot.

        Match simulation remains slot-based. This fills the calendar gaps so
        transfer-offer/contract deadlines and AI market decisions do not wait
        until the next match day. DATE_ADVANCED remains a separate slot event
        for existing monthly accounting consumers.
        """
        try:
            end_date = date.fromisoformat(str(target_date))
        except (TypeError, ValueError):
            return 0

        last_text = self.s.get("economy_last_date")
        if last_text:
            try:
                last_date = date.fromisoformat(str(last_text))
            except (TypeError, ValueError):
                last_date = None
        else:
            last_date = None

        if last_date is None:
            prior_dates = []
            for row in self.s.get("slots", []):
                if row is current_slot or not row.get("played"):
                    continue
                try:
                    prior_dates.append(date.fromisoformat(str(row.get("date", ""))))
                except (TypeError, ValueError):
                    continue
            if prior_dates:
                last_date = max(prior_dates)
            else:
                configured_start = _safe_date(self.config.get("calendar", {}).get("start_date", "2026-08-01"))
                delta = int(self.s.get("season", configured_start.year)) - configured_start.year
                try:
                    configured_start = configured_start.replace(year=configured_start.year + delta)
                except ValueError:
                    configured_start = configured_start.replace(year=configured_start.year + delta, day=28)
                last_date = configured_start - timedelta(days=1)

        gap = (end_date - last_date).days
        if gap <= 0:
            # Older saves can have the slot marked played before daily ticks
            # were introduced. Do not rewind the persisted calendar cursor.
            return 0
        if gap > 400:
            raise ValueError(f"Khoảng daily tick vượt giới hạn an toàn 400 ngày: {gap} ngày.")

        published = 0
        cursor = last_date + timedelta(days=1)
        while cursor <= end_date:
            day_text = cursor.isoformat()
            player_health.advance_medical_day(self.s, day_text)
            self.economy_events.publish("DAY_ADVANCED", game=self, date=day_text,
                                        month=cursor.month, season=int(self.s.get("season", 0)))
            self.s["economy_last_date"] = day_text
            published += 1
            cursor += timedelta(days=1)
        return published

    def _complete_slot(self, slot, persist=True):
        """Finalize a slot after all its fixtures have been played."""
        slot["played"] = True
        self._publish_economy_daily_ticks(str(slot.get("date", "")), current_slot=slot)
        if slot.get("kind") == "league" and int(slot.get("round", 0)) == 14:
            self._initialize_league_phase2_if_ready()
        slot_matches = [self.match_by_id(mid) for mid in slot.get("match_ids", [])]
        slot["matches_played"] = len(slot_matches)
        # P1 event hooks: later economy services subscribe here without reaching
        # into match generation or changing the match engine.
        previous_month = self.s.get("economy_last_calendar_month")
        current_month = int(str(slot.get("date", ""))[5:7] or 0)
        if current_month:
            if previous_month:
                was_open = self.transfer_windows.is_open(int(previous_month))
                is_open = self.transfer_windows.is_open(current_month)
                if not was_open and is_open:
                    self.economy_events.publish("TRANSFER_WINDOW_OPENED", game=self,
                                                month=current_month,
                                                window=self.transfer_windows.window_name(current_month))
                elif was_open and not is_open:
                    self.economy_events.publish("TRANSFER_WINDOW_CLOSED", game=self,
                                                month=int(previous_month),
                                                window=self.transfer_windows.window_name(int(previous_month)))
            self.s["economy_last_calendar_month"] = current_month
            self.economy_events.publish("DATE_ADVANCED", game=self, date=str(slot.get("date", "")),
                                        month=current_month, season=int(self.s.get("season", 0)))
        for match in slot_matches:
            self.economy_events.publish("MATCH_FINISHED", game=self, match=match)
        self.economy_events.publish("ROUND_FINISHED", game=self, slot=slot, matches=slot_matches)
        self.s.setdefault("team_elo_history", []).append({
            "season": int(self.s["season"]), "date": str(slot["date"]),
            "label": str(slot.get("label", slot.get("id", "Slot"))),
            "elo": dict(self.s.get("elo", {})),
        })
        self.s["team_elo_history"] = self.s["team_elo_history"][-600:]
        # Keep the Overview feed strictly one item per match. This is idempotent,
        # so matches already recorded through Live Match are not duplicated here.
        match_items = [self._record_match_news(match) for match in slot_matches]
        match_items = [item for item in match_items if item]
        if match_items:
            self.s["last_news"] = [item["text"] for item in match_items]
        else:
            self.s["last_news"] = [f"{slot['date']} · {slot['label']}: đã mô phỏng {slot['matches_played']} trận."]
        completed_before = {key for key, value in self.s.get("competition_awards", {}).items()
                            if isinstance(value, dict) and value.get("completed")}
        self._refresh_completed_competition_awards(announce=True)
        for key, value in self.s.get("competition_awards", {}).items():
            if key not in completed_before and isinstance(value, dict) and value.get("completed"):
                self.economy_events.publish("COMPETITION_FINISHED", game=self,
                                            competition=key, result=value)
        if not any(not x["played"] for x in self.s["slots"]):
            record = self.finalize_season(persist=persist)
            return {"finished": True, "slot": slot, "matches": slot_matches,
                    "message": f"Đã chốt mùa {record['label']} · Premier: {record['champion_premier']} · Cup: {record['champion_cup']} · C1: {record['champion_c1']}."}
        if persist:
            self.save()
        return {"finished": False, "slot": slot, "matches": slot_matches,
                "message": self.s["last_news"][0] if self.s.get("last_news") else "Đã hoàn tất slot."}

    def _update_player_match_stats(self, match):
        s = self.s
        home, away = match["home"], match["away"]
        used = {home: set(match["xi_h"]), away: set(match["xi_a"])}
        goals_by_player = Counter(); assists_by_player = Counter()
        for g in match["goals"]:
            if g.get("scorer_id"): goals_by_player[g["scorer_id"]] += 1
            if g.get("assist_id"): assists_by_player[g["assist_id"]] += 1
        for team, opp, side in ((home, away, "home"), (away, home, "away")):
            for pid in s["rosters"][team]:
                dyn = s["dyn"].setdefault(pid, {"form": 0.0, "fit": 100.0})
                if pid in used[team]:
                    stat = s["season_stats"].setdefault(pid, self._empty_stat())
                    g = goals_by_player[pid]; a = assists_by_player[pid]
                    team_gf = match["hg"] if side == "home" else match["ag"]
                    team_ga = match["ag"] if side == "home" else match["hg"]
                    result = 1 if team_gf > team_ga else (0 if team_gf == team_ga else -1)
                    rating = max(3.0, min(10.0, 6.3 + 1.1*g + .7*a + .42*result + self.rng.gauss(0, .40)))
                    stat["apps"] += 1; stat["mins"] += 90; stat["goals"] += g; stat["assists"] += a
                    stat["rating_sum"] += rating; stat["rating_count"] += 1
                    if team_ga == 0: stat["clean_sheets"] += 1
                    target = max(-1.0, min(1.0, (rating-6.4)/1.6))
                    dyn["form"] = max(-1.0, min(1.0, float(dyn.get("form", 0.0)) + .45*(target-float(dyn.get("form", 0.0)))))
                    dyn["fit"] = max(25.0, float(dyn.get("fit", 100.0)) - (13 + self.rng.random()*7))
                else:
                    dyn["fit"] = min(100.0, float(dyn.get("fit", 100.0)) + 9.0)
                    dyn["form"] = float(dyn.get("form", 0.0)) * .97

    def _slot_by_kind(self, kind):
        return next((x for x in self.s["slots"] if x["kind"] == kind), None)

    def _add_bracket_match(self, bracket, key, comp, round_label, home, away, dt, neutral=False, force_winner=True, stage=None):
        m = self._record_match(comp, round_label, home, away, dt, neutral, force_winner, stage=stage)
        bracket.setdefault(key, []).append(m["id"])
        return m

    def _seeded_home_away(self, team_a, team_b):
        # "lower seed" nghĩa đội có thứ hạng hạt giống thấp hơn (số lớn hơn) làm chủ nhà.
        ranks = self.s["cup"]["seed_rank"]
        if ranks.get(team_a, 99) > ranks.get(team_b, 99): return team_a, team_b
        return team_b, team_a

    def _cup_winners(self, round_key):
        ids = self.s["cup"]["bracket"].get(round_key, [])
        return [self.match_by_id(mid)["winner"] for mid in ids]

    def match_by_id(self, match_id):
        return next(m for m in self.s["matches"] if int(m["id"]) == int(match_id))

    def _handle_cup(self, slot):
        s, cup = self.s, self.s["cup"]
        dt = slot["date"]
        seeds = cup["seed_order"]
        ranks = cup["seed_rank"]
        if slot["kind"] == "cup_r1":
            bracket_pairs = [(9,24),(10,23),(11,22),(12,21),(13,20),(14,19),(15,18),(16,17)]
            for i, (ra, rb) in enumerate(bracket_pairs):
                h, a = self._seeded_home_away(seeds[ra-1], seeds[rb-1])
                self._add_bracket_match(cup["bracket"], "r1", "Cup", "Vòng 1", h, a, dt, stage="cup_r1")
        elif slot["kind"] == "cup_r16":
            w = self._cup_winners("r1")
            # Vị trí nhánh theo seed cố định, 8 hạt giống đầu được miễn vòng 1.
            pairings = [(1,w[7]), (8,w[0]), (4,w[4]), (5,w[3]), (3,w[5]), (6,w[2]), (7,w[1]), (2,w[6])]
            for ra, opponent in pairings:
                h, a = self._seeded_home_away(seeds[ra-1], opponent)
                self._add_bracket_match(cup["bracket"], "r16", "Cup", "Vòng 16", h, a, dt, stage="cup_r16")
        elif slot["kind"] == "cup_qf":
            winners = self._cup_winners("r16")
            for i, j in ((0,1),(2,3),(4,5),(6,7)):
                h, a = self._seeded_home_away(winners[i], winners[j])
                self._add_bracket_match(cup["bracket"], "qf", "Cup", "Tứ kết", h, a, dt, stage="cup_qf")
        elif slot["kind"] == "cup_sf":
            winners = self._cup_winners("qf")
            for i, j in ((0,1),(2,3)):
                h, a = self._seeded_home_away(winners[i], winners[j])
                self._add_bracket_match(cup["bracket"], "sf", "Cup", "Bán kết", h, a, dt, stage="cup_sf")
        elif slot["kind"] == "cup_final":
            winners = self._cup_winners("sf")
            if len(winners) == 2:
                # Chung kết trung lập, không lợi thế sân nhà.
                m = self._add_bracket_match(cup["bracket"], "final", "Cup", "Chung kết", winners[0], winners[1], dt,
                                            neutral=bool(self.config["cup"].get("final_neutral", True)), force_winner=True, stage="cup_final")
                cup["winner"] = m["winner"]
                cup["runner_up"] = m["away"] if m["winner"] == m["home"] else m["home"]

    def _c1_ranking_slice(self):
        order = self.s["c1"]["seed_order"]
        return order

    def _handle_c1(self, slot):
        s, c1 = self.s, self.s["c1"]
        dt = slot["date"]
        seed = c1["seed_order"]
        seed_rank = {t:i+1 for i,t in enumerate(seed)}
        if slot["kind"] == "c1_pi_r1":
            pi = c1["playin"]
            ordered = c1.get("playin_seed_teams", seed[4:12])
            # X: 5,8,9,12; Y: 6,7,10,11 theo hạng tổng hợp trước mùa.
            groups = {"X": [seed[4], seed[7], seed[8], seed[11]], "Y": [seed[5], seed[6], seed[9], seed[10]]}
            for gname, teams in groups.items():
                entry = {"teams": teams, "r1": [], "winner_match": None, "loser_match": None, "decider": None,
                         "winner_qualifier": None, "decider_qualifier": None}
                for a, b in ((teams[0], teams[3]), (teams[1], teams[2])):
                    h, aw = self._seeded_home_away(a, b)
                    m = self._record_match("C1 Play-in", f"Bảng {gname} - Vòng 1", h, aw, dt, force_winner=True, stage=f"c1_pi_{gname}_r1")
                    entry["r1"].append(m["id"])
                pi[gname] = entry
            c1["playin_groups"] = groups
        elif slot["kind"] == "c1_pi_mid":
            for gname, entry in c1["playin"].items():
                r1 = [self.match_by_id(mid) for mid in entry["r1"]]
                winners = [m["winner"] for m in r1]
                losers = [m["away"] if m["winner"] == m["home"] else m["home"] for m in r1]
                h, a = self._seeded_home_away(winners[0], winners[1])
                wm = self._record_match("C1 Play-in", f"Bảng {gname} - Nhánh thắng", h, a, dt, force_winner=True, stage=f"c1_pi_{gname}_wb")
                h, a = self._seeded_home_away(losers[0], losers[1])
                lm = self._record_match("C1 Play-in", f"Bảng {gname} - Nhánh thua", h, a, dt, force_winner=True, stage=f"c1_pi_{gname}_lb")
                entry["winner_match"] = wm["id"]; entry["loser_match"] = lm["id"]
        elif slot["kind"] == "c1_pi_decider":
            for gname, entry in c1["playin"].items():
                wm, lm = self.match_by_id(entry["winner_match"]), self.match_by_id(entry["loser_match"])
                wb_loser = wm["away"] if wm["winner"] == wm["home"] else wm["home"]
                lb_winner = lm["winner"]
                h, a = self._seeded_home_away(wb_loser, lb_winner)
                dec = self._record_match("C1 Play-in", f"Bảng {gname} - Trận quyết định", h, a, dt, force_winner=True, stage=f"c1_pi_{gname}_decider")
                entry["decider"] = dec["id"]
                entry["winner_qualifier"] = wm["winner"]
                entry["decider_qualifier"] = dec["winner"]
            c1["playin_qualified"] = {g: {"winner": c["winner_qualifier"], "decider": c["decider_qualifier"]} for g,c in c1["playin"].items()}
        elif slot["kind"] == "c1_group1":
            self._setup_c1_groups()
            self._play_c1_group_round(0, dt)
        elif slot["kind"] == "c1_group2":
            self._play_c1_group_round(1, dt)
        elif slot["kind"] == "c1_group3":
            self._play_c1_group_round(2, dt)
            c1["group_tables"] = {g: self.c1_group_table(g) for g in ("A", "B")}
        elif slot["kind"] == "c1_semi1":
            self._setup_c1_semis(dt, leg=1)
        elif slot["kind"] == "c1_semi2":
            self._play_c1_semis_return(dt)
        elif slot["kind"] == "c1_final":
            winners = []
            for tie in c1.get("semi_ties", []):
                second = self.match_by_id(tie["leg2"])
                winners.append(second["winner"])
            if len(winners) == 2:
                m = self._record_match("C1", "Chung kết", winners[0], winners[1], dt,
                                       neutral=bool(self.config["c1"].get("final_neutral", True)), force_winner=True, stage="c1_final")
                c1["champion"] = m["winner"]
                c1["runner_up"] = m["away"] if m["winner"] == m["home"] else m["home"]
                c1["bracket"]["final"] = m["id"]

    def _setup_c1_groups(self):
        c1 = self.s["c1"]
        if not c1.get("groups"):
            # Main stage là vòng tròn MỘT lượt: chỉ lấy 3 vòng đầu và không dùng lượt về.
            c1["groups"] = self._build_c1_groups()
        return c1["groups"]

    def _play_c1_group_round(self, round_index, dt):
        for label in ("A", "B"):
            group = self.s["c1"]["groups"][label]
            for h, a in group["rounds"][round_index]:
                m = self._record_match("C1", f"Bảng {label} - Vòng {round_index+1}", h, a, dt, force_winner=False,
                                       stage=f"c1_group_{label}_{round_index+1}")
                group["match_ids"][round_index].append(m["id"])

    def c1_group_table(self, label):
        group = self.s["c1"].get("groups", {}).get(label)
        if not group: return []
        all_ids = [mid for one in group["match_ids"] for mid in one]
        return self._standings_for_teams(group["teams"], all_ids, use_elo=True)

    def _setup_c1_semis(self, dt, leg=1):
        c1 = self.s["c1"]
        ta = self.c1_group_table("A"); tb = self.c1_group_table("B")
        if len(ta) < 2 or len(tb) < 2: raise RuntimeError("Bảng C1 chưa hoàn tất.")
        pairs = [(ta[0]["team"], tb[1]["team"]), (tb[0]["team"], ta[1]["team"])]
        c1["semi_ties"] = []
        for idx, (winner_group, runner_group) in enumerate(pairs, 1):
            # Lượt đi sân đội nhì bảng; lượt về sân đội nhất bảng.
            m = self._record_match("C1", f"Bán kết {idx} - Lượt đi", runner_group, winner_group, dt,
                                   force_winner=False, stage=f"c1_semi{idx}_leg1")
            c1["semi_ties"].append({"group_winner": winner_group, "group_runner": runner_group, "leg1": m["id"], "leg2": None})
            c1["bracket"].setdefault("semis", []).append(m["id"])

    def _play_c1_semis_return(self, dt):
        c1 = self.s["c1"]
        for idx, tie in enumerate(c1.get("semi_ties", []), 1):
            leg1 = self.match_by_id(tie["leg1"])
            # Lượt đi: runner-up là chủ nhà, group-winner là đội khách.
            agg_home = leg1["ag"]  # bàn đội nhất bảng đã ghi ở sân khách
            agg_away = leg1["hg"]  # bàn đội nhì bảng đã ghi ở sân nhà
            m = self._record_match("C1", f"Bán kết {idx} - Lượt về", tie["group_winner"], tie["group_runner"], dt,
                                   force_winner=True, aggregate_before=(agg_home, agg_away), stage=f"c1_semi{idx}_leg2")
            tie["leg2"] = m["id"]
            c1["bracket"].setdefault("semis", []).append(m["id"])

    def advance_slot(self, persist=True):
        """Chạy slot kế tiếp; persist=False chỉ dùng cho batch mô phỏng nội bộ."""
        if self.s["season_done"]:
            return {"finished": True, "message": f"Mùa {self.s['season']}/{self.s['season']+1} đã hoàn tất."}
        slot = next((x for x in self.s["slots"] if not x["played"]), None)
        if slot is None:
            self.finalize_season(persist=persist)
            return {"finished": True, "message": f"Đã chốt mùa {self.s['season']}/{self.s['season']+1}."}
        kind = slot["kind"]
        start_matches = len(self.s["matches"])
        if slot.get("interactive"):
            # A user may have played one or more fixtures manually. Simulate only
            # the remaining fixtures from the same slot; never duplicate a result.
            for spec in self.slot_fixture_specs(slot, include_played=False):
                self._assert_match_rest(spec["home"], spec["away"], spec["date"])
                match = self._record_match(spec["comp"], spec["round"], spec["home"], spec["away"],
                                           spec["date"], neutral=spec["neutral"],
                                           force_winner=spec["force_winner"],
                                           aggregate_before=spec.get("aggregate_before"), stage=spec["stage"])
                self._register_slot_fixture(slot, spec, match)
            if kind == "c1_group3":
                self.s["c1"]["group_tables"] = {g: self.c1_group_table(g) for g in ("A", "B")}
        elif kind == "league":
            self._play_league_round(slot)
        elif kind == "league_phase2":
            for spec in self.slot_fixture_specs(slot, include_played=False):
                self._assert_match_rest(spec["home"], spec["away"], spec["date"])
                match = self._record_match(spec["comp"], spec["round"], spec["home"], spec["away"],
                                           spec["date"], neutral=spec["neutral"],
                                           force_winner=spec["force_winner"], stage=spec["stage"])
                self._register_slot_fixture(slot, spec, match)
        elif kind.startswith("cup_"):
            self._handle_cup(slot)
        elif kind.startswith("c1_"):
            self._handle_c1(slot)
        else:
            raise ValueError(f"Loại slot không rõ: {kind}")
        new_matches = self.s["matches"][start_matches:]
        slot.setdefault("match_ids", [])
        slot["match_ids"].extend(m["id"] for m in new_matches if m["id"] not in slot["match_ids"])
        return self._complete_slot(slot, persist=persist)

    def _play_league_round(self, slot):
        r = int(slot["round"])-1
        tiers = (slot["tier"],) if slot.get("tier") in TIERS else TIERS
        for tier in tiers:
            matches = self.s["league_fixtures"][tier][r]
            stage = f"league_{tier}_{r+1}"
            already = {(m.get("home"), m.get("away")) for m in self.s["matches"]
                       if m.get("stage") == stage and m.get("date") == slot["date"] and m.get("played")}
            for home, away in matches:
                if (home, away) in already:
                    continue
                self._record_match(TIER_LABELS[tier], f"Vòng {r+1}", home, away, slot["date"],
                                   force_winner=False, stage=stage)

    def simulate_full_season(self, persist_each_slot=True):
        """Simulate to season end.

        The default preserves interactive safety by checkpointing every slot. Batch
        callers (CLI checks/benchmarks/tests) may set persist_each_slot=False to avoid
        repeatedly serializing the full replay-rich career; that mode writes once at
        season end.
        """
        if self.s["season_done"]:
            return self.s["history"][-1] if self.s["history"] else None
        while not self.s["season_done"]:
            self.advance_slot(persist=persist_each_slot)
        if not persist_each_slot:
            self.save()
        return self.s["history"][-1]

    def next_season(self):
        if not self.s["season_done"]:
            raise RuntimeError("Hãy hoàn tất mùa hiện tại trước khi bắt đầu mùa tiếp theo.")
        self.s["season"] += 1
        order = self.s.get("previous_ranks", {})
        self.s["seed_order"] = sorted(self.s["rosters"], key=lambda t: order.get(t, 999))
        self._setup_season()
        if getattr(self, "transfer_market", None) is not None:
            self.transfer_market.activate_pre_contracts(int(self.s.get("season", 0)))
        self.save()

    def _standings_for_teams(self, teams, match_ids, use_elo=True, apply_ffp_deductions=False):
        rows = {t: {"team":t, "p":0, "w":0, "d":0, "l":0, "gf":0, "ga":0, "gd":0, "pts":0,
                    "h2h":0, "elo":self.s["elo"].get(t, 1500)} for t in teams}
        matches = [self.match_by_id(mid) for mid in match_ids]
        for m in matches:
            if not m["played"]: continue
            h, a, hg, ag = m["home"], m["away"], int(m["hg"]), int(m["ag"])
            for team, gf, ga in ((h,hg,ag),(a,ag,hg)):
                if team not in rows: continue
                r = rows[team]; r["p"] += 1; r["gf"] += gf; r["ga"] += ga
                if gf > ga: r["w"] += 1; r["pts"] += 3
                elif gf == ga: r["d"] += 1; r["pts"] += 1
                else: r["l"] += 1
        for m in matches:
            h, a = m["home"], m["away"]
            if h not in rows or a not in rows: continue
            hg, ag = int(m["hg"]), int(m["ag"])
            if hg > ag: rows[h]["h2h"] += 3
            elif hg < ag: rows[a]["h2h"] += 3
            else: rows[h]["h2h"] += 1; rows[a]["h2h"] += 1
        for r in rows.values():
            r["gd"] = r["gf"] - r["ga"]
            if apply_ffp_deductions:
                sanction = (self.s.get("ffp_point_deductions", {}).get(str(self.s.get("season", 0)), {})
                            .get(r["team"], {}))
                deduction = max(0, min(30, int(sanction.get("points", 0) or 0)))
                r["points_before_deduction"] = r["pts"]
                r["points_deduction"] = deduction
                r["pts"] -= deduction
            else:
                r["points_deduction"] = 0
        return sorted(rows.values(), key=lambda r: (-r["pts"], -r["gd"], -r["gf"], -r["h2h"], -r["elo"], r["team"]))

    def league_recent_matches(self, team, tier, limit=5, phase1_only=False):
        """Return the latest League matches in chronological display order.

        The selection is made newest-first (so the oldest item outside the
        window can never leak into the five-match form), then returned oldest
        to newest for the conventional left-to-right form display. Cup, C1,
        promotion play-offs and unrelated stage names are excluded.
        """
        if tier not in TIERS:
            tier = TIER_KEY_BY_LABEL.get(tier, "premier")
        try:
            count = max(0, int(limit))
        except (TypeError, ValueError):
            count = 5
        if count == 0:
            return []

        comp = TIER_LABELS[tier]
        regular_stage = re.compile(rf"^league_{re.escape(tier)}_\d+$")
        phase2_stage = re.compile(r"^league_p2_premier_(?:champion|challenge)_\d+$")
        matches = []

        def order_key(match):
            raw_date = str(match.get("date", ""))[:10]
            try:
                played_date = date.fromisoformat(raw_date)
            except (TypeError, ValueError):
                # Invalid/missing dates sort before valid ones rather than
                # being mistaken for a recent fixture.
                played_date = date.min
            try:
                match_id = int(match.get("id", -1))
            except (TypeError, ValueError):
                match_id = -1
            return played_date, match_id

        for match in self.s.get("matches", []):
            if not match.get("played") or match.get("comp") != comp:
                continue
            if team not in (match.get("home"), match.get("away")):
                continue
            stage = str(match.get("stage", ""))
            is_regular = bool(regular_stage.fullmatch(stage))
            is_premier_phase2 = bool(
                tier == "premier" and not phase1_only and phase2_stage.fullmatch(stage)
            )
            if not (is_regular or is_premier_phase2):
                continue
            # A malformed/incomplete result must not occupy one of the five
            # slots and displace a valid older League result.
            try:
                int(match.get("hg"))
                int(match.get("ag"))
            except (TypeError, ValueError):
                continue
            matches.append(match)

        matches.sort(key=order_key)
        # Keep only the latest N, then render from oldest to newest.
        return matches[-count:]

    def league_recent_form(self, team, tier, limit=5, phase1_only=False):
        """Return W/D/L from oldest to newest across the latest League matches.

        The rightmost result is the most recent match. Phase-2 Premier matches
        are included unless ``phase1_only`` is requested for the phase-1 table.
        """
        form = []
        for match in self.league_recent_matches(team, tier, limit, phase1_only):
            home = team == match.get("home")
            gf = int(match.get("hg") if home else match.get("ag"))
            ga = int(match.get("ag") if home else match.get("hg"))
            form.append("W" if gf > ga else "D" if gf == ga else "L")
        return form

    def all_competition_recent_matches(self, team, limit=5):
        """Return the latest played matches for a club across every competition.

        Unlike ``league_recent_matches``, this intentionally includes League,
        domestic Cup, Champions League, play-offs and any other competition
        represented in the match ledger. Results are returned oldest to newest
        so the most recent match appears at the right edge of a form strip.
        Incomplete scores are skipped and cannot displace a valid result.
        """
        try:
            count = max(0, int(limit))
        except (TypeError, ValueError):
            count = 5
        if count == 0:
            return []

        matches = []
        for match in self.s.get("matches", []):
            if not match.get("played"):
                continue
            if team not in (match.get("home"), match.get("away")):
                continue
            try:
                int(match.get("hg"))
                int(match.get("ag"))
            except (TypeError, ValueError):
                continue
            matches.append(match)

        def order_key(match):
            raw_date = str(match.get("date", ""))[:10]
            try:
                played_date = date.fromisoformat(raw_date)
            except (TypeError, ValueError):
                played_date = date.min
            try:
                match_id = int(match.get("id", -1))
            except (TypeError, ValueError):
                match_id = -1
            return played_date, match_id

        matches.sort(key=order_key)
        return matches[-count:]

    def all_competition_recent_form(self, team, limit=5):
        """Return W/D/L across all competitions, oldest to newest."""
        form = []
        for match in self.all_competition_recent_matches(team, limit):
            home = team == match.get("home")
            gf = int(match.get("hg") if home else match.get("ag"))
            ga = int(match.get("ag") if home else match.get("hg"))
            form.append("W" if gf > ga else "D" if gf == ga else "L")
        return form

    def league_table(self, tier):
        if tier not in TIERS: tier = TIER_KEY_BY_LABEL.get(tier, "premier")
        if tier == "premier" and self.s.get("league_phase2", {}).get("initialized"):
            tables = self.premier_phase2_tables()
            return tables["champion"] + tables["challenge"]
        return self.league_phase1_table(tier)

    def live_league_table(self, preview, provisional_results=None):
        """Build read-only in-play League tables from saved and provisional scores.

        During Premier League phase 2, both Champion and Challenge groups are
        returned together as ``sections`` so the Live Match standings tab can show
        them side by side in the same scrollable view.  ``rows`` remains the
        currently selected group's table for backwards compatibility.
        """
        preview = dict(preview or {})
        meta = preview.get("_meta") or {}
        stage = str(preview.get("stage", ""))
        comp = str(preview.get("comp", ""))
        tier = preview.get("tier") or meta.get("tier") or TIER_KEY_BY_LABEL.get(comp)

        unavailable = {
            "available": False,
            "title": "BXH TRỰC TIẾP",
            "note": "Bảng xếp hạng trực tiếp chỉ áp dụng cho các trận League.",
            "rows": [],
            "live_teams": set(),
        }
        if tier not in TIERS:
            return unavailable

        phase2_group = None
        if stage.startswith("league_p2_premier_"):
            phase2_group = meta.get("group")
            if phase2_group not in ("champion", "challenge"):
                for candidate in ("champion", "challenge"):
                    if stage.startswith(f"league_p2_premier_{candidate}_"):
                        phase2_group = candidate
                        break

        is_promotion_playoff = stage.startswith("promotion_")
        elo_cfg = self.config.get("engine", {})

        def build_rows(teams, baseline, provisional):
            """Calculate one table without mutating saved matches or Elo."""
            teams = list(teams or [])
            elo_projection = {
                team: float(self.s.get("elo", {}).get(team, 1500)) for team in teams
            }
            rows = {
                team: {"team": team, "p": 0, "w": 0, "d": 0, "l": 0,
                       "gf": 0, "ga": 0, "gd": 0, "pts": 0, "h2h": 0,
                       "elo": elo_projection[team]}
                for team in teams
            }
            live_teams = set()

            def apply_result(result, update_elo=False):
                home, away = result.get("home"), result.get("away")
                try:
                    hg, ag = int(result.get("hg", 0)), int(result.get("ag", 0))
                except (TypeError, ValueError):
                    return
                for team, gf, ga in ((home, hg, ag), (away, ag, hg)):
                    if team not in rows:
                        continue
                    row = rows[team]
                    row["p"] += 1
                    row["gf"] += gf
                    row["ga"] += ga
                    if gf > ga:
                        row["w"] += 1
                        row["pts"] += 3
                    elif gf == ga:
                        row["d"] += 1
                        row["pts"] += 1
                    else:
                        row["l"] += 1
                if home in rows and away in rows:
                    if hg > ag:
                        rows[home]["h2h"] += 3
                    elif hg < ag:
                        rows[away]["h2h"] += 3
                    else:
                        rows[home]["h2h"] += 1
                        rows[away]["h2h"] += 1

                # Match the engine's Elo tiebreak for provisional fixtures only.
                # Already-saved results are reflected in the current saved Elo.
                if update_elo and home in elo_projection and away in elo_projection:
                    eh, ea = elo_projection[home], elo_projection[away]
                    neutral = bool(result.get("neutral", False))
                    home_advantage = 0.0 if neutral else float(elo_cfg.get("elo_home_advantage", 45))
                    expected_home = 1.0 / (1.0 + 10 ** (-((eh + home_advantage) - ea) / 400.0))
                    actual = 1.0 if hg > ag else 0.0 if hg < ag else 0.5
                    margin = min(
                        float(elo_cfg.get("elo_margin_cap", 1.5)),
                        1.0 + float(elo_cfg.get("elo_margin_bonus", .08)) * abs(hg - ag),
                    )
                    change = float(elo_cfg.get("elo_k", 18.0)) * margin * (actual - expected_home)
                    elo_projection[home] = round(eh + change, 2)
                    elo_projection[away] = round(ea - change, 2)

            for match in baseline:
                apply_result(match)
            for result in provisional:
                apply_result(result, update_elo=True)
                live_teams.update((result["home"], result["away"]))

            for team, row in rows.items():
                row["gd"] = row["gf"] - row["ga"]
                row["elo"] = elo_projection[team]
                sanction = (self.s.get("ffp_point_deductions", {}).get(str(self.s.get("season", 0)), {})
                            .get(team, {}))
                deduction = max(0, min(30, int(sanction.get("points", 0) or 0)))
                row["points_before_deduction"] = row["pts"]
                row["points_deduction"] = deduction
                row["pts"] -= deduction
            ordered = sorted(
                rows.values(),
                key=lambda row: (-row["pts"], -row["gd"], -row["gf"],
                                 -row["h2h"], -row["elo"], row["team"]),
            )
            return ordered, live_teams

        if tier == "premier" and phase2_group:
            phase2 = self.s.get("league_phase2", {}).get("premier", {})
            phase1_prefix = "league_premier_"
            all_saved = self.s.get("matches", [])
            sections = []
            selected_rows = []
            all_live_teams = set()
            for group_key, group_title in (("champion", "CHAMPION"), ("challenge", "CHALLENGE")):
                teams = list(phase2.get(f"{group_key}_teams") or [])
                if not teams:
                    continue
                group_prefix = f"league_p2_premier_{group_key}_"
                baseline = [
                    match for match in all_saved
                    if match.get("played") and match.get("comp") == TIER_LABELS["premier"]
                    and (str(match.get("stage", "")).startswith(phase1_prefix)
                         or str(match.get("stage", "")).startswith(group_prefix))
                ]
                provisional = []
                for item in provisional_results or []:
                    if not isinstance(item, dict):
                        continue
                    if item.get("comp") != TIER_LABELS["premier"]:
                        continue
                    if not str(item.get("stage", "")).startswith(group_prefix):
                        continue
                    if item.get("home") not in teams or item.get("away") not in teams:
                        continue
                    try:
                        hg, ag = int(item.get("hg", 0)), int(item.get("ag", 0))
                    except (TypeError, ValueError):
                        continue
                    provisional.append({
                        "home": item["home"], "away": item["away"],
                        "hg": hg, "ag": ag,
                        "neutral": bool(item.get("neutral", False)),
                    })
                rows, live_teams = build_rows(teams, baseline, provisional)
                all_live_teams.update(live_teams)
                sections.append({
                    "key": group_key,
                    "title": group_title,
                    "rows": rows,
                    "live_teams": live_teams,
                })
                if group_key == phase2_group:
                    selected_rows = rows

            if not sections:
                return unavailable
            return {
                "available": True,
                "title": "BXH LIVE · PREMIER · CHAMPION & CHALLENGE",
                "note": "Hai bảng 4 CLB được hiển thị cùng lúc · bảo lưu điểm giai đoạn 1 và cộng tỷ số tạm thời giai đoạn 2.",
                "rows": selected_rows or sections[0]["rows"],
                "live_teams": all_live_teams,
                "sections": sections,
            }

        # Regular League table (Premier phase 1, FC1 and FC2).  Promotion
        # play-off fixtures show the regular table as reference and are excluded.
        teams = [team for team, team_tier in self.s.get("season_tiers", {}).items()
                 if team_tier == tier]
        if not teams:
            teams = [team for team, team_tier in self.s.get("team_tiers", {}).items()
                     if team_tier == tier]
        if not teams:
            return unavailable
        prefix = f"league_{tier}_"
        baseline = [
            match for match in self.s.get("matches", [])
            if match.get("played") and match.get("comp") == TIER_LABELS[tier]
            and str(match.get("stage", "")).startswith(prefix)
        ]
        provisional = []
        for item in provisional_results or []:
            if not isinstance(item, dict):
                continue
            if item.get("comp") != TIER_LABELS[tier] or not str(item.get("stage", "")).startswith(prefix):
                continue
            if item.get("home") not in teams or item.get("away") not in teams:
                continue
            try:
                hg, ag = int(item.get("hg", 0)), int(item.get("ag", 0))
            except (TypeError, ValueError):
                continue
            provisional.append({
                "home": item["home"], "away": item["away"],
                "hg": hg, "ag": ag,
                "neutral": bool(item.get("neutral", False)),
            })

        rows, live_teams = build_rows(teams, baseline, provisional)
        note = "BXH League tính theo tỷ số hiện tại; trận đang đá được tạm tính như đã kết thúc."
        if is_promotion_playoff:
            note = "Bảng League chính thức trước play-off; tỷ số play-off không cộng vào thành tích League."
        return {
            "available": True,
            "title": f"BXH LIVE · {TIER_LABELS[tier].upper()}",
            "note": note,
            "rows": rows,
            "live_teams": live_teams,
        }

    def _league_points(self, tier, pos, team=None):
        """Return C1 ranking points for a league position and earned promotion.

        A team that wins the FC1/FC2 promotion play-off receives the dedicated
        second-promotion-place award, regardless of whether it started the
        play-off as 2nd, 3rd or 4th in the regular table. Losing play-off teams
        keep their ordinary league-position points.
        """
        if tier not in TIERS:
            tier = TIER_KEY_BY_LABEL.get(tier, tier)
        if tier not in TIERS:
            raise ValueError(f"Hạng đấu không hợp lệ để tính điểm: {tier}")
        cfg = self.config["points"][tier]
        positions = cfg.get("positions")
        if isinstance(positions, list) and positions:
            index = max(0, min(len(positions) - 1, int(pos) - 1))
            points = float(positions[index])
        else:
            # Backward compatibility for custom configs using the old linear formula.
            points = float(cfg["first"] - cfg["step"] * (int(pos) - 1))

        if team and tier in ("fc1", "fc2"):
            playoffs = self.s.get("league_phase2", {}).get("promotion_playoffs", {})
            playoff_winner = playoffs.get(tier, {}).get("winner")
            if playoff_winner == team:
                points = float(cfg.get("promotion_playoff_winner", points))
        return int(points) if points.is_integer() else round(points, 2)

    def _cup_points(self):
        cup = self.s["cup"]
        scores = {t: 0 for t in self.s["rosters"]}
        bracket = cup["bracket"]
        key_score = {"r1": 3, "r16": 8, "qf": 14, "sf": 22, "final": 30}
        round_order = ["r1", "r16", "qf", "sf", "final"]
        for key in round_order:
            ids = bracket.get(key, [])
            if not ids: continue
            stage_score = key_score[key]
            for mid in ids:
                m = self.match_by_id(mid)
                winner = m.get("winner")
                loser = m["away"] if winner == m["home"] else m["home"]
                if key == "final":
                    scores[winner] = 40; scores[loser] = 30
                else:
                    scores[loser] = stage_score
                    next_stage = {"r1":"r16", "r16":"qf", "qf":"sf", "sf":"final"}[key]
                    next_score = key_score[next_stage]
                    if scores[winner] < next_score: scores[winner] = next_score
        # Nếu Cup chưa kết thúc thì đội chưa bị loại nhận mốc tối thiểu đã đạt.
        for t in scores:
            if not any((m["comp"] == "Cup" and t in (m["home"],m["away"]) and m["winner"] and
                        (m["home"] if m["winner"] == m["away"] else m["away"]) == t) for m in self.s["matches"]):
                if bracket.get("r1") and t in [x for mid in bracket["r1"] for x in (self.match_by_id(mid)["home"], self.match_by_id(mid)["away"])]:
                    if t not in [self._loser(self.match_by_id(mid)) for mid in bracket["r1"]]: scores[t] = max(scores[t], 8)
        return scores

    @staticmethod
    def _loser(match):
        return match["away"] if match.get("winner") == match["home"] else match["home"]

    def c1_qualification_table(self, projected=True):
        s = self.s
        if s["season_done"] and s.get("history"):
            return s["history"][-1].get("qualification_table", [])
        cup_scores = self._cup_points()
        league_pts = {}
        league_gd = {}
        for tier in TIERS:
            table = self.league_table(tier)
            for i, row in enumerate(table, 1):
                league_pts[row["team"]] = self._league_points(tier, i, row["team"])
                league_gd[row["team"]] = row["gd"]
        prev = s.get("previous_total_points", {})
        rows = []
        for team in s["rosters"]:
            lp = league_pts.get(team, 0); cp = cup_scores.get(team, 0)
            total = lp + cp + float(self.config["points"].get("history_weight", 0.0))*float(prev.get(team, 0.0))
            rows.append({"team":team, "tier":s["season_tiers"].get(team,"fc2"), "league_points":lp,
                         "cup_points":cp, "total":round(total,2), "gd":league_gd.get(team,0), "elo":s["elo"].get(team,1500)})
        rows.sort(key=lambda r: (-r["total"], -r["league_points"], -r["gd"], -r["elo"], r["team"]))
        for i,r in enumerate(rows,1): r["rank"] = i
        return rows

    def _live_cup_round(self, team):
        # Ước lượng mốc Cup tối thiểu/đạt được tới hiện tại cho BXH C1 tạm tính.
        bracket = self.s["cup"]["bracket"]
        score = 0
        keys = [("r1",3),("r16",8),("qf",14),("sf",22),("final",30)]
        max_achieved = 0
        for key, points in keys:
            for mid in bracket.get(key, []):
                m = self.match_by_id(mid)
                if team in (m["home"],m["away"]):
                    max_achieved = max(max_achieved, points)
                    if m["winner"] != team: return points
        if self.s["cup"].get("winner") == team: return 40
        return max_achieved

    def _season_total_table(self):
        league_pts, league_gd, league_rank = {}, {}, {}
        tables = {}
        for tier in TIERS:
            table = self.league_table(tier); tables[tier] = table
            for i, row in enumerate(table, 1):
                league_pts[row["team"]] = self._league_points(tier, i, row["team"])
                league_gd[row["team"]] = row["gd"]
                league_rank[row["team"]] = i
        cup_points = self._cup_points()
        rows = []
        for team in self.s["rosters"]:
            lp = league_pts.get(team,0); cp = cup_points.get(team,0)
            old = float(self.s.get("previous_total_points",{}).get(team,0))
            total = lp + cp + float(self.config["points"].get("history_weight",0))*old
            rows.append({"team":team,"tier":self.s["season_tiers"].get(team,"fc2"),"league_rank":league_rank.get(team),
                         "league_points":lp,"cup_points":cp,"total":round(total,2),"gd":league_gd.get(team,0),"elo":self.s["elo"].get(team,1500)})
        rows.sort(key=lambda r:(-r["total"],-r["league_points"],-r["gd"],-r["elo"],r["team"]))
        for i,r in enumerate(rows,1): r["rank"] = i
        return rows, tables

    @staticmethod
    def _award_competition_matches(key, matches):
        """Return matches belonging to one official competition, not friendlies."""
        if key in TIERS:
            label = TIER_LABELS[key]
            return [m for m in matches if m.get("played", True) and m.get("comp") == label]
        if key == "cup":
            return [m for m in matches if m.get("played", True) and m.get("comp") == "Cup"]
        if key == "c1":
            return [m for m in matches if m.get("played", True) and m.get("comp") in ("C1", "C1 Play-in")]
        return []

    def _award_player_lookup(self):
        """Player identity/position lookup covering active and retired career players."""
        lookup = {str(pid): dict(player) for pid, player in self.s.get("players", {}).items()}
        for retired in self.s.get("retired_players", []) or []:
            pid = str(retired.get("player_id") or retired.get("id") or "")
            if not pid:
                continue
            row = lookup.setdefault(pid, {})
            for key in ("birthdate", "position", "overall_rating", "club"):
                if key not in row and retired.get(key) is not None:
                    row[key] = retired[key]
            row.setdefault("common_name", retired.get("player_name") or retired.get("common_name") or pid)
        return lookup

    def _award_rows_from_matches(self, matches, player_lookup=None):
        """Aggregate competition-level appearances, minutes, G/A, ratings and MOTM."""
        lookup = player_lookup if player_lookup is not None else self._award_player_lookup()
        rows = {}
        def ensure(pid, team="", fallback_name=""):
            pid = str(pid or "")
            if not pid:
                return None
            player = lookup.get(pid, {})
            row = rows.setdefault(pid, {
                "player_id": pid,
                "player": player.get("common_name") or player.get("player_name") or fallback_name or pid,
                "team": team or player.get("club", ""),
                "position": player.get("position", ""),
                "apps": 0, "minutes": 0, "goals": 0, "assists": 0,
                "rating_sum": 0.0, "rating_count": 0, "motm_count": 0,
            })
            if team:
                row["team"] = team
            if fallback_name and (not row.get("player") or row["player"] == pid):
                row["player"] = fallback_name
            return row

        for match in matches or []:
            if not match.get("played", True):
                continue
            goal_goals, goal_assists, goal_names, goal_teams = Counter(), Counter(), {}, {}
            for event in match.get("goals", []) or []:
                scorer_id = str(event.get("scorer_id") or "")
                assist_id = str(event.get("assist_id") or "")
                if scorer_id:
                    goal_goals[scorer_id] += 1
                    goal_names[scorer_id] = event.get("scorer") or event.get("player") or ""
                    goal_teams[scorer_id] = event.get("team", "")
                if assist_id:
                    goal_assists[assist_id] += 1
                    goal_names[assist_id] = event.get("assist") or ""
                    goal_teams[assist_id] = event.get("team", "")

            for side, team_key, minutes_key in (("h", "home", "minutes_h"), ("a", "away", "minutes_a")):
                team = str(match.get(team_key, ""))
                mins_map = match.get(minutes_key, {}) or {}
                side_stats = ((match.get("player_stats") or {}).get(side, {}) or {})
                ids = {str(pid) for pid, mins in mins_map.items() if int(mins or 0) > 0}
                ids.update(str(pid) for pid in side_stats)
                ids.update(pid for pid, goal_team in goal_teams.items() if goal_team == team)
                ids.update(str(pid) for pid in (match.get("xi_" + side, []) or []) if pid)
                sub_entries = match.get("subs_" + side, []) or []
                for entry in sub_entries:
                    if isinstance(entry, (list, tuple)) and len(entry) >= 3:
                        if entry[1]: ids.add(str(entry[1]))
                        if entry[2]: ids.add(str(entry[2]))
                for pid in ids:
                    details = side_stats.get(pid, {}) or {}
                    minutes = int(mins_map.get(pid, details.get("min", 0)) or 0)
                    is_starter = pid in {str(x) for x in (match.get("xi_" + side, []) or [])}
                    if minutes <= 0 and is_starter:
                        minutes = 90
                    if minutes <= 0:
                        for entry in sub_entries:
                            if isinstance(entry, (list, tuple)) and len(entry) >= 3:
                                try: minute = int(entry[0])
                                except (TypeError, ValueError): minute = 0
                                if str(entry[2] or "") == pid:
                                    minutes = max(minutes, 90 - minute)
                    if minutes <= 0 and not details and not goal_goals[pid] and not goal_assists[pid]:
                        continue
                    row = ensure(pid, team, goal_names.get(pid, ""))
                    if row is None:
                        continue
                    row["apps"] += 1
                    row["minutes"] += max(0, minutes)
                    row["goals"] += int(details.get("g", goal_goals[pid]))
                    row["assists"] += int(details.get("a", goal_assists[pid]))
                    if details.get("rt") is not None:
                        try:
                            row["rating_sum"] += float(details["rt"])
                            row["rating_count"] += 1
                        except (TypeError, ValueError):
                            pass

            motm = match.get("motm")
            if isinstance(motm, (list, tuple)) and len(motm) >= 2 and motm[1]:
                side, pid = str(motm[0]), str(motm[1])
                team = str(match.get("home" if side == "h" else "away", ""))
                row = ensure(pid, team)
                if row is not None:
                    row["motm_count"] += 1

        result = []
        for row in rows.values():
            row["avg_rating"] = round(row["rating_sum"] / row["rating_count"], 2) if row["rating_count"] else None
            result.append(row)
        return result

    @staticmethod
    def _rank_award_candidates(rows, settings=None):
        """Apply the configured, bounded ballot formula to a list of player totals."""
        settings = dict(AWARD_SETTINGS_DEFAULTS, **(settings or {}))
        wr = float(settings.get("weight_rating", 42.0)) / 100.0
        wc = float(settings.get("weight_contribution", 28.0)) / 100.0
        wm = float(settings.get("weight_motm", 20.0)) / 100.0
        wmin = float(settings.get("weight_minutes", 10.0)) / 100.0
        rows = [dict(row) for row in rows if int(row.get("apps", 0) or 0) > 0]
        max_impact = max((int(r.get("goals", 0) or 0) + .7 * int(r.get("assists", 0) or 0) for r in rows), default=0.0)
        max_motm = max((int(r.get("motm_count", 0) or 0) for r in rows), default=0)
        max_minutes = max((int(r.get("minutes", 0) or 0) for r in rows), default=0)
        for row in rows:
            rating = row.get("avg_rating")
            rating_score = max(0.0, min(100.0, ((float(rating) - 5.5) / 4.5) * 100.0)) if rating is not None else 55.0
            impact = int(row.get("goals", 0) or 0) + .7 * int(row.get("assists", 0) or 0)
            impact_score = min(100.0, impact / max_impact * 100.0) if max_impact else 0.0
            motm_score = (int(row.get("motm_count", 0) or 0) / max_motm * 100.0) if max_motm else 0.0
            minutes_score = (int(row.get("minutes", 0) or 0) / max_minutes * 100.0) if max_minutes else 0.0
            score = wr * rating_score + wc * impact_score + wm * motm_score + wmin * minutes_score
            row["vote_score"] = round(score, 2)
            row["score_components"] = {
                "rating": round(rating_score, 1), "contribution": round(impact_score, 1),
                "motm": round(motm_score, 1), "minutes": round(minutes_score, 1),
            }
        rows.sort(key=lambda r: (-r.get("vote_score", 0), -(r.get("avg_rating") or 0),
                                 -(int(r.get("goals", 0)) + int(r.get("assists", 0))),
                                 -int(r.get("motm_count", 0)), -int(r.get("minutes", 0)),
                                 str(r.get("player", "")).casefold()))
        return rows

    def _award_rows_by_player_club(self, matches, player_lookup=None):
        """Aggregate real match contributions by player AND the club represented in that match.

        This avoids attributing a whole-season award to the player's final club after a
        mid-season transfer. The archive stores only the award winner's primary club,
        selected by the relevant contribution (or minutes for non-statistical awards).
        """
        lookup = player_lookup if player_lookup is not None else self._award_player_lookup()
        def safe_int(value):
            try:
                return int(float(value or 0))
            except (TypeError, ValueError, OverflowError):
                return 0
        totals = {}
        for match in matches or []:
            if not isinstance(match, dict) or not match.get("played", True):
                continue
            goal_stats = defaultdict(lambda: {"goals": 0, "assists": 0})
            goal_names = {}
            for event in match.get("goals", []) or []:
                if not isinstance(event, dict):
                    continue
                event_team = str(event.get("team") or "")
                scorer_id = str(event.get("scorer_id") or event.get("player_id") or "")
                assist_id = str(event.get("assist_id") or "")
                if scorer_id and event_team:
                    goal_stats[(scorer_id, event_team)]["goals"] += 1
                    goal_names[scorer_id] = event.get("scorer") or event.get("player") or ""
                if assist_id and event_team:
                    goal_stats[(assist_id, event_team)]["assists"] += 1
                    goal_names[assist_id] = event.get("assist") or ""

            for side, team_key, minutes_key in (("h", "home", "minutes_h"), ("a", "away", "minutes_a")):
                team = str(match.get(team_key) or "")
                if not team:
                    continue
                mins_map = match.get(minutes_key, {}) or {}
                side_stats = ((match.get("player_stats") or {}).get(side, {}) or {})
                starters = {str(x) for x in (match.get("xi_" + side, []) or []) if x}
                sub_entries = match.get("subs_" + side, []) or []
                ids = {str(pid) for pid, mins in mins_map.items() if safe_int(mins) > 0}
                ids.update(str(pid) for pid in side_stats)
                ids.update(starters)
                ids.update(pid for (pid, event_team), values in goal_stats.items()
                           if event_team == team and (values["goals"] or values["assists"]))
                for entry in sub_entries:
                    if isinstance(entry, (list, tuple)) and len(entry) >= 3:
                        if entry[1]:
                            ids.add(str(entry[1]))
                        if entry[2]:
                            ids.add(str(entry[2]))
                for pid in ids:
                    details = side_stats.get(pid, {}) or {}
                    minutes = safe_int(mins_map.get(pid, details.get("min", 0)))
                    if minutes <= 0 and pid in starters:
                        minutes = 90
                    if minutes <= 0:
                        for entry in sub_entries:
                            if isinstance(entry, (list, tuple)) and len(entry) >= 3 and str(entry[2] or "") == pid:
                                minutes = max(minutes, 90 - safe_int(entry[0]))
                    event_values = goal_stats[(pid, team)]
                    goals = safe_int(details.get("g", event_values["goals"]))
                    assists = safe_int(details.get("a", event_values["assists"]))
                    if minutes <= 0 and not details and not goals and not assists:
                        continue
                    row = totals.setdefault((pid, team), {
                        "player_id": pid,
                        "player": (lookup.get(pid, {}) or {}).get("common_name")
                                  or (lookup.get(pid, {}) or {}).get("player_name")
                                  or goal_names.get(pid) or pid,
                        "team": team, "apps": 0, "minutes": 0, "goals": 0, "assists": 0,
                    })
                    row["apps"] += 1
                    row["minutes"] += max(0, minutes)
                    row["goals"] += max(0, goals)
                    row["assists"] += max(0, assists)
        return sorted(totals.values(), key=lambda row: (row["player_id"], row["team"].casefold()))

    @staticmethod
    def _primary_award_club(rows, metric="minutes"):
        """Choose the club where a player made the largest measurable contribution."""
        if not rows:
            return ""
        metric = metric if metric in ("goals", "assists", "minutes", "apps") else "minutes"
        ordered = sorted(rows, key=lambda row: (
            int(row.get(metric, 0) or 0), int(row.get("minutes", 0) or 0),
            int(row.get("apps", 0) or 0), int(row.get("goals", 0) or 0),
            int(row.get("assists", 0) or 0), str(row.get("team", "")).casefold()), reverse=True)
        return str(ordered[0].get("team") or "")

    def _award_leader_rows(self, matches, metric="goals", player_lookup=None):
        """Return all tied leaders across clubs, retaining a defensible club attribution."""
        metric = metric if metric in ("goals", "assists") else "goals"
        grouped = defaultdict(list)
        for row in self._award_rows_by_player_club(matches, player_lookup):
            grouped[str(row.get("player_id") or "")].append(row)
        candidates = []
        for pid, club_rows in grouped.items():
            if not pid:
                continue
            total = {
                "player_id": pid,
                "player": club_rows[0].get("player") or pid,
                "goals": sum(int(row.get("goals", 0) or 0) for row in club_rows),
                "assists": sum(int(row.get("assists", 0) or 0) for row in club_rows),
                "apps": sum(int(row.get("apps", 0) or 0) for row in club_rows),
                "minutes": sum(int(row.get("minutes", 0) or 0) for row in club_rows),
                "team": self._primary_award_club(club_rows, metric),
            }
            candidates.append(total)
        best = max((int(row.get(metric, 0) or 0) for row in candidates), default=0)
        if best <= 0:
            return []
        leaders = [row for row in candidates if int(row.get(metric, 0) or 0) == best]
        leaders.sort(key=lambda row: (str(row.get("player", "")).casefold(), str(row.get("team", "")).casefold()))
        return leaders

    def _build_individual_awards(self, season, season_label, matches, season_awards, competition_awards):
        """Create the canonical, portable award ledger stored in each season archive."""
        official_labels = set(TIER_LABELS.values()) | {"Cup", "C1", "C1 Play-in"}
        official_matches = [match for match in (matches or [])
                            if isinstance(match, dict) and match.get("played", True)
                            and str(match.get("comp") or "") in official_labels]
        club_rows = self._award_rows_by_player_club(official_matches)
        player_clubs = defaultdict(list)
        for row in club_rows:
            player_clubs[str(row.get("player_id") or "")].append(row)
        primary_clubs = {pid: self._primary_award_club(rows, "minutes")
                         for pid, rows in player_clubs.items()}
        output, seen = [], set()

        def add(row, award_id, title, category, competition="Toàn mùa", rank=None,
                value=None, unit="", club_metric="minutes", detail=""):
            if not isinstance(row, dict):
                return
            pid = str(row.get("player_id") or "")
            if not pid:
                return
            token = (pid, str(award_id), str(competition))
            if token in seen:
                return
            seen.add(token)
            fallback_club = primary_clubs.get(pid, "")
            club = str(row.get("team") or fallback_club or "—")
            if club_metric in ("minutes", "apps") and fallback_club:
                club = fallback_club
            output.append({
                "season": int(season), "season_label": str(season_label),
                "player_id": pid, "player": str(row.get("player") or pid),
                "award_id": str(award_id), "title": str(title),
                "category": str(category), "competition": str(competition or "Toàn mùa"),
                "club": club or "—", "rank": rank,
                "value": value, "unit": str(unit or ""),
                "detail": str(detail or row.get("role") or row.get("position") or ""),
            })

        annual = season_awards if isinstance(season_awards, dict) else {}
        podium = annual.get("ballon_podium") or []
        podium_titles = (("ballon_dor", "Quả bóng Vàng"),
                         ("ballon_silver", "Quả bóng Bạc"),
                         ("ballon_bronze", "Quả bóng Đồng"))
        if podium:
            for rank, winner in enumerate(podium[:3], 1):
                award_id, title = podium_titles[rank - 1]
                add(winner, award_id, title, "season", "Ballon d'Or", rank=rank)
        else:
            for rank, (key, title) in enumerate(podium_titles, 1):
                add(annual.get(key), key, title, "season", "Ballon d'Or", rank=rank)

        add(annual.get("young_player") or annual.get("golden_boy"),
            "golden_boy", "Golden Boy", "season", "Toàn mùa")
        add(annual.get("golden_glove"), "golden_glove", "Găng tay Vàng", "season", "Toàn mùa")
        for row in annual.get("best_xi_442", []) or []:
            add(row, "best_xi", "Đội hình tiêu biểu", "season", "Toàn mùa",
                detail=row.get("role") or row.get("position") or "")

        # Golden Boot and season assist crown use official competition match logs,
        # not the player's current club field or a stale season_stats snapshot.
        for row in self._award_leader_rows(official_matches, "goals"):
            add(row, "golden_boot", "Chiếc giày Vàng", "season", "Tất cả giải đấu",
                rank=1, value=row.get("goals"), unit="bàn", club_metric="goals")
        for row in self._award_leader_rows(official_matches, "assists"):
            add(row, "season_assist_leader", "Vua kiến tạo mùa giải", "season", "Tất cả giải đấu",
                rank=1, value=row.get("assists"), unit="kiến tạo", club_metric="assists")

        for key, award in (competition_awards or {}).items():
            if not isinstance(award, dict) or not award.get("completed"):
                continue
            competition = str(award.get("competition") or key)
            for row in award.get("top_scorers", []) or []:
                add(row, f"top_scorer:{key}", "Vua phá lưới", "competition", competition,
                    rank=1, value=row.get("goals"), unit="bàn", club_metric="goals")
            for row in award.get("top_assists", []) or []:
                add(row, f"top_assist:{key}", "Vua kiến tạo", "competition", competition,
                    rank=1, value=row.get("assists"), unit="kiến tạo", club_metric="assists")
            add(award.get("player_of_tournament"), f"player_of_tournament:{key}",
                f"Cầu thủ xuất sắc · {competition}", "competition", competition)

        output.sort(key=lambda row: (row["season"], row["category"], row["competition"],
                                     row["title"], row["player"].casefold()))
        return output

    def _build_team_awards(self, season, season_label, matches, competition_awards,
                           fallback_record=None):
        """Create player-level collective honours backed by actual club appearances.

        A player receives a club honour only when match logs prove at least one
        appearance for that exact club in that exact competition. This deliberately
        avoids awarding trophies from the player's final club field or a roster-only
        association. Runner-up finishes are retained as achievements, not counted as
        championships.
        """
        registry = competition_awards if isinstance(competition_awards, dict) else {}
        fallback = fallback_record if isinstance(fallback_record, dict) else {}
        definitions = (
            ("premier", "Premier League", "champion_premier", None, "runner_up_premier"),
            ("fc1", TIER_LABELS["fc1"], None, "fc1", None),
            ("fc2", TIER_LABELS["fc2"], None, "fc2", None),
            ("cup", "Major Cup", "champion_cup", None, "runner_up_cup"),
            ("c1", "Champions League", "champion_c1", None, "runner_up_c1"),
        )
        output = []
        seen = set()
        for key, default_name, champion_field, league_key, runner_field in definitions:
            award = registry.get(key) if isinstance(registry.get(key), dict) else {}
            if award and award.get("completed") is False:
                continue
            competition = str(award.get("competition") or default_name)
            champion = award.get("champion")
            runner_up = award.get("runner_up")
            if not champion and champion_field:
                champion = fallback.get(champion_field)
            if not runner_up and runner_field:
                runner_up = fallback.get(runner_field)
            if not champion and league_key:
                rows = ((fallback.get("leagues") or {}).get(league_key) or [])
                champion = rows[0].get("team") if rows and isinstance(rows[0], dict) else None
                runner_up = rows[1].get("team") if len(rows) > 1 and isinstance(rows[1], dict) else None
            if key == "c1" and not runner_up:
                c1_record = fallback.get("c1") or {}
                runner_up = c1_record.get("runner_up") if isinstance(c1_record, dict) else None
            if not champion and not runner_up:
                continue

            competition_matches = self._award_competition_matches(key, matches or [])
            if not competition_matches:
                # No match-level evidence means we cannot know which players actually
                # represented the champion/runner-up club in this competition.
                continue
            club_rows = self._award_rows_by_player_club(competition_matches)
            for placement, club in (("champion", champion), ("runner_up", runner_up)):
                club_name = str(club or "").strip()
                if not club_name:
                    continue
                for row in club_rows:
                    if str(row.get("team") or "").casefold() != club_name.casefold():
                        continue
                    if int(row.get("apps", 0) or 0) < 1:
                        continue
                    pid = str(row.get("player_id") or "")
                    if not pid:
                        continue
                    award_id = f"team_{placement}:{key}"
                    token = (pid, award_id, club_name.casefold())
                    if token in seen:
                        continue
                    seen.add(token)
                    title = "Vô địch" if placement == "champion" else "Á quân"
                    output.append({
                        "season": int(season), "season_label": str(season_label),
                        "player_id": pid, "player": str(row.get("player") or pid),
                        "award_id": award_id, "title": title,
                        "category": "team", "achievement": placement,
                        "competition": competition, "club": club_name,
                        "rank": 1 if placement == "champion" else 2,
                        "appearances": int(row.get("apps", 0) or 0),
                        "minutes": int(row.get("minutes", 0) or 0),
                    })
        output.sort(key=lambda row: (row["season"], row["competition"].casefold(),
                                     row["rank"], row["club"].casefold(), row["player"].casefold()))
        return output

    def _competition_award_record(self, key, matches=None, player_lookup=None):
        """Create one finished competition's championship and player-of-tournament record."""
        all_matches = self.s.get("matches", []) if matches is None else matches
        played = [m for m in all_matches if m.get("played", True)]
        by_id = {int(m.get("id", -1)): m for m in played if m.get("id") is not None}
        if key == "premier":
            phase2 = self.s.get("league_phase2", {}).get("premier", {})
            match_ids = phase2.get("match_ids", {})
            counts = {group: sum(len(ids or []) for ids in (match_ids.get(group, []) or [])) for group in ("champion", "challenge")}
            complete = all(counts[group] >= 12 for group in counts)
            complete = complete and all(
                int(mid) in by_id for group in counts
                for round_ids in (match_ids.get(group, []) or []) for mid in (round_ids or [])
            )
            if not complete:
                return None
            tables = self.premier_phase2_tables()
            winner = tables["champion"][0]["team"] if tables.get("champion") else None
            runner = tables["champion"][1]["team"] if len(tables.get("champion", [])) > 1 else None
            name = "Premier League"
        elif key in ("fc1", "fc2"):
            label = TIER_LABELS[key]
            regular = [m for m in played if m.get("comp") == label and str(m.get("stage", "")).startswith(f"league_{key}_")]
            playoff = self.s.get("league_phase2", {}).get("promotion_playoffs", {}).get(key, {})
            final_id = playoff.get("final_match")
            final_match = by_id.get(int(final_id)) if final_id else None
            if len(regular) < 56 or not final_match or str(final_match.get("stage", "")).endswith("_semi"):
                return None
            table = self.league_phase1_table(key)
            winner = table[0]["team"] if table else None
            runner = table[1]["team"] if len(table) > 1 else None
            name = label
        elif key == "cup":
            final_match = next((m for m in played if m.get("stage") == "cup_final"), None)
            if not final_match:
                return None
            winner = self.s.get("cup", {}).get("winner") or final_match.get("winner")
            runner = self.s.get("cup", {}).get("runner_up")
            name = "Major Cup"
        elif key == "c1":
            final_match = next((m for m in played if m.get("stage") == "c1_final"), None)
            if not final_match:
                return None
            winner = self.s.get("c1", {}).get("champion") or final_match.get("winner")
            runner = self.s.get("c1", {}).get("runner_up")
            name = "Champions League"
        else:
            return None
        if not winner:
            return None
        competition_matches = self._award_competition_matches(key, played)
        candidates = self._rank_award_candidates(self._award_rows_from_matches(competition_matches, player_lookup))
        min_apps = max(2, min(6, int(math.ceil(len(competition_matches) * .06))))
        eligible = [row for row in candidates if int(row.get("apps", 0)) >= min_apps]
        if not eligible:
            eligible = [row for row in candidates if int(row.get("apps", 0)) >= 1]
        pot = dict(eligible[0]) if eligible else None
        club_rows = self._award_rows_by_player_club(competition_matches, player_lookup)
        if pot:
            player_clubs = [row for row in club_rows if str(row.get("player_id")) == str(pot.get("player_id"))]
            primary_club = self._primary_award_club(player_clubs, "minutes")
            if primary_club:
                pot["team"] = primary_club
        top_scorers = self._award_leader_rows(competition_matches, "goals", player_lookup)
        top_assists = self._award_leader_rows(competition_matches, "assists", player_lookup)
        last_date = max((str(m.get("date", "")) for m in competition_matches), default="")
        return {
            "key": key, "competition": name, "completed": True,
            "champion": winner, "runner_up": runner,
            "player_of_tournament": pot,
            "top_scorers": top_scorers, "top_assists": top_assists,
            "individual_awards_version": 1,
            "match_count": len(competition_matches), "completed_date": last_date,
            "eligibility_apps": min_apps,
        }

    def _refresh_completed_competition_awards(self, announce=True):
        """Settle completed competitions once, as soon as their final fixture is committed."""
        registry = self.s.setdefault("competition_awards", {})
        names = {"premier": "Premier League", "fc1": TIER_LABELS["fc1"],
                 "fc2": TIER_LABELS["fc2"], "cup": "Major Cup", "c1": "Champions League"}
        created = []
        for key in ("premier", "fc1", "fc2", "cup", "c1"):
            existing = registry.get(key)
            if isinstance(existing, dict) and existing.get("completed"):
                if int(existing.get("individual_awards_version", 0) or 0) < 1:
                    refreshed = self._competition_award_record(key)
                    if refreshed:
                        for field in ("top_scorers", "top_assists", "individual_awards_version"):
                            existing[field] = refreshed.get(field, [] if field != "individual_awards_version" else 1)
                continue
            record = self._competition_award_record(key)
            if not record:
                continue
            registry[key] = record
            created.append(record)
            if announce:
                pot = record.get("player_of_tournament") or {}
                headline = f"{record['champion']} vô địch {record['competition']}"
                body = (f"Cầu thủ xuất sắc giải: {pot.get('player', 'Chưa đủ dữ liệu')}"
                        if pot else "Chưa đủ dữ liệu để bình chọn Cầu thủ xuất sắc giải.")
                item = {"kind": "competition_award", "format_version": 1,
                        "season": int(self.s.get("season", 0)), "date": record.get("completed_date", ""),
                        "comp": record["competition"], "stage": "Trao danh hiệu",
                        "headline": headline, "body": body, "text": f"{headline} · {body}"}
                news = self.s.setdefault("news", [])
                news.append(item)
                self.s["news"] = match_news.retain_recent_news(
                    news, match_news.reference_date_from_state(self.s))
        if created and announce:
            self.s["last_news"] = [f"{row['competition']}: {row['champion']} vô địch · Cầu thủ xuất sắc: "
                                    f"{(row.get('player_of_tournament') or {}).get('player', '—')}"
                                    for row in created[:8]]
        return created

    def competition_award_status(self):
        """UI-friendly status for each current-season competition award."""
        registry = self.s.get("competition_awards", {}) or {}
        names = (("premier", "Premier League"), ("fc1", TIER_LABELS["fc1"]),
                 ("fc2", TIER_LABELS["fc2"]), ("cup", "Major Cup"), ("c1", "Champions League"))
        result = []
        for key, name in names:
            award = registry.get(key) if isinstance(registry.get(key), dict) else None
            result.append({"key": key, "competition": name,
                           "status": "Đã trao" if award and award.get("completed") else "Đang thi đấu",
                           "champion": (award or {}).get("champion"),
                           "player_of_tournament": (award or {}).get("player_of_tournament"),
                           "completed_date": (award or {}).get("completed_date", ""),
                           "match_count": (award or {}).get("match_count", 0)})
        return result

    @staticmethod
    def _award_match_appearances(match):
        """Return (player_id, team) pairs for players who actually appeared in a match."""
        appearances = set()
        if not match or not match.get("played", True):
            return appearances
        player_stats = match.get("player_stats", {}) or {}
        for side, team_key, minutes_key in (("h", "home", "minutes_h"), ("a", "away", "minutes_a")):
            team = str(match.get(team_key, "") or "")
            if not team:
                continue
            ids = set()
            minutes_map = match.get(minutes_key, {}) or {}
            for pid, minutes in minutes_map.items():
                try:
                    if int(minutes or 0) > 0:
                        ids.add(str(pid))
                except (TypeError, ValueError):
                    continue
            ids.update(str(pid) for pid in (match.get("xi_" + side, []) or []) if pid)
            side_stats = player_stats.get(side, {}) or {}
            for pid, details in side_stats.items():
                if not pid or not isinstance(details, dict):
                    continue
                raw_minutes = details.get("min")
                if raw_minutes is None:
                    # Legacy records without a minutes field still use the stats row
                    # as evidence of an appearance.
                    ids.add(str(pid))
                else:
                    try:
                        if int(raw_minutes or 0) > 0:
                            ids.add(str(pid))
                    except (TypeError, ValueError):
                        continue
            for entry in match.get("subs_" + side, []) or []:
                if isinstance(entry, (list, tuple)) and len(entry) >= 3:
                    # Both the substituted-off and substituted-on players have appeared.
                    if entry[1]:
                        ids.add(str(entry[1]))
                    if entry[2]:
                        ids.add(str(entry[2]))
            for card in match.get("cards_" + side, []) or []:
                pid = card.get("p") if isinstance(card, dict) else None
                if pid:
                    ids.add(str(pid))
            appearances.update((pid, team) for pid in ids)

        # Goal involvement is a final compatibility fallback for older match schemas.
        for event in match.get("goals", []) or []:
            team = str(event.get("team", "") or "")
            if team not in (str(match.get("home", "")), str(match.get("away", ""))):
                continue
            for key in ("scorer_id", "assist_id"):
                pid = str(event.get(key) or "")
                if pid:
                    appearances.add((pid, team))
        return appearances

    def _season_award_eligibility(self, matches, archived_eligibility=None):
        """Eligibility for annual awards; use precomputed sets for compact season archives."""
        if isinstance(archived_eligibility, dict):
            return {
                "premier_champion_players": set(str(x) for x in archived_eligibility.get("premier_champion_players", []) or []),
                "c1_semifinal_players": set(str(x) for x in archived_eligibility.get("c1_semifinal_players", []) or []),
            }
        premier_champion_players = set()
        c1_semifinal_teams = set()
        c1_players_by_team = defaultdict(set)
        premier_pattern = re.compile(r"^league_p2_premier_champion_\d+$")
        c1_semi_pattern = re.compile(r"^c1_semi\d+_leg[12]$")

        for match in matches or []:
            if not match.get("played", True):
                continue
            comp = str(match.get("comp", ""))
            stage = str(match.get("stage", ""))
            round_label = str(match.get("round", "")).casefold()
            appearances = self._award_match_appearances(match)

            is_premier_champion_match = (
                comp == TIER_LABELS["premier"] and
                (bool(premier_pattern.fullmatch(stage)) or round_label.startswith("champion ·"))
            )
            if is_premier_champion_match:
                premier_champion_players.update(pid for pid, _team in appearances)

            if comp == "C1" and (c1_semi_pattern.fullmatch(stage) or "bán kết" in round_label):
                c1_semifinal_teams.update((str(match.get("home", "")), str(match.get("away", ""))))

            if comp == "C1":
                for pid, team in appearances:
                    c1_players_by_team[team].add(pid)

        c1_semifinal_players = set()
        for team in c1_semifinal_teams:
            c1_semifinal_players.update(c1_players_by_team.get(team, set()))
        return {
            "premier_champion_players": premier_champion_players,
            "c1_semifinal_players": c1_semifinal_players,
        }

    def _season_award_candidates(self, stats=None, matches=None, archived_rows=None, eligibility_record=None):
        """Build annual ballot candidates and attach hard eligibility for Best XI/Ballon d'Or."""
        source_stats = stats if stats is not None else self.s.get("season_stats", {})
        match_rows = matches if matches is not None else self.s.get("matches", [])
        eligibility = self._season_award_eligibility(match_rows, archived_eligibility=eligibility_record)
        motm = Counter()
        for match in match_rows or []:
            value = match.get("motm")
            if isinstance(value, (list, tuple)) and len(value) >= 2 and value[1]:
                motm[str(value[1])] += 1
        player_lookup = self._award_player_lookup()
        team_by_player = {str(pid): team for team, ids in self.s.get("rosters", {}).items() for pid in ids}
        candidates = []
        if archived_rows is not None:
            raw_rows = archived_rows
            for raw in raw_rows:
                pid = str(raw.get("player_id") or "")
                if not pid:
                    continue
                p = player_lookup.get(pid, {})
                age = None
                if p.get("birthdate"):
                    try:
                        age = _age_from_birthdate(p.get("birthdate"), date(int(self.s.get("season", 2026)), 8, 1))
                    except Exception:
                        age = None
                candidates.append({
                    "player_id": pid, "player": raw.get("player") or p.get("common_name") or pid,
                    "team": raw.get("team") or team_by_player.get(pid) or p.get("club", ""),
                    "position": raw.get("position") or p.get("position", ""),
                    "apps": int(raw.get("apps", 0) or 0), "minutes": int(raw.get("minutes", 0) or 0),
                    "goals": int(raw.get("goals", 0) or 0), "assists": int(raw.get("assists", 0) or 0),
                    "avg_rating": raw.get("avg_rating"), "motm_count": int(raw.get("motm_count", motm.get(pid, 0)) or 0),
                    "age": age, "potential": None,
                })
        else:
            for pid, stat in source_stats.items():
                p = player_lookup.get(str(pid))
                if not p or int(stat.get("apps", 0) or 0) <= 0:
                    continue
                meta = self.s.get("player_meta", {}).get(pid, {})
                candidates.append({
                    "player_id": str(pid), "player": p.get("common_name", str(pid)),
                    "team": team_by_player.get(str(pid), p.get("club", "")),
                    "position": p.get("position", ""),
                    "apps": int(stat.get("apps", 0) or 0), "minutes": int(stat.get("mins", 0) or 0),
                    "goals": int(stat.get("goals", 0) or 0), "assists": int(stat.get("assists", 0) or 0),
                    "avg_rating": (round(float(stat.get("rating_sum", 0)) / int(stat.get("rating_count", 0)), 2)
                                   if int(stat.get("rating_count", 0) or 0) else None),
                    "motm_count": int(motm.get(str(pid), 0)),
                    "age": float(meta.get("age", 26)), "potential": float(meta.get("potential", p.get("overall_rating", 50))),
                    "gk_apps": int(stat.get("gk_apps", 0) or 0),
                    "gk_minutes": int(stat.get("gk_minutes", 0) or 0),
                    "gk_clean_sheets": int(stat.get("gk_clean_sheets", 0) or 0),
                    "gk_goals_conceded": int(stat.get("gk_goals_conceded", 0) or 0),
                    "gk_saves": int(stat.get("gk_saves", 0) or 0),
                })
        for row in candidates:
            pid = str(row.get("player_id", ""))
            reasons = []
            if pid in eligibility["premier_champion_players"]:
                reasons.append("Đã ra sân tại bảng Champion Premier League")
            if pid in eligibility["c1_semifinal_players"]:
                reasons.append("Đã ra sân C1 cùng CLB vào bán kết")
            settings = getattr(self, "award_settings", AWARD_SETTINGS_DEFAULTS)
            try:
                apps = int(row.get("apps", 0) or 0)
            except (TypeError, ValueError):
                apps = 0
            required_paths = []
            if settings.get("require_premier_champion", True):
                required_paths.append(pid in eligibility["premier_champion_players"])
            if settings.get("require_c1_semifinal", True):
                required_paths.append(pid in eligibility["c1_semifinal_players"])
            competition_ok = any(required_paths) if required_paths else True
            min_apps = int(settings.get("min_appearances", 21) or 21)
            row["award_eligible"] = bool(apps >= min_apps and competition_ok)
            row["eligibility_reasons"] = reasons
            row["eligibility_note"] = (
                "Đủ điều kiện: " + str(min_apps) + " trận; " + ("; ".join(reasons) if reasons else "đạt ngưỡng số trận")
                if row["award_eligible"] else
                (f"Chưa đủ {min_apps} trận" if apps < min_apps else "Không đạt điều kiện giải đấu đã bật")
            )
        return self._rank_award_candidates(candidates, settings=getattr(self, "award_settings", None))

    @staticmethod
    def _legacy_premier_runner_up_from_record(season_record):
        """Rebuild the Champion-group runner-up from an older season archive."""
        phase2_root = season_record.get("league_phase2", {}) if isinstance(season_record, dict) else {}
        premier = phase2_root.get("premier", {}) if isinstance(phase2_root, dict) else {}
        phase1_rows = premier.get("phase1_table", []) if isinstance(premier, dict) else []
        teams = list(premier.get("champion_teams", []) or []) if isinstance(premier, dict) else []
        if not teams and phase1_rows:
            teams = [row.get("team") for row in phase1_rows[:4] if row.get("team")]
        if not teams:
            league_rows = (season_record.get("leagues", {}) or {}).get("premier", []) or []
            teams = [row.get("team") for row in league_rows[:4] if row.get("team")]
        teams = [str(team) for team in teams if team]
        if len(teams) < 2:
            return None
        base_by_team = {str(row.get("team")): row for row in phase1_rows if row.get("team")}
        table = {}
        for team in teams:
            base = base_by_team.get(team, {})
            table[team] = {"pts": int(base.get("pts", 0) or 0),
                           "gf": int(base.get("gf", 0) or 0),
                           "ga": int(base.get("ga", 0) or 0),
                           "h2h": int(base.get("h2h", 0) or 0)}
        phase2_matches = [match for match in season_record.get("matches", []) or []
                          if isinstance(match, dict) and match.get("played", True)
                          and match.get("comp") == "Premier League"
                          and str(match.get("stage", "")).startswith("league_p2_premier_champion_")]
        if len(phase2_matches) < 12:
            return None
        for match in phase2_matches:
            home, away = str(match.get("home", "")), str(match.get("away", ""))
            if home not in table or away not in table:
                continue
            try:
                hg, ag = int(match.get("hg", 0)), int(match.get("ag", 0))
            except (TypeError, ValueError):
                continue
            table[home]["gf"] += hg; table[home]["ga"] += ag
            table[away]["gf"] += ag; table[away]["ga"] += hg
            if hg > ag:
                table[home]["pts"] += 3; table[home]["h2h"] += 3
            elif hg < ag:
                table[away]["pts"] += 3; table[away]["h2h"] += 3
            else:
                table[home]["pts"] += 1; table[away]["pts"] += 1
                table[home]["h2h"] += 1; table[away]["h2h"] += 1
        ordered = sorted(table, key=lambda team: (-table[team]["pts"],
                         -(table[team]["gf"] - table[team]["ga"]),
                         -table[team]["gf"], -table[team]["h2h"], team.casefold()))
        return ordered[1] if len(ordered) > 1 else None

    def _season_champion_teams(self, season_record=None):
        """Return champion and runner-up clubs for Best XI preference only."""
        if isinstance(season_record, dict):
            awards = season_record.get("competition_awards", {}) or {}
            premier_award = awards.get("premier", {}) or {}
            c1_award = awards.get("c1", {}) or {}
            cup_award = awards.get("cup", {}) or {}
            c1_record = season_record.get("c1", {}) or {}
            cup_record = season_record.get("cup", {}) or {}
            premier = (premier_award.get("champion") or season_record.get("champion_premier"))
            c1 = (c1_award.get("champion") or season_record.get("champion_c1")
                  or c1_record.get("champion"))
            premier_runner_up = (self._legacy_premier_runner_up_from_record(season_record)
                                 or premier_award.get("runner_up") or season_record.get("runner_up_premier")
                                 or ((season_record.get("league_phase2") or {}).get("premier", {}) or {}).get("runner_up"))
            c1_runner_up = (c1_award.get("runner_up") or season_record.get("runner_up_c1")
                            or c1_record.get("runner_up"))
            major_cup = (cup_award.get("champion") or season_record.get("champion_cup")
                         or cup_record.get("winner") or cup_record.get("champion"))
        else:
            awards = self.s.get("competition_awards", {}) or {}
            premier_award = awards.get("premier", {}) or {}
            c1_award = awards.get("c1", {}) or {}
            cup_award = awards.get("cup", {}) or {}
            premier = premier_award.get("champion")
            c1 = c1_award.get("champion") or self.s.get("c1", {}).get("champion")
            premier_runner_up = premier_award.get("runner_up")
            c1_runner_up = c1_award.get("runner_up") or self.s.get("c1", {}).get("runner_up")
            major_cup = cup_award.get("champion") or self.s.get("cup", {}).get("winner")
            if not premier and self.s.get("season_done") and self.s.get("history"):
                latest = self.s["history"][-1]
                premier = latest.get("champion_premier")
                latest_awards = latest.get("competition_awards", {}) or {}
                premier_runner_up = (self._legacy_premier_runner_up_from_record(latest) or premier_runner_up or
                                     (latest_awards.get("premier", {}) or {}).get("runner_up") or
                                     latest.get("runner_up_premier"))
                c1_runner_up = (c1_runner_up or (latest_awards.get("c1", {}) or {}).get("runner_up") or
                                latest.get("runner_up_c1"))
                major_cup = (major_cup or (latest_awards.get("cup", {}) or {}).get("champion") or
                             latest.get("champion_cup"))
        return {
            "premier": str(premier) if premier else None,
            "c1": str(c1) if c1 else None,
            "premier_runner_up": str(premier_runner_up) if premier_runner_up else None,
            "c1_runner_up": str(c1_runner_up) if c1_runner_up else None,
            "major_cup": str(major_cup) if major_cup else None,
        }

    @staticmethod
    def _best_xi_honor_bonus(team, champion_teams, settings=None):
        """Best XI-only club honours, configurable from award_settings.json."""
        if not team:
            return 0.0, []
        settings = dict(AWARD_SETTINGS_DEFAULTS, **(settings or {}))
        bonus = 0.0
        honors = []
        rules = (
            ("c1", float(settings["c1_champion_bonus"]), "Vô địch Champions League"),
            ("premier", float(settings["premier_champion_bonus"]), "Vô địch Premier League"),
            ("c1_runner_up", float(settings["c1_runner_up_bonus"]), "Á quân Champions League"),
            ("premier_runner_up", float(settings["premier_runner_up_bonus"]), "Á quân Premier League"),
            ("major_cup", float(settings["major_cup_champion_bonus"]), "Vô địch Major Cup"),
        )
        for key, value, label in rules:
            if champion_teams.get(key) and str(team) == str(champion_teams[key]):
                bonus += value
                honors.append(label)
        return round(bonus, 2), honors

    def _make_best_xi_442(self, candidates, champion_teams=None, settings=None):
        """Build a strict 4-4-2 from the Ballon Top 20, using C1 champions only as positional fallback.

        Every selected player must still pass the normal annual eligibility gate (>20 apps and
        a Premier Champion or Champions League semi-final appearance). First, each role is
        filled from the overall Ballon Top 20. Only when no unused Top-20 player fits a role
        may an eligible player from the Champions League winning club outside the Top 20 fill it.
        """
        champions = champion_teams if champion_teams is not None else self._season_champion_teams()
        pool = [dict(row) for row in candidates if bool(row.get("award_eligible"))]

        def identity(row):
            return str(row.get("player_id") or f"{row.get('team', '')}::{row.get('player', '')}")

        def ballot_sort_key(row):
            return (-float(row.get("vote_score", 0) or 0),
                    -(float(row.get("avg_rating") or 0)),
                    -(int(row.get("goals", 0) or 0) + int(row.get("assists", 0) or 0)),
                    -int(row.get("motm_count", 0) or 0),
                    -int(row.get("minutes", 0) or 0),
                    str(row.get("player", "")).casefold())

        for row in pool:
            bonus, honors = self._best_xi_honor_bonus(
                row.get("team"), champions, settings or getattr(self, "award_settings", None))
            row["best_xi_bonus"] = round(bonus, 2)
            row["best_xi_score"] = round(float(row.get("vote_score", 0) or 0) + bonus, 2)
            row["team_honors"] = list(honors)

        # Ballon Top 20 is ranked before Best-XI club bonuses, matching the
        # Top 20 / Gold-Silver-Bronze ballot shown by the auxiliary tool.
        ballot_pool = sorted(pool, key=ballot_sort_key)
        top20 = ballot_pool[:20]
        top20_ids = {identity(row) for row in top20}
        # Within the Top 20, club honours can determine which player wins a
        # positional place. The fallback list is deliberately outside Top 20.
        top20.sort(key=lambda r: (-r.get("best_xi_score", 0), *ballot_sort_key(r)[1:]))
        c1_champion = str(champions.get("c1") or "").strip().casefold()
        c1_fallback_pool = [row for row in pool
                            if identity(row) not in top20_ids
                            and c1_champion
                            and str(row.get("team") or "").strip().casefold() == c1_champion]
        c1_fallback_pool.sort(key=lambda r: (-r.get("best_xi_score", 0), *ballot_sort_key(r)[1:]))

        # Keep the 4-4-2 positional structure strict. Wide forwards (LW/RW)
        # may serve as LM/RM; LWB/RWB may serve as full-backs.
        slots = [
            ("GK", {"GK", "G"}),
            ("LB", {"LB", "LWB"}), ("CB", {"CB", "SW"}), ("CB", {"CB", "SW"}),
            ("RB", {"RB", "RWB"}),
            ("LM", {"LM", "LW"}),
            ("CM", {"CM", "CDM", "CAM", "DM", "AM", "LCM", "RCM"}),
            ("CM", {"CM", "CDM", "CAM", "DM", "AM", "LCM", "RCM"}),
            ("RM", {"RM", "RW"}),
            ("ST", {"ST", "CF", "FW"}), ("ST", {"ST", "CF", "FW"}),
        ]
        chosen, used = [], set()
        for role, positions in slots:
            pick = next((row for row in top20
                         if identity(row) not in used
                         and str(row.get("position", "")).strip().upper() in positions), None)
            source = "top20"
            if pick is None:
                pick = next((row for row in c1_fallback_pool
                             if identity(row) not in used
                             and str(row.get("position", "")).strip().upper() in positions), None)
                source = "c1_champion_fallback"
            if pick is None:
                # Do not violate the appearance/competition/position requirements
                # merely to force eleven names into the formation.
                continue
            row = dict(pick)
            row["role"] = role
            row["best_xi_selection_source"] = source
            row["best_xi_fallback"] = source == "c1_champion_fallback"
            if source == "c1_champion_fallback":
                row["team_honors"] = list(row.get("team_honors") or []) + [
                    "Bổ sung vị trí từ CLB vô địch Champions League (ngoài Top 20)"
                ]
            used.add(identity(row))
            chosen.append(row)
        return chosen

    def _award_settings_signature(self):
        """Stable marker so historic awards can be refreshed when shared settings change."""
        settings = getattr(self, "award_settings", AWARD_SETTINGS_DEFAULTS)
        packed = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"{zlib.crc32(packed.encode('utf-8')):08x}"

    @staticmethod
    def _calculate_golden_glove(candidates):
        """Select the goalkeeper award only from verified goalkeeper season totals."""
        keepers = []
        for raw in candidates:
            row = dict(raw)
            apps = int(row.get("gk_apps", 0) or 0)
            minutes = int(row.get("gk_minutes", 0) or 0)
            if str(row.get("position", "")).upper() != "GK" or apps < 10 or minutes < 900:
                continue
            clean = int(row.get("gk_clean_sheets", 0) or 0)
            conceded = int(row.get("gk_goals_conceded", 0) or 0)
            saves = int(row.get("gk_saves", 0) or 0)
            avg_rating = float(row.get("avg_rating") or 6.0)
            clean_rate = clean / max(1, apps)
            goals_per_90 = conceded * 90.0 / max(1, minutes)
            saves_per_90 = saves * 90.0 / max(1, minutes)
            score = (clean_rate * 55.0 + max(0.0, min(10.0, avg_rating)) * 3.0 +
                     min(15.0, saves_per_90 * 2.0) - min(20.0, goals_per_90 * 10.0))
            row.update({"golden_glove_score": round(score, 2),
                        "clean_sheet_rate": round(clean_rate, 3),
                        "goals_conceded_per_90": round(goals_per_90, 3),
                        "saves_per_90": round(saves_per_90, 3)})
            keepers.append(row)
        keepers.sort(key=lambda r: (-r["golden_glove_score"],
                                    -int(r.get("gk_clean_sheets", 0) or 0),
                                    float(r.get("avg_rating") or 0) * -1,
                                    str(r.get("player", "")).casefold()))
        return dict(keepers[0]) if keepers else None

    def _calculate_best_coach(self):
        """Transparent season award from final league finish, titles and promotion."""
        _points, tables = self._season_total_table()
        awards = self.s.get("competition_awards", {}) or {}
        promoted = {str(row.get("team")) for row in self.s.get("last_promotions", {}).get("promoted", [])
                    if isinstance(row, dict)}
        score_by_team = {}
        reasons_by_team = {}
        for tier, rows in (tables or {}).items():
            for rank, row in enumerate(rows or [], 1):
                team = str(row.get("team", ""))
                if not team:
                    continue
                base = max(0.0, 22.0 - (rank - 1) * 2.4)
                score_by_team[team] = score_by_team.get(team, 0.0) + base
                reasons_by_team.setdefault(team, []).append(f"Hạng {rank} {TIER_LABELS.get(tier, tier)}")
        title_values = {"premier": 14.0, "fc1": 12.0, "fc2": 10.0, "cup": 9.0, "c1": 13.0}
        for key, value in title_values.items():
            champion = (awards.get(key) or {}).get("champion") if isinstance(awards.get(key), dict) else None
            if champion:
                score_by_team[str(champion)] = score_by_team.get(str(champion), 0.0) + value
                reasons_by_team.setdefault(str(champion), []).append(f"Vô địch {key.upper()}")
        for team in promoted:
            score_by_team[team] = score_by_team.get(team, 0.0) + 5.0
            reasons_by_team.setdefault(team, []).append("Thăng hạng")
        coaches = self.s.get("coaches", {}) or {}
        rows = []
        for team, score in score_by_team.items():
            coach = coaches.get(team, {}) or {}
            rows.append({"team": team, "coach": coach.get("name") or "HLV AI",
                         "score": round(score, 2), "reasons": reasons_by_team.get(team, [])})
        rows.sort(key=lambda r: (-r["score"], r["team"].casefold()))
        return dict(rows[0]) if rows else None

    def _calculate_season_awards(self):
        """Ballon podium and 4-4-2 use strict eligibility; young-player award remains separate."""
        candidates = self._season_award_candidates()
        eligible = [row for row in candidates if row.get("award_eligible")]
        podium = eligible[:3]
        young_pool = [row for row in eligible if row.get("age") is not None and 16 <= float(row["age"]) <= 21]
        if not young_pool:
            young_pool = [row for row in candidates if row.get("age") is not None and 16 <= float(row["age"]) <= 21
                          and int(row.get("apps", 0)) >= 1]
        for row in young_pool:
            potential = row.get("potential")
            potential_score = max(0.0, min(100.0, (float(potential or 50) - 50.0) / 49.0 * 100.0))
            age = float(row.get("age", 21))
            age_score = max(0.0, min(100.0, ((22.0 - age) / 6.0) * 100.0))
            row["young_vote_score"] = round(.75 * float(row.get("vote_score", 0)) + .20 * potential_score + .05 * age_score, 2)
        young_pool.sort(key=lambda r: (-r.get("young_vote_score", 0), -r.get("vote_score", 0),
                                       -int(r.get("apps", 0)), str(r.get("player", "")).casefold()))
        award_settings = getattr(self, "award_settings", AWARD_SETTINGS_DEFAULTS)
        best_xi = self._make_best_xi_442(candidates, settings=award_settings)
        min_apps = int(award_settings.get("min_appearances", 21))
        paths_text = []
        if award_settings.get("require_premier_champion", True):
            paths_text.append("ra sân ở bảng Champion Premier League")
        if award_settings.get("require_c1_semifinal", True):
            paths_text.append("ra sân C1 cùng CLB vào bán kết")
        eligibility_text = " hoặc ".join(paths_text) if paths_text else "không yêu cầu giải đấu cụ thể"
        method = (f"Điều kiện bắt buộc cho Quả bóng Vàng/Bạc/Đồng: ít nhất {min_apps} trận và {eligibility_text}. "
                  f"Trọng số: {award_settings.get('weight_rating', 42):g}% điểm trung bình trận, "
                  f"{award_settings.get('weight_contribution', 28):g}% đóng góp bàn thắng/kiến tạo, "
                  f"{award_settings.get('weight_motm', 20):g}% MOTM, "
                  f"{award_settings.get('weight_minutes', 10):g}% số phút thi đấu. "
                  "Cầu thủ trẻ: 75% điểm mùa, 20% tiềm năng, 5% hệ số tuổi (16–21), là giải thưởng riêng.")
        best_xi_method = (f"Điều kiện bắt buộc: ít nhất {min_apps} trận và {eligibility_text}. Nếu không đủ 11 người đạt điều kiện, "
                          "đội hình giữ đúng nhóm vị trí GK–DEF–MID–FWD: ưu tiên cầu thủ trong Top 20; nếu thiếu vị trí mới lấy cầu thủ cùng vị trí từ CLB vô địch C1 ngoài Top 20 (vẫn phải đủ điều kiện và trên 20 trận). Nếu vẫn thiếu thì để trống, không lấy sai vị trí. "
                          f"Sau khi lọc điều kiện, dùng điểm cá nhân; trọng số bình chọn theo cấu hình: "
                          f"{award_settings.get('weight_rating', 42):g}% rating, {award_settings.get('weight_contribution', 28):g}% bàn/kiến tạo, "
                          f"{award_settings.get('weight_motm', 20):g}% MOTM, {award_settings.get('weight_minutes', 10):g}% phút; "
                          f"ưu tiên CLB: vô địch C1 +{award_settings.get('c1_champion_bonus', 5):g}, "
                          f"vô địch Premier +{award_settings.get('premier_champion_bonus', 4):g}, "
                          f"á quân C1 +{award_settings.get('c1_runner_up_bonus', 4):g}, "
                          f"á quân Premier +{award_settings.get('premier_runner_up_bonus', 3):g}, "
                          f"vô địch Major Cup +{award_settings.get('major_cup_champion_bonus', 3):g}; cộng dồn. "
                          "Điểm ưu tiên chỉ áp dụng cho đội hình 4-4-2.")
        return {
            "ballon_dor": dict(podium[0]) if len(podium) > 0 else None,
            "ballon_silver": dict(podium[1]) if len(podium) > 1 else None,
            "ballon_bronze": dict(podium[2]) if len(podium) > 2 else None,
            "ballon_podium": [dict(row) for row in podium],
            "top20_players": [
                {**dict(row), "rank": rank,
                 "ballon_award": {1: "Quả bóng Vàng", 2: "Quả bóng Bạc", 3: "Quả bóng Đồng"}.get(rank)}
                for rank, row in enumerate(eligible[:20], 1)
            ],
            "top20_version": 1,
            "young_player": dict(young_pool[0]) if young_pool else None,
            "golden_glove": self._calculate_golden_glove(candidates),
            "best_coach": self._calculate_best_coach(),
            "best_xi_442": best_xi,
            "best_xi_version": 8,
            "best_xi_fallback_rule": ("Ưu tiên Top 20; nếu thiếu vị trí, bổ sung cầu thủ cùng vị trí từ CLB vô địch C1 ngoài Top 20, "
                                     "vẫn phải đủ điều kiện và trên 20 trận. Nếu không có thì để trống."),
            "eligibility_rule": ("Trên 20 trận và ra sân ở bảng Champion Premier League "
                                 "hoặc ra sân C1 cùng CLB vào bán kết."),
            "best_xi_method": best_xi_method,
            "eligible_players": len(eligible),
            "recalculation_settings": dict(award_settings),
            "award_settings_signature": self._award_settings_signature(),
            "method": method,
        }

    def _backfill_history_awards(self):
        """Build awards for older archived seasons when their saved match/player data permits it."""
        before = json.dumps(self.s.get("history", []) or [], ensure_ascii=False, sort_keys=True, default=str)
        lookup = self._award_player_lookup()
        for record in self.s.get("history", []) or []:
            matches = [m for m in record.get("matches", []) or [] if m.get("played", True)]
            if not record.get("competition_awards"):
                awards = {}
                for key, champion in (("premier", record.get("champion_premier")),
                                       ("fc1", (record.get("leagues", {}).get("fc1", [{}]) or [{}])[0].get("team")),
                                       ("fc2", (record.get("leagues", {}).get("fc2", [{}]) or [{}])[0].get("team")),
                                       ("cup", record.get("champion_cup")), ("c1", record.get("champion_c1"))):
                    if not champion:
                        continue
                    comp_matches = self._award_competition_matches(key, matches)
                    candidates = self._rank_award_candidates(self._award_rows_from_matches(comp_matches, lookup))
                    min_apps = max(2, min(6, int(math.ceil(len(comp_matches) * .06))))
                    eligible = [row for row in candidates if int(row.get("apps", 0)) >= min_apps]
                    if not eligible:
                        eligible = [row for row in candidates if int(row.get("apps", 0)) >= 1]
                    runner = None
                    if key == "premier":
                        runner = self._legacy_premier_runner_up_from_record(record)
                        if not runner:
                            rows = record.get("leagues", {}).get("premier", []) or []
                            runner = rows[1].get("team") if len(rows) > 1 else None
                    elif key in ("fc1", "fc2"):
                        rows = record.get("leagues", {}).get(key, []) or []
                        runner = rows[1].get("team") if len(rows) > 1 else None
                    elif key == "cup":
                        runner = record.get("runner_up_cup")
                    elif key == "c1":
                        runner = record.get("runner_up_c1")
                    awards[key] = {"key": key, "competition": {"premier":"Premier League", "fc1":TIER_LABELS["fc1"],
                                    "fc2":TIER_LABELS["fc2"], "cup":"Major Cup", "c1":"Champions League"}[key],
                                    "completed": True, "champion": champion, "runner_up": runner,
                                    "player_of_tournament": dict(eligible[0]) if eligible else None,
                                    "match_count": len(comp_matches), "legacy_backfill": True}
                record["competition_awards"] = awards
            # Rebuild team honours only after legacy competition winners/runners-up
            # have been restored. Compact archives without appearance evidence remain
            # empty rather than attributing trophies from a player's last-known club.
            if int(record.get("team_awards_version", 0) or 0) < 1:
                season_number = int(record.get("season", 0) or 0)
                season_label = str(record.get("label") or
                                   (f"{season_number}/{season_number + 1}" if season_number else "—"))
                record["team_awards"] = self._build_team_awards(
                    season_number, season_label, matches,
                    record.get("competition_awards") or {}, fallback_record=record)
                record["team_awards_version"] = 1
            if not record.get("season_awards") and (record.get("career_player_stats") or matches):
                try:
                    # Use season aggregates when available, otherwise rebuild player totals
                    # from archived match logs. Legacy potential may be estimated.
                    season_number = int(record.get("season", self.s.get("season", 2026)))
                    original_season = self.s.get("season")
                    self.s["season"] = season_number
                    archived_totals = record.get("career_player_stats") or self._award_rows_from_matches(matches, lookup)
                    try:
                        candidates = self._season_award_candidates(matches=matches,
                                                                   archived_rows=archived_totals,
                                                                   eligibility_record=record.get("award_eligibility"))
                    finally:
                        self.s["season"] = original_season
                    eligible = [r for r in candidates if r.get("award_eligible")]
                    podium = eligible[:3]
                    young = [r for r in candidates if r.get("age") is not None and 16 <= float(r["age"]) <= 21
                             and int(r.get("apps", 0)) >= 1]
                    for row in young:
                        row["young_vote_score"] = round(.80 * float(row.get("vote_score", 0)) +
                                                         .20 * max(0.0, min(100.0, (22-float(row["age"])) / 6 * 100)), 2)
                    young.sort(key=lambda r: (-r.get("young_vote_score", 0), -r.get("vote_score", 0)))
                    record["season_awards"] = {
                        "ballon_dor": dict(podium[0]) if podium else None,
                        "ballon_silver": dict(podium[1]) if len(podium) > 1 else None,
                        "ballon_bronze": dict(podium[2]) if len(podium) > 2 else None,
                        "ballon_podium": [dict(r) for r in podium],
                        "top20_players": [
                            {**dict(row), "rank": rank,
                             "ballon_award": {1: "Quả bóng Vàng", 2: "Quả bóng Bạc", 3: "Quả bóng Đồng"}.get(rank)}
                            for rank, row in enumerate(eligible[:20], 1)
                        ],
                        "top20_version": 1,
                        "young_player": dict(young[0]) if young else None,
                        "best_xi_442": self._make_best_xi_442(
                            candidates, champion_teams=self._season_champion_teams(record)),
                        "best_xi_version": 8,
                        "award_settings_signature": self._award_settings_signature(),
                        "recalculation_settings": dict(getattr(self, "award_settings", AWARD_SETTINGS_DEFAULTS)),
                        "best_xi_fallback_rule": "Ưu tiên Top 20; nếu thiếu vị trí, bổ sung cầu thủ cùng vị trí từ CLB vô địch C1 ngoài Top 20, vẫn phải đủ điều kiện theo award_settings.json.",
                        "eligible_players": len(eligible),
                        "eligibility_rule": (f"Ít nhất {getattr(self, 'award_settings', AWARD_SETTINGS_DEFAULTS).get('min_appearances', 21)} trận; điều kiện giải đấu theo award_settings.json."),
                        "legacy_backfill": True,
                        "method": ("Bản lưu cũ: áp dụng điều kiện và trọng số từ award_settings.json; "
                                   "điểm tiềm năng cầu thủ trẻ được ước tính do save cũ thiếu dữ liệu."),
                    }
                except (TypeError, ValueError, KeyError):
                    # Keep older history readable when source data is incomplete.
                    pass
            annual = record.get("season_awards")
            if isinstance(annual, dict) and (
                    int(annual.get("best_xi_version", 0) or 0) < 8
                    or int(annual.get("top20_version", 0) or 0) < 1):
                archived_rows = record.get("career_player_stats") or self._award_rows_from_matches(matches, lookup)
                if archived_rows:
                    try:
                        season_number = int(record.get("season", self.s.get("season", 2026)))
                        original_season = self.s.get("season")
                        self.s["season"] = season_number
                        try:
                            candidates = self._season_award_candidates(
                                matches=matches, archived_rows=archived_rows,
                                eligibility_record=record.get("award_eligibility"))
                        finally:
                            self.s["season"] = original_season
                        eligible = [row for row in candidates if row.get("award_eligible")]
                        podium = eligible[:3]
                        annual.update({
                            "ballon_dor": dict(podium[0]) if len(podium) > 0 else None,
                            "ballon_silver": dict(podium[1]) if len(podium) > 1 else None,
                            "ballon_bronze": dict(podium[2]) if len(podium) > 2 else None,
                            "ballon_podium": [dict(row) for row in podium],
                            "top20_players": [
                                {**dict(row), "rank": rank,
                                 "ballon_award": {1: "Quả bóng Vàng", 2: "Quả bóng Bạc", 3: "Quả bóng Đồng"}.get(rank)}
                                for rank, row in enumerate(eligible[:20], 1)
                            ],
                            "top20_version": 1,
                            "eligible_players": len(eligible),
                            "eligibility_rule": (f"Ít nhất {getattr(self, 'award_settings', AWARD_SETTINGS_DEFAULTS).get('min_appearances', 21)} trận; điều kiện giải đấu theo award_settings.json."),
                            "method": ("Điều kiện bắt buộc và trọng số bình chọn lấy từ award_settings.json; "
                                      "thay đổi thông số sẽ làm mới lịch sử khi app khởi động."),
                        })
                        annual["best_xi_442"] = self._make_best_xi_442(
                            candidates, champion_teams=self._season_champion_teams(record))
                        annual["best_xi_version"] = 8
                        annual["award_settings_signature"] = self._award_settings_signature()
                        annual["recalculation_settings"] = dict(getattr(self, "award_settings", AWARD_SETTINGS_DEFAULTS))
                        annual["best_xi_fallback_rule"] = ("Ưu tiên Top 20; nếu thiếu vị trí, bổ sung cầu thủ cùng vị trí từ CLB vô địch C1 ngoài Top 20, "
                                                           "vẫn phải đạt điều kiện đang lưu trong award_settings.json. Nếu không có thì để trống.")
                        annual["best_xi_method"] = (
                            "Điều kiện bình chọn, trọng số và điểm ưu tiên CLB được lấy từ award_settings.json. "
                            "Ưu tiên Top 20; nếu thiếu vị trí thì dùng cầu thủ cùng vị trí thuộc CLB vô địch C1 ngoài Top 20, "
                            "vẫn phải đạt điều kiện đã lưu; nếu không có ứng viên phù hợp thì để trống.")
                    except (TypeError, ValueError, KeyError):
                        # Do not preserve unverified legacy winners if their source rows are malformed.
                        annual.update({
                            "ballon_dor": None, "ballon_silver": None, "ballon_bronze": None,
                            "ballon_podium": [], "best_xi_442": [], "eligible_players": 0,
                            "best_xi_version": 8, "top20_players": [], "top20_version": 1,
                            "award_settings_signature": self._award_settings_signature(),
                            "recalculation_settings": dict(getattr(self, "award_settings", AWARD_SETTINGS_DEFAULTS)),
                            "eligibility_backfill_unavailable": True,
                            "eligibility_rule": ("Trên 20 trận và ra sân ở bảng Champion Premier League "
                                                 "hoặc ra sân C1 cùng CLB vào bán kết."),
                        })
                else:
                    # Do not silently preserve an old unfiltered ballot without verifiable source data.
                    annual.update({
                        "ballon_dor": None, "ballon_silver": None, "ballon_bronze": None,
                        "ballon_podium": [], "best_xi_442": [], "eligible_players": 0,
                        "best_xi_version": 8, "top20_players": [], "top20_version": 1,
                        "award_settings_signature": self._award_settings_signature(),
                        "recalculation_settings": dict(getattr(self, "award_settings", AWARD_SETTINGS_DEFAULTS)),
                        "eligibility_backfill_unavailable": True,
                        "eligibility_rule": ("Trên 20 trận và ra sân ở bảng Champion Premier League "
                                             "hoặc ra sân C1 cùng CLB vào bán kết."),
                    })
        after = json.dumps(self.s.get("history", []) or [], ensure_ascii=False, sort_keys=True, default=str)
        return before != after

    def finalize_season(self, persist=True):
        if self.s["season_done"]: return self.s["history"][-1] if self.s["history"] else None
        s = self.s
        points_table, tables = self._season_total_table()
        # Giải thưởng và hồ sơ mùa dùng dữ liệu tổng hợp; không cần giữ player_stats
        # lặp lại trong từng match archive.
        motm_counts = Counter()
        for played_match in s.get("matches", []):
            motm = played_match.get("motm")
            if isinstance(motm, (list, tuple)) and len(motm) >= 2 and motm[1]:
                motm_counts[str(motm[1])] += 1
        scorer_rows, assist_rows = [], []
        for pid, st in s["season_stats"].items():
            p = s["players"].get(pid)
            if not p: continue
            meta = s.get("player_meta", {}).get(pid, {})
            # Legacy/incomplete season_stats rows may omit fields introduced by
            # newer versions. Missing counters must behave as zero so one bad
            # player record cannot prevent the entire season from being finalized.
            goals = int(st.get("goals", 0) or 0)
            assists = int(st.get("assists", 0) or 0)
            apps = int(st.get("apps", 0) or 0)
            minutes = int(st.get("mins", st.get("minutes", 0)) or 0)
            rating_sum = float(st.get("rating_sum", 0) or 0)
            rating_count = int(st.get("rating_count", 0) or 0)
            scorer_rows.append({"player_id":pid,"player":p.get("common_name",pid),"team":p.get("club",""),
                                "position":p.get("position",""),"goals":goals,"assists":assists,
                                "apps":apps,"minutes":minutes,
                                "avg_rating":round(rating_sum/rating_count,2) if rating_count > 0 else None,
                                "motm_count":int(motm_counts.get(str(pid),0)),
                                "gk_apps":int(st.get("gk_apps", 0) or 0),
                                "gk_minutes":int(st.get("gk_minutes", 0) or 0),
                                "gk_clean_sheets":int(st.get("gk_clean_sheets", 0) or 0),
                                "gk_goals_conceded":int(st.get("gk_goals_conceded", 0) or 0),
                                "gk_saves":int(st.get("gk_saves", 0) or 0),
                                "age":meta.get("age"),"potential":meta.get("potential")})
            if assists: assist_rows.append(scorer_rows[-1])
        scorer_rows.sort(key=lambda x:(-x["goals"],-x["assists"],-(x["avg_rating"] or 0),x["player"]))
        assist_rows.sort(key=lambda x:(-x["assists"],-x["goals"],x["player"]))
        pl_table = tables["premier"]
        cup_winner, c1_winner = s["cup"].get("winner"), s["c1"].get("champion")
        self._refresh_completed_competition_awards(announce=False)
        season_awards = self._calculate_season_awards()
        # Lưu bảng C1 lúc kết mùa.
        group_tables = {g:self.c1_group_table(g) for g in ("A","B")}
        promoted, relegated = self._promote_relegate(tables)
        dev, retirees, youth = self._develop_retire_promote()
        record = {
            "season": s["season"], "label": f"{s['season']}/{s['season']+1}",
            "champion_premier": pl_table[0]["team"] if pl_table else None,
            "champion_cup": cup_winner, "runner_up_cup": s["cup"].get("runner_up"),
            "champion_c1": c1_winner, "runner_up_c1": s["c1"].get("runner_up"),
            "leagues": tables, "league_phase2": s.get("league_phase2", {}),
            "promoted": promoted, "relegated": relegated,
            "cup_bracket": s["cup"]["bracket"],
            "c1": {"playin": s["c1"].get("playin",{}), "groups": s["c1"].get("groups",{}),
                   "group_tables": group_tables, "bracket": s["c1"].get("bracket",{}),
                   "champion": c1_winner, "runner_up": s["c1"].get("runner_up")},
            "qualification_table": points_table, "top_scorers": scorer_rows[:20], "top_assists": assist_rows[:20],
            "career_player_stats": [dict(row) for row in scorer_rows],
            "competition_awards": {key: dict(value) for key, value in s.get("competition_awards", {}).items()},
            "season_awards": season_awards,
            "individual_awards": self._build_individual_awards(
                s["season"], f"{s['season']}/{s['season'] + 1}", s.get("matches", []),
                season_awards, s.get("competition_awards", {})),
            "individual_awards_version": 1,
            "team_awards": self._build_team_awards(
                s["season"], f"{s['season']}/{s['season'] + 1}", s.get("matches", []),
                s.get("competition_awards", {})),
            "team_awards_version": 1,
            "development": dev, "retired": retirees, "youth_promoted": youth,
            "matches": s["matches"], "match_count": len(s["matches"]),
            "award_eligibility": {
                "premier_champion_players": sorted(self._season_award_eligibility(s["matches"])["premier_champion_players"]),
                "c1_semifinal_players": sorted(self._season_award_eligibility(s["matches"])["c1_semifinal_players"]),
            },
            "league_points": {r["team"]:r["league_points"] for r in points_table},
            "total_points": {r["team"]:r["total"] for r in points_table}
        }
        # Generate final headlines while the full match log is still available.
        # Settle the completed season before compacting/writing its archive so the
        # financial report is stored in the SQLite season archive.
        if getattr(self, "economy_runtime", None) is not None:
            self.economy_runtime.on_season_finishing(record)
        season_headlines = match_news.season_news(self, record, limit=14)
        # Store completed season details in SQLite; no per-season JSON files.
        archive_record = compact_season_record(record, s, record.get("award_eligibility"))
        if persist:
            self.database.save_season_archive(archive_record)
        s["matches"] = [compact_match(match, {pid: p.get("common_name", pid) for pid, p in s.get("players", {}).items()})
                        for match in s.get("matches", [])]
        s["history"].append(archive_record)
        record = archive_record
        s["last_development"] = dev
        s["last_promotions"] = {"promoted":promoted,"relegated":relegated}
        s["previous_total_points"] = {r["team"]:r["total"] for r in points_table}
        s["previous_league_points"] = {r["team"]:r["league_points"] for r in points_table}
        s["previous_ranks"] = {r["team"]:r["rank"] for r in points_table}
        s["seed_order"] = [r["team"] for r in points_table]
        s["season_done"] = True
        s["season_awards"] = season_awards
        for item in season_headlines:
            item.update({"season": int(s["season"]), "date": str(s.get("slots", [{}])[-1].get("date", "")),
                         "stage": "Tổng kết mùa"})
        s.setdefault("news", []).extend(season_headlines)
        s["news"] = match_news.retain_recent_news(
            s["news"], match_news.reference_date_from_state(s))
        s["last_news"] = [item["text"] for item in season_headlines[:8]] or [
            f"Mùa {record['label']} đã khép lại. Premier: {record['champion_premier']}; Cup: {cup_winner}; C1: {c1_winner}."]
        self._write_persistent_csvs(s["season"])
        self.economy_events.publish("SEASON_FINISHED", game=self, record=record,
                                    season=int(s.get("season", 0)))
        if persist:
            self.save()
        return record

    def _promote_relegate(self, tables):
        """Premier relegation uses Challenge group; FC promotions use 3v4 then 2v-winner."""
        pl, f1, f2 = tables["premier"], tables["fc1"], tables["fc2"]
        phase2_tables = self.premier_phase2_tables()
        challenge = phase2_tables["challenge"]
        if len(challenge) != 4:
            raise RuntimeError("Premier Challenge chưa có đủ 4 CLB để xác định xuống hạng.")
        down_pl = [row["team"] for row in challenge[-2:]]
        playoff = self.s.get("league_phase2", {}).get("promotion_playoffs", {})
        up_f1_winner = playoff.get("fc1", {}).get("winner")
        up_f2_winner = playoff.get("fc2", {}).get("winner")
        if not up_f1_winner or not up_f2_winner:
            raise RuntimeError("Thiếu kết quả play-off lên hạng của FC1 hoặc FC2.")
        up_f1 = [f1[0]["team"], up_f1_winner]
        relegation_tie = self.relegation_playoff_summary()
        if not relegation_tie.get("winner") or not relegation_tie.get("loser"):
            raise RuntimeError("Thiếu kết quả play-off trụ hạng FC1 (hai lượt trận).")
        down_f1 = [f1[-1]["team"], relegation_tie["loser"]]
        up_f2 = [f2[0]["team"], up_f2_winner]
        for team in down_pl: self.s["team_tiers"][team] = "fc1"
        for team in up_f1: self.s["team_tiers"][team] = "premier"
        for team in down_f1: self.s["team_tiers"][team] = "fc2"
        for team in up_f2: self.s["team_tiers"][team] = "fc1"
        promoted = ([{"team": team, "from": "fc1", "to": "premier",
                      "path": "Vô địch GĐ1" if team == f1[0]["team"] else "Thắng play-off"} for team in up_f1] +
                    [{"team": team, "from": "fc2", "to": "fc1",
                      "path": "Vô địch GĐ1" if team == f2[0]["team"] else "Thắng play-off"} for team in up_f2])
        relegated = ([{"team": team, "from": "premier", "to": "fc1", "path": "Challenge · Top 2 cuối"} for team in down_pl] +
                     [{"team": f1[-1]["team"], "from": "fc1", "to": "fc2", "path": "BXH GĐ1 · Hạng 8"},
                      {"team": relegation_tie["loser"], "from": "fc1", "to": "fc2",
                       "path": "Thua play-off trụ hạng · Hạng 6–7"}])
        counts = Counter(self.s["team_tiers"].values())
        if counts != Counter({"premier": 8, "fc1": 8, "fc2": 8}):
            raise RuntimeError(f"Lỗi lên/xuống hạng: phân bổ sau mùa không còn 8-8-8 ({dict(counts)}).")
        promoted_teams = [item["team"] for item in promoted]
        relegated_teams = [item["team"] for item in relegated]
        if len(set(promoted_teams)) != 4 or len(set(relegated_teams)) != 4:
            raise RuntimeError("Lỗi lên/xuống hạng: một CLB bị ghi nhận đổi hạng nhiều lần.")
        if set(promoted_teams).intersection(relegated_teams):
            raise RuntimeError("Lỗi lên/xuống hạng: CLB không thể vừa lên vừa xuống hạng.")
        return promoted, relegated

    @staticmethod
    def _age_base(age):
        if age <= 19: return 4.0
        return {20:3.0,21:2.5,22:2.0,23:1.2,24:.8,25:.2,26:.2,27:.2,28:0,29:-.5,30:-1.0,31:-1.5,32:-2.0,33:-2.5}.get(int(age),-3.0 if age >= 34 else .2)

    def _retire_probability(self, p, age, career_ending_injury=False):
        """Return the off-season retirement probability for a player's age.

        Age in player_meta is the age at the start of the season. It is advanced
        by one year before the retirement record is written, so age 33 here is
        normally displayed as 34 in the history.

        IMPORTANT: low overall / low squad rank must never create an early
        retirement chance. The only supported exception below age 33 is an
        explicit career-ending injury flag stored on player metadata/player.
        """
        if career_ending_injury:
            return 1.0
        try:
            age = float(age)
        except (TypeError, ValueError):
            age = 26.0
        # No normal retirement before the player's 33rd season-age. This hard
        # floor also protects older config.json files containing lower keys.
        if age < 33.0:
            return 0.0
        if p.get("position") == "GK" and age >= 39.0:
            return 1.0

        cfg = self.config["players"]
        tab = cfg.get("retire_table_gk" if p.get("position") == "GK" else "retire_table_outfield", {})
        eligible = {}
        for raw_age, raw_prob in (tab or {}).items():
            try:
                table_age = int(raw_age)
                probability = float(raw_prob)
            except (TypeError, ValueError):
                continue
            if table_age >= 33:
                eligible[table_age] = max(0.0, min(1.0, probability))
        matching = [table_age for table_age in eligible if table_age <= age]
        probability = eligible[max(matching)] if matching else 0.0
        return max(0.0, min(1.0, probability))

    @staticmethod
    def _is_enabled_flag(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value == 1
        return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _retirement_details(p, meta, season_stat, career_ending_injury=False):
        """Create an explicit, human-readable retirement reason for the archive."""
        final_age = round(float(meta.get("age", 0.0)), 1)
        minutes = int((season_stat or {}).get("mins", 0) or 0)
        if career_ending_injury:
            reason = (meta.get("career_ending_injury_reason")
                      or p.get("career_ending_injury_reason")
                      or "Chấn thương nghiêm trọng ảnh hưởng lâu dài, không thể tiếp tục thi đấu chuyên nghiệp.")
            return {
                "retirement_reason": str(reason),
                "retirement_type": "career_ending_injury",
                "early_retirement": final_age < 34.0,
            }
        if final_age >= 37.0:
            reason = "Giải nghệ theo tuổi tác sau nhiều mùa thi đấu; thể lực và khả năng hồi phục suy giảm."
            code = "age_related"
        elif final_age >= 35.0 and minutes < 450:
            reason = "Ở giai đoạn cuối sự nghiệp, cầu thủ ít được sử dụng và khó duy trì thể lực thi đấu."
            code = "age_and_limited_role"
        else:
            reason = "Khép lại sự nghiệp sau nhiều mùa thi đấu khi đã bước vào ngưỡng tuổi giải nghệ."
            code = "age_related"
        return {
            "retirement_reason": reason,
            "retirement_type": code,
            "early_retirement": False,
        }

    def _develop_retire_promote(self):
        s = self.s
        dev, retiring, youth = [], [], []
        # Học viện già thêm một tuổi khi hết mùa; người đã quá 20 tuổi tốt nghiệp
        # khỏi danh sách trẻ và được lưu archive trước khi bổ sung lứa mới.
        for team, entries in s["academy"].items():
            for entry in entries:
                entry["meta"]["age"] = round(float(entry["meta"].get("age",18))+1,1)
        self._maintain_academy_age_range(season=int(s.get("season", 2026)) + 1)
        retire_candidates = []
        for team, ids in list(s["rosters"].items()):
            for pid in list(ids):
                p = s["players"].get(pid)
                if not p: continue
                meta = s["player_meta"].setdefault(pid, {"potential":float(p["overall_rating"]),"age":26.0,"status":"active","peak_ovr":int(p["overall_rating"]),"seasons_played":0})
                age = float(meta.get("age",26))
                st = s["season_stats"].get(pid, self._empty_stat())
                rating_count = int(st.get("rating_count", 0) or 0)
                avg_rating = st["rating_sum"] / rating_count if rating_count else None
                ovr = int(p["overall_rating"])
                potential_before = float(meta.get("potential", ovr))
                injury_matches = int(st.get("injury_matches", 0) or 0)
                injury_days = int(st.get("injury_days", 0) or 0)
                training_days = int(st.get("training_days", 0) or 0)
                morale_count = int(st.get("morale_count", 0) or 0)
                average_morale = (float(st.get("morale_sum", 0.0)) / morale_count) if morale_count else None
                suspension_matches = int(st.get("suspension_matches", 0) or 0)
                model = calculate_season_development(
                    age=age, overall=ovr, potential=potential_before,
                    minutes=int(st.get("mins", 0) or 0), average_rating=avg_rating,
                    injury_matches=injury_matches, injury_days=injury_days,
                    training_days=training_days, average_morale=average_morale,
                    noise=self.rng.gauss(0, .45),
                )
                training_focus = str(meta.get("training_focus") or choose_training_focus(p))
                meta["training_focus"] = training_focus
                delta = int(model["delta"])
                soft_cap = float(self.config["players"].get("soft_cap_from", 85))
                if delta > 0 and ovr > soft_cap:
                    damping = max(0.0, (99.0 - ovr) / max(1.0, 99.0 - soft_cap))
                    delta = int(round(delta * damping))
                new_ovr = int(max(40, min(99, ovr + delta)))

                attributes_before = {}
                for key in ATTRIBUTE_FIELDS:
                    try:
                        attributes_before[key] = int(round(float(p.get(key, 50))))
                    except (TypeError, ValueError):
                        attributes_before[key] = 50
                calibration_offset = meta.get("overall_attribute_offset")
                if calibration_offset is None:
                    calibration_offset = float(ovr) - estimate_overall_from_attributes(p)
                attribute_changes = self._adjust_attributes(
                    p, delta, age,
                    context={**model, "minutes": int(st.get("mins", 0) or 0),
                             "potential_gap": potential_before - ovr,
                             "training_focus": training_focus},
                )
                reconciliation = reconcile_overall_rating(
                    p, original_ovr=ovr, development_ovr=new_ovr,
                    calibration_offset=calibration_offset,
                    attribute_weight=float(self.config["players"].get("attribute_ovr_weight", 0.35)),
                )
                meta["overall_attribute_offset"] = reconciliation["calibration_offset"]
                new_ovr = int(reconciliation["overall_rating"])
                p["overall_rating"] = new_ovr
                meta["peak_ovr"] = max(int(meta.get("peak_ovr", ovr)), new_ovr)
                meta["seasons_played"] = int(meta.get("seasons_played", 0)) + 1
                if int(st.get("mins", 0) or 0) == 0:
                    meta["potential"] = max(new_ovr, float(meta.get("potential", new_ovr)) - .3)
                potential_after = float(meta.get("potential", new_ovr))
                meta["age"] = round(age + 1, 1)

                # Save the exact attribute deltas made by the engine. No profile
                # code reconstructs or interpolates these values later.
                dev.append({
                    "player_id": pid,
                    "player": p.get("common_name", pid),
                    "team": team,
                    "position": p.get("position"),
                    "old_ovr": ovr,
                    "new_ovr": new_ovr,
                    "delta": new_ovr - ovr,
                    "age": round(age, 1),
                    "age_end": round(age + 1, 1),
                    "potential_before": round(potential_before, 1),
                    "potential": round(potential_after, 1),
                    "minutes": int(st.get("mins", 0) or 0),
                    "avg_rating": round(avg_rating, 2) if avg_rating is not None else None,
                    "injury_matches": injury_matches,
                    "injury_days": injury_days,
                    "training_days": training_days,
                    "average_morale": round(average_morale, 2) if average_morale is not None else None,
                    "training_focus": training_focus,
                    "suspension_matches": suspension_matches,
                    "attribute_changes": attribute_changes,
                    "attribute_ovr": reconciliation,
                    "development_model": {
                        **model,
                        "delta": new_ovr - ovr,
                        "soft_cap_delta": delta,
                    },
                })
                # Chỉ cho giải nghệ theo tuổi từ 33 tuổi đầu mùa trở lên.
                # OVR thấp / nằm cuối danh sách đội hình KHÔNG phải lý do giải nghệ.
                # Trường hợp sớm chỉ được phép khi có cờ chấn thương kết thúc sự nghiệp
                # được ghi rõ trên player_meta hoặc player row.
                career_ending_injury = (
                    self._is_enabled_flag(meta.get("career_ending_injury"))
                    or self._is_enabled_flag(p.get("career_ending_injury"))
                )
                prob = self._retire_probability(p, age, career_ending_injury=career_ending_injury)
                if self.rng.random() < prob:
                    retire_candidates.append((team,pid,dict(p),dict(meta),dict(st)))
        for team,pid,p,meta,st in retire_candidates:
            pmeta = s["player_meta"].setdefault(pid,meta)
            pmeta["status"] = "retired"; pmeta["retire_season"] = s["season"]
            career_ending_injury = (
                self._is_enabled_flag(pmeta.get("career_ending_injury"))
                or self._is_enabled_flag(p.get("career_ending_injury"))
            )
            retirement_details = self._retirement_details(
                p, pmeta, st, career_ending_injury=career_ending_injury
            )
            rec = {"player_id":pid,"player_name":p.get("common_name",pid),"club":team,"retire_season":s["season"],
                   "age":round(float(pmeta.get("age",0)),1),"overall_peak":int(pmeta.get("peak_ovr",p.get("overall_rating",0))),
                   "position":p.get("position"),"appearances":st.get("apps",0),"goals":st.get("goals",0),"assists":st.get("assists",0),
                   **retirement_details}
            pmeta["retirement_reason"] = retirement_details["retirement_reason"]
            pmeta["retirement_type"] = retirement_details["retirement_type"]
            s["retired_players"].append(rec); retiring.append(rec)
            if pid in s["rosters"][team]: s["rosters"][team].remove(pid)
        # Mỗi người giải nghệ được thay bởi học viện cùng vị trí; nếu thiếu thì tự tạo tài năng mới.
        next_year = int(s["season"])+1
        counter = Counter()
        for pid, p in s["players"].items():
            mm = re.match(r"GEN\d+_[A-Z0-9]+_(\d+)$", pid)
            if mm: counter[p.get("club","")] = max(counter[p.get("club","")],int(mm.group(1)))
        for team,pid,p,old_meta,st in retire_candidates:
            # A retired player is replaced only when the club would otherwise fall
            # below 23 players or a positional-group minimum. Larger 24–30 squads
            # keep their vacant slot instead of automatically growing every season.
            remaining_ids = list(s["rosters"].get(team, []))
            group_counts = Counter(position_group(s["players"].get(x, {}).get("position", "")) for x in remaining_ids)
            required_groups = {"GK": 3, "DEF": 7, "MID": 7, "FWD": 4}
            need_replacement = len(remaining_ids) < 23 or any(
                group_counts.get(group, 0) < minimum for group, minimum in required_groups.items())
            if not need_replacement:
                continue
            pos = p.get("position","CM")
            academy = s["academy"].setdefault(team, [])
            idx = next((i for i,e in enumerate(academy) if e["player"].get("position")==pos), None)
            if idx is not None:
                entry = academy.pop(idx)
                youth_row, youth_meta = dict(entry["player"]), dict(entry["meta"])
                potential = float(youth_meta.get("potential", youth_row["overall_rating"]))
                age = float(youth_meta.get("age",18))
            else:
                counter[team] += 1
                aid = f"ACD{next_year}_{_team_code(team)}_{counter[team]:02d}"
                youth_row, youth_meta = self._make_youth_row(team,pos,next_year,aid,academy=False)
                potential = float(youth_meta["potential"]); age = float(youth_meta["age"])
            counter[team] += 1
            new_id = f"GEN{next_year}_{_team_code(team)}_{counter[team]:02d}"
            # Bảo đảm ID không trùng kể cả khi đội từng sinh cầu thủ từ học viện.
            while new_id in s["players"] or any(x["player_id"]==new_id for x in s["generated_players"]):
                counter[team] += 1; new_id = f"GEN{next_year}_{_team_code(team)}_{counter[team]:02d}"
            youth_row["player_id"] = new_id; youth_row["club"] = team; youth_row["position"] = pos
            youth_row["is_default"] = "FALSE"
            s["players"][new_id] = youth_row
            s["rosters"][team].append(new_id)
            s["player_meta"][new_id] = {
                "potential": potential, "status": "active", "age": age, "retire_season": None,
                "peak_ovr": int(youth_row["overall_rating"]), "seasons_played": 0,
                "academy_talent_group": youth_meta.get("academy_talent_group", self.academy_talent_profile(team)["group"]),
                "is_superstar": bool(youth_meta.get("is_superstar", False)),
            }
            s["dyn"][new_id] = {"form":0.0,"fit":100.0}
            s["season_stats"][new_id] = self._empty_stat()
            s["generated_players"].append({**youth_row, "potential": round(potential, 1), "season_added": next_year,
                                             "replaced_player_id": pid,
                                             "academy_talent_group": youth_meta.get("academy_talent_group", "standard"),
                                             "is_superstar": bool(youth_meta.get("is_superstar", False))})
            youth.append({"player_id":new_id,"player":youth_row.get("common_name",new_id),"team":team,"position":pos,
                          "age":age,"overall":int(youth_row["overall_rating"]),"potential":round(potential,1),"replaced_player_id":pid})
        # Bổ sung prospect nếu một học viên vừa được đôn lên đội một.
        self._maintain_academy_age_range(season=next_year)
        # Maintain the new 23–30 squad rule and minimum positional groups.
        for team, ids in s["rosters"].items():
            if not 23 <= len(ids) <= 30:
                raise RuntimeError(f"Bất biến đội hình lỗi: {team} có {len(ids)} cầu thủ (yêu cầu 23–30).")
            c = Counter(position_group(s["players"][pid].get("position", "")) for pid in ids)
            required_groups = {"GK": 3, "DEF": 7, "MID": 7, "FWD": 4}
            if any(c.get(group, 0) < minimum for group, minimum in required_groups.items()):
                raise RuntimeError(f"Bất biến vị trí lỗi: {team}: {dict(c)}")
        dev.sort(key=lambda x:(-x["delta"],x["player"]))
        return dev, retiring, youth

    def _adjust_attributes(self, p, delta, age, context=None):
        """Apply seasonal skill-point changes and return an auditable change list."""
        if delta == 0:
            return []
        sign = 1 if delta > 0 else -1
        n = abs(delta) * 3
        context = context or {}
        minutes = max(0, int(context.get("minutes", 0) or 0))
        minutes_ratio = min(1.0, minutes / 3420.0)
        form_factor = float(context.get("form_factor", 0.0) or 0.0)
        injury_matches = max(0, int(context.get("injury_matches", 0) or 0))
        potential_gap = float(context.get("potential_gap", 0.0) or 0.0)
        before = {}

        for _ in range(n):
            weights = {}
            for key in ATTRIBUTE_FIELDS:
                try:
                    value = int(round(float(p.get(key, 50))))
                except (TypeError, ValueError):
                    value = 50
                # A capped attribute cannot receive another point; the lower
                # bound similarly prevents repeated decline below the engine floor.
                if sign > 0 and value >= 99 or sign < 0 and value <= 35:
                    continue
                if key.startswith("goalkeeping_") and p.get("position") == "GK":
                    age_adj = age - 3
                else:
                    age_adj = age
                weight = 1.0
                is_physical = key in PHYSICAL_FIELDS
                is_mental = key in MENTAL_FIELDS
                if is_physical and age_adj >= 28:
                    if sign > 0:
                        weight *= max(.18, 1.0 - (age_adj - 27) * .12)
                    else:
                        weight *= 1.0 + (age_adj - 27) * .22
                if is_mental and age_adj <= 31:
                    weight *= 1.55 if sign > 0 else .75

                if p.get("position") == "GK":
                    if key.startswith("goalkeeping_"):
                        weight *= 2.2
                    elif key in ("attacking_finishing", "shooting", "pace"):
                        weight *= .25
                elif p.get("position") in ("CB", "LB", "RB") and key in (
                    "defending", "defending_awareness", "defending_standing_tackle", "mentality_interceptions"
                ):
                    weight *= 1.5
                elif p.get("position") in ("ST", "CF", "LW", "RW") and key in (
                    "attacking_finishing", "shooting", "pace", "movement_acceleration"
                ):
                    weight *= 1.5

                # Strong match exposure and form focus development toward the
                # player's technical/decision-making skills. Long injury spells
                # reduce the chance of physical gains and make physical decline
                # more likely in a negative season.
                if minutes_ratio >= .65 and key in MENTAL_FIELDS:
                    weight *= 1.0 + min(.25, (minutes_ratio - .65) * .7)
                if form_factor > .25 and key in MENTAL_FIELDS:
                    weight *= 1.0 + min(.25, form_factor * .2)
                if form_factor < -.25 and sign < 0 and key in MENTAL_FIELDS:
                    weight *= 1.15
                if injury_matches >= 6 and is_physical:
                    weight *= 1.0 + min(.8, injury_matches * .025) if sign < 0 else max(.25, 1.0 - injury_matches * .035)
                focus = str(context.get("training_focus", ""))
                focus_groups = {
                    "goalkeeping": {k for k in ATTRIBUTE_FIELDS if k.startswith("goalkeeping_")},
                    "pace": {"movement_acceleration", "movement_sprint_speed", "pace"},
                    "finishing": {"attacking_finishing", "shooting", "attacking_volleys", "power_shot_power"},
                    "passing": {"attacking_short_passing", "skill_long_passing", "mentality_vision"},
                    "defending": {"defending_awareness", "defending_standing_tackle", "defending_sliding_tackle", "mentality_interceptions"},
                    "physical": {"power_stamina", "power_strength", "power_jumping"},
                }
                if focus == key:
                    weight *= 2.0
                elif focus in focus_groups and key in focus_groups[focus]:
                    weight *= 1.35
                if potential_gap <= 1.0 and sign > 0:
                    weight *= .85
                weights[key] = max(.01, weight)

            if not weights:
                break
            keys = list(weights)
            chosen = self.rng.choices(keys, weights=[weights[key] for key in keys], k=1)[0]
            try:
                old_value = int(round(float(p.get(chosen, 50))))
            except (TypeError, ValueError):
                old_value = 50
            new_value = max(35, min(99, old_value + sign))
            if new_value != old_value:
                before.setdefault(chosen, old_value)
                p[chosen] = new_value

        changes = []
        for key, old_value in before.items():
            try:
                new_value = int(round(float(p.get(key, old_value))))
            except (TypeError, ValueError):
                new_value = old_value
            if new_value != old_value:
                changes.append({"attribute": key, "old": old_value, "new": new_value,
                                "delta": new_value - old_value})
        changes.sort(key=lambda item: item["attribute"])
        return changes

    def _write_initial_csvs(self):
        """Legacy hook retained for callers; all career data now lives in SQLite."""
        return None

    def _write_persistent_csvs(self, season):
        """Legacy hook retained for callers; snapshots are indexed inside SQLite."""
        return None

    @staticmethod
    def _atomic_csv(path, fields, rows):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp,"w",encoding="utf-8-sig",newline="") as f:
            writer = csv.DictWriter(f,fieldnames=fields,extrasaction="ignore")
            writer.writeheader()
            for row in rows: writer.writerow(row)
        os.replace(tmp,path)

    def export_csvs(self, out_dir=None):
        out = Path(out_dir) if out_dir else Path(tempfile.gettempdir()) / "SuperLeague_exports"
        out.mkdir(parents=True, exist_ok=True)
        # Xuất BXH 3 hạng và lịch/kết quả đang xem.
        for tier in TIERS:
            self._atomic_csv(out/f"standings_{tier}_{self.s['season']}.csv",
                             ["position","team","p","w","d","l","gf","ga","gd","pts","elo"],
                             [{"position":i+1,**r} for i,r in enumerate(self.league_table(tier))])
        self._atomic_csv(out/f"matches_{self.s['season']}.csv",
                         ["id","date","comp","round","home","hg","ag","away","winner","neutral"],
                         [{k:m.get(k) for k in ["id","date","comp","round","home","hg","ag","away","winner","neutral"]} for m in self.s["matches"]])
        return out

    def slot_progress(self):
        done = sum(1 for x in self.s["slots"] if x["played"])
        total = len(self.s["slots"])
        return done, total

    def next_slot(self):
        return next((x for x in self.s["slots"] if not x["played"]), None)

    def season_label(self): return f"{self.s['season']}/{self.s['season']+1}"

    def team_form(self, team):
        rows = []
        for pid in self.s["rosters"][team]:
            p = self.s["players"][pid]; meta = self.s["player_meta"].get(pid,{})
            dyn = self.s["dyn"].get(pid,{})
            st = self.s["season_stats"].get(pid,self._empty_stat())
            rows.append({"player_id":pid,"name":p.get("common_name",pid),"position":p.get("position"),
                         "age":meta.get("age"),"ovr":p.get("overall_rating"),"potential":meta.get("potential"),
                         "form":dyn.get("form",0),"fit":dyn.get("fit",100),"apps":st.get("apps",0),
                         "goals":st.get("goals",0),"assists":st.get("assists",0),
                         "avg_rating":round(st["rating_sum"]/st["rating_count"],2) if st["rating_count"] else None})
        return sorted(rows,key=lambda x:(-int(x["ovr"]),x["name"]))

    def competition_matches(self, comp=None, team=None, played=None):
        out = []
        for m in self.s["matches"]:
            if comp and m["comp"] != comp: continue
            if team and team not in (m["home"],m["away"]): continue
            if played is not None and bool(m.get("played")) != bool(played): continue
            out.append(m)
        return out

    def competition_player_leaderboards(self, competition):
        """Bảng vua phá lưới/kiến tạo theo riêng từng giải của mùa hiện tại.

        C1 gộp cả C1 Play-in và C1 main stage. Chỉ lấy trận đã đá và dữ liệu bàn
        thắng/kiến tạo được engine lưu trong match.goals; không suy diễn số liệu.
        """
        comp = str(competition or "").strip()
        if comp.lower() in ("c1", "champions league", "cúp c1"):
            allowed = {"C1", "C1 Play-in"}
        else:
            allowed = {comp}
        rows = {}
        for match in self.s.get("matches", []):
            if not match.get("played") or match.get("comp") not in allowed:
                continue
            # Ghi nhận số lần ra sân từ đội hình xuất phát và cầu thủ vào thay.
            for side, team_key in (("h", "home"), ("a", "away")):
                ids = set(match.get("xi_" + side, []) or [])
                for item in match.get("subs_" + side, []) or []:
                    if len(item) >= 3 and item[2]:
                        ids.add(item[2])
                for pid in ids:
                    if not pid:
                        continue
                    rec = rows.setdefault(str(pid), {
                        "player_id": str(pid), "player": self.s.get("players", {}).get(pid, {}).get("common_name", str(pid)),
                        "team": match.get(team_key, ""), "position": self.s.get("players", {}).get(pid, {}).get("position", ""),
                        "goals": 0, "assists": 0, "apps": 0,
                    })
                    rec["apps"] += 1
            for goal in match.get("goals", []) or []:
                scorer_id = goal.get("scorer_id")
                scorer_name = goal.get("scorer") or ""
                if scorer_id or scorer_name:
                    key = str(scorer_id) if scorer_id else f"name:{scorer_name}:{goal.get('team', '')}"
                    rec = rows.setdefault(key, {
                        "player_id": str(scorer_id or ""), "player": scorer_name or str(scorer_id),
                        "team": goal.get("team", ""), "position": self.s.get("players", {}).get(scorer_id, {}).get("position", ""),
                        "goals": 0, "assists": 0, "apps": 0,
                    })
                    rec["goals"] += 1
                    if goal.get("team"):
                        rec["team"] = goal["team"]
                assist_id = goal.get("assist_id")
                assist_name = goal.get("assist") or ""
                if assist_id or assist_name:
                    key = str(assist_id) if assist_id else f"name:{assist_name}:{goal.get('team', '')}"
                    rec = rows.setdefault(key, {
                        "player_id": str(assist_id or ""), "player": assist_name or str(assist_id),
                        "team": goal.get("team", ""), "position": self.s.get("players", {}).get(assist_id, {}).get("position", ""),
                        "goals": 0, "assists": 0, "apps": 0,
                    })
                    rec["assists"] += 1
                    if goal.get("team"):
                        rec["team"] = goal["team"]
        values = list(rows.values())
        scorers = sorted((dict(r) for r in values if r["goals"] > 0),
                         key=lambda r: (-r["goals"], -r["assists"], r["player"].casefold(), r["team"].casefold()))
        assisters = sorted((dict(r) for r in values if r["assists"] > 0),
                           key=lambda r: (-r["assists"], -r["goals"], r["player"].casefold(), r["team"].casefold()))
        return {"scorers": scorers[:50], "assists": assisters[:50]}

    def _career_match_records(self):
        """Return completed matches across archived seasons and the active season once each."""
        records = []
        seen = set()
        for season_record in self.s.get("history", []):
            season = int(season_record.get("season", 0) or 0)
            for match in season_record.get("matches", []) or []:
                if not match.get("played", True):
                    continue
                identity = (int(match.get("season", season) or season),
                            str(match.get("id", "")), str(match.get("date", "")),
                            str(match.get("comp", "")), str(match.get("home", "")),
                            str(match.get("away", "")))
                if identity in seen:
                    continue
                seen.add(identity)
                records.append(match)
        current_season = int(self.s.get("season", 0) or 0)
        for match in self.s.get("matches", []) or []:
            if not match.get("played", True):
                continue
            identity = (int(match.get("season", current_season) or current_season),
                        str(match.get("id", "")), str(match.get("date", "")),
                        str(match.get("comp", "")), str(match.get("home", "")),
                        str(match.get("away", "")))
            if identity in seen:
                continue
            seen.add(identity)
            records.append(match)
        records.sort(key=lambda m: (int(m.get("season", 0) or 0),
                                    str(m.get("date", "")), int(m.get("id", 0) or 0)))
        return records

    def career_trophy_table(self):
        """Count completed career titles by club, split by league tier, Cup and C1."""
        totals = {team: {"team": team, "premier": 0, "fc1": 0, "fc2": 0,
                         "cup": 0, "c1": 0, "total": 0}
                  for team in self.s.get("rosters", {})}
        for record in self.s.get("history", []):
            leagues = record.get("leagues", {}) or {}
            for tier in ("premier", "fc1", "fc2"):
                rows = leagues.get(tier, []) or []
                champion = (rows[0].get("team") if rows else None)
                if tier == "premier":
                    champion = record.get("champion_premier") or champion
                if champion in totals:
                    totals[champion][tier] += 1
            for field, category in (("champion_cup", "cup"), ("champion_c1", "c1")):
                champion = record.get(field)
                if champion in totals:
                    totals[champion][category] += 1
        if not self.s.get("season_done"):
            for key, award in (self.s.get("competition_awards", {}) or {}).items():
                if not isinstance(award, dict) or not award.get("completed"):
                    continue
                champion = award.get("champion")
                if champion in totals and key in ("premier", "fc1", "fc2", "cup", "c1"):
                    totals[champion][key] += 1
        rows = list(totals.values())
        for row in rows:
            row["total"] = sum(row[key] for key in ("premier", "fc1", "fc2", "cup", "c1"))
        rows.sort(key=lambda r: (-r["total"], -r["cup"], -r["c1"], -r["premier"], r["team"].casefold()))
        for index, row in enumerate(rows, 1):
            row["rank"] = index
        return rows

    def career_team_trophies(self, team):
        """Return a team's completed-career trophy counts without mutating the save."""
        row = next((item for item in self.career_trophy_table() if item["team"] == team), None)
        if row:
            return dict(row)
        return {"team": team, "premier": 0, "fc1": 0, "fc2": 0,
                "cup": 0, "c1": 0, "total": 0, "rank": None}

    def career_player_legends(self, team=None, limit=11):
        """Career all-time leaders from season snapshots or saved match records.

        New season archives store full per-player season totals. Older archives fall
        back to their saved XI/substitute lists and goal events. If an old match did
        not store substitutes or minutes, those unavailable values are not guessed.
        """
        players = self.s.get("players", {})
        aggregate = {}

        def add_totals(pid, player_name, club, position, apps=0, goals=0, assists=0, minutes=0):
            if not pid or not club or (team and club != team):
                return
            key = (str(pid), str(club))
            rec = aggregate.setdefault(key, {
                "player_id": str(pid), "player": player_name or str(pid), "team": str(club),
                "position": position or "", "apps": 0, "goals": 0, "assists": 0, "minutes": 0,
            })
            rec["apps"] += int(apps or 0)
            rec["goals"] += int(goals or 0)
            rec["assists"] += int(assists or 0)
            rec["minutes"] += int(minutes or 0)

        def accumulate_legacy_match(match):
            side_data = {}
            for side, team_key in (("h", "home"), ("a", "away")):
                club = match.get(team_key, "")
                if team and club != team:
                    continue
                detail_map = (match.get("player_stats") or {}).get(side, {}) or {}
                ids = set(str(pid) for pid in (match.get("xi_" + side, []) or []) if pid)
                for item in match.get("subs_" + side, []) or []:
                    if len(item) >= 3 and item[2]:
                        ids.add(str(item[2]))
                for pid, details in detail_map.items():
                    try:
                        played_minutes = float((details or {}).get("min", 0) or 0)
                    except (TypeError, ValueError):
                        played_minutes = 0
                    if played_minutes > 0:
                        ids.add(str(pid))
                side_data[side] = (club, detail_map, ids)

            goal_events = {}
            assist_events = {}
            for goal in match.get("goals", []) or []:
                goal_side = goal.get("side")
                club = goal.get("team")
                if goal_side in ("home", "away"):
                    side = "h" if goal_side == "home" else "a"
                else:
                    side = next((key for key, value in side_data.items() if value[0] == club), None)
                if side not in side_data:
                    continue
                club = side_data[side][0]
                scorer_id = goal.get("scorer_id")
                assist_id = goal.get("assist_id")
                if scorer_id:
                    goal_events[(str(scorer_id), club)] = goal_events.get((str(scorer_id), club), 0) + 1
                if assist_id:
                    assist_events[(str(assist_id), club)] = assist_events.get((str(assist_id), club), 0) + 1

            for side, (club, detail_map, ids) in side_data.items():
                if not club:
                    continue
                for pid in ids:
                    details = detail_map.get(pid, detail_map.get(str(pid), {})) or {}
                    player_row = players.get(pid, players.get(str(pid), {})) or {}
                    # A detail payload is authoritative; otherwise use the saved goal log.
                    if "g" in details or "a" in details:
                        player_goals = int(details.get("g", 0) or 0)
                        player_assists = int(details.get("a", 0) or 0)
                    else:
                        player_goals = goal_events.get((str(pid), club), 0)
                        player_assists = assist_events.get((str(pid), club), 0)
                    try:
                        mins = int(float(details.get("min", 0) or 0))
                    except (TypeError, ValueError):
                        mins = 0
                    add_totals(pid, player_row.get("common_name", str(pid)), club,
                               player_row.get("position", ""), 1, player_goals, player_assists, mins)

            # Be robust to old goal logs whose scorer/assister was not present in XI/sub data.
            for (pid, club), count in goal_events.items():
                if (pid, club) not in aggregate:
                    player_row = players.get(pid, {}) or {}
                    add_totals(pid, player_row.get("common_name", pid), club,
                               player_row.get("position", ""), 0, count,
                               assist_events.get((pid, club), 0), 0)
            for (pid, club), count in assist_events.items():
                if (pid, club) not in aggregate:
                    player_row = players.get(pid, {}) or {}
                    add_totals(pid, player_row.get("common_name", pid), club,
                               player_row.get("position", ""), 0,
                               goal_events.get((pid, club), 0), count, 0)

        # Completed seasons created by this version use full season_stats snapshots.
        # Old records without the snapshot remain readable through match-level fallback.
        for season_record in self.s.get("history", []):
            snapshot = season_record.get("career_player_stats")
            if isinstance(snapshot, list):
                for row in snapshot:
                    add_totals(row.get("player_id"), row.get("player"), row.get("team"),
                               row.get("position"), row.get("apps", 0), row.get("goals", 0),
                               row.get("assists", 0), row.get("minutes", 0))
            else:
                for match in season_record.get("matches", []) or []:
                    if match.get("played", True):
                        accumulate_legacy_match(match)

        # Active season totals are already maintained for starters and substitutes,
        # so they are more accurate than old match schemas that may not store subs.
        current_stats = self.s.get("season_stats", {})
        has_current_totals = isinstance(current_stats, dict) and any(
            any(int(stat.get(key, 0) or 0) for key in ("apps", "goals", "assists"))
            for stat in current_stats.values() if isinstance(stat, dict))
        if not self.s.get("season_done") and has_current_totals:
            team_by_player = {str(pid): club for club, ids in self.s.get("rosters", {}).items() for pid in ids}
            for pid, stat in current_stats.items():
                club = team_by_player.get(str(pid)) or (players.get(pid, {}) or {}).get("club", "")
                if not club or not isinstance(stat, dict):
                    continue
                if not any(int(stat.get(key, 0) or 0) for key in ("apps", "goals", "assists")):
                    continue
                player_row = players.get(pid, {}) or {}
                add_totals(pid, player_row.get("common_name", str(pid)), club,
                           player_row.get("position", ""), stat.get("apps", 0), stat.get("goals", 0),
                           stat.get("assists", 0), stat.get("mins", 0))
        elif not self.s.get("season_done"):
            for match in self.s.get("matches", []) or []:
                if match.get("played", True):
                    accumulate_legacy_match(match)

        rows = list(aggregate.values())
        appearances = sorted((dict(row) for row in rows),
                             key=lambda r: (-r["apps"], -r["minutes"], r["player"].casefold(), r["team"].casefold()))
        goals = sorted((dict(row) for row in rows if row["goals"] > 0),
                       key=lambda r: (-r["goals"], -r["apps"], r["player"].casefold()))
        assists = sorted((dict(row) for row in rows if row["assists"] > 0),
                         key=lambda r: (-r["assists"], -r["apps"], r["player"].casefold()))
        return {"appearances": appearances[:max(0, int(limit))],
                "goals": goals[:max(0, int(limit))],
                "assists": assists[:max(0, int(limit))]}

    def club_history_analytics(self, team):
        """Build club-only season finishes, match history and all-time statistics."""
        if not team:
            return {"seasons": [], "by_season": [], "by_competition": [], "matches": [],
                    "stats": {}, "top11": []}

        all_matches = [dict(match) for match in self._career_match_records()
                       if team in (match.get("home"), match.get("away"))]
        all_matches.sort(key=lambda row: (int(row.get("season", 0) or 0),
                                          str(row.get("date", "")), int(row.get("id", 0) or 0)))
        seasons = {}
        for record in self.s.get("history", []) or []:
            season = int(record.get("season", 0) or 0)
            if season:
                seasons[season] = dict(record)
        current_season = int(self.s.get("season", 0) or 0)
        if current_season:
            current_record = seasons.setdefault(current_season, {})
            current_record.setdefault("season", current_season)
            current_record.setdefault("matches", [
                dict(match) for match in self.s.get("matches", [])
                if int(match.get("season", current_season) or current_season) == current_season
            ])

        season_rows = []
        competition_rows = []
        competition_names = (("premier", "Premier League"), ("fc1", "FC1"),
                             ("fc2", "FC2"), ("cup", "Major Cup"), ("c1", "C1"))
        league_labels = {"premier": "Premier League", "fc1": "Football Championship 1",
                         "fc2": "Football Championship 2"}
        stage_labels = {
            "cup_r1": "Vòng 1", "cup_r16": "Vòng 16", "cup_qf": "Tứ kết",
            "cup_sf": "Bán kết", "cup_final": "Á quân",
            "c1_pi": "Play-in", "c1_group": "Vòng bảng",
            "c1_semi": "Bán kết", "c1_final": "Á quân",
        }

        def finish_for_competition(record, season, key):
            record_matches = [m for m in (record.get("matches") or [])
                              if m.get("played", True) and team in (m.get("home"), m.get("away"))]
            comp_matches = [m for m in record_matches if (
                (key == "cup" and m.get("comp") == "Cup") or
                (key == "c1" and m.get("comp") in ("C1", "C1 Play-in"))
            )]
            if key in league_labels:
                rows = (record.get("leagues", {}) or {}).get(key, []) or []
                pos = next((i for i, row in enumerate(rows, 1) if row.get("team") == team), None)
                if pos is None and season == current_season and not self.s.get("season_done"):
                    table = (self.premier_phase2_tables()["champion"] + self.premier_phase2_tables()["challenge"]
                             if key == "premier" and self.s.get("league_phase2", {}).get("initialized")
                             else self.league_phase1_table(key))
                    pos = next((i for i, row in enumerate(table, 1) if row.get("team") == team), None)
                if not pos:
                    return "—"
                return f"Tạm thời · Hạng {pos}" if season == current_season and not self.s.get("season_done") else f"Hạng {pos}"
            if key == "cup":
                if season == current_season and not self.s.get("season_done"):
                    return "Đang diễn ra" if comp_matches else "Chưa thi đấu"
                if record.get("champion_cup") == team:
                    return "Vô địch"
                if record.get("runner_up_cup") == team:
                    return "Á quân"
                stages = [str(m.get("stage", "")) for m in comp_matches]
                order = ("cup_final", "cup_sf", "cup_qf", "cup_r16", "cup_r1")
                furthest = next((stage for stage in order if stage in stages), None)
                return f"Dừng ở {stage_labels[furthest]}" if furthest else "Không tham dự"
            if season == current_season and not self.s.get("season_done"):
                if comp_matches:
                    return "Đang diễn ra"
                c1_state = self.s.get("c1", {}) or {}
                eligible = set(c1_state.get("direct_teams", []) or []) | set(c1_state.get("playin_seed_teams", []) or [])
                return "Chưa thi đấu" if team in eligible else "Không tham dự"
            if record.get("champion_c1") == team or (record.get("c1") or {}).get("champion") == team:
                return "Vô địch"
            if record.get("runner_up_c1") == team or (record.get("c1") or {}).get("runner_up") == team:
                return "Á quân"
            stages = [str(m.get("stage", "")) for m in comp_matches]
            if any(stage.startswith("c1_semi") for stage in stages):
                return "Bán kết"
            if any(stage.startswith("c1_group") for stage in stages):
                return "Vòng bảng"
            if any(stage.startswith("c1_pi") for stage in stages):
                return "Play-in"
            return "Không tham dự"

        for season in sorted(seasons, reverse=True):
            record = seasons[season]
            row = {"season": season, "label": record.get("label", str(season))}
            for key, label in competition_names:
                finish = finish_for_competition(record, season, key)
                row[key] = finish
                if finish not in ("—", "Không tham dự"):
                    competition_rows.append({"competition": label, "season": season, "finish": finish})
            season_rows.append(row)
        competition_order = {label: index for index, (_key, label) in enumerate(competition_names)}
        competition_rows.sort(key=lambda row: (competition_order.get(row["competition"], 99),
                                                -int(row.get("season", 0))))

        wins = draws = losses = 0
        biggest_win = biggest_loss = None
        for match in all_matches:
            home = match.get("home") == team
            try:
                gf = int(match.get("hg" if home else "ag"))
                ga = int(match.get("ag" if home else "hg"))
            except (TypeError, ValueError):
                continue
            if gf > ga:
                wins += 1
            elif gf == ga:
                # A penalty shoot-out determines progression, not the 90-minute W/D/L.
                draws += 1
            else:
                losses += 1
            margin = abs(gf - ga)
            detail = {"date": match.get("date", "—"), "competition": match.get("comp", "—"),
                      "home": match.get("home", ""), "away": match.get("away", ""),
                      "score": f"{match.get('hg')}–{match.get('ag')}", "margin": margin}
            if gf > ga and (biggest_win is None or margin > biggest_win["margin"]):
                biggest_win = detail
            if gf < ga and (biggest_loss is None or margin > biggest_loss["margin"]):
                biggest_loss = detail

        legends = self.career_player_legends(team=team, limit=100000)
        top_scorer = (legends.get("goals") or [None])[0]
        top_assister = (legends.get("assists") or [None])[0]
        first_match = all_matches[0] if all_matches else None
        stats = {
            "matches": len(all_matches), "wins": wins, "draws": draws, "losses": losses,
            "top_scorer": top_scorer, "top_assister": top_assister,
            "biggest_win": biggest_win, "biggest_loss": biggest_loss,
            "first_match": first_match,
        }
        return {"seasons": sorted(seasons, reverse=True), "by_season": season_rows,
                "by_competition": competition_rows, "matches": all_matches,
                "stats": stats, "top11": (legends.get("appearances") or [])[:11]}

    @staticmethod
    def _player_search_fold(value):
        """Normalize names for case- and accent-insensitive player lookup."""
        text = str(value or "").replace("Đ", "D").replace("đ", "d")
        return "".join(char for char in unicodedata.normalize("NFKD", text).casefold()
                       if not unicodedata.combining(char)).strip()

    def career_player_directory(self, query=""):
        """Search every known player record, including retired and academy players.

        The returned rows carry their source player/meta payload so the GUI can
        open a profile even when an old archive has no live ``players`` entry.
        Search covers active, free-agent, retired, academy and archived careers.
        """
        needle = self._player_search_fold(query)
        if len(needle) < 2:
            return []

        state = self.s
        players = state.get("players", {}) or {}
        metas = state.get("player_meta", {}) or {}
        contracts = state.get("contracts", {}) or {}
        rosters = state.get("rosters", {}) or {}
        roster_by_player = {str(pid): str(team) for team, ids in rosters.items() for pid in ids or []}
        free_agents = {str(pid) for pid in state.get("free_agents", []) or []}
        retired = {}
        for item in state.get("retired_players", []) or []:
            if isinstance(item, dict):
                pid = str(item.get("player_id") or item.get("id") or "").strip()
                if pid:
                    retired[pid] = dict(item)
        for archive in state.get("history", []) or []:
            for item in archive.get("retired", []) or []:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("player_id") or item.get("id") or "").strip()
                if pid:
                    previous = retired.get(pid, {})
                    if not previous or int(item.get("retire_season", 0) or 0) >= int(previous.get("retire_season", 0) or 0):
                        retired[pid] = dict(item)

        entries = {}

        def add_entry(pid, player=None, meta=None, club_hint="", status_hint=""):
            pid = str(pid or "").strip()
            if not pid:
                return
            player = dict(player or {})
            meta = dict(meta or {})
            retirement = retired.get(pid, {})
            contract = contracts.get(pid) or {}
            player.setdefault("player_id", pid)
            player.setdefault("common_name", retirement.get("player_name") or retirement.get("common_name") or pid)
            if not player.get("position"):
                player["position"] = retirement.get("position") or "—"
            if not player.get("overall_rating") and retirement.get("overall_peak") is not None:
                player["overall_rating"] = retirement.get("overall_peak")
            if not player.get("club"):
                player["club"] = retirement.get("club") or club_hint or ""
            name = str(player.get("common_name") or player.get("name") or retirement.get("player_name") or pid).strip()
            if not name:
                return
            club = roster_by_player.get(pid) or contract.get("team_id") or player.get("club") or club_hint or retirement.get("club") or "—"
            if meta.get("status") == "retired" or pid in retired:
                status = "Giải nghệ"
            elif status_hint == "Học viện":
                status = "Học viện"
            elif pid in roster_by_player:
                status = "Đang thi đấu"
            elif pid in free_agents or contract.get("status") in ("EXPIRED", "FREE_AGENT"):
                status = "Cầu thủ tự do"
            else:
                status = status_hint or "Trong dữ liệu career"
            row = {
                "player_id": pid, "iid": f"player-search:{pid}", "player": name,
                "club": str(club or "—"), "position": str(player.get("position") or "—"),
                "status": status, "ovr": player.get("overall_rating", retirement.get("overall_peak", "—")),
                "age": meta.get("age", retirement.get("age")),
                "player_data": player, "meta_data": meta,
            }
            # Prefer a live player payload over a snapshot-only fallback, while
            # keeping an academy identity distinct from the senior player pool.
            old = entries.get(pid)
            if old is None or (not old.get("player_data", {}).get("overall_rating") and player.get("overall_rating")):
                entries[pid] = row

        for pid, player in players.items():
            pid = str(pid)
            add_entry(pid, player, metas.get(pid, {}))
        # Retirement snapshots are searchable even when their player object was
        # removed by an older save migration.
        for pid, retirement in retired.items():
            if pid not in entries:
                add_entry(pid, retirement, metas.get(pid, {}), retirement.get("club"), "Giải nghệ")
        # Some old compact archives retain career names after the source player
        # object has disappeared. Keep those records searchable and profileable.
        for archive in state.get("history", []) or []:
            for item in archive.get("career_player_stats", []) or []:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("player_id") or "").strip()
                if pid and pid not in entries:
                    add_entry(pid, {"player_id": pid, "common_name": item.get("player") or pid,
                                    "position": item.get("position") or "—", "club": item.get("team") or ""},
                              metas.get(pid, {}), item.get("team") or "")
        for pid, rows in (state.get("player_career", {}) or {}).items():
            if str(pid) in entries:
                continue
            last = next((r for r in reversed(rows or []) if isinstance(r, dict)), {})
            add_entry(pid, {"player_id": str(pid), "common_name": last.get("player") or str(pid),
                            "position": last.get("position") or "—", "club": last.get("club") or ""},
                      metas.get(str(pid), {}), last.get("club") or "")

        # Academy players are part of the player database too, but have a
        # separate profile payload until promoted to a senior roster.
        academy_rows = []
        for team, academy in (state.get("academy", {}) or {}).items():
            for item in academy or []:
                if not isinstance(item, dict):
                    continue
                player = item.get("player", {}) or {}
                meta = item.get("meta", {}) or {}
                pid = str(player.get("player_id") or player.get("id") or "").strip()
                if not pid or pid in entries:
                    continue
                name = str(player.get("common_name") or player.get("name") or pid).strip()
                row = {"player_id": pid, "iid": f"academy-search:{pid}", "player": name,
                       "club": str(team), "position": str(player.get("position") or "—"),
                       "status": "Học viện", "ovr": player.get("overall_rating", "—"),
                       "age": meta.get("age"), "player_data": dict(player), "meta_data": dict(meta)}
                academy_rows.append(row)

        all_rows = list(entries.values()) + academy_rows
        matched = []
        for row in all_rows:
            folded_name = self._player_search_fold(row.get("player", ""))
            if needle not in folded_name:
                continue
            row = dict(row)
            row["_match_rank"] = 0 if folded_name == needle else (1 if folded_name.startswith(needle) else 2)
            matched.append(row)
        matched.sort(key=lambda row: (row["_match_rank"], self._player_search_fold(row["player"]),
                                      str(row.get("club", "")).casefold(), row["player_id"]))
        for row in matched:
            row.pop("_match_rank", None)
        return matched

    def career_hall_of_fame(self, limit=50):
        """Return retired-player Hall of Fame entries using verified career data.

        Retirement rows are the membership source. Career totals are rebuilt from
        saved season snapshots/match logs; the final-season retirement CSV values
        are used only when no career aggregate is available, avoiding double count.
        """
        retired = {}
        for row in self.s.get("retired_players", []) or []:
            if not isinstance(row, dict):
                continue
            pid = str(row.get("player_id") or row.get("id") or "").strip()
            if not pid:
                continue
            old = retired.get(pid)
            candidate = dict(row)
            if old is None or int(candidate.get("retire_season", 0) or 0) >= int(old.get("retire_season", 0) or 0):
                retired[pid] = candidate
        for season_record in self.s.get("history", []) or []:
            for row in season_record.get("retired", []) or []:
                if not isinstance(row, dict):
                    continue
                pid = str(row.get("player_id") or row.get("id") or "").strip()
                if not pid:
                    continue
                old = retired.get(pid)
                candidate = dict(row)
                if old is None or int(candidate.get("retire_season", 0) or 0) >= int(old.get("retire_season", 0) or 0):
                    retired[pid] = candidate

        career_by_player = {}
        legends = self.career_player_legends(limit=100000)
        for category in ("appearances", "goals", "assists"):
            for row in legends.get(category, []) or []:
                pid = str(row.get("player_id") or "")
                if not pid:
                    continue
                total = career_by_player.setdefault(pid, {
                    "apps": 0, "goals": 0, "assists": 0, "minutes": 0, "clubs": set()
                })
                total["apps"] = max(total["apps"], 0) if category == "appearances" else total["apps"]
                total["clubs"].add(str(row.get("team") or ""))
                if category == "appearances":
                    total["apps"] += int(row.get("apps", 0) or 0)
                    total["minutes"] += int(row.get("minutes", 0) or 0)
                elif category == "goals":
                    total["goals"] += int(row.get("goals", 0) or 0)
                elif category == "assists":
                    total["assists"] += int(row.get("assists", 0) or 0)

        honors_by_player = {}
        def add_honor(pid, season_label, title, club="", competition="", award_id=""):
            pid = str(pid or "")
            if not pid:
                return
            token = (str(season_label), str(title), str(competition or ""))
            entry = honors_by_player.setdefault(pid, {})
            entry.setdefault(token, {"season": str(season_label), "title": str(title),
                                     "club": str(club or "—"),
                                     "competition": str(competition or ""),
                                     "award_id": str(award_id or "")})

        for record in self.s.get("history", []) or []:
            season = int(record.get("season", 0) or 0)
            label = record.get("label") or (f"{season}/{season + 1}" if season else "—")
            if int(record.get("individual_awards_version", 0) or 0) >= 1:
                for award in record.get("individual_awards", []) or []:
                    if not isinstance(award, dict):
                        continue
                    add_honor(award.get("player_id"), label, award.get("title") or "Danh hiệu cá nhân",
                              award.get("club"), award.get("competition"), award.get("award_id"))
                continue
            annual = record.get("season_awards") or {}
            podium = annual.get("ballon_podium") or []
            podium_titles = ("Quả bóng Vàng", "Quả bóng Bạc", "Quả bóng Đồng")
            if podium:
                for index, winner in enumerate(podium[:3]):
                    add_honor(winner.get("player_id"), label, podium_titles[index], winner.get("team"))
            else:
                for key, title in (("ballon_dor", "Quả bóng Vàng"),
                                   ("ballon_silver", "Quả bóng Bạc"),
                                   ("ballon_bronze", "Quả bóng Đồng")):
                    winner = annual.get(key) or {}
                    add_honor(winner.get("player_id"), label, title, winner.get("team"))
            for key, title in (("young_player", "Golden Boy"), ("golden_boy", "Golden Boy"),
                               ("golden_glove", "Găng tay Vàng")):
                winner = annual.get(key) or {}
                add_honor(winner.get("player_id"), label, title, winner.get("team"), award_id=key)
            for row in annual.get("best_xi_442", []) or []:
                add_honor(row.get("player_id"), label, "Đội hình tiêu biểu", row.get("team"))
            for award in (record.get("competition_awards") or {}).values():
                winner = (award or {}).get("player_of_tournament") or {}
                if winner.get("player_id"):
                    comp_name = (award or {}).get("competition") or "Giải đấu"
                    add_honor(winner.get("player_id"), label, f"Cầu thủ xuất sắc · {comp_name}", winner.get("team"))

        rows = []
        for pid, retirement in retired.items():
            career = career_by_player.get(pid, {})
            has_career = pid in career_by_player and any(
                int(career.get(key, 0) or 0) for key in ("apps", "goals", "assists", "minutes")
            )
            honors = sorted(honors_by_player.get(pid, {}).values(),
                            key=lambda item: (item["season"], item["title"]))
            row = {
                "player_id": pid,
                "player": retirement.get("player_name") or retirement.get("common_name") or pid,
                "club": retirement.get("club") or retirement.get("team") or "—",
                "position": retirement.get("position") or "—",
                "retire_season": retirement.get("retire_season"),
                "age": retirement.get("age"),
                "overall_peak": retirement.get("overall_peak"),
                "appearances": career.get("apps", 0) if has_career else int(retirement.get("appearances", 0) or 0),
                "goals": career.get("goals", 0) if has_career else int(retirement.get("goals", 0) or 0),
                "assists": career.get("assists", 0) if has_career else int(retirement.get("assists", 0) or 0),
                "minutes": career.get("minutes", 0) if has_career else None,
                "clubs": len({club for club in career.get("clubs", set()) if club}) or 1,
                "honors_count": len(honors),
                "honors": honors,
                "retirement_type": retirement.get("retirement_type") or "—",
                "retirement_reason": retirement.get("retirement_reason") or "",
                "early_retirement": bool(retirement.get("early_retirement", False)),
            }
            rows.append(row)
        rows.sort(key=lambda row: (-int(row.get("honors_count", 0) or 0),
                                   -int(row.get("overall_peak", 0) or 0),
                                   -int(row.get("goals", 0) or 0),
                                   -int(row.get("appearances", 0) or 0),
                                   str(row.get("player", "")).casefold()))
        # This screen is intentionally a curated Top 50, even if a caller
        # accidentally asks for a larger page size.
        return rows[:min(50, max(0, int(limit)))]

    def career_record_table(self):
        """Build a compact all-time record book from season archives and saved matches."""
        records = []
        legends = self.career_player_legends(limit=100000)
        career = {}
        for category in ("appearances", "goals", "assists"):
            for row in legends.get(category, []) or []:
                pid = str(row.get("player_id") or "")
                if not pid:
                    continue
                item = career.setdefault(pid, {"player": row.get("player", pid), "apps": 0,
                                               "goals": 0, "assists": 0, "clubs": set()})
                item["clubs"].add(str(row.get("team") or ""))
                field = {"appearances": "apps", "goals": "goals", "assists": "assists"}[category]
                item[field] += int(row.get(field, 0) or 0)
        for field, title, unit in (("apps", "Ra sân nhiều nhất sự nghiệp", "trận"),
                                   ("goals", "Ghi bàn nhiều nhất sự nghiệp", "bàn"),
                                   ("assists", "Kiến tạo nhiều nhất sự nghiệp", "kiến tạo")):
            candidates = sorted(career.values(), key=lambda row: (-row[field], str(row["player"]).casefold()))
            if candidates and candidates[0][field] > 0:
                holder = candidates[0]
                records.append({"record": title, "holder": holder["player"], "club": ", ".join(sorted(x for x in holder["clubs"] if x)),
                                "value": f"{holder[field]} {unit}", "season": "Toàn career"})

        best_single_season = None
        best_points = None
        biggest_win = None
        longest_unbeaten = None
        unbeaten_by_team = {}
        for season_record in self.s.get("history", []) or []:
            label = season_record.get("label") or str(season_record.get("season", "—"))
            for row in season_record.get("top_scorers", []) or []:
                candidate = {"record": "Ghi bàn nhiều nhất trong một mùa", "holder": row.get("player", "—"),
                             "club": row.get("team", "—"), "value": f"{int(row.get('goals', 0) or 0)} bàn",
                             "season": label, "_value": int(row.get("goals", 0) or 0)}
                if best_single_season is None or candidate["_value"] > best_single_season["_value"]:
                    best_single_season = candidate
            for tier, rows in (season_record.get("leagues", {}) or {}).items():
                for row in rows or []:
                    candidate = {"record": "Nhiều điểm nhất trong một mùa", "holder": row.get("team", "—"),
                                 "club": row.get("team", "—"), "value": f"{int(row.get('pts', 0) or 0)} điểm",
                                 "season": f"{label} · {tier.upper()}", "_value": int(row.get("pts", 0) or 0)}
                    if best_points is None or candidate["_value"] > best_points["_value"]:
                        best_points = candidate
        for match in self._career_match_records():
            if not match.get("played", True):
                continue
            home, away = str(match.get("home", "")), str(match.get("away", ""))
            try:
                hg, ag = int(match.get("hg")), int(match.get("ag"))
            except (TypeError, ValueError):
                continue
            margin = abs(hg - ag)
            if margin > 0 and (biggest_win is None or margin > biggest_win["_value"]):
                winner = home if hg > ag else away
                biggest_win = {"record": "Chiến thắng đậm nhất", "holder": winner, "club": winner,
                               "value": f"{margin} bàn", "season": f"{match.get('season', '—')} · {match.get('comp', '—')} · {hg}–{ag}",
                               "_value": margin}
            for team, gf, ga in ((home, hg, ag), (away, ag, hg)):
                if not team:
                    continue
                state = unbeaten_by_team.setdefault(team, {"current": 0, "best": 0, "best_season": None})
                if gf >= ga:
                    state["current"] += 1
                    if state["current"] > state["best"]:
                        state["best"] = state["current"]
                        state["best_season"] = match.get("season")
                    if longest_unbeaten is None or state["best"] > longest_unbeaten["_value"]:
                        longest_unbeaten = {"record": "Chuỗi bất bại dài nhất", "holder": team, "club": team,
                                            "value": f"{state['best']} trận", "season": str(state["best_season"] or "—"),
                                            "_value": state["best"]}
                else:
                    state["current"] = 0
        for candidate in (best_single_season, best_points, biggest_win, longest_unbeaten):
            if candidate:
                candidate.pop("_value", None)
                records.append(candidate)
        trophy_rows = self.career_trophy_table()
        if trophy_rows and trophy_rows[0].get("total", 0) > 0:
            row = trophy_rows[0]
            records.append({"record": "CLB giành nhiều danh hiệu nhất", "holder": row["team"], "club": row["team"],
                            "value": f"{row['total']} danh hiệu", "season": "Toàn career"})
        return records

    def status_text(self):
        done, total = self.slot_progress()
        next_slot = self.next_slot()
        if self.s["season_done"]:
            return f"Mùa {self.season_label()} đã hoàn tất · {len(self.s['matches'])} trận · {len(self.s['history'])} mùa trong lịch sử."
        if next_slot:
            return f"Mùa {self.season_label()} · Lịch {done}/{total} slot · Tiếp theo {next_slot['date']} — {next_slot['label']}."
        return f"Mùa {self.season_label()} · đang chờ chốt mùa."
