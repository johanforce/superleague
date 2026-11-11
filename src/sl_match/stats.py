#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WNL STATS - thống kê chi tiết sau trận ("thông số trận đấu") sinh từ kết quả của engine.
=======================================================================================
detail_stats(r, A, B) -> {
    't': {tên chỉ số: [chủ nhà, khách]},                 # thống kê đội (tổng của các cầu thủ + số liệu đội)
    'p': {'h': {pid: {...}}, 'a': {pid: {...}}},          # thống kê từng cầu thủ + điểm đánh giá
}
Dùng chung cho đá nhanh và xem live: số liệu được sinh MỘT lần ngay khi mô phỏng trận (nên tab Thống kê của trang
live, bảng thống kê sau trận và lịch sử luôn khớp nhau). Bàn thắng, cú sút, thẻ phạt... lấy nguyên từ engine.
"""
import math
import random
from collections import Counter

GRP_PASS = {'GK': 0.55, 'DEF': 1.15, 'MID': 1.45, 'FWD': 0.75}
GRP_TKL = {'GK': 0.0, 'DEF': 1.3, 'MID': 1.1, 'FWD': 0.35}
GRP_CLR = {'GK': 0.15, 'DEF': 1.6, 'MID': 0.5, 'FWD': 0.12}
GRP_FOUL = {'GK': 0.05, 'DEF': 1.1, 'MID': 1.15, 'FWD': 0.7}
GRP_DRIB = {'GK': 0.0, 'DEF': 0.35, 'MID': 1.0, 'FWD': 1.6}
GRP_OFF = {'GK': 0.0, 'DEF': 0.02, 'MID': 0.35, 'FWD': 1.4}
GRP_CROSS = {'GK': 0.0, 'DEF': 0.8, 'MID': 1.0, 'FWD': 0.7}


def _poisson(lam):
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= random.random()
        if p <= L:
            return k
        k += 1


def _alloc(total, ids, weights):
    """Chia `total` đơn vị cho các cầu thủ theo trọng số (ngẫu nhiên có trọng số)."""
    if total <= 0 or not ids:
        return Counter()
    ws = [max(1e-6, w) for w in weights]
    return Counter(random.choices(ids, ws, k=total))


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def detail_stats(r, A, B):
    teams = {'h': A, 'a': B}
    shots = r.get('shot_log', [])
    poss = list(r['stats']['poss'])
    mins = {'h': r['mins_h'], 'a': r['mins_a']}
    cards = {'h': r['cards_h'], 'a': r['cards_a']}
    ga = {'h': r['ga_h'], 'a': r['ga_a']}
    goals_for = {'h': r['hg'], 'a': r['ag']}
    P = {'h': {}, 'a': {}}
    T = {}
    ctx = r.get('ctx') or {}
    pass_adj = {'rain': -0.03, 'wind': -0.012}.get(ctx.get('weather'), 0.0) + {'poor': -0.03, 'fair': -0.01}.get(ctx.get('pitch'), 0.0)
    foul_mult = r.get('ref_strict') or 1.0

    # ---- cú sút (từ engine)
    sh_by = {'h': {}, 'a': {}}
    for s in shots:
        d = sh_by[s['t']].setdefault(s['s'], {'sh': 0, 'sot': 0, 'g': 0, 'xg': 0.0, 'off': 0, 'blk': 0})
        d['sh'] += 1
        d['xg'] += s.get('xg') or 0.0
        if s['o'] in ('goal', 'save', 'goal_disallowed'):      # bàn bị VAR huỷ vẫn là cú sút trúng đích
            d['sot'] += 1
        if s['o'] == 'goal':
            d['g'] += 1
        elif s['o'] == 'off':
            d['off'] += 1
        elif s['o'] == 'block':
            d['blk'] += 1
    ast_by = {'h': Counter(), 'a': Counter()}
    for g in r['goals']:
        if g.get('a'):
            ast_by[g['t']][g['a']] += 1

    # ---- chỉ số đội cần để chia cho cầu thủ
    team_tot = {}
    for si, side in enumerate(('h', 'a')):
        opp = 'a' if side == 'h' else 'h'
        pl = teams[side]['players']
        ids = [pid for pid in mins[side]]
        mshare = {pid: mins[side][pid] / 90.0 for pid in ids}
        n_shots = sum(1 for s in shots if s['t'] == side)
        n_shots_opp = sum(1 for s in shots if s['t'] == opp)
        pq = sum(pl[pid]['poss_q'] for pid in ids) / max(1, len(ids))
        pass_att = int(round(780 * (poss[si] / 100.0) * random.uniform(0.93, 1.07)))
        acc = _clip(0.70 + (pq - 60) / 170.0 + (poss[si] - 50) / 260.0 + pass_adj + random.gauss(0, 0.012), 0.60, 0.93)
        corners = _clip(_poisson(1.3 + 0.27 * n_shots + 0.03 * (poss[si] - 50)), 0, 16)
        n_cards = len(cards[side])
        fouls = max(_poisson(10.5 * foul_mult) + 1, n_cards + 2)
        offs = _clip(_poisson(1.7), 0, 7)
        tkl = _clip(int(random.gauss(15 + (50 - poss[si]) * 0.12, 3.5)), 5, 30)
        intc = _clip(int(random.gauss(9 + (50 - poss[si]) * 0.06, 2.8)), 2, 20)
        clr = _clip(_poisson(12 + (50 - poss[si]) * 0.35 + n_shots_opp * 0.5), 2, 45)
        crosses = _clip(_poisson(13 + (poss[si] - 50) * 0.1), 3, 35)
        drib = _clip(_poisson(14 + (poss[si] - 50) * 0.05), 3, 28)
        throw = _clip(_poisson(18), 8, 34)
        team_tot[side] = dict(pass_att=pass_att, acc=acc, corners=corners, fouls=fouls, offs=offs, tkl=tkl, intc=intc,
                              clr=clr, crosses=crosses, drib=drib, throw=throw)

    # ---- chia cho từng cầu thủ
    for si, side in enumerate(('h', 'a')):
        opp = 'a' if side == 'h' else 'h'
        pl = teams[side]['players']
        ids = list(mins[side])
        tt = team_tot[side]
        w = lambda grp, key=None, power=1.0: [mins[side][i] / 90.0 * grp[pl[i]['group']] *
                                             ((max(20.0, pl[i].get(key, 60)) / 70.0) ** power if key else 1.0) for i in ids]
        passes = _alloc(tt['pass_att'], ids, w(GRP_PASS, 'poss_q', 2))
        tkl = _alloc(tt['tkl'], ids, w(GRP_TKL, 'dfn', 2))
        intc = _alloc(tt['intc'], ids, w(GRP_TKL, 'dfn', 2))
        clr = _alloc(tt['clr'], ids, w(GRP_CLR, 'dfn', 1))
        fouls = _alloc(tt['fouls'], ids, w(GRP_FOUL, 'agg', 1))
        for c in cards[side]:                       # người nhận thẻ phải có lỗi
            if fouls[c['p']] < (2 if c['c'] in ('yr', 'r') else 1):
                fouls[c['p']] += 1
        offs = _alloc(tt['offs'], ids, w(GRP_OFF, 'spd', 1))
        drib = _alloc(tt['drib'], ids, w(GRP_DRIB, 'dri', 1))
        crosses = _alloc(tt['crosses'], ids, w(GRP_CROSS, 'ast_q', 1))
        # key pass: mỗi cú sút (không phạt đền) có 1 người tạo cơ hội
        kp = Counter()
        for s in shots:
            if s['t'] == side and not s.get('pen'):
                if s.get('a'):
                    kp[s['a']] += 1
                else:
                    cand = [i for i in ids if i != s['s'] and pl[i]['group'] != 'GK']
                    if cand:
                        kp[random.choices(cand, [max(20, pl[i]['ast_q']) ** 2 * GRP_PASS[pl[i]['group']] for i in cand])[0]] += 1
        saves_total = sum(1 for s in shots if s['t'] == opp and s['o'] == 'save')
        ycards = Counter(c['p'] for c in cards[side] if c['c'] in ('y', 'yr'))
        rcards = Counter(c['p'] for c in cards[side] if c['c'] in ('r', 'yr'))
        acc_t = tt['acc']
        for pid in ids:
            e = pl[pid]
            att = passes[pid]
            a_i = _clip(acc_t + (e['poss_q'] - 70) / 260.0 + (-0.03 if e['group'] == 'FWD' else 0.02 if e['group'] == 'DEF' else 0)
                        + random.gauss(0, 0.02), 0.45, 0.98)
            ok = int(_clip(round(att * a_i), 0, att))
            sd = sh_by[side].get(pid, {})
            mn = mins[side][pid]
            d = {'min': mn, 'g': sd.get('g', 0), 'a': ast_by[side][pid], 'sh': sd.get('sh', 0), 'sot': sd.get('sot', 0),
                 'xg': round(sd.get('xg', 0.0), 2), 'pass': att, 'pok': ok, 'kp': kp[pid], 'tkl': tkl[pid], 'int': intc[pid],
                 'clr': clr[pid], 'fl': fouls[pid], 'yc': ycards[pid], 'rc': rcards[pid], 'off': offs[pid],
                 'drb': drib[pid], 'crs': crosses[pid], 'sv': 0, 'ga': ga[side].get(pid, 0)}
            P[side][pid] = d
        # thủ môn: cứu thua
        gks = [i for i in ids if pl[i]['group'] == 'GK']
        if gks and saves_total:
            gk_alloc = _alloc(saves_total, gks, [mins[side][i] for i in gks])
            for i, n in gk_alloc.items():
                P[side][i]['sv'] = n
        # tổng đội = tổng cầu thủ
        tt['tot'] = {k: sum(P[side][i][k] for i in ids) for k in
                     ('pass', 'pok', 'tkl', 'int', 'clr', 'fl', 'off', 'drb', 'crs', 'kp')}

    # ---- điểm đánh giá
    for si, side in enumerate(('h', 'a')):
        opp_goals = goals_for['a' if side == 'h' else 'h']
        sign = 1 if goals_for[side] > opp_goals else (-1 if goals_for[side] < opp_goals else 0)
        if r.get('pens'):
            sign = 0
        for pid, d in P[side].items():
            grp = teams[side]['players'][pid]['group']
            q = 6.15 + 0.95 * d['g'] + 0.65 * d['a'] + 0.12 * d['sot'] + 0.02 * (d['sh'] - d['sot']) + 0.11 * d['kp']
            q += 0.09 * d['tkl'] + 0.08 * d['int'] + 0.035 * d['clr'] + 0.28 * d['sv']
            q -= 0.035 * d['fl'] + 0.25 * d['yc'] + 1.6 * d['rc'] + 0.05 * d['off']
            conc = d['ga']
            q -= conc * (0.2 if grp in ('GK', 'DEF') else 0.04)
            if conc == 0 and d['min'] >= 60 and grp in ('GK', 'DEF'):
                q += 0.35
            if d['pass'] >= 15:
                q += (d['pok'] / d['pass'] - 0.80) * 1.3
            q += 0.22 * sign + random.gauss(0, 0.28)
            w = min(1.0, d['min'] / 60.0)
            q = 6.0 + (q - 6.0) * (0.35 + 0.65 * w)
            d['rt'] = round(_clip(q, 3.0, 10.0), 1)

    # ---- tổng hợp chỉ số đội [nhà, khách]
    cnt = lambda side, fn: sum(1 for x in shots if x['t'] == side and fn(x))
    hs = lambda fn: [cnt('h', fn), cnt('a', fn)]
    T['poss'] = poss
    T['shots'] = hs(lambda x: True)
    T['sot'] = hs(lambda x: x['o'] in ('goal', 'save', 'goal_disallowed'))
    T['off'] = hs(lambda x: x['o'] == 'off')
    T['blk'] = hs(lambda x: x['o'] == 'block')
    T['big'] = hs(lambda x: (x.get('xg') or 0) >= 0.3)
    T['xg'] = [round(sum(x.get('xg') or 0 for x in shots if x['t'] == s), 2) for s in ('h', 'a')]
    both = lambda f: [f('h'), f('a')]
    T['pass'] = both(lambda s: team_tot[s]['tot']['pass'])
    T['pok'] = both(lambda s: team_tot[s]['tot']['pok'])
    T['crs'] = both(lambda s: team_tot[s]['tot']['crs'])
    T['corners'] = both(lambda s: team_tot[s]['corners'])
    T['fouls'] = both(lambda s: team_tot[s]['tot']['fl'])
    T['offs'] = both(lambda s: team_tot[s]['tot']['off'])
    T['tkl'] = both(lambda s: team_tot[s]['tot']['tkl'])
    T['int'] = both(lambda s: team_tot[s]['tot']['int'])
    T['clr'] = both(lambda s: team_tot[s]['tot']['clr'])
    T['drb'] = both(lambda s: team_tot[s]['tot']['drb'])
    T['throw'] = both(lambda s: team_tot[s]['throw'])
    T['sv'] = both(lambda s: sum(d['sv'] for d in P[s].values()))
    T['yc'] = both(lambda s: sum(1 for c in cards[s] if c['c'] in ('y', 'yr')))
    T['rc'] = both(lambda s: sum(1 for c in cards[s] if c['c'] in ('r', 'yr')))
    T['kp'] = both(lambda s: team_tot[s]['tot']['kp'])
    var = r.get('var', [])
    T['varck'] = [sum(1 for v in var if v['t'] == s) for s in ('h', 'a')]
    T['gdis'] = [sum(1 for v in var if v['t'] == s and v['k'] == 'goal_overturned') for s in ('h', 'a')]
    return {'t': T, 'p': P}


def best_player(det):
    """(side, pid, rating) của cầu thủ có điểm cao nhất (ưu tiên đội thắng khi bằng điểm)."""
    best = None
    for side in ('h', 'a'):
        for pid, d in det['p'][side].items():
            if d['min'] >= 10 and (best is None or d['rt'] > best[2]):
                best = [side, pid, d['rt']]
    return best


# nhãn hiển thị (dùng chung cho trang live và báo cáo sau trận): (khoá, tên, kiểu: 'n' số, 'p' %, 'x' xG, 'r' tỉ lệ)
STAT_ROWS = [
    ('poss', 'Kiểm soát bóng', 'p'), ('xg', 'xG (bàn thắng kỳ vọng)', 'x'), ('shots', 'Số cú sút', 'n'),
    ('sot', 'Sút trúng đích', 'n'), ('off', 'Sút ngoài khung thành', 'n'), ('blk', 'Sút bị chặn', 'n'),
    ('big', 'Cơ hội ăn bàn rõ rệt', 'n'), ('corners', 'Phạt góc', 'n'), ('offs', 'Việt vị', 'n'),
    ('pass', 'Chuyền bóng', 'n'), ('pok', 'Chuyền chính xác', 'n'), ('kp', 'Đường chuyền tạo cơ hội', 'n'),
    ('crs', 'Tạt bóng', 'n'), ('drb', 'Rê bóng', 'n'), ('tkl', 'Tắc bóng thành công', 'n'),
    ('int', 'Cắt bóng', 'n'), ('clr', 'Phá bóng', 'n'), ('sv', 'Cứu thua (thủ môn)', 'n'), ('throw', 'Ném biên', 'n'),
    ('fouls', 'Phạm lỗi', 'n'), ('yc', 'Thẻ vàng', 'n'), ('rc', 'Thẻ đỏ', 'n'),
    ('varck', 'Lần xem lại VAR', 'n'), ('gdis', 'Bàn thắng bị huỷ (VAR)', 'n'),
]
