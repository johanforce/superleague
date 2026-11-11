# -*- coding: utf-8 -*-
"""Giao diện desktop Tkinter cho Super League Simulator.

Bản thiết kế lại: thanh điều hướng bên trái, thanh tiến độ mùa giải, bảng sọc,
thông báo dạng toast, báo cáo trận dạng bảng điểm, sơ đồ Cup/C1 bo góc.
Toàn bộ logic game, bộ lọc và dữ liệu giữ nguyên như bản cũ.
"""
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont
from datetime import date, timedelta, datetime
from pathlib import Path
import platform
import traceback
import threading
import json

from sl_game import Game, TIERS, TIER_LABELS, APP_DIR
from league_intelligence import forecast_season
from sl_google_sheets import compress_pil_image
from sl_match import adapter as match_adapter
from sl_match import engine as match_engine_core
from sl_match import report_ui as match_report_ui
from sl_match import news as match_news
from economy.notifications import format_economy_notification, player_display_name
from economy import player_profile as pprofile

# ── Bảng màu: "sân đêm" ──────────────────────────────────────────────────────
BG         = "#0c1319"   # nền cửa sổ
SIDEBAR    = "#090e13"   # thanh bên
SURFACE    = "#131c25"   # thẻ, thanh công cụ
SURFACE_2  = "#0f1820"   # vùng bảng, canvas
STRIPE     = "#111b24"   # dòng chẵn của bảng
RAISED     = "#1b2733"   # nút phụ, mục đang chọn
LINE       = "#233241"   # viền mảnh
LINE_2     = "#34485b"   # viền đậm, đường nối bracket
TEXT       = "#e9edf1"
MUTED      = "#8a9aab"
DIM        = "#9aa8b7"   # chữ phụ/disabled: tương phản AA trên nền tối
IVORY      = "#efe9d8"   # nút chính
GOLD       = "#e3b94e"   # chỉ dùng cho nhà vô địch / điểm nhấn
GOLD_BG    = "#2a2514"
TURF       = "#5cc58a"   # thắng, lên hạng
TURF_LIGHT = "#9be0b9"
TURF_DIM   = "#3b8f68"
TURF_BG    = "#12301f"
RED        = "#e0636b"   # xuống hạng
RED_BG     = "#38191e"
SELECT     = "#23415a"
HOME_COL   = TURF
AWAY_COL   = "#78a6d6"
PANEL, ACCENT = SURFACE, TURF   # tên cũ, giữ để tương thích

UI_FONT = "Helvetica"
MONO_FONT = "Courier"

# AI controls every club decision; the user controls only when/how much simulation advances.
# Club management remains read-only in the GUI.
SPECTATOR_MODE = True


def init_fonts(root):
    """Chọn font có sẵn trên máy (hỗ trợ tiếng Việt) cho từng hệ điều hành."""
    global UI_FONT, MONO_FONT
    families = {name.lower(): name for name in tkfont.families(root)}

    def pick(candidates, fallback):
        for name in candidates:
            if name.lower() in families:
                return families[name.lower()]
        return fallback

    UI_FONT = pick(("Segoe UI", "Helvetica Neue", "SF Pro Text", "Ubuntu", "Noto Sans", "DejaVu Sans", "Arial"), "Helvetica")
    MONO_FONT = pick(("Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono", "Courier New"), "Courier")


def F(size=10, weight="normal", mono=False):
    return (MONO_FONT if mono else UI_FONT, size, weight)


def _format_optional_float(value, decimals=2, missing="—"):
    """Format an optional numeric value without breaking GUI refresh callbacks."""
    if value in (None, ""):
        return missing
    try:
        return f"{float(value):.{int(decimals)}f}"
    except (TypeError, ValueError, OverflowError):
        return missing


def make_card(parent, bg=SURFACE):
    return tk.Frame(parent, bg=bg, bd=0, highlightthickness=1, highlightbackground=LINE, highlightcolor=LINE)


def autohide(scrollbar):
    """Thanh cuộn chỉ hiện khi nội dung dài hơn khung."""
    def _set(lo, hi):
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            scrollbar.grid_remove()
        else:
            scrollbar.grid()
        scrollbar.set(lo, hi)
    return _set


class FlatButton(tk.Label):
    """Nút phẳng dùng Label để màu giống nhau trên Windows, macOS và Linux."""
    KINDS = {
        "primary":   {"bg": IVORY,  "fg": "#101820", "hover_bg": "#ffffff", "hover_fg": "#101820"},
        "secondary": {"bg": RAISED, "fg": TEXT,      "hover_bg": "#27384a", "hover_fg": TEXT},
        "ghost":     {"bg": None,   "fg": MUTED,     "hover_bg": RAISED,    "hover_fg": TEXT},
        "danger":    {"bg": RED_BG, "fg": "#f4c7cb", "hover_bg": "#54232a", "hover_fg": "#ffffff"},
    }

    def __init__(self, parent, text, command, kind="secondary", font=None, padx=16, pady=8):
        spec = self.KINDS[kind]
        base = spec["bg"] or parent.cget("bg")
        super().__init__(parent, text=text, bg=base, fg=spec["fg"], font=font or F(10, "bold"),
                         padx=padx, pady=pady, cursor="hand2", takefocus=True,
                         highlightthickness=1, highlightbackground=base, highlightcolor=GOLD)
        self._spec, self._base, self._command, self._enabled = spec, base, command, True
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Return>", self._on_click)
        self.bind("<space>", self._on_click)

    def _on_enter(self, _event=None):
        if self._enabled:
            self.configure(bg=self._spec["hover_bg"], fg=self._spec["hover_fg"])

    def _on_leave(self, _event=None):
        if self._enabled:
            self.configure(bg=self._base, fg=self._spec["fg"])

    def _on_click(self, _event=None):
        if self._enabled and self._command:
            self._command()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.configure(fg=self._spec["fg"] if enabled else DIM, bg=self._base, cursor="hand2" if enabled else "")


class NavItem(tk.Frame):
    """Một mục trong thanh điều hướng bên trái."""

    def __init__(self, parent, text, command):
        super().__init__(parent, bg=SIDEBAR, cursor="hand2")
        self._active = False
        self._mark = tk.Frame(self, bg=SIDEBAR, width=3)
        self._mark.pack(side="left", fill="y")
        self._label = tk.Label(self, text=text, anchor="w", font=F(10), bg=SIDEBAR, fg=MUTED,
                               padx=17, pady=6, cursor="hand2")
        self._label.pack(side="left", fill="x", expand=True)
        for widget in (self, self._label, self._mark):
            widget.bind("<Button-1>", lambda _e: command())
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)

    def _paint(self, bg, fg, mark, weight):
        self.configure(bg=bg)
        self._label.configure(bg=bg, fg=fg, font=F(10, weight))
        self._mark.configure(bg=mark)

    def _enter(self, _event=None):
        if not self._active:
            self._paint("#0f171e", TEXT, "#0f171e", "normal")

    def _leave(self, _event=None):
        if not self._active:
            self._paint(SIDEBAR, MUTED, SIDEBAR, "normal")

    def set_active(self, active):
        self._active = bool(active)
        if active:
            self._paint("#121c26", TEXT, GOLD, "bold")
        else:
            self._paint(SIDEBAR, MUTED, SIDEBAR, "normal")


class Segmented(tk.Frame):
    """Bộ chuyển trang dạng nút phân đoạn, thay cho ttk.Notebook lồng nhau."""

    def __init__(self, parent, hint=""):
        super().__init__(parent, bg=BG)
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", pady=(0, 10))
        self._bar = tk.Frame(head, bg=SURFACE, highlightthickness=1, highlightbackground=LINE)
        self._bar.pack(side="left")
        if hint:
            tk.Label(head, text=hint, font=F(9), bg=BG, fg=MUTED).pack(side="left", padx=14)
        self._body = tk.Frame(self, bg=BG)
        self._body.pack(fill="both", expand=True)
        self._items = {}
        self._current = None

    def add(self, key, text):
        frame = tk.Frame(self._body, bg=BG)
        label = tk.Label(self._bar, text=text, font=F(10, "bold"), padx=16, pady=6,
                         cursor="hand2", bg=SURFACE, fg=MUTED)
        label.pack(side="left", padx=2, pady=2)
        label.bind("<Button-1>", lambda _e, k=key: self.select(k))
        label.bind("<Enter>", lambda _e, k=key: self._hover(k, True))
        label.bind("<Leave>", lambda _e, k=key: self._hover(k, False))
        self._items[key] = (label, frame)
        if self._current is None:
            self.select(key)
        return frame

    def _hover(self, key, inside):
        if key != self._current:
            self._items[key][0].configure(fg=TEXT if inside else MUTED)

    def select(self, key):
        for name, (label, frame) in self._items.items():
            if name == key:
                label.configure(bg=RAISED, fg=TEXT)
                frame.pack(fill="both", expand=True)
            else:
                label.configure(bg=SURFACE, fg=MUTED)
                frame.pack_forget()
        self._current = key


class _UpcomingMatchGameProxy:
    """Bridge LivePage to a selected match and its same-kickoff companion fixtures."""
    def __init__(self, app, preview, simulate_remaining=False, companion_previews=None, read_only=False):
        self.app = app
        self.preview = preview
        self.simulate_remaining = bool(simulate_remaining)
        self.companion_previews = list(companion_previews or [])
        self.read_only = bool(read_only)
        self.completed_matches = []

    def concurrent_scoreboard(self):
        if self.read_only:
            return []
        return self.app.game.slot_concurrent_scoreboard(self.preview)

    def live_league_table(self, preview, provisional_results=None):
        if self.read_only:
            return {"available": False, "note": "Phát lại lịch sử · bảng xếp hạng không cập nhật."}
        return self.app.game.live_league_table(preview, provisional_results)

    def commit_live(self, match, _prep):
        if self.read_only:
            raise RuntimeError("Phát lại trận lịch sử là chế độ chỉ đọc.")
        committed, message = self.app.game.commit_slot_fixture(self.preview)
        match.update(committed)
        self.preview.update(committed)
        self.completed_matches = [committed]
        if self.simulate_remaining and self.companion_previews:
            batch = self.app.game.commit_prepared_slot_fixtures(self.companion_previews)
            companion_matches = list(batch.get("matches", []))
            self.completed_matches.extend(companion_matches)
            count = len(companion_matches)
            message = (f"Đã chơi trận được chọn và mô phỏng đồng thời thêm {count} trận cùng giải; "
                       "các giải khác trong cùng giờ không bị ảnh hưởng.")
        self.app.root.after_idle(self.app.refresh_all)
        self.app.toast(message)
        return message

    def reports_after_live(self, selected_match=None):
        """All played matches in this kickoff slot and competition, in fixture order.

        Include same-competition fixtures that the user played earlier in the slot, then
        the selected Live Match and any synchronized companion matches just committed.
        Other competitions and still-pending fixtures are excluded.
        """
        anchor = selected_match or self.preview
        if self.read_only:
            return [anchor]
        competition = anchor.get("comp")
        slot_specs = [
            item for item in (self.preview.get("slot_fixtures") or [])
            if item.get("comp") == competition
        ]
        result_ids = self.preview.get("slot_result_ids") or {}
        matches_by_fixture = {}

        def fixture_key(item):
            return (item.get("stage"), str(item.get("date", "")),
                    item.get("home"), item.get("away"))

        # Recover fixtures already played before this Live Match was opened.
        for spec in slot_specs:
            match_id = result_ids.get(spec.get("spec_key"))
            if match_id is None:
                continue
            try:
                saved = self.app.game.match_by_id(match_id)
            except (StopIteration, TypeError, ValueError):
                saved = None
            if saved:
                matches_by_fixture[fixture_key(spec)] = saved

        # These are the current Live Match and companions committed at full-time.
        for item in self.completed_matches:
            if item.get("comp") == competition:
                matches_by_fixture[fixture_key(item)] = item

        matches = [matches_by_fixture[fixture_key(spec)] for spec in slot_specs
                   if fixture_key(spec) in matches_by_fixture]
        if not matches:
            matches = [anchor]
        return matches

    def report_of(self, match):
        return match_adapter.report_for_match(
            match, self.app.game.s["players"], self.app.game.s.get("match_refs", []))

    def report_header(self, m=None):
        match = m or self.preview
        note = "Phát lại trận đã lưu · chỉ đọc" if self.read_only else "Trận mô phỏng trực tiếp"
        if match.get("pens"):
            note += f" · luân lưu {match['pens']['home']}–{match['pens']['away']}"
        return (match.get("comp", "Trận đấu") + " · " + match.get("round", ""),
                match.get("date", ""),
                "Sân trung lập" if match.get("neutral") else "Sân nhà: " + match["home"],
                match["home"], match["away"],
                f"{match.get('hg', 0)}–{match.get('ag', 0)}", note)


class _UpcomingMatchHost:
    """Minimal host interface expected by the reusable WNL LivePage."""
    def __init__(self, app, window, entry, preview, simulate_remaining=False, companion_previews=None,
                 read_only=False):
        self.app, self.root, self.window, self.entry = app, app.root, window, entry
        self.g = _UpcomingMatchGameProxy(
            app, preview, simulate_remaining=simulate_remaining,
            companion_previews=companion_previews, read_only=read_only,
        )
        self.stack = [entry]
        self.page = None

    def say(self, message):
        self.app.status_var.set(message)

    def go_back(self):
        if self.page is not None:
            if not self.page.on_back():
                return
            self.page.cleanup()
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        if self.app._active_live_window is self.window:
            self.app._active_live_window = None
            self.app._active_live_host = None
            self.app._active_live_match_key = None
        if self.entry.get("dirty"):
            self.app._record_player_careers()
            self.app.refresh_all()



