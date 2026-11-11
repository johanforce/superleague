#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WNL SHAPE - mô hình vị trí cầu thủ trên sân (dùng chung cho wnl_live.py và wnl_live_ui.py).
=======================================================================================
Mỗi cầu thủ có 1 "vị trí gốc" (slot) theo sơ đồ + vị trí thật (LB/RB/CDM/CAM/LW/ST...), nhưng trong trận
vị trí thực tế KHÔNG cố định mà lệch đi một chút:
  * mỗi đội mỗi trận có "tính cách" riêng (đường dây cao/thấp, bản đồ rộng/hẹp, ép cao nhiều/ít, dâng cao của hậu vệ biên),
  * mỗi cầu thủ có độ lệch riêng + dao động chậm theo thời gian (có người hay chạy chỗ nhiều hơn),
  * cả khối dịch lên/xuống theo bóng, co hẹp về phía bóng, đội đang tấn công dâng cao, đội phòng ngự lùi sâu,
  * hậu vệ biên bó biên dâng cao khi đội tấn công ở nửa sân đối phương, tiền đạo cánh cắt vào trong ở phía xa bóng,
  * 1-2 cầu thủ gần bóng nhất của đội không có bóng sẽ lao vào ép.
Hệ toạ độ tuyệt đối: x 0..1 (khung thành nhà -> khung thành khách), y 0..1. Sân thật 105m x 68m.
Hệ toạ độ "đội": depth 0..1 (từ khung thành nhà mình đến khung thành đối phương), lat 0..1 (trái -> phải theo hướng tấn công).
"""
import math
import random

PITCH_W, PITCH_H = 105.0, 68.0
GROUP_DEPTH = {'GK': 0.05, 'DEF': 0.22, 'MID': 0.45, 'FWD': 0.68}
POS_DEPTH_ADJ = {'CDM': -0.07, 'CAM': 0.07, 'LM': 0.02, 'RM': 0.02, 'LW': -0.03, 'RW': -0.03, 'ST': 0.03}
POS_LAT = {'LB': 0.0, 'LM': 0.0, 'LW': 0.0, 'RB': 1.0, 'RM': 1.0, 'RW': 1.0}
FOLLOW = {'GK': 0.15, 'DEF': 1.0, 'MID': 0.9, 'FWD': 0.7}
LAT_TABLE = {
    ('DEF', 3): [.22, .5, .78], ('DEF', 4): [.12, .38, .62, .88], ('DEF', 5): [.07, .28, .5, .72, .93],
    ('MID', 1): [.5], ('MID', 2): [.38, .62], ('MID', 3): [.24, .5, .76], ('MID', 4): [.1, .37, .63, .9],
    ('MID', 5): [.08, .3, .5, .7, .92], ('FWD', 1): [.5], ('FWD', 2): [.38, .62], ('FWD', 3): [.15, .5, .85],
}


def meters(a, b):
    return math.hypot((a[0] - b[0]) * PITCH_W, (a[1] - b[1]) * PITCH_H)


def lat_slots(n, group):
    return LAT_TABLE.get((group, n)) or [(i + 1) / (n + 1.0) for i in range(n)]


def assign_slots(xi):
    """xi: danh sách cầu thủ xuất phát. Trả về {pid: (depth, lat)} theo vị trí thật (LB trái, RB phải, CDM sâu, CAM cao...)."""
    out = {}
    by = {'GK': [], 'DEF': [], 'MID': [], 'FWD': []}
    for e in xi:
        by[e['group']].append(e)
    for g, lst in by.items():
        if g == 'GK':
            for e in lst:
                out[e['pid']] = (GROUP_DEPTH['GK'], 0.5)
            continue
        lst = sorted(lst, key=lambda e: POS_LAT.get(e['pos'], 0.5) + random.uniform(-0.05, 0.05))
        for e, la in zip(lst, lat_slots(len(lst), g)):
            out[e['pid']] = (GROUP_DEPTH[g] + POS_DEPTH_ADJ.get(e['pos'], 0.0), la)
    return out


class TeamShape:
    """Hình dạng + 'tính cách' của 1 đội trong 1 trận."""

    def __init__(self, side, xi, rng=None):
        r = rng or random
        self.side = side
        self.players = {e['pid']: e for e in xi}
        self.slot = assign_slots(xi)
        self.sched = []          # [(phút, {pid: (depth, lat)})] - các lần đổi sơ đồ giữa trận (reform)
        self.line_off = r.gauss(0.0, 0.02)
        self.width = r.uniform(0.95, 1.07)
        self.press = r.uniform(0.8, 1.25)
        self.compact = r.uniform(0.9, 1.2)
        self.overlap = r.uniform(0.6, 1.25)
        self.indiv = {}
        for pid in self.slot:
            roam = 2.2 if r.random() < 0.15 else 1.0
            self.indiv[pid] = (r.gauss(0, 0.016) * roam, r.gauss(0, 0.028) * roam, r.uniform(0, 6.28), r.uniform(0.35, 0.9),
                               r.uniform(0, 6.28), r.uniform(0.3, 0.8), 0.012 * roam)

    def inherit(self, new_pid, old_pid, new_player):
        """Cầu thủ vào sân thay người nhận vị trí của người ra (có lệch nhẹ riêng)."""
        self.players[new_pid] = new_player
        self.slot[new_pid] = self.slot.get(old_pid, (0.45, 0.5))
        self.indiv[new_pid] = (random.gauss(0, 0.016), random.gauss(0, 0.028), random.uniform(0, 6.28),
                               random.uniform(0.35, 0.9), random.uniform(0, 6.28), random.uniform(0.3, 0.8), 0.012)

    def reform(self, minute, players):
        """Đổi sơ đồ từ phút `minute`: người đang trên sân giữ tên nhưng được gán lại vị trí theo cơ cấu mới."""
        slots = assign_slots(list(players))
        for e in players:
            self.players[e['pid']] = e
            if e['pid'] not in self.indiv:
                self.indiv[e['pid']] = (random.gauss(0, 0.016), random.gauss(0, 0.028), random.uniform(0, 6.28),
                                        random.uniform(0.35, 0.9), random.uniform(0, 6.28), random.uniform(0.3, 0.8), 0.012)
        self.sched.append((minute, slots))
        self.sched.sort(key=lambda x: x[0])

    def slot_at(self, pid, minute):
        if minute is not None:
            for m, sl in reversed(self.sched):
                if m <= minute and pid in sl:
                    return sl[pid]
        return self.slot.get(pid, (0.45, 0.5))

    def targets(self, ids, T, ball_team, poss_team, holder, push=0.0, minute=None):
        """
        ids        : cầu thủ đang trên sân
        ball_team  : (depth, lat) của bóng theo toạ độ ĐỘI NÀY
        poss_team  : True nếu đội này đang cầm bóng
        holder     : pid đang cầm bóng của đội này (hoặc None)
        push       : độ dâng cao thêm (đội đang bị dẫn bàn thì > 0)
        Trả về {pid: (depth, lat)} theo toạ độ đội.
        """
        bd, bl = ball_team
        shift = (bd - 0.5) * 0.46 + (0.055 if poss_team else -0.045) + self.line_off + push
        res = {}
        for pid in ids:
            e = self.players.get(pid)
            if e is None:
                continue
            d0, l0 = self.slot_at(pid, minute)
            g, pos = e['group'], e['pos']
            ix, iy, ph1, f1, ph2, f2, amp = self.indiv.get(pid, (0, 0, 0, 0.5, 0, 0.5, 0.01))
            d = d0 + shift * FOLLOW[g]
            wide = l0 < 0.2 or l0 > 0.8
            l = 0.5 + (l0 - 0.5) * self.width * (1.0 if poss_team else 0.9)
            if g != 'GK':
                l += (bl - l) * 0.16 * self.compact          # cả khối co về phía bóng
            if poss_team and bd > 0.58 and g != 'GK':
                near_ball_side = abs(l0 - bl) < 0.4
                if pos in ('LB', 'RB') or (g == 'DEF' and wide):
                    d += (0.20 if near_ball_side else 0.08) * self.overlap          # hậu vệ biên dâng cao bó biên
                elif pos in ('LW', 'RW', 'LM', 'RM') and not near_ball_side:
                    l += (0.5 - l) * 0.35                                              # cánh xa bóng cắt vào trong
                    d += 0.04
                elif g == 'FWD' and not wide:
                    d += 0.05                                                          # tiền đạo trung tâm áp sát vòng cấm
                elif g == 'MID':
                    d += 0.03
            if g == 'DEF' and poss_team and bd < 0.4:
                d += 0.03 * self.overlap
            if g == 'GK':
                d = 0.05 + max(0.0, (bd - 0.55)) * 0.12 * (1.0 if poss_team else 0.0)
            d += ix + amp * math.sin(T * f1 + ph1)
            l += iy + amp * 1.6 * math.sin(T * f2 + ph2)
            res[pid] = [min(0.985, max(0.015, d)), min(0.985, max(0.015, l))]
        if not poss_team:
            # 1-2 cầu thủ gần bóng nhất lao vào ép
            cand = sorted((p for p in res if self.players[p]['group'] != 'GK'),
                          key=lambda p: math.hypot((res[p][0] - bd) * PITCH_W, (res[p][1] - bl) * PITCH_H))
            for rank, p in enumerate(cand[:2]):
                w = (0.62 if rank == 0 else 0.30) * min(1.4, self.press)
                res[p][0] += (bd - res[p][0]) * w
                res[p][1] += (bl - res[p][1]) * w
        if holder in res:
            res[holder] = [bd, bl]
        return {p: (v[0], v[1]) for p, v in res.items()}


class MatchShape:
    """Hai đội cùng lúc, trả về toạ độ tuyệt đối trên sân."""

    def __init__(self, P_h, P_a):
        self.team = {'h': TeamShape('h', P_h['xi']), 'a': TeamShape('a', P_a['xi'])}
        self.ments = {'h': [], 'a': []}        # [(phút, 'attack'|'balanced'|'defend')] do HLV quyết định

    def set_ments(self, ments):
        """ments: [(phút, đội 'h'/'a', thế trận, lý do)] từ kết quả engine."""
        for mn, side, ment, _ in ments:
            self.ments[side].append((mn, ment))
        for k in self.ments:
            self.ments[k].sort(key=lambda x: x[0])

    def ment_at(self, side, minute, base='balanced'):
        cur = base
        for mn, m in self.ments[side]:
            if mn <= minute:
                cur = m
        return cur

    @staticmethod
    def to_team(side, xy):
        return (xy[0], xy[1]) if side == 'h' else (1.0 - xy[0], 1.0 - xy[1])

    @staticmethod
    def to_abs(side, dl):
        return (dl[0], dl[1]) if side == 'h' else (1.0 - dl[0], 1.0 - dl[1])

    def positions(self, on, T, ball, poss_side, holder, diff=(0, 0), minute=0):
        """on: {'h': [pid], 'a': [pid]}; ball: (x, y) tuyệt đối. Trả về {(side, pid): (x, y)} tuyệt đối."""
        out = {}
        for side in ('h', 'a'):
            my = diff[0] - diff[1] if side == 'h' else diff[1] - diff[0]
            push = 0.0
            if minute > 60 and my < 0:
                push = min(0.07, 0.025 * (-my) * (minute - 55) / 35.0)
            elif minute > 70 and my > 0:
                push = -0.03
            if self.ments[side]:
                mt = self.ment_at(side, minute, 'balanced')
                push += 0.04 if mt == 'attack' else (-0.035 if mt == 'defend' else 0.0)
            tt = self.team[side].targets(on[side], T, self.to_team(side, ball), poss_side == side,
                                         holder if poss_side == side else None, push, minute)
            for pid, dl in tt.items():
                out[(side, pid)] = self.to_abs(side, dl)
        return out
