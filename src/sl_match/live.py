#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WNL LIVE - dựng "diễn biến trận đấu" từ kết quả của engine để phát lại như xem trực tiếp.
===========================================================================================
File này KHÔNG có giao diện (tkinter) - chỉ tạo ra danh sách "beat" (từng pha bóng) theo thời gian:

    LiveMatch(home, away, prep, neutral, knockout).beats  ->  [ {T, k, side, text, ball, frm, to, ...}, ... ]

Kết quả (tỷ số, người ghi bàn, kiến tạo, cú sút + xG, thẻ, chấn thương, thay người, hiệp phụ, luân lưu) và các số
liệu thống kê chi tiết (phạt góc, phạm lỗi, việt vị... xem wnl_stats.py) lấy NGUYÊN từ kết quả đã mô phỏng nên trận xem
live luôn khớp 100% với kết quả được ghi vào giải đấu.

Phần "diễn" được dựng theo VỊ TRÍ THẬT của cầu thủ (wnl_shape.py): người nhận bóng được chọn theo khoảng cách + khoảng
trống + hướng tấn công; có đập nhả (một-hai), chọc khe, bóng dài đổi cánh, bó biên tạt bóng, phạt góc, đá phạt, việt vị,
ném biên, tắc bóng, cắt bóng, rê bóng. Đội hình trong trận lệch nhẹ so với sơ đồ xếp trước trận (xem wnl_shape.py).

Hệ toạ độ sân: x từ 0 (khung thành nhà) đến 1 (khung thành khách), y từ 0 đến 1. Đội nhà tấn công về x = 1.
"T" là đồng hồ trận đấu tính bằng phút (đã gồm bù giờ).

Các loại beat (k): kickoff, pass, long, cross, dribble, tackle (mất bóng), throw, foul, offside, clearance, shot, goal,
  save, off, block, corner, goalkick, restart, yellow, red, injury, sub, leave, tactic, info, var_check, var_decision, ht, ft, et, pens_start, pen_kick, end