class SuperLeagueApp:
    def __init__(self, game: Game):
        self.game = game
        self.root = tk.Tk()
        self.root.withdraw()  # Build the complete window before showing it; avoids a blank first frame.
        self.root.title("Super League · Football Simulator")
        self.root.configure(bg=BG)
        self.root.resizable(True, True)
        init_fonts(self.root)
        self._toast = None
        self._toast_job = None
        self.current_page = None
        # Widget registries are created before page construction because the
        # squad page builds the career Top-11 trees before Cup/C1 leaderboards.
        self.career_legend_trees = {}
        self.leaderboard_trees = {}
        self._active_live_window = None
        self._active_live_host = None
        # Explicit user-started full-season batch. It advances one complete slot
        # per Tk event-loop turn so progress remains visible and cancellation is
        # possible between slots; it never starts the next season automatically.
        self._full_season_running = False
        self._full_season_cancel_requested = False
        self._full_season_after_id = None
        self._full_season_start_done = 0
        self._full_season_total_slots = 0
        self._configure_window_for_screen()
        self._style()
        self.root.report_callback_exception = self._report_callback_exception
        self._build()
        self.game.google_sheets_screenshot_provider = self._capture_google_sheets_screenshots
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.lift()
        # Render data after the event loop starts. If a save has an unexpected schema,
        # the window remains visible and reports the error rather than looking like a black screen.
        self.root.after(80, self._initial_refresh)
        if getattr(self.game, "google_sheets_startup_sync_pending", False):
            self.root.after(900, self._start_startup_google_sheets_sync)

    def _configure_window_for_screen(self):
        """Choose a window size that fits the actual desktop, including smaller displays."""
        self.root.update_idletasks()
        screen_w = max(800, self.root.winfo_screenwidth())
        screen_h = max(600, self.root.winfo_screenheight())
        width = min(1420, screen_w - 40)
        height = min(900, screen_h - 100)
        width = max(800, width)
        height = max(600, height)
        self.root.geometry(f"{width}x{height}+20+20")
        self.root.minsize(min(1100, width), min(680, height))

    def _style(self):
        style = ttk.Style(self.root)
        try: style.theme_use("clam")
        except tk.TclError: pass
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=F(10))
        style.configure("TPanedwindow", background=BG)
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
        style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED,
                        padding=(14, 8), borderwidth=0, font=F(9, "bold"))
        style.map("TNotebook.Tab", background=[("selected", RAISED), ("active", "#223243")],
                  foreground=[("selected", TEXT), ("active", TEXT)])
        style.configure("TButton", background=RAISED, foreground=TEXT, borderwidth=0,
                        padding=(10, 6), font=F(9, "bold"))
        style.map("TButton", background=[("active", "#2a3b4d"), ("pressed", SELECT)],
                  foreground=[("disabled", DIM), ("active", TEXT)])
        style.configure("TCheckbutton", background=BG, foreground=TEXT, font=F(9))
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("disabled", DIM)])

        # Bảng dữ liệu
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Treeview", background=SURFACE_2, foreground=TEXT, fieldbackground=SURFACE_2,
                        rowheight=30, borderwidth=0, relief="flat", font=F(10))
        style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=SURFACE, foreground=MUTED, font=F(9, "bold"),
                        padding=(8, 9), relief="flat", borderwidth=0,
                        bordercolor=LINE, lightcolor=SURFACE, darkcolor=SURFACE)
        style.map("Treeview.Heading", background=[("active", RAISED)], foreground=[("active", TEXT)])

        # Thanh cuộn mảnh
        for orient in ("Vertical", "Horizontal"):
            name = f"{orient}.TScrollbar"
            style.configure(name, background=RAISED, troughcolor=SURFACE_2, bordercolor=SURFACE_2,
                            lightcolor=RAISED, darkcolor=RAISED, arrowcolor=MUTED, gripcount=0,
                            relief="flat", arrowsize=12)
            style.map(name, background=[("active", LINE_2)])

        # Ô chọn
        style.configure("TCombobox", fieldbackground=SURFACE_2, background=RAISED, foreground=TEXT,
                        arrowcolor=MUTED, bordercolor=LINE, lightcolor=SURFACE_2, darkcolor=SURFACE_2,
                        padding=6, selectbackground=SURFACE_2, selectforeground=TEXT)
        style.map("TCombobox",
                  fieldbackground=[("readonly", SURFACE_2)], foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", SURFACE_2)], selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", GOLD)], arrowcolor=[("active", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", SURFACE)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SELECT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", f"{{{UI_FONT}}} 10")

        # Thanh tiến độ mùa
        style.configure("Season.Horizontal.TProgressbar", troughcolor=LINE, background=TURF,
                        bordercolor=SURFACE, lightcolor=TURF, darkcolor=TURF, thickness=6)
        style.configure("Loading.Horizontal.TProgressbar", troughcolor=LINE, background=GOLD,
                        bordercolor=SURFACE, lightcolor=GOLD, darkcolor=GOLD, thickness=5)

    # ── Khung ứng dụng ──────────────────────────────────────────────────────

    def _build(self):
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True)
        self.pages = {}
        self.nav_items = {}
        self._build_sidebar(shell)

        main = tk.Frame(shell, bg=BG)
        self.content_main = main  # Exclude sidebar when capturing season overview screenshots.
        main.pack(side="left", fill="both", expand=True, padx=26, pady=20)
        head = tk.Frame(main, bg=BG)
        head.pack(fill="x")
        self.page_title = tk.StringVar(value="")
        self.page_sub = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="")
        self.page_title_label = tk.Label(head, textvariable=self.page_title, font=F(21, "bold"), bg=BG, fg=TEXT)
        self.page_title_label.pack(side="left")
        self.page_header_actions = None
        head_actions = tk.Frame(head, bg=BG)
        head_actions.pack(side="right")
        self.page_header_actions = head_actions
        self.status_label = tk.Label(head_actions, textvariable=self.status_var, font=F(9), bg=BG, fg=MUTED)
        self.status_label.pack(side="right", pady=(8, 0))
        tk.Label(main, textvariable=self.page_sub, font=F(10), bg=BG, fg=MUTED, anchor="w").pack(fill="x", pady=(2, 14))
        self._build_matchday_bar(main)
        self.page_host = tk.Frame(main, bg=BG)
        self.page_host.pack(fill="both", expand=True, pady=(16, 0))

        self.dashboard = self._new_page("dashboard", "Tổng quan", "Tiến độ mùa giải, bản tin và đội đang dẫn đầu")
        self._build_dashboard()

        league_columns = [("pos","#",46),("team","CLB",220),("p","Trận",60),("w","T",50),("d","H",50),
                          ("l","B",50),("gf","BT",55),("ga","BB",55),("gd","HS",55),("pts","Điểm",65),
                          ("form","5 trận gần nhất",145),("elo","Elo",75)]
        self.league_trees = {}
        self.premier_phase2_trees = {}
        self.playoff_texts = {}
        self.league_segments = {}
        for tier in TIERS:
            title = TIER_LABELS.get(tier, tier)
            if tier == "premier":
                frame = self._new_page(tier, title, "Giai đoạn 1 → tách Champion/Challenge · giữ nguyên điểm tích lũy")
                seg = Segmented(frame, hint="Champion tìm nhà vô địch · Challenge xác định hai đội xuống hạng")
                self.league_segments[tier] = seg
                seg.pack(fill="both", expand=True)
                stage1_page = seg.add("stage1", "Giai đoạn 1 · BXH")
                champion_page = seg.add("champion", "Champion")
                challenge_page = seg.add("challenge", "Challenge")
                leaders_page = seg.add("leaders", "Ghi bàn & kiến tạo")
                self._build_leaderboard_pair(leaders_page, tier)
                phase2_columns = [
                    ("pos", "#", 42), ("team", "CLB", 210), ("p", "Trận", 52),
                    ("w", "T", 42), ("d", "H", 42), ("l", "B", 42),
                    ("gf", "BT", 48), ("ga", "BB", 48), ("gd", "HS", 48),
                    ("phase1_pts", "Điểm GĐ1", 82), ("phase2_pts", "Điểm GĐ2", 82),
                    ("pts", "Tổng điểm", 82), ("form", "5 trận gần nhất", 145), ("elo", "Elo", 65),
                ]
                self.league_trees[tier] = self._tree(stage1_page, league_columns)
                self.premier_phase2_trees["champion"] = self._tree(champion_page, phase2_columns)
                self.premier_phase2_trees["challenge"] = self._tree(challenge_page, phase2_columns)
                self._bind_standings_team_click(self.league_trees[tier])
                self._bind_standings_team_click(self.premier_phase2_trees["champion"])
                self._bind_standings_team_click(self.premier_phase2_trees["challenge"])
                for page, items in ((stage1_page, [(GOLD, "Top 4 vào Champion"), (TURF, "Bottom 4 vào Challenge")]),
                                    (champion_page, [(GOLD, "Vô địch Premier League")]),
                                    (challenge_page, [(RED, "Hai đội cuối xuống hạng")])):
                    legend = tk.Frame(page, bg=BG)
                    legend.pack(side="bottom", fill="x", pady=(8, 0))
                    for color, label in items:
                        self._legend_item(legend, color, label)
            else:
                subtitle = ("Hạng 1 lên thẳng · Hạng 3 vs 4 · thắng gặp hạng 2 tranh vé lên Premier" if tier == "fc1"
                            else "Hạng 1 lên thẳng · Hạng 3 vs 4 · thắng gặp hạng 2 tranh vé lên FC1")
                frame = self._new_page(tier, title, subtitle)
                seg = Segmented(frame, hint="Bảng giai đoạn 1 và nhánh play-off lên hạng")
                self.league_segments[tier] = seg
                seg.pack(fill="both", expand=True)
                standings_page = seg.add("standings", "Bảng giai đoạn 1")
                playoff_page = seg.add("playoff", "Play-off lên hạng")
                leaders_page = seg.add("leaders", "Ghi bàn & kiến tạo")
                self._build_leaderboard_pair(leaders_page, tier)
                self.league_trees[tier] = self._tree(standings_page, league_columns)
                self._bind_standings_team_click(self.league_trees[tier])
                self.playoff_texts[tier] = self._scrolled_text(playoff_page, mono=False, bg=SURFACE_2, padx=18, pady=14)
                legend = tk.Frame(standings_page, bg=BG)
                legend.pack(side="bottom", fill="x", pady=(8, 0))
                self._legend_item(legend, GOLD, "Hạng 1 lên thẳng")
                self._legend_item(legend, TURF, "Play-off: hạng 2, 3, 4")
                if tier == "fc1":
                    self._legend_item(legend, "#d9a441", "Hạng 6–7 play-off trụ hạng")
                    self._legend_item(legend, RED, "Hạng 8 xuống hạng trực tiếp")

        self.fixtures_frame = self._new_page("fixtures", "Lịch & kết quả",
                                             "AI tự mô phỏng toàn bộ lịch đấu; nhấp đúp trận đã đá để xem báo cáo.")
        self._build_fixtures()

        self.squad_frame = self._new_page("squad", "CLB & cầu thủ", "Đội hình, học viện, danh hiệu và cầu thủ huyền thoại của từng CLB")
        self._build_squad()

        self.cup_frame = self._new_page("cup", "Major Cup", "Nhánh đấu từ vòng 1 đến chung kết")
        seg = Segmented(self.cup_frame, hint="Chọn trận đã đá để xem báo cáo.")
        self.cup_segmented = seg
        seg.pack(fill="both", expand=True)
        self.cup_bracket_frame = seg.add("bracket", "Sơ đồ nhánh đấu")
        self.cup_results_frame = seg.add("results", "Danh sách trận")
        self.cup_leaders_frame = seg.add("leaders", "Ghi bàn & kiến tạo")
        self.cup_canvas = self._make_canvas(self.cup_bracket_frame)
        self.cup_tree = self._tree(self.cup_results_frame, [("round","Vòng",130),("home","Chủ nhà",220),("score","Tỷ số",100),
                                                            ("away","Đội khách",220),("winner","Đi tiếp",220),("date","Ngày",105)])
        self.cup_tree.bind("<ButtonRelease-1>", self.on_cup_match_click)
        self._build_leaderboard_pair(self.cup_leaders_frame, "Cup")

        self.c1_frame = self._new_page("c1", "Champions League", "Play-in loại kép, hai bảng vòng tròn và vòng knock-out")
        seg = Segmented(self.c1_frame, hint="Trận thuộc slot kế tiếp mở Live Match · trận đã đá có dữ liệu thì phát lại sân đấu (chỉ đọc).")
        self.c1_segmented = seg
        seg.pack(fill="both", expand=True)
        self.c1_playin_frame = seg.add("playin", "Play-in · Nhánh kép")
        self.c1_groups_frame = seg.add("groups", "Vòng bảng · BXH")
        self.c1_knockout_frame = seg.add("knockout", "Bán kết & chung kết")
        self.c1_detail_frame = seg.add("detail", "Chi tiết giải")
        self.c1_leaders_frame = seg.add("leaders", "Ghi bàn & kiến tạo")
        self._build_leaderboard_pair(self.c1_leaders_frame, "C1")
        self.c1_playin_canvas = self._make_canvas(self.c1_playin_frame)

        # Vòng bảng C1: bảng xếp hạng ở trái, toàn bộ lịch/kết quả ở bên phải.
        groups_split = tk.Frame(self.c1_groups_frame, bg=BG)
        groups_split.pack(fill="both", expand=True)
        groups_split.columnconfigure(0, weight=3, uniform="c1groups")
        groups_split.columnconfigure(1, weight=2, uniform="c1groups")
        groups_split.rowconfigure(0, weight=1)
        groups_standings_host = tk.Frame(groups_split, bg=BG)
        groups_standings_host.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        groups_fixtures_host = tk.Frame(groups_split, bg=BG)
        groups_fixtures_host.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        self.c1_groups_canvas = self._make_canvas(groups_standings_host)
        fixtures_card, fixtures_body = self._section_card(
            groups_fixtures_host, "Lịch & kết quả vòng bảng C1",
            "Nhấp trận thuộc slot kế tiếp để mở Live Match; trận đã đá mở phát lại"
        )
        fixtures_card.pack(fill="both", expand=True)
        self.c1_group_fixture_tree = self._tree(fixtures_body, [
            ("date", "Ngày", 82), ("group", "Bảng", 48), ("round", "Vòng", 52),
            ("home", "Chủ nhà", 118), ("score", "Tỷ số", 60),
            ("away", "Đội khách", 118), ("status", "Trạng thái", 76),
        ], height=18)
        self.c1_group_fixture_matches = {}
        self.c1_group_fixture_tree.tag_configure("c1_played", foreground=TURF_LIGHT)
        self.c1_group_fixture_tree.tag_configure("c1_pending", foreground=TEXT)
        self.c1_group_fixture_tree.bind("<ButtonRelease-1>", self._on_c1_group_fixture_click)

        self.c1_knockout_canvas = self._make_canvas(self.c1_knockout_frame)
        self.c1_canvas = self.c1_playin_canvas
        self.c1_text = self._scrolled_text(self.c1_detail_frame, mono=True, bg=SURFACE_2, padx=18, pady=14)

        self.ranking_frame = self._new_page("ranking", "Điểm xếp hạng Champions League", "Điểm League theo thứ hạng + thưởng thắng play-off lên hạng; điểm Cup được cộng riêng")
        legend = tk.Frame(self.ranking_frame, bg=BG)
        legend.pack(side="bottom", fill="x", pady=(10, 0))
        self._legend_item(legend, GOLD, "Top 4: vào thẳng vòng bảng")
        self._legend_item(legend, TURF, "Hạng 5 đến 12: đá play-in")
        self.ranking_tree = self._tree(self.ranking_frame, [("rank","Hạng",60),("team","CLB",230),("tier","Hạng đấu",180),
            ("league_points","Điểm League",110),("cup_points","Điểm Cup",100),("total","Tổng điểm",100),("gd","HS mùa",80),("elo","Elo",85)])

        self.history_frame = self._new_page("history", "Lịch sử mùa", "Lịch sử các mùa, thống kê danh hiệu CLB và những cầu thủ huyền thoại trong toàn bộ career")
        self.history_tabs = Segmented(self.history_frame, hint="Dữ liệu được tổng hợp từ các trận đã lưu trong career")
        self.history_tabs.pack(fill="both", expand=True)
        self.history_list_frame = self.history_tabs.add("seasons", "Lịch sử mùa")
        self.history_analytics_frame = self.history_tabs.add("analytics", "Thống kê career")
        self.history_hall_frame = self.history_tabs.add("hall", "Hall of Fame")
        self.history_records_frame = self.history_tabs.add("records", "Kỷ lục")
        self.history_tree = self._tree(self.history_list_frame, [("season","Mùa",90),("premier","VĐ Premier",220),("cup","VĐ Cup",220),
            ("c1","VĐ C1",220),("scorer","Vua phá lưới",240),("matches","Số trận",90)])
        self.history_tree.bind("<Double-1>", self.on_history_detail)
        self._build_history_analytics()
        self._build_hall_of_fame()
        self._build_career_records()

        self.dev_frame = self._new_page("dev", "Phát triển cầu thủ", "Mô hình phát triển OVR và từng thuộc tính sau mùa giải")
        self.dev_tree = self._tree(self.dev_frame, [("player","Cầu thủ",210),("team","CLB",180),("pos","VT",55),("old","OVR cũ",70),
            ("new","OVR mới",70),("delta","Δ",50),("age","Tuổi",55),("pot","Potential",75),("mins","Phút",65),
            ("injury","Vắng CT",70),("attrs","Đổi thuộc tính",100)])

        self.intelligence_frame = self._new_page(
            "intelligence", "Dự báo & xác suất giải đấu",
            "Monte Carlo read-only · dự báo theo lịch còn lại, Elo và kết quả đã lưu"
        )
        self._build_intelligence_page()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show_page("dashboard")

    def _build_intelligence_page(self):
        """Read-only league projections; running them never simulates real fixtures."""
        root = self.intelligence_frame
        toolbar = tk.Frame(root, bg=BG)
        toolbar.pack(fill="x", pady=(0, 10))
        self.intel_runs_var = tk.StringVar(value="1200")
        tk.Label(toolbar, text="Mô phỏng thống kê", font=F(9), bg=BG, fg=MUTED).pack(side="left")
        combo = ttk.Combobox(toolbar, textvariable=self.intel_runs_var,
                             values=("500", "1200", "2500", "5000"),
                             state="readonly", width=9)
        combo.pack(side="left", padx=(8, 14))
        self.intel_run_button = FlatButton(toolbar, "Tính dự báo Monte Carlo",
                                           self.refresh_intelligence, kind="primary")
        self.intel_run_button.pack(side="left")
        self.intel_status_var = tk.StringVar(
            value="Chưa chạy dự báo. Nhấn nút để tính xác suất; thao tác này không thay đổi save hoặc tiến độ mùa."
        )
        tk.Label(root, textvariable=self.intel_status_var, font=F(9), bg=BG, fg=MUTED,
                 anchor="w", justify="left", wraplength=1080).pack(fill="x", pady=(0, 8))
        self.intelligence_tree = self._tree(root, [
            ("team", "CLB", 205), ("tier", "Hạng", 90),
            ("champion", "Vô địch %", 85), ("direct", "Lên thẳng %", 85),
            ("playoff", "Thắng play-off %", 105), ("relegation", "Xuống hạng %", 90),
            ("c1direct", "C1 trực tiếp %", 95), ("c1playin", "C1 play-in %", 95),
            ("rank", "Hạng C1 TB", 85), ("secure", "Suất chắc chắn", 210),
        ], height=22)
        self.intelligence_tree.configure(selectmode="none")
        note = (
            "Cách đọc: các tỷ lệ là số lần một kịch bản kết thúc ở trạng thái tương ứng chia cho tổng số lần thử. "
            "Suất chắc chắn chỉ hiển thị khi điều kiện điểm tối đa/tối thiểu chứng minh được về mặt toán học. "
            "Điểm C1 hiện tính điểm Cup đã đạt, không dự đoán các vòng Cup chưa đá. Kết quả là tham khảo, không phải kết quả thật."
        )
        tk.Label(root, text=note, font=F(8), bg=BG, fg=DIM, wraplength=1080,
                 justify="left", anchor="w").pack(fill="x", pady=(8, 0))

    def refresh_intelligence(self):
        try:
            simulations = int(self.intel_runs_var.get())
            self.intel_run_button.set_enabled(False)
            self.intel_status_var.set(f"Đang chạy {simulations:,} kịch bản độc lập…")
            self.root.update_idletasks()
            forecast = forecast_season(self.game, simulations=simulations)
            rows = forecast.get("teams", [])
            self._set_tree(self.intelligence_tree, rows, lambda _i, row: (
                row.get("team", "—"), TIER_LABELS.get(row.get("tier"), row.get("tier", "—")),
                f"{row.get('champion_probability', 0.0):.1f}%",
                f"{row.get('direct_promotion_probability', 0.0):.1f}%",
                f"{row.get('playoff_promotion_probability', 0.0):.1f}%",
                f"{row.get('relegation_probability', 0.0):.1f}%",
                f"{row.get('c1_direct_probability', 0.0):.1f}%",
                f"{row.get('c1_playin_probability', 0.0):.1f}%",
                f"{row.get('average_c1_rank', 0.0):.2f}", row.get("clinching", "—")))
            self.intel_status_var.set(
                f"Mùa {forecast.get('season')} · {forecast.get('simulations'):,} kịch bản · "
                f"seed {forecast.get('seed')} · hoàn tất; save và lịch thi đấu thực tế không thay đổi. "
                f"{forecast.get('note', '')}"
            )
        except Exception as exc:
            self.intel_status_var.set(f"Không thể tính dự báo: {exc}")
            try:
                self.game.log_gui_error("Monte Carlo forecast", exc)
            except Exception:
                pass
        finally:
            self.intel_run_button.set_enabled(True)

    def _build_sidebar(self, shell):
        side = tk.Frame(shell, bg=SIDEBAR, width=276)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(shell, bg=LINE, width=1).pack(side="left", fill="y")

        # Logo: vòng tròn giữa sân và vạch giữa sân
        brand = tk.Frame(side, bg=SIDEBAR)
        brand.pack(fill="x", padx=20, pady=(22, 14))
        logo = tk.Canvas(brand, width=36, height=36, bg=SIDEBAR, highlightthickness=0)
        logo.pack(side="left")
        logo.create_oval(3, 3, 33, 33, outline=GOLD, width=2)
        logo.create_line(18, 3, 18, 33, fill=GOLD, width=2)
        logo.create_oval(14, 14, 22, 22, fill=GOLD, outline=GOLD)
        words = tk.Frame(brand, bg=SIDEBAR)
        words.pack(side="left", padx=(12, 0))
        tk.Label(words, text="Super League", font=F(14, "bold"), bg=SIDEBAR, fg=TEXT).pack(anchor="w")
        tk.Label(words, text="Football Simulator", font=F(9), bg=SIDEBAR, fg=MUTED).pack(anchor="w")

        # Chân thanh bên chỉ hiển thị trạng thái; lưu career do engine tự động thực hiện.
        foot = tk.Frame(side, bg=SIDEBAR)
        foot.pack(side="bottom", fill="x", padx=12, pady=(0, 16))
        tk.Frame(foot, bg=LINE, height=1).pack(fill="x", pady=(0, 10))
        tk.Label(foot, text="CHẾ ĐỘ KHÁN GIẢ", font=F(9, "bold"), bg=SIDEBAR, fg=TURF_LIGHT).pack(anchor="w")
        tk.Label(foot, text="Career tự lưu · AI tự quản lý", font=F(8), bg=SIDEBAR, fg=MUTED).pack(anchor="w", pady=(4, 0))
        initial_sheets_status = self.game.google_sheets_status()
        status_text = {
            "synced": "Google Sheets: Đã đồng bộ",
            "queued": "Google Sheets: Đang chờ đồng bộ",
            "retrying": "Google Sheets: Đang thử lại",
            "setup_required": "Google Sheets: Cần thiết lập",
            "snapshot_error": "Google Sheets: Lỗi tạo ảnh",
            "error": "Google Sheets: Lỗi đồng bộ",
            "disabled": "Google Sheets: Đang tắt",
        }.get(initial_sheets_status.get("status"), "Google Sheets: Chưa đồng bộ")
        self.google_sheets_status_var = tk.StringVar(value=status_text)
        tk.Label(foot, textvariable=self.google_sheets_status_var, font=F(8), bg=SIDEBAR, fg=MUTED,
                 anchor="w", padx=10, wraplength=225, justify="left").pack(fill="x", pady=(5, 0))
        tk.Label(foot, text="Career vẫn lưu cục bộ khi mất mạng", font=F(8), bg=SIDEBAR, fg=DIM,
                 anchor="w", padx=10).pack(fill="x", pady=(3, 0))

        groups = [
            (None, [("dashboard", "Tổng quan")]),
            ("Liên đoàn", [
                ("premier", "Premier League"),
                ("fc1", "Football Championship 1"),
                ("fc2", "Football Championship 2"),
            ]),
            ("Trận đấu", [("fixtures", "Lịch & kết quả")]),
            ("Cúp", [
                ("cup", "Major Cup"),
                ("c1", "Champions League"),
                ("ranking", "Điểm xếp hạng Champions League"),
            ]),
            ("Dữ liệu", [
                ("squad", "CLB & cầu thủ"),
                ("history", "Lịch sử mùa"),
                ("dev", "Phát triển cầu thủ"),
            ]),
            ("Phân tích", [
                ("intelligence", "Dự báo & xác suất"),
            ]),
        ]
        highlighted_sections = {"Trận đấu", "Cúp", "Dữ liệu"}
        for title, items in groups:
            if title in highlighted_sections:
                section = tk.Frame(side, bg="#121c26", height=26,
                                   highlightthickness=1, highlightbackground=LINE)
                section.pack(fill="x", padx=12, pady=(12, 4))
                tk.Frame(section, bg=GOLD, width=3).pack(side="left", fill="y")
                tk.Label(section, text=title.upper(), font=F(9, "bold"), bg="#121c26",
                         fg=GOLD, anchor="w", padx=9, pady=4).pack(side="left", fill="x", expand=True)
            elif title:
                tk.Label(side, text=title, font=F(9), bg=SIDEBAR, fg=DIM, anchor="w",
                         padx=20).pack(fill="x", pady=(12, 2))
            for key, text in items:
                item = NavItem(side, text, lambda k=key: self.show_page(k))
                item.pack(fill="x")
                self.nav_items[key] = item

    def _build_matchday_bar(self, parent):
        bar = make_card(parent)
        self.matchday_bar = bar
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=SURFACE)
        inner.pack(fill="x", padx=18, pady=14)
        inner.columnconfigure(0, weight=1)

        # Hàng 1: mùa giải + slot kế tiếp (trái), nút mô phỏng (phải)
        left = tk.Frame(inner, bg=SURFACE)
        left.grid(row=0, column=0, sticky="ew")
        self.season_var = tk.StringVar(value="Mùa —")
        self.next_var = tk.StringVar(value="")
        tk.Label(left, textvariable=self.season_var, font=F(14, "bold"), bg=SURFACE, fg=TEXT).pack(anchor="w")
        tk.Label(left, textvariable=self.next_var, font=F(9), bg=SURFACE, fg=MUTED,
                 justify="left", anchor="w", wraplength=340).pack(anchor="w", pady=(2, 0))
        buttons = tk.Frame(inner, bg=SURFACE)
        buttons.grid(row=0, column=1, sticky="e")
        self.btn_next = FlatButton(buttons, "▶  Mô phỏng trận còn lại", self.on_next, kind="primary")
        self.btn_next.pack(side="left")
        self.btn_full_season = FlatButton(
            buttons, "⏩ Chạy hết mùa", self.on_full_season, kind="secondary", padx=12
        )
        self.btn_full_season.pack(side="left", padx=(8, 0))
        self.btn_next_season = FlatButton(buttons, "Mùa tiếp theo", self.on_next_season)
        self.btn_next_season.pack(side="left", padx=(8, 0))

        # Hàng 2: thanh tiến độ chạy suốt chiều ngang
        prog = tk.Frame(inner, bg=SURFACE)
        prog.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        line = tk.Frame(prog, bg=SURFACE)
        line.pack(fill="x")
        tk.Label(line, text="Tiến độ mùa", font=F(9), bg=SURFACE, fg=MUTED).pack(side="left")
        self.progress_var = tk.StringVar(value="")
        tk.Label(line, textvariable=self.progress_var, font=F(9, "bold"), bg=SURFACE, fg=TEXT).pack(side="right")
        self.progress = ttk.Progressbar(prog, style="Season.Horizontal.TProgressbar", mode="determinate", maximum=1, value=0)
        self.progress.pack(fill="x", pady=(6, 0))

    def _new_page(self, key, title, subtitle):
        frame = tk.Frame(self.page_host, bg=BG)
        self.pages[key] = (frame, title, subtitle)
        return frame

    def _bind_standings_team_click(self, tree):
        """Make the CLB column in standings tables open that club's profile."""
        tree.bind("<ButtonRelease-1>",
                  lambda event, widget=tree: self._open_club_from_standings(widget, event), add="+")

    def _open_club_from_standings(self, tree, event):
        if tree.identify_column(event.x) != "#2":
            return
        item_id = tree.identify_row(event.y)
        if not item_id:
            return
        values = tree.item(item_id, "values")
        if len(values) < 2:
            return
        team = str(values[1]).strip()
        if team not in self.game.s.get("rosters", {}):
            return

        # Remember exactly which standings page opened the club profile.  The
        # league/dashboard segmented controls keep their current selection, so
        # returning to this page also returns the user to the same BXH view.
        self._club_back_context = {"page": getattr(self, "current_page", "dashboard")}
        self._update_club_back_button()
        self.squad_team.set(team)
        self.show_page("squad")
        self.toast(f"Đã mở hồ sơ CLB {team}.")

    def _update_club_back_button(self):
        button = getattr(self, "club_back_button", None)
        if button is None:
            return
        context = getattr(self, "_club_back_context", None)
        if context and context.get("page") in self.pages:
            if not button.winfo_manager():
                button.pack(side="left", padx=(0, 12), pady=(16, 0))
        elif button.winfo_manager():
            button.pack_forget()

    def _back_from_club_profile(self):
        context = getattr(self, "_club_back_context", None)
        target = context.get("page") if context else None
        self._club_back_context = None
        self._update_club_back_button()
        if target in self.pages:
            self.show_page(target)

    def _make_loading_overlay(self, parent, message):
        """Overlay nhẹ để người dùng thấy màn hình đang nạp dữ liệu thay vì trống."""
        overlay = tk.Frame(parent, bg="#0a1118", highlightthickness=1, highlightbackground=LINE)
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        card = make_card(overlay, SURFACE)
        card.place(relx=0.5, rely=0.5, anchor="center", width=300, height=112)
        tk.Label(card, text="◌  ĐANG TẢI DỮ LIỆU", font=F(10, "bold"),
                 bg=SURFACE, fg=GOLD).pack(pady=(17, 5))
        tk.Label(card, text=message, font=F(9), bg=SURFACE, fg=MUTED).pack(pady=(0, 8))
        bar = ttk.Progressbar(card, mode="indeterminate", length=230, style="Loading.Horizontal.TProgressbar")
        bar.pack(pady=(0, 13))
        bar.start(12)
        overlay.lift()
        return overlay, bar

    def _refresh_page_for_display(self, key):
        """Refresh only the selected view while its loading overlay is visible."""
        if key in ("dashboard", "premier", "fc1", "fc2"):
            self.refresh_all()
        elif key == "fixtures":
            self.refresh_fixtures()
        elif key == "squad":
            self.refresh_squad()
        elif key == "cup":
            self.refresh_cup()
            self.refresh_competition_leaderboards()
        elif key == "c1":
            self.refresh_c1()
            self.refresh_competition_leaderboards()
        elif key == "ranking":
            self.refresh_ranking()
        elif key == "history":
            self.refresh_history()
        elif key == "dev":
            self.refresh_development()
        elif key == "intelligence":
            # Forecasting is opt-in: opening the page does not launch a costly
            # Monte Carlo run. The page stays read-only until the user clicks.
            self.intel_status_var.set(
                "Sẵn sàng tính dự báo. Chọn số kịch bản rồi nhấn nút; save không bị thay đổi."
            )

    def _finish_page_load(self, key, token, overlay, progress):
        try:
            if token != getattr(self, "_page_load_token", 0) or self.current_page != key:
                return
            self._refresh_page_for_display(key)
        except Exception:
            self._write_gui_log(f"Page refresh failed: {key}", traceback.format_exc())
            self.toast("Không tải được dữ liệu màn hình. Xem log Super League trong thư mục tạm của hệ điều hành.", "warn")
        finally:
            try:
                progress.stop()
                if overlay.winfo_exists():
                    overlay.destroy()
            except tk.TclError:
                pass

    def show_page(self, key, loading=True):
        if key not in self.pages:
            return
        self._page_load_token = getattr(self, "_page_load_token", 0) + 1
        token = self._page_load_token
        for frame, _title, _sub in self.pages.values():
            frame.pack_forget()
        frame, title, subtitle = self.pages[key]
        frame.pack(fill="both", expand=True)
        self.page_title.set(title)
        self.page_sub.set(subtitle)
        for name, item in self.nav_items.items():
            item.set_active(name == key)
        # Thanh tiến trình và điều khiển mùa chỉ xuất hiện ở trang Tổng quan.
        if hasattr(self, "matchday_bar"):
            if key == "dashboard":
                if not self.matchday_bar.winfo_manager():
                    self.matchday_bar.pack(fill="x", before=self.page_host)
                if hasattr(self, "status_label") and not self.status_label.winfo_manager():
                    self.status_label.pack(side="right", pady=(8, 0))
            else:
                self.matchday_bar.pack_forget()
                if hasattr(self, "status_label"):
                    self.status_label.pack_forget()
        previous_page = getattr(self, "current_page", None)
        self.current_page = key

        # Leaving a club profile through the normal sidebar/navigation cancels
        # its temporary BXH back target. The dedicated Back button captures its
        # target before calling show_page(), so this does not break Back itself.
        if previous_page == "squad" and key != "squad" and getattr(self, "_club_back_context", None):
            self._club_back_context = None
            self._update_club_back_button()

        # Startup builds pages while the root is withdrawn; _initial_refresh handles that pass.
        if loading and self.root.winfo_viewable():
            labels = {
                "dashboard": "Đang tổng hợp mùa giải và bảng xếp hạng…",
                "premier": "Đang tải dữ liệu Premier League…",
                "fc1": "Đang tải dữ liệu FC1…", "fc2": "Đang tải dữ liệu FC2…",
                "fixtures": "Đang tải lịch và kết quả thi đấu…",
                "squad": "Đang tải đội hình và tình trạng cầu thủ…",
                "cup": "Đang tải dữ liệu Major Cup…", "c1": "Đang tải dữ liệu Champions League…",
                "ranking": "Đang tải bảng điểm liên đoàn…", "history": "Đang tải lịch sử mùa giải…",
                "dev": "Đang tải dữ liệu phát triển cầu thủ…",
            }
            overlay, progress = self._make_loading_overlay(frame, labels.get(key, "Đang tải dữ liệu…"))
            self.root.after(35, lambda k=key, t=token, o=overlay, p=progress:
                            self._finish_page_load(k, t, o, p))

    # ── Thành phần dùng chung ───────────────────────────────────────────────

    def _legend_item(self, parent, color, text):
        tk.Frame(parent, bg=color, width=10, height=10).pack(side="left", padx=(0, 6))
        tk.Label(parent, text=text, font=F(9), bg=BG, fg=MUTED).pack(side="left", padx=(0, 18))

    def _section_card(self, parent, title, hint=""):
        card = make_card(parent)
        head = tk.Frame(card, bg=SURFACE)
        head.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(head, text=title, font=F(11, "bold"), bg=SURFACE, fg=TEXT).pack(side="left")
        if hint:
            tk.Label(head, text=hint, font=F(9), bg=SURFACE, fg=MUTED).pack(side="left", padx=10)
        body = tk.Frame(card, bg=SURFACE)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        return card, body

    def _caption(self, parent, title, hint=""):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(0, 6))
        tk.Label(row, text=title, font=F(11, "bold"), bg=BG, fg=TEXT).pack(side="left")
        if hint:
            tk.Label(row, text=hint, font=F(9), bg=BG, fg=MUTED).pack(side="left", padx=10)

    def _make_text(self, parent, mono=False, bg=SURFACE, size=10, padx=0, pady=0, height=None, border=False):
        kwargs = {"height": height} if height else {}
        txt = tk.Text(parent, wrap="word", bg=bg, fg=TEXT, insertbackground=TEXT, relief="flat", bd=0,
                      highlightthickness=0, font=F(size, mono=mono), padx=padx, pady=pady,
                      spacing1=2, spacing3=2, selectbackground=SELECT, selectforeground="#ffffff",
                      cursor="arrow", **kwargs)
        txt.tag_configure("h", font=F(size, "bold", mono), foreground=GOLD, spacing1=8)
        txt.configure(state="disabled")
        return txt

    def _scrolled_text(self, parent, mono=False, bg=SURFACE_2, size=10, padx=0, pady=0, border=True):
        wrap = tk.Frame(parent, bg=bg, highlightthickness=1 if border else 0, highlightbackground=LINE, highlightcolor=LINE)
        wrap.pack(fill="both", expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        txt = self._make_text(wrap, mono=mono, bg=bg, size=size, padx=padx, pady=pady)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=autohide(bar))
        txt.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        return txt

    def _make_canvas(self, parent):
        wrap = tk.Frame(parent, bg=SURFACE_2, highlightthickness=1, highlightbackground=LINE, highlightcolor=LINE)
        wrap.pack(fill="both", expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        canvas = tk.Canvas(wrap, bg=SURFACE_2, highlightthickness=0)
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        hbar = ttk.Scrollbar(wrap, orient="horizontal", command=canvas.xview)
        canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        canvas.configure(yscrollcommand=autohide(vbar), xscrollcommand=autohide(hbar))
        self._bind_wheel(canvas)
        return canvas

    @staticmethod
    def _bind_wheel(canvas):
        """Cuộn canvas bằng con lăn chuột (giữ Shift để cuộn ngang)."""
        def on_wheel(event):
            if getattr(event, "num", None) == 4:
                step = -1
            elif getattr(event, "num", None) == 5:
                step = 1
            elif event.delta:
                step = -1 if event.delta > 0 else 1
            else:
                return
            if event.state & 0x0001:
                canvas.xview_scroll(step, "units")
            else:
                canvas.yview_scroll(step, "units")

        def on_enter(_event):
            canvas.bind_all("<MouseWheel>", on_wheel)
            canvas.bind_all("<Button-4>", on_wheel)
            canvas.bind_all("<Button-5>", on_wheel)

        def on_leave(_event):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", on_enter)
        canvas.bind("<Leave>", on_leave)

    def _tree(self, parent, columns, height=None):
        wrap = tk.Frame(parent, bg=SURFACE_2, highlightthickness=1, highlightbackground=LINE, highlightcolor=LINE)
        wrap.pack(fill="both", expand=True)
        names = [c[0] for c in columns]
        kwargs = {"height": height} if height else {}
        tree = ttk.Treeview(wrap, columns=names, show="headings", selectmode="browse", **kwargs)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        hs = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=autohide(vs), xscrollcommand=autohide(hs))
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        left_cols = ("team", "home", "away", "winner", "player", "premier", "cup", "c1", "scorer", "tier",
                     "name", "label", "comp", "round")
        for key, title, width in columns:
            anchor = "w" if key in left_cols else "center"
            tree.heading(key, text=title, anchor=anchor)
            tree.column(key, width=width, minwidth=45, anchor=anchor, stretch=True)
        # Mỗi dòng chỉ gắn một tag: màu nền sọc + màu theo ý nghĩa (top / promote / relegate / pending).
        meaning = {"top": (GOLD, GOLD_BG), "promote": ("#c4f0d6", TURF_BG),
                   "relegate": ("#f4c7cb", RED_BG),
                   "relegate_playoff": ("#f6d59a", "#4a3820"),
                   "pending": (MUTED, None), "next_match": (GOLD, GOLD_BG),
                   "starting_xi": (TURF_LIGHT, TURF_BG)}
        for parity, zebra in enumerate((SURFACE_2, STRIPE)):
            tree.tag_configure(f"row_{parity}", background=zebra, foreground=TEXT)
            for name, (fg, bg) in meaning.items():
                tree.tag_configure(f"{name}_{parity}", foreground=fg, background=bg or zebra)
        return tree

    def _set_tree(self, tree, rows, mapper, tagger=None, iid_of=None):
        """Replace Treeview rows, optionally assigning stable item IDs.

        ``iid_of`` is used by clickable player/transfer rows so selection can be
        resolved back to the domain object.  Keep it optional for all existing
        tables that do not need stable identifiers.
        """
        tree.delete(*tree.get_children())
        for i, row in enumerate(rows):
            vals = mapper(i, row)
            tags = tagger(i, row) if tagger else ()
            parity = i % 2
            tag = f"{tags[0]}_{parity}" if tags else f"row_{parity}"
            iid = iid_of(i, row) if iid_of else None
            insert_kwargs = {"values": vals, "tags": (tag,)}
            if iid:
                insert_kwargs["iid"] = str(iid)
            tree.insert("", "end", **insert_kwargs)

    def _set_text(self, widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.tag_remove("h", "1.0", "end")
        # Dòng tiêu đề (viết hoa, không thụt lề) được tô nổi bật.
        for number, line in enumerate(str(value).split("\n"), 1):
            if not line or line[0] in " \t=·":
                continue
            head = line.split(":")[0] if ":" in line else line
            if sum(ch.isalpha() for ch in head) >= 3 and head == head.upper():
                widget.tag_add("h", f"{number}.0", f"{number}.{len(head)}")
        widget.configure(state="disabled")

    def _set_news_feed(self, widget, items, empty_message="Chưa có trận đấu nào hoàn tất. Bản tin sẽ xuất hiện sau mỗi trận."):
        """Render editorial articles; use a neutral empty state when there is no story."""
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.tag_configure("news_title", font=F(10, "bold"), foreground=GOLD, spacing1=7, spacing3=2)
        widget.tag_configure("news_meta", font=F(8), foreground=MUTED, spacing3=4)
        widget.tag_configure("news_body", font=F(9), foreground=TEXT, lmargin1=2, lmargin2=2, spacing3=5)
        widget.tag_configure("news_divider", font=F(8), foreground=LINE_2, spacing1=3, spacing3=7)
        if not items:
            widget.insert("end", str(empty_message) + "\n", "news_body")
            widget.configure(state="disabled")
            return

        for index, item in enumerate(items):
            headline = str(item.get("headline") or "Tin trận đấu")
            date_text = str(item.get("date") or "")
            comp = str(item.get("comp") or "Giải đấu")
            round_label = str(item.get("round") or "")
            home, away = str(item.get("home") or ""), str(item.get("away") or "")
            score = str(item.get("score") or "")
            meta_bits = [bit for bit in (date_text, comp, round_label) if bit]
            if home and away and score:
                meta_bits.append(f"{home} {score} {away}")
            body = str(item.get("body") or item.get("text") or "")
            # Legacy entries may store date/competition in text and have no separate body.
            if not item.get("body") and body.startswith(date_text):
                body = body.split("\n", 1)[-1]
            widget.insert("end", headline + "\n", "news_title")
            widget.insert("end", "  ·  ".join(meta_bits) + "\n", "news_meta")
            widget.insert("end", body.strip() + "\n", "news_body")
            if index < len(items) - 1:
                widget.insert("end", "────────────────────────────────────────\n", "news_divider")
        widget.configure(state="disabled")
        widget.yview_moveto(0.0)

    def _build_leaderboard_pair(self, parent, key):
        """Hai bảng xếp hạng cá nhân: vua phá lưới và vua kiến tạo."""
        host = tk.Frame(parent, bg=BG)
        host.pack(fill="both", expand=True)
        host.columnconfigure(0, weight=1, uniform="leaders")
        host.columnconfigure(1, weight=1, uniform="leaders")
        host.rowconfigure(0, weight=1)
        columns_s = [("rank", "#", 42), ("player", "Cầu thủ", 175), ("team", "CLB", 145),
                     ("goals", "Bàn", 58), ("assists", "KT", 58), ("apps", "Trận", 58)]
        columns_a = [("rank", "#", 42), ("player", "Cầu thủ", 175), ("team", "CLB", 145),
                     ("assists", "KT", 58), ("goals", "Bàn", 58), ("apps", "Trận", 58)]
        left_card, left_body = self._section_card(host, "Vua phá lưới", "Số bàn thắng trong giải")
        right_card, right_body = self._section_card(host, "Vua kiến tạo", "Số đường kiến tạo trong giải")
        left_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        right_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        scorer_tree = self._tree(left_body, columns_s, height=15)
        assist_tree = self._tree(right_body, columns_a, height=15)
        self.leaderboard_trees[key] = {"scorers": scorer_tree, "assists": assist_tree}

    # ── Dashboard ───────────────────────────────────────────────────────────

    def _build_dashboard(self):
        root = self.dashboard
        top = tk.Frame(root, bg=BG)
        top.pack(fill="x")
        self.dash_labels = {}
        self.dash_subs = {}
        cards = [("season", "Mùa giải"), ("matches", "Trận đã đá"),
                 ("history", "Mùa đã lưu"), ("leader", "Dẫn đầu Premier League")]
        for col, (key, title) in enumerate(cards):
            top.columnconfigure(col, weight=1, uniform="stat")
            card = make_card(top)
            card.grid(row=0, column=col, sticky="nsew",
                      padx=(0 if col == 0 else 6, 0 if col == len(cards) - 1 else 6))
            inner = tk.Frame(card, bg=SURFACE)
            inner.pack(fill="both", expand=True, padx=16, pady=14)
            tk.Label(inner, text=title, font=F(9), bg=SURFACE, fg=MUTED, anchor="w").pack(fill="x")
            value, sub = tk.StringVar(value="—"), tk.StringVar(value="")
            tk.Label(inner, textvariable=value, font=F(16, "bold"), bg=SURFACE,
                     fg=TEXT, anchor="w").pack(fill="x", pady=(4, 0))
            tk.Label(inner, textvariable=sub, font=F(9), bg=SURFACE,
                     fg=MUTED, anchor="w").pack(fill="x", pady=(1, 0))
            self.dash_labels[key] = value
            self.dash_subs[key] = sub

        mid = tk.Frame(root, bg=BG)
        mid.pack(fill="both", expand=True, pady=(12, 0))
        # Keep standings permanently visible and give it more horizontal room.
        # News/results share the second column and are switched with tabs.
        mid.columnconfigure(0, weight=5, uniform="mid")
        mid.columnconfigure(1, weight=4, uniform="mid")
        mid.rowconfigure(0, weight=1)

        # Three federation leagues.
        left = tk.Frame(mid, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        table_card, table_body = self._section_card(left, "Bảng xếp hạng liên đoàn",
                                                     "Chọn Premier League, FC1 hoặc FC2; chọn tên CLB để xem hồ sơ")
        table_card.grid(row=0, column=0, sticky="nsew")
        self.dash_league_tabs = Segmented(table_body, hint="Bảng xếp hạng hiện tại")
        self.dash_league_tabs.pack(fill="both", expand=True)
        self.dash_league_trees = {}
        dashboard_columns = [("pos", "#", 32), ("team", "CLB", 135),
                             ("p", "Trận", 40), ("w", "T", 30), ("d", "H", 30),
                             ("l", "B", 30), ("gd", "HS", 42), ("pts", "Điểm", 48),
                             ("form", "5 trận", 115)]
        for key, label in (("premier", "Premier League"), ("fc1", "FC1"), ("fc2", "FC2")):
            tab_body = self.dash_league_tabs.add(key, label)
            self.dash_league_trees[key] = self._tree(tab_body, dashboard_columns, height=8)
            self._bind_standings_team_click(self.dash_league_trees[key])
        self.dash_tree = self.dash_league_trees["premier"]

        # News and weekly results now share one dashboard card. This frees the
        # third column so the standings block can be wider and easier to scan.
        info_col = tk.Frame(mid, bg=BG)
        info_col.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        info_col.rowconfigure(0, weight=1)
        info_col.columnconfigure(0, weight=1)
        info_card, info_body = self._section_card(
            info_col, "Bản tin & kết quả", "Chuyển tab để xem tin hoặc kết quả/lịch đấu"
        )
        info_card.grid(row=0, column=0, sticky="nsew")
        self.dash_info_tabs = Segmented(info_body, hint="Nội dung tuần")
        self.dash_info_tabs.pack(fill="both", expand=True)

        # Editorial news from previous + current simulation week.
        news_tab = self.dash_info_tabs.add("news", "Bản tin")
        self.dash_news = self._scrolled_text(news_tab, bg=SURFACE, size=9, border=False, padx=10, pady=8)

        talent_tab = self.dash_info_tabs.add("talents", "Tài năng tuần")
        self.dash_talent_news = self._scrolled_text(
            talent_tab, bg=SURFACE, size=9, border=False, padx=10, pady=8
        )

        # Compact results + upcoming schedule for the same two simulation weeks.
        results_tab = self.dash_info_tabs.add("results", "Kết quả & lịch đấu")
        self.dash_week_tabs = Segmented(results_tab, hint="Chuyển giữa hai tuần")
        self.dash_week_tabs.pack(fill="both", expand=True)
        self.dash_week_trees = {}
        self.dash_week_labels = {}
        week_columns = [("date", "Ngày", 48), ("comp", "Giải", 64),
                        ("match", "Trận đấu", 180), ("result", "Kết quả", 68)]
        for week_key, label in (("previous", "Tuần trước"), ("current", "Tuần này")):
            tab = self.dash_week_tabs.add(week_key, label)
            week_label = tk.StringVar(value="")
            tk.Label(tab, textvariable=week_label, font=F(8), bg=BG, fg=MUTED,
                     anchor="w").pack(fill="x", pady=(0, 5))
            self.dash_week_labels[week_key] = week_label
            tree = self._tree(tab, week_columns)
            self.dash_week_trees[week_key] = tree
            tree.tag_configure("week_played_0", foreground=TURF_LIGHT, background=SURFACE_2)
            tree.tag_configure("week_played_1", foreground=TURF_LIGHT, background=STRIPE)
            tree.tag_configure("week_pending_0", foreground=TEXT, background=SURFACE_2)
            tree.tag_configure("week_pending_1", foreground=TEXT, background=STRIPE)

        tk.Label(root, text="Bản tin, kết quả và lịch tuần được lọc theo ngày mô phỏng; danh mục cầu thủ gốc được đọc từ SQLite, không bị ghi đè.",
                 font=F(9), bg=BG, fg=DIM, anchor="w").pack(fill="x", pady=(8, 0))

    # ── Lịch & kết quả ──────────────────────────────────────────────────────

    def _build_fixtures(self):
        root = self.fixtures_frame
        bar = make_card(root)
        bar.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(bar, bg=SURFACE)
        inner.pack(fill="x", padx=14, pady=10)

        self.filter_comp = tk.StringVar(value="Tất cả")
        self.filter_team = tk.StringVar(value="Tất cả")
        self.filter_venue = tk.StringVar(value="Tất cả")
        self.filter_period = tk.StringVar(value="Toàn bộ lịch")
        self.filter_status = tk.StringVar(value="Tất cả")
        comps = ["Tất cả", "Premier League", "Football Championship 1", "Football Championship 2", "Cup", "C1 Play-in", "C1"]
        fields = [
            ("Giải", self.filter_comp, comps, 22),
            ("CLB", self.filter_team, ["Tất cả"] + sorted(self.game.s["rosters"]), 20),
            ("Sân của CLB", self.filter_venue, ["Tất cả", "Sân nhà", "Sân khách"], 10),
            ("Khoảng thời gian", self.filter_period, ["Toàn bộ lịch", "Tuần mô phỏng", "Tháng mô phỏng", "7 ngày tới"], 14),
            ("Trạng thái", self.filter_status, ["Tất cả", "Chưa đá", "Đã đá"], 10),
        ]
        for col, (label, var, values, width) in enumerate(fields):
            inner.columnconfigure(col, weight=1)
            cell = tk.Frame(inner, bg=SURFACE)
            cell.grid(row=0, column=col, sticky="ew", padx=(0, 9))
            tk.Label(cell, text=label, font=F(9), bg=SURFACE, fg=MUTED).pack(anchor="w")
            combo = ttk.Combobox(cell, textvariable=var, values=values, state="readonly", width=width)
            combo.pack(fill="x", pady=(3, 0))
            combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_fixtures())
        FlatButton(inner, "Xóa lọc", self.reset_fixture_filters, kind="secondary", pady=6).grid(row=0, column=len(fields), sticky="s")

        self.fixtures_views = Segmented(root, hint="Calendar tháng giúp xem lịch theo ngày; Danh sách dùng để lọc và tra cứu chi tiết.")
        self.fixtures_views.pack(fill="both", expand=True)
        calendar_page = self.fixtures_views.add("calendar", "Calendar tháng")
        list_page = self.fixtures_views.add("list", "Danh sách trận")

        calendar_toolbar = tk.Frame(calendar_page, bg=BG)
        calendar_toolbar.pack(fill="x", pady=(0, 8))
        FlatButton(calendar_toolbar, "‹", lambda: self._move_calendar_month(-1), kind="secondary", padx=12, pady=5).pack(side="left")
        self.calendar_month_label = tk.StringVar(value="")
        tk.Label(calendar_toolbar, textvariable=self.calendar_month_label, font=F(13, "bold"), bg=BG, fg=TEXT).pack(side="left", padx=14)
        FlatButton(calendar_toolbar, "›", lambda: self._move_calendar_month(1), kind="secondary", padx=12, pady=5).pack(side="left")
        FlatButton(calendar_toolbar, "Tháng có trận kế tiếp", self._go_calendar_next_slot, kind="ghost", padx=10, pady=5).pack(side="right")
        tk.Label(calendar_page, text="Chọn trận thuộc slot kế tiếp để mở Live Match · Chọn trận đã đá để phát lại diễn biến đã lưu hoặc xem báo cáo",
                 font=F(9), bg=BG, fg=MUTED, anchor="w").pack(fill="x", pady=(0, 6))
        self.calendar_grid = tk.Frame(calendar_page, bg=BG)
        self.calendar_grid.pack(fill="both", expand=True)
        self.calendar_grid.columnconfigure(tuple(range(7)), weight=1, uniform="calendar_day")
        self.calendar_day_cells = []
        for col, weekday in enumerate(("Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật")):
            tk.Label(self.calendar_grid, text=weekday, font=F(9, "bold"), bg=SURFACE, fg=MUTED,
                     pady=7, highlightthickness=1, highlightbackground=LINE).grid(row=0, column=col, sticky="nsew", padx=1, pady=1)
        for week in range(6):
            self.calendar_grid.rowconfigure(week + 1, weight=1, uniform="calendar_week")
            for day_index in range(7):
                cell = tk.Frame(self.calendar_grid, bg=SURFACE_2, highlightthickness=1, highlightbackground=LINE)
                cell.grid(row=week + 1, column=day_index, sticky="nsew", padx=1, pady=1)
                cell.pack_propagate(False)
                date_label = tk.Label(cell, text="", font=F(9, "bold"), bg=SURFACE_2, fg=TEXT, anchor="w")
                date_label.pack(fill="x", padx=6, pady=(3, 1))
                events = tk.Frame(cell, bg=SURFACE_2)
                events.pack(fill="both", expand=True, padx=3, pady=(0, 3))
                self.calendar_day_cells.append({"frame": cell, "date": date_label, "events": events})
        self.calendar_month = self._initial_calendar_month()

        split = ttk.Panedwindow(list_page, orient="vertical")
        split.pack(fill="both", expand=True)
        done_frame = tk.Frame(split, bg=BG)
        pending_frame = tk.Frame(split, bg=BG)
        split.add(done_frame, weight=3)
        split.add(pending_frame, weight=1)
        self._caption(done_frame, "Lịch & kết quả", "Nhấp trận chưa đá thuộc slot kế tiếp để mở trình mô phỏng · Nhấp trận đã đá để phát lại diễn biến đã lưu")
        self.fixture_tree = self._tree(done_frame, [("date","Ngày",92),("comp","Giải",150),("round","Vòng",118),("home","Chủ nhà",165),
            ("score","Tỷ số",78),("red","Thẻ đỏ H–K",92),("away","Đội khách",165),("winner","Trạng thái",140)])
        self._caption(pending_frame, "Lịch slot sắp tới")
        self.slot_tree = self._tree(pending_frame, [("date","Ngày",100),("label","Slot",300),("kind","Mã slot",150),("status","Trạng thái",120)])
        self.fixture_rows_by_iid = {}
        self.fixture_tree.bind("<ButtonRelease-1>", self.on_fixture_click)
        self.fixture_tree.bind("<Double-1>", self.on_match_detail)

    def _initial_calendar_month(self):
        try:
            focus = self.game.next_slot()
            target = date.fromisoformat(focus["date"]) if focus else self._fixture_reference_date()
        except Exception:
            target = date.today()
        return date(target.year, target.month, 1)

    def _move_calendar_month(self, delta):
        current = getattr(self, "calendar_month", date.today().replace(day=1))
        month_index = current.year * 12 + current.month - 1 + int(delta)
        year, month_zero = divmod(month_index, 12)
        self.calendar_month = date(year, month_zero + 1, 1)
        self._refresh_fixture_calendar(getattr(self, "_last_fixture_rows", []))

    def _go_calendar_next_slot(self):
        slot = self.game.next_slot()
        if slot:
            target = date.fromisoformat(slot["date"])
            self.calendar_month = date(target.year, target.month, 1)
            self._refresh_fixture_calendar(getattr(self, "_last_fixture_rows", []))
            self.fixtures_views.select("calendar")

    @staticmethod
    def _calendar_comp_short(comp):
        return {"Premier League": "PREM", "Football Championship 1": "FC1",
                "Football Championship 2": "FC2", "Liên đoàn": "LEAGUE", "C1 Play-in": "C1 PI", "C1": "C1", "Cup": "CUP"}.get(comp, str(comp)[:7].upper())

    def _calendar_click_row(self, row):
        if row.get("_calendar_marker"):
            self.toast(row.get("round", "Lịch thi đấu"), "info")
            return
        if row.get("_played"):
            self._show_match_report(row)
        elif row.get("_eligible"):
            self._show_fixture_simulation_options(row)
        else:
            self.toast("Trận này chưa đến slot kế tiếp. Hãy chủ động hoàn tất các slot trước để mở Live Match.", "info")

    def _refresh_fixture_calendar(self, rows):
        """Render a compact month calendar grouped by competition/day.

        Dense league matchdays are summarized by competition so the day cells
        remain readable; clicking a day opens the complete fixture list.
        """
        if not hasattr(self, "calendar_day_cells"):
            return
        month = getattr(self, "calendar_month", self._initial_calendar_month())
        month_names = ("Tháng 1", "Tháng 2", "Tháng 3", "Tháng 4", "Tháng 5", "Tháng 6",
                       "Tháng 7", "Tháng 8", "Tháng 9", "Tháng 10", "Tháng 11", "Tháng 12")
        self.calendar_month_label.set(f"{month_names[month.month - 1]} {month.year}")
        start = month - timedelta(days=month.weekday())
        month_rows = [row for row in rows if str(row.get("date", "")).startswith(f"{month.year:04d}-{month.month:02d}-")]
        next_slot = self.game.next_slot()
        preparation_date = str((next_slot or {}).get("date", ""))
        for slot in self.game.s.get("slots", []):
            if slot.get("played") or slot.get("id") == (next_slot or {}).get("id"):
                continue
            if not str(slot.get("date", "")).startswith(f"{month.year:04d}-{month.month:02d}-"):
                continue
            kind = str(slot.get("kind", ""))
            if kind == "league":
                continue  # Regular league fixtures are projected individually below.
            if kind == "league_phase2":
                # Before phase one is complete, opponents are unknown. Show a
                # separate marker on the correct weekday for each league tier.
                if not self.game.s.get("league_phase2", {}).get("initialized"):
                    tier = slot.get("phase2_tier")
                    comp = TIER_LABELS.get(tier, "Liên đoàn")
                    if self.filter_comp.get() not in ("Tất cả", comp, "Liên đoàn"):
                        continue
                    if self.filter_status.get() != "Đã đá":
                        phase_label = ("Premier · Giai đoạn 2" if tier == "premier" else
                                       f"{comp} · Play-off thăng hạng" if tier in ("fc1", "fc2") else
                                       "Liên đoàn · Giai đoạn 2")
                        month_rows.append({
                            "date": slot["date"], "comp": comp,
                            "round": f"{phase_label} · Vòng {slot.get('round', '')}",
                            "home": "", "away": "", "_calendar_marker": True,
                            "_played": False, "_eligible": False,
                        })
                continue
            comp = "Cup" if kind.startswith("cup_") else ("C1" if kind.startswith("c1_") else "")
            if self.filter_comp.get() not in ("Tất cả", comp):
                continue
            if self.filter_status.get() == "Đã đá":
                continue
            month_rows.append({
                "date": slot["date"], "comp": comp, "round": slot.get("label", "Lịch giải đấu"),
                "home": "", "away": "", "_calendar_marker": True,
                "_played": False, "_eligible": False,
            })

        # Keep generated placeholders alongside real fixtures so clicking a
        # phase-two calendar marker can open the day detail dialog as well.
        self._calendar_rows = list(month_rows)
        for index, cell in enumerate(self.calendar_day_cells):
            day = start + timedelta(days=index)
            inside = day.month == month.month
            is_preparation_day = bool(preparation_date and day.isoformat() == preparation_date)
            base_bg = GOLD_BG if is_preparation_day else (SURFACE_2 if inside else BG)
            date_text = ("★ " if is_preparation_day else "") + day.strftime("%d")
            cell["date"].configure(text=date_text,
                                   fg=GOLD if is_preparation_day else (TEXT if inside else DIM),
                                   bg=base_bg, cursor="hand2")
            cell["frame"].configure(bg=base_bg,
                                    highlightbackground=GOLD if is_preparation_day else (LINE if inside else BG),
                                    highlightthickness=2 if is_preparation_day else 1)
            cell["events"].configure(bg=base_bg)
            for child in cell["events"].winfo_children():
                child.destroy()
            day_rows = [row for row in month_rows if row.get("date") == day.isoformat()]
            if not day_rows:
                cell["date"].unbind("<Button-1>")
                if is_preparation_day:
                    next_label = str((next_slot or {}).get("label", "Lượt thi đấu tiếp theo"))
                    marker = tk.Label(cell["events"], text="★ NGÀY CHUẨN BỊ\n" + next_label,
                                      font=F(8, "bold"), bg=GOLD_BG, fg=GOLD,
                                      anchor="w", justify="left", padx=4, pady=2,
                                      wraplength=max(105, cell["events"].winfo_width() - 8))
                    marker.pack(fill="x")
                continue
            cell["date"].bind("<Button-1>", lambda _event, value=day.isoformat(): self._show_calendar_day_matches(value))

            grouped = {}
            for row in day_rows:
                grouped.setdefault(row.get("comp", "Lịch"), []).append(row)
            summary_parts = []
            has_ready = False
            has_played = False
            for comp, group_rows in sorted(grouped.items(), key=lambda item: item[0]):
                marker_rows = [row for row in group_rows if row.get("_calendar_marker")]
                short = self._calendar_comp_short(comp)
                if marker_rows:
                    round_text = str(marker_rows[0].get("round", "")).replace("Cup - ", "").replace("C1 - ", "").replace("Liên đoàn · ", "")
                    round_text = round_text.replace("Giai đoạn 2", "GĐ2").replace("Vòng ", "V")
                    summary_parts.append(f"{short} {round_text}")
                    continue
                ready = sum(1 for row in group_rows if row.get("_eligible") and not row.get("_played"))
                played = sum(1 for row in group_rows if row.get("_played"))
                pending = max(0, len(group_rows) - ready - played)
                has_ready = has_ready or ready > 0
                has_played = has_played or played > 0
                count = len(group_rows)
                if ready and played:
                    summary_parts.append(f"{short} {count} ({ready}▶/{played}✓)")
                elif ready:
                    summary_parts.append(f"{short} ×{ready} ▶")
                elif played:
                    summary_parts.append(f"{short} ×{played} ✓")
                else:
                    summary_parts.append(f"{short} ×{pending or count}")
            if summary_parts:
                prefix = "▶ " if has_ready else ("✓ " if has_played else "· ")
                bg, fg = ((TURF_BG, TURF_LIGHT) if has_ready else
                          (SURFACE, TEXT) if has_played else (RAISED, MUTED))
                if is_preparation_day:
                    prefix, bg, fg = "★ CHUẨN BỊ · ", GOLD_BG, GOLD
                summary = prefix + " · ".join(summary_parts[:4])
                label = tk.Label(cell["events"], text=summary, font=F(8), bg=bg, fg=fg,
                                 anchor="w", justify="left", padx=4, pady=1,
                                 cursor="hand2", wraplength=max(105, cell["events"].winfo_width() - 8))
                label.pack(fill="x", pady=0)
                label.bind("<Button-1>", lambda _event, value=day.isoformat(): self._show_calendar_day_matches(value))
            # Clicking anywhere in the date cell opens all matches for that day.
            cell["frame"].bind("<Double-1>", lambda _event, value=day.isoformat(): self._show_calendar_day_matches(value))

    def _show_calendar_day_matches(self, day_value):
        """Show every fixture on a calendar date; double-click a row to open it."""
        rows = [row for row in getattr(self, "_calendar_rows", getattr(self, "_last_fixture_rows", []))
                if row.get("date") == day_value]
        if not rows:
            return
        top = tk.Toplevel(self.root)
        top.title(f"Lịch thi đấu · {day_value}")
        top.configure(bg=BG)
        top.transient(self.root)
        top.geometry("1120x620")
        shell = tk.Frame(top, bg=BG)
        shell.pack(fill="both", expand=True, padx=18, pady=16)
        tk.Label(shell, text=f"Các trận ngày {day_value}", font=F(15, "bold"), bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(shell, text=f"{len(rows)} trận / sự kiện · Chọn một trận để mở Live Match hoặc phát lại diễn biến đã lưu",
                 font=F(9), bg=BG, fg=MUTED).pack(anchor="w", pady=(2, 10))
        tree = self._tree(shell, [
            ("comp", "Giải", 155), ("round", "Vòng", 175), ("home", "Chủ nhà", 190),
            ("score", "Tỷ số", 90), ("red", "Thẻ đỏ H–K", 95), ("away", "Đội khách", 190),
            ("state", "Trạng thái", 150),
        ])
        row_map = {}
        for index, row in enumerate(rows):
            if row.get("_calendar_marker"):
                score, red, state = "—", "—", "Lịch dự kiến"
            elif row.get("_played"):
                score = f"{row.get('hg')}–{row.get('ag')}"
                red = "—"
                state = row.get("winner") or "Đã đá"
                if row.get("pens"):
                    score += f" (P {row['pens'].get('home')}-{row['pens'].get('away')})"
                def count_red(side):
                    cards = row.get("cards_" + side)
                    if isinstance(cards, list):
                        return sum(1 for item in cards if str(item.get("c", "")).lower() in ("r", "yr"))
                    stats = (row.get("player_stats") or {}).get(side, {}) or {}
                    return sum(int((item or {}).get("rc", 0) or 0) for item in stats.values())
                red = f"{count_red('h')}–{count_red('a')}"
            else:
                score, red = "—", "—"
                state = "▶ Sẵn sàng" if row.get("_eligible") else "Chưa đến lượt"
            iid = tree.insert("", "end", values=(row.get("comp"), row.get("round"), row.get("home"),
                                score, red, row.get("away"), state),
                              tags=(("next_match_0" if row.get("_eligible") else "row_0"),))
            row_map[iid] = row
        def open_selected(_event=None):
            selected = tree.selection()
            if not selected:
                return
            row = row_map.get(selected[0])
            top.destroy()
            if row:
                if row.get("_calendar_marker"):
                    self.toast(row.get("round", "Lịch giải đấu"), "info")
                else:
                    self._calendar_click_row(row)
        tree.bind("<Double-1>", open_selected)
        buttons = tk.Frame(shell, bg=BG)
        buttons.pack(fill="x", pady=(10, 0))
        FlatButton(buttons, "Mở trình mô phỏng / phát lại", open_selected, kind="primary").pack(side="right")
        FlatButton(buttons, "Đóng", top.destroy, kind="secondary").pack(side="right", padx=(0, 8))
        top.update_idletasks()
        self._center_dialog(top, 1120, 620)
        top.focus_set()

    # ── CLB & cầu thủ ───────────────────────────────────────────────────────

    def _record_player_careers(self):
            """Ghi sổ sự nghiệp khi số trận đã đá thay đổi (không bao giờ làm hỏng giao diện)."""
            try:
                state = self.game.s
                signature = (int(state.get("season", 0)),
                             sum(1 for m in state.get("matches", []) if m.get("played")))
                if signature == getattr(self, "_career_signature", None):
                    return
                pprofile.record_all(self.game)
                self._career_signature = signature
            except Exception:
                pass

    def _bind_player_profile(self, tree, resolver=None):
            """Bấm một dòng cầu thủ -> mở hồ sơ. ``resolver`` đổi iid của dòng thành player_id."""
            def open_row(iid):
                if not iid:
                    return
                pid = resolver(iid) if resolver else iid
                if pid:
                    self._open_player_profile(str(pid), iid=str(iid))

            def on_release(event):
                if tree.identify_region(event.x, event.y) in ("cell", "tree"):
                    open_row(tree.identify_row(event.y))

            tree.bind("<ButtonRelease-1>", on_release, add="+")
            tree.bind("<Return>", lambda _e: open_row((tree.selection() or (None,))[0]), add="+")

    def _draw_player_spider(self, canvas, axes):
            import math
            canvas.delete("all")
            if not axes:
                return
            width = max(280, int(canvas.winfo_width()))
            height = max(260, int(canvas.winfo_height()))
            cx, cy = width / 2, height / 2 + 4
            radius = min(width * 0.30, height * 0.34)
            count = len(axes)
            angles = [-90 + i * 360 / count for i in range(count)]
            unit = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in angles]
            for level in (20, 40, 60, 80, 100):
                pts = []
                for ax, ay in unit:
                    pts.extend((cx + ax * radius * level / 100, cy + ay * radius * level / 100))
                canvas.create_polygon(*pts, fill="", outline=LINE_2, width=1)
            for ax, ay in unit:
                canvas.create_line(cx, cy, cx + ax * radius, cy + ay * radius, fill=LINE_2)
            pts = []
            for (_label, value), (ax, ay) in zip(axes, unit):
                pts.extend((cx + ax * radius * value / 100, cy + ay * radius * value / 100))
            canvas.create_polygon(*pts, fill="#1c6844", outline=TURF_LIGHT, width=2, stipple="gray50")
            for (label, value), (ax, ay) in zip(axes, unit):
                px, py = cx + ax * radius * value / 100, cy + ay * radius * value / 100
                canvas.create_oval(px - 3, py - 3, px + 3, py + 3, fill=GOLD, outline="")
                anchor = "e" if ax < -0.3 else ("w" if ax > 0.3 else "center")
                lx, ly = cx + ax * (radius + 14), cy + ay * (radius + 16)
                canvas.create_text(lx, ly - 6, text=label, fill=MUTED, font=F(8, "bold"), anchor=anchor)
                canvas.create_text(lx, ly + 7, text=f"{value:.0f}", fill=TEXT, font=F(9, "bold"), anchor=anchor)

    def _attr_color(self, value):
            return TURF_LIGHT if value >= 80 else (TEXT if value >= 65 else (GOLD if value >= 50 else RED))

    def _attribute_bars(self, parent, rows, columns=3):
            grid = tk.Frame(parent, bg=SURFACE)
            grid.pack(fill="x", padx=12, pady=(0, 10))
            for col in range(columns):
                grid.columnconfigure(col, weight=1, uniform="attr")
            for index, (label, value) in enumerate(rows):
                cell = tk.Frame(grid, bg=SURFACE)
                cell.grid(row=index // columns, column=index % columns, sticky="ew", padx=6, pady=2)
                tk.Label(cell, text=label, font=F(9), bg=SURFACE, fg=MUTED, anchor="w", width=13).pack(side="left")
                bar = tk.Canvas(cell, height=8, width=90, bg=SURFACE_2, highlightthickness=0)
                bar.pack(side="left", padx=(0, 6))
                bar.create_rectangle(0, 0, 90 * max(0, min(100, value)) / 100, 8, fill=self._attr_color(value), outline="")
                tk.Label(cell, text=f"{value:.0f}", font=F(9, "bold"), bg=SURFACE, fg=self._attr_color(value)).pack(side="left")

    def _open_player_profile(self, pid, iid=None):
        state = self.game.s
        player = meta = None
        club_hint = None
        search_entry = getattr(self, "_hall_player_search_lookup", {}).get(str(iid)) if iid else None
        if search_entry:
            player, meta, club_hint, resolved_pid = search_entry
            pid = str(resolved_pid or player.get("player_id") or player.get("id") or pid)
        elif iid and str(iid).startswith("academy:"):
            entry = getattr(self, "_academy_lookup", {}).get(str(iid))
            if not entry:
                return
            player, meta, club_hint = entry
            pid = str(player.get("player_id") or player.get("id") or iid)
        elif pid not in state.get("players", {}):
            return
        try:
            season_label = str(self.game.season_label())
        except Exception:
            season_label = ""
        form_row = None
        club = next((team for team, roster in state.get("rosters", {}).items()
                     if str(pid) in {str(x) for x in roster}), "")
        if not club:
            club = (state.get("contracts", {}).get(pid, {}) or {}).get("team_id") or (
                state.get("players", {}).get(pid, {}) or {}).get("club")
        if club in state.get("rosters", {}):
            try:
                form_row = next((r for r in self.game.team_form(club)
                                 if str(r.get("player_id")) == str(pid)), None)
            except Exception:
                form_row = None
        profile = pprofile.build_player_profile(
            state, pid, make_player=match_engine_core.make_player, team_form_row=form_row,
            season_label=season_label, player=player, meta=meta)
        if club_hint and profile["bio"]["club"] in ("—", ""):
            profile["bio"]["club"] = f"{club_hint} (học viện)"

        old = getattr(self, "_player_win", None)
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
        top = tk.Toplevel(self.root)
        self._player_win = top
        bio = profile["bio"]
        def show_stat(value):
            if value is None:
                return "—"
            try:
                if isinstance(value, bool):
                    return int(value)
                return int(round(float(value)))
            except (TypeError, ValueError):
                return value

        def show_rating(value):
            if value is None:
                return "—"
            try:
                return f"{float(value):.2f}"
            except (TypeError, ValueError):
                return value
        top.title(f"Hồ sơ cầu thủ · {bio['name']}")
        top.configure(bg=BG)
        top.transient(self.root)
        top.bind("<Escape>", lambda _e: top.destroy())
        self._center_dialog(top, 1180, 820)
        shell = tk.Frame(top, bg=BG)
        shell.pack(fill="both", expand=True, padx=18, pady=14)

        head = tk.Frame(shell, bg=BG)
        head.pack(fill="x", pady=(0, 10))
        left = tk.Frame(head, bg=BG)
        left.pack(side="left", fill="x", expand=True)
        title = bio["name"] + ("  ★" if bio["superstar"] else "")
        tk.Label(left, text=title, font=F(20, "bold"), bg=BG, fg=TEXT, anchor="w").pack(anchor="w")
        facts = [f"{bio['position']} · {bio['group_label']}"]
        if bio["age"] is not None:
            facts.append(f"{bio['age']:.0f} tuổi")
        for key, fmt in (("nationality", "{}"), ("foot", "Chân {}"), ("height", "{} cm"),
                         ("weight", "{} kg"), ("birthdate", "Sinh {}")):
            if bio.get(key):
                facts.append(fmt.format(show_stat(bio[key]) if key in {"height", "weight"} else bio[key]))
        if bio.get("shirt"):
            facts.append(f"Số áo {show_stat(bio['shirt'])}")
        if bio.get("weak_foot") not in (None, ""):
            facts.append(f"Chân không thuận {show_stat(bio['weak_foot'])}/5")
        if bio.get("skill_moves") not in (None, ""):
            facts.append(f"Kỹ thuật {show_stat(bio['skill_moves'])}/5")
        if bio.get("alternate_positions"):
            facts.append(f"Vị trí phụ: {bio['alternate_positions']}")
        tk.Label(left, text=" · ".join(facts), font=F(9), bg=BG, fg=MUTED,
                 anchor="w", wraplength=850, justify="left").pack(anchor="w", pady=(2, 0))
        tk.Label(left, text=f"CLB: {bio['club']}", font=F(11, "bold"), bg=BG,
                 fg=GOLD, anchor="w").pack(anchor="w", pady=(3, 0))
        if bio.get("playstyles"):
            tk.Label(left, text=f"PlayStyles: {bio['playstyles']}", font=F(9), bg=BG,
                     fg=MUTED, anchor="w", wraplength=850, justify="left").pack(anchor="w")
        if bio["unavailable"]:
            tk.Label(left, text="⚠ " + " · ".join(bio["unavailable"]), font=F(9, "bold"),
                     bg=BG, fg=RED, anchor="w").pack(anchor="w")
        badges = tk.Frame(head, bg=BG)
        badges.pack(side="right", padx=(10, 0))
        for caption, value in (("OVR", f"{bio['ovr']:.0f}"),
                               ("POT", f"{float(bio['potential']):.0f}" if bio["potential"] is not None else "—")):
            badge = tk.Frame(badges, bg=RAISED, padx=14, pady=6)
            badge.pack(side="left", padx=(8, 0))
            tk.Label(badge, text=value, font=F(20, "bold"), bg=RAISED, fg=TURF_LIGHT).pack()
            tk.Label(badge, text=caption, font=F(8, "bold"), bg=RAISED, fg=MUTED).pack()

        tabs = Segmented(shell, hint="Thông tin chỉ đọc · mọi quyết định do AI xử lý")
        tabs.pack(fill="both", expand=True)
        overview = tabs.add("overview", "Tổng quan")
        attributes_page = tabs.add("attributes", "Chỉ số")
        career_page = tabs.add("career", "Sự nghiệp")
        stats_page = tabs.add("stats", "Thống kê giải")
        awards_page = tabs.add("awards", "Danh hiệu")
        transfer_page = tabs.add("transfers", "Chuyển nhượng")
        development_page = tabs.add("development", "Phát triển")

        # Tổng quan
        top_row = tk.Frame(overview, bg=BG)
        top_row.pack(fill="x", pady=(0, 8))
        top_row.columnconfigure(0, weight=1, uniform="pp")
        top_row.columnconfigure(1, weight=1, uniform="pp")
        radar_card = make_card(top_row, SURFACE)
        radar_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(radar_card, text="BIỂU ĐỒ NHỆN · NĂNG LỰC" +
                 (" (ước lượng từ OVR)" if profile["axes_approximate"] else ""),
                 font=F(9, "bold"), bg=SURFACE, fg=TEXT).pack(anchor="w", padx=12, pady=(8, 0))
        canvas = tk.Canvas(radar_card, height=270, bg=SURFACE, highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        canvas.bind("<Configure>", lambda _e: self._draw_player_spider(canvas, profile["axes"]))

        info_card = make_card(top_row, SURFACE)
        info_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        tk.Label(info_card, text="HỢP ĐỒNG & TÌNH TRẠNG", font=F(9, "bold"),
                 bg=SURFACE, fg=TEXT).pack(anchor="w", padx=12, pady=(8, 4))
        rows = [
            ("Giá trị thị trường", f"{bio['market_value']:.2f} M€"),
            ("Lương / năm", f"{bio['wage']:.2f} M€"),
            ("Hợp đồng còn lại", f"{bio['contract_years']} năm" if bio["contract_years"] is not None else "Tự do"),
            ("Vai trò", bio["role"] or "—"),
            ("Phong độ", f"{int(round(float(bio['form']))):+d}" if bio["form"] is not None else "—"),
            ("Thể lực", f"{float(bio['fitness']):.0f}%" if bio["fitness"] is not None else "—"),
        ]
        medical = bio.get("medical", {}) or {}
        active_injury = medical.get("active_injury") or {}
        if active_injury:
            rows.append(("Chấn thương", active_injury.get("injury_name", "Đang hồi phục")))
            rows.append(("Hồi phục còn lại", f"{int(medical.get('days_to_recovery', 0) or 0)} ngày"))
        rows.append(("Số lần chấn thương", int(medical.get("episodes", 0) or 0)))
        damage = medical.get("permanent_damage", {}) or {}
        if damage:
            rows.append(("Suy giảm thể chất", ", ".join(
                f"{key.replace('_', ' ')} -{int(value)}" for key, value in sorted(damage.items())
            )))
        for label, value in rows:
            line = tk.Frame(info_card, bg=SURFACE)
            line.pack(fill="x", padx=12, pady=3)
            tk.Label(line, text=label, font=F(9), bg=SURFACE, fg=MUTED, anchor="w").pack(side="left")
            tk.Label(line, text=str(value), font=F(10, "bold"), bg=SURFACE,
                     fg=RED if label in ("Chấn thương", "Hồi phục còn lại", "Suy giảm thể chất") else TEXT,
                     anchor="e", wraplength=320, justify="right").pack(side="right")

        season = profile["season"]
        season_card = make_card(overview, SURFACE)
        season_card.pack(fill="x", pady=(0, 8))
        tk.Label(season_card, text=f"THÀNH TÍCH MÙA {season.get('label') or 'HIỆN TẠI'}",
                 font=F(9, "bold"), bg=SURFACE, fg=TEXT).pack(anchor="w", padx=12, pady=(8, 4))
        stat_row = tk.Frame(season_card, bg=SURFACE)
        stat_row.pack(fill="x", padx=8, pady=(0, 10))
        for caption, value in (
            ("Trận", show_stat(season.get("apps", 0))), ("Đá chính", show_stat(season.get("starts", 0))),
            ("Phút", show_stat(season.get("minutes", 0))), ("Bàn", show_stat(season.get("goals", 0))),
            ("Kiến tạo", show_stat(season.get("assists", 0))), ("Sạch lưới", show_stat(season.get("clean_sheets", 0))),
            ("Thẻ vàng", show_stat(season.get("yellow", 0))), ("Thẻ đỏ", show_stat(season.get("red", 0))),
            ("ĐG TB", show_rating(season.get("avg_rating"))),
        ):
            box = tk.Frame(stat_row, bg=RAISED, padx=8, pady=6)
            box.pack(side="left", padx=3, expand=True, fill="x")
            tk.Label(box, text=str(value), font=F(14, "bold"), bg=RAISED, fg=TEXT).pack()
            tk.Label(box, text=caption, font=F(8), bg=RAISED, fg=MUTED).pack()

        attr_card = make_card(overview, SURFACE)
        attr_card.pack(fill="x", pady=(0, 8))
        tk.Label(attr_card, text="6 CHỈ SỐ CHÍNH", font=F(9, "bold"),
                 bg=SURFACE, fg=TEXT).pack(anchor="w", padx=12, pady=(8, 4))
        main_labels = {"Tốc độ", "Pace", "Dứt điểm", "Chuyền", "Rê bóng", "Phòng ngự", "Thể chất",
                       "Bay người", "Bắt bóng", "Phát bóng", "Chọn vị trí", "Phản xạ"}
        main_attributes = [row for row in profile["attributes"] if row[0] in main_labels]
        self._attribute_bars(attr_card, main_attributes or profile["axes"][:6], columns=3)
        if bio.get("playstyles"):
            tk.Label(overview, text=f"Phong cách chơi: {bio['playstyles']}", font=F(9),
                     bg=BG, fg=MUTED, anchor="w", wraplength=1080, justify="left").pack(anchor="w", pady=4)

        # Chỉ số chi tiết: vùng cuộn để hiển thị toàn bộ dữ liệu FC/FIFA của cầu thủ.
        attr_shell = tk.Frame(attributes_page, bg=BG)
        attr_shell.pack(fill="both", expand=True)
        attr_canvas = tk.Canvas(attr_shell, bg=BG, highlightthickness=0)
        attr_scroll = ttk.Scrollbar(attr_shell, orient="vertical", command=attr_canvas.yview)
        attr_canvas.configure(yscrollcommand=attr_scroll.set)
        attr_canvas.pack(side="left", fill="both", expand=True)
        attr_scroll.pack(side="right", fill="y")
        attr_inner = tk.Frame(attr_canvas, bg=BG)
        attr_window = attr_canvas.create_window((0, 0), window=attr_inner, anchor="nw")
        attr_inner.bind("<Configure>", lambda _e: attr_canvas.configure(scrollregion=attr_canvas.bbox("all")))
        attr_canvas.bind("<Configure>", lambda e: attr_canvas.itemconfigure(attr_window, width=e.width))
        if profile["attributes"]:
            self._attribute_bars(attr_inner, profile["attributes"], columns=2)
        else:
            tk.Label(attr_inner, text="Không có dữ liệu chỉ số chi tiết.", font=F(10),
                     bg=BG, fg=MUTED).pack(anchor="w", padx=12, pady=12)
        if bio.get("playstyles"):
            tk.Label(attr_inner, text=f"PlayStyles: {bio['playstyles']}", font=F(9),
                     bg=BG, fg=MUTED, wraplength=1000, justify="left").pack(anchor="w", padx=12, pady=10)

        # Sự nghiệp: mỗi dòng là một mùa tại một CLB, tổng hợp từ match logs/archive.
        totals = profile["career_totals"]
        clubs = profile["career_clubs"]
        ledger = list(profile["career"])
        summary = tk.Frame(career_page, bg=BG)
        summary.pack(fill="x", pady=(0, 8))
        for caption, value in (
            ("Số CLB", len(clubs)), ("Tổng trận", show_stat(totals["apps"])), ("Đá chính", show_stat(totals["starts"])),
            ("Tổng phút", show_stat(totals["minutes"])), ("Tổng bàn", show_stat(totals["goals"])),
            ("Kiến tạo", show_stat(totals["assists"])), ("Sạch lưới", show_stat(totals["clean_sheets"])),
            ("ĐG TB", show_rating(totals["avg_rating"])) ,
        ):
            box = tk.Frame(summary, bg=RAISED, padx=10, pady=6)
            box.pack(side="left", padx=3, expand=True, fill="x")
            tk.Label(box, text=str(value), font=F(14, "bold"), bg=RAISED, fg=GOLD).pack()
            tk.Label(box, text=caption, font=F(8), bg=RAISED, fg=MUTED).pack()
        tk.Label(career_page, text="Lịch sử thi đấu theo từng mùa và CLB",
                 font=F(9, "bold"), bg=BG, fg=TEXT).pack(anchor="w", pady=(2, 5))
        if any(r.get("archive_compact") for r in ledger):
            tk.Label(career_page,
                     text="Lưu ý: một số mùa cũ chỉ còn thống kê tổng hợp. CLB hiển thị là CLB ở lần ra sân cuối mùa; dữ liệu gốc không đủ log để tách chính xác từng CLB giữa mùa.",
                     font=F(8), bg=BG, fg=MUTED, wraplength=1080, justify="left").pack(anchor="w", pady=(0, 5))
        career_tree = self._tree(career_page, [
            ("season", "Mùa", 85), ("club", "CLB", 180), ("apps", "Trận", 55),
            ("starts", "Đá chính", 65), ("sub_apps", "Dự bị", 60), ("minutes", "Phút", 70),
            ("goals", "Bàn", 55), ("assists", "KT", 55), ("clean_sheets", "Sạch lưới", 75),
            ("yellow", "Vàng", 55), ("red", "Đỏ", 50), ("rating", "ĐG TB", 65),
        ], height=13)
        shown = list(reversed(ledger))
        self._set_tree(career_tree, shown, lambda _i, r: (
            r.get("season", "—"), r.get("club", "—"), show_stat(r.get("apps", 0)), show_stat(r.get("starts")),
            show_stat(r.get("sub_apps")), show_stat(r.get("minutes", 0)), show_stat(r.get("goals", 0)),
            show_stat(r.get("assists", 0)), show_stat(r.get("clean_sheets")), show_stat(r.get("yellow")),
            show_stat(r.get("red")), show_rating(r.get("avg_rating"))))
        career_tree.configure(selectmode="none")
        if not ledger:
            tk.Label(career_page, text="Chưa có dữ liệu thi đấu được lưu cho cầu thủ này.",
                     font=F(9), bg=BG, fg=MUTED).pack(anchor="w", pady=(6, 0))

        # Thống kê theo giải: Premier/FC/Cup/C1, lấy trực tiếp từ từng trận.
        comp_rows = list(profile.get("career_competitions", []))
        stats_tree = self._tree(stats_page, [
            ("season", "Mùa", 85), ("club", "CLB", 170), ("competition", "Giải đấu", 150),
            ("apps", "Trận", 55), ("minutes", "Phút", 65), ("goals", "Bàn", 55),
            ("assists", "KT", 55), ("shots", "Sút", 55), ("sot", "Trúng đích", 70),
            ("xg", "xG", 55), ("key_passes", "Cơ hội tạo", 75), ("tackles", "Tắc bóng", 65),
            ("interceptions", "Cắt bóng", 65), ("saves", "Cứu thua", 65), ("rating", "ĐG TB", 65),
        ], height=14)
        self._set_tree(stats_tree, list(reversed(comp_rows)), lambda _i, r: (
            r.get("season", "—"), r.get("club", "—"), r.get("competition", "—"),
            r.get("apps", 0), r.get("minutes", 0), r.get("goals", 0), r.get("assists", 0),
            r.get("shots", 0), r.get("shots_on_target", 0), show_stat(r.get("xg", 0)),
            r.get("key_passes", 0), r.get("tackles", 0), r.get("interceptions", 0),
            r.get("saves", 0), show_rating(r.get("avg_rating"))))
        stats_tree.configure(selectmode="none")
        if any(r.get("archive_compact") for r in ledger):
            tk.Label(stats_page,
                     text="Các mùa lưu trữ cũ chỉ có tổng thống kê mùa, chưa có log cá nhân theo từng giải. Bảng này hiển thị phân tách giải đấu cho những mùa còn dữ liệu trận chi tiết.",
                     font=F(8), bg=BG, fg=MUTED, wraplength=1080, justify="left").pack(anchor="w", pady=(5, 0))
        if not comp_rows:
            tk.Label(stats_page, text="Chưa có thống kê theo giải đấu.",
                     font=F(9), bg=BG, fg=MUTED).pack(anchor="w", pady=(6, 0))

        # Lịch sử chuyển nhượng
        transfer_tree = self._tree(transfer_page, [
            ("date", "Ngày", 110), ("from", "Từ CLB", 220), ("to", "Đến CLB", 220),
            ("fee", "Phí (M€)", 90), ("status", "Trạng thái", 150),
            ("wage", "Lương/năm", 90), ("years", "Thời hạn", 80),
        ], height=14)
        self._set_tree(transfer_tree, profile["transfers"], lambda _i, t: (
            t["date"], t["from"], t["to"], f"{t['fee']:.3f}", t["status"],
            _format_optional_float(t.get("wage")),
            f"{t['years']} năm" if t.get("years") else "—"))
        transfer_tree.configure(selectmode="none")
        if not profile["transfers"]:
            tk.Label(transfer_page, text="Chưa có thương vụ nào được ghi nhận cho cầu thủ này.",
                     font=F(9), bg=BG, fg=MUTED).pack(anchor="w", pady=(6, 0))

        # Phát triển dài hạn: chỉ vẽ các mốc OVR đã được engine lưu thực tế.
        development = profile.get("development", {})
        timeline = development.get("timeline", [])
        dev_summary = tk.Frame(development_page, bg=BG)
        dev_summary.pack(fill="x", pady=(0, 8))
        for caption, value in (
            ("Mốc mùa", len(timeline)),
            ("Biến động OVR", f"{development['ovr_change_total']:+.0f}" if development.get("ovr_change_total") is not None else "—"),
            ("Đỉnh OVR", f"{development['best_ovr']:.0f}" if development.get("best_ovr") is not None else "—"),
            ("Potential cao nhất", show_stat(development.get("best_potential"))),
            ("Lượt đổi thuộc tính", development.get("attribute_change_count", 0)),
            ("Danh hiệu cá nhân", len(development.get("awards", []))),
        ):
            tile = tk.Frame(dev_summary, bg=RAISED, padx=12, pady=7)
            tile.pack(side="left", padx=(0, 7), expand=True, fill="x")
            tk.Label(tile, text=str(value), font=F(14, "bold"), bg=RAISED, fg=GOLD).pack()
            tk.Label(tile, text=caption, font=F(8), bg=RAISED, fg=MUTED).pack()
        chart_card = make_card(development_page, SURFACE)
        chart_card.pack(fill="x", pady=(0, 8))
        tk.Label(chart_card, text="DIỄN BIẾN OVR QUA CÁC MÙA", font=F(9, "bold"),
                 bg=SURFACE, fg=TEXT).pack(anchor="w", padx=12, pady=(8, 0))
        chart = tk.Canvas(chart_card, height=220, bg=SURFACE, highlightthickness=0)
        chart.pack(fill="x", padx=8, pady=6)
        def draw_development_chart(_event=None):
            chart.delete("all")
            points = [(i, row.get("ovr")) for i, row in enumerate(timeline) if row.get("ovr") is not None]
            if not points:
                chart.create_text(18, 35, text="Chưa có đủ dữ liệu OVR qua nhiều mùa.", anchor="w", fill=MUTED, font=F(9))
                return
            width = max(300, chart.winfo_width())
            left, right, top_y, bottom = 48, width - 24, 22, 174
            values = [v for _, v in points]
            low = max(35, min(values) - 3); high = min(100, max(values) + 3)
            if high <= low: high = low + 1
            for tick in range(int(low // 5 * 5), int(high // 5 * 5) + 1, 5):
                y = bottom - (tick - low) / (high - low) * (bottom - top_y)
                chart.create_line(left, y, right, y, fill=LINE)
                chart.create_text(left - 9, y, text=str(tick), anchor="e", fill=MUTED, font=F(8))
            coords = []
            for index, value in points:
                x = left if len(points) == 1 else left + index * (right - left) / max(1, len(timeline) - 1)
                y = bottom - (value - low) / (high - low) * (bottom - top_y)
                coords.extend((x, y))
            if len(coords) >= 4:
                chart.create_line(*coords, fill=TURF_LIGHT, width=2, smooth=True)
            for (index, value), j in zip(points, range(0, len(coords), 2)):
                x, y = coords[j], coords[j + 1]
                chart.create_oval(x-4, y-4, x+4, y+4, fill=GOLD, outline=GOLD)
                chart.create_text(x, y-13, text=f"{value:.0f}", fill=TEXT, font=F(8, "bold"))
                chart.create_text(x, bottom+17, text=timeline[index].get("season", "—"), fill=MUTED, font=F(7))
        chart.bind("<Configure>", draw_development_chart)
        draw_development_chart()
        dev_tree = self._tree(development_page, [
            ("season", "Mùa", 90), ("club", "CLB", 160), ("age", "Tuổi đầu mùa", 75),
            ("old", "OVR đầu", 70), ("ovr", "OVR cuối", 70), ("delta", "Δ OVR", 65),
            ("pot_before", "POT đầu", 70), ("potential", "POT cuối", 70),
            ("minutes", "Phút", 70), ("rating", "ĐG TB", 65), ("injury", "Vắng CT", 65),
        ], height=7)
        self._set_tree(dev_tree, list(reversed(timeline)), lambda _i, row: (
            row.get("season"), row.get("club"), show_stat(row.get("age")),
            show_stat(row.get("old_ovr")), show_stat(row.get("ovr")),
            (f"{int(round(float(row['delta']))):+d}" if row.get("delta") is not None else "—"),
            show_stat(row.get("potential_before")), show_stat(row.get("potential")),
            show_stat(row.get("minutes", 0)), show_rating(row.get("avg_rating")),
            show_stat(row.get("injury_matches", 0))))
        dev_tree.configure(selectmode="none")

        tk.Label(development_page, text="CÁC YẾU TỐ TẠO RA TĂNG/GIẢM OVR", font=F(9, "bold"),
                 bg=BG, fg=TEXT).pack(anchor="w", pady=(10, 4))
        factor_rows = [row for row in reversed(timeline) if row.get("development_model")]
        factor_tree = self._tree(development_page, [
            ("season", "Mùa", 90), ("age", "Tuổi đóng góp", 90),
            ("potential", "POT factor*", 85), ("bonus", "POT bonus", 85),
            ("minutes", "Phút", 85), ("form", "Phong độ", 85),
            ("injury", "Chấn thương", 90), ("score", "Điểm tổng", 85),
            ("delta", "Δ OVR", 70),
        ], height=5)
        def factor_value(row, key):
            value = (row.get("development_model") or {}).get(key)
            return f"{float(value):+.2f}" if value is not None else "—"
        self._set_tree(factor_tree, factor_rows, lambda _i, row: (
            row.get("season"), factor_value(row, "age_factor"),
            factor_value(row, "potential_factor"), factor_value(row, "potential_bonus"),
            factor_value(row, "minutes_factor"), factor_value(row, "form_factor"),
            factor_value(row, "injury_factor"), factor_value(row, "development_score"),
            factor_value(row, "delta")))
        factor_tree.configure(selectmode="none")
        tk.Label(development_page,
                 text="* POT factor áp dụng để mở dư địa phát triển ở nhóm cầu thủ trẻ; Potential cao không triệt tiêu suy giảm tự nhiên ở tuổi lớn.",
                 font=F(8), bg=BG, fg=MUTED).pack(anchor="w", pady=(4, 0))

        tk.Label(development_page, text="BIẾN ĐỘNG TỪNG THUỘC TÍNH", font=F(9, "bold"),
                 bg=BG, fg=TEXT).pack(anchor="w", pady=(10, 4))
        attribute_changes = list(reversed(development.get("attribute_changes", [])))
        attribute_tree = self._tree(development_page, [
            ("season", "Mùa", 90), ("club", "CLB", 160), ("attribute", "Thuộc tính", 210),
            ("old", "Trước", 70), ("new", "Sau", 70), ("delta", "Thay đổi", 80),
        ], height=8)
        self._set_tree(attribute_tree, attribute_changes, lambda _i, change: (
            change.get("season"), change.get("club"), change.get("label"),
            change.get("old") if change.get("old") is not None else "—",
            change.get("new") if change.get("new") is not None else "—",
            f"{change['delta']:+.0f}" if change.get("delta") is not None else "—"))
        attribute_tree.configure(selectmode="none")
        if not attribute_changes:
            tk.Label(development_page,
                     text="Chưa có log thay đổi thuộc tính. Dữ liệu mùa từ Increment 5.8 sẽ được ghi mới; mùa cũ không được nội suy.",
                     font=F(8), bg=BG, fg=MUTED).pack(anchor="w", pady=(4, 0))
        tk.Label(development_page, text=development.get("history_note", ""), font=F(8),
                 bg=BG, fg=MUTED, wraplength=1080, justify="left").pack(anchor="w", pady=(5, 8))
        medical = development.get("medical", {}) or {}
        injury_rows = list(reversed(medical.get("injury_history", []) or []))
        tk.Label(development_page, text="LỊCH SỬ CHẤN THƯƠNG & PHỤC HỒI", font=F(9, "bold"),
                 bg=BG, fg=TEXT).pack(anchor="w", pady=(10, 4))
        injury_tree = self._tree(development_page, [
            ("date", "Ngày", 100), ("injury", "Chấn thương", 220), ("severity", "Mức độ", 85),
            ("matches", "Trận vắng", 75), ("recovery", "Ngày hồi phục", 95),
            ("recurrence", "Tái phát 12T", 85), ("status", "Trạng thái", 100),
        ], height=5)
        self._set_tree(injury_tree, injury_rows, lambda _i, row: (
            row.get("date", "—"), row.get("name", "—"), row.get("severity_label", "—"),
            row.get("matches", 0), row.get("recovery_days_total", "—"),
            row.get("recurrence_12m", 0), row.get("status", "—")))
        injury_tree.configure(selectmode="none")
        if not injury_rows:
            tk.Label(development_page, text="Chưa có hồ sơ chấn thương chi tiết trong save này.",
                     font=F(8), bg=BG, fg=MUTED).pack(anchor="w")
        if damage:
            tk.Label(development_page,
                     text="Suy giảm thuộc tính thể chất tích lũy: " + ", ".join(
                         f"{key.replace('_', ' ')} -{int(value)}" for key, value in sorted(damage.items())),
                     font=F(8), bg=BG, fg=RED, wraplength=1080, justify="left").pack(anchor="w", pady=(4, 0))

        # Tách danh hiệu cá nhân và thành tích tập thể thành hai bảng độc lập.
        award_views = Segmented(awards_page, hint="Lịch sử danh hiệu theo mùa và CLB")
        award_views.pack(fill="both", expand=True)
        individual_page = award_views.add("individual", "Danh hiệu cá nhân")
        team_page = award_views.add("team", "Danh hiệu tập thể")

        # Danh hiệu cá nhân.
        award_rows = list(reversed(development.get("awards", []) or []))
        award_header = tk.Frame(individual_page, bg=BG)
        award_header.pack(fill="x", pady=(0, 10))
        tk.Label(award_header, text="BẢNG VÀNG DANH HIỆU CÁ NHÂN",
                 font=F(11, "bold"), bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(award_header,
                 text="Lưu lại các danh hiệu cầu thủ thực sự đạt được. CLB ghi nhận là đội bóng gắn với thành tích trong mùa/giải đó; trường hợp chuyển CLB giữa mùa sẽ ưu tiên CLB có đóng góp lớn nhất.",
                 font=F(8), bg=BG, fg=MUTED, wraplength=1080, justify="left").pack(anchor="w", pady=(3, 0))
        summary = tk.Frame(individual_page, bg=BG)
        summary.pack(fill="x", pady=(0, 10))
        award_counts = (
            ("Tổng danh hiệu", len(award_rows)),
            ("Chiếc giày Vàng", sum(1 for row in award_rows if row.get("award_id") == "golden_boot")),
            ("Quả bóng Vàng", sum(1 for row in award_rows if row.get("award_id") == "ballon_dor")),
            ("Golden Boy", sum(1 for row in award_rows if row.get("award_id") == "golden_boy")),
        )
        for caption, value in award_counts:
            tile = tk.Frame(summary, bg=RAISED, padx=12, pady=8)
            tile.pack(side="left", padx=(0, 7), expand=True, fill="x")
            tk.Label(tile, text=str(value), font=F(16, "bold"), bg=RAISED, fg=GOLD).pack()
            tk.Label(tile, text=caption, font=F(8), bg=RAISED, fg=MUTED).pack()
        award_tree = self._tree(individual_page, [
            ("season", "Mùa", 90), ("title", "Danh hiệu", 235),
            ("competition", "Giải đấu / phạm vi", 190), ("club", "CLB khoác áo", 190),
            ("rank", "Thứ hạng", 75), ("detail", "Thành tích", 125),
        ], height=16)
        def award_detail(row):
            value, unit = row.get("value"), row.get("unit") or ""
            if value not in (None, ""):
                try:
                    number = int(value) if float(value).is_integer() else round(float(value), 2)
                except (TypeError, ValueError):
                    number = value
                return f"{number} {unit}".strip()
            return row.get("detail") or "—"
        self._set_tree(award_tree, award_rows, lambda _i, row: (
            row.get("season", "—"), row.get("title", "—"),
            row.get("competition", "Toàn mùa"), row.get("club", "—"),
            f"Hạng {row['rank']}" if row.get("rank") not in (None, "") else "—",
            award_detail(row)))
        award_tree.configure(selectmode="none")
        if not award_rows:
            tk.Label(individual_page,
                     text="Chưa có danh hiệu cá nhân được lưu cho cầu thủ này. Danh hiệu mới sẽ được ghi sau khi mùa/giải kết thúc; lịch sử cũ chỉ hiển thị những dữ liệu còn được lưu xác thực trong archive.",
                     font=F(9), bg=BG, fg=MUTED, wraplength=1080, justify="left").pack(anchor="w", pady=(8, 0))

        # Danh hiệu tập thể: chỉ ghi khi có bằng chứng cầu thủ thực sự ra sân cho CLB
        # trong giải đấu tương ứng; á quân được phân biệt, không tính là chức vô địch.
        team_award_rows = list(reversed(development.get("team_awards", []) or []))
        team_header = tk.Frame(team_page, bg=BG)
        team_header.pack(fill="x", pady=(0, 10))
        tk.Label(team_header, text="THÀNH TÍCH TẬP THỂ TRONG SỰ NGHIỆP",
                 font=F(11, "bold"), bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(team_header,
                 text="Ghi nhận các chức vô địch và lần về nhì mà cầu thủ góp mặt thực tế trong giải đấu, kèm màu áo CLB. Cầu thủ chuyển CLB giữa mùa chỉ nhận thành tích của những đội mà họ đã ra sân đại diện.",
                 font=F(8), bg=BG, fg=MUTED, wraplength=1080, justify="left").pack(anchor="w", pady=(3, 0))
        team_summary = tk.Frame(team_page, bg=BG)
        team_summary.pack(fill="x", pady=(0, 10))
        team_counts = (
            ("Chức vô địch", sum(1 for row in team_award_rows if row.get("achievement") == "champion")),
            ("Premier League", sum(1 for row in team_award_rows if row.get("achievement") == "champion" and row.get("competition") == "Premier League")),
            ("Major Cup", sum(1 for row in team_award_rows if row.get("achievement") == "champion" and row.get("competition") == "Major Cup")),
            ("FC1 / FC2", sum(1 for row in team_award_rows if row.get("achievement") == "champion" and row.get("competition") in ("Football Championship 1", "Football Championship 2"))),
            ("Champions League", sum(1 for row in team_award_rows if row.get("achievement") == "champion" and row.get("competition") == "Champions League")),
            ("Á quân", sum(1 for row in team_award_rows if row.get("achievement") == "runner_up")),
        )
        for caption, value in team_counts:
            tile = tk.Frame(team_summary, bg=RAISED, padx=10, pady=8)
            tile.pack(side="left", padx=(0, 6), expand=True, fill="x")
            tk.Label(tile, text=str(value), font=F(16, "bold"), bg=RAISED,
                     fg=GOLD if caption != "Á quân" else TURF_LIGHT).pack()
            tk.Label(tile, text=caption, font=F(8), bg=RAISED, fg=MUTED).pack()
        team_tree = self._tree(team_page, [
            ("season", "Mùa", 95), ("title", "Thành tích", 105),
            ("competition", "Giải đấu", 215), ("club", "CLB khoác áo", 210),
            ("apps", "Số trận", 85), ("minutes", "Số phút", 100),
        ], height=16)
        self._set_tree(team_tree, team_award_rows, lambda _i, row: (
            row.get("season", "—"), row.get("title", "—"),
            row.get("competition", "—"), row.get("club", "—"),
            int(row.get("appearances", 0) or 0), int(row.get("minutes", 0) or 0)))
        team_tree.configure(selectmode="none")
        if not team_award_rows:
            tk.Label(team_page,
                     text="Chưa có danh hiệu tập thể được xác minh cho cầu thủ này. Thành tích sẽ được ghi từ các mùa mới; archive cũ chỉ có kết quả đội bóng mà không còn log ra sân sẽ không được tự suy đoán.",
                     font=F(9), bg=BG, fg=MUTED, wraplength=1080, justify="left").pack(anchor="w", pady=(8, 0))

        status = development.get("current_unavailability", {})
        if status.get("injury_matches") or status.get("suspension_matches"):
            tk.Label(development_page,
                     text=f"Tình trạng hiện tại: chấn thương {status.get('injury_matches', 0)} trận · treo giò {status.get('suspension_matches', 0)} trận",
                     font=F(9, "bold"), bg=BG, fg=RED).pack(anchor="w", pady=(8, 0))


    def _build_squad(self):
        root = self.squad_frame
        bar = tk.Frame(root, bg=BG)
        bar.pack(fill="x", pady=(0, 12))

        self._club_back_context = None
        self.club_back_button = FlatButton(
            bar, "← Back", self._back_from_club_profile, kind="ghost", padx=12, pady=6
        )
        # Hidden by default. It is only shown when the profile was opened by
        # clicking a club name in a standings table.
        self._update_club_back_button()

        cell = tk.Frame(bar, bg=BG)
        cell.pack(side="left")
        tk.Label(cell, text="CLB", font=F(9), bg=BG, fg=MUTED).pack(anchor="w")
        self.squad_team = tk.StringVar(value=self.game.s["seed_order"][0])
        combo = ttk.Combobox(cell, textvariable=self.squad_team, values=sorted(self.game.s["rosters"]), state="readonly", width=32)
        combo.pack(pady=(3, 0))
        combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_squad())
        self.squad_title = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.squad_title, font=F(10), bg=BG, fg=MUTED).pack(side="left", padx=18, pady=(18, 0))

        self.squad_tabs = Segmented(root, hint="Thông tin hiện tại và lịch sử CLB")
        self.squad_tabs.pack(fill="both", expand=True)
        overview_page = self.squad_tabs.add("overview", "Đội hình & học viện")
        legacy = self.squad_tabs.add("legacy", "Danh hiệu & huyền thoại")
        history_page = self.squad_tabs.add("history", "Lịch sử & thống kê")
        self._build_club_history(history_page)
        management_page = self.squad_tabs.add("management", "Quản lý CLB")
        self.club_management_tabs = Segmented(management_page, hint="Thông tin tài chính, chuyển nhượng và hợp đồng do AI tự quyết định · Chế độ chỉ xem")
        self.club_management_tabs.pack(fill="both", expand=True)
        finance_page = self.club_management_tabs.add("finance", "Tài chính")
        transfers_page = self.club_management_tabs.add("transfers", "Chuyển nhượng")
        contracts_page = self.club_management_tabs.add("contracts", "Hợp đồng")
        stadium_page = self.club_management_tabs.add("stadium", "Sân vận động")
        reports_page = self.club_management_tabs.add("reports", "Báo cáo")
        inbox_page = self.club_management_tabs.add("inbox", "Thông báo")
        self.club_finance_text = self._scrolled_text(finance_page, mono=False, bg=SURFACE_2, padx=14, pady=12)
        market_notice = tk.Frame(transfers_page, bg=BG)
        market_notice.pack(fill="x", pady=(0, 8))
        self.club_market_hint = tk.StringVar(value="AI tự đánh giá nhu cầu, đàm phán, mua, bán, cho mượn và trao đổi cầu thủ. Người xem không thể can thiệp.")
        tk.Label(market_notice, textvariable=self.club_market_hint, bg=BG, fg=MUTED,
                 font=F(9), anchor="w", justify="left", wraplength=1000).pack(side="left", fill="x", expand=True)
        self.club_market_tree = self._tree(transfers_page, [
            ("player_id", "ID", 0), ("player", "Cầu thủ", 180), ("pos", "VT", 50),
            ("age", "Tuổi", 55), ("ovr", "OVR", 55), ("club", "CLB", 160),
            ("wage", "Lương/năm", 90), ("value", "Giá trị M€", 95),
            ("contract", "HĐ còn", 75), ("score", "Đề xuất", 75),
        ], height=9)
        self.club_market_tree.column("player_id", width=0, minwidth=0, stretch=False)
        self.club_transfer_tree = self._tree(transfers_page, [
            ("date", "Ngày", 95), ("player", "Cầu thủ", 180), ("from", "Từ CLB", 160),
            ("to", "Đến CLB", 160), ("fee", "Phí (M€)", 95), ("status", "Trạng thái", 120)
        ], height=8)
        contract_notice = tk.Label(contracts_page,
            text="AI tự theo dõi thời hạn, đánh giá khả năng giữ chân, gia hạn hoặc chấm dứt hợp đồng theo chính sách tài chính và đội hình.",
            bg=BG, fg=MUTED, font=F(9), anchor="w", justify="left", wraplength=1000)
        contract_notice.pack(fill="x", pady=(0, 8))
        self.club_contract_tree = self._tree(contracts_page, [
            ("player", "Cầu thủ", 190), ("pos", "VT", 55), ("age", "Tuổi", 55),
            ("ovr", "OVR", 55), ("role", "Vai trò", 100), ("wage", "Lương/năm", 95),
            ("mv", "Giá trị (M€)", 100), ("years", "Còn lại", 75), ("status", "Hợp đồng", 100)
        ], height=18)
        self.club_stadium_text = self._scrolled_text(stadium_page, mono=False, bg=SURFACE_2, padx=14, pady=12)
        self.club_report_text = self._scrolled_text(reports_page, mono=False, bg=SURFACE_2, padx=14, pady=12)
        self.club_inbox_text = self._scrolled_text(inbox_page, mono=False, bg=SURFACE_2, padx=14, pady=12)

        # Trang CLB có nhiều khối lớn; dùng một vùng cuộn dọc chung để người dùng
        # xem đầy đủ chiến thuật, thông tin phong độ/lịch đấu và danh sách cầu thủ.
        overview_canvas = tk.Canvas(overview_page, bg=BG, highlightthickness=0)
        overview_scroll = ttk.Scrollbar(overview_page, orient="vertical", command=overview_canvas.yview)
        overview_canvas.configure(yscrollcommand=overview_scroll.set)
        overview_canvas.pack(side="left", fill="both", expand=True)
        overview_scroll.pack(side="right", fill="y")
        overview = tk.Frame(overview_canvas, bg=BG)
        overview_window = overview_canvas.create_window((0, 0), window=overview, anchor="nw")
        overview.bind("<Configure>", lambda _e: overview_canvas.configure(scrollregion=overview_canvas.bbox("all")))
        overview_canvas.bind("<Configure>", lambda e: overview_canvas.itemconfigure(overview_window, width=e.width))
        self._bind_wheel(overview_canvas)

        split = tk.Frame(overview, bg=BG)
        split.pack(fill="both", expand=True)
        first = tk.Frame(split, bg=BG)
        academy = tk.Frame(split, bg=BG)
        first.pack(fill="both", expand=True)
        academy.pack(fill="both", expand=True, pady=(12, 0))
        self._caption(first, "Đội một", "11 đá chính tô nền xanh · 🩹 Chấn thương · 🟨 Thẻ vàng · 🟥 Thẻ đỏ · ⛔ Treo giò")

        # Tactical overview: coach + projected formation board + squad strength radar.
        tactical = tk.Frame(first, bg=BG)
        tactical.pack(fill="x", pady=(0, 8))
        tactical.columnconfigure(0, weight=3, uniform="club_tactical")
        tactical.columnconfigure(1, weight=2, uniform="club_tactical")

        pitch_card = make_card(tactical, SURFACE)
        pitch_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        pitch_head = tk.Frame(pitch_card, bg=SURFACE)
        pitch_head.pack(fill="x", padx=12, pady=(8, 2))
        self.club_coach_var = tk.StringVar(value="HLV: —")
        self.club_formation_var = tk.StringVar(value="Sơ đồ dự kiến: —")
        tk.Label(pitch_head, textvariable=self.club_coach_var, font=F(10, "bold"), bg=SURFACE, fg=TEXT).pack(side="left")
        tk.Label(pitch_head, textvariable=self.club_formation_var, font=F(9, "bold"), bg=SURFACE, fg=GOLD).pack(side="right")
        self.club_pitch_canvas = tk.Canvas(pitch_card, height=285, bg="#102a1d", highlightthickness=0)
        self.club_pitch_canvas.pack(fill="x", expand=True, padx=8, pady=(2, 8))
        self.club_pitch_canvas.bind("<Configure>", lambda _e: self._draw_club_pitch(
            self.club_pitch_canvas, getattr(self.club_pitch_canvas, "_club_snapshot", None)))

        radar_card = make_card(tactical, SURFACE)
        radar_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        tk.Label(radar_card, text="SỨC MẠNH ĐỘI HÌNH · SPIDER CHART", font=F(9, "bold"),
                 bg=SURFACE, fg=TEXT).pack(anchor="w", padx=12, pady=(8, 0))
        self.club_radar_canvas = tk.Canvas(radar_card, height=164, bg=SURFACE, highlightthickness=0)
        self.club_radar_canvas.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.club_radar_canvas.bind("<Configure>", lambda _e: self._draw_club_spider(
            self.club_radar_canvas, getattr(self.club_radar_canvas, "_club_metrics", None)))

        status_card = make_card(first, SURFACE)
        status_card.pack(fill="x", pady=(0, 10))
        status_body = tk.Frame(status_card, bg=SURFACE)
        status_body.pack(fill="x", padx=14, pady=10)
        self.club_rank_var = tk.StringVar(value="Vị trí hiện tại: —")
        self.club_form5_var = tk.StringVar(value="5 trận gần nhất: —")
        self.club_next_var = tk.StringVar(value="Trận kế tiếp: —")
        self.club_h2h_var = tk.StringVar(value="Đối đầu gần đây: —")
        tk.Label(status_body, textvariable=self.club_rank_var, font=F(10, "bold"), bg=SURFACE, fg=GOLD).pack(anchor="w")
        tk.Label(status_body, textvariable=self.club_form5_var, font=F(9), bg=SURFACE, fg=TEXT).pack(anchor="w", pady=(4, 0))
        tk.Label(status_body, textvariable=self.club_next_var, font=F(9), bg=SURFACE, fg=TEXT, wraplength=980, justify="left").pack(anchor="w", pady=(4, 0))
        tk.Label(status_body, textvariable=self.club_h2h_var, font=F(9), bg=SURFACE, fg=MUTED, wraplength=980, justify="left").pack(anchor="w", pady=(3, 0))

        self.squad_tree = self._tree(first, [("pos","VT",55),("name","Cầu thủ",250),("age","Tuổi",60),("ovr","OVR",60),
            ("pot","Potential",90),("wage","Lương/năm",90),("contract","HĐ còn",70),("market","Giá trị M€",90),
            ("form","Phong độ",90),("fit","Thể lực",90),("apps","Trận",60),("goals","Bàn",55),("assists","KT",55),("rating","ĐG TB",75)])
        self._caption(academy, "Học viện", "Chỉ hiển thị cầu thủ 16–20 tuổi")
        self.academy_tree = self._tree(academy, [("pos","VT",55),("name","Cầu thủ học viện",205),("age","Tuổi",55),("ovr","OVR",55),("pot","Potential",80),("talent","Tiềm năng",100)])
        # Mọi bảng cầu thủ chỉ mở hồ sơ để xem; không tạo bất kỳ thao tác quản lý CLB nào.
        self._bind_player_profile(self.squad_tree)
        self._bind_player_profile(self.academy_tree)
        self._bind_player_profile(self.club_contract_tree)
        self._bind_player_profile(self.club_market_tree)
        self._bind_player_profile(self.club_transfer_tree, lambda iid: self._transfer_pid_by_iid.get(iid))

        self.club_trophy_vars = {}
        self._build_trophy_cards(legacy, "club")
        self.club_legend_trees = self._build_legend_tabs(legacy, "club", include_team=False)

    def _build_club_history(self, parent):
        """Create season-by-season, competition, match and all-time club views."""
        self._caption(parent, "Thành tích & thống kê toàn thời gian",
                      "Dữ liệu ghép từ các mùa đã lưu và mùa đang diễn ra; tỷ số hòa sau 90 phút vẫn tính là hòa dù có luân lưu.")
        stats_host = tk.Frame(parent, bg=BG)
        stats_host.pack(fill="x", pady=(0, 10))
        self.club_history_stats_vars = {}
        stat_items = (
            ("matches", "Tổng số trận"), ("wins", "Thắng"), ("draws", "Hòa"),
            ("losses", "Thua"), ("top_scorer", "Vua phá lưới CLB"),
            ("top_assister", "Kiến tạo nhiều nhất"), ("biggest_win", "Thắng đậm nhất"),
            ("biggest_loss", "Thua đậm nhất"), ("first_match", "Trận đầu tiên"),
        )
        for index, (key, label) in enumerate(stat_items):
            card = make_card(stats_host, SURFACE)
            card.grid(row=index // 3, column=index % 3, sticky="nsew",
                      padx=(0 if index % 3 == 0 else 5, 5), pady=4)
            stats_host.columnconfigure(index % 3, weight=1, uniform="club_history_stats")
            body = tk.Frame(card, bg=SURFACE)
            body.pack(fill="both", expand=True, padx=10, pady=8)
            tk.Label(body, text=label, font=F(8), bg=SURFACE, fg=MUTED).pack(anchor="w")
            value = tk.StringVar(value="—")
            tk.Label(body, textvariable=value, font=F(10, "bold"), bg=SURFACE,
                     fg=TEXT, anchor="w", justify="left", wraplength=280).pack(fill="x", pady=(3, 0))
            self.club_history_stats_vars[key] = value

        self.club_history_tabs = Segmented(parent, hint="Lịch sử CLB theo mùa, theo giải, danh sách trận và Top 11 ra sân")
        self.club_history_tabs.pack(fill="both", expand=True)
        by_season = self.club_history_tabs.add("by_season", "Theo mùa")
        self.club_history_season_tree = self._tree(by_season, [
            ("season", "Mùa", 80), ("premier", "Premier", 95), ("fc1", "FC1", 75),
            ("fc2", "FC2", 75), ("cup", "Major Cup", 115), ("c1", "C1", 115),
        ], height=12)
        by_competition = self.club_history_tabs.add("by_competition", "Theo giải")
        self.club_history_comp_tree = self._tree(by_competition, [
            ("competition", "Giải đấu", 210), ("season", "Mùa", 90), ("finish", "Thành tích", 180),
        ], height=14)
        matches_page = self.club_history_tabs.add("matches", "Lịch sử trận")
        controls = tk.Frame(matches_page, bg=BG)
        controls.pack(fill="x", pady=(0, 7))
        tk.Label(controls, text="Mùa giải", font=F(9, "bold"), bg=BG, fg=MUTED).pack(side="left")
        self.club_history_season_var = tk.StringVar(value="")
        self.club_history_season_combo = ttk.Combobox(
            controls, textvariable=self.club_history_season_var, state="readonly", width=14
        )
        self.club_history_season_combo.pack(side="left", padx=(8, 0))
        self.club_history_season_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_club_history_matches())
        tk.Label(controls, text="Nhấp đúp trận đã đá để xem báo cáo.", font=F(8),
                 bg=BG, fg=MUTED).pack(side="left", padx=12)
        self.club_history_match_tree = self._tree(matches_page, [
            ("date", "Ngày", 95), ("competition", "Giải", 145), ("round", "Vòng", 175),
            ("home", "Chủ nhà", 190), ("score", "Tỷ số", 75), ("away", "Đội khách", 190),
            ("result", "Kết quả CLB", 100),
        ], height=16)
        self.club_history_match_lookup = {}
        self.club_history_match_tree.bind("<Double-1>", self._open_club_history_match)
        self.club_history_match_tree.bind("<Return>", self._open_club_history_match)
        top11_page = self.club_history_tabs.add("top11", "Top 11 ra sân")
        self.club_history_top11_tree = self._tree(top11_page, [
            ("rank", "#", 42), ("player", "Cầu thủ", 210), ("position", "VT", 55),
            ("apps", "Số trận", 85), ("goals", "Bàn", 70), ("assists", "Kiến tạo", 85),
        ], height=12)
        self._club_history_data = None

    def _refresh_club_history_matches(self):
        tree = getattr(self, "club_history_match_tree", None)
        data = getattr(self, "_club_history_data", None)
        if tree is None or not data:
            return
        selected = str(self.club_history_season_var.get() or "")
        season = int(selected) if selected.isdigit() else None
        rows = [row for row in data.get("matches", [])
                if season is None or int(row.get("season", season) or season) == season]
        self.club_history_match_lookup = {str(row.get("id")): row for row in rows}
        self._set_tree(tree, rows, lambda _i, row: (
            row.get("date", "—"), row.get("comp", "—"), row.get("round", "—"),
            row.get("home", "—"), f"{row.get('hg', '—')}–{row.get('ag', '—')}",
            row.get("away", "—"),
            ("Thắng" if ((row.get("home") == self._club_history_data.get("team") and int(row.get("hg", 0)) > int(row.get("ag", 0))) or
                         (row.get("away") == self._club_history_data.get("team") and int(row.get("ag", 0)) > int(row.get("hg", 0))))
             else "Hòa" if int(row.get("hg", 0)) == int(row.get("ag", 0)) else "Thua")),
            iid_of=lambda _i, row: str(row.get("id") or f"{row.get('season')}_{row.get('date')}_{row.get('home')}"))
        if not rows:
            tree.insert("", "end", values=("—", "—", "Chưa có trận", "", "", "", ""))

    def _open_club_history_match(self, event=None):
        tree = getattr(self, "club_history_match_tree", None)
        if tree is None:
            return
        iid = tree.identify_row(event.y) if event is not None and getattr(event, "y", None) is not None else (
            tree.selection()[0] if tree.selection() else "")
        row = getattr(self, "club_history_match_lookup", {}).get(str(iid))
        if row:
            self._show_match_report(row)

    def _build_trophy_cards(self, parent, prefix):
        """Compact six-card trophy summary; values are filled from career history."""
        host = tk.Frame(parent, bg=BG)
        host.pack(fill="x", pady=(0, 12))
        categories = (("premier", "Premier"), ("fc1", "FC1"), ("fc2", "FC2"),
                      ("cup", "Cup"), ("c1", "C1"), ("total", "Tổng danh hiệu"))
        variables = {}
        for index, (key, label) in enumerate(categories):
            card = make_card(host)
            card.grid(row=index // 3, column=index % 3, sticky="ew", padx=(0 if index % 3 == 0 else 5, 5), pady=4)
            host.columnconfigure(index % 3, weight=1, uniform=f"{prefix}_trophies")
            body = tk.Frame(card, bg=SURFACE)
            body.pack(fill="both", expand=True, padx=12, pady=10)
            tk.Label(body, text=label, font=F(9), bg=SURFACE, fg=MUTED).pack(anchor="w")
            value = tk.StringVar(value="0")
            tk.Label(body, textvariable=value, font=F(19, "bold"), bg=SURFACE,
                     fg=GOLD if key in ("cup", "c1", "total") else TEXT).pack(anchor="w", pady=(2, 0))
            variables[key] = value
        if prefix == "club":
            self.club_trophy_vars = variables
        return variables

    def _build_legend_tabs(self, parent, key, include_team=True):
        seg = Segmented(parent, hint="Top 11 trong toàn bộ lịch sử career; không chỉ riêng mùa hiện tại")
        seg.pack(fill="both", expand=True)
        columns = [("rank", "#", 42), ("player", "Cầu thủ", 190)]
        if include_team:
            columns.append(("team", "CLB", 165))
        columns += [("pos", "VT", 55), ("apps", "Trận", 65), ("goals", "Bàn", 60),
                    ("assists", "KT", 60), ("minutes", "Phút", 70)]
        trees = {}
        for tab_key, title in (("appearances", "Ra sân nhiều nhất"),
                               ("assists", "Kiến tạo nhiều nhất"),
                               ("goals", "Ghi bàn nhiều nhất")):
            frame = seg.add(tab_key, title)
            trees[tab_key] = self._tree(frame, columns, height=13)
        self.career_legend_trees[key] = trees
        return trees

    def _build_history_analytics(self):
        split = ttk.Panedwindow(self.history_analytics_frame, orient="vertical")
        split.pack(fill="both", expand=True)
        trophy_frame = tk.Frame(split, bg=BG)
        legends_frame = tk.Frame(split, bg=BG)
        split.add(trophy_frame, weight=2)
        split.add(legends_frame, weight=3)
        self._caption(trophy_frame, "Danh hiệu theo CLB", "Đếm các mùa đã hoàn tất; Cup và C1 được tách riêng")
        self.history_trophy_tree = self._tree(trophy_frame, [
            ("rank", "#", 42), ("team", "CLB", 230), ("premier", "Premier", 80),
            ("fc1", "FC1", 65), ("fc2", "FC2", 65), ("cup", "Cup", 65),
            ("c1", "C1", 65), ("total", "Tổng", 75)])
        self.history_legend_trees = self._build_legend_tabs(legends_frame, "history", include_team=True)

    def _build_hall_of_fame(self):
        self._caption(self.history_hall_frame, "Hall of Fame · Top 50 huyền thoại đã giải nghệ",
                      "Xếp hạng theo danh hiệu cá nhân, OVR đỉnh và thành tích career.")

        # Player search is deliberately separate from the Top 50 list: it queries
        # the full player directory, so active players and academy prospects can
        # still be found even when they are not Hall of Fame candidates.
        search_panel = tk.Frame(self.history_hall_frame, bg=SURFACE, padx=10, pady=8,
                                highlightthickness=1, highlightbackground=LINE)
        search_panel.pack(fill="x", pady=(0, 8))
        search_head = tk.Frame(search_panel, bg=SURFACE)
        search_head.pack(fill="x")
        tk.Label(search_head, text="Tìm cầu thủ trong toàn bộ dữ liệu", font=F(10, "bold"),
                 bg=SURFACE, fg=TEXT).pack(side="left")
        self.hall_search_count_var = tk.StringVar(value="Nhập ít nhất 2 ký tự để tìm kiếm")
        tk.Label(search_head, textvariable=self.hall_search_count_var, font=F(8),
                 bg=SURFACE, fg=MUTED).pack(side="right")
        search_controls = tk.Frame(search_panel, bg=SURFACE)
        search_controls.pack(fill="x", pady=(6, 5))
        self.hall_search_var = tk.StringVar(value="")
        self.hall_search_entry = ttk.Entry(search_controls, textvariable=self.hall_search_var)
        self.hall_search_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(search_controls, text="Xóa", command=lambda: self.hall_search_var.set("")).pack(side="left", padx=(7, 0))
        self.hall_search_var.trace_add("write", self._refresh_hall_player_search)
        tk.Label(search_panel,
                 text="Tìm được cầu thủ đang thi đấu, tự do, đã giải nghệ và cầu thủ học viện · không giới hạn trong Top 50.",
                 font=F(8), bg=SURFACE, fg=MUTED, anchor="w").pack(fill="x", pady=(0, 5))
        search_host = tk.Frame(search_panel, bg=SURFACE, height=166)
        search_host.pack(fill="x")
        search_host.pack_propagate(False)
        self.hall_search_tree = self._tree(search_host, [
            ("player", "Cầu thủ", 220), ("club", "CLB", 190),
            ("status", "Trạng thái", 125), ("pos", "VT", 48),
            ("ovr", "OVR", 60), ("age", "Tuổi", 55),
        ], height=4)
        self.hall_search_tree.bind("<Double-1>", self._open_hall_search_profile)
        self.hall_search_tree.bind("<Return>", self._open_hall_search_profile)
        self._hall_player_search_lookup = {}

        self._caption(self.history_hall_frame, "Bảng xếp hạng Top 50",
                      "Chỉ gồm cầu thủ đã giải nghệ; nhấp đúp để mở hồ sơ.")
        self.hall_of_fame_tree = self._tree(self.history_hall_frame, [
            ("rank", "#", 42), ("player", "Cầu thủ", 190), ("club", "CLB cuối", 165), ("pos", "VT", 48),
            ("season", "Mùa giải nghệ", 90), ("age", "Tuổi", 58), ("peak", "OVR đỉnh", 70),
            ("apps", "Trận career", 85), ("goals", "Bàn", 55), ("assists", "KT", 55),
            ("honors", "Danh hiệu cá nhân", 100),
        ], height=14)
        self.hall_of_fame_tree.bind("<Double-1>", self._open_hall_of_fame_profile)
        self._caption(self.history_hall_frame, "Danh hiệu cá nhân gồm Quả bóng Vàng/Bạc/Đồng, Găng tay Vàng, đội hình tiêu biểu và cầu thủ xuất sắc từng giải.",
                      "Archive cũ thiếu dữ liệu thì không tự suy đoán thành tích.")

    def _refresh_hall_player_search(self, *_args):
        tree = getattr(self, "hall_search_tree", None)
        variable = getattr(self, "hall_search_var", None)
        if tree is None or variable is None:
            return
        query = str(variable.get() or "").strip()
        tree.delete(*tree.get_children())
        self._hall_player_search_lookup = {}
        if len(query) < 2:
            self.hall_search_count_var.set("Nhập ít nhất 2 ký tự để tìm kiếm")
            return
        try:
            rows = self.game.career_player_directory(query)
        except Exception as exc:
            self.hall_search_count_var.set(f"Không thể tìm kiếm: {exc}")
            return
        self._hall_player_search_lookup = {
            str(row.get("iid")): (dict(row.get("player_data") or {}),
                                  dict(row.get("meta_data") or {}),
                                  str(row.get("club") or "—"),
                                  str(row.get("player_id") or ""))
            for row in rows
        }
        self._set_tree(tree, rows, lambda _i, row: (
            row.get("player", "—"), row.get("club", "—"), row.get("status", "—"),
            row.get("position", "—"), row.get("ovr") if row.get("ovr") is not None else "—",
            f"{float(row.get('age')):.1f}" if row.get("age") not in (None, "") else "—"),
            iid_of=lambda _i, row: str(row.get("iid")))
        count = len(rows)
        self.hall_search_count_var.set(f"{count} kết quả · tìm trên toàn bộ dữ liệu")

    def _open_hall_search_profile(self, event=None):
        tree = getattr(self, "hall_search_tree", None)
        if tree is None:
            return
        iid = tree.identify_row(event.y) if event is not None and getattr(event, "y", None) is not None else (
            tree.selection()[0] if tree.selection() else "")
        entry = getattr(self, "_hall_player_search_lookup", {}).get(str(iid))
        if entry and entry[3]:
            self._open_player_profile(entry[3], iid=str(iid))

    def _build_career_records(self):
        self._caption(self.history_records_frame, "Sổ kỷ lục Super League",
                      "Các mốc được tính từ kết quả và thống kê career đã lưu; không tính dự báo Monte Carlo.")
        self.career_records_tree = self._tree(self.history_records_frame, [
            ("record", "Kỷ lục", 245), ("holder", "Người / CLB giữ kỷ lục", 220),
            ("club", "CLB", 190), ("value", "Thành tích", 120), ("season", "Mùa / giải", 190),
        ], height=22)

    def _open_hall_of_fame_profile(self, event=None):
        tree = getattr(self, "hall_of_fame_tree", None)
        if tree is None:
            return
        iid = tree.identify_row(event.y) if event is not None else (tree.selection()[0] if tree.selection() else "")
        if iid:
            self._open_player_profile(str(iid), iid=str(iid))

    # ── Làm mới dữ liệu ─────────────────────────────────────────────────────

    def _overview_week_windows(self):
        """Simulation-calendar windows for the previous and current Monday-Sunday weeks."""
        focus = self._fixture_reference_date()
        current_start = focus - timedelta(days=focus.weekday())
        previous_start = current_start - timedelta(days=7)
        return {
            "previous": (previous_start, current_start - timedelta(days=1)),
            "current": (current_start, current_start + timedelta(days=6)),
        }

    def _overview_week_fixtures(self):
        """Collect played results and read-only pending projections from all competitions."""
        rows = []
        seen = set()
        for match in self.game.s.get("matches", []):
            if not match.get("played", True):
                continue
            key = (str(match.get("date", "")), str(match.get("comp", "")),
                   str(match.get("home", "")), str(match.get("away", "")))
            if key in seen:
                continue
            seen.add(key)
            rows.append({**match, "_played": True})

        for slot in self.game.s.get("slots", []):
            if slot.get("played"):
                continue
            try:
                specs = self.game.slot_fixture_specs(slot, include_played=False, allow_future=True)
            except (RuntimeError, KeyError, IndexError, ValueError):
                # A later bracket round may not have qualified teams yet.
                continue
            for spec in specs:
                key = (str(spec.get("date", "")), str(spec.get("comp", "")),
                       str(spec.get("home", "")), str(spec.get("away", "")))
                if key in seen:
                    continue
                seen.add(key)
                rows.append({**spec, "_played": False})
        rows.sort(key=lambda row: (str(row.get("date", "")), str(row.get("comp", "")),
                                   str(row.get("home", "")), str(row.get("away", ""))))
        return rows

    @staticmethod
    def _overview_comp_short(comp):
        name = str(comp or "")
        mapping = {
            "Premier League": "PREM", "Football Championship 1": "FC1",
            "Football Championship 2": "FC2", "Cup": "MAJOR",
            "C1": "C1", "C1 Play-in": "C1 PI",
        }
        return mapping.get(name, name[:7].upper())

    def _refresh_overview_week_panel(self):
        if not hasattr(self, "dash_week_trees"):
            return
        windows = self._overview_week_windows()
        all_rows = self._overview_week_fixtures()
        for key, (start, end) in windows.items():
            rows = []
            for row in all_rows:
                try:
                    row_date = date.fromisoformat(str(row.get("date", ""))[:10])
                except ValueError:
                    continue
                if start <= row_date <= end:
                    rows.append(row)
            counts = {"played": sum(1 for row in rows if row.get("_played")),
                      "pending": sum(1 for row in rows if not row.get("_played"))}
            self.dash_week_labels[key].set(
                f"{start.strftime('%d/%m')} – {end.strftime('%d/%m/%Y')} · "
                f"Đã đá {counts['played']} · Chưa đá {counts['pending']}"
            )
            tree = self.dash_week_trees[key]
            tree.delete(*tree.get_children())
            if not rows:
                tree.insert("", "end", values=("—", "—", "Không có trận", "—"))
                continue
            for index, row in enumerate(rows):
                match_text = f"{row.get('home', '')} – {row.get('away', '')}"
                if row.get("_played"):
                    result = f"FT {row.get('hg', 0)}–{row.get('ag', 0)}"
                    if row.get("pens"):
                        result += " P"
                    tag = f"week_played_{index % 2}"
                else:
                    result = "Chưa đá"
                    tag = f"week_pending_{index % 2}"
                tree.insert("", "end", values=(str(row.get("date", ""))[5:10],
                           self._overview_comp_short(row.get("comp")), match_text, result), tags=(tag,))

    def refresh_all(self):
        s = self.game.s
        self.status_var.set(self.game.status_text())
        done, total = self.game.slot_progress()
        nxt = self.game.next_slot()

        # Thanh tiến độ mùa
        self.season_var.set(f"Mùa {self.game.season_label()}")
        if nxt:
            detail = f"Kế tiếp: {nxt['label']} · {nxt['date']}"
            try:
                all_fixtures = self.game.slot_fixture_specs(nxt, include_played=True)
                remaining_fixtures = self.game.slot_fixture_specs(nxt, include_played=False)
                played_fixtures = len(all_fixtures) - len(remaining_fixtures)
                if played_fixtures:
                    detail += f" · đã đá {played_fixtures}/{len(all_fixtures)} trận"
            except (KeyError, RuntimeError, ValueError):
                pass
            self.next_var.set(detail)
        else:
            self.next_var.set("Đã chốt mùa" if s["season_done"] else "Chờ chốt mùa")
        self.progress.configure(maximum=max(total, 1), value=done)
        percent = round(100 * done / total) if total else 0
        self.progress_var.set(f"{done}/{total} slot ({percent}%)")
        self._sync_season_action_buttons()

        # Dashboard KPI + three league tabs.
        premier = self.game.league_table("premier")
        self.dash_labels["season"].set(self.game.season_label())
        self.dash_subs["season"].set("Đã chốt mùa" if s["season_done"] else "Đang diễn ra")
        self.dash_labels["matches"].set(f"{len(s['matches'])} / 248")
        self.dash_subs["matches"].set("trận trong mùa này")
        self.dash_labels["history"].set(str(len(s["history"])))
        self.dash_subs["history"].set("mùa đã hoàn tất")
        if premier:
            self.dash_labels["leader"].set(premier[0]["team"])
            self.dash_subs["leader"].set(f"{premier[0]['pts']} điểm sau {premier[0]['p']} trận")
        else:
            self.dash_labels["leader"].set("—")
            self.dash_subs["leader"].set("")

        def league_form_icons(team, tier, phase1_only=False):
            results = self.game.league_recent_form(team, tier, limit=5, phase1_only=phase1_only)
            icons = {"W": "✅", "D": "➖", "L": "❌"}
            return " ".join(icons.get(result, "—") for result in results) or "—"

        for tier in ("premier", "fc1", "fc2"):
            table = self.game.league_table(tier)
            def dashboard_tags(index, row, active_tier=tier):
                if active_tier == "premier":
                    if index == 0:
                        return ("top",)
                    if self.game.s.get("league_phase2", {}).get("initialized") and index >= 6:
                        return ("relegate",)
                elif active_tier == "fc1":
                    if index == 0:
                        return ("top",)
                    if index in (1, 2, 3):
                        return ("promote",)
                    if index in (5, 6):
                        return ("relegate_playoff",)
                    if index == 7:
                        return ("relegate",)
                elif active_tier == "fc2":
                    if index == 0:
                        return ("top",)
                    if index in (1, 2, 3):
                        return ("promote",)
                return ()
            self._set_tree(
                self.dash_league_trees[tier], table,
                lambda i, r, active_tier=tier: (i + 1, r["team"], r["p"], r["w"], r["d"], r["l"], r["gd"], r["pts"],
                                                  league_form_icons(r["team"], active_tier)),
                dashboard_tags,
            )

        # One editorial article per completed match, newest first. A five-match
        # slot therefore creates five separate news stories, never one slot recap.
        reference_date = self._fixture_reference_date()
        match_items = match_news.filter_recent_match_news(s.get("news", []), reference_date)
        match_items.sort(key=lambda item: (str(item.get("date", "")), int(item.get("match_id", 0))), reverse=True)
        match_items = match_news.unique_headlines(match_items)
        self._set_news_feed(self.dash_news, match_items)

        talent_items = match_news.potential_star_weekly_news(self.game, reference_date, limit=3)
        self._set_news_feed(
            self.dash_talent_news, talent_items,
            empty_message="Tuần này chưa có cầu thủ tiềm năng nào đạt tiêu chí nổi bật; vì vậy không có bài báo mới."
        )

        # Bảng xếp hạng và thể thức giai đoạn 2.
        def standings_values(i, row, tier):
            return (i + 1, row["team"], row["p"], row["w"], row["d"], row["l"],
                    row["gf"], row["ga"], row["gd"], row["pts"],
                    league_form_icons(row["team"], tier, phase1_only=True), round(row["elo"]))

        premier_phase1 = self.game.league_phase1_table("premier")
        self._set_tree(self.league_trees["premier"], premier_phase1,
                       lambda i, row: standings_values(i, row, "premier"),
                       lambda i, _r: ("promote",) if i < 4 else ())
        phase2_tables = self.game.premier_phase2_tables()
        def phase2_values(i, row):
            return (i + 1, row["team"], row["p"], row["w"], row["d"], row["l"],
                    row["gf"], row["ga"], row["gd"], row.get("phase1_pts", row["pts"]),
                    row.get("phase2_pts", 0), row["pts"], league_form_icons(row["team"], "premier"), round(row["elo"]))
        self._set_tree(self.premier_phase2_trees["champion"], phase2_tables["champion"], phase2_values,
                       lambda i, _r: ("top",) if i == 0 else ())
        self._set_tree(self.premier_phase2_trees["challenge"], phase2_tables["challenge"], phase2_values,
                       lambda i, _r: ("relegate",) if i >= 2 else ())

        for tier in ("fc1", "fc2"):
            table = self.game.league_phase1_table(tier)
            def league_row_tags(index, row, active_tier=tier):
                if index == 0: return ("top",)
                if index in (1, 2, 3): return ("promote",)
                if active_tier == "fc1" and index in (5, 6): return ("relegate_playoff",)
                if active_tier == "fc1" and index == 7: return ("relegate",)
                return ()
            self._set_tree(self.league_trees[tier], table,
                           lambda i, row, active_tier=tier: standings_values(i, row, active_tier),
                           league_row_tags)
            summary = self.game.promotion_playoff_summary(tier)
            teams = summary.get("teams", {})
            matches = summary.get("matches", {})
            semi, final = matches.get("semi"), matches.get("final")
            semi_text = (f"{semi['home']} {semi['hg']}–{semi['ag']} {semi['away']}" +
                         (f" · Pen {semi['pens']['home']}–{semi['pens']['away']}" if semi.get("pens") else "")
                         if semi else f"{teams.get('third', 'Hạng 3')} vs {teams.get('fourth', 'Hạng 4')} · Chưa đá")
            if final:
                final_text = f"{final['home']} {final['hg']}–{final['ag']} {final['away']}"
                if final.get("pens"): final_text += f" · Pen {final['pens']['home']}–{final['pens']['away']}"
            elif semi and semi.get("winner"):
                final_text = f"{teams.get('second', 'Hạng 2')} vs {semi['winner']} · Chờ chung kết"
            else:
                final_text = f"{teams.get('second', 'Hạng 2')} vs đội thắng bán kết · Chưa xác định"
            winner_text = (f"\n\nĐỘI GIÀNH SUẤT LÊN HẠNG: {summary['winner']}"
                           if summary.get("winner") else "\n\nChưa xác định đội thắng play-off.")
            text = (f"{TIER_LABELS[tier]} · PLAY-OFF LÊN HẠNG\n\n"
                    f"VÉ LÊN HẠNG 1 · TRỰC TIẾP\n{teams.get('first', 'Chờ BXH giai đoạn 1')}\n\n"
                    f"BÁN KẾT · HẠNG 3 VS HẠNG 4\n{semi_text}\n\n"
                    f"CHUNG KẾT · HẠNG 2 VS ĐỘI THẮNG BÁN KẾT\n{final_text}{winner_text}\n\n"
                    "Thể thức: một trận loại trực tiếp; nếu hòa sau 90 phút sẽ đá hiệp phụ và luân lưu.")
            if tier == "fc1":
                relegation = self.game.relegation_playoff_summary()
                relegation_teams = relegation.get("teams", {})
                leg1, leg2 = relegation.get("matches", {}).get("leg1"), relegation.get("matches", {}).get("leg2")
                def leg_text(match, home, away):
                    return (f"{match.get('home')} {match.get('hg')}–{match.get('ag')} {match.get('away')} · {match.get('date')}"
                            if match else f"{home or '—'} vs {away or '—'} · Chưa đá")
                aggregate = relegation.get("aggregate") or {}
                aggregate_text = (f"Tổng tỷ số: {relegation_teams.get('sixth')} {aggregate.get('sixth', 0)}–"
                                  f"{aggregate.get('seventh', 0)} {relegation_teams.get('seventh')}"
                                  if aggregate else "Tổng tỷ số: chờ hoàn tất hai lượt")
                text += ("\n\nFC1 · PLAY-OFF TRỤ HẠNG (HẠNG 6 VS HẠNG 7)\n\n"
                         f"Lượt đi · Hạng 7 sân nhà\n{leg_text(leg1, relegation_teams.get('seventh'), relegation_teams.get('sixth'))}\n\n"
                         f"Lượt về · Hạng 6 sân nhà\n{leg_text(leg2, relegation_teams.get('sixth'), relegation_teams.get('seventh'))}\n\n"
                         f"{aggregate_text}\n"
                         f"Ở lại FC1: {relegation.get('winner') or 'Chưa xác định'}\n"
                         f"Xuống FC2: {relegation.get('loser') or 'Chưa xác định'}\n"
                         "Hạng 8 xuống FC2 trực tiếp.")
            self._set_text(self.playoff_texts[tier], text)
        self.refresh_fixtures()
        self._refresh_overview_week_panel()
        self.refresh_squad(); self.refresh_cup(); self.refresh_c1(); self.refresh_ranking(); self.refresh_history(); self.refresh_development(); self.refresh_competition_leaderboards()

    # ── Thông báo, hộp thoại ────────────────────────────────────────────────

    def toast(self, text, kind="info"):
        """Thông báo nhỏ ở góc dưới phải, tự biến mất sau vài giây (không chặn thao tác)."""
        self._dismiss_toast()
        color = {"info": TURF, "warn": GOLD, "error": RED}.get(kind, TURF)
        box = tk.Frame(self.root, bg=RAISED, highlightthickness=1, highlightbackground=LINE_2, cursor="hand2")
        tk.Frame(box, bg=color, width=4).pack(side="left", fill="y")
        label = tk.Label(box, text=text, justify="left", anchor="w", wraplength=380, font=F(10),
                         bg=RAISED, fg=TEXT, padx=14, pady=10)
        label.pack(side="left")
        for widget in (box, label):
            widget.bind("<Button-1>", lambda _e: self._dismiss_toast())
        box.place(relx=1.0, rely=1.0, x=-24, y=-24, anchor="se")
        box.lift()
        self._toast = box
        self._toast_job = self.root.after(4500, self._dismiss_toast)

    def _dismiss_toast(self):
        if self._toast_job is not None:
            try: self.root.after_cancel(self._toast_job)
            except tk.TclError: pass
            self._toast_job = None
        if self._toast is not None:
            try: self._toast.destroy()
            except tk.TclError: pass
            self._toast = None

    def _busy(self, on=True):
        try:
            self.root.configure(cursor="watch" if on else "")
            self.root.update_idletasks()
        except tk.TclError:
            pass

    def _center_dialog(self, top, width, height):
        self.root.update_idletasks()
        screen_w, screen_h = top.winfo_screenwidth(), top.winfo_screenheight()
        width, height = min(width, screen_w - 40), min(height, screen_h - 100)
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 3)
        top.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

    @staticmethod
    def _round_rect(canvas, x1, y1, x2, y2, r=8, **kwargs):
        points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                  x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return canvas.create_polygon(points, smooth=True, splinesteps=12, **kwargs)

    @staticmethod
    def _draw_split_bar(canvas, home_value, away_value):
        canvas.delete("all")
        width = max(canvas.winfo_width(), 10)
        try:
            h, a = float(home_value), float(away_value)
        except (TypeError, ValueError):
            h = a = 0.0
        share = h / (h + a) if (h + a) > 0 else 0.5
        gap = 3
        split = int((width - gap) * share)
        canvas.create_rectangle(0, 0, split, 6, fill=HOME_COL, outline="")
        canvas.create_rectangle(split + gap, 0, width, 6, fill=AWAY_COL, outline="")

    # ── Thao tác ────────────────────────────────────────────────────────────

    def _sync_season_action_buttons(self):
        """Keep manual simulation controls aligned with the active career state."""
        running = bool(getattr(self, "_full_season_running", False))
        season_done = bool(self.game.s.get("season_done"))
        if hasattr(self, "btn_next"):
            self.btn_next.set_enabled(not running and not season_done)
        if hasattr(self, "btn_next_season"):
            self.btn_next_season.set_enabled(not running and season_done)
        button = getattr(self, "btn_full_season", None)
        if button is not None:
            button.configure(text="⏸ Dừng mô phỏng" if running else "⏩ Chạy hết mùa")
            button.set_enabled(running or not season_done)

    def on_full_season(self):
        """Run every remaining slot after explicit confirmation; never auto-advance season."""
        if getattr(self, "_full_season_running", False):
            self._full_season_cancel_requested = True
            if hasattr(self, "btn_full_season"):
                self.btn_full_season.configure(text="Đang dừng…")
                self.btn_full_season.set_enabled(False)
            self.toast("Sẽ dừng sau slot đang xử lý. Các slot đã hoàn tất vẫn được lưu.", "warn")
            return
        if self.game.s.get("season_done"):
            self.toast("Mùa hiện tại đã hoàn tất. Hãy chủ động chọn Mùa tiếp theo.", "info")
            return
        active = getattr(self, "_active_live_window", None)
        if active is not None:
            try:
                if active.winfo_exists():
                    self.toast("Hãy đóng cửa sổ Live Match trước khi chạy hết mùa.", "warn")
                    return
            except tk.TclError:
                pass
        done, total = self.game.slot_progress()
        remaining = max(0, int(total) - int(done))
        season_label = self.game.season_label()
        confirmed = messagebox.askyesno(
            "Chạy hết mùa",
            f"Bạn muốn mô phỏng toàn bộ {remaining} slot còn lại của mùa {season_label}?\n\n"
            "Các trận đã đá sẽ được giữ nguyên; chỉ trận/slot còn thiếu mới được xử lý. "
            "Career được lưu sau từng slot.\n\n"
            "Khi mùa kết thúc, chương trình sẽ dừng tại màn hình tổng kết. "
            "Không tự động tạo mùa tiếp theo.",
            parent=self.root,
        )
        if not confirmed:
            return
        self._full_season_running = True
        self._full_season_cancel_requested = False
        self._full_season_start_done = int(done)
        self._full_season_total_slots = int(total)
        self._sync_season_action_buttons()
        self._busy(True)
        self.status_var.set(f"Đang chạy hết mùa {season_label}…")
        self.toast(f"Bắt đầu mô phỏng {remaining} slot còn lại của mùa {season_label}.")
        self._schedule_full_season_step(20)

    def _schedule_full_season_step(self, delay_ms=20):
        if not getattr(self, "_full_season_running", False):
            return
        try:
            self._full_season_after_id = self.root.after(
                max(1, int(delay_ms)), self._run_full_season_step
            )
        except tk.TclError:
            self._full_season_after_id = None
            self._full_season_running = False
            self._busy(False)

    def _run_full_season_step(self):
        """Advance exactly one remaining slot, checkpoint, update progress, then yield to Tk."""
        self._full_season_after_id = None
        if not getattr(self, "_full_season_running", False):
            return
        if getattr(self, "_full_season_cancel_requested", False):
            self._finish_full_season(cancelled=True)
            return
        try:
            if self.game.s.get("season_done"):
                self._finish_full_season(completed=True)
                return
            result = self.game.advance_slot(persist=True)
            self._record_player_careers()
            done, total = self.game.slot_progress()
            total = max(int(total), 1)
            done = int(done)
            self.progress.configure(maximum=total, value=done)
            percent = round(100 * done / total)
            self.progress_var.set(f"{done}/{total} slot ({percent}%)")
            season_label = self.game.season_label()
            slot = result.get("slot") or {}
            slot_label = slot.get("label") or slot.get("id") or "Slot kế tiếp"
            self.status_var.set(f"Đang chạy hết mùa {season_label} · {done}/{total} slot · {slot_label}")
            if result.get("finished") or self.game.s.get("season_done"):
                self._finish_full_season(completed=True, result=result)
                return
            # Let Tk process repainting and the user's stop request between slots.
            self._schedule_full_season_step(25)
        except Exception as exc:
            self._finish_full_season(error=exc)

    def _finish_full_season(self, completed=False, cancelled=False, result=None, error=None):
        """Restore controls and refresh all pages after completion, cancellation or failure."""
        after_id = getattr(self, "_full_season_after_id", None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self._full_season_after_id = None
        self._full_season_running = False
        self._full_season_cancel_requested = False
        self._busy(False)
        try:
            self.refresh_all()
        except Exception:
            self._write_gui_log("Full-season final refresh failed", traceback.format_exc())
        self._sync_season_action_buttons()
        done, total = self.game.slot_progress()
        advanced = max(0, int(done) - int(getattr(self, "_full_season_start_done", done)))
        if error is not None:
            self._write_gui_log("Full-season simulation failed", traceback.format_exc())
            messagebox.showerror(
                "Mô phỏng mùa bị gián đoạn",
                f"Đã xử lý {advanced} slot trước khi gặp lỗi:\n{error}\n\n"
                "Các slot hoàn tất trước đó đã được lưu. Hãy kiểm tra log để biết chi tiết.",
                parent=self.root,
            )
        elif cancelled:
            self.toast(f"Đã dừng mô phỏng sau {advanced} slot. Career đã lưu đến slot hoàn tất gần nhất.", "warn")
            self.status_var.set(f"Đã dừng mô phỏng · {done}/{total} slot")
        elif completed or self.game.s.get("season_done"):
            record = (result or {}).get("record") or (self.game.s.get("history") or [{}])[-1]
            label = record.get("label", self.game.season_label()) if isinstance(record, dict) else self.game.season_label()
            self.toast(f"Đã mô phỏng và hoàn tất mùa {label}. Không tự động chuyển mùa.")
            self.status_var.set(f"Đã chốt mùa {label} · {done}/{total} slot")

    def on_next(self):
        if getattr(self, "_full_season_running", False):
            self.toast("Đang chạy hết mùa. Hãy dừng batch trước khi mô phỏng riêng một slot.", "warn")
            return
        try:
            self._busy(True)
            result = self.game.advance_slot()
            self._record_player_careers()
            self.refresh_all()
            self._busy(False)
            self.toast(result.get("message", "Đã chạy slot."))
        except Exception as exc:
            self._busy(False)
            messagebox.showerror("Lỗi mô phỏng", str(exc))


    def on_next_season(self):
        if getattr(self, "_full_season_running", False):
            self.toast("Chưa thể tạo mùa mới khi batch mô phỏng đang chạy.", "warn")
            return
        try:
            self._busy(True)
            self.game.next_season()
            self.refresh_all()
            self._busy(False)
            self.toast(f"Đã tạo mùa {self.game.season_label()}.")
        except Exception as exc:
            self._busy(False)
            messagebox.showerror("Không thể tạo mùa mới", str(exc))

    def on_reset_simulation(self):
        """Career reset is unavailable in spectator mode."""
        self.toast("Reset career đã bị khóa trong chế độ khán giả.", "info")

    def on_save(self):
        """Career checkpoints are persisted automatically by the game engine."""
        self.toast("Career được engine tự động lưu.", "info")

    def on_export(self):
        """Manual export controls are not exposed in spectator mode."""
        self.toast("Chế độ khán giả chỉ cung cấp chức năng theo dõi trực tiếp.", "info")

    def _start_startup_google_sheets_sync(self):
        """Run deferred startup sync only after the GUI and its data views are ready."""
        if not getattr(self.game, "google_sheets_startup_sync_pending", False):
            return
        self.game.google_sheets_startup_sync_pending = False
        # Startup sync is text-only. Images are uploaded only by the manual Sync button.
        result = self.game.sync_text_to_google_sheets(wait=False)
        status = result.get("status")
        if status in ("setup_required", "configuration_error", "snapshot_error"):
            self.google_sheets_status_var.set("Google Sheets: Cần thiết lập" if status != "snapshot_error"
                                              else "Google Sheets: Lỗi tạo dữ liệu")
        elif status == "queued":
            self.google_sheets_status_var.set("Google Sheets: Đang đồng bộ…")
            self.root.after(1200, lambda: self._poll_google_sheets_status(0))

    def _capture_google_sheets_screenshots(self, season, season_label):
        """Capture the actual visible content of the league/Cup/C1 child views.

        Tk widgets can only be safely changed on the main thread. The capture routine
        temporarily switches the main content page/segmented view, captures it, then
        restores the user's original page and selected tabs.
        """
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Screenshot giao diện phải được chụp trên Tk main thread.")
        if not self.root.winfo_exists() or not self.root.winfo_viewable() or self.root.state() == "iconic":
            raise RuntimeError("Cửa sổ Super League cần đang hiển thị (không thu nhỏ) để chụp screenshot.")
        try:
            from PIL import ImageGrab, ImageStat
        except ImportError as exc:
            raise RuntimeError("Thiếu Pillow. Chạy: pip install -r requirements-google-sheets.txt") from exc

        # Make sure Treeview/Canvas data reflects the latest saved game state.
        self.root.lift()
        self.refresh_all()
        original_page = self.current_page
        original_tabs = {
            "premier": self.league_segments["premier"]._current,
            "fc1": self.league_segments["fc1"]._current,
            "fc2": self.league_segments["fc2"]._current,
            "cup": self.cup_segmented._current,
            "c1": self.c1_segmented._current,
        }
        visible_dialogs = []
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                try:
                    if child.winfo_exists() and child.winfo_viewable():
                        visible_dialogs.append(child)
                        child.withdraw()
                except tk.TclError:
                    pass

        capture_plan = (
            ("cup_bracket", "cup", "bracket"),
            ("c1_playin", "c1", "playin"),
            ("c1_groups", "c1", "groups"),
            ("c1_knockout", "c1", "knockout"),
        )
        import tempfile
        capture_dir = Path(tempfile.gettempdir()) / "SuperLeague_google_sheets_captures"
        capture_dir.mkdir(parents=True, exist_ok=True)
        output = {}
        capture_diagnostics = {
            "season": int(season),
            "season_label": str(season_label),
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "window_state": str(self.root.state()),
            "screen_px": [int(self.root.winfo_screenwidth()), int(self.root.winfo_screenheight())],
            "captures": {},
        }
        actions_were_packed = bool(self.page_header_actions and self.page_header_actions.winfo_manager())
        if actions_were_packed:
            self.page_header_actions.pack_forget()
        try:
            for key, page_key, tab_key in capture_plan:
                self.show_page(page_key, loading=False)
                if page_key in ("premier", "fc1", "fc2"):
                    self.league_segments[page_key].select(tab_key)
                elif page_key == "cup":
                    self.cup_segmented.select(tab_key)
                else:
                    self.c1_segmented.select(tab_key)
                self.root.update_idletasks()
                self.root.update()

                x = self.content_main.winfo_rootx()
                y = self.page_title_label.winfo_rooty()
                width = self.content_main.winfo_width()
                bottom = self.page_host.winfo_rooty() + self.page_host.winfo_height()
                height = bottom - y
                if width < 100 or height < 100:
                    raise RuntimeError(f"View {key} chưa được layout đầy đủ để chụp ảnh.")
                image = ImageGrab.grab(bbox=(x, y, x + width, y + height))
                # macOS may return a blank frame when Screen Recording permission is missing.
                if max(ImageStat.Stat(image.convert("RGB")).stddev) < 2.0:
                    raise RuntimeError(
                        "Screenshot gần như trống. Hãy cấp quyền Screen Recording cho Terminal "
                        "(System Settings → Privacy & Security), rồi mở lại Terminal và ứng dụng."
                    )
                # Use a conservative cap so full-screen captures still upload quickly.
                path = capture_dir / f"season_{int(season)}_{key}.jpg"
                source_size = [int(image.width), int(image.height)]
                compressed = compress_pil_image(
                    image, max_side=1200, quality=78, max_bytes=750_000
                )
                path.write_bytes(compressed)
                from PIL import Image as PILImage
                from io import BytesIO
                with PILImage.open(BytesIO(compressed)) as final_image:
                    compressed_size = [int(final_image.width), int(final_image.height)]
                capture_diagnostics["captures"][key] = {
                    "source_px": source_size,
                    "compressed_px": compressed_size,
                    "compressed_bytes": len(compressed),
                }
                output[key] = str(path)
        finally:
            # Restore segmented selections first, then the top-level page.
            self.league_segments["premier"].select(original_tabs["premier"] or "stage1")
            self.league_segments["fc1"].select(original_tabs["fc1"] or "standings")
            self.league_segments["fc2"].select(original_tabs["fc2"] or "standings")
            self.cup_segmented.select(original_tabs["cup"] or "bracket")
            self.c1_segmented.select(original_tabs["c1"] or "playin")
            self.show_page(original_page or "dashboard", loading=False)
            if actions_were_packed:
                self.page_header_actions.pack(side="right")
            self.root.update_idletasks()
            self.root.update()
            for dialog in visible_dialogs:
                try:
                    if dialog.winfo_exists():
                        dialog.deiconify()
                        dialog.lift()
                except tk.TclError:
                    pass
        manifest_path = capture_dir / f"season_{int(season)}_capture_manifest.json"
        manifest_tmp = manifest_path.with_suffix(".tmp")
        manifest_tmp.write_text(
            json.dumps(capture_diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest_tmp.replace(manifest_path)
        return output

    def on_google_sheets_sync(self):
        """Manual sync is disabled; configured background sync remains autonomous."""
        self.toast("Đồng bộ được quản lý tự động theo cấu hình hệ thống.", "info")

    def on_google_sheets_history_sync(self):
        """Manual history sync is disabled in spectator mode."""
        self.toast("Đồng bộ lịch sử được quản lý tự động theo cấu hình hệ thống.", "info")

    def _poll_google_sheets_status(self, attempt=0):
        try:
            status = self.game.google_sheets_status()
            state = status.get("status", "not_synced")
            text = {
                "synced": "Google Sheets: Đã đồng bộ",
                "queued": "Google Sheets: Đang chờ đồng bộ",
                "retrying": "Google Sheets: Đang thử lại",
                "setup_required": "Google Sheets: Cần thiết lập",
                "snapshot_error": "Google Sheets: Lỗi tạo ảnh",
                "error": "Google Sheets: Lỗi đồng bộ",
                "disabled": "Google Sheets: Đang tắt",
            }.get(state, "Google Sheets: Chưa đồng bộ")
            self.google_sheets_status_var.set(text)
            if state in ("queued", "retrying") and attempt < 60:
                self.root.after(1500, lambda: self._poll_google_sheets_status(attempt + 1))
            elif state == "synced":
                if status.get("sync_type") == "season_history":
                    self.toast(f"Đã đồng bộ lịch sử {status.get('history_count', 0)} mùa giải lên Google Sheets.")
                else:
                    self.toast("Đã đồng bộ dữ liệu lên Google Sheets.")
            elif state == "retrying":
                self.toast("Google Sheets đang lỗi kết nối; ứng dụng sẽ tự thử lại.", "warn")
            elif state in ("error", "snapshot_error"):
                self.toast(status.get("message", "Đồng bộ Google Sheets thất bại."), "warn")
        except Exception:
            pass

    def _show_season_summary(self, record):
        top = tk.Toplevel(self.root)
        top.title(f"Mùa {record['label']} đã kết thúc")
        top.configure(bg=BG)
        top.transient(self.root)
        top.resizable(False, False)
        top.bind("<Escape>", lambda _e: top.destroy())
        pad = tk.Frame(top, bg=BG)
        pad.pack(fill="both", expand=True, padx=28, pady=24)
        tk.Label(pad, text=f"Mùa {record['label']} đã kết thúc", font=F(17, "bold"), bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(pad, text=f"{record['match_count']} trận đã được mô phỏng", font=F(10), bg=BG, fg=MUTED).pack(anchor="w", pady=(2, 16))
        card = make_card(pad)
        card.pack(fill="x")
        for i, (title, champion) in enumerate((("Premier League", record.get("champion_premier")),
                                               ("Cup", record.get("champion_cup")),
                                               ("C1", record.get("champion_c1")))):
            row = tk.Frame(card, bg=SURFACE)
            row.pack(fill="x", padx=18, pady=(14 if i == 0 else 6, 14 if i == 2 else 6))
            tk.Label(row, text=title, font=F(9), bg=SURFACE, fg=MUTED, width=14, anchor="w").pack(side="left")
            tk.Frame(row, bg=GOLD, width=8, height=8).pack(side="left", padx=(0, 8))
            tk.Label(row, text=champion or "—", font=F(13, "bold"), bg=SURFACE, fg=TEXT).pack(side="left")
        foot = tk.Frame(pad, bg=BG)
        foot.pack(fill="x", pady=(18, 0))
        FlatButton(foot, "Đóng", top.destroy, kind="primary").pack(side="right")
        top.update_idletasks()
        self._center_dialog(top, 460, top.winfo_reqheight())
        top.focus_set()

    def _show_match_report(self, match):
        """Mở báo cáo theo đúng schema thống kê WNL: đội, từng cầu thủ và timeline."""
        display_match = dict(match)
        detailed_stats = display_match.get("advanced_stats")
        player_stats = display_match.get("player_stats")
        # Một vài save cũ chỉ lưu bundle nén. Khôi phục số liệu từ bundle nếu có.
        if (not detailed_stats or not player_stats) and display_match.get("live_bundle"):
            try:
                prep = match_adapter.restore_live_bundle(display_match.get("live_bundle")) or {}
                det = prep.get("det") or {}
                if not detailed_stats:
                    detailed_stats = det.get("t")
                if not player_stats:
                    player_stats = det.get("p")
                display_match["advanced_stats"] = detailed_stats
                display_match["player_stats"] = player_stats
            except Exception:
                # Không làm hỏng chức năng xem báo cáo khi bundle cũ không còn tương thích.
                pass

        report = match_adapter.report_for_match(
            display_match, self.game.s.get("players", {}), self.game.s.get("match_refs", []))
        if not report.get("stats") and not report.get("summary_only"):
            # Chỉ dùng các chỉ số thực sự có trong save legacy; không tự tạo
            # possession 50/50 hay các giá trị 0 khi trận không lưu thống kê.
            raw = display_match.get("stats") or {}
            if isinstance(raw, dict) and any(raw.get(key) is not None
                                             for key in ("shots", "sot", "xg", "possession", "poss")):
                report["stats"] = {
                    key: list(raw[source]) for key, source in (
                        ("shots", "shots"), ("sot", "sot"), ("xg", "xg"),
                        ("poss", "possession" if raw.get("possession") is not None else "poss"))
                    if isinstance(raw.get(source), (list, tuple)) and len(raw[source]) >= 2
                }

        home, away = display_match["home"], display_match["away"]
        score_suffix, note = match_adapter.format_report_result(display_match)
        if not detailed_stats and not report.get("summary_only") and not note:
            note = "Save trận này không lưu thống kê chi tiết."

        top = tk.Toplevel(self.root)
        top.title(f"Báo cáo · {home} {display_match.get('hg')}–{display_match.get('ag')} {away}")
        top.configure(bg=BG)
        top.transient(self.root)
        top.minsize(900, 620)
        top.bind("<Escape>", lambda _e: top.destroy())
        host = tk.Frame(top, bg=BG)
        host.pack(fill="both", expand=True, padx=14, pady=12)
        header = (
            f"{display_match.get('comp', 'Trận đấu')} · {display_match.get('round', '')}",
            str(display_match.get("date", "")),
            "Sân trung lập" if display_match.get("neutral") else f"Sân nhà: {home}",
            home, away,
            f"{display_match.get('hg', 0)} – {display_match.get('ag', 0)}{score_suffix}",
            note,
        )
        report_widget = match_report_ui.build_report(host, header, report)
        report_widget.pack(fill="both", expand=True)
        top.update_idletasks()
        self._center_dialog(top, 1220, 840)
        top.focus_set()

    def _write_gui_log(self, heading, detail):
        """Persist GUI startup/refresh diagnostics so errors are visible even on double-click launch."""
        try:
            import tempfile
            log_dir = Path(tempfile.gettempdir())
            log_path = log_dir / "SuperLeague_gui_startup.log"
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] {heading}\n")
                log.write(f"Python: {platform.python_version()} | Tk: {self.root.tk.call('info', 'patchlevel')}\n")
                log.write(f"Screen: {self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}\n")
                log.write(f"Working directory: {Path.cwd()}\n")
                log.write(str(detail) + "\n")
            return log_path
        except Exception:
            return None

    def _initial_refresh(self):
        overlay = progress = None
        try:
            dashboard_frame = self.pages.get("dashboard", (None,))[0]
            if dashboard_frame is not None:
                overlay, progress = self._make_loading_overlay(dashboard_frame, "Đang chuẩn bị dữ liệu mùa giải…")
                self.root.update_idletasks()
            self.refresh_all()
            self.status_var.set("Chế độ khán giả · Chờ người dùng chọn slot hoặc trận để mô phỏng")
        except Exception as exc:
            diagnostic = traceback.format_exc()
            log_path = self._write_gui_log("Initial GUI refresh failed", diagnostic)
            print("Không tải được dữ liệu GUI:\n" + diagnostic)
            self.status_var.set("Không tải được dữ liệu — xem log Super League trong thư mục tạm")
            message = f"Cửa sổ đã mở nhưng không tải được dữ liệu.\n\nLỗi: {exc}"
            if log_path:
                message += f"\n\nChi tiết lỗi: {log_path}"
            try:
                messagebox.showerror("Lỗi khởi tạo giao diện", message, parent=self.root)
            except tk.TclError:
                pass
        finally:
            if progress is not None:
                try:
                    progress.stop()
                    if overlay is not None and overlay.winfo_exists():
                        overlay.destroy()
                except tk.TclError:
                    pass



    def _report_callback_exception(self, exc_type, exc_value, exc_traceback):
        diagnostic = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        log_path = self._write_gui_log("Tkinter callback failed", diagnostic)
        print(diagnostic)
        try:
            message = f"Một thao tác giao diện gặp lỗi: {exc_value}"
            if log_path:
                message += f"\n\nChi tiết lỗi: {log_path}"
            messagebox.showerror("Lỗi giao diện", message, parent=self.root)
        except tk.TclError:
            pass

    def reset_fixture_filters(self):
        self.filter_comp.set("Tất cả")
        self.filter_team.set("Tất cả")
        self.filter_period.set("Toàn bộ lịch")
        self.filter_status.set("Tất cả")
        self.filter_venue.set("Tất cả")
        self.refresh_fixtures()

    def _fixture_reference_date(self):
        return match_news.reference_date_from_state(self.game.s)

    def _date_in_fixture_period(self, date_value):
        period = self.filter_period.get() if hasattr(self, "filter_period") else "Toàn bộ lịch"
        if period == "Toàn bộ lịch":
            return True
        focus = self._fixture_reference_date()
        if period == "Tuần mô phỏng":
            start = focus - timedelta(days=focus.weekday())
            end = start + timedelta(days=6)
        elif period == "Tháng mô phỏng":
            start = focus.replace(day=1)
            if focus.month == 12:
                next_month = date(focus.year + 1, 1, 1)
            else:
                next_month = date(focus.year, focus.month + 1, 1)
            end = next_month - timedelta(days=1)
        elif period == "7 ngày tới":
            start, end = focus, focus + timedelta(days=6)
        else:
            return True
        return start <= date.fromisoformat(str(date_value)) <= end

    def refresh_fixtures(self):
        comp = self.filter_comp.get() if hasattr(self, "filter_comp") else "Tất cả"
        team = self.filter_team.get() if hasattr(self, "filter_team") else "Tất cả"
        status = self.filter_status.get() if hasattr(self, "filter_status") else "Tất cả"
        venue = self.filter_venue.get() if hasattr(self, "filter_venue") else "Tất cả"
        next_slot = self.game.next_slot()
        next_slot_id = next_slot.get("id") if next_slot else None
        rows = []

        def passes_filters(comp_name, home_name, away_name):
            if comp != "Tất cả":
                if comp == "C1" and comp_name not in ("C1", "C1 Play-in"):
                    return False
                if comp != "C1" and comp_name != comp:
                    return False
            if team != "Tất cả" and team not in (home_name, away_name):
                return False
            if team != "Tất cả" and venue == "Sân nhà" and home_name != team:
                return False
            if team != "Tất cả" and venue == "Sân khách" and away_name != team:
                return False
            return True

        # Played rows open read-only stadium replay when the saved live bundle exists.
        for match in self.game.s.get("matches", []):
            if passes_filters(match.get("comp", ""), match.get("home", ""), match.get("away", "")):
                row = dict(match)
                row["_played"] = bool(match.get("played", True))
                row["_eligible"] = False
                row["_tier"] = None
                rows.append(row)

        # Every fixture in the current next slot (league, Cup or C1) is individually playable.
        # Opening one only creates an in-memory preview; the result is saved at full time.
        if next_slot is not None:
            for spec in self.game.slot_fixture_specs(next_slot, include_played=False):
                if not passes_filters(spec.get("comp", ""), spec["home"], spec["away"]):
                    continue
                meta = spec.get("_meta", {})
                rows.append({
                    **spec, "id": None, "hg": None, "ag": None, "winner": None,
                    "played": False, "pens": None, "_played": False,
                    "_eligible": True, "_tier": meta.get("tier"),
                    "_spec_key": spec["spec_key"], "_slot_id": spec.get("slot_id"),
                })

        # Future league rounds remain visible but disabled until they become the next slot.
        for slot in self.game.s.get("slots", []):
            if slot.get("played") or slot.get("id") == next_slot_id:
                continue
            if slot.get("kind") == "league_phase2":
                for spec in self.game.future_phase2_fixtures(slot):
                    if not passes_filters(spec.get("comp", ""), spec["home"], spec["away"]):
                        continue
                    rows.append({**spec, "id": None, "hg": None, "ag": None, "winner": None,
                                 "played": False, "pens": None, "_played": False, "_eligible": False,
                                 "_tier": spec.get("_meta", {}).get("tier"), "_slot_id": slot.get("id"),
                                 "_spec_key": spec.get("spec_key")})
                continue
            if slot.get("kind") != "league":
                continue
            round_index = int(slot["round"]) - 1
            tiers = (slot["tier"],) if slot.get("tier") in TIERS else TIERS
            for tier in tiers:
                stage = f"league_{tier}_{round_index + 1}"
                comp_name = TIER_LABELS[tier]
                played_pairs = {(m.get("home"), m.get("away")) for m in self.game.s.get("matches", [])
                                if m.get("played") and m.get("stage") == stage and m.get("date") == slot["date"]}
                for home_name, away_name in self.game.s["league_fixtures"][tier][round_index]:
                    if (home_name, away_name) in played_pairs:
                        continue
                    if not passes_filters(comp_name, home_name, away_name):
                        continue
                    rows.append({
                        "id": None, "date": slot["date"], "comp": comp_name,
                        "round": f"Vòng {round_index + 1}", "home": home_name, "away": away_name,
                        "hg": None, "ag": None, "winner": None, "played": False, "pens": None,
                        "_played": False, "_eligible": False, "_tier": tier,
                        "_slot_id": slot.get("id"), "_stage": stage,
                    })

        if status == "Đã đá":
            rows = [row for row in rows if row.get("_played")]
        elif status == "Chưa đá":
            rows = [row for row in rows if not row.get("_played")]
        rows = [row for row in rows if self._date_in_fixture_period(row.get("date", ""))]
        # Keep playable fixtures visible at the top even late in a season, when
        # hundreds of historical results would otherwise push them off-screen.
        def fixture_priority(row):
            if row.get("_eligible") and not row.get("_played"):
                return 0
            if row.get("_played"):
                return 1
            return 2
        rows.sort(key=lambda row: (fixture_priority(row), row.get("date", ""),
                                   row.get("comp", ""), row.get("round", ""), row.get("home", "")))

        def values(_index, row):
            played = row.get("_played")
            if played:
                score = f"{row.get('hg')}–{row.get('ag')}"
                if row.get("pens"):
                    score += f" (pen {row['pens']['home']}-{row['pens']['away']})"
                state = row.get("winner") or ("Hòa" if row.get("hg") == row.get("ag") else "Đã đá")
                has_card_data = ("cards_h" in row or "cards_a" in row or bool(row.get("player_stats")))
                if has_card_data:
                    def red_count(side):
                        cards = row.get("cards_" + side)
                        if isinstance(cards, list):
                            return sum(1 for card in cards if str(card.get("c", "")).lower() in ("r", "yr"))
                        stats = (row.get("player_stats") or {}).get(side, {}) or {}
                        return sum(int((item or {}).get("rc", 0) or 0) for item in stats.values())
                    red = f"🟥 {red_count('h')}–{red_count('a')}"
                else:
                    red = "—"
            else:
                score = "—"
                red = "—"
                state = "▶ Sẵn sàng" if row.get("_eligible") else "Chưa đến lượt"
            return (row.get("date", ""), row.get("comp", ""), row.get("round", ""),
                    row.get("home", ""), score, red, row.get("away", ""), state)

        def tags(_index, row):
            if row.get("_eligible") and not row.get("_played"):
                return ("next_match",)
            if not row.get("_played"):
                return ("pending",)
            return ()

        self._set_tree(self.fixture_tree, rows, values, tags)
        self.fixture_rows_by_iid = dict(zip(self.fixture_tree.get_children(), rows))
        self._last_fixture_rows = list(rows)
        self._refresh_fixture_calendar(rows)

        pending_slots = [slot for slot in self.game.s.get("slots", [])
                         if not slot.get("played") and self._date_in_fixture_period(slot.get("date", ""))]
        slot_rows = []
        for slot in pending_slots:
            if slot.get("id") == next_slot_id:
                total = len(self.game.slot_fixture_specs(slot, include_played=True))
                remaining = len(self.game.slot_fixture_specs(slot, include_played=False))
                played_count = total - remaining
                state_text = f"{played_count}/{total} trận" if played_count else "Sẵn sàng"
            else:
                state_text = "Chờ slot trước"
            slot_rows.append({**slot, "_state": state_text})
        self._set_tree(self.slot_tree, slot_rows,
                       lambda _i, row: (row["date"], row["label"], row["kind"], row["_state"]))

    def on_fixture_click(self, event=None):
        """Open a saved match report, or offer simulation for the next eligible fixture."""
        if getattr(self, "_full_season_running", False):
            self.toast("Đang chạy hết mùa. Hãy đợi hoàn tất hoặc dừng mô phỏng trước khi mở trận.", "warn")
            return
        if event is None:
            return
        row_id = self.fixture_tree.identify_row(event.y)
        if not row_id:
            return
        self.fixture_tree.selection_set(row_id)
        self.fixture_tree.focus(row_id)
        row = getattr(self, "fixture_rows_by_iid", {}).get(row_id)
        if not row:
            return
        if row.get("_played"):
            self._show_match_report(row)
            return
        if not row.get("_eligible"):
            self.toast("Chỉ trận chưa đá thuộc slot kế tiếp mới mở được Live Match.", "warn")
            return
        self._show_fixture_simulation_options(row)

    def _show_fixture_simulation_options(self, fixture):
        """Choose one live fixture or the same-competition fixtures sharing its kickoff."""
        slot = self.game.next_slot()
        fixture_slot_id = fixture.get("_slot_id", fixture.get("slot_id"))
        fixture_spec_key = fixture.get("_spec_key", fixture.get("spec_key"))
        if not slot or slot.get("id") != fixture_slot_id:
            self.toast("Lịch đã thay đổi. Hãy chọn lại trận thuộc slot kế tiếp.", "warn")
            self.refresh_fixtures()
            return
        remaining = self.game.slot_fixture_specs(slot, include_played=False)
        competition_fixtures = [item for item in remaining if item.get("comp") == fixture.get("comp")]
        remaining_count = len(competition_fixtures)
        if not any(item.get("spec_key") == fixture_spec_key for item in remaining):
            self.toast("Trận này không còn trong danh sách chưa đá.", "warn")
            self.refresh_fixtures()
            return
        other_count = max(0, remaining_count - 1)
        top = tk.Toplevel(self.root)
        top.title("Chọn phạm vi mô phỏng")
        top.configure(bg=BG)
        top.transient(self.root)
        top.resizable(False, False)
        top.bind("<Escape>", lambda _e: top.destroy())
        shell = tk.Frame(top, bg=BG)
        shell.pack(fill="both", expand=True, padx=24, pady=20)
        tk.Label(shell, text="Bạn muốn mô phỏng thế nào?", font=F(16, "bold"), bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(shell, text=f"{fixture['home']}  vs  {fixture['away']}", font=F(12, "bold"), bg=BG, fg=GOLD).pack(anchor="w", pady=(8, 2))
        tk.Label(shell, text=f"{fixture.get('comp', '')} · {fixture.get('round', '')} · {fixture.get('date', '')}",
                 font=F(9), bg=BG, fg=MUTED).pack(anchor="w")
        card = make_card(shell)
        card.pack(fill="x", pady=(16, 14))
        body = tk.Frame(card, bg=SURFACE)
        body.pack(fill="x", padx=14, pady=12)
        tk.Label(body, text=f"{fixture.get('comp', 'Giải đấu')} · còn {remaining_count} trận chưa đá cùng giờ trong giải này.",
                 font=F(10, "bold"), bg=SURFACE, fg=TEXT).pack(anchor="w")
        tk.Label(body, text=(f"Trận được chọn sẽ chạy Live Match; {other_count} trận còn lại của cùng giải sẽ chạy đồng thời. Sau khi kết thúc, bạn có thể chọn tab từng trận để xem thống kê. Các giải khác cùng giờ không bị mô phỏng."
                             if other_count else "Đây là trận duy nhất còn lại của giải này trong slot kickoff."),
                 font=F(9), bg=SURFACE, fg=MUTED, wraplength=470, justify="left").pack(anchor="w", pady=(5, 0))
        buttons = tk.Frame(shell, bg=BG)
        buttons.pack(fill="x")
        def choose(batch):
            try:
                top.grab_release()
            except tk.TclError:
                pass
            top.destroy()
            self.open_live_match(fixture, simulate_remaining=batch)
        FlatButton(buttons, "Chỉ mô phỏng trận này", lambda: choose(False), kind="secondary", padx=12).pack(side="left")
        all_button = FlatButton(buttons, f"Trận này + {other_count} trận cùng giải", lambda: choose(True), kind="primary", padx=12)
        all_button.pack(side="left", padx=(8, 0))
        if other_count == 0:
            all_button.set_enabled(False)
        FlatButton(buttons, "Hủy", top.destroy, kind="ghost", padx=12).pack(side="right")
        top.update_idletasks()
        self._center_dialog(top, 560, top.winfo_reqheight())
        top.grab_set()
        top.focus_set()

    def open_live_match(self, fixture, simulate_remaining=False):
        if getattr(self, "_full_season_running", False):
            self.toast("Không thể mở Live Match trong khi batch chạy hết mùa đang hoạt động.", "warn")
            return
        fixture_spec_key = fixture.get("_spec_key", fixture.get("spec_key"))
        upcoming_key = ("upcoming", str(fixture_spec_key or ""))
        active = getattr(self, "_active_live_window", None)
        if active is not None:
            try:
                if active.winfo_exists():
                    active.lift()
                    if getattr(self, "_active_live_match_key", None) != upcoming_key:
                        self.toast("Hãy hoàn tất hoặc đóng trận Live Match đang mở trước khi mở trận khác.", "warn")
                    return
            except tk.TclError:
                pass
            self._active_live_window = None
            self._active_live_host = None
            self._active_live_match_key = None
        try:
            preview = self.game.preview_slot_fixture(fixture_spec_key)
            prep = match_adapter.restore_live_bundle(preview["data"].get("live_bundle"))
            if not prep:
                raise RuntimeError("Không thể khôi phục dữ liệu mô phỏng trực tiếp của trận.")
            from sl_match.live import LiveMatch
            from sl_match.live_ui import LivePage

            top = tk.Toplevel(self.root)
            top.title(f"LIVE MATCH · {preview['home']} vs {preview['away']}")
            top.configure(bg=BG)
            screen_w, screen_h = top.winfo_screenwidth(), top.winfo_screenheight()
            window_w = min(1420, max(1080, screen_w - 40))
            window_h = min(900, max(700, screen_h - 80))
            top.geometry(f"{window_w}x{window_h}+{max(0, (screen_w-window_w)//2)}+{max(0, (screen_h-window_h)//2)}")
            top.minsize(min(1080, window_w), min(700, window_h))
            top.transient(self.root)
            container = tk.Frame(top, bg=BG)
            container.pack(fill="both", expand=True)
            entry = {"replay": False, "dirty": False}
            companion_previews = self.game.preview_slot_companions(preview) if simulate_remaining else []
            host = _UpcomingMatchHost(
                self, top, entry, preview, simulate_remaining=simulate_remaining,
                companion_previews=companion_previews,
            )
            live_match = LiveMatch(preview["home"], preview["away"], prep,
                                   neutral=bool(preview.get("neutral")),
                                   knockout=bool(preview.get("force_winner")))
            page = LivePage(host, container, entry, preview, prep, live_match)
            host.page = page
            self._active_live_window = top
            self._active_live_host = host
            self._active_live_match_key = upcoming_key
            top.protocol("WM_DELETE_WINDOW", host.go_back)
            top.grab_set()
            top.lift()
            self.toast(f"Đang mở trận {preview['home']} – {preview['away']}.")
        except Exception as exc:
            self._write_gui_log("Upcoming live match failed", traceback.format_exc())
            messagebox.showerror("Không thể mở Live Match", str(exc), parent=self.root)

    def _club_lineup_snapshot(self, team):
        """Build a deterministic projected XI using current injuries, suspensions and fitness."""
        state = self.game.s
        raw_roster = self.game.team_players(team)
        players = [match_engine_core.make_player(dict(player)) for player in raw_roster]
        dyn_source = state.get("dyn", {})
        status_source = state.get("player_status", {})
        effective = {}
        unavailable = set()
        for player in players:
            pid = player["pid"]
            dyn = dyn_source.get(pid, {}) or {}
            status = status_source.get(pid, {}) or {}
            fit = float(dyn.get("fit", 100.0) or 0.0)
            form = float(dyn.get("form", 0.0) or 0.0)
            effective[pid] = match_engine_core.effective(player, form, fit)
            if int(status.get("inj", 0) or 0) > 0 or int(status.get("susp", 0) or 0) > 0:
                unavailable.add(pid)

        pool = [player for pid, player in effective.items() if pid not in unavailable]
        # Match engine asks for a 14-player pool; in a shortage it brings back the
        # highest-rated unavailable players, matching its emergency fallback.
        if len(pool) < 14:
            unavailable_pool = sorted(
                (player for pid, player in effective.items() if pid in unavailable),
                key=lambda player: -player.get("ovr", 0),
            )
            pool.extend(unavailable_pool[:14 - len(pool)])
        if len(pool) < 11:
            pool = sorted(effective.values(), key=lambda player: -player.get("ovr", 0))
        xi, formation = match_engine_core.select_xi(pool)

        by_group = {
            group: [player for player in xi if player.get("group") == group]
            for group in ("GK", "DEF", "MID", "FWD")
        }
        def average(items, field, default=60.0):
            values = [float(item.get(field, default) or 0.0) for item in items]
            return sum(values) / len(values) if values else float(default)

        outfield = by_group["DEF"] + by_group["MID"] + by_group["FWD"]
        metrics = {
            "Tấn công": average(by_group["FWD"] + by_group["MID"], "att"),
            "Kiểm soát": average(by_group["MID"] + by_group["DEF"], "poss_q"),
            "Phòng ngự": average(by_group["DEF"], "dfn"),
            "Thủ môn": average(by_group["GK"], "gk"),
            "Tốc độ": average(outfield, "spd"),
            "Thể lực": average(xi, "fit0", 100.0),
        }
        metrics = {key: max(0.0, min(100.0, value)) for key, value in metrics.items()}
        coach = (state.get("coaches", {}) or {}).get(team) or {"name": "Chưa cập nhật"}
        return {"xi": xi, "formation": tuple(formation), "metrics": metrics,
                "coach": str(coach.get("name", "HLV")), "unavailable": unavailable}

    def _draw_club_pitch(self, canvas, snapshot):
        """Draw a landscape football pitch at a real-world 105:68 length/width ratio."""
        if not snapshot:
            return
        try:
            width = max(260, int(canvas.winfo_width()))
            height = max(190, int(canvas.winfo_height()))
        except (tk.TclError, ValueError):
            return

        canvas.delete("all")

        # A standard full-size pitch is approximately 105 x 68 metres.
        # Keep this ratio fixed and letterbox the pitch instead of stretching it
        # to the full Canvas width/height.
        pitch_ratio = 105.0 / 68.0
        margin_x, margin_y = 12, 10
        available_w = max(220, width - margin_x * 2)
        available_h = max(150, height - margin_y * 2)
        pitch_w = min(available_w, available_h * pitch_ratio)
        pitch_h = pitch_w / pitch_ratio
        x1 = (width - pitch_w) / 2
        y1 = (height - pitch_h) / 2
        x2, y2 = x1 + pitch_w, y1 + pitch_h
        mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2

        # Grass and alternating mowing stripes, running along the length.
        canvas.create_rectangle(x1, y1, x2, y2, fill="#16452d", outline="#d8e8d8", width=2)
        stripe_count = 10
        for index in range(stripe_count):
            sx1 = x1 + pitch_w * index / stripe_count
            sx2 = x1 + pitch_w * (index + 1) / stripe_count
            if index % 2 == 0:
                canvas.create_rectangle(sx1, y1 + 1, sx2, y2 - 1,
                                        fill="#1b5034", outline="")

        line_color = "#a8c9ad"
        line_width = 2
        canvas.create_rectangle(x1, y1, x2, y2, outline=line_color, width=line_width)
        canvas.create_line(mid_x, y1, mid_x, y2, fill=line_color, width=line_width)

        # Centre circle and spot: dimensions scaled from a 68 m wide pitch.
        centre_r = pitch_h * (9.15 / 68.0)
        spot_r = max(2, pitch_h * 0.009)
        canvas.create_oval(mid_x - centre_r, mid_y - centre_r,
                           mid_x + centre_r, mid_y + centre_r,
                           outline=line_color, width=line_width)
        canvas.create_oval(mid_x - spot_r, mid_y - spot_r,
                           mid_x + spot_r, mid_y + spot_r,
                           fill=line_color, outline="")

        # Penalty areas (16.5 m deep x 40.3 m wide) and goal areas.
        penalty_depth = pitch_w * (16.5 / 105.0)
        penalty_half_h = pitch_h * (40.3 / 68.0) / 2
        goal_depth = pitch_w * (5.5 / 105.0)
        goal_half_h = pitch_h * (18.32 / 68.0) / 2
        for side in ("left", "right"):
            if side == "left":
                px1, px2 = x1, x1 + penalty_depth
                gx1, gx2 = x1, x1 + goal_depth
                goal_x1, goal_x2 = x1 - max(5, pitch_w * 0.012), x1
                penalty_spot_x = x1 + pitch_w * (11.0 / 105.0)
            else:
                px1, px2 = x2 - penalty_depth, x2
                gx1, gx2 = x2 - goal_depth, x2
                goal_x1, goal_x2 = x2, x2 + max(5, pitch_w * 0.012)
                penalty_spot_x = x2 - pitch_w * (11.0 / 105.0)

            canvas.create_rectangle(px1, mid_y - penalty_half_h,
                                    px2, mid_y + penalty_half_h,
                                    outline=line_color, width=line_width)
            canvas.create_rectangle(gx1, mid_y - goal_half_h,
                                    gx2, mid_y + goal_half_h,
                                    outline=line_color, width=line_width)
            canvas.create_rectangle(goal_x1, mid_y - goal_half_h * 0.40,
                                    goal_x2, mid_y + goal_half_h * 0.40,
                                    outline=line_color, width=2)
            canvas.create_oval(penalty_spot_x - spot_r, mid_y - spot_r,
                               penalty_spot_x + spot_r, mid_y + spot_r,
                               fill=line_color, outline="")

        # Projected XI arranged in horizontal tactical lines: goalkeeper on the
        # left, forwards toward the attacking end on the right.
        groups = {group: [] for group in ("GK", "DEF", "MID", "FWD")}
        for player in snapshot.get("xi", []):
            groups.setdefault(player.get("group", "MID"), []).append(player)

        x_factor = {"GK": 0.075, "DEF": 0.27, "MID": 0.52, "FWD": 0.79}
        player_radius = max(8, min(12, pitch_h * 0.045))
        player_fills = {
            "GK": "#b18b3e",
            "DEF": "#32865a",
            "MID": "#347db1",
            "FWD": "#a34f5c",
        }
        for group in ("GK", "DEF", "MID", "FWD"):
            line = groups.get(group, [])
            if not line:
                continue
            count = len(line)
            player_x = x1 + pitch_w * x_factor[group]
            for index, player in enumerate(line):
                player_y = mid_y if count == 1 else (
                    y1 + pitch_h * (0.18 + 0.64 * index / (count - 1))
                )
                canvas.create_oval(
                    player_x - player_radius, player_y - player_radius,
                    player_x + player_radius, player_y + player_radius,
                    fill=player_fills.get(group, "#347db1"),
                    outline="#f0f5ef", width=2,
                )
                short_name = str(player.get("name", "")).split()[-1][:9]
                # Keep labels inside the touchlines and away from the player dot.
                label_y = (player_y + player_radius + 8 if player_y < mid_y
                           else player_y - player_radius - 8)
                canvas.create_text(
                    player_x, label_y, text=short_name,
                    fill="#f4f7f4", font=F(7, "bold"),
                    anchor="center", width=max(44, pitch_w * 0.18),
                )

    def _draw_club_spider(self, canvas, metrics):
        if not metrics:
            return
        try:
            width = max(250, int(canvas.winfo_width()))
            height = max(145, int(canvas.winfo_height()))
        except (tk.TclError, ValueError):
            return
        canvas.delete("all")
        labels = ("Tấn công", "Kiểm soát", "Phòng ngự", "Thủ môn", "Tốc độ", "Thể lực")
        values = [max(0.0, min(100.0, float(metrics.get(label, 0)))) for label in labels]
        cx, cy = width * 0.50, height * 0.48
        radius = min(width * 0.30, height * 0.34)
        angles = [-90 + index * 60 for index in range(6)]
        import math
        axes = [(math.cos(math.radians(angle)), math.sin(math.radians(angle))) for angle in angles]
        for level in (20, 40, 60, 80, 100):
            coords = []
            for ax, ay in axes:
                coords.extend((cx + ax * radius * level / 100, cy + ay * radius * level / 100))
            canvas.create_polygon(*coords, fill="", outline="#34485b", width=1)
        for ax, ay in axes:
            canvas.create_line(cx, cy, cx + ax * radius, cy + ay * radius, fill="#34485b", width=1)
        coords = []
        for value, (ax, ay) in zip(values, axes):
            coords.extend((cx + ax * radius * value / 100, cy + ay * radius * value / 100))
        canvas.create_polygon(*coords, fill="#1c6844", outline=TURF_LIGHT, width=2, stipple="gray50")
        for value, (ax, ay) in zip(values, axes):
            px, py = cx + ax * radius * value / 100, cy + ay * radius * value / 100
            canvas.create_oval(px - 3, py - 3, px + 3, py + 3, fill=GOLD, outline="")
        for label, (ax, ay) in zip(labels, axes):
            lx, ly = cx + ax * (radius + 17), cy + ay * (radius + 15)
            anchor = "center"
            if ax < -0.3: anchor = "e"
            elif ax > 0.3: anchor = "w"
            canvas.create_text(lx, ly, text=label, fill=MUTED, font=F(7, "bold"), anchor=anchor)

    def _refresh_club_status(self, team):
        tier = self.game.s.get("team_tiers", {}).get(team, "fc2")
        table = self.game.league_table(tier)
        pos = next((i + 1 for i, row in enumerate(table) if row.get("team") == team), None)
        self.club_rank_var.set(f"Vị trí hiện tại: #{pos or '—'} · {TIER_LABELS.get(tier, tier)}")

        played = [m for m in self.game.s.get("matches", []) if m.get("played") and team in (m.get("home"), m.get("away"))]
        played.sort(key=lambda m: (str(m.get("date", "")), int(m.get("id", 0) or 0)), reverse=True)
        form = []
        for m in played[:5]:
            hg, ag = int(m.get("hg", 0)), int(m.get("ag", 0))
            if hg == ag:
                form.append("➖")
                continue
            won = (m.get("home") == team and hg > ag) or (m.get("away") == team and ag > hg)
            form.append("✅" if won else "❌")
        self.club_form5_var.set("5 trận gần nhất: " + ("  ".join(form) if form else "Chưa có trận"))

        upcoming = []
        for slot in self.game.s.get("slots", []):
            if slot.get("played"):
                continue
            try:
                specs = self.game.slot_fixture_specs(slot, include_played=False, allow_future=True)
            except (RuntimeError, KeyError, IndexError, ValueError):
                continue
            for spec in specs:
                if team in (spec.get("home"), spec.get("away")):
                    upcoming.append(spec)
        upcoming.sort(key=lambda x: str(x.get("date", "")))
        if not upcoming:
            self.club_next_var.set("Trận kế tiếp: Chưa xác định")
            self.club_h2h_var.set("Đối đầu gần đây: —")
            return
        nxt = upcoming[0]
        opponent = nxt.get("away") if nxt.get("home") == team else nxt.get("home")
        venue = "sân nhà" if nxt.get("home") == team else "sân khách"
        self.club_next_var.set(f"Trận kế tiếp: {nxt.get('date', '—')} · {nxt.get('comp', '—')} · vs {opponent} ({venue})")
        h2h = [m for m in self.game.s.get("matches", []) if m.get("played") and {m.get("home"), m.get("away")} == {team, opponent}]
        h2h.sort(key=lambda m: (str(m.get("date", "")), int(m.get("id", 0) or 0)), reverse=True)
        if h2h:
            bits = [f"{m.get('date','—')} {m.get('home')} {m.get('hg',0)}–{m.get('ag',0)} {m.get('away')}" for m in h2h[:5]]
            self.club_h2h_var.set("Đối đầu gần đây: " + "  |  ".join(bits))
        else:
            self.club_h2h_var.set(f"Đối đầu gần đây với {opponent}: Chưa có dữ liệu")

    def refresh_squad(self):
        if not hasattr(self, "squad_tree"):
            return
        team = self.squad_team.get()
        if team not in self.game.s["rosters"]:
            return
        rows = self.game.team_form(team)
        meta = self.game.s["player_meta"]
        snapshot = self._club_lineup_snapshot(team)
        xi = snapshot["xi"]
        xi_ids = [player.get("pid") for player in xi]
        xi_set = set(xi_ids)
        rows_by_id = {row.get("player_id"): row for row in rows}
        ordered_rows = [rows_by_id[pid] for pid in xi_ids if pid in rows_by_id]
        ordered_rows.extend(row for row in rows if row.get("player_id") not in xi_set)
        self.squad_title.set(
            f"Hạng: {TIER_LABELS[self.game.s['team_tiers'].get(team, 'fc2')]} · {len(rows)} cầu thủ · "
            f"HLV: {snapshot['coach']} · Sơ đồ { '-'.join(str(x) for x in snapshot['formation'])}"
        )
        self._refresh_club_status(team)
        self.club_coach_var.set(f"HLV: {snapshot['coach']}")
        self.club_formation_var.set("Sơ đồ dự kiến: " + "-".join(str(x) for x in snapshot["formation"]))
        self.club_pitch_canvas._club_snapshot = snapshot
        self.club_radar_canvas._club_metrics = snapshot["metrics"]
        self._draw_club_pitch(self.club_pitch_canvas, snapshot)
        self._draw_club_spider(self.club_radar_canvas, snapshot["metrics"])

        player_status = self.game.s.get("player_status", {}) or {}
        season_stats = self.game.s.get("season_stats", {}) or {}
        def squad_values(_index, row):
            pid = row.get("player_id")
            current = player_status.get(pid, {}) or {}
            totals = season_stats.get(pid, {}) or {}
            indicators = []
            if int(current.get("inj", 0) or 0) > 0:
                indicators.append("🩹")
            if int(current.get("yc", 0) or 0) > 0 or int(totals.get("yellow", 0) or 0) > 0:
                indicators.append("🟨")
            if int(totals.get("red", 0) or 0) > 0:
                indicators.append("🟥")
            elif int(current.get("susp", 0) or 0) > 0:
                # Suspension can also come from accumulated yellow cards; do not
                # incorrectly label every suspension as a straight red card.
                indicators.append("⛔")
            badge_text = ("  " + "".join(indicators)) if indicators else ""
            name = ("XI · " if pid in xi_set else "") + str(row.get("name", "—")) + badge_text
            potential = row.get("potential")
            contract = self.game.s.get("contracts", {}).get(pid, {})
            player = self.game.s.get("players", {}).get(pid, {})
            return (row["position"], name, round(row["age"] or 0, 1), row["ovr"],
                    f"{potential:.1f}" if potential is not None else "—",
                    f"{float(player.get('wage', 0)):.2f}",
                    f"{int(contract.get('years_remaining', 0))} năm" if contract else "—",
                    f"{float(player.get('market_value', 0)):.2f}",
                    f"{row['form']:+.2f}", round(row["fit"]), row["apps"], row["goals"],
                    row["assists"], row["avg_rating"] or "—")

        self._set_tree(self.squad_tree, ordered_rows, squad_values,
                       lambda _i, row: ("starting_xi",) if row.get("player_id") in xi_set else (),
                       iid_of=lambda _i, row: str(row.get("player_id") or ""))

        academy = []
        self._academy_lookup = {}
        for entry in self.game.s.get("academy", {}).get(team, []):
            player = entry["player"]
            player_meta = entry["meta"]
            player_id = str(player.get("player_id") or player.get("id") or "")
            iid = f"academy:{player_id}"
            academy.append({"player_id": player_id, "iid": iid, "position": player.get("position"),
                            "name": player.get("common_name"), "age": player_meta.get("age"),
                            "ovr": player.get("overall_rating"), "potential": player_meta.get("potential"),
                            "talent": "★ Siêu sao" if player_meta.get("is_superstar") else "Triển vọng"})
            if player_id:
                self._academy_lookup[iid] = (player, player_meta, team)
        self._set_tree(self.academy_tree, academy, lambda _i, row: (
            row["position"], row["name"], round(row["age"] or 0, 1), row["ovr"],
            f"{row['potential']:.1f}", row["talent"]),
            iid_of=lambda _i, row: row["iid"])
        self.refresh_club_analytics(team)
        self.refresh_club_history(team)
        self._refresh_club_economy_views(team)

    def _refresh_club_economy_views(self, team):
        """Read-only economy/contract/transfer/stadium profile views for the selected club."""
        state = self.game.s
        finance = state.get("club_finance", {}).get(team, {})
        if not finance:
            return
        ids = list(state.get("rosters", {}).get(team, []))
        contracts = state.get("contracts", {})
        active_contracts = [contracts.get(pid, {}) for pid in ids]
        annual_wages = sum(float(c.get("wage_per_year", state.get("players", {}).get(pid, {}).get("wage", 0)))
                           for pid, c in zip(ids, active_contracts))
        wage_budget = float(finance.get("wage_budget", 0))
        ledger = [tx for tx in state.get("finance_ledger", []) if tx.get("teamId") == team]
        current_season = int(state.get("season", 0))
        season_txs = [tx for tx in ledger if int(tx.get("season", -1)) == current_season]
        # Keep the dashboard consistent with the audited season report: owner
        # rescue, opening capital and debt principal are financing movements,
        # not operating turnover/expenses.
        pnl_txs = [tx for tx in season_txs
                   if tx.get("type") not in ("OWNER_INJECTION", "OWNER_DISTRIBUTION", "DEBT_REPAYMENT")
                   and not str(tx.get("relatedId") or "").startswith("finance-init:")]
        revenue = sum(max(0.0, float(tx.get("amount", 0))) for tx in pnl_txs)
        costs = -sum(min(0.0, float(tx.get("amount", 0))) for tx in pnl_txs)
        balance = float(finance.get("balance", 0))
        ffp = state.get("ffp_status", {}).get(team, {})
        ffp_label = f"{ffp.get('status')} · phạt cấm chuyển nhượng còn {ffp.get('transfer_ban_windows_remaining', 0)} cửa sổ" if ffp else "Chưa có kết quả FFP (đợi chốt mùa đầu tiên)"
        if ffp and int(ffp.get("points_deducted", 0)) > 0:
            ffp_label += (f" · trừ {int(ffp['points_deducted'])} điểm mùa "
                          f"{int(ffp.get('point_deduction_effective_season', current_season + 1))}/"
                          f"{int(ffp.get('point_deduction_effective_season', current_season + 1))+1}")
        debt_terms = finance.get("debt_terms", {})
        debt_label = (f"{float(finance.get('debt', 0)):,.3f} M€"
                      + (f" · tái cơ cấu {float(debt_terms.get('annual_interest_rate', 0))*100:.2f}%/năm, "
                         f"còn {int(debt_terms.get('months_remaining', 0))} tháng"
                         if debt_terms.get("status") == "ACTIVE" else ""))
        finance_lines = [
            f"{team} · TÀI CHÍNH MÙA {current_season}/{current_season+1}",
            "=" * 76,
            f"Số dư tiền mặt:                 {balance:,.3f} M€",
            f"Ngân sách chuyển nhượng:       {float(finance.get('transfer_budget', 0)):,.3f} M€",
            f"Quỹ lương năm:                  {wage_budget:,.3f} M€",
            f"Lương đội hình đang cam kết:    {annual_wages:,.3f} M€ ({(annual_wages/wage_budget*100 if wage_budget else 0):.1f}% quỹ lương)",
            f"Nợ:                             {debt_label}",
            f"Hỗ trợ khẩn cấp từ chủ sở hữu: {float(finance.get('owner_support_received', 0)):,.3f} M€",
            f"Vốn đầu tư thường niên:         {float(finance.get('owner_investment_received', 0)):,.3f} M€",
            f"Phân phối cho cổ đông:         {float(finance.get('owner_distribution_paid', 0)):,.3f} M€",
            f"Mục tiêu tiền mặt theo hạng:    {float(finance.get('shareholder_cash_target_m', {'big': 500, 'medium': 250, 'small': 125}.get(str(finance.get('tier', 'small')), 125))):.1f} M€",
            f"Danh tiếng / Fan base:          {float(finance.get('reputation', 0)):.1f} / {int(finance.get('fan_base', 0)):,}",
            f"Doanh thu đã ghi mùa này:       {revenue:,.3f} M€",
            f"Chi phí đã ghi mùa này:         {costs:,.3f} M€",
            f"Lãi/lỗ đã ghi mùa này:          {revenue-costs:,.3f} M€",
            f"FFP:                            {ffp_label}",
            "", "GIAO DỊCH GẦN NHẤT", "-" * 76,
        ]
        transaction_labels = {
            "OWNER_INJECTION": "VỐN CHỦ SỞ HỮU",
            "OWNER_DISTRIBUTION": "PHÂN PHỐI CỔ ĐÔNG",
            "DEBT_REPAYMENT": "TRẢ NỢ GỐC",
            "TRANSFER_IN": "MUA CẦU THỦ",
            "TRANSFER_OUT": "BÁN CẦU THỦ",
            "MATCHDAY": "DOANH THU SÂN",
            "SPONSOR": "TÀI TRỢ",
            "COMMERCIAL": "THƯƠNG MẠI",
            "WAGE": "LƯƠNG",
            "PRIZE": "TIỀN THƯỞNG",
        }
        for tx in ledger[-30:][::-1]:
            amount = float(tx.get("amount", 0))
            tx_type = str(tx.get("type", "OTHER"))
            type_label = transaction_labels.get(tx_type, tx_type)
            finance_lines.append(f"{tx.get('date','—')}  {type_label:<24} {amount:+9.3f} M€  {tx.get('description','')}")
        finance_lines.append("\nGhi chú: các số liệu thay đổi khi mô phỏng qua tháng/trận; số dư và giao dịch lấy từ ledger.")
        self.club_finance_text.configure(state="normal")
        self.club_finance_text.delete("1.0", "end")
        self.club_finance_text.insert("1.0", "\n".join(finance_lines))
        self.club_finance_text.configure(state="disabled")

        contract_rows = []
        for pid in ids:
            player = state.get("players", {}).get(pid, {})
            meta = state.get("player_meta", {}).get(pid, {})
            contract = contracts.get(pid, {})
            contract_rows.append({
                "player_id": pid, "player": player.get("common_name", pid), "pos": player.get("position", "—"),
                "age": round(float(meta.get("age", 0)), 1), "ovr": player.get("overall_rating", "—"),
                "role": contract.get("squad_role", "—"),
                "wage": f"{float(contract.get('wage_per_year', player.get('wage', 0))):.2f}",
                "mv": f"{float(player.get('market_value', 0)):.2f}",
                "years": f"{int(contract.get('years_remaining', 0))} năm" if contract else "—",
                "status": contract.get("status", "Chưa có"),
            })
        contract_rows.sort(key=lambda r: (r["years"] != "1 năm", -float(r["wage"])))
        self.club_contract_tree.delete(*self.club_contract_tree.get_children())
        for index, row in enumerate(contract_rows):
            pid = row.get("player_id")
            if not pid:
                continue
            self.club_contract_tree.insert("", "end", iid=pid, values=(
                row["player"], row["pos"], row["age"], row["ovr"], row["role"],
                row["wage"], row["mv"], row["years"], row["status"]))

        # Market candidates are read-only observer data; AI directors make all deal decisions.
        self.club_market_tree.delete(*self.club_market_tree.get_children())
        market_rows = []
        roster_ids = set(ids)
        for pid, player in state.get("players", {}).items():
            contract = contracts.get(pid, {})
            seller = contract.get("team_id") or player.get("club", "")
            is_free = contract.get("status") in ("EXPIRED", "FREE_AGENT") or pid in set(state.get("free_agents", [])) or not seller
            if seller == team and not is_free:
                continue
            if not is_free and seller not in state.get("rosters", {}):
                continue
            meta = state.get("player_meta", {}).get(pid, {})
            market_rows.append((pid, player.get("common_name", pid), player.get("position", "—"),
                                round(float(meta.get("age", 0)), 1), player.get("overall_rating", "—"),
                                "Cầu thủ tự do" if is_free else seller,
                                f"{float(contract.get('wage_per_year', player.get('wage', 0))):.2f}",
                                f"{float(player.get('market_value', 0)):.2f}",
                                f"{int(contract.get('years_remaining', 0))} năm" if contract else "Tự do",
                                f"{self.game.transfer_market.target_score(team, pid):.1f}"))
        market_rows.sort(key=lambda r: (-float(r[9] or 0), r[5] == "Cầu thủ tự do", -float(r[4] or 0)))
        for index, row in enumerate(market_rows):
            self.club_market_tree.insert("", "end", iid=str(row[0]), values=row,
                                         tags=(f"row_{index % 2}",))
        analysis = self.game.transfer_market.analyze_squad(team)
        needs_label = ", ".join(f"{need['position']} (ưu tiên {need['priority']})" for need in analysis["needs"][:3]) or "đội hình cân bằng"
        self.club_market_hint.set(f"{len(market_rows)} mục tiêu · Nhu cầu: {needs_label} · điểm Đề xuất đã tính khả năng chi trả.")

        transfer_rows = []
        for item in state.get("transfer_history", []):
            if team not in (item.get("from_team"), item.get("to_team")):
                continue
            transfer_rows.append({
                "player_id": str(item.get("player_id") or ""),
                "date": item.get("date", "—"), "player": item.get("player_name", item.get("player_id", "—")),
                "from": item.get("from_team", "—"), "to": item.get("to_team", "—"),
                "fee": f"{float(item.get('fee', 0)):.3f}", "status": item.get("status", "Hoàn tất"),
            })
        self._transfer_pid_by_iid = {}
        def transfer_iid(index, row):
            iid = f"transfer:{row['player_id']}:{index}"
            self._transfer_pid_by_iid[iid] = row["player_id"]
            return iid
        self._set_tree(self.club_transfer_tree, transfer_rows, lambda _i, r: (
            r["date"], r["player"], r["from"], r["to"], r["fee"], r["status"]),
            iid_of=transfer_iid)

        stadium = state.get("stadiums", {}).get(team, {})
        stadium_lines = [
            f"{stadium.get('name', team + ' Arena')} · {team}", "=" * 68,
            f"Sức chứa: {int(stadium.get('capacity', 0)):,} chỗ",
            f"Sức chứa gốc: {int(stadium.get('base_capacity', stadium.get('capacity', 0))):,} chỗ",
            f"Phân hạng: {str(stadium.get('tier_fit', finance.get('tier', '—'))).upper()}",
            f"Không khí sân: {stadium.get('atmosphere', '—')}/100",
            f"Khu hospitality: {float(stadium.get('hospitality_ratio', 0))*100:.1f}%",
            f"Giá vé cơ sở: {float(finance.get('ticket_price', 0)):.2f} €",
            "", "TRẬN SÂN NHÀ GẦN ĐÂY", "-" * 68,
        ]
        home_matches = [m for m in state.get("matches", []) if m.get("home") == team and m.get("played")]
        for match in home_matches[-20:][::-1]:
            md = match.get("matchday", {})
            if md:
                stadium_lines.append(f"{match.get('date')} · {match.get('comp')} · vs {match.get('away')} · "
                                     f"{md.get('attendance',0):,}/{md.get('capacity',0):,} · "
                                     f"{md.get('importance_label')} · {md.get('gross_revenue_m',0):.3f} M€")
        stadium_lines.append("\nSân hiện dùng tên Arena sinh ổn định theo seed vì danh sách CLB trong save là tên hư cấu.")
        self.club_stadium_text.configure(state="normal")
        self.club_stadium_text.delete("1.0", "end")
        self.club_stadium_text.insert("1.0", "\n".join(stadium_lines))
        self.club_stadium_text.configure(state="disabled")

        reports = state.get("season_financial_reports", {})
        report_lines = [f"BÁO CÁO TÀI CHÍNH · {team}", "=" * 68]
        for season_key in sorted(reports, reverse=True):
            report = reports[season_key]
            row = report.get("clubs", {}).get(team)
            if row:
                report_lines += [f"Mùa {season_key}", f"  Doanh thu: {row.get('revenue',0):.3f} M€",
                                 f"  Chi phí:  {row.get('costs',0):.3f} M€",
                                 f"  Lãi/lỗ:   {row.get('profit_loss',0):+.3f} M€",
                                 f"  Số dư:    {row.get('balance',0):.3f} M€",
                                 f"  Hỗ trợ chủ sở hữu: {row.get('owner_support',0):.3f} M€",
                                 f"  Vốn đầu tư thường niên: {row.get('owner_investment',0):.3f} M€",
                                 f"  Phân phối cho cổ đông: {row.get('owner_distribution',0):.3f} M€",
                                 f"  Đã trả nợ gốc: {row.get('debt_principal_paid',0):.3f} M€",
                                 f"  Lương/thu: {row.get('wage_revenue_ratio') if row.get('wage_revenue_ratio') is not None else '—'}", ""]
        current_ffp = state.get("ffp_status", {}).get(team, {})
        if current_ffp:
            report_lines += ["FFP HIỆN TẠI", f"  Trạng thái: {current_ffp.get('status')}",
                             f"  Lỗ lũy kế 3 mùa: {current_ffp.get('cumulative_loss_last_3', 0):.3f} M€ / ngưỡng {current_ffp.get('loss_limit', 0):.3f} M€",
                             f"  Trừ điểm mùa sau: {current_ffp.get('points_deducted', 0)} điểm (mùa {current_ffp.get('point_deduction_effective_season', '—')})",
                             f"  Chi lương + khấu hao/doanh thu: {current_ffp.get('wage_revenue_ratio') if current_ffp.get('wage_revenue_ratio') is not None else '—'}", ""]
        if not reports:
            report_lines.append("Chưa có mùa nào được chốt báo cáo tài chính. Báo cáo sẽ được tạo khi kết thúc mùa.")
        self.club_report_text.configure(state="normal")
        self.club_report_text.delete("1.0", "end")
        self.club_report_text.insert("1.0", "\n".join(report_lines))
        self.club_report_text.configure(state="disabled")

        notices = state.get("economy_notifications", [])
        notice_lines = [f"THÔNG BÁO · {team}", "=" * 68]
        selected = [n for n in notices if n.get("team_id") in (None, team)]
        for item in selected[-100:][::-1]:
            readable_message = format_economy_notification(item, state)
            notice_lines.append(
                f"[{item.get('priority','Thông tin')}] {item.get('date','')} · {readable_message}"
            )
        incoming_offers = [o for o in state.get("transfer_offers", [])
                           if o.get("from_team") == team and o.get("status") in ("PENDING", "COUNTERED")]
        outgoing_offers = [o for o in state.get("transfer_offers", [])
                           if o.get("to_team") == team and o.get("status") in ("PENDING", "COUNTERED")]
        if incoming_offers or outgoing_offers:
            notice_lines += ["", "OFFER ĐANG CHỜ"]
            for offer in incoming_offers + outgoing_offers:
                pid = offer.get("player_id")
                player_name = player_display_name(state, pid)
                contract = state.get("contracts", {}).get(str(pid), {})
                contract_id = contract.get("id") or "chưa có mã"
                years_left = int(contract.get("years_remaining", 0) or 0)
                contract_label = f"HĐ {contract_id} · còn {years_left} năm" if contract else "chưa có HĐ hiện tại"
                notice_lines.append(
                    f"{offer.get('id')} · {player_name} · {contract_label} · "
                    f"{offer.get('from_team')} → {offer.get('to_team')} · "
                    f"{offer.get('fee',0):.3f} M€ · {offer.get('status')}"
                )
        if not selected and not incoming_offers and not outgoing_offers:
            notice_lines.append("Chưa có thông báo tài chính/chuyển nhượng.")
        self.club_inbox_text.configure(state="normal")
        self.club_inbox_text.delete("1.0", "end")
        self.club_inbox_text.insert("1.0", "\n".join(notice_lines))
        self.club_inbox_text.configure(state="disabled")

    def refresh_cup(self):
        rows=[]
        self.cup_match_rows = {}
        names={"r1":"Vòng 1","r16":"Vòng 16","qf":"Tứ kết","sf":"Bán kết","final":"Chung kết"}
        for key in ("r1","r16","qf","sf","final"):
            for mid in self.game.s["cup"]["bracket"].get(key,[]):
                m=self.game.match_by_id(mid)
                rows.append({"round":names[key],"home":m["home"],"score":f"{m['hg']}–{m['ag']}"+(f" pen {m['pens']['home']}-{m['pens']['away']}" if m.get("pens") else ""),
                             "away":m["away"],"winner":m.get("winner") or "—","date":m["date"],"match":m})
        self._set_tree(self.cup_tree,rows,lambda i,r:(r["round"],r["home"],r["score"],r["away"],r["winner"],r["date"]))
        self.cup_match_rows = dict(zip(self.cup_tree.get_children(), [row["match"] for row in rows]))
        self.refresh_cup_bracket()

    @staticmethod
    def _cup_ellipsize(value, width=27):
        value = str(value or "TBD")
        return value if len(value) <= width else value[:width - 1] + "…"

    def refresh_cup_bracket(self):
        """Vẽ bracket Cup từ dữ liệu trận thật; các vòng tương lai hiển thị TBD có đường đi rõ ràng."""
        if not hasattr(self, "cup_canvas"):
            return
        canvas = self.cup_canvas
        canvas.delete("all")
        cup = self.game.s["cup"]
        seeds = cup.get("seed_order", self.game.s.get("season_seed_order", []))
        ranks = cup.get("seed_rank", {team: i + 1 for i, team in enumerate(seeds)})
        bracket = cup.get("bracket", {})
        round_keys = ("r1", "r16", "qf", "sf", "final")
        round_titles = {"r1": "Vòng 1", "r16": "Vòng 16", "qf": "Tứ kết", "sf": "Bán kết", "final": "Chung kết"}
        counts = {"r1": 8, "r16": 8, "qf": 4, "sf": 2, "final": 1}
        card_w, card_h, gap_x = 228, 72, 42
        x_positions = [20 + i * (card_w + gap_x) for i in range(5)]
        base_y, row_gap = 82, 100
        centers = {
            "r1": [base_y + i * row_gap for i in range(8)],
            "r16": [base_y + i * row_gap for i in range(8)],
            "qf": [base_y + row_gap / 2 + i * row_gap * 2 for i in range(4)],
            "sf": [base_y + row_gap * 1.5 + i * row_gap * 4 for i in range(2)],
            "final": [base_y + row_gap * 3.5],
        }

        def actual_or_none(key, index):
            ids = bracket.get(key, [])
            return self.game.match_by_id(ids[index]) if index < len(ids) else None

        def node_from_match(match, round_label, index, home=None, away=None):
            if match is not None:
                return {"match": match, "home": match["home"], "away": match["away"], "winner": match.get("winner"),
                        "hg": match.get("hg"), "ag": match.get("ag"), "pens": match.get("pens"), "round": round_label,
                        "index": index, "played": True}
            return {"match": None, "home": home or "Chưa xác định", "away": away or "Chưa xác định", "winner": None,
                    "hg": None, "ag": None, "pens": None, "round": round_label, "index": index, "played": False}

        def seed_home_away(team_a, team_b):
            if ranks.get(team_a, 999) > ranks.get(team_b, 999):
                return team_a, team_b
            return team_b, team_a

        r1_specs = [(8, 23), (9, 22), (10, 21), (11, 20), (12, 19), (13, 18), (14, 17), (15, 16)]
        r1_nodes_raw = []
        for index, (seed_a, seed_b) in enumerate(r1_specs):
            match = actual_or_none("r1", index)
            home, away = seed_home_away(seeds[seed_a], seeds[seed_b])
            r1_nodes_raw.append(node_from_match(match, "Vòng 1", index, home, away))
        # Sắp xếp hiển thị theo thứ tự các nhánh mà vòng 16 tham chiếu, để đường nối không bị chéo.
        r1_display_order = [7, 0, 4, 3, 5, 2, 1, 6]
        r1_nodes = [r1_nodes_raw[index] for index in r1_display_order]

        r16_specs = [(0, 7), (7, 0), (3, 4), (4, 3), (2, 5), (5, 2), (6, 1), (1, 6)]
        r16_nodes = []
        for index, (seed_index, r1_index) in enumerate(r16_specs):
            winner = r1_nodes_raw[r1_index]["winner"] or f"Thắng Vòng 1 #{r1_index + 1}"
            home, away = winner, seeds[seed_index]
            r16_nodes.append(node_from_match(actual_or_none("r16", index), "Vòng 16", index, home, away))

        qf_nodes = []
        for index, (left, right) in enumerate(((0, 1), (2, 3), (4, 5), (6, 7))):
            home = r16_nodes[left]["winner"] or f"Thắng Vòng 16 #{left + 1}"
            away = r16_nodes[right]["winner"] or f"Thắng Vòng 16 #{right + 1}"
            qf_nodes.append(node_from_match(actual_or_none("qf", index), "Tứ kết", index, home, away))

        sf_nodes = []
        for index, (left, right) in enumerate(((0, 1), (2, 3))):
            home = qf_nodes[left]["winner"] or f"Thắng Tứ kết #{left + 1}"
            away = qf_nodes[right]["winner"] or f"Thắng Tứ kết #{right + 1}"
            sf_nodes.append(node_from_match(actual_or_none("sf", index), "Bán kết", index, home, away))

        final_home = sf_nodes[0]["winner"] or "Thắng Bán kết #1"
        final_away = sf_nodes[1]["winner"] or "Thắng Bán kết #2"
        final_nodes = [node_from_match(actual_or_none("final", 0), "Chung kết", 0, final_home, final_away)]
        nodes = {"r1": r1_nodes, "r16": r16_nodes, "qf": qf_nodes, "sf": sf_nodes, "final": final_nodes}

        def is_placeholder(name):
            return name == "Chưa xác định" or str(name).startswith("Thắng ")

        # Đường nối kiểu khuỷu tay; nhánh đã có đội thắng được tô sáng.
        def connector(x1, y1, x2, y2, hot):
            mid = x1 + (x2 - x1) / 2
            canvas.create_line(x1, y1, mid, y1, mid, y2, x2, y2, fill=TURF_DIM if hot else LINE_2,
                               width=2 if hot else 1.5, joinstyle="round")

        for index in range(8):
            connector(x_positions[0] + card_w, centers["r1"][index], x_positions[1], centers["r16"][index],
                      bool(r1_nodes[index]["winner"]))
        for source_key, target_key in (("r16", "qf"), ("qf", "sf"), ("sf", "final")):
            source_col = round_keys.index(source_key)
            target_col = round_keys.index(target_key)
            for index, source_y in enumerate(centers[source_key]):
                connector(x_positions[source_col] + card_w, source_y, x_positions[target_col],
                          centers[target_key][index // 2], bool(nodes[source_key][index]["winner"]))

        for key in round_keys:
            x = x_positions[round_keys.index(key)]
            canvas.create_text(x, 20, text=round_titles[key], anchor="w", fill=TEXT, font=F(11, "bold"))
            canvas.create_line(x, 35, x + card_w, 35, fill=LINE_2)
            for index, node in enumerate(nodes[key]):
                top = centers[key][index] - card_h / 2
                is_final = key == "final"
                has_winner = bool(node["winner"])
                tag = f"cup_{key}_{index}"
                if is_final and has_winner:
                    fill, outline = GOLD_BG, GOLD
                else:
                    fill, outline = SURFACE, LINE_2
                rect = self._round_rect(canvas, x, top, x + card_w, top + card_h, 9, fill=fill, outline=outline,
                                        width=1.5 if (is_final and has_winner) else 1, tags=(tag,))
                if has_winner:
                    canvas.create_rectangle(x + 1, top + 10, x + 4, top + card_h - 10,
                                            fill=GOLD if is_final else TURF, outline="", tags=(tag,))
                match = node["match"]
                header = f"Trận {index + 1}"
                if match and match.get("date"):
                    header += f"   {match.get('date')}"
                canvas.create_text(x + 14, top + 12, text=header, anchor="w", fill=DIM, font=F(8), tags=(tag,))
                if node["played"] and node.get("pens"):
                    canvas.create_text(x + card_w - 12, top + 12, text=f"pen {node['pens']['home']}-{node['pens']['away']}",
                                       anchor="e", fill=GOLD, font=F(8, "bold"), tags=(tag,))
                for row, side, goals in ((0, "home", node["hg"]), (1, "away", node["ag"])):
                    name = node[side]
                    y = top + 36 + row * 22
                    won = has_winner and node["winner"] == name
                    if node["played"]:
                        color = TEXT if won else MUTED
                    else:
                        color = DIM if is_placeholder(name) else TEXT
                    canvas.create_text(x + 14, y, text=self._cup_ellipsize(name, 25), anchor="w", fill=color,
                                       font=F(10, "bold" if won else "normal"), tags=(tag,))
                    if node["played"]:
                        score_color = (GOLD if is_final else TEXT) if won else MUTED
                        canvas.create_text(x + card_w - 14, y, text=str(goals), anchor="e", fill=score_color,
                                           font=F(11, "bold", mono=True), tags=(tag,))
                if match:
                    canvas.tag_bind(tag, "<Button-1>", lambda _event, selected=match: self._show_match_report(selected))
                    canvas.tag_bind(tag, "<Enter>", lambda _event, r=rect: (canvas.configure(cursor="hand2"),
                                                                           canvas.itemconfigure(r, outline=TEXT)))
                    canvas.tag_bind(tag, "<Leave>", lambda _event, r=rect, o=outline: (canvas.configure(cursor=""),
                                                                                       canvas.itemconfigure(r, outline=o)))
                else:
                    next_fixture = self._find_next_slot_fixture(comp="Cup", home=node["home"], away=node["away"])
                    if next_fixture:
                        canvas.tag_bind(tag, "<Button-1>",
                                        lambda _event, selected=next_fixture: self._show_fixture_simulation_options(selected))
                        canvas.tag_bind(tag, "<Enter>", lambda _event, r=rect: (canvas.configure(cursor="hand2"),
                                                                               canvas.itemconfigure(r, outline=TEXT)))
                        canvas.tag_bind(tag, "<Leave>", lambda _event, r=rect, o=outline: (canvas.configure(cursor=""),
                                                                                           canvas.itemconfigure(r, outline=o)))
        canvas.configure(scrollregion=(0, 0, x_positions[-1] + card_w + 24, 920))

    def _c1_group_fixture_rows(self):
        """Return the six A/B group-stage fixtures, including unplayed scheduled matches."""
        state = self.game.s
        c1 = state.get("c1", {})
        groups = c1.get("groups") or {}
        if not groups and c1.get("playin_qualified"):
            try:
                groups = self.game._build_c1_groups()  # read-only deterministic projection
            except (KeyError, RuntimeError, TypeError, ValueError):
                groups = {}
        if not groups:
            return []

        slots = {str(slot.get("kind")): slot for slot in state.get("slots", [])
                 if str(slot.get("kind", "")).startswith("c1_group")}
        played_matches = [m for m in state.get("matches", []) if m.get("played")]
        rows = []
        for group_label in ("A", "B"):
            group = groups.get(group_label) or {}
            rounds = group.get("rounds") or []
            for round_index, pairings in enumerate(rounds[:3]):
                slot = slots.get(f"c1_group{round_index + 1}") or {}
                scheduled_date = str(slot.get("date") or "")
                stage = f"c1_group_{group_label}_{round_index + 1}"
                for fixture_index, (home, away) in enumerate(pairings):
                    match = next((m for m in played_matches
                                  if m.get("stage") == stage
                                  and m.get("home") == home and m.get("away") == away), None)
                    if match is None:
                        # Compatibility with older saves whose match rows omitted stage metadata.
                        match = next((m for m in played_matches
                                      if m.get("comp") == "C1" and m.get("home") == home
                                      and m.get("away") == away
                                      and (not scheduled_date or str(m.get("date", ""))[:10] == scheduled_date[:10])), None)
                    date_value = str((match or {}).get("date") or scheduled_date)
                    try:
                        date_label = date.fromisoformat(date_value[:10]).strftime("%d/%m/%Y")
                    except ValueError:
                        date_label = date_value or "Chưa xếp lịch"
                    rows.append({
                        "date": date_label, "date_sort": date_value,
                        "group": group_label, "round": f"V{round_index + 1}",
                        "home": home, "away": away,
                        "score": (f"{int(match.get('hg', 0))}–{int(match.get('ag', 0))}" if match else "—"),
                        "status": "FT" if match else "Chưa đá",
                        "match": match,
                        "order": fixture_index,
                    })
        rows.sort(key=lambda row: (row["date_sort"], row["group"], row["round"], row["order"]))
        return rows

    def _refresh_c1_group_fixtures(self):
        tree = getattr(self, "c1_group_fixture_tree", None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        self.c1_group_fixture_matches = {}
        self.c1_group_fixture_rows = {}
        rows = self._c1_group_fixture_rows()
        if not rows:
            tree.insert("", "end", values=("—", "—", "—", "Chờ Play-in", "—", "hoàn tất", "Chưa xếp lịch"))
            return
        for index, row in enumerate(rows):
            iid = f"c1_{row['group']}_{row['round']}_{index}"
            tag = "c1_played" if row.get("match") else "c1_pending"
            tree.insert("", "end", iid=iid, values=(row["date"], row["group"], row["round"],
                       row["home"], row["score"], row["away"], row["status"]), tags=(tag,))
            self.c1_group_fixture_rows[iid] = row
            if row.get("match"):
                self.c1_group_fixture_matches[iid] = row["match"]

    def _find_next_slot_fixture(self, *, comp=None, home=None, away=None, date_value=None):
        """Find an exact pending fixture only in the next user-controlled slot."""
        slot = self.game.next_slot()
        if not slot:
            return None
        try:
            pending = self.game.slot_fixture_specs(slot, include_played=False)
        except Exception:
            return None
        for spec in pending:
            if comp and spec.get("comp") != comp:
                continue
            if home and away:
                same_order = spec.get("home") == home and spec.get("away") == away
                reverse_order = spec.get("home") == away and spec.get("away") == home
                if not (same_order or reverse_order):
                    continue
            elif home and spec.get("home") != home:
                continue
            elif away and spec.get("away") != away:
                continue
            if date_value and str(spec.get("date", ""))[:10] != str(date_value)[:10]:
                continue
            return spec
        return None

    def _on_c1_group_fixture_click(self, event=None):
        tree = getattr(self, "c1_group_fixture_tree", None)
        if tree is None:
            return
        iid = tree.identify_row(event.y) if event is not None else (tree.selection() or [None])[0]
        row = getattr(self, "c1_group_fixture_rows", {}).get(iid)
        if not row:
            return
        match = row.get("match")
        if match:
            self._show_match_report(match)
            return
        spec = self._find_next_slot_fixture(comp="C1", home=row.get("home"),
                                            away=row.get("away"), date_value=row.get("date_sort"))
        if spec:
            self._show_fixture_simulation_options(spec)
        else:
            self.toast("Trận C1 này chưa thuộc slot kế tiếp. Hãy hoàn tất các slot trước để mở Live Match.", "info")

    def refresh_c1(self):
        c = self.game.s["c1"]
        lines = [f"C1 MÙA {self.game.season_label()}", "",
                 "VÀO THẲNG VÒNG BẢNG: " + ", ".join(c.get("direct_teams", [])),
                 "PLAY-IN: " + ", ".join(c.get("playin_seed_teams", [])), "",
                 "THỂ THỨC PLAY-IN · DOUBLE ELIMINATION",
                 "Mỗi bảng 4 đội: 2 trận mở màn → nhánh thắng / nhánh thua → trận quyết định.",
                 "Đội thắng nhánh thắng và đội thắng trận quyết định giành vé vào vòng bảng.", ""]
        if c.get("playin"):
            for label in ("X", "Y"):
                entry = c["playin"].get(label, {})
                lines.append(f"BẢNG {label}: " + ", ".join(entry.get("teams", [])))
                for index, mid in enumerate(entry.get("r1", []), 1):
                    result = self._format_match(self.game.match_by_id(mid)) if mid else "Chưa diễn ra"
                    lines.append(f"  Vòng mở màn {index}: {result}")
                for key, title in (("winner_match", "Nhánh thắng"), ("loser_match", "Nhánh thua"),
                                   ("decider", "Trận quyết định")):
                    result_id = entry.get(key)
                    result = self._format_match(self.game.match_by_id(result_id)) if result_id else "Chưa diễn ra"
                    lines.append(f"  {title}: {result}")
                lines.append(f"  Vé trực tiếp: {entry.get('winner_qualifier') or 'Chưa xác định'}")
                lines.append(f"  Vé còn lại: {entry.get('decider_qualifier') or 'Chưa xác định'}")
                lines.append("")
        if c.get("groups"):
            for label in ("A", "B"):
                group = c["groups"].get(label, {})
                table = self.game.c1_group_table(label)
                played = sum(len(ids) for ids in group.get("match_ids", []))
                lines.append(f"VÒNG BẢNG {label} · {played}/6 trận")
                for pos, row in enumerate(table, 1):
                    lines.append(f"  {pos}. {row['team']} | {row['p']}T {row['w']}W {row['d']}D {row['l']}L "
                                 f"| {row['gf']}:{row['ga']} ({row['gd']:+}) | {row['pts']}đ | Elo {row['elo']:.0f}")
                lines.append("  Hai đội đầu bảng vào bán kết.")
                lines.append("")
        if c.get("semi_ties"):
            lines.append("BÁN KẾT HAI LƯỢT")
            for index, tie in enumerate(c["semi_ties"], 1):
                result = f"  BK{index}: "
                if tie.get("leg1"):
                    first = self.game.match_by_id(tie["leg1"])
                    result += f"Lượt đi {first['home']} {first['hg']}-{first['ag']} {first['away']}"
                else:
                    result += "Lượt đi chưa diễn ra"
                if tie.get("leg2"):
                    second = self.game.match_by_id(tie["leg2"])
                    result += f" | Lượt về {second['home']} {second['hg']}-{second['ag']} {second['away']}"
                    result += f" | Đi tiếp: {second.get('winner')}"
                lines.append(result)
            lines.append("")
        if c.get("champion"):
            lines.append(f"VÔ ĐỊCH C1: {c['champion']} · Á quân: {c.get('runner_up')}")
        self._set_text(self.c1_text, "\n".join(lines))
        self._refresh_c1_group_fixtures()
        self.refresh_c1_bracket()


    def refresh_c1_bracket(self):
        """Render separate GSL double-elimination, group tables and knockout views."""
        c1 = self.game.s["c1"]
        seeds = c1.get("seed_order", self.game.s.get("season_seed_order", []))
        if len(seeds) < 12:
            return

        def fetch(mid):
            if not mid:
                return None
            try:
                return self.game.match_by_id(mid)
            except (StopIteration, TypeError, ValueError):
                return None

        def draw_card(canvas, x, y, w, h, title, teams=None, match=None, subtitle=None, highlight=False,
                      competition="C1"):
            fill = GOLD_BG if highlight else SURFACE
            outline = GOLD if highlight else LINE_2
            self._round_rect(canvas, x, y, x + w, y + h, 9, fill=fill, outline=outline,
                             width=1.5 if highlight else 1)
            canvas.create_text(x + 13, y + 17, text=title, anchor="w", fill=GOLD if highlight else TEXT,
                               font=F(9, "bold"))
            canvas.create_line(x + 12, y + 31, x + w - 12, y + 31, fill=LINE)
            if match:
                hname, aname = match.get("home", "—"), match.get("away", "—")
                score = f"{match.get('hg', '—')}  :  {match.get('ag', '—')}"
                canvas.create_text(x + 13, y + 51, text=self._cup_ellipsize(hname, 17), anchor="w",
                                   fill=TURF_LIGHT if match.get("winner") == hname else TEXT, font=F(9, "bold"))
                canvas.create_text(x + w - 13, y + 51, text=score, anchor="e", fill=GOLD,
                                   font=F(10, "bold", mono=True))
                canvas.create_text(x + 13, y + 75, text=self._cup_ellipsize(aname, 17), anchor="w",
                                   fill=TURF_LIGHT if match.get("winner") == aname else TEXT, font=F(9, "bold"))
                if match.get("pens"):
                    pens = match["pens"]
                    foot = f"Luân lưu {pens['home']}–{pens['away']}"
                else:
                    foot = match.get("winner") or "Đã đá · không có đội thắng"
                if subtitle:
                    foot = subtitle + " · " + foot
                canvas.create_text(x + 13, y + h - 12, text=self._cup_ellipsize(foot, 24), anchor="w",
                                   fill=GOLD if match.get("winner") else MUTED, font=F(8))
                tag = f"c1report_{match.get('id')}"
                # Played cards open the saved match report only; no replay or simulation.
                rect = self._round_rect(canvas, x, y, x + w, y + h, 9, fill="", outline="", tags=(tag,))
                canvas.tag_bind(tag, "<Button-1>", lambda _event, selected=match: self._show_match_report(selected))
                canvas.tag_bind(tag, "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
                canvas.tag_bind(tag, "<Leave>", lambda _event: canvas.configure(cursor=""))
            else:
                team_a, team_b = teams or ("Chưa xác định", "Chưa xác định")
                canvas.create_text(x + 13, y + 51, text=self._cup_ellipsize(team_a, 30), anchor="w",
                                   fill=TEXT, font=F(9, "bold"))
                canvas.create_text(x + w - 13, y + 51, text="vs", anchor="e", fill=MUTED,
                                   font=F(9, "bold"))
                canvas.create_text(x + 13, y + 75, text=self._cup_ellipsize(team_b, 30), anchor="w",
                                   fill=TEXT, font=F(9, "bold"))
                canvas.create_text(x + 13, y + h - 12,
                                   text=self._cup_ellipsize(subtitle or "Chưa diễn ra", 42), anchor="w",
                                   fill=MUTED, font=F(8))
                # A known upcoming pairing in the next slot launches the same Live Match flow.
                # Unknown future opponents remain decorative/non-clickable until their path is known.
                next_fixture = self._find_next_slot_fixture(comp=competition, home=team_a, away=team_b)
                if next_fixture:
                    tag = f"c1upcoming_{next_fixture.get('spec_key', title)}"
                    rect = self._round_rect(canvas, x, y, x + w, y + h, 9, fill="", outline="", tags=(tag,))
                    canvas.tag_bind(tag, "<Button-1>",
                                    lambda _event, selected=next_fixture: self._show_fixture_simulation_options(selected))
                    canvas.tag_bind(tag, "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
                    canvas.tag_bind(tag, "<Leave>", lambda _event: canvas.configure(cursor=""))

        # ── PLAY-IN: GSL double elimination ───────────────────────────────
        canvas = self.c1_playin_canvas
        canvas.delete("all")
        canvas.create_text(24, 22, text="C1 PLAY-IN · DOUBLE ELIMINATION", anchor="w", fill=TEXT,
                           font=F(14, "bold"))
        canvas.create_text(24, 46, text="Mỗi bảng gồm nhánh thắng, nhánh thua và trận quyết định · hai đội giành vé vào vòng bảng.",
                           anchor="w", fill=MUTED, font=F(9))
        default_groups = {"X": [seeds[4], seeds[7], seeds[8], seeds[11]],
                          "Y": [seeds[5], seeds[6], seeds[9], seeds[10]]}
        groups = c1.get("playin_groups") or default_groups
        entries = c1.get("playin", {})
        card_w, card_h = 190, 104
        open_x, winner_x, decider_x, qualifier_x = 24, 250, 476, 700
        for idx, label in enumerate(("X", "Y")):
            y0 = 78 + idx * 385
            teams = groups.get(label, default_groups[label])
            entry = entries.get(label, {})
            canvas.create_text(24, y0, text=f"BẢNG {label} · 4 ĐỘI", anchor="w", fill=GOLD, font=F(12, "bold"))
            canvas.create_line(24, y0 + 20, 925, y0 + 20, fill=LINE_2)
            pairs = ((teams[0], teams[3]), (teams[1], teams[2]))
            r1_ids = entry.get("r1", [])
            r1_matches = [fetch(r1_ids[i]) if i < len(r1_ids) else None for i in range(2)]
            draw_card(canvas, open_x, y0 + 40, card_w, card_h, "VÒNG MỞ MÀN · TRẬN 1",
                      pairs[0], r1_matches[0], "Nhánh thắng / nhánh thua", competition="C1 Play-in")
            draw_card(canvas, open_x, y0 + 190, card_w, card_h, "VÒNG MỞ MÀN · TRẬN 2",
                      pairs[1], r1_matches[1], "Nhánh thắng / nhánh thua", competition="C1 Play-in")

            winners = [m.get("winner") for m in r1_matches if m and m.get("winner")]
            losers = []
            for match in r1_matches:
                if match and match.get("winner"):
                    losers.append(match["away"] if match["winner"] == match["home"] else match["home"])
            wb_teams = tuple(winners[:2]) if len(winners) == 2 else ("Thắng trận 1", "Thắng trận 2")
            lb_teams = tuple(losers[:2]) if len(losers) == 2 else ("Thua trận 1", "Thua trận 2")
            winner_match = fetch(entry.get("winner_match"))
            loser_match = fetch(entry.get("loser_match"))
            decider = fetch(entry.get("decider"))
            draw_card(canvas, winner_x, y0 + 40, card_w, card_h, "NHÁNH THẮNG",
                      wb_teams, winner_match, "Thắng → đi thẳng vòng bảng", competition="C1 Play-in")
            draw_card(canvas, winner_x, y0 + 190, card_w, card_h, "NHÁNH THUA",
                      lb_teams, loser_match, "Thua → bị loại", competition="C1 Play-in")

            wb_loser = None
            if winner_match and winner_match.get("winner"):
                wb_loser = (winner_match["away"] if winner_match["winner"] == winner_match["home"]
                            else winner_match["home"])
            lb_winner = loser_match.get("winner") if loser_match else None
            decider_teams = (wb_loser or "Thua nhánh thắng", lb_winner or "Thắng nhánh thua")
            draw_card(canvas, decider_x, y0 + 115, card_w, card_h, "TRẬN QUYẾT ĐỊNH",
                      decider_teams, decider, "Thắng → lấy vé thứ hai", competition="C1 Play-in")

            # Không vẽ connector; từng match card là một mục độc lập, theo yêu cầu UI.

            direct = entry.get("winner_qualifier") or (winner_match.get("winner") if winner_match else None)
            qualifier = entry.get("decider_qualifier") or (decider.get("winner") if decider else None)
            qx, qy = qualifier_x, y0 + 105
            self._round_rect(canvas, qx, qy, qx + 215, qy + 125, 9, fill=TURF_BG,
                             outline=TURF_DIM, width=1.2)
            canvas.create_text(qx + 14, qy + 18, text="VÉ VÒNG BẢNG", anchor="w", fill=TURF_LIGHT,
                               font=F(9, "bold"))
            canvas.create_text(qx + 14, qy + 49, text="1 · Nhánh thắng", anchor="w", fill=MUTED, font=F(8))
            canvas.create_text(qx + 201, qy + 49, text=self._cup_ellipsize(direct or "Chưa xác định", 18),
                               anchor="e", fill=TEXT, font=F(8, "bold"))
            canvas.create_text(qx + 14, qy + 83, text="2 · Trận quyết định", anchor="w", fill=MUTED, font=F(8))
            canvas.create_text(qx + 201, qy + 83, text=self._cup_ellipsize(qualifier or "Chưa xác định", 18),
                               anchor="e", fill=TEXT, font=F(8, "bold"))
        canvas.configure(scrollregion=(0, 0, 930, 860))

        # ── GROUP STAGE: full standings tables with W/D/L and goal metrics ──
        canvas = self.c1_groups_canvas
        canvas.delete("all")
        canvas.create_text(24, 22, text="C1 MAIN STAGE · VÒNG BẢNG", anchor="w", fill=TEXT, font=F(14, "bold"))
        canvas.create_text(24, 46, text="Mỗi bảng 4 đội · đá vòng tròn một lượt (6 trận/bảng) · Top 2 vào bán kết.",
                           anchor="w", fill=MUTED, font=F(9))
        qualified = c1.get("playin_qualified", {})
        expected = {
            "A": [seeds[0], seeds[3], qualified.get("X", {}).get("winner") or "Thắng nhánh thắng X",
                  qualified.get("Y", {}).get("decider") or "Thắng trận quyết định Y"],
            "B": [seeds[1], seeds[2], qualified.get("Y", {}).get("winner") or "Thắng nhánh thắng Y",
                  qualified.get("X", {}).get("decider") or "Thắng trận quyết định X"],
        }
        columns = [(22, "#"), (62, "CLB"), (310, "P"), (355, "W"), (400, "D"), (445, "L"),
                   (500, "GF"), (555, "GA"), (610, "GD"), (680, "PTS"), (750, "ELO")]
        for index, label in enumerate(("A", "B")):
            x0 = 25
            y0 = 82 + index * 410
            group = c1.get("groups", {}).get(label)
            table = self.game.c1_group_table(label) if group else []
            team_list = group.get("teams", expected[label]) if group else expected[label]
            played = sum(len(ids) for ids in group.get("match_ids", [])) if group else 0
            self._round_rect(canvas, x0, y0, x0 + 780, y0 + 385, 10, fill=SURFACE, outline=LINE_2)
            canvas.create_text(x0 + 18, y0 + 22, text=f"BẢNG {label}", anchor="w", fill=GOLD, font=F(12, "bold"))
            canvas.create_text(x0 + 760, y0 + 22, text=f"{played}/6 trận", anchor="e", fill=MUTED, font=F(9))
            head_y = y0 + 55
            canvas.create_rectangle(x0 + 12, head_y, x0 + 768, head_y + 34, fill=RAISED, outline="")
            for col_x, title in columns:
                canvas.create_text(x0 + col_x, head_y + 17, text=title, anchor="w" if title == "CLB" else "center",
                                   fill=MUTED, font=F(8, "bold"))
            rows = table if table else [{"team": team, "p": 0, "w": 0, "d": 0, "l": 0, "gf": 0,
                                         "ga": 0, "gd": 0, "pts": 0, "elo": 0} for team in team_list]
            for row_index, row in enumerate(rows[:4]):
                ry = head_y + 35 + row_index * 58
                if row_index < 2:
                    row_bg, row_fg = TURF_BG, TURF_LIGHT
                else:
                    row_bg, row_fg = (SURFACE_2 if row_index % 2 == 0 else STRIPE), TEXT
                canvas.create_rectangle(x0 + 12, ry, x0 + 768, ry + 54, fill=row_bg, outline="")
                values = [str(row_index + 1), self._cup_ellipsize(row.get("team", "—"), 32),
                          str(row.get("p", 0)) if group else "—", str(row.get("w", 0)) if group else "—",
                          str(row.get("d", 0)) if group else "—", str(row.get("l", 0)) if group else "—",
                          str(row.get("gf", 0)) if group else "—", str(row.get("ga", 0)) if group else "—",
                          f"{row.get('gd', 0):+}" if group else "—", str(row.get("pts", 0)) if group else "—",
                          f"{row.get('elo', 0):.0f}" if group else "—"]
                for (col_x, title), value in zip(columns, values):
                    anchor = "w" if title == "CLB" else "center"
                    color = row_fg if title != "PTS" else (GOLD if row_index == 0 else row_fg)
                    canvas.create_text(x0 + col_x, ry + 27, text=value, anchor=anchor,
                                       fill=color, font=F(9, "bold" if title in ("CLB", "PTS") else "normal"))
            canvas.create_text(x0 + 18, y0 + 354, text="TOP 2 → BÁN KẾT", anchor="w", fill=TURF_LIGHT,
                               font=F(9, "bold"))
            canvas.create_text(x0 + 760, y0 + 354,
                               text="Nhất bảng A gặp nhì bảng B" if label == "A" else "Nhất bảng B gặp nhì bảng A",
                               anchor="e", fill=MUTED, font=F(8))
        canvas.configure(scrollregion=(0, 0, 850, 900))

        # ── KNOCKOUT ─────────────────────────────────────────────────────
        canvas = self.c1_knockout_canvas
        canvas.delete("all")
        canvas.create_text(24, 22, text="C1 · VÒNG KNOCK-OUT", anchor="w", fill=TEXT, font=F(14, "bold"))
        canvas.create_text(24, 46, text="Bán kết hai lượt · đội nhất bảng đá lượt về trên sân nhà · chung kết sân trung lập.",
                           anchor="w", fill=MUTED, font=F(9))
        ties = c1.get("semi_ties", [])
        semi_positions = [(24, 100), (480, 100)]
        for index in range(2):
            x, y = semi_positions[index]
            if len(ties) > index:
                tie = ties[index]
                first = fetch(tie.get("leg1"))
                second = fetch(tie.get("leg2"))
                title = f"BÁN KẾT {index + 1} · {tie.get('group_winner', 'Nhất bảng')} vs {tie.get('group_runner', 'Nhì bảng')}"
                draw_card(canvas, x, y, 420, 112, title + " · LƯỢT ĐI",
                          match=first, teams=(tie.get("group_runner"), tie.get("group_winner")),
                          subtitle="Sân đội nhì bảng")
                draw_card(canvas, x, y + 128, 420, 112, "LƯỢT VỀ",
                          match=second, teams=(tie.get("group_winner"), tie.get("group_runner")),
                          subtitle=("Đội thắng chung cuộc" if second else "Sân đội nhất bảng"))
                if second and second.get("winner"):
                    canvas.create_text(x + 420, y + 252, text=f"✓ ĐI TIẾP: {second['winner']}",
                                       anchor="e", fill=TURF_LIGHT, font=F(9, "bold"))
            else:
                labels = (("Nhất bảng A", "Nhì bảng B") if index == 0 else ("Nhất bảng B", "Nhì bảng A"))
                draw_card(canvas, x, y, 420, 112, f"BÁN KẾT {index + 1} · LƯỢT ĐI",
                          teams=(labels[1], labels[0]), subtitle="Sân đội nhì bảng")
                draw_card(canvas, x, y + 128, 420, 112, "LƯỢT VỀ",
                          teams=(labels[0], labels[1]), subtitle="Sân đội nhất bảng")
        final_match = fetch(c1.get("bracket", {}).get("final"))
        draw_card(canvas, 260, 430, 420, 145, "CHUNG KẾT C1 · SÂN TRUNG LẬP",
                  match=final_match, teams=("Thắng bán kết 1", "Thắng bán kết 2"),
                  subtitle=(f"Vô địch: {c1.get('champion')}" if c1.get("champion") else "Chưa diễn ra"),
                  highlight=bool(c1.get("champion")))
        canvas.create_line(234, 352, 234, 390, 380, 430, fill=LINE_2, width=2, smooth=True)
        canvas.create_line(690, 352, 690, 390, 560, 430, fill=LINE_2, width=2, smooth=True)
        canvas.configure(scrollregion=(0, 0, 930, 700))


    @staticmethod
    def _format_match(m):
        home, away = m.get("home", "—"), m.get("away", "—")
        try:
            hg, ag = int(m.get("hg")), int(m.get("ag"))
        except (TypeError, ValueError):
            hg, ag = None, None
        score = f"{hg if hg is not None else '—'}-{ag if ag is not None else '—'}"
        if m.get("aet") or m.get("et"):
            score += " AET"
        pens = m.get("pens")
        winner = m.get("winner")
        if not winner and isinstance(pens, dict):
            try:
                ph, pa = int(pens.get("home")), int(pens.get("away"))
                if ph != pa:
                    winner = home if ph > pa else away
            except (TypeError, ValueError):
                pass
            if "home" in pens and "away" in pens:
                score += f" (pen {pens.get('home', '?')}-{pens.get('away', '?')})"
        elif not winner and hg is not None and ag is not None:
            winner = home if hg > ag else away if ag > hg else "Hòa"
        return f"{home} {score} {away} → {winner or 'Hòa'}"

    def refresh_competition_leaderboards(self):
        if not getattr(self, "leaderboard_trees", None):
            return
        comp_map = {"premier": "Premier League", "fc1": "Football Championship 1",
                    "fc2": "Football Championship 2", "Cup": "Cup", "C1": "C1"}
        for key, trees in self.leaderboard_trees.items():
            tables = self.game.competition_player_leaderboards(comp_map[key])
            scorers, assisters = tables.get("scorers", []), tables.get("assists", [])
            self._set_tree(trees["scorers"], scorers,
                           lambda i, r: (i + 1, r.get("player", "—"), r.get("team", "—"),
                                        r.get("goals", 0), r.get("assists", 0), r.get("apps", 0)))
            self._set_tree(trees["assists"], assisters,
                           lambda i, r: (i + 1, r.get("player", "—"), r.get("team", "—"),
                                        r.get("assists", 0), r.get("goals", 0), r.get("apps", 0)))
            if not scorers:
                trees["scorers"].insert("", "end", values=("—", "Chưa có bàn thắng", "", "", "", ""))
            if not assisters:
                trees["assists"].insert("", "end", values=("—", "Chưa có kiến tạo", "", "", "", ""))

    def refresh_ranking(self):
        rows=self.game.c1_qualification_table()
        self._set_tree(self.ranking_tree,rows,lambda i,r:(r["rank"],r["team"],TIER_LABELS.get(r["tier"],r["tier"]),r["league_points"],r["cup_points"],r["total"],r["gd"],round(r["elo"])),
            lambda i,r:("top",) if r["rank"]<=4 else (("promote",) if r["rank"]<=12 else ()))

    def refresh_history(self):
        rows=list(reversed(self.game.s.get("history",[])))
        self._set_tree(self.history_tree,rows,lambda i,r:(r["label"],r.get("champion_premier") or "—",r.get("champion_cup") or "—",
            r.get("champion_c1") or "—",(r.get("top_scorers") or [{"player":"—"}])[0].get("player","—"),r.get("match_count",0)))
        self.refresh_history_analytics()

    def _legend_row_values(self, index, row, include_team=True):
        values = [index + 1, row.get("player", "—")]
        if include_team:
            values.append(row.get("team", "—"))
        minutes = row.get("minutes", 0)
        values.extend((row.get("position", "—"), row.get("apps", 0), row.get("goals", 0),
                       row.get("assists", 0), minutes if minutes else "—"))
        return tuple(values)

    def _refresh_legend_trees(self, trees, legends, include_team=True):
        for key in ("appearances", "assists", "goals"):
            rows = legends.get(key, [])
            tree = trees[key]
            self._set_tree(tree, rows,
                           lambda i, row, team_col=include_team: self._legend_row_values(i, row, team_col))
            if not rows:
                label = {"appearances": "Chưa có dữ liệu ra sân", "assists": "Chưa có kiến tạo",
                         "goals": "Chưa có bàn thắng"}[key]
                columns = tree["columns"]
                tree.insert("", "end", values=("—", label) + tuple("" for _ in range(len(columns) - 2)))

    def refresh_history_analytics(self):
        if not hasattr(self, "history_trophy_tree"):
            return
        trophies = self.game.career_trophy_table()
        self._set_tree(self.history_trophy_tree, trophies,
                       lambda i, row: (i + 1, row["team"], row["premier"], row["fc1"], row["fc2"],
                                       row["cup"], row["c1"], row["total"]),
                       lambda i, row: ("top",) if row["total"] > 0 and i == 0 else ())
        legends = self.game.career_player_legends(limit=11)
        self._refresh_legend_trees(self.history_legend_trees, legends, include_team=True)
        if hasattr(self, "hall_of_fame_tree"):
            hof_rows = self.game.career_hall_of_fame(limit=50)
            self._set_tree(self.hall_of_fame_tree, hof_rows, lambda i, row: (
                i + 1, row.get("player", "—"), row.get("club", "—"), row.get("position", "—"),
                row.get("retire_season", "—"),
                f"{float(row.get('age')):.1f}" if row.get("age") is not None else "—",
                row.get("overall_peak", "—"), row.get("appearances", 0), row.get("goals", 0),
                row.get("assists", 0), row.get("honors_count", 0)),
                iid_of=lambda _i, row: str(row.get("player_id") or f"hof_{_i}"))
            if not hof_rows:
                self.hall_of_fame_tree.insert("", "end", values=("—", "Chưa có cầu thủ giải nghệ", "", "", "", "", "", "", "", ""))
        if hasattr(self, "career_records_tree"):
            record_rows = self.game.career_record_table()
            self._set_tree(self.career_records_tree, record_rows, lambda _i, row: (
                row.get("record", "—"), row.get("holder", "—"), row.get("club", "—"),
                row.get("value", "—"), row.get("season", "—")))
            if not record_rows:
                self.career_records_tree.insert("", "end", values=("—", "Chưa có dữ liệu lịch sử", "", "", ""))

    def refresh_club_analytics(self, team):
        if not hasattr(self, "club_trophy_vars") or not self.club_trophy_vars:
            return
        trophies = self.game.career_team_trophies(team)
        for key, variable in self.club_trophy_vars.items():
            variable.set(str(trophies.get(key, 0)))
        legends = self.game.career_player_legends(team=team, limit=11)
        self._refresh_legend_trees(self.club_legend_trees, legends, include_team=False)

    def refresh_club_history(self, team):
        if not hasattr(self, "club_history_stats_vars"):
            return
        data = self.game.club_history_analytics(team)
        data["team"] = team
        self._club_history_data = data
        stats = data.get("stats", {})
        def match_text(match):
            if not match:
                return "Chưa có"
            return (f"{match.get('home')} {match.get('hg')}–{match.get('ag')} "
                    f"{match.get('away')} · {match.get('date')} · {match.get('comp')}")
        scorer = stats.get("top_scorer") or {}
        assister = stats.get("top_assister") or {}
        values = {
            "matches": str(stats.get("matches", 0)),
            "wins": str(stats.get("wins", 0)),
            "draws": str(stats.get("draws", 0)),
            "losses": str(stats.get("losses", 0)),
            "top_scorer": (f"{scorer.get('player')} · {scorer.get('goals')} bàn" if scorer else "Chưa có"),
            "top_assister": (f"{assister.get('player')} · {assister.get('assists')} kiến tạo" if assister else "Chưa có"),
            "biggest_win": match_text(stats.get("biggest_win")),
            "biggest_loss": match_text(stats.get("biggest_loss")),
            "first_match": match_text(stats.get("first_match")),
        }
        for key, variable in self.club_history_stats_vars.items():
            variable.set(values.get(key, "—"))
        self._set_tree(self.club_history_season_tree, data.get("by_season", []), lambda _i, row: (
            row.get("season"), row.get("premier", "—"), row.get("fc1", "—"),
            row.get("fc2", "—"), row.get("cup", "—"), row.get("c1", "—")))
        self._set_tree(self.club_history_comp_tree, data.get("by_competition", []), lambda _i, row: (
            row.get("competition", "—"), row.get("season", "—"), row.get("finish", "—")))
        if not data.get("by_season"):
            self.club_history_season_tree.insert("", "end", values=("—", "Chưa có dữ liệu", "", "", "", ""))
        if not data.get("by_competition"):
            self.club_history_comp_tree.insert("", "end", values=("—", "—", "Chưa có thành tích"))
        seasons = [str(value) for value in data.get("seasons", [])]
        self.club_history_season_combo.configure(values=seasons)
        if self.club_history_season_var.get() not in seasons:
            self.club_history_season_var.set(seasons[0] if seasons else "")
        self._refresh_club_history_matches()
        top11 = data.get("top11", [])
        self._set_tree(self.club_history_top11_tree, top11, lambda i, row: (
            i + 1, row.get("player", "—"), row.get("position", "—"),
            row.get("apps", 0), row.get("goals", 0), row.get("assists", 0)))
        if not top11:
            self.club_history_top11_tree.insert("", "end", values=("—", "Chưa có dữ liệu ra sân", "", "", "", ""))

    def refresh_development(self):
        rows=self.game.s.get("last_development",[])
        if not rows and self.game.s.get("history"):
            rows=self.game.s["history"][-1].get("development",[])
        rows=sorted(rows,key=lambda r:(-r.get("delta",0),r.get("player","")))
        self._set_tree(self.dev_tree,rows,lambda i,r:(r.get("player"),r.get("team"),r.get("position"),r.get("old_ovr"),r.get("new_ovr"),
            f"{r.get('delta',0):+}",round(r.get("age",0),1),r.get("potential"),r.get("minutes",0),
            r.get("injury_matches",0),len(r.get("attribute_changes",[]))))

    def on_history_detail(self,event=None):
        selected=self.history_tree.selection()
        if not selected: return
        label=self.history_tree.item(selected[0],"values")[0]
        record=next((r for r in self.game.s.get("history",[]) if r.get("label")==label),None)
        if not record: return
        top=tk.Toplevel(self.root); top.title(f"Lịch sử mùa {label}"); top.configure(bg=BG); top.transient(self.root)
        top.bind("<Escape>", lambda _e: top.destroy())
        self._center_dialog(top, 1000, 720)
        shell=tk.Frame(top,bg=BG); shell.pack(fill="both",expand=True,padx=22,pady=18)
        tk.Label(shell,text=f"Mùa {label}",font=F(18,"bold"),bg=BG,fg=TEXT).pack(anchor="w",pady=(0,12))
        tabs=Segmented(shell); tabs.pack(fill="both",expand=True)
        match_map={int(m["id"]):m for m in record.get("matches",[])}
        def tab_text(title,body):
            frame=tabs.add(title,title)
            txt=self._scrolled_text(frame,mono=True,bg=SURFACE_2,size=10,padx=16,pady=12)
            self._set_text(txt,body)
        overview=(f"MÙA {label}\n\nVô địch Premier League: {record.get('champion_premier')}\n"
                  f"Vô địch Cup: {record.get('champion_cup')} (á quân: {record.get('runner_up_cup')})\n"
                  f"Vô địch C1: {record.get('champion_c1')} (á quân: {record.get('runner_up_c1')})\n"
                  f"Tổng số trận: {record.get('match_count')}\n\nLÊN HẠNG\n"+
                  "\n".join(f"{x['team']}: {TIER_LABELS[x['from']]} → {TIER_LABELS[x['to']]}" for x in record.get("promoted",[]))+
                  "\n\nXUỐNG HẠNG\n"+
                  "\n".join(f"{x['team']}: {TIER_LABELS[x['from']]} → {TIER_LABELS[x['to']]}" for x in record.get("relegated",[])))
        tab_text("Tổng kết",overview)
        league_lines=[]
        for tier in ("premier","fc1","fc2"):
            league_lines.append(TIER_LABELS[tier].upper())
            for i,r in enumerate(record.get("leagues",{}).get(tier,[]),1):
                league_lines.append(f"{i:>2}. {r['team']:<24} {r['pts']:>2}đ | {r['w']}T {r['d']}H {r['l']}B | {r['gf']}:{r['ga']} ({r['gd']:+})")
            league_lines.append("")
        tab_text("Liên đoàn", "\n".join(league_lines))
        cup_lines=["CUP · NHÁNH ĐẤU"]
        cup_names={"r1":"Vòng 1","r16":"Vòng 16","qf":"Tứ kết","sf":"Bán kết","final":"Chung kết"}
        for key in ("r1","r16","qf","sf","final"):
            cup_lines.append("\n"+cup_names[key])
            for mid in record.get("cup_bracket",{}).get(key,[]):
                m=match_map.get(int(mid))
                if m: cup_lines.append("  "+self._format_match(m))
        c1=record.get("c1",{})
        cup_lines.append("\nC1 · PLAY-IN")
        for group,entry in c1.get("playin",{}).items():
            cup_lines.append(f"Bảng {group}: {', '.join(entry.get('teams',[]))}")
            for mid in entry.get("r1",[]):
                m=match_map.get(int(mid))
                if m: cup_lines.append("  "+self._format_match(m))
            for key in ("winner_match","loser_match","decider"):
                if entry.get(key):
                    m=match_map.get(int(entry[key]))
                    if m: cup_lines.append("  "+self._format_match(m))
        for group,table in c1.get("group_tables",{}).items():
            cup_lines.append(f"\nC1 · BẢNG {group}")
            for i,r in enumerate(table,1): cup_lines.append(f"  {i}. {r['team']} — {r['pts']}đ ({r['gf']}:{r['ga']})")
        cup_lines.append("\nC1 · BÁN KẾT / CHUNG KẾT")
        for mid in c1.get("bracket",{}).get("semis",[]):
            m=match_map.get(int(mid))
            if m: cup_lines.append("  "+self._format_match(m))
        mid=c1.get("bracket",{}).get("final")
        if mid and match_map.get(int(mid)): cup_lines.append("  "+self._format_match(match_map[int(mid)]))
        tab_text("Cup & C1", "\n".join(cup_lines))
        player_lines=["VUA PHÁ LƯỚI"]
        for i,p in enumerate(record.get("top_scorers",[])[:15],1): player_lines.append(f"{i}. {p['player']} ({p['team']}) — {p['goals']} bàn, {p['assists']} kiến tạo")
        player_lines.append("\nVUA KIẾN TẠO")
        for i,p in enumerate(record.get("top_assists",[])[:15],1): player_lines.append(f"{i}. {p['player']} ({p['team']}) — {p['assists']} kiến tạo, {p['goals']} bàn")
        player_lines.append("\nGIẢI NGHỆ")
        for p in record.get("retired",[]): player_lines.append(f"{p['player_name']} ({p['club']}) — {p['age']} tuổi, OVR cao nhất {p['overall_peak']}")
        player_lines.append("\nCẦU THỦ TRẺ ĐƯỢC ĐÔN LÊN")
        for p in record.get("youth_promoted",[]): player_lines.append(f"{p['player']} ({p['team']}) — {p['position']}, OVR {p['overall']}, Potential {p['potential']}")
        tab_text("Cầu thủ", "\n".join(player_lines))
        award_lines = ["GIẢI THƯỞNG CÁ NHÂN MÙA GIẢI"]
        annual = record.get("season_awards") or {}
        podium = annual.get("ballon_podium") or []
        if podium:
            for index, winner in enumerate(podium[:3], 1):
                medal = ("Quả bóng Vàng", "Quả bóng Bạc", "Quả bóng Đồng")[index - 1]
                award_lines.append(f"{medal}: {winner.get('player', '—')} ({winner.get('team', '—')}) · {winner.get('vote_score', '—')} điểm")
        else:
            for key, label in (("ballon_dor", "Quả bóng Vàng"), ("ballon_silver", "Quả bóng Bạc"),
                               ("ballon_bronze", "Quả bóng Đồng")):
                winner = annual.get(key) or {}
                if winner:
                    award_lines.append(f"{label}: {winner.get('player', '—')} ({winner.get('team', '—')})")
        golden_boy = annual.get("young_player") or annual.get("golden_boy") or {}
        if golden_boy:
            subject = golden_boy.get("player") or golden_boy.get("player_name") or "—"
            club = golden_boy.get("team") or golden_boy.get("club") or "—"
            award_lines.append(f"Golden Boy: {subject} ({club})")
        for key, label in (("golden_glove", "Găng tay Vàng"), ("best_coach", "HLV xuất sắc")):
            winner = annual.get(key) or {}
            if winner:
                subject = winner.get("coach") or winner.get("manager") or winner.get("player") or winner.get("name") or "—"
                club = winner.get("team") or winner.get("club") or "—"
                award_lines.append(f"{label}: {subject} ({club})")
        best_xi = annual.get("best_xi_442") or []
        award_lines.append("\nĐỘI HÌNH TIÊU BIỂU 4-4-2")
        if best_xi:
            for row in best_xi:
                award_lines.append(f"{row.get('position', '—'):>3} · {row.get('player', '—')} ({row.get('team', '—')}) · {row.get('avg_rating', '—')}")
        else:
            award_lines.append("Không có dữ liệu đội hình tiêu biểu.")
        award_lines.append("\nCẦU THỦ XUẤT SẮC TỪNG GIẢI")
        comp_awards = record.get("competition_awards") or {}
        found_comp_award = False
        for key, award in sorted(comp_awards.items()):
            if not isinstance(award, dict):
                continue
            winner = award.get("player_of_tournament") or {}
            if winner:
                found_comp_award = True
                award_lines.append(f"{award.get('competition') or key}: {winner.get('player', '—')} ({winner.get('team', '—')}) · "
                                   f"{winner.get('apps', '—')} trận · {winner.get('goals', '—')} bàn · {winner.get('assists', '—')} kiến tạo")
        if not found_comp_award:
            award_lines.append("Không có dữ liệu giải thưởng từng giải.")
        tab_text("Giải thưởng", "\n".join(award_lines))
        match_lines = [f"LỊCH SỬ {len(record.get('matches', []) or [])} TRẬN ĐẤU", ""]
        for match in sorted(record.get("matches", []) or [], key=lambda item: (str(item.get("date", "")), int(item.get("id", 0) or 0))):
            line = f"{match.get('date', '—')} · {match.get('comp', '—')} · {match.get('round', '—')}\n  {self._format_match(match)}"
            events = []
            for goal in match.get("goals", []) or []:
                minute = goal.get("minute", "?")
                scorer = goal.get("scorer") or "Không rõ"
                team = goal.get("team") or ""
                suffix = " (pen)" if goal.get("pen") else ""
                events.append(f"  ⚽ {minute}' {scorer}{suffix} · {team}")
            for card in match.get("red_cards", []) or []:
                minute = card.get("minute", "?")
                player = card.get("player") or "Không rõ"
                team = card.get("team") or ""
                kind = "thẻ vàng thứ hai" if card.get("type") == "second_yellow" else "thẻ đỏ"
                events.append(f"  🟥 {minute}' {player} · {team} ({kind})")
            if events:
                line += "\n" + "\n".join(events)
            else:
                line += "\n  Không có bàn thắng hoặc thẻ đỏ."
            match_lines.append(line)
        tab_text("Trận đấu", "\n\n".join(match_lines))

    def _open_saved_match_replay(self, match):
        """Backward-compatible route: past matches are report-only, never replayed."""
        self._show_match_report(match)

    def on_match_detail(self, event=None):
        selected = self.fixture_tree.selection()
        if not selected:
            return
        row = getattr(self, "fixture_rows_by_iid", {}).get(selected[0])
        if row and not row.get("_played"):
            self.toast("Chỉ trận thuộc slot kế tiếp mới mở được Live Match. Trận ở slot tương lai đang bị khóa.", "warn")
            return
        if not row:
            return
        match = next((m for m in self.game.s.get("matches", [])
                      if m.get("date") == row.get("date") and m.get("comp") == row.get("comp")
                      and m.get("round") == row.get("round") and m.get("home") == row.get("home")
                      and m.get("away") == row.get("away") and m.get("played")), None)
        if match:
            self._show_match_report(match)

    def on_cup_match_click(self, event=None):
        tree = getattr(self, "cup_tree", None)
        if tree is None:
            return
        iid = tree.identify_row(event.y) if event is not None else (tree.selection() or [None])[0]
        match = getattr(self, "cup_match_rows", {}).get(iid)
        if match:
            self._show_match_report(match)

    def on_close(self):
        self._full_season_running = False
        self._full_season_cancel_requested = True
        after_id = getattr(self, "_full_season_after_id", None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
            self._full_season_after_id = None
        try: self.game.save()
        except Exception: pass
        try: self.game.close_google_sheets_sync(timeout=1.5)
        except Exception: pass
        self.root.destroy()

    def run(self): self.root.mainloop()# -*- coding: utf-8 -*-
