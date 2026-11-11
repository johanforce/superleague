#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WNL REPORT UI - "Thông số trận đấu" (giống trang thống kê trận trên các app bóng đá).
====================================================================================
build_report(parent, header, rp) dựng Notebook gồm các tab:
  Tổng quan : tỷ số, tiến trình bàn thắng / thẻ / chấn thương / thay người theo phút, cầu thủ xuất sắc nhất, điểm FIFA
  Thống kê  : thanh so sánh 2 đội (kiểm soát bóng, xG, sút, chuyền, tắc bóng, phạm lỗi, ...)
  Cầu thủ   : bảng chỉ số + điểm đánh giá từng cầu thủ hai đội (chỉ có với trận mùa hiện tại và bán kết/chung kết League A)
  Diễn biến : bình luận các pha chính (chỉ có với trận mùa hiện tại và bán kết/chung kết League A)
header = (tiêu đề, ngày, sân, chủ nhà, khách, tỷ số, ghi chú); rp = dict do Game.report_of() / report_of_compact() tạo ra.
StatBars cũng được trang live dùng lại để cập nhật thống kê trong lúc trận diễn ra.
"""
import tkinter as tk
from tkinter import ttk

from . import stats as wnl_stats
from . import coach as wnl_coach

# Material-inspired dark surface tokens: keep all text readable on the simulator's night theme.
BG, SURFACE, SURFACE_2 = '#0c1319', '#131c25', '#0f1820'
TEXT, MUTED, LINE = '#e9edf1', '#9aa8b7', '#34485b'
HOME_C, AWAY_C = '#8fc8ff', '#ff9da4'
EVENT_STYLES = {
    'goal': ('#153a2a', '#b7f4c9'), 'red': ('#3d2025', '#ffb5b9'),
    'yel': ('#3a3218', '#ffe2a8'), 'inj': ('#302443', '#e1c7ff'),
    'sub': ('#1b2e43', '#c5ddff'), 'var': ('#292744', '#d2ccff'),
    'tac': ('#24372d', '#c6efd2'),
}
EVT_LABEL = {'y': "Thẻ vàng", 'r': "THẺ ĐỎ", 'yr': "THẺ ĐỎ (2 vàng)", 'inj': "Chấn thương", 'sub': "Thay người"}


def fmt_stat(key, kind, v, stats=None, i=0):
    if key == 'pok' and stats:
        att = stats['pass'][i]
        return "%d (%d%%)" % (v, round(100.0 * v / att)) if att else "0"
    if kind == 'p':
        return "%d%%" % round(v)
    if kind == 'x':
        return "%.2f" % v
    return str(int(round(v)))


class StatBars(ttk.Frame):
    """Danh sách chỉ số dạng thanh so sánh 2 đội (vẽ bằng Canvas, tự co giãn theo chiều rộng)."""
    ROW_H = 38

    def __init__(self, parent, home, away, rows_def=None):
        super().__init__(parent)
        self.home, self.away = home, away
        self.rows_def = rows_def or wnl_stats.STAT_ROWS
        self.vals = None
        self.cv = tk.Canvas(self, highlightthickness=0, bg=SURFACE)
        sb = ttk.Scrollbar(self, orient='vertical', command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.cv.pack(side='left', fill='both', expand=True)
        self.cv.bind('<Configure>', lambda e: self.redraw())
        self.cv.bind('<MouseWheel>', lambda e: self.cv.yview_scroll(-1 if e.delta > 0 else 1, 'units'))

    def set(self, stats):
        """stats: {khoá: [chủ, khách]} (các khoá của wnl_stats.STAT_ROWS)."""
        self.vals = stats
        self.redraw()

    def redraw(self):
        c = self.cv
        c.delete('all')
        W = max(320, c.winfo_width())
        c.create_text(W * 0.12, 16, text=self.home, fill=HOME_C, font=('TkDefaultFont', 11, 'bold'), anchor='w')
        c.create_text(W * 0.88, 16, text=self.away, fill=AWAY_C, font=('TkDefaultFont', 11, 'bold'), anchor='e')
        if not self.vals:
            c.create_text(W / 2, 60, text="Chưa có số liệu thống kê", fill=MUTED)
            return
        y = 40
        half = W * 0.5
        for key, name, kind in self.rows_def:
            v = self.vals.get(key)
            if v is None:
                continue
            h, a = v
            c.create_text(W * 0.06, y + 8, text=fmt_stat(key, kind, h, self.vals, 0), fill=TEXT, font=('TkDefaultFont', 11, 'bold'), anchor='w')
            c.create_text(W * 0.94, y + 8, text=fmt_stat(key, kind, a, self.vals, 1), fill=TEXT, font=('TkDefaultFont', 11, 'bold'), anchor='e')
            c.create_text(half, y + 8, text=name, fill=MUTED)
            tot = float(h + a)
            share = (h / tot) if tot > 0 else 0.5
            if tot <= 0:
                share = 0.5
            L, R, by = W * 0.12, W * 0.88, y + 20
            mid = L + (R - L) * 0.5
            wl, wr = (mid - L - 2) * (share if tot > 0 else 0.0) * 2, (R - mid - 2) * ((1 - share) if tot > 0 else 0.0) * 2
            c.create_rectangle(L, by, mid - 2, by + 6, fill=LINE, outline='')
            c.create_rectangle(mid + 2, by, R, by + 6, fill=LINE, outline='')
            if h > 0:
                c.create_rectangle(mid - 2 - min(wl, mid - L - 2), by, mid - 2, by + 6, fill=HOME_C, outline='')
            if a > 0:
                c.create_rectangle(mid + 2, by, mid + 2 + min(wr, R - mid - 2), by + 6, fill=AWAY_C, outline='')
            y += self.ROW_H
        c.configure(scrollregion=(0, 0, W, y + 10))


def build_report(parent, header, rp, start_tab=0):
    title, date, venue, home, away, score, note = header
    root = ttk.Frame(parent)
    top = ttk.Frame(root)
    top.pack(fill='x', padx=10, pady=(4, 2))
    top.columnconfigure(0, weight=1, uniform='r')
    top.columnconfigure(2, weight=1, uniform='r')
    ttk.Label(top, text=home, font=('TkDefaultFont', 16, 'bold'), foreground=HOME_C, anchor='e').grid(row=0, column=0, sticky='e', padx=10)
    ttk.Label(top, text=score, font=('TkDefaultFont', 26, 'bold')).grid(row=0, column=1)
    ttk.Label(top, text=away, font=('TkDefaultFont', 16, 'bold'), foreground=AWAY_C, anchor='w').grid(row=0, column=2, sticky='w', padx=10)
    ttk.Label(top, text=("(%s)" % note) if note else " ", foreground=MUTED).grid(row=1, column=1)
    ttk.Label(root, text="%s  |  %s  |  %s" % (title, date, venue), foreground=MUTED).pack(pady=(0, 0 if (rp and rp.get('ctx')) else 4))
    if rp and rp.get('ctx'):
        ttk.Label(root, text=rp['ctx'], foreground=MUTED, wraplength=900, justify='center').pack(pady=(0, 4))
    if not rp:
        ttk.Label(root, text="Trận này chưa có dữ liệu chi tiết.").pack(pady=20)
        return root
    nb = ttk.Notebook(root)
    nb.pack(fill='both', expand=True, padx=8, pady=(0, 6))
    # ---------------- tổng quan
    f1 = ttk.Frame(nb)
    nb.add(f1, text="Tổng quan")
    cols = [('m', "Phút", 55, 'center'), ('h', home, 330, 'e'), ('a', away, 330, 'w')]
    tf = ttk.Frame(f1)
    tf.pack(fill='both', expand=True, padx=4, pady=4)
    t = ttk.Treeview(tf, columns=[c[0] for c in cols], show='headings', height=16)
    for k, hd, w, an in cols:
        t.heading(k, text=hd)
        t.column(k, width=w, anchor=an)
    vs = ttk.Scrollbar(tf, orient='vertical', command=t.yview)
    t.configure(yscrollcommand=vs.set)
    vs.pack(side='right', fill='y')
    t.pack(side='left', fill='both', expand=True)
    for tag, (tag_bg, tag_fg) in EVENT_STYLES.items():
        t.tag_configure(tag, background=tag_bg, foreground=tag_fg)
    evs = []
    for mn, side, scorer, ast, pen, xg in rp.get('goals', []):
        txt = "[Bàn thắng] %s%s" % (scorer, " (pen)" if pen else "")
        if ast:
            txt += " - kiến tạo: " + ast
        if xg is not None:
            txt += "  (xG %.2f)" % xg
        evs.append((mn, 0, side, txt, 'goal'))
    for ev in rp.get('events', []):
        mn, side, kind, name, extra = ev[:5]
        why = ev[5] if len(ev) > 5 else ''
        if kind == 'sub':
            txt = "[Thay người] %s ra, %s vào" % (name, extra) if extra else "[Rời sân] %s (không có người thay)" % name
            if extra and why in wnl_coach.REASON_TXT:
                txt += " (%s)" % wnl_coach.REASON_TXT[why].replace("vì ", "").replace("để ", "")
            tag = 'sub'
        elif kind == 'inj':
            txt, tag = "[Chấn thương] %s - %s" % (name, extra), 'inj'
        else:
            txt, tag = "[%s] %s" % (EVT_LABEL.get(kind, kind), name), ('yel' if kind == 'y' else 'red')
        evs.append((mn, 1, side, txt, tag))
    for mn, side, kind, txt in rp.get('var', []):
        evs.append((mn, 2, side, "[VAR] " + txt, 'var'))
    for mn, side, form, why in rp.get('tactics', []):
        evs.append((mn, 3, side, "[Chiến thuật] Đổi sang sơ đồ %s (%s)" % (form, wnl_coach.REASON_TXT.get(why, why)), 'tac'))
    for mn, _, side, txt, tag in sorted(evs, key=lambda e: (e[0], e[1])):
        t.insert('', 'end', values=(("%d'" % mn), txt if side == 'h' else "", txt if side == 'a' else ""), tags=(tag,))
    if not evs:
        t.insert('', 'end', values=("", "(không có sự kiện)", ""))
    info = []
    if rp.get('form'):
        info.append("Sơ đồ: %s %s  -  %s %s" % (home, rp['form'][0], away, rp['form'][1]))
    if rp.get('coach'):
        info.append("HLV: %s %s  |  %s %s" % (home, rp['coach'][0], away, rp['coach'][1]))
    if rp.get('motm'):
        nm, side, rt = rp['motm']
        info.append("Cầu thủ xuất sắc nhất: %s (%s) - %.1f điểm" % (nm, home if side == 'h' else away, rt))
    if rp.get('fifa'):
        p1, d1, p2, d2 = rp['fifa']
        info.append("Điểm FIFA: %s %.2f (%+.2f)  |  %s %.2f (%+.2f)" % (home, p1, d1, away, p2, d2))
    for line in info:
        ttk.Label(f1, text=line).pack(anchor='w', padx=10)
    # ---------------- thống kê
    f2 = ttk.Frame(nb)
    nb.add(f2, text="Thống kê")
    if rp.get('summary_only'):
        ttk.Label(f2, text="Báo cáo C1 / Major Cup chỉ lưu người ghi bàn, thẻ đỏ, AET và kết quả luân lưu.",
                  foreground=MUTED, anchor='center', justify='center', wraplength=760).pack(
                      fill='both', expand=True, padx=24, pady=24)
    else:
        sbars = StatBars(f2, home, away)
        sbars.pack(fill='both', expand=True)
        sbars.set(rp.get('stats'))
    # ---------------- cầu thủ
    if rp.get('players'):
        f3 = ttk.Frame(nb)
        nb.add(f3, text="Cầu thủ")
        has_player_data = any(rp.get('players', {}).get(side) for side in ('h', 'a'))
        if not has_player_data:
            message = ("Báo cáo C1 / Major Cup chỉ lưu cầu thủ ghi bàn và thẻ đỏ."
                       if rp.get('summary_only') else
                       "Save trận này chưa lưu thống kê riêng cho từng cầu thủ.")
            ttk.Label(f3, text=message, foreground=MUTED, anchor='center',
                      wraplength=760, justify='center').pack(fill='both', expand=True, padx=20, pady=30)
        pc = [('n', "Cầu thủ", 180, 'w'), ('p', "Vt", 36, 'center'), ('mn', "Phút", 40, 'center'), ('rt', "Điểm", 44, 'center'),
              ('g', "Bàn", 34, 'center'), ('a', "KT", 30, 'center'), ('sh', "Sút", 34, 'center'), ('sot', "Đích", 36, 'center'),
              ('xg', "xG", 40, 'center'), ('ps', "Chuyền", 66, 'center'), ('kp', "KP", 30, 'center'), ('tk', "Tắc", 34, 'center'),
              ('it', "Cắt", 34, 'center'), ('cl', "Phá", 34, 'center'), ('sv', "Cứu", 34, 'center'), ('fl', "Lỗi", 32, 'center'),
              ('yc', "V", 24, 'center'), ('rc', "Đ", 24, 'center')]
        for side, nm_t, col in ((('h', home, HOME_C), ('a', away, AWAY_C)) if has_player_data else ()):
            ttk.Label(f3, text=nm_t, foreground=col, font=('TkDefaultFont', 10, 'bold')).pack(anchor='w', padx=6)
            fr = ttk.Frame(f3)
            fr.pack(fill='both', expand=True, padx=4, pady=(0, 4))
            tr = ttk.Treeview(fr, columns=[c[0] for c in pc], show='headings', height=11)
            for k, hd, w, an in pc:
                tr.heading(k, text=hd)
                tr.column(k, width=w, anchor=an, stretch=(k == 'n'))
            sb = ttk.Scrollbar(fr, orient='vertical', command=tr.yview)
            hs = ttk.Scrollbar(fr, orient='horizontal', command=tr.xview)
            tr.configure(yscrollcommand=sb.set, xscrollcommand=hs.set)
            sb.pack(side='right', fill='y')
            hs.pack(side='bottom', fill='x')
            tr.pack(side='left', fill='both', expand=True)
            tr.tag_configure('top', background='#153a2a', foreground='#b7f4c9')
            tr.tag_configure('bench', background=SURFACE_2, foreground=MUTED)
            rows = rp['players'][side]
            best = max((r['rt'] for r in rows), default=0)
            for r in rows:
                nm = r['name']
                if r.get('son'):
                    nm += "  (vào %d')" % r['son']
                if r.get('soff') is not None:
                    nm += "  (ra %d')" % r['soff']
                tag = ('top',) if r['rt'] == best else (() if r.get('st') else ('bench',))
                tr.insert('', 'end', tags=tag, values=(
                    nm, r['pos'], r['min'], "%.1f" % r['rt'], r['g'] or '', r['a'] or '', r['sh'] or '', r['sot'] or '',
                    ("%.2f" % r['xg']) if r['xg'] else '', "%d/%d" % (r['pok'], r['pass']), r['kp'] or '', r['tkl'] or '',
                    r['int'] or '', r['clr'] or '', r['sv'] or '', r['fl'] or '', r['yc'] or '', r['rc'] or ''))
    # ---------------- diễn biến
    if rp.get('tl') or rp.get('summary_only'):
        f4 = ttk.Frame(nb)
        nb.add(f4, text="Diễn biến chính")
        tx = tk.Text(f4, wrap='word', font=('TkDefaultFont', 10), bg=SURFACE_2, fg=TEXT,
                     insertbackground=TEXT, selectbackground='#23415a', selectforeground='#ffffff',
                     relief='flat', highlightthickness=1, highlightbackground=LINE)
        sb = ttk.Scrollbar(f4, orient='vertical', command=tx.yview)
        tx.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        tx.pack(side='left', fill='both', expand=True)
        for tag, event_key in (('goal', 'goal'), ('yellow', 'yel'), ('red', 'red'),
                               ('injury', 'inj'), ('sub', 'sub'), ('var', 'var'), ('tactic', 'tac')):
            bg, fg = EVENT_STYLES[event_key]
            tx.tag_configure(tag, background=bg, foreground=fg,
                             font=('TkDefaultFont', 10, 'bold') if tag in ('goal', 'red') else ('TkDefaultFont', 10))
        tx.tag_configure('mark', font=('TkDefaultFont', 10, 'bold'), foreground=TEXT)
        for lab, kind, side, text in rp.get('tl', []):
            tag = kind if kind in ('goal', 'yellow', 'red', 'injury', 'sub', 'tactic') else (
                'var' if kind in ('var_check', 'var_decision') else
                'mark' if kind in ('kickoff', 'ht', 'ft', 'et', 'pens_start', 'end') else None)
            tx.insert('end', "%5s  %s\n" % (lab, text), (tag,) if tag else ())
        if rp.get('summary_only') and not rp.get('tl'):
            tx.insert('end', "Không có bàn thắng hoặc thẻ đỏ trong trận đấu.\n", ('mark',))
        tx.configure(state='disabled')
    try:
        nb.select(start_tab)
    except Exception:
        pass
    return root


def build_rank_chart(parent, pts, letters, size=16):
    """Biểu đồ đường: hạng FIFA qua từng mùa (hạng 1 ở trên cùng), nền chia theo League (mỗi League = `size` hạng)."""
    cv = tk.Canvas(parent, bg=SURFACE_2, highlightthickness=1, highlightbackground=LINE, height=300)

    def draw(_e=None):
        cv.delete('all')
        W, H = max(420, cv.winfo_width()), max(220, cv.winfo_height())
        if not pts:
            cv.create_text(W / 2, H / 2, text="Chưa có dữ liệu", fill=MUTED)
            return
        ml, mr, mt, mb = 56, 70, 18, 46
        ranks = [p['rank'] for p in pts]
        lo, hi = max(1, min(ranks) - 6), min(192, max(ranks) + 6)
        if hi - lo < 14:
            hi = min(192, lo + 14)
        Y = lambda rk: mt + (rk - lo) / float(hi - lo) * (H - mt - mb)
        n = len(pts)
        X = lambda i: ml + (i / float(max(1, n - 1)) if n > 1 else 0.5) * (W - ml - mr)
        li0, li1 = (lo - 1) // size, (hi - 1) // size
        for li in range(li0, li1 + 1):
            a, b = max(lo, li * size + 0.5), min(hi, (li + 1) * size + 0.5)
            cv.create_rectangle(ml, Y(a), W - mr, Y(b), fill=SURFACE if li % 2 == 0 else SURFACE_2, outline='')
            cv.create_text(W - mr + 8, (Y(a) + Y(b)) / 2, text="League %s" % letters[li], anchor='w', fill=MUTED,
                           font=('TkDefaultFont', 9, 'bold'))
            cv.create_text(W - mr + 8, (Y(a) + Y(b)) / 2 + 12, text="(%d-%d)" % (li * size + 1, (li + 1) * size), anchor='w',
                           fill=MUTED, font=('TkDefaultFont', 8))
        step = 1 if hi - lo <= 12 else (5 if hi - lo <= 60 else 10)
        start = lo if lo % step == 0 else lo + (step - lo % step)
        for rk in range(start, hi + 1, step):
            cv.create_line(ml - 4, Y(rk), W - mr, Y(rk), fill=LINE, dash=(2, 3))
            cv.create_text(ml - 8, Y(rk), text="#%d" % rk, anchor='e', fill=MUTED, font=('TkDefaultFont', 8))
        coords = [(X(i), Y(p['rank'])) for i, p in enumerate(pts)]
        if len(coords) > 1:
            cv.create_line(*[v for xy in coords for v in xy], fill=HOME_C, width=2)
        for i, ((x, y), p) in enumerate(zip(coords, pts)):
            last = (i == n - 1)
            cv.create_oval(x - 5, y - 5, x + 5, y + 5, fill='#d9453d' if last else '#2f6fdb', outline='white', width=2)
            cv.create_text(x, y - 13, text="#%d" % p['rank'], fill=TEXT, font=('TkDefaultFont', 9, 'bold'))
            cv.create_text(x, H - mb + 14, text=p['label'], fill=TEXT, font=('TkDefaultFont', 8))
            if p.get('league') is not None:
                cv.create_text(x, H - mb + 28, text="League %s" % letters[p['league']], fill=MUTED, font=('TkDefaultFont', 8))
    cv.bind('<Configure>', draw)
    cv.draw = draw
    return cv