Mỗi beat có thể kèm: 'tg' = [(đội, chỉ số)] để thống kê chạy theo diễn biến, 'combo' = True nếu là đập nhả.
"""
import math
import random
from . import engine as eng
from . import shape as shp
from . import coach as wnl_coach

GROUP_VN = {'GK': 'thủ môn', 'DEF': 'hậu vệ', 'MID': 'tiền vệ', 'FWD': 'tiền đạo'}
VAR_WHY = {'offside': "việt vị", 'foul': "lỗi trước tình huống ghi bàn", 'handball': "chạm tay"}

STEP = 0.21                   # phút trận giữa 2 đường chuyền trong chuỗi dẫn tới cú sút
SCALED = ('pass', 'pok', 'kp', 'crs', 'drb', 'tkl', 'int', 'clr', 'throw')   # thống kê chạy theo tỉ lệ diễn biến

PASS_TXT = ["{a} chuyền cho {b}", "{a} đưa bóng cho {b}", "{a} nhả bóng cho {b}", "{a} phát bóng sang {b}",
            "{a} một chạm cho {b}", "{a} bật nhả cho {b}", "{a} chuyền ngắn cho {b}"]
BACK_TXT = ["{a} chuyền về cho {b}, đội nhà tổ chức lại", "{a} lùi bóng cho {b}", "{a} trả bóng về {b}"]
LONG_TXT = ["{a} bóng dài vượt tuyến cho {b}", "{a} tung đường chuyền dài cho {b}", "{a} nâng bóng bổng cho {b}"]
SWITCH_TXT = ["{a} chuyển bóng sang cánh cho {b}", "{a} đổi cánh bằng đường chuyền chéo sân cho {b}"]
THROUGH_TXT = ["{a} chọc khe xuyên hàng thủ cho {b}", "{a} đường chuyền xé toang hàng thủ cho {b}",
               "{a} luồn bóng tinh tế cho {b} băng xuống"]
CROSS_TXT = ["{a} tạt bóng vào vòng cấm cho {b}", "{a} căng ngang trước khung thành, {b} xuất hiện", "{a} bó biên tạt sớm cho {b}"]
CUTBACK_TXT = ["{a} xuống đáy kéo ngược bóng cho {b}", "{a} căng ngang sát đất về {b}"]
OVERLAP_TXT = ["{a} đẩy bóng cho {b} bó biên chạy chỗ", "{a} nhả cho {b} đang dâng cao dọc đường biên"]
ONE_TWO_1 = ["{a} đập nhả với {b}", "{a} chơi một-hai cùng {b}", "{a} tam giác chuyền với {b}"]
ONE_TWO_2 = ["{b} bật nhả lại, {a} thoát xuống nhận bóng", "{b} trả lại một chạm, {a} băng lên phía trước"]
DRIB_TXT = ["{a} rê bóng qua người, đưa bóng lên", "{a} đi bóng tốc độ, bỏ lại hậu vệ", "{a} cắt vào trong rồi đẩy bóng lên",
            "{a} giữ bóng, tìm hướng đi"]
TACK_TXT = ["{b} cắt đường chuyền của {a}", "{b} vào bóng quyết đoán, đoạt bóng từ {a}", "{b} tắc bóng thành công trước {a}",
            "Đường chuyền của {a} đi hỏng, {b} nhặt bóng", "{b} bọc lót và lấy bóng từ chân {a}"]
FOUL_TXT = ["{b} phạm lỗi với {a}", "{b} kéo áo {a}, trọng tài thổi phạt", "{b} vào bóng muộn với {a}", "{b} chắn người {a} trái luật"]
FK_TXT = ["{a} thực hiện quả đá phạt, chuyền cho {b}", "{a} đá phạt nhanh cho {b}"]
OFFSIDE_TXT = ["{p} đã việt vị, cờ trọng tài phất lên.", "Cờ phất lên! {p} rơi vào thế việt vị.", "{p} bị bắt việt vị khi đón đường chọc khe."]
SHOT_OFF = ["{s} dứt điểm, bóng bay chệch cột dọc", "{s} sút vọt xà ngang", "{s} sút xa nhưng không trúng đích",
            "{s} dứt điểm hụt, bóng đi ra ngoài"]
HEAD_OFF = ["{s} đánh đầu cận thành nhưng bóng đi chệch khung gỗ", "{s} đánh đầu bật lên cao hơn xà ngang"]
SHOT_SAVE = ["{g} bay người cản phá cú sút của {s}", "{g} cản phá xuất sắc cú dứt điểm của {s}",
             "{s} đá trúng đích nhưng {g} bắt gọn"]
HEAD_SAVE = ["{s} đánh đầu đúng khung thành nhưng {g} cứu thua", "{g} phản xạ đẩy cú đánh đầu của {s}"]
SHOT_BLOCK = ["Hàng thủ chặn được cú sút của {s}", "{s} sút bị chắn lại ngay trước vòng cấm"]
GOAL_TXT = ["VÀO!!! {s} ({t}) ghi bàn!", "BÀN THẮNG! {s} đưa {t} vươn lên!", "{s} dứt điểm cực chuẩn, bóng vào lưới!"]
HEAD_GOAL = ["VÀO!!! {s} ({t}) đánh đầu ghi bàn!", "{s} bật cao đánh đầu, bóng găm vào lưới!"]


def shootout_script(sa, sb):
    """Tạo kịch bản từng quả luân lưu (danh sách True/False cho mỗi đội) cho khớp tỷ số cuối sa-sb."""
    hi, lo = max(sa, sb), min(sa, sb)
    if hi <= 5 or hi - lo != 1:
        n = max(5, hi)
        outs = []
        for goals in (sa, sb):
            idx = set(random.sample(range(n), min(goals, n)))
            outs.append([i in idx for i in range(n)])
        return outs
    e = random.choice([3, 4, 5])
    k = hi - e
    n = 5 + k
    outs = [[False] * n, [False] * n]
    for t in (0, 1):
        for i in random.sample(range(5), e):
            outs[t][i] = True
        for i in range(5, n - 1):
            outs[t][i] = True
    win = 0 if sa > sb else 1
    outs[win][n - 1] = True
    return outs


class LiveMatch:
    def __init__(self, home, away, prep, neutral=False, knockout=False):
        self.home, self.away = home, away
        self.P = {'h': prep['A'], 'a': prep['B']}
        self.r = prep['r']
        self.det = prep['det']
        self.neutral, self.knockout = neutral, knockout
        self.sx = random.choice([1, 2, 2, 3])          # bù giờ hiệp 1
        self.sy = random.choice([2, 3, 3, 4, 5])       # bù giờ hiệp 2
        self.ht_T = 45 + self.sx
        self.reg_end = 90 + self.sx + self.sy
        self.et = bool(self.r['et'])
        self.end_T = self.reg_end + (30 if self.et else 0)
        self.beats = []
        self.chains = []
        self.shape = shp.MatchShape(self.P['h'], self.P['a'])
        for side in ('h', 'a'):                         # người vào sân nhận vị trí người ra
            P = self.P[side]
            for mn, out, inn in P['fin']['subs']:
                if inn:
                    self.shape.team[side].inherit(inn, out, P['players'][inn])
        for side in ('h', 'a'):                         # đổi cơ cấu đội hình giữa trận (HLV): gán lại vị trí theo sơ đồ mới
            P = self.P[side]
            for mn, out, inn in sorted(P['fin']['subs'], key=lambda x: x[0]):
                if inn and P['players'][inn]['group'] != P['players'][out]['group']:
                    self.shape.team[side].reform(mn, [P['players'][i] for i in eng.on_pitch(P, mn)])
        self.shape.set_ments(self.r.get('ments', []))
        self.ctx_txt = prep.get('ctx_txt', '')
        self._oncache = {}
        self._pt = {'h': 0.0001, 'a': 0.0001}           # thời gian kiểm soát bóng đã diễn (để bám số liệu kiểm soát bóng)
        self.pending = None
        self._last_combo = {'h': -9.0, 'a': -9.0}
        self._build()

    # ------------------------------------------------------------------ đồng hồ
    def T_of(self, m):
        if m <= 45:
            return float(m)
        if m <= 90:
            return m + self.sx
        return m + self.sx + self.sy

    def m_of_T(self, T):
        if T <= self.ht_T:
            return max(1, int(math.ceil(min(T, 45))))
        if T <= self.reg_end:
            return max(46, min(90, int(math.ceil(T - self.sx))))
        return max(91, int(math.ceil(T - self.sx - self.sy)))

    def label(self, T):
        c = lambda x: int(math.ceil(x - 1e-9))
        if T <= 0.05:
            return "0'"
        if T <= 45:
            return "%d'" % max(1, c(T))
        if T <= self.ht_T:
            return "45+%d'" % max(1, c(T - 45))
        if T <= 90 + self.sx:
            return "%d'" % c(T - self.sx)
        if T <= self.reg_end:
            return "90+%d'" % max(1, c(T - self.sx - 90))
        return "%d'" % c(T - self.sx - self.sy)

    def home_share(self, m):
        for t0, t1, s in self.r.get('seg_share', []):
            if t0 < m <= t1:
                return s
        return 0.5

    def diff_at(self, m):
        h = sum(1 for g in self.r['goals'] if g['t'] == 'h' and g['m'] <= m)
        a = sum(1 for g in self.r['goals'] if g['t'] == 'a' and g['m'] <= m)
        return (h, a)

    # ------------------------------------------------------------------ tiện ích
    def team_name(self, side):
        return self.home if side == 'h' else self.away

    def pl(self, side, pid):
        return self.P[side]['players'][pid]

    def name(self, side, pid):
        return self.pl(side, pid)['name']

    def on(self, side, m):
        k = (side, m)
        if k not in self._oncache:
            self._oncache[k] = eng.on_pitch(self.P[side], m)
        return self._oncache[k]

    def gk_id(self, side, m):
        for i in self.on(side, m):
            if self.pl(side, i)['group'] == 'GK':
                return i
        return None

    def gk_name(self, side, m):
        i = self.gk_id(side, m)
        return self.name(side, i) if i else "Thủ môn"

    def add(self, T, k, side=None, text='', ball=None, frm=None, to=None, **kw):
        b = {'T': round(T, 3), 'k': k, 'side': side, 'text': text, 'ball': ball, 'frm': frm, 'to': to}
        b.update(kw)
        self.beats.append(b)
        return b

    def positions(self, T, ball, poss, holder, m=None):
        m = m or self.m_of_T(T)
        on = {'h': self.on('h', m), 'a': self.on('a', m)}
        return self.shape.positions(on, T, ball, poss, holder, self.diff_at(m), m)

    @staticmethod
    def other(side):
        return 'a' if side == 'h' else 'h'

    @staticmethod
    def depth(side, xy):
        return xy[0] if side == 'h' else 1.0 - xy[0]

    @staticmethod
    def lat(side, xy):
        return xy[1] if side == 'h' else 1.0 - xy[1]

    @staticmethod
    def xy(side, p, y):
        return (p if side == 'h' else 1.0 - p, y)

    def _lead(self, side, xy, metres=2.5):
        d = metres / shp.PITCH_W
        return (min(0.99, max(0.01, xy[0] + (d if side == 'h' else -d))), xy[1])

    # ------------------------------------------------------------------ chọn người nhận bóng theo vị trí
    def _near_opp(self, pos, opp, xy, m, exclude_gk=True):
        best, bd = None, 1e9
        for o in self.on(opp, m):
            if exclude_gk and self.pl(opp, o)['group'] == 'GK':
                continue
            d = shp.meters(pos[(opp, o)], xy)
            if d < bd:
                best, bd = o, d
        return best, bd

    def _targets(self, poss, holder, pos, m, exclude=(), force=None):
        """Danh sách (pid, trọng số, khoảng cách m, tiến độ, độ thoáng) các đồng đội có thể nhận bóng."""
        opp = self.other(poss)
        sgn = 1 if poss == 'h' else -1
        hp = pos[(poss, holder)]
        hd = self.depth(poss, hp)
        out = []
        opps = [pos[(opp, o)] for o in self.on(opp, m) if self.pl(opp, o)['group'] != 'GK']
        for pid in self.on(poss, m):
            if pid == holder or pid in exclude:
                continue
            e = self.pl(poss, pid)
            if e['group'] == 'GK' and hd > 0.3 and pid != force:
                continue
            rp = pos[(poss, pid)]
            dist = shp.meters(hp, rp)
            gain = (rp[0] - hp[0]) * sgn
            opn = min(shp.meters(rp, o) for o in opps) if opps else 15.0
            mid = ((hp[0] + rp[0]) / 2, (hp[1] + rp[1]) / 2)
            lane = min(shp.meters(mid, o) for o in opps) if opps else 15.0
            w = math.exp(-dist / 11.0) * (1.0 + 3.2 * max(0.0, gain)) * (0.55 + min(opn, 9.0) / 9.0) * \
                (0.6 + min(lane, 6.0) / 6.0 * 0.5) * (max(30.0, e['poss_q']) / 70.0)
            if dist < 5.0:
                w *= 0.25
            if dist > 34.0:
                w *= 0.35
            if gain < -0.02:
                w *= 0.45
            if e['group'] == 'GK':
                w *= 0.2
            out.append((pid, w, dist, gain, opn))
        return out

    def _pick_target(self, poss, holder, pos, m, exclude=(), force=None):
        cands = self._targets(poss, holder, pos, m, exclude, force)
        if force is not None:
            for c in cands:
                if c[0] == force:
                    return c
            rp, hp = pos[(poss, force)], pos[(poss, holder)]
            return (force, 1.0, shp.meters(hp, rp), (rp[0] - hp[0]) * (1 if poss == 'h' else -1), 8.0)
        if not cands:
            return None
        return random.choices(cands, [c[1] for c in cands])[0]

    def _pass_ok(self, poss, holder, dist, gain, press, opn, rdepth, m):
        q = self.pl(poss, holder)['poss_q']
        p = 0.90 - 0.0042 * max(0.0, dist - 14) - 0.5 * max(0.0, gain) + (q - 70) / 300.0 + (min(opn, 12) - 6) * 0.006
        if press < 4.0:
            p -= 0.08
        share = self._pt[poss] / (self._pt['h'] + self._pt['a'])
        hs = self.home_share(m)
        target = hs if poss == 'h' else 1.0 - hs
        p -= max(-0.2, min(0.2, (share - target) * 1.6))
        if rdepth > 0.80:
            p -= 0.10
        return max(0.40, min(0.97, p))

    # ------------------------------------------------------------------ các pha chuyền / rê / mất bóng
    def _spend(self, side, dt):
        self._pt[side] += dt

    def _pass_beat(self, T, poss, holder, rec, dist, gain, pos, kind=None, **kw):
        a, b = self.name(poss, holder), self.name(poss, rec)
        rp = pos[(poss, rec)]
        rd, rl = self.depth(poss, rp), self.lat(poss, rp)
        hp = pos[(poss, holder)]
        hd = self.depth(poss, hp)
        if kind is None:
            if gain < -0.04:
                kind, txt = 'pass', random.choice(BACK_TXT)
            elif dist > 38 and abs(rl - self.lat(poss, hp)) > 0.35:
                kind, txt = 'long', random.choice(SWITCH_TXT)
            elif dist > 30 and gain > 0.12:
                kind, txt = 'long', random.choice(LONG_TXT)
            elif gain > 0.13 and dist > 18 and rd > 0.6 and self.pl(poss, rec)['group'] == 'FWD':
                kind, txt = 'long', random.choice(THROUGH_TXT)
            else:
                kind, txt = 'pass', random.choice(PASS_TXT)
        else:
            txt = random.choice({'cross': CROSS_TXT, 'cutback': CUTBACK_TXT, 'overlap': OVERLAP_TXT,
                                 'through': THROUGH_TXT}[kind])
            kind = {'cross': 'cross', 'cutback': 'pass', 'overlap': 'pass', 'through': 'long'}[kind]
        tg = [(poss, 'pass'), (poss, 'pok')]
        if kind == 'cross':
            tg.append((poss, 'crs'))
        b_ = self.add(T, kind, poss, txt.format(a=a, b=b), self._lead(poss, rp), frm=holder, to=rec, tg=tg, dist=round(dist, 1), **kw)
        return b_

    def _turnover(self, T, poss, holder, pos, m, target_xy, press_who=None, via_pass=True, rec=None):
        """Đội đang giữ bóng để mất bóng: bị cắt/tắc hoặc bóng ra biên."""
        opp = self.other(poss)
        r0 = random.random()
        if r0 < 0.16 and via_pass:                       # bóng ra ngoài biên -> ném biên cho đối phương
            tx = (min(0.97, max(0.03, target_xy[0])), random.choice([0.01, 0.99]))
            d, _ = self._near_opp(pos, opp, tx, m)
            self.add(T, 'throw', opp, "Bóng ra ngoài biên, ném biên cho %s, %s thực hiện." % (self.team_name(opp), self.name(opp, d)),
                     tx, frm=holder, to=d, tg=[(poss, 'pass'), (opp, 'throw')])
            self.pending = d
            return d, tx
        d, _ = self._near_opp(pos, opp, target_xy, m)
        tkl = random.random() < 0.55 and not via_pass
        txt = random.choice(TACK_TXT).format(a=self.name(poss, holder), b=self.name(opp, d))
        tg = [(poss, 'pass')] if via_pass else []
        tg.append((opp, 'tkl' if (tkl or not via_pass) else 'int'))
        pxy = pos[(opp, d)]
        self.add(T, 'tackle', opp, txt, pxy, frm=holder, to=d, tg=tg)
        self.pending = d
        return d, pxy

    def _action(self, T, st, m, in_chain=False):
        """1 hành động của đội đang cầm bóng. st = {'poss','holder','ball'}. Trả về thời gian đã dùng (phút)."""
        poss, holder = st['poss'], st['holder']
        opp = self.other(poss)
        pos = self.positions(T, st['ball'], poss, holder, m)
        if (poss, holder) not in pos:
            holder = st['holder'] = self.on(poss, m)[0]
            pos = self.positions(T, st['ball'], poss, holder, m)
        hp = pos[(poss, holder)]
        e = self.pl(poss, holder)
        hd, hl = self.depth(poss, hp), self.lat(poss, hp)
        _, press = self._near_opp(pos, opp, hp, m)
        wide = abs(hl - 0.5) > 0.30
        A = self.name(poss, holder)
        r0 = random.random()
        dt = 0.17
        cands = self._targets(poss, holder, pos, m)
        if not cands:
            return 0.2
        near = min(cands, key=lambda c: c[2])
        # --- đập nhả (một-hai): đồng đội gần + bị ép + khu vực giữa sân tới 3/4 sân
        if (near[2] < 15 and press < 9 and 0.28 < hd < 0.88 and e['group'] != 'GK' and r0 < 0.09 and near[2] > 4
                and T - self._last_combo[poss] > 1.5):
            self._last_combo[poss] = T
            B = near[0]
            nb = self.name(poss, B)
            ok = random.random() < min(0.88, 0.70 + (e['poss_q'] - 70) / 250.0 + (self.pl(poss, B)['poss_q'] - 70) / 250.0)
            self.add(T, 'pass', poss, random.choice(ONE_TWO_1).format(a=A, b=nb), self._lead(poss, pos[(poss, B)]),
                     frm=holder, to=B, combo=True, tg=[(poss, 'pass'), (poss, 'pok')])
            if ok:
                adv = random.uniform(7, 13) / shp.PITCH_W * (1 if poss == 'h' else -1)
                nxy = (min(0.97, max(0.03, hp[0] + adv)), min(0.96, max(0.04, hp[1] + random.uniform(-0.04, 0.04))))
                self.add(T + 0.15, 'pass', poss, random.choice(ONE_TWO_2).format(a=A, b=nb), nxy, frm=B, to=holder,
                         combo=True, tg=[(poss, 'pass'), (poss, 'pok')])
                st['ball'] = nxy
                self._spend(poss, 0.32)
                return 0.40
            self._turnover(T + 0.15, poss, B, pos, m, pos[(poss, B)], via_pass=True)
            d = self.pending
            st['poss'], st['holder'], st['ball'] = opp, d, pos[(opp, d)]
            self._spend(poss, 0.30)
            return 0.35
        # --- bó biên: hậu vệ biên / cánh đang dâng cao, tạt bóng khi vào khu vực cuối sân
        if wide and hd > 0.80 and e['group'] != 'GK' and not in_chain:
            box = [c for c in cands if abs(self.lat(poss, pos[(poss, c[0])]) - 0.5) < 0.2 and
                   self.depth(poss, pos[(poss, c[0])]) > 0.78]
            if box and random.random() < 0.55:
                c = random.choice(box)
                ok = random.random() < 0.40
                if ok:
                    self._pass_beat(T, poss, holder, c[0], c[2], c[3], pos, kind='cross')
                    st['holder'], st['ball'] = c[0], self._lead(poss, pos[(poss, c[0])])
                    self._spend(poss, 0.25)
                    return 0.30
                d, _ = self._near_opp(pos, opp, pos[(poss, c[0])], m)
                self.add(T, 'clearance', opp, "%s tạt bóng nhưng %s bật cao phá ra." % (A, self.name(opp, d)),
                         self._lead(opp, pos[(opp, d)], 6), frm=d, to=None,
                         tg=[(poss, 'pass'), (poss, 'crs'), (opp, 'clr')])
                st['poss'] = opp
                st['holder'] = self._pick_clear_receiver(opp, pos, m)
                st['ball'] = pos[(opp, st['holder'])]
                self._spend(poss, 0.25)
                return 0.30
        # --- rê bóng
        if e['group'] != 'GK' and r0 > 0.88 and hd < 0.85:
            gainm = random.uniform(6, 13) / shp.PITCH_W
            nxy = (hp[0] + (gainm if poss == 'h' else -gainm), min(0.95, max(0.05, hp[1] + random.uniform(-0.1, 0.1))))
            nxy = (min(0.97, max(0.03, nxy[0])), nxy[1])
            ok = random.random() < min(0.82, max(0.35, 0.58 + (e.get('dri', e['poss_q']) - 70) / 160.0 - (0.14 if press < 3 else 0)))
            if ok:
                self.add(T, 'dribble', poss, random.choice(DRIB_TXT).format(a=A), nxy, frm=holder, to=holder, tg=[(poss, 'drb')])
                st['ball'] = nxy
                self._spend(poss, 0.2)
                return 0.22
            d, _ = self._near_opp(pos, opp, hp, m)
            self.add(T, 'tackle', opp, "%s tắc bóng thành công, chặn đứng pha đi bóng của %s" % (self.name(opp, d), A),
                     pos[(opp, d)], frm=holder, to=d, tg=[(opp, 'tkl')])
            st['poss'], st['holder'], st['ball'] = opp, d, pos[(opp, d)]
            self._spend(poss, 0.2)
            return 0.22
        # --- chuyền thường
        c = random.choices(cands, [x[1] for x in cands])[0]
        pid, _, dist, gain, opn = c
        ok = random.random() < self._pass_ok(poss, holder, dist, gain, press, opn,
                                              self.depth(poss, pos[(poss, pid)]), m)
        dt = 0.13 + min(dist, 50) / 60.0 * 0.22 + random.uniform(0.0, 0.05)
        if ok:
            self._pass_beat(T, poss, holder, pid, dist, gain, pos)
            st['holder'], st['ball'] = pid, self._lead(poss, pos[(poss, pid)])
            self._spend(poss, dt)
            return dt
        d, pxy = self._turnover(T, poss, holder, pos, m, pos[(poss, pid)])
        st['poss'], st['holder'], st['ball'] = opp, d, pxy
        self._spend(poss, dt)
        return dt

    def _pick_clear_receiver(self, side, pos, m):
        best, bd = None, 1e9
        for pid in self.on(side, m):
            if self.pl(side, pid)['group'] == 'GK':
                continue
            dd = abs(self.depth(side, pos[(side, pid)]) - 0.38) + random.uniform(0, 0.12)
            if dd < bd:
                best, bd = pid, dd
        return best or self.on(side, m)[0]

    # ------------------------------------------------------------------ dựng toàn bộ diễn biến
    def _build(self):
        r = self.r
        self.add(0.0, 'kickoff', 'h', "Trọng tài thổi còi khai cuộc! %s gặp %s." % (self.home, self.away), (0.5, 0.5), key=True)
        self._ctx_beats()
        self._plan_pools()
        shots = sorted(r.get('shot_log', []), key=lambda s: s['m'])
        last_end = 0.0
        for s in shots:
            m = s['m']
            n = 0 if s.get('pen') else random.choice([2, 3, 3, 4, 5])
            Tshot = self.T_of(m) - random.uniform(0.1, 0.5)
            period_start = 0.0 if m <= 45 else (self.ht_T if m <= 90 else self.reg_end)
            period_end = self.ht_T if m <= 45 else (self.reg_end if m <= 90 else self.end_T)
            length = (n + 1) * STEP + 0.3
            start = Tshot - length
            if start < max(last_end, period_start + 0.05):
                Tshot += max(last_end, period_start + 0.05) - start
                start = max(last_end, period_start + 0.05)
            Tshot = min(Tshot, period_end - 1.2)
            info = self._shot(s, n, Tshot, m, start)
            self.chains.append(info)
            last_end = info['e']
        self._side_events()
        self._var_events()
        self._fillers()
        self.add(self.ht_T, 'ht', None, "Hết hiệp 1: %s - %s" % (self.home, self.away), (0.5, 0.5), key=True)
        self.add(self.ht_T + 0.02, 'kickoff', 'a', "Hiệp 2 bắt đầu!", (0.5, 0.5), key=True)
        self.add(self.reg_end, 'ft', None, "Hết 90 phút thi đấu chính thức.", (0.5, 0.5), key=True)
        if self.et:
            self.add(self.reg_end + 0.02, 'et', None, "Hai đội bước vào hiệp phụ!", (0.5, 0.5), key=True)
        if r.get('pens'):
            self._shootout(self.end_T + 0.2)
        else:
            self.add(self.end_T + 0.1, 'end', None, "Trọng tài bấm còi mãn cuộc.", (0.5, 0.5), key=True)
        self.beats.sort(key=lambda b: (b['T'], b.get('o', 0)))
        self.final_T = self.beats[-1]['T']
        self._tally()

    def _tally(self):
        """Đếm tổng số beat có gắn thẻ thống kê (để thống kê trong trận chạy theo tỉ lệ và kết thúc đúng số cuối)."""
        self.tagtot = {k: {'h': 0, 'a': 0} for k in SCALED}
        cnt = {'fouls': [0, 0], 'offs': [0, 0], 'corners': [0, 0]}
        for b in self.beats:
            for side, key in b.get('tg', ()):
                if key in self.tagtot:
                    self.tagtot[key][side] += 1
            for k, key in (('foul', 'fouls'), ('offside', 'offs'), ('corner', 'corners')):
                if b['k'] == k and b['side']:
                    cnt[key][0 if b['side'] == 'h' else 1] += 1
        for key, v in cnt.items():           # số liệu hiển thị = đúng số pha đã diễn
            self.det['t'][key] = v

    # ------------------------------------------------------------------ kho sự kiện theo số liệu thống kê
    def _plan_pools(self):
        """Phạt góc / phạm lỗi / việt vị lấy đúng số liệu trong det (để diễn biến khớp bảng thống kê sau trận)."""
        d = self.det
        self.corner_left = {'h': d['t']['corners'][0], 'a': d['t']['corners'][1]}
        self.foul_pool = {}
        self.off_pool = {}
        for side in ('h', 'a'):
            fp, op = [], []
            for pid, x in d['p'][side].items():
                fp += [pid] * x['fl']
                op += [pid] * x['off']
            random.shuffle(fp)
            random.shuffle(op)
            self.foul_pool[side], self.off_pool[side] = fp, op

    def _pop_foul(self, side, pid=None):
        pool = self.foul_pool[side]
        if pid is not None:
            if pid in pool:
                pool.remove(pid)
            return pid
        return pool.pop() if pool else None

    # ------------------------------------------------------------------ cú sút + chuỗi dẫn tới nó
    def _corner_seq(self, T, side, key=True):
        """Phạt góc: bóng ra biên ngang -> người đá tạt vào -> hàng thủ phá ra."""
        opp = self.other(side)
        gx = 1.0 if side == 'h' else 0.0
        m = self.m_of_T(T)
        cy = random.choice([0.02, 0.98])
        taker = self.pick_group(side, m, ('MID', 'FWD', 'DEF'), 'ast_q')
        self.add(T, 'corner', side, "Phạt góc cho %s, %s ra đá." % (self.team_name(side), self.name(side, taker)),
                 (gx, cy), frm=taker, key=key, o=1)
        dfn = self.pick_group(opp, m, ('DEF',), 'dfn')
        self.add(T + 0.3, 'cross', side, "%s tạt bóng vào vòng cấm..." % self.name(side, taker),
                 (gx * 0.88 + (0.06 if side == 'a' else 0), 0.5 + random.uniform(-0.12, 0.12)), frm=taker, to=None,
                 tg=[(side, 'crs')], o=1)
        self.add(T + 0.55, 'clearance', opp, "%s bật cao đánh đầu phá bóng ra ngoài." % self.name(opp, dfn),
                 self.xy(opp, 0.30, random.uniform(0.2, 0.8)), frm=dfn, to=None, tg=[(opp, 'clr')], o=1)

    def pick_group(self, side, m, groups, key):
        ids = [i for i in self.on(side, m) if self.pl(side, i)['group'] in groups] or list(self.on(side, m))
        ws = [(max(20.0, self.pl(side, i)[key]) / 70.0) ** 2 + 1e-4 for i in ids]
        return random.choices(ids, ws)[0]

    def _shot(self, s, n, Tshot, m, start):
        side = s['t']
        opp = self.other(side)
        tn = self.team_name(side)
        sh = s['s']
        o = s['o']
        xg = s.get('xg')
        gx = 1.0 if side == 'h' else 0.0
        seq_end = Tshot - 0.1
        header = False
        corner_origin = False
        pending = s.get('var') in ('checked', 'overturned')
        if s.get('pen'):
            v = self._pop_foul(opp)
            if s.get('var') == 'awarded':
                self.add(Tshot - 0.8, 'foul', opp, "%s ngã trong vòng cấm sau va chạm, nhưng trọng tài cho chơi tiếp." %
                         self.name(side, sh), self.xy(side, 0.86, 0.5), frm=v, to=sh, key=True, chain=True)
                self._var(Tshot - 0.7, side, "VAR đề nghị xem lại tình huống trong vòng cấm của %s..." % self.team_name(opp),
                          "VAR can thiệp! Trọng tài xem màn hình và chỉ tay vào chấm phạt đền cho %s!" % tn, False,
                          chain=True, vk='penalty_awarded')
            else:
                self.add(Tshot - 0.45, 'foul', opp, "Trọng tài chỉ tay vào chấm phạt đền! %s sắp đá phạt đền cho %s." %
                         (self.name(side, sh), tn), self.xy(side, 0.86, 0.5), frm=v, to=sh, key=True)
            spot = self.xy(side, 0.89, 0.5)
        else:
            corner_origin = self.corner_left[side] > 0 and random.random() < 0.13 and s['o'] != 'block'
            if corner_origin:
                self.corner_left[side] -= 1
                self._corner_seq_shot(Tshot, side, sh, s)
                header = True
                spot = self.xy(side, 0.91, 0.5 + random.uniform(-0.1, 0.1))
            else:
                spot, header = self._chain(s, n, Tshot, m, start)
        yy = 0.5 + random.uniform(-0.12, 0.12)
        sname = self.name(side, sh)
        gk = self.gk_name(opp, m)
        base = dict(side=side, frm=sh, to=None, em=m, key=True, shooter=sh, xg=xg, spot=spot)
        end = Tshot + 0.6
        if o in ('goal', 'goal_disallowed'):
            text = random.choice(HEAD_GOAL if header else GOAL_TXT).format(s=sname, t=tn)
            if s.get('pen'):
                text = "VÀO!!! %s đá phạt đền thành công cho %s!" % (sname, tn)
            elif s.get('a'):
                text += " Kiến tạo: %s." % self.name(side, s['a'])
            if xg is not None:
                text += " (xG %.2f)" % xg
            gkw = {'var': 'pending'} if pending else {}
            self.add(Tshot, 'goal', ball=(gx, yy), text=text, assist=s.get('a'), pen=bool(s.get('pen')), **base, **gkw)
            if pending:
                over = s['var'] == 'overturned'
                end = Tshot + 0.95
                self._var(Tshot + 0.06, side, "VAR đang kiểm tra bàn thắng của %s..." % sname,
                          ("Bàn thắng của %s bị huỷ vì %s!" % (sname, VAR_WHY.get(s.get('why'), "vi phạm")))
                          if over else "Bàn thắng của %s hợp lệ, trọng tài công nhận!" % sname,
                          over, chain=True, vk='goal_overturned' if over else 'goal_checked', goal=True, shooter=sh)
                if over:
                    gid = self.gk_id(opp, m)
                    self.add(end - 0.25, 'goalkick', opp, "%s phát bóng lên sau khi bàn thắng bị huỷ." % gk,
                             self.xy(opp, 0.04, 0.5), frm=gid, to=gid, o=1)
                    o = 'off'
            if not pending or s['var'] == 'checked':
                mid = self.pick_group(opp, m, ('MID', 'FWD'), 'poss_q')
                self.add((end - 0.25) if pending else Tshot + 0.35, 'restart', opp,
                         "Hai đội trở lại giữa sân, %s giao bóng." % self.team_name(opp), (0.5, 0.5), to=mid, o=1)
        elif o == 'save':
            text = random.choice(HEAD_SAVE if header else SHOT_SAVE).format(s=sname, g=gk)
            if s.get('pen'):
                text = "%s cản phá được quả phạt đền của %s!" % (gk, sname)
            elif xg is not None and xg >= 0.15:
                text += " (xG %.2f)" % xg
            self.add(Tshot, 'save', ball=(gx * 0.97 + (0.015 if side == 'a' else 0), yy), text=text, **base)
            if self.corner_left[side] > 0 and not s.get('pen') and random.random() < 0.30:
                self.corner_left[side] -= 1
                self._corner_seq(Tshot + 0.3, side)
                end = Tshot + 1.25
        elif o == 'block':
            self.add(Tshot, 'block', ball=self.xy(side, 0.84, yy), text=random.choice(SHOT_BLOCK).format(s=sname), **base)
            if self.corner_left[side] > 0 and random.random() < 0.3:
                self.corner_left[side] -= 1
                self._corner_seq(Tshot + 0.3, side)
                end = Tshot + 1.25
        else:
            text = random.choice(HEAD_OFF if header else SHOT_OFF).format(s=sname)
            self.add(Tshot, 'off', ball=(gx, random.choice([0.2, 0.82])), text=text, **base)
            gid = self.gk_id(opp, m)
            self.add(Tshot + 0.25, 'goalkick', opp, "%s phát bóng lên." % gk, self.xy(opp, 0.04, 0.5), frm=gid, to=gid, o=1)
        return {'s': start - 0.05, 'e': end, 'side': side, 'o': o, 'pen': bool(s.get('pen')), 'corner': end > Tshot + 1.0}

    def _corner_seq_shot(self, Tshot, side, shooter, s):
        """Phạt góc dẫn tới cú đánh đầu."""
        opp = self.other(side)
        gx = 1.0 if side == 'h' else 0.0
        m = self.m_of_T(Tshot)
        cy = random.choice([0.02, 0.98])
        taker = s['a'] if s.get('a') else self.pick_group(side, m, ('MID', 'FWD', 'DEF'), 'ast_q')
        if taker == shooter:
            taker = self.pick_group(side, m, ('MID',), 'ast_q')
        self.add(Tshot - 0.7, 'corner', side, "Phạt góc cho %s, %s ra đá." % (self.team_name(side), self.name(side, taker)),
                 (gx, cy), frm=taker, key=True, o=1)
        self.add(Tshot - 0.35, 'cross', side, "%s tạt bóng cho %s phía cột xa..." % (self.name(side, taker), self.name(side, shooter)),
                 self.xy(side, 0.92, 0.5), frm=taker, to=shooter, tg=[(side, 'pass'), (side, 'pok'), (side, 'crs'), (side, 'kp')],
                 o=1)

    def _chain(self, s, n, Tshot, m, start):
        """Chuỗi chuyền dẫn tới cú sút theo vị trí thật. Trả về (vị trí sút, có phải đánh đầu không)."""
        side, sh = s['t'], s['s']
        ast = s.get('a')
        opp = self.other(side)
        sgn = 1 if side == 'h' else -1
        T = start + 0.05
        pool = [i for i in self.on(side, m) if i != sh]
        # người bắt đầu: hậu vệ / tiền vệ (hoặc thủ môn) ở phần sân nhà
        starters = [i for i in pool if self.pl(side, i)['group'] in ('DEF', 'MID') and i != ast] or pool
        holder = random.choice(starters) if random.random() > 0.08 else (self.gk_id(side, m) or random.choice(starters))
        if holder == sh:
            holder = random.choice(pool)
        p0 = random.uniform(0.2, 0.5)
        ball = self.xy(side, p0, random.uniform(0.25, 0.75))
        steps = max(1, n)
        beats_before = len(self.beats)
        st = {'poss': side, 'holder': holder, 'ball': ball}
        hops = max(1, n - 1)
        used = []
        for i in range(hops):
            pos = self.positions(T, st['ball'], side, st['holder'], m)
            if (side, st['holder']) not in pos:
                st['holder'] = pool[0]
                pos = self.positions(T, st['ball'], side, st['holder'], m)
            last_hop = (i == hops - 1)
            force = ast if (last_hop and ast and ast != st['holder']) else None
            # đập nhả xen vào chuỗi (không dùng cho bước chuyền cuối)
            c = self._pick_target(side, st['holder'], pos, m, exclude=set(used[-1:]) | {sh} | ({ast} if (ast and not last_hop) else set()),
                                  force=force)
            if c is None:
                break
            pid, _, dist, gain, opn = c
            if not last_hop and 4 < c[2] < 15 and random.random() < 0.14 and T - self._last_combo[side] > 1.5:
                self._last_combo[side] = T
                A, B = self.name(side, st['holder']), self.name(side, pid)
                hp = pos[(side, st['holder'])]
                self.add(T, 'pass', side, random.choice(ONE_TWO_1).format(a=A, b=B), self._lead(side, pos[(side, pid)]),
                         frm=st['holder'], to=pid, chain=True, combo=True, tg=[(side, 'pass'), (side, 'pok')])
                adv = random.uniform(7, 12) / shp.PITCH_W * sgn
                nxy = (min(0.9, max(0.1, hp[0] + adv)), min(0.9, max(0.1, hp[1])))
                T += STEP * 0.7
                self.add(T, 'pass', side, random.choice(ONE_TWO_2).format(a=A, b=B), nxy, frm=pid, to=st['holder'],
                         chain=True, combo=True, tg=[(side, 'pass'), (side, 'pok')])
                st['ball'] = nxy
                T += STEP
                continue
            self._pass_beat(T, side, st['holder'], pid, dist, gain, pos, chain=True)
            used.append(st['holder'])
            st['holder'], st['ball'] = pid, self._lead(side, pos[(side, pid)])
            T += STEP
        # đường chuyền cuối cho người sút
        pos = self.positions(T, st['ball'], side, st['holder'], m)
        passer = st['holder']
        if passer == sh:
            passer = next((i for i in pool if i != sh), passer)
            st['holder'] = passer
            pos = self.positions(T, st['ball'], side, passer, m)
        pp, sp = pos[(side, passer)], pos[(side, sh)]
        pl_, sl = self.lat(side, pp), self.lat(side, sp)
        pd_ = self.depth(side, pp)
        dist = shp.meters(pp, sp)
        gain = (sp[0] - pp[0]) * sgn
        header = False
        kind = None
        sg = self.pl(side, sh)['group']
        if abs(pl_ - 0.5) > 0.28 and pd_ > 0.68 and abs(sl - 0.5) < 0.22 and dist > 12:
            kind = 'cross'
            header = random.random() < (0.72 if sg in ('FWD', 'DEF') else 0.4)
        elif abs(pl_ - 0.5) > 0.30 and pd_ > 0.86:
            kind = 'cutback'
        elif gain > 0.1 and dist > 14:
            kind = 'through'
        b = self._pass_beat(T, side, passer, sh, dist, gain, pos, kind=kind, chain=True)
        b['tg'] = b['tg'] + [(side, 'kp')]
        # nén thời gian chuỗi cho khớp cú sút
        new = self.beats[beats_before:]
        if new:
            t0, t1 = new[0]['T'], new[-1]['T']
            tgt1 = Tshot - 0.12
            if t1 > tgt1 and t1 > t0:
                f = (tgt1 - start) / (t1 - t0) if tgt1 > start else 0.5
                for x in new:
                    x['T'] = round(start + (x['T'] - t0) * min(1.0, f), 3)
        self._spend(side, max(0.2, Tshot - start))
        yy = min(0.7, max(0.3, sl + random.uniform(-0.05, 0.05)))
        dep = random.uniform(0.82, 0.92) if (s['o'] != 'off' or (s.get('xg') or 0) > 0.06) else random.uniform(0.68, 0.8)
        return self.xy(side, dep, yy), header

    # ------------------------------------------------------------------ thẻ phạt / chấn thương / thay người
    def _side_events(self):
        evT = {}
        for side in ('h', 'a'):
            fin = self.P[side]['fin']
            nm = lambda pid: self.name(side, pid)
            tn = self.team_name(side)
            for c in fin['cards']:
                T = self.T_of(c['m']) - random.uniform(0.05, 0.4)
                pid = c['p']
                self._pop_foul(side, pid)
                if c['c'] == 'y':
                    self.add(T - 0.15, 'foul', side, "%s phạm lỗi thô bạo." % nm(pid), None, frm=pid, key=True, o=2)
                    self.add(T, 'yellow', side, "Thẻ vàng cho %s (%s)." % (nm(pid), tn), None, frm=pid, em=c['m'], key=True, o=3)
                elif c['c'] == 'yr':
                    self._pop_foul(side, pid)
                    self.add(T + 0.05, 'red', side, "Thẻ vàng thứ hai! %s (%s) nhận thẻ đỏ và bị đuổi khỏi sân." % (nm(pid), tn),
                             None, frm=pid, em=c['m'], key=True, o=4)
                    evT[(side, pid)] = T + 0.05
                elif c.get('var') == 'upgraded':
                    self.add(T - 0.3, 'foul', side, "%s vào bóng thô bạo, trọng tài chỉ rút thẻ vàng." % nm(pid), None, frm=pid, key=True, o=2)
                    self._var(T - 0.2, side, "VAR đề nghị trọng tài xem lại pha vào bóng của %s..." % nm(pid),
                              "VAR can thiệp: thẻ vàng được nâng thành THẺ ĐỎ!", False, o=2.5, vk='red_upgraded')
                    self.add(T, 'red', side, "THẺ ĐỎ trực tiếp! %s (%s) bị đuổi khỏi sân sau khi xem VAR." % (nm(pid), tn), None, frm=pid,
                             em=c['m'], key=True, o=4)
                    evT[(side, pid)] = T
                else:
                    self.add(T - 0.15, 'foul', side, "%s vào bóng bằng hai chân!" % nm(pid), None, frm=pid, key=True, o=2)
                    self.add(T, 'red', side, "THẺ ĐỎ trực tiếp! %s (%s) bị đuổi khỏi sân." % (nm(pid), tn), None, frm=pid,
                             em=c['m'], key=True, o=4)
                    evT[(side, pid)] = T
            for j in fin['injs']:
                T = self.T_of(j['m']) - random.uniform(0.05, 0.4)
                self.add(T, 'injury', side, "%s (%s) ngã xuống sân, ôm %s. Đội ngũ y tế vào sân." % (nm(j['p']), tn, j['name'].lower()),
                         None, frm=j['p'], em=j['m'], key=True, o=3, inj=j['name'], games=j['n'])
                evT[(side, j['p'])] = T
        for side in ('h', 'a'):
            fin = self.P[side]['fin']
            nm = lambda pid: self.name(side, pid)
            tn = self.team_name(side)
            for mn, out, inn in fin['subs']:
                T = evT.get((side, out))
                T = (T + 0.35) if T is not None else (self.T_of(mn) - 0.95)
                if inn:
                    why = self.r.get('sub_why', {}).get(side, {}).get(out)
                    rs = (" - " + wnl_coach.REASON_TXT[why]) if why in wnl_coach.REASON_TXT else ""
                    self.add(T, 'sub', side, "Thay người (%s): %s ra, %s vào sân%s." % (tn, nm(out), nm(inn), rs), None,
                             frm=out, to=inn, em=mn, key=True, o=5, why=why)
                else:
                    red = (side, out) in evT and any(c['p'] == out and c['c'] in ('r', 'yr') for c in fin['cards'])
                    self.add(T + 0.02, 'leave', side,
                             '' if red else "%s rời sân vì chấn thương, %s chơi thiếu người." % (nm(out), tn),
                             None, frm=out, em=mn, key=not red, o=5)

        self._tactic_beats()

    def _tactic_beats(self):
        """Bình luận quyết định của HLV: đổi sơ đồ (kèm thay người) và đổi thế trận."""
        tact = {}
        for mn, sd, form, why in self.r.get('tactics', []):
            tact[(mn, sd)] = (form, why)
        for (mn, sd), (form, why) in tact.items():
            P = self.P[sd]
            coach = self.r['coach'][0 if sd == 'h' else 1]
            parts = []
            for m2, out, inn in P['fin']['subs']:
                if m2 == mn and inn:
                    parts.append("đưa %s vào thay %s (%s)" % (self.name(sd, inn), self.name(sd, out),
                                                              GROUP_VN[P['players'][out]['group']]))
            self.add(self.T_of(mn) - 0.9, 'tactic', sd, wnl_coach.describe_tactic(coach, mn, form, why, parts), None,
                     em=mn, key=True, o=5.5, form=form, why=why)
        for mn, sd, ment, why in self.r.get('ments', []):
            if (mn, sd) in tact:
                continue
            coach = self.r['coach'][0 if sd == 'h' else 1]
            tn = self.team_name(sd)
            if why == 'red_card':
                txt = "HLV %s chuyển %s sang thế trận phòng ngự chắc chắn sau thẻ đỏ." % (coach, tn)
            elif ment == 'attack' and why == 'chasing':
                txt = "HLV %s dồn đội hình lên cao, %s tăng tốc tìm bàn gỡ." % (coach, tn)
            elif ment == 'defend' and why == 'protect':
                txt = "HLV %s ra hiệu lùi sâu, %s tập trung bảo vệ tỷ số." % (coach, tn)
            elif ment == 'attack':
                txt = "HLV %s đẩy cao đội hình, %s gia tăng sức ép." % (coach, tn)
            elif ment == 'defend':
                txt = "HLV %s thu hẹp đội hình, %s chơi chắc chắn hơn." % (coach, tn)
            else:
                txt = "HLV %s cân bằng lại thế trận cho %s." % (coach, tn)
            self.add(self.T_of(mn) - 0.9, 'tactic', sd, txt, None, em=mn, key=True, o=5.5, ment=ment, why=why)

    def _var(self, T, side, check_text, dec_text, overturn=None, o=1.0, **kw):
        """Cặp beat VAR: đang kiểm tra -> quyết định (đồng hồ sẽ dừng ở var_check trong giao diện live)."""
        self.add(T, 'var_check', side, check_text, None, key=True, o=o, **kw)
        self.add(T + 0.06, 'var_decision', side, dec_text, None, key=True, o=o + 0.05, overturn=overturn, **kw)

    def _free_T(self, T, span=1.1):
        for _ in range(12):
            c = self._in_chain(T)
            c2 = self._in_chain(T + span)
            if c is None and c2 is None:
                return T
            T = (c or c2)['e'] + 0.15
        return T

    def _var_events(self):
        """Các tình huống VAR không gắn với một cú sút: phạt đền bị huỷ, thẻ đỏ bị rút, xem lại không đổi quyết định."""
        for v in self.r.get('var', []):
            k, side, pid = v['k'], v['t'], v.get('p')
            if k in ('goal_overturned', 'penalty_awarded', 'red_upgraded') or (k == 'no_change' and v.get('what') == 'goal'):
                continue
            opp = self.other(side)
            nm = self.name(side, pid) if pid else ''
            tn = self.team_name(side)
            T = self._free_T(self.T_of(v['m']) - 0.4)
            m = self.m_of_T(T)
            gid = self.gk_id(opp, m) if k == 'penalty_cancelled' else self.gk_id(opp, m)
            if k == 'penalty_cancelled':
                dv = self._pop_foul(opp)
                self.add(T, 'foul', opp, "Trọng tài chỉ tay vào chấm phạt đền cho %s! %s ngã trong vòng cấm." % (tn, nm),
                         self.xy(side, 0.86, 0.5), frm=dv, to=pid, key=True, o=2, chain=True)
                self._var(T + 0.2, side, "VAR đề nghị xem lại tình huống phạt đền...",
                          "Không có va chạm đủ nặng - phạt đền bị huỷ! Trận đấu tiếp tục bằng quả phát bóng.", True,
                          chain=True, vk=k)
            elif k == 'red_overturned':
                self._pop_foul(side, pid)
                self.add(T, 'foul', side, "%s vào bóng bằng hai chân! Trọng tài chạy tới định rút thẻ đỏ." % nm, None, frm=pid,
                         key=True, o=2, chain=True)
                self._var(T + 0.2, side, "VAR đề nghị trọng tài xem lại pha vào bóng của %s..." % nm,
                          "VAR can thiệp: thẻ đỏ bị rút, %s ở lại sân." % nm, True, chain=True, vk=k)
            elif v.get('what') == 'red':
                self._var(T, side, "VAR kiểm tra pha vào bóng của %s (%s)..." % (nm, tn),
                          "Không có thẻ đỏ, giữ nguyên quyết định của trọng tài.", False, chain=True, vk='no_change')
            else:
                self._var(T, opp, "Tình huống va chạm trong vòng cấm của %s - VAR kiểm tra có phạt đền không..." % tn,
                          "VAR: không có phạt đền, trận đấu tiếp tục.", False, chain=True, vk='no_change')
            e = T + 1.0
            self.add(e - 0.25, 'goalkick', opp, "%s phát bóng lên." % self.gk_name(opp, m), self.xy(opp, 0.04, 0.5),
                     frm=gid, to=gid, o=1)
            self.chains.append({'s': T - 0.1, 'e': e, 'side': side, 'o': 'off', 'pen': False, 'corner': False})

    def _ctx_beats(self):
        """Bình luận bối cảnh: thời tiết, mặt sân, khán đài (cầu thủ uống nước khi nóng, sân trơn khi mưa...)."""
        ctx = self.r.get('ctx')
        if self.ctx_txt:
            self.add(0.02, 'info', None, "Bối cảnh trận đấu: %s." % self.ctx_txt, None, key=True, o=0.5)
        if not ctx:
            return
        w, pitch = ctx['weather'], ctx['pitch']
        lines = []
        if w == 'hot':
            lines += [(28, "Trời nóng, trọng tài cho cả hai đội nghỉ uống nước giữa hiệp."),
                      (73, "Cầu thủ đã thấm mệt vì cái nóng, nhịp độ trận đấu chậm lại.")]
        elif w == 'rain':
            lines += [(12, "Mưa làm mặt sân trơn, các đường chuyền khó chính xác hơn."), (62, "Mưa nặng hạt hơn, bóng đi nhanh trên mặt cỏ ướt.")]
        elif w == 'wind':
            lines += [(20, "Gió mạnh ảnh hưởng tới các đường chuyền dài và quả tạt.")]
        elif w == 'cold':
            lines += [(8, "Trời lạnh, cầu thủ phải vận động liên tục để giữ ấm.")]
        if pitch == 'poor':
            lines.append((35, "Mặt sân xấu khiến bóng nảy khó đoán, nhiều pha xử lý bị lỗi."))
        if ctx.get('altitude'):
            lines.append((55, "Sân ở độ cao lớn, đội khách bắt đầu hụt hơi."))
        for mn, txt in lines:
            self.add(self.T_of(mn) + random.uniform(-0.2, 0.2), 'info', None, txt, None, key=False, o=0.5)

    def _in_chain(self, T):
        for c in self.chains:
            if c['s'] <= T <= c['e']:
                return c
        return None

    # ------------------------------------------------------------------ các pha bóng xen giữa (theo vị trí thật)
    def _sched_events(self, ps, pe):
        """Rải phạm lỗi / việt vị / phạt góc còn lại của mỗi đội vào các khoảng thời gian trống."""
        ev = []
        for side in ('h', 'a'):
            for pid in self.foul_pool[side]:
                ev.append(('foul', side, pid))
            for pid in self.off_pool[side]:
                ev.append(('offside', side, pid))
            ev += [('corner', side, None)] * self.corner_left[side]
        # gán thời gian theo từng hiệp tỉ lệ độ dài
        periods = [(0.0, self.ht_T), (self.ht_T, self.reg_end)] + ([(self.reg_end, self.end_T)] if self.et else [])
        total = sum(b - a for a, b in periods)
        sched = {i: [] for i in range(len(periods))}
        for kind, side, pid in ev:
            for _ in range(30):
                x = random.uniform(0, total)
                pi = 0
                for i, (a, b) in enumerate(periods):
                    if x < b - a:
                        pi, T = i, a + x
                        break
                    x -= b - a
                else:
                    pi, T = len(periods) - 1, periods[-1][1] - 1
                if self._in_chain(T) is None and T > periods[pi][0] + 0.6 and T < periods[pi][1] - 0.8:
                    if pid is None or pid in self.on(side, self.m_of_T(T)):
                        sched[pi].append((T, kind, side, pid))
                        break
        for i in sched:
            sched[i].sort()
        self.foul_pool = {'h': [], 'a': []}
        self.off_pool = {'h': [], 'a': []}
        self.corner_left = {'h': 0, 'a': 0}
        return periods, sched

    def _fillers(self):
        for b in self.beats:
            if b['k'] in ('yellow', 'red', 'injury', 'sub', 'leave', 'foul', 'tactic', 'var_check', 'var_decision') and not b.get('chain'):
                c = self._in_chain(b['T'])
                if c is not None:
                    b['T'] = round(c['e'] + 0.05, 3)
        periods, sched = self._sched_events(0, 0)
        for pi, (ps, pe) in enumerate(periods):
            T = ps + 0.35
            poss = 'h' if pi != 1 else 'a'
            m = self.m_of_T(T)
            holder = self.pick_group(poss, m, ('MID',), 'poss_q')
            st = {'poss': poss, 'holder': holder, 'ball': (0.5, 0.5)}
            sc = sched[pi]
            si = 0
            while T < pe - 0.2:
                c = self._in_chain(T)
                if c is not None:
                    T = c['e'] + 0.1
                    if T >= pe - 0.2:
                        break
                    m = self.m_of_T(T)
                    opp = self.other(c['side'])
                    if c['o'] == 'goal':
                        mids = [x for x in self.beats if x['k'] == 'restart' and abs(x['T'] - (c['e'] - 0.25)) < 0.9 and x['side'] == opp]
                        h = mids[0]['to'] if mids and mids[0].get('to') in self.on(opp, m) else self.pick_group(opp, m, ('MID',), 'poss_q')
                        st = {'poss': opp, 'holder': h, 'ball': (0.5, 0.5)}
                    elif c['o'] in ('save', 'off') or c.get('corner'):
                        g = self.gk_id(opp, m)
                        st = {'poss': opp, 'holder': g or self.pick_group(opp, m, ('DEF',), 'dfn'),
                              'ball': self.xy(opp, 0.06, 0.5)}
                    else:
                        d = self.pick_group(opp, m, ('DEF',), 'dfn')
                        st = {'poss': opp, 'holder': d, 'ball': self.xy(opp, 0.2, 0.5)}
                    continue
                m = self.m_of_T(T)
                # sự kiện đã lên lịch (phạm lỗi / việt vị / phạt góc)
                if si < len(sc) and sc[si][0] <= T:
                    _, kind, side, pid = sc[si]
                    si += 1
                    T += self._set_event(T, kind, side, pid, st, m)
                    continue
                if st['holder'] not in self.on(st['poss'], m):
                    st['holder'] = self.pick_group(st['poss'], m, ('MID', 'DEF'), 'poss_q')
                T += self._action(T, st, m)

    def _set_event(self, T, kind, side, pid, st, m):
        """Phạm lỗi / việt vị / phạt góc nằm giữa trận. Trả về thời gian đã dùng."""
        opp = self.other(side)
        if kind == 'foul':
            pos = self.positions(T, st['ball'], st['poss'], st['holder'], m)
            # nạn nhân: cầu thủ đối phương gần người phạm lỗi nhất
            tp = pos.get((side, pid))
            if tp is None:
                return 0.1
            victim = self._nearest(pos, opp, tp, m)
            self.add(T, 'foul', side, random.choice(FOUL_TXT).format(a=self.name(opp, victim), b=self.name(side, pid)),
                     tp, frm=pid, to=victim, o=2)
            rec = self._nearest(pos, opp, tp, m, exclude=(victim,))
            if rec is not None and rec != victim:
                self.add(T + 0.18, 'pass', opp, random.choice(FK_TXT).format(a=self.name(opp, victim), b=self.name(opp, rec)),
                         pos[(opp, rec)], frm=victim, to=rec, o=2, tg=[(opp, 'pass'), (opp, 'pok')])
                st.update({'poss': opp, 'holder': rec, 'ball': pos[(opp, rec)]})
            else:
                st.update({'poss': opp, 'holder': victim, 'ball': tp})
            self._spend(opp, 0.3)
            return 0.35
        if kind == 'offside':
            self.add(T, 'offside', side, random.choice(OFFSIDE_TXT).format(p=self.name(side, pid)),
                     self.xy(side, 0.9, random.uniform(0.2, 0.8)), frm=pid, to=None, key=False)
            g = self.gk_id(opp, m)
            st.update({'poss': opp, 'holder': g or self.pick_group(opp, m, ('DEF',), 'dfn'), 'ball': self.xy(opp, 0.06, 0.5)})
            self._spend(side, 0.2)
            return 0.35
        # phạt góc: bóng đi hết cuối sân sau pha tạt/bóng bị chặn
        self._corner_seq(T, side, key=False)
        d = self._pick_clear_receiver(opp, self.positions(T + 0.6, st['ball'], opp, None, m), m)
        pos = self.positions(T + 0.6, self.xy(opp, 0.3, 0.5), opp, d, m)
        st.update({'poss': opp, 'holder': d, 'ball': pos[(opp, d)]})
        self._spend(side, 0.5)
        return 0.9

    def _nearest(self, pos, side, xy, m, exclude=()):
        best, bd = None, 1e9
        for pid in self.on(side, m):
            if pid in exclude or self.pl(side, pid)['group'] == 'GK':
                continue
            d = shp.meters(pos[(side, pid)], xy)
            if d < bd:
                best, bd = pid, d
        return best

    # ------------------------------------------------------------------ đá luân lưu
    def _shootout(self, T0):
        r = self.r
        sa, sb = r['pens']
        self.add(T0, 'pens_start', None, "Hai đội hoà nhau, trận đấu được quyết định bằng loạt sút luân lưu 11m!",
                 (0.5, 0.5), key=True)
        outs = shootout_script(sa, sb)
        kick = {}
        for side in ('h', 'a'):
            P = self.P[side]
            pl = [P['players'][i] for i in eng.on_pitch(P, 120) if P['players'][i]['group'] != 'GK'] or \
                [P['players'][i] for i in eng.on_pitch(P, 120)]
            kick[side] = sorted(pl, key=lambda e: -(0.7 * e['pen'] + 0.3 * e['cmp']))
        n = len(outs[0])
        T = T0 + 1.2
        sc = [0, 0]
        for i in range(n):
            for ti, side in enumerate(('h', 'a')):
                taker = kick[side][i % len(kick[side])]
                ok = outs[ti][i]
                opp = self.other(side)
                gk = self.gk_name(opp, 120)
                if ok:
                    sc[ti] += 1
                    txt = "%s sút thành công! (%d-%d)" % (taker['name'], sc[0], sc[1])
                else:
                    if random.random() < 0.5:
                        txt = "%s sút bị %s cản phá!" % (taker['name'], gk)
                    else:
                        txt = "%s sút hỏng, bóng đi ra ngoài!" % taker['name']
                    txt += " (%d-%d)" % (sc[0], sc[1])
                x = 1.0 if side == 'h' else 0.0
                self.add(T, 'pen_kick', side, txt, (x, 0.5 + random.uniform(-0.1, 0.1)), frm=taker['pid'], to=None,
                         ok=ok, pens=(sc[0], sc[1]), key=True, shooter=taker['pid'])
                T += 1.2
        w = self.home if sa > sb else self.away
        self.add(T + 0.3, 'end', None, "%s thắng loạt luân lưu %d-%d!" % (w, sa, sb), (0.5, 0.5), key=True)

    # ------------------------------------------------------------------ bình luận chính (lưu cho lịch sử)
    def key_lines(self):
        out = []
        for b in self.beats:
            if b.get('text') and b.get('key'):
                lab = "11m" if b['k'] == 'pen_kick' else self.label(b['T'])
                out.append([lab, b['k'], b.get('side'), b['text']])
        return out
