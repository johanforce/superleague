#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Super League LIVE UI - trang xem trận đấu trực tiếp (tkinter).
===============================================================
LivePage phát lại danh sách "beat" do LiveMatch dựng và có thể theo dõi bảng tỷ số
các trận khác cùng giải, cùng kickoff trong chế độ mô phỏng đồng thời:
  * Sân bóng (Canvas): 22 cầu thủ di chuyển theo bóng, bóng chạy theo từng đường chuyền / cú sút / bàn thắng.
  * Bảng tỷ số + đồng hồ (có bù giờ, nghỉ giữa hiệp, hiệp phụ, luân lưu).
  * Tab Diễn biến, Thống kê, Đội hình, Tỷ số & thẻ và BXH Live của giải đang đá.
  * Sau khi kết thúc loạt trận, báo cáo có tab riêng cho từng trận để xem thống kê.
  * Điều khiển: tạm dừng, tốc độ x1 (≈5 phút cho 90') .. x30, chuyển nhanh đến hết, ẩn/hiện đường chuyền.
Khi trận kết thúc, kết quả được ghi vào game bằng Game.commit_live() (cùng cơ chế với mô phỏng nhanh).
"""
import math
import random
import tkinter as tk
from tkinter import ttk, messagebox

from . import report_ui as wnl_report_ui
from . import stats as wnl_stats
from .adapter import restore_live_bundle
from .live import LiveMatch

BG, SURFACE, SURFACE_2 = '#0c1319', '#131c25', '#0f1820'
TEXT, MUTED, LINE = '#e9edf1', '#9aa8b7', '#34485b'
HOME_C, AWAY_C = '#8fc8ff', '#ff9da4'
HOME_GK, AWAY_GK = '#9cc0ff', '#ffb0aa'
SPEEDS = [("x1 (≈5 phút)", 1), ("x2", 2), ("x4", 4), ("x10", 10), ("x30", 30)]
TICK_MS = 40
VAR_HOLD = 4.0                # giây thực (ở x1) đồng hồ dừng khi VAR kiểm tra
HT_HOLD = 5.0                 # giây thực (ở x1) nghỉ giữa hiệp
SHOT_KINDS = ('goal', 'save', 'off', 'block')
CARRY_KINDS = ('pass', 'long', 'cross', 'dribble', 'throw', 'tackle')
EXACT_SHOT = ('goal', 'save', 'off', 'block')


def concurrent_fixture_detail_text(row):
    """Readable goal and red-card details for one synchronized fixture."""
    row = dict(row or {})
    home = row.get("home", "Chủ nhà")
    away = row.get("away", "Đội khách")
    home_goals = row.get("goal_home") or []
    away_goals = row.get("goal_away") or []
    home_red = row.get("red_home") or []
    away_red = row.get("red_away") or []
    details = [
        f"{row.get('comp', '')} · {home} – {away}",
        f"Tỷ số: {row.get('score', '—')} · {row.get('status', '—')}",
        "BÀN THẮNG",
    ]

    def goal_line(team, goals):
        if not goals:
            return f"{team}: Chưa có bàn thắng"
        scorers = []
        for item in goals:
            label = f"{item.get('minute', '')} {item.get('player', 'Cầu thủ')}".strip()
            if item.get("pen"):
                label += " (pen)"
            scorers.append(label)
        return f"{team}: " + ", ".join(scorers)

    details.append(goal_line(home, home_goals))
    details.append(goal_line(away, away_goals))
    def red_label(item):
        # Saved fixtures store red-card details as text; live trackers store dicts.
        if isinstance(item, dict):
            player = item.get("player", "Cầu thủ")
            minute = item.get("minute", "")
            return f"{player} ({minute})" if minute else str(player)
        return str(item)

    red_lines = []
    if home_red:
        red_lines.append("Chủ nhà: " + ", ".join(red_label(item) for item in home_red))
    if away_red:
        red_lines.append("Đội khách: " + ", ".join(red_label(item) for item in away_red))
    # Theo yêu cầu: hoàn toàn không thêm mục thẻ đỏ khi không có ai bị đuổi.
    if red_lines:
        details.append("THẺ ĐỎ")
        details.extend(red_lines)
    return "\n".join(details)


def wnl_live_scaled():
    from . import live as wnl_live
    return wnl_live.SCALED


class ConcurrentFixtureTracker:
    """A lightweight synchronized timeline for another same-competition fixture at kickoff."""

    def __init__(self, preview):
        self.preview = preview
        self.spec_key = preview["spec_key"]
        prep = restore_live_bundle((preview.get("data") or {}).get("live_bundle"))
        if not prep:
            raise RuntimeError("Không thể khôi phục dữ liệu một trận cùng giờ.")
        # LiveMatch uses the module RNG for stoppage-time presentation. Preserve the
        # active match's RNG stream while constructing companion score timelines.
        state = random.getstate()
        try:
            random.seed(0x51A7 + int(preview.get("match_id", 0)) * 7919)
            self.lm = LiveMatch(
                preview["home"], preview["away"], prep,
                neutral=bool(preview.get("neutral")),
                knockout=bool(preview.get("force_winner")),
            )
        finally:
            random.setstate(state)
        self.i = 0
        self.score = [0, 0]
        self.goals = {"h": [], "a": []}
        self.reds = {"h": [], "a": []}
        self.pens = None
        self.finished = False

    def update_to(self, current_T, force_finish=False):
        limit = self.lm.final_T if force_finish else min(float(current_T), self.lm.final_T)
        beats = self.lm.beats
        while self.i < len(beats) and beats[self.i].get("T", 0) <= limit:
            beat = beats[self.i]
            self.i += 1
            kind, side = beat.get("k"), beat.get("side")
            side_index = 0 if side == "h" else 1
            if kind == "goal" and side:
                self.score[side_index] += 1
                player_id = beat.get("frm")
                self.goals[side].append({
                    "player": self.lm.name(side, player_id) if player_id else "Cầu thủ",
                    "minute": self.lm.label(beat.get("T", 0)),
                    "pen": bool(beat.get("pen")),
                    "pending_var": beat.get("var") == "pending",
                })
            elif kind == "var_decision" and side and beat.get("goal"):
                if beat.get("overturn"):
                    self.score[side_index] = max(0, self.score[side_index] - 1)
                    for goal_index in range(len(self.goals[side]) - 1, -1, -1):
                        if self.goals[side][goal_index].get("pending_var"):
                            self.goals[side].pop(goal_index)
                            break
                else:
                    for goal in reversed(self.goals[side]):
                        if goal.get("pending_var"):
                            goal["pending_var"] = False
                            break
            elif kind == "red" and side:
                player_id = beat.get("frm")
                self.reds[side].append({
                    "player": self.lm.name(side, player_id),
                    "minute": self.lm.label(beat.get("T", 0)),
                })
            elif kind == "pen_kick":
                self.pens = tuple(beat.get("pens", (0, 0)))
            elif kind == "end":
                self.finished = True
        if force_finish:
            self.finished = True
        minute = "FT" if self.finished else self.lm.label(limit)
        score = f"{self.score[0]}–{self.score[1]}"
        if self.pens:
            score += f" (P {self.pens[0]}–{self.pens[1]})"
        return {
            "score": score,
            # Numeric score fields are consumed by live standings and tests;
            # keep them alongside the human-readable score label.
            "live_hg": int(self.score[0]),
            "live_ag": int(self.score[1]),
            "goal_home": [{key: value for key, value in item.items() if key != "pending_var"}
                          for item in self.goals["h"]],
            "goal_away": [{key: value for key, value in item.items() if key != "pending_var"}
                          for item in self.goals["a"]],
            "red_home": list(self.reds["h"]),
            "red_away": list(self.reds["a"]),
            "status": "FT" if self.finished else f"{minute} · LIVE",
            "played": self.finished,
        }


class LivePage:
    def __init__(self, app, parent, entry, m, prep, lm):
        self.app, self.g, self.m, self.prep, self.lm = app, app.g, m, prep, lm
        self.parent, self.entry = parent, entry
        entry['on_back'] = self.on_back
        self.home, self.away = m['home'], m['away']
        self.P = lm.P
        self.i, self.T, self.hold = 0, 0.0, 0.0
        self.speed, self.paused = 1, False
        self.finished = self.committed = self.dead = False
        self.rate = lm.reg_end / 300.0        # phút trận / giây thực ở x1  => 90' + bù giờ ≈ 300 giây
        self.score = [0, 0]
        self.pens = None
        self.scorers = {'h': [], 'a': []}
        self.goal_log = {'h': [], 'a': []}
        self.card_log = {'h': [], 'a': []}
        self.on = {s: list(self.P[s]['xi_ids']) for s in ('h', 'a')}
        self.yc, self.rc, self.inj = {'h': set(), 'a': set()}, {'h': set(), 'a': set()}, {'h': set(), 'a': set()}
        self.sub_on, self.sub_off = {'h': set(), 'a': set()}, {'h': set(), 'a': set()}
        self.ex = {k: [0, 0] for k in ('shots', 'sot', 'off', 'blk', 'big', 'corners', 'offs', 'fouls', 'yc', 'rc', 'sv', 'varck', 'gdis')}
        self.var_hold = False
        self.cum = {k: [0, 0] for k in wnl_live_scaled()}
        self.xg = [0.0, 0.0]
        self.ptime = {'h': 0.0001, 'a': 0.0001}
        self.poss_side = 'h'
        self.ball, self.ball_t = (0.5, 0.5), (0.5, 0.5)
        self.carry = None
        self.pos = {}
        self.tt = 0.0
        self.stats_dirty = True
        self.banner, self.banner_ticks = '', 0
        self.after_id = None
        self.report_widget = None
        self.concurrent_trackers = {}
        for preview in getattr(self.g, "companion_previews", []) or []:
            tracker = ConcurrentFixtureTracker(preview)
            self.concurrent_trackers[tracker.spec_key] = tracker
        self._build_ui()
        self.refresh_lineups()
        self.refresh_match_summary()
        self.refresh_live_standings()
        self.update_stats()
        self.after_id = self.app.root.after(600, self.tick)

    # ------------------------------------------------------------------ giao diện
    def _build_ui(self):
        p = self.parent
        sb = ttk.Frame(p)
        sb.pack(fill='x', padx=10, pady=(0, 4))
        sb.columnconfigure(0, weight=1, uniform='x')
        sb.columnconfigure(2, weight=1, uniform='x')
        big = ('TkDefaultFont', 16, 'bold')
        self.lbl_h = ttk.Label(sb, text=self.home, font=big, foreground=HOME_C, anchor='e')
        self.lbl_h.grid(row=0, column=0, sticky='e', padx=10)
        self.lbl_score = ttk.Label(sb, text="0 - 0", font=('TkDefaultFont', 26, 'bold'))
        self.lbl_score.grid(row=0, column=1)
        self.lbl_a = ttk.Label(sb, text=self.away, font=big, foreground=AWAY_C, anchor='w')
        self.lbl_a.grid(row=0, column=2, sticky='w', padx=10)
        self.lbl_sc_h = ttk.Label(sb, text="", anchor='e', justify='right', wraplength=380)
        self.lbl_sc_h.grid(row=1, column=0, sticky='e', padx=10)
        self.lbl_clock = ttk.Label(sb, text="0'", font=('TkDefaultFont', 14, 'bold'))
        self.lbl_clock.grid(row=1, column=1)
        self.lbl_sc_a = ttk.Label(sb, text="", anchor='w', justify='left', wraplength=380)
        self.lbl_sc_a.grid(row=1, column=2, sticky='w', padx=10)
        self.lbl_phase = ttk.Label(sb, text="Chuẩn bị khai cuộc...", foreground=MUTED)
        self.lbl_phase.grid(row=2, column=1)
        ctx_txt = getattr(self.lm, 'ctx_txt', '')
        if ctx_txt:
            ttk.Label(p, text=ctx_txt, foreground=MUTED, anchor='center', wraplength=980).pack(fill='x', padx=12)
        self.lbl_tick = ttk.Label(p, text="", font=('TkDefaultFont', 10, 'italic'), anchor='w')
        self.lbl_tick.pack(fill='x', padx=12)

        mid = ttk.Frame(p)
        self.mid = mid
        mid.pack(fill='both', expand=True, padx=8, pady=4)
        self.canvas = tk.Canvas(mid, bg='#2e8b3e', highlightthickness=0, width=760, height=470)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda e: self.draw_pitch())
        right = ttk.Notebook(mid, width=390)
        right.pack(side='right', fill='y', padx=(8, 0))
        # --- diễn biến
        f1 = ttk.Frame(right)
        right.add(f1, text="Diễn biến")
        self.txt = tk.Text(f1, wrap='word', width=48, state='disabled', font=('TkDefaultFont', 10),
                           bg=SURFACE_2, fg=TEXT, insertbackground=TEXT, selectbackground='#23415a',
                           selectforeground='#ffffff', relief='flat', highlightthickness=1, highlightbackground=LINE)
        sbr = ttk.Scrollbar(f1, orient='vertical', command=self.txt.yview)
        self.txt.configure(yscrollcommand=sbr.set)
        sbr.pack(side='right', fill='y')
        self.txt.pack(side='left', fill='both', expand=True)
        cfg = {
            'goal': dict(background='#153a2a', foreground='#b7f4c9', font=('TkDefaultFont', 10, 'bold')),
            'yellow': dict(background='#3a3218', foreground='#ffe2a8'),
            'red': dict(background='#3d2025', foreground='#ffb5b9', font=('TkDefaultFont', 10, 'bold')),
            'injury': dict(background='#302443', foreground='#e1c7ff'),
            'sub': dict(background='#1b2e43', foreground='#c5ddff'),
            'mark': dict(font=('TkDefaultFont', 10, 'bold'), foreground=TEXT),
            'minor': dict(elide=False, foreground=MUTED),
            'pen_kick': dict(background='#3a3218', foreground='#ffe2a8'),
            'var': dict(background='#292744', foreground='#d2ccff', font=('TkDefaultFont', 10, 'bold')),
            'tactic': dict(background='#24372d', foreground='#c6efd2')}
        for k, v in cfg.items():
            self.txt.tag_configure(k, **v)
        # --- thống kê
        f2 = ttk.Frame(right)
        right.add(f2, text="Thống kê")
        self.sbars = wnl_report_ui.StatBars(f2, self.home, self.away)
        self.sbars.pack(fill='both', expand=True)
        # --- đội hình
        f3 = ttk.Frame(right)
        right.add(f3, text="Đội hình")
        self.lu = tk.Text(f3, wrap='none', width=48, state='disabled', font=('TkFixedFont', 9),
                         bg=SURFACE_2, fg=TEXT, insertbackground=TEXT, selectbackground='#23415a',
                         selectforeground='#ffffff', relief='flat', highlightthickness=1, highlightbackground=LINE)
        sb3 = ttk.Scrollbar(f3, orient='vertical', command=self.lu.yview)
        self.lu.configure(yscrollcommand=sb3.set)
        sb3.pack(side='right', fill='y')
        self.lu.pack(side='left', fill='both', expand=True)
        self.lu.tag_configure('hdr', font=('TkFixedFont', 9, 'bold'))
        # --- tỷ số & thẻ của các trận cùng giải/cùng giờ, không lặp lại trận đang xem
        f4 = ttk.Frame(right)
        right.add(f4, text="Tỷ số & thẻ")
        self.summary_clock = ttk.Label(
            f4, text="Các trận khác cùng giải, cùng giờ", foreground=MUTED,
            anchor="w", wraplength=350,
        )
        self.summary_clock.pack(fill="x", padx=8, pady=(8, 4))
        summary_wrap = ttk.Frame(f4)
        summary_wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        columns = ("match", "score", "red", "status")
        self.summary_tree = ttk.Treeview(summary_wrap, columns=columns, show="headings", height=12)
        for key, title, width, anchor in (
            ("match", "Trận cùng giờ", 150, "w"),
            ("score", "Tỷ số", 58, "center"),
            ("red", "Đỏ H–K", 58, "center"),
            ("status", "Phút / TT", 78, "center"),
        ):
            self.summary_tree.heading(key, text=title, anchor=anchor)
            self.summary_tree.column(key, width=width, minwidth=42, anchor=anchor, stretch=(key == "match"))
        summary_scroll = ttk.Scrollbar(summary_wrap, orient="vertical", command=self.summary_tree.yview)
        self.summary_tree.configure(yscrollcommand=summary_scroll.set)
        self.summary_tree.pack(side="left", fill="both", expand=True)
        summary_scroll.pack(side="right", fill="y")
        self.summary_tree.tag_configure("played", foreground="#9be0b9")
        self.summary_tree.tag_configure("live", foreground="#e3b94e")
        self.summary_tree.tag_configure("pending", foreground=MUTED)
        self.summary_tree.tag_configure("red", foreground="#ffb5b9")
        self.summary_rows = {}
        self.summary_tree.bind("<<TreeviewSelect>>", self._show_concurrent_fixture_details)
        self.summary_detail = ttk.Label(
            f4, text="Chọn trận để xem bàn thắng và thẻ đỏ (nếu có).", foreground=MUTED,
            anchor="w", justify="left", wraplength=355,
        )
        self.summary_detail.pack(fill="x", padx=9, pady=(2, 9))
        # --- bảng xếp hạng League cập nhật trực tiếp theo tỷ số đang diễn ra
        f5 = ttk.Frame(right)
        right.add(f5, text="BXH Live")
        self.live_standings_title = ttk.Label(
            f5, text="BẢNG XẾP HẠNG LIVE", foreground=TEXT,
            font=("TkDefaultFont", 10, "bold"), anchor="w", wraplength=355,
        )
        self.live_standings_title.pack(fill="x", padx=9, pady=(8, 3))
        self.live_standings_note = ttk.Label(
            f5, text="Đang chờ dữ liệu…", foreground=MUTED,
            anchor="w", justify="left", wraplength=355,
        )
        self.live_standings_note.pack(fill="x", padx=9, pady=(0, 6))
        standings_wrap = ttk.Frame(f5)
        standings_wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        standings_columns = ("pos", "team", "played", "record", "goals", "gd", "pts")
        self.live_standings_tree = ttk.Treeview(
            standings_wrap, columns=standings_columns, show="headings", height=13,
        )
        for key, title, width, anchor in (
            ("pos", "#", 27, "center"),
            ("team", "CLB", 112, "w"),
            ("played", "Tr", 30, "center"),
            ("record", "T-H-B", 49, "center"),
            ("goals", "BT-BB", 55, "center"),
            ("gd", "HS", 34, "center"),
            ("pts", "Đ", 35, "center"),
        ):
            self.live_standings_tree.heading(key, text=title, anchor=anchor)
            self.live_standings_tree.column(
                key, width=width, minwidth=width, anchor=anchor,
                stretch=(key == "team"),
            )
        standings_scroll = ttk.Scrollbar(
            standings_wrap, orient="vertical", command=self.live_standings_tree.yview,
        )
        self.live_standings_tree.configure(yscrollcommand=standings_scroll.set)
        self.live_standings_tree.pack(side="left", fill="both", expand=True)
        standings_scroll.pack(side="right", fill="y")
        self.live_standings_tree.tag_configure("section", foreground="#e3b94e", background=SURFACE)
        self.live_standings_tree.tag_configure("live", foreground="#9be0b9")
        # --- điều khiển
        ctl = ttk.Frame(p)
        self.ctl = ctl
        ctl.pack(fill='x', padx=8, pady=(2, 8))
        self.btn_pause = ttk.Button(ctl, text="Tạm dừng", width=14, command=self.toggle_pause)
        self.btn_pause.pack(side='left', padx=3)
        ttk.Label(ctl, text="Tốc độ:").pack(side='left', padx=(10, 2))
        self.cb_speed = ttk.Combobox(ctl, values=[s[0] for s in SPEEDS], state='readonly', width=12)
        self.cb_speed.current(0)
        self.cb_speed.pack(side='left')
        self.cb_speed.bind('<<ComboboxSelected>>', lambda e: setattr(self, 'speed', SPEEDS[self.cb_speed.current()][1]))
        self.btn_skip = ttk.Button(ctl, text="Chuyển nhanh đến hết", command=self.skip_to_end)
        self.btn_skip.pack(side='left', padx=10)
        self.show_minor = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Hiện cả đường chuyền / pha bóng nhỏ", variable=self.show_minor,
                        command=self.apply_minor).pack(side='left', padx=6)
        self.btn_view = ttk.Button(ctl, text="Xem lại diễn biến trên sân", command=self.toggle_view)
        self.btn_done = ttk.Button(ctl, text="Quay lại giải đấu", command=self.app.go_back)
        self.btn_done.pack(side='right', padx=3)
        self.app.root.bind('<space>', lambda e: self.toggle_pause() if self.app.stack and self.app.stack[-1] is self.entry else None)

    def apply_minor(self):
        self.txt.tag_configure('minor', elide=not self.show_minor.get())
        self.txt.see('end')

    # ------------------------------------------------------------------ vẽ sân
    def _px(self, x, y):
        W, H = max(300, self.canvas.winfo_width()), max(200, self.canvas.winfo_height())
        mx, my = 22, 16
        return mx + x * (W - 2 * mx), my + y * (H - 2 * my)

    def draw_pitch(self):
        c = self.canvas
        c.delete('bg')
        ln = dict(outline='white', width=2, tags='bg')
        for i in range(10):
            a, b = self._px(i / 10.0, 0), self._px((i + 1) / 10.0, 1)
            c.create_rectangle(a[0], a[1], b[0], b[1], fill='#2e8b3e' if i % 2 == 0 else '#34973f', outline='', tags='bg')
        a, b = self._px(0, 0), self._px(1, 1)
        c.create_rectangle(a[0], a[1], b[0], b[1], **ln)
        m0, m1 = self._px(0.5, 0), self._px(0.5, 1)
        c.create_line(m0[0], m0[1], m1[0], m1[1], fill='white', width=2, tags='bg')
        o = self._px(0.5, 0.5)
        r = (self._px(0.5 + 0.087, 0.5)[0] - o[0])
        c.create_oval(o[0] - r, o[1] - r, o[0] + r, o[1] + r, **ln)
        for x0, x1, yh, dep in ((0.0, 0.157, 0.2965, 'L'), (1.0, 0.843, 0.2965, 'R'), (0.0, 0.052, 0.135, 'L'), (1.0, 0.948, 0.135, 'R')):
            p0, p1 = self._px(x0, 0.5 - yh), self._px(x1, 0.5 + yh)
            c.create_rectangle(p0[0], p0[1], p1[0], p1[1], **ln)
        for x, sgn in ((0.0, -1), (1.0, 1)):
            p0, p1 = self._px(x, 0.45), self._px(x + sgn * 0.012, 0.55)
            c.create_rectangle(p0[0], p0[1], p1[0], p1[1], fill='#eeeeee', outline='white', tags='bg')
        c.tag_lower('bg')

    def animate(self):
        c = self.canvas
        c.delete('dyn')
        W, H = max(300, c.winfo_width()), max(200, c.winfo_height())
        rad = max(6, min(W, H) / 52.0)
        self.ball = (self.ball[0] + (self.ball_t[0] - self.ball[0]) * 0.22, self.ball[1] + (self.ball_t[1] - self.ball[1]) * 0.22)
        bx, by = self.ball
        lm = self.lm
        holder = self.carry[1] if (self.carry and self.carry[0] == self.poss_side) else None
        m = lm.m_of_T(min(self.T, lm.end_T))
        try:
            tgt = lm.shape.positions(self.on, self.T + self.tt, self.ball, self.poss_side, holder, tuple(self.score), m)
        except Exception:
            tgt = {}
        self.tt += 0.012
        for side in ('h', 'a'):
            col_o, col_g = (HOME_C, HOME_GK) if side == 'h' else (AWAY_C, AWAY_GK)
            for pid in self.on[side]:
                e = self.P[side]['players'][pid]
                t = tgt.get((side, pid))
                if t is None:
                    continue
                cur = self.pos.setdefault((side, pid), [t[0], t[1]])
                sp = 0.20 if (self.carry == (side, pid)) else 0.11
                cur[0] += (t[0] - cur[0]) * sp
                cur[1] += (t[1] - cur[1]) * sp
                px, py = self._px(min(0.99, max(0.01, cur[0])), min(0.99, max(0.01, cur[1])))
                hl = self.carry == (side, pid)
                c.create_oval(px - rad, py - rad, px + rad, py + rad, fill=col_g if e['group'] == 'GK' else col_o,
                              outline='#ffe94d' if hl else 'white', width=3 if hl else 1, tags='dyn')
                nm = e['name'].split()[-1][:9]
                c.create_text(px, py + rad + 7, text=nm, fill='white', font=('TkDefaultFont', 7), tags='dyn')
        px, py = self._px(bx, by)
        c.create_oval(px - 5, py - 5, px + 5, py + 5, fill='white', outline='black', width=1, tags='dyn')
        if self.banner_ticks > 0:
            self.banner_ticks -= 1
            c.create_text(W / 2, H / 2, text=self.banner, fill='white', font=('TkDefaultFont', 30, 'bold'), tags='dyn')

    # ------------------------------------------------------------------ vòng lặp phát
    def tick(self):
        self.after_id = None
        if self.dead:
            return
        if not self.paused and not self.finished:
            step = (TICK_MS / 1000.0) * self.speed
            if self.hold > 0:
                self.hold -= step
                if self.hold <= 0:
                    self.hold = 0
                    self.var_hold = False
            else:
                dm = step * self.rate
                self.T = min(self.T + dm, self.lm.final_T)
                self.ptime[self.poss_side] += dm
                self.consume()
        self.animate()
        self.update_clock()
        self.nt = getattr(self, 'nt', 0) + 1
        if self.nt % 6 == 0:
            self.refresh_match_summary()
            self.refresh_live_standings()
            if self.stats_dirty:
                self.update_stats()
        self.after_id = self.app.root.after(TICK_MS, self.tick)

    def consume(self):
        beats = self.lm.beats
        while self.i < len(beats) and beats[self.i]['T'] <= self.T and not self.finished:
            b = beats[self.i]
            self.i += 1
            self.handle(b)
            if b['k'] == 'ht':
                self.hold = HT_HOLD
                break
            if b['k'] == 'var_check':
                self.hold, self.var_hold = VAR_HOLD, True
                break

    def toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.configure(text="Tiếp tục" if self.paused else "Tạm dừng")

    def skip_to_end(self):
        if self.finished:
            return
        beats = self.lm.beats
        while self.i < len(beats):
            b = beats[self.i]
            self.i += 1
            self.T = b['T']
            self.handle(b)
        self.hold = 0

    def update_clock(self):
        lm, T = self.lm, self.T
        if self.finished:
            self.lbl_clock.configure(text="Hết giờ" if not self.pens else "Luân lưu")
            self.lbl_phase.configure(text="Trận đấu đã kết thúc - kết quả đã được lưu" if self.committed else "Trận đấu đã kết thúc")
            return
        if self.hold > 0 and self.var_hold:
            self.lbl_clock.configure(text=lm.label(T))
            self.lbl_phase.configure(text="VAR đang kiểm tra...")
            return
        if self.hold > 0:
            self.lbl_clock.configure(text="45'")
            self.lbl_phase.configure(text="Nghỉ giữa hiệp")
            return
        if T > lm.end_T:
            self.lbl_clock.configure(text="Luân lưu")
            self.lbl_phase.configure(text="Loạt sút luân lưu 11m")
            return
        self.lbl_clock.configure(text=lm.label(T))
        ph = "Hiệp 1" if T <= lm.ht_T else ("Hiệp 2" if T <= lm.reg_end else "Hiệp phụ")
        self.lbl_phase.configure(text=ph + ("  (đang tạm dừng)" if self.paused else ""))

    # ------------------------------------------------------------------ xử lý từng beat
    def handle(self, b):
        k, side = b['k'], b.get('side')
        si = 0 if side == 'h' else 1
        if b.get('ball'):
            self.ball_t = b['ball']
        if k in CARRY_KINDS and side:
            self.poss_side = side
            pid = b.get('to') or b.get('frm')
            self.carry = (side, pid) if (pid and k != 'cross' or (k == 'cross' and b.get('to'))) else None
        elif k in SHOT_KINDS or k == 'pen_kick':
            self.carry = None
            if side:
                self.poss_side = side
        elif k in ('foul', 'clearance', 'corner', 'goalkick', 'restart', 'kickoff', 'offside'):
            self.carry = None
            if side and k in ('clearance',):
                self.poss_side = side
        for tside, key in b.get('tg', ()):
            if key in self.cum:
                self.cum[key][0 if tside == 'h' else 1] += 1
        if k in SHOT_KINDS:
            ex = self.ex
            ex['shots'][si] += 1
            if k in ('goal', 'save'):
                ex['sot'][si] += 1          # bàn bị VAR huỷ vẫn là cú sút trúng đích
            if k == 'save':
                ex['sv'][1 - si] += 1
            if k == 'off':
                ex['off'][si] += 1
            if k == 'block':
                ex['blk'][si] += 1
            if (b.get('xg') or 0) >= 0.3:
                ex['big'][si] += 1
            if b.get('xg') is not None:
                self.xg[si] += b['xg']
            if k == 'goal':
                self.score[si] += 1           # nếu đang chờ VAR thì tỷ số chỉ tạm thời, var_decision sẽ chốt
                self.lbl_score.configure(text="%d - %d" % tuple(self.score))
                nm = self.lm.name(side, b['frm'])
                if b.get('var') == 'pending':
                    self.banner, self.banner_ticks = "BÀN THẮNG?  %s" % nm, 60
                else:
                    self._add_scorer(side, b)
                    self.banner, self.banner_ticks = "BÀN THẮNG!  %s" % nm, 70
        elif k == 'var_check':
            self.ex['varck'][si] += 1
            self.banner, self.banner_ticks = "VAR ĐANG KIỂM TRA", 90
        elif k == 'var_decision':
            if b.get('goal'):
                gi = si
                if b.get('overturn'):
                    self.score[gi] -= 1
                    self.ex['gdis'][gi] += 1
                    self.lbl_score.configure(text="%d - %d" % tuple(self.score))
                    self.banner, self.banner_ticks = "BÀN THẮNG BỊ HUỶ!", 90
                else:
                    gb = next((x for x in reversed(self.lm.beats[:self.i]) if x['k'] == 'goal' and x.get('side') == side), None)
                    if gb:
                        self._add_scorer(side, gb)
                    self.banner, self.banner_ticks = "BÀN THẮNG HỢP LỆ", 70
            else:
                self.banner, self.banner_ticks = ("PHẠT ĐỀN!" if b.get('vk') == 'penalty_awarded' else
                                                  "THẺ ĐỎ!" if b.get('vk') == 'red_upgraded' else "VAR: QUYẾT ĐỊNH"), 70
        elif k == 'tactic':
            self.refresh_lineups()
        elif k == 'corner' and side:
            self.ex['corners'][si] += 1
        elif k == 'offside' and side:
            self.ex['offs'][si] += 1
        elif k == 'foul' and side:
            self.ex['fouls'][si] += 1
        elif k == 'yellow':
            self.ex['yc'][si] += 1
            self.yc[side].add(b['frm'])
            self.card_log[side].append({'kind': 'yellow', 'player': self.lm.name(side, b['frm']),
                                        'minute': self.lm.label(b['T'])})
            self.refresh_lineups()
        elif k == 'red':
            self.ex['rc'][si] += 1
            self.rc[side].add(b['frm'])
            self.card_log[side].append({'kind': 'red', 'player': self.lm.name(side, b['frm']),
                                        'minute': self.lm.label(b['T'])})
            self._remove(side, b['frm'])
            self.refresh_lineups()
        elif k == 'injury':
            self.inj[side].add(b['frm'])
            self.refresh_lineups()
        elif k == 'sub':
            out, inn = b['frm'], b['to']
            if out in self.on[side]:
                self.on[side][self.on[side].index(out)] = inn
                old = self.pos.pop((side, out), None)
                self.pos[(side, inn)] = [old[0] if old else 0.5, -0.05 if side == 'h' else 1.05]
            self.sub_off[side].add(out)
            self.sub_on[side].add(inn)
            self.refresh_lineups()
        elif k == 'leave':
            self._remove(side, b['frm'])
            self.refresh_lineups()
        elif k == 'pen_kick':
            self.pens = b['pens']
            self.banner, self.banner_ticks = ("SÚT THÀNH CÔNG!" if b['ok'] else "KHÔNG VÀO!"), 45
            self.lbl_score.configure(text="%d - %d   (pen %d-%d)" % (self.score[0], self.score[1], b['pens'][0], b['pens'][1]))
        elif k == 'ht':
            self.banner, self.banner_ticks = "NGHỈ GIỮA HIỆP", 100
        elif k == 'end':
            self.finish()
        if k in ('goal', 'var_decision', 'yellow', 'red', 'pen_kick', 'end'):
            self.refresh_match_summary()
        self.log(b)
        self.update_stats()

    def _add_scorer(self, side, b):
        nm = self.lm.name(side, b['frm'])
        minute = f"{b.get('em', 0)}'"
        self.scorers[side].append("%s %d'%s" % (nm, b.get('em', 0), " (pen)" if b.get('pen') else ""))
        self.goal_log[side].append({"player": nm, "minute": minute, "pen": bool(b.get('pen'))})
        (self.lbl_sc_h if side == 'h' else self.lbl_sc_a).configure(text="\n".join(self.scorers[side]))

    def _remove(self, side, pid):
        if pid in self.on[side]:
            self.on[side].remove(pid)
        self.pos.pop((side, pid), None)

    # ------------------------------------------------------------------ bình luận / thống kê / đội hình
    def log(self, b):
        k, text = b['k'], b.get('text')
        if not text:
            return
        label = "11m" if k == 'pen_kick' else self.lm.label(b['T'])
        self.lbl_tick.configure(text="%s  %s" % (label, text))
        major = b.get('key')
        if k in ('kickoff', 'ht', 'ft', 'et', 'pens_start', 'end'):
            tag = 'mark'
        elif k in ('goal', 'yellow', 'red', 'injury', 'sub', 'pen_kick', 'tactic'):
            tag = k
        elif k in ('var_check', 'var_decision'):
            tag = 'var'
        else:
            tag = None
        tags = tuple(t for t in (tag, None if major else 'minor') if t)
        self.txt.configure(state='normal')
        self.txt.insert('end', "%4s  %s\n" % (label, text), tags)
        self.txt.configure(state='disabled')
        self.txt.see('end')

    def update_stats(self):
        """Thống kê trong trận: số cú sút / thẻ / phạt góc... đếm đúng theo từng pha; chuyền, tắc bóng, cắt bóng...
        chạy theo tỉ lệ diễn biến rồi khớp ĐÚNG số liệu cuối trận (cùng số liệu trong bảng thông số sau trận)."""
        self.stats_dirty = False
        det = self.prep['det']['t']
        if self.finished:
            self.sbars.set({k: list(v) for k, v in det.items()})
            return
        lm, ex = self.lm, self.ex
        prog = min(1.0, self.T / max(1.0, lm.reg_end))
        tot = self.ptime['h'] + self.ptime['a']
        narr = 100.0 * self.ptime['h'] / tot if tot > 1.0 else det['poss'][0]
        ph = narr * (1 - prog) + det['poss'][0] * prog
        v = {'poss': [ph, 100 - ph], 'xg': list(self.xg)}
        for k in ('shots', 'sot', 'off', 'blk', 'big', 'corners', 'offs', 'fouls', 'yc', 'rc', 'sv', 'varck', 'gdis'):
            v[k] = list(ex[k])
        for k in self.cum:
            tt = lm.tagtot[k]
            v[k] = [int(round(det[k][i] * self.cum[k][i] / tt['h' if i == 0 else 'a'])) if tt['h' if i == 0 else 'a'] else 0
                    for i in (0, 1)]
        v['pok'] = [min(v['pok'][i], v['pass'][i]) for i in (0, 1)]
        self.sbars.set(v)

    def refresh_lineups(self):
        t = self.lu
        t.configure(state='normal')
        t.delete('1.0', 'end')
        for side in ('h', 'a'):
            P = self.P[side]
            name = self.home if side == 'h' else self.away
            t.insert('end', "%s  (sơ đồ %s)\n" % (name, "-".join(map(str, P['formation']))), 'hdr')
            t.insert('end', " Trên sân / đã ra sân:\n")
            for e in P['xi']:
                t.insert('end', "  %-3s %-24s %s\n" % (e['pos'], e['name'][:24], self._mark(side, e['pid'])))
            t.insert('end', " Dự bị:\n")
            for e in P['bench']:
                t.insert('end', "  %-3s %-24s %s\n" % (e['pos'], e['name'][:24], self._mark(side, e['pid'])))
            t.insert('end', "\n")
        t.configure(state='disabled')

    def refresh_match_summary(self, force_finish=False):
        """Show scores and red cards for other same-competition fixtures at this kickoff."""
        tree = getattr(self, "summary_tree", None)
        if tree is None or not tree.winfo_exists():
            return
        try:
            rows = self.g.concurrent_scoreboard()
        except Exception:
            rows = []
        # The match currently being watched is also selectable here, so its goal
        # and red-card details appear in the same lower detail area as companions.
        active_score = f"{self.score[0]}–{self.score[1]}"
        if self.pens:
            active_score += f" (P {self.pens[0]}–{self.pens[1]})"
        active_status = "FT" if self.finished else f"{self.lm.label(self.T)} · LIVE"
        active_row = {
            "spec_key": "__active_live_match__",
            "comp": self.m.get("comp", "League"),
            "home": self.home, "away": self.away,
            "score": active_score, "status": active_status,
            "goal_home": [dict(item) for item in self.goal_log["h"]],
            "goal_away": [dict(item) for item in self.goal_log["a"]],
            "red_home": [dict(item) for item in self.card_log["h"] if item.get("kind") == "red"],
            "red_away": [dict(item) for item in self.card_log["a"] if item.get("kind") == "red"],
            "played": self.finished,
        }
        rows = [active_row] + list(rows or [])
        tracker_map = self.concurrent_trackers
        old_selection = tree.selection()
        selected_spec_key = None
        if old_selection:
            selected_row = getattr(self, "summary_rows", {}).get(old_selection[0], {})
            selected_spec_key = selected_row.get("spec_key")
        self.summary_rows = {}
        tree.delete(*tree.get_children())
        selected_iid = None
        first_iid = None
        live_count = 0
        finished_count = 0
        for row in rows:
            item = dict(row)
            tracker = tracker_map.get(item.get("spec_key"))
            if tracker is not None and not item.get("played"):
                item.update(tracker.update_to(self.T, force_finish=force_finish))
            if item.get("status") == "FT":
                finished_count += 1
            elif "LIVE" in str(item.get("status", "")):
                live_count += 1
            red_home = item.get("red_home") or []
            red_away = item.get("red_away") or []
            red_counts = f"{len(red_home)}–{len(red_away)}"
            red_present = bool(red_home or red_away)
            comp_short = {
                "Premier League": "PREM", "Football Championship 1": "FC1",
                "Football Championship 2": "FC2", "C1 Play-in": "C1 PI",
                "C1": "C1", "Cup": "CUP",
            }.get(item.get("comp", ""), str(item.get("comp", ""))[:5].upper())
            match_label = f"[{comp_short}] {item.get('home', '')} – {item.get('away', '')}"
            if len(match_label) > 46:
                match_label = match_label[:43] + "…"
            status = item.get("status", "Chưa đá")
            tags = ("red",) if red_present else (("played",) if status == "FT" else (("live",) if "LIVE" in status else ("pending",)))
            iid = tree.insert("", "end", values=(match_label, item.get("score", "—"),
                                                    red_counts if red_present else "—", status), tags=tags)
            self.summary_rows[iid] = item
            if first_iid is None:
                first_iid = iid
            if selected_spec_key and item.get("spec_key") == selected_spec_key:
                selected_iid = iid
        if rows:
            selected_iid = selected_iid or first_iid
            if selected_iid:
                tree.selection_set(selected_iid)
                self._show_concurrent_fixture_details()
            if len(rows) > 1:
                self.summary_clock.configure(
                    text=f"Cùng giải · {finished_count} FT · {live_count} đang đá · {len(rows)} trận trong slot"
                )
            else:
                self.summary_clock.configure(text="Trận đang xem · Không có trận khác cùng giờ trong giải.")
        else:
            self.summary_clock.configure(text="Không có trận trong slot này.")
            self.summary_detail.configure(text="Chọn trận để xem bàn thắng và thẻ đỏ (nếu có).")

    def _show_concurrent_fixture_details(self, _event=None):
        selected = self.summary_tree.selection() if hasattr(self, "summary_tree") else ()
        if not selected:
            return
        row = self.summary_rows.get(selected[0])
        if not row:
            return
        self.summary_detail.configure(text=concurrent_fixture_detail_text(row))
    def _live_standings_provisional_results(self):
        """Current fixture plus synchronized same-competition fixtures, at this match minute."""
        if self.entry.get("replay") or self.committed:
            return []
        provisional = []

        def add_result(preview, hg, ag):
            if not isinstance(preview, dict):
                return
            if not preview.get("comp") or not preview.get("stage"):
                return
            if not preview.get("home") or not preview.get("away"):
                return
            provisional.append({
                "comp": preview.get("comp"),
                "stage": preview.get("stage"),
                "home": preview.get("home"),
                "away": preview.get("away"),
                "hg": int(hg),
                "ag": int(ag),
                "neutral": bool(preview.get("neutral", False)),
            })

        add_result(self.m, self.score[0], self.score[1])
        for preview in getattr(self.g, "companion_previews", []) or []:
            tracker = self.concurrent_trackers.get(preview.get("spec_key"))
            if tracker is None:
                continue
            snapshot = tracker.update_to(self.T, force_finish=self.finished)
            add_result(preview, snapshot.get("live_hg", 0), snapshot.get("live_ag", 0))
        return provisional

    def refresh_live_standings(self):
        """Recompute and render League standings without saving provisional results."""
        tree = getattr(self, "live_standings_tree", None)
        if tree is None or not tree.winfo_exists():
            return
        getter = getattr(self.g, "live_league_table", None)
        if not callable(getter):
            self.live_standings_title.configure(text="BẢNG XẾP HẠNG LIVE")
            self.live_standings_note.configure(text="Không có dữ liệu bảng xếp hạng League cho trận này.")
            tree.delete(*tree.get_children())
            return
        try:
            snapshot = getter(self.m, self._live_standings_provisional_results()) or {}
        except Exception as exc:
            self.live_standings_note.configure(text=f"Không thể tính BXH live: {exc}")
            return

        tree.delete(*tree.get_children())
        if not snapshot.get("available"):
            self.live_standings_title.configure(text="BẢNG XẾP HẠNG LIVE")
            self.live_standings_note.configure(text=snapshot.get(
                "note", "Bảng xếp hạng trực tiếp chỉ áp dụng cho các trận League."
            ))
            return

        self.live_standings_title.configure(text=snapshot.get("title", "BẢNG XẾP HẠNG LIVE"))
        self.live_standings_note.configure(
            text=(snapshot.get("note", "") + "\n● CLB đang có trận trực tiếp")
        )
        sections = snapshot.get("sections") or [{
            "key": "league", "title": "BẢNG XẾP HẠNG", "rows": snapshot.get("rows", []),
            "live_teams": snapshot.get("live_teams", set()),
        }]
        for section in sections:
            title = str(section.get("title") or section.get("key", "")).upper()
            tree.insert("", "end", values=("", title, "", "", "", "", ""), tags=("section",))
            live_teams = set(section.get("live_teams") or [])
            for position, row in enumerate(section.get("rows") or [], 1):
                team = row.get("team", "—")
                team_label = f"{team} ●" if team in live_teams else team
                record = f"{row.get('w', 0)}-{row.get('d', 0)}-{row.get('l', 0)}"
                goals = f"{row.get('gf', 0)}-{row.get('ga', 0)}"
                gd = int(row.get("gd", 0) or 0)
                iid_tags = ("live",) if team in live_teams else ()
                tree.insert(
                    "", "end",
                    values=(position, team_label, row.get("p", 0), record, goals,
                            f"{gd:+d}", row.get("pts", 0)),
                    tags=iid_tags,
                )

    def _mark(self, side, pid):
        out = []
        if pid in self.sub_on[side]:
            out.append("[vào sân]")
        if pid in self.sub_off[side]:
            out.append("[rời sân]")
        if pid in self.yc[side]:
            out.append("[vàng]")
        if pid in self.rc[side]:
            out.append("[ĐỎ]")
        if pid in self.inj[side]:
            out.append("[chấn thương]")
        if pid not in self.on[side] and not out and pid in self.P[side]['xi_ids']:
            out.append("[rời sân]")
        return " ".join(out)

    # ------------------------------------------------------------------ kết thúc / thoát
    def finish(self):
        if self.finished:
            return
        self.finished = True
        r = self.prep['r']
        # đối chiếu với engine (phòng lỗi): tỷ số hiển thị phải đúng bằng kết quả sẽ lưu
        self.score = [r['hg'], r['ag']]
        txt = "%d - %d" % (r['hg'], r['ag'])
        if r.get('pens'):
            txt += "   (pen %d-%d)" % tuple(r['pens'])
        elif r.get('et'):
            txt += "   (sau hiệp phụ)"
        self.lbl_score.configure(text=txt)
        if self.entry.get('replay'):
            # Historical replay is read-only: it reuses the recorded result and must
            # never append a duplicate fixture or update season statistics.
            self.committed = True
            self.app.say("Đã phát lại trận đã lưu: %s - %s %s" % (self.home, self.away, txt.strip()))
        else:
            try:
                msg = self.g.commit_live(self.m, self.prep)
                self.committed = True
                self.entry['dirty'] = True
                self.refresh_match_summary(force_finish=True)
                self.app.say("%s - %s: %s. %s" % (self.home, self.away, txt.strip(), msg))
            except Exception as ex:      # không để lỗi ghi kết quả làm treo giao diện
                self.app.say("Lỗi khi lưu kết quả trận: %s" % ex)
        self.btn_pause.state(['disabled'])
        self.btn_skip.state(['disabled'])
        self.banner, self.banner_ticks = "HẾT GIỜ", 90
        self.update_stats()
        self.show_report()

    def show_report(self):
        """Show full-time stats; simultaneous fixtures are selectable as separate match tabs."""
        if not self.committed or self.report_widget is not None:
            return
        try:
            reports_getter = getattr(self.g, "reports_after_live", None)
            matches = reports_getter(self.m) if callable(reports_getter) else [self.m]
            matches = list(matches or [self.m])
            if len(matches) <= 1:
                rp = self.g.report_of(matches[0])
                self.report_widget = wnl_report_ui.build_report(
                    self.parent, self.g.report_header(m=matches[0]), rp)
            else:
                self.report_widget = ttk.Notebook(self.parent)
                for index, match in enumerate(matches, 1):
                    tab = ttk.Frame(self.report_widget)
                    home, away = match.get("home", "Chủ nhà"), match.get("away", "Đội khách")
                    label = f"{home} – {away}"
                    if len(label) > 32:
                        label = label[:29] + "…"
                    self.report_widget.add(tab, text=f"{index}. {label}")
                    rp = self.g.report_of(match)
                    report = wnl_report_ui.build_report(tab, self.g.report_header(m=match), rp)
                    report.pack(fill="both", expand=True)
        except Exception as ex:
            self.app.say("Không dựng được bảng thông số trận: %s" % ex)
            return
        self.btn_view.pack(side='left', padx=10)
        self.btn_view.configure(text="Xem lại diễn biến trên sân")
        self.mid.pack_forget()
        self.report_widget.pack(fill='both', expand=True, padx=8, before=self.ctl)
        self.btn_pause.pack_forget()
        self.btn_skip.pack_forget()

    def toggle_view(self):
        if self.report_widget is None:
            return
        if self.mid.winfo_ismapped():
            self.mid.pack_forget()
            self.report_widget.pack(fill='both', expand=True, padx=8, before=self.ctl)
            self.btn_view.configure(text="Xem lại diễn biến trên sân")
        else:
            self.report_widget.pack_forget()
            self.mid.pack(fill='both', expand=True, padx=8, pady=4, before=self.ctl)
            self.btn_view.configure(text="Xem thông số trận đấu")

    def cleanup(self):
        self.dead = True
        if self.after_id is not None:
            try:
                self.app.root.after_cancel(self.after_id)
            except Exception:
                pass
        self.app.root.unbind('<space>')

    def on_back(self):
        if self.finished:
            self.cleanup()
            return True
        was = self.paused
        self.paused = True
        ans = messagebox.askyesnocancel(
            "Trận đấu chưa kết thúc",
            "Trận đấu vẫn đang diễn ra.\n\n"
            "Yes: chuyển nhanh đến hết và lưu kết quả\n"
            "No: thoát, huỷ trận (trận vẫn chưa được đá)\n"
            "Cancel: ở lại xem tiếp")
        if ans is None:
            self.paused = was
            return False
        if ans:
            self.skip_to_end()
        self.cleanup()
        return True
