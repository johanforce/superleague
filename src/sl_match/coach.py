#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WNL COACH - HLV AI: thay người và đổi chiến thuật theo diễn biến trận (mục 3 trong kế hoạch).
===========================================================================================
File này KHÔNG import engine. Engine (wnl_engine.simulate) chạy trận theo từng cửa sổ thời gian (~5 phút, thêm các
điểm ngắt: sau bàn thắng, thẻ đỏ, chấn thương, hết hiệp). Tại mỗi cửa sổ engine dựng một "MatchView" cho mỗi đội rồi gọi
decide(coach, cs, view, rng) và áp dụng các hành động trả về (có kiểm tra giới hạn lượt thay).

Tính cách HLV (0..1), mặc định sinh theo tên đội để ổn định:
    aggr   (aggression)   : dám dâng cao / đổi người tấn công sớm khi bị dẫn
    cons   (conservatism) : thích bảo toàn tỷ số, lùi sâu sớm khi dẫn bàn
    trust  (trust_bench)  : tin dự bị -> thay nhiều và sớm hơn

Mỗi hành động có 'why' (lý do) để bình luận / báo cáo nói được "thay người vì thể lực" hay "thay người chiến thuật":
    fatigue | injury | chasing | protect | red_card | tactical

MatchView (dict):
    t, gf, ga, ko, subs_left, stops_left, free_stop, trig (set), ment, formation, gk_on,
    on    : [{pid, group, pos, energy, yc, ovr, form, agg, pen, gk}]  (cầu thủ đang trên sân)
    bench : [{pid, group, pos, ovr, form, fit0, pen, gk}]              (dự bị còn có thể vào; không gồm người đã bị thay ra)
Hành động: {'t': 'sub', 'out': pid, 'in': pid, 'why': str} | {'t': 'ment', 'ment': 'attack'|'balanced'|'defend', 'why': str}
"""
import math
import random
import zlib

from .human_data import load_human_data

REASONS = ('fatigue', 'injury', 'chasing', 'protect', 'red_card', 'tactical')
REASON_TXT = {'fatigue': "vì thể lực", 'injury': "vì chấn thương", 'chasing': "để đuổi tỷ số", 'protect': "để giữ tỷ số",
              'red_card': "sau thẻ đỏ", 'tactical': "vì lý do chiến thuật"}
def make_coach(team, manager_name=None):
    """Tạo hồ sơ HLV; tên lấy từ reference_data trong SQLite, thuộc tính ổn định theo CLB."""
    h = zlib.crc32(("coach|" + str(team)).encode('utf-8')) & 0xffffffff
    r = random.Random(h)
    if not manager_name:
        managers = load_human_data()["managers"]
        manager_name = managers[h % len(managers)]
    return {'name': str(manager_name),
            'aggr': round(r.uniform(0.15, 0.95), 2), 'cons': round(r.uniform(0.15, 0.95), 2),
            'trust': round(r.uniform(0.2, 0.95), 2)}


def default_coach():
    return {'name': "HLV", 'aggr': 0.5, 'cons': 0.5, 'trust': 0.55}


def new_state(coach, rng):
    """Trạng thái quyết định trong 1 trận (mỗi đội 1 bản)."""
    target = 3 + int(round(2.0 * coach['trust'] + rng.gauss(0, 0.55)))
    return {'target': max(2, min(5, target)), 'chase': 0, 'protect': 0, 'fresh': 0, 'red_done': False, 'opp_red_done': False,
            'ht_done': False}


# --------------------------------------------------------------------- tiện ích chọn người
def _counts(on):
    c = {'GK': 0, 'DEF': 0, 'MID': 0, 'FWD': 0}
    for e in on:
        c[e['group']] += 1
    return c


def formation_of(on):
    c = _counts(on)
    return (c['DEF'], c['MID'], c['FWD'])


def _bench_score(b, want=None):
    s = b['ovr'] + 8.0 * b['form'] + 0.05 * b['fit0']
    if want == 'pen':
        s = b['pen']
    elif want == 'def':
        s += 0.0
    return s


def _best_bench(v, groups, used_in, want=None, pos_pref=None):
    c = [b for b in v['bench'] if b['group'] in groups and b['pid'] not in used_in]
    if not c:
        return None
    if pos_pref:
        pp = [b for b in c if b['pos'] in pos_pref]
        if pp and max(_bench_score(b) for b in pp) >= max(_bench_score(b) for b in c) - 6:
            c = pp
    return max(c, key=lambda b: _bench_score(b, want))


def _tired_first(on, groups, used_out, bias_low_quality=0.5):
    c = [e for e in on if e['group'] in groups and e['pid'] not in used_out and not e.get('is_gk')]
    if not c:
        return None
    return min(c, key=lambda e: e['energy'] * 100.0 + bias_low_quality * (e['ovr'] - 70) * 0.5)


# --------------------------------------------------------------------- quyết định
def decide(coach, cs, v, rng):
    """Trả về danh sách hành động cho 1 đội tại 1 cửa sổ."""
    acts = []
    if v.get('pens_next'):          # hết hiệp phụ vẫn hoà -> chỉ xét thay người cho loạt luân lưu
        return _pens_subs(v, acts, set(), set())
    t, gf, ga = v['t'], v['gf'], v['ga']
    diff = gf - ga
    on = v['on']
    trig = v['trig']
    subs_left, stops_left = v['subs_left'], v['stops_left']
    free = v['free_stop']
    used_out, used_in = set(), set()
    cur_ment = v['ment']
    pool = [e for e in on if e.get('since', 99) >= 25]       # người vừa vào sân chưa bị thay ra ngay

    def can_sub(n_planned):
        # lượt thay còn lại, và còn cửa sổ thay (hoặc đang là cửa sổ miễn phí: nghỉ giữa hiệp / trước hiệp phụ)
        return subs_left - n_planned > 0 and (stops_left > 0 or free)

    def add_sub(out, inn, why):
        acts.append({'t': 'sub', 'out': out['pid'], 'in': inn['pid'], 'why': why})
        used_out.add(out['pid'])
        used_in.add(inn['pid'])

    def set_ment(m, why, force=False):
        nonlocal cur_ment
        if m != cur_ment or force:
            acts.append({'t': 'ment', 'ment': m, 'why': why})
            cur_ment = m

    reserve = 1 if (v['ko'] and diff == 0 and 70 <= t < 90 and not v['et']) else 0   # giữ lượt thay cuối cho hiệp phụ

    # 0) mất thủ môn (thẻ đỏ / chấn thương) -> khẩn cấp thay thủ môn dù hết cửa sổ
    if not v['gk_on'] and subs_left > 0:
        gkb = [b for b in v['bench'] if b['group'] == 'GK']
        if gkb:
            out = _tired_first(pool, ('FWD', 'MID'), used_out) or _tired_first(pool, ('DEF',), used_out)
            if out:
                add_sub(out, max(gkb, key=lambda b: b['gkq']), 'red_card' if 'red_own' in trig else 'injury')

    # 1) thẻ đỏ của mình: phản ứng ngay (đổi tiền đạo/tiền vệ cánh lấy hậu vệ, lùi sâu)
    if 'red_own' in trig:
        cs['red_done'] = True
        set_ment('defend', 'red_card', force=True)       # luôn có phản ứng ngay khi bị thẻ đỏ
        n_def = _counts(on)['DEF']
        if can_sub(len(acts)) and not reserve:
            out = _tired_first(pool, ('FWD',), used_out, 0.3) or _tired_first(pool, ('MID',), used_out, 0.3)
            inn = _best_bench(v, ('DEF',), used_in) if n_def < 5 else _best_bench(v, ('MID',), used_in)
            if out and inn:
                add_sub(out, inn, 'red_card')
    # 2) đối thủ bị đuổi: dâng cao, thêm người tuyến trên
    elif 'red_opp' in trig and not cs['opp_red_done']:
        cs['opp_red_done'] = True
        if diff <= 0 or rng.random() < 0.5 + 0.4 * coach['aggr']:
            set_ment('attack', 'tactical')
        if diff <= 0 and can_sub(len(acts)) and t >= 30 and rng.random() < 0.55 + 0.35 * coach['aggr']:
            out = _tired_first(pool, ('DEF',), used_out) if _counts(on)['DEF'] >= 4 else _tired_first(pool, ('MID',), used_out)
            inn = _best_bench(v, ('FWD',), used_in)
            if out and inn:
                add_sub(out, inn, 'tactical')

    # 3) đang bị dẫn: đuổi tỷ số
    chase_from = 58 - 10 * coach['aggr']
    if diff < 0 and t >= chase_from and 'red_own' not in trig:
        if cur_ment != 'attack' and (t >= 65 - 8 * coach['aggr'] or diff <= -2):
            set_ment('attack', 'chasing')
        max_chase = 3 if (diff <= -2 and coach['aggr'] > 0.6) else 2
        p = (0.50 + 0.35 * coach['aggr']) * (1.35 if 'goal_against' in trig else 1.0)
        if cs['chase'] < max_chase and can_sub(len(acts)) and rng.random() < p:
            n_def = _counts(on)['DEF'] - sum(1 for a in acts if a['t'] == 'sub' and any(e['pid'] == a['out'] and e['group'] == 'DEF' for e in on))
            if n_def >= 4 or (n_def >= 3 and cs['chase'] >= 1 and rng.random() < 0.3):
                out = _tired_first(pool, ('DEF',), used_out)
            else:
                out = _tired_first(pool, ('MID',), used_out) or _tired_first(pool, ('DEF',), used_out)
            inn = _best_bench(v, ('FWD',), used_in) or _best_bench(v, ('MID',), used_in, pos_pref=('CAM', 'LM', 'RM'))
            if out and inn and out['group'] != inn['group']:
                add_sub(out, inn, 'chasing')
                cs['chase'] += 1
    # 4) đang dẫn: bảo vệ tỷ số
    protect_from = 74 - 9 * coach['cons']
    if diff > 0 and t >= protect_from and 'red_own' not in trig and not reserve:
        if cur_ment != 'defend' and (diff >= 2 or t >= 78 - 6 * coach['cons']) and rng.random() < 0.4 + 0.5 * coach['cons']:
            set_ment('defend', 'protect')
        p = (0.35 + 0.45 * coach['cons']) * (1.3 if 'goal_for' in trig else 1.0)
        if cs['protect'] < 2 and can_sub(len(acts)) and rng.random() < p:
            n_fwd = _counts(on)['FWD']
            out = _tired_first(pool, ('FWD',), used_out) if n_fwd >= 2 else _tired_first(pool, ('MID',), used_out)
            inn = _best_bench(v, ('DEF',), used_in) or _best_bench(v, ('MID',), used_in, pos_pref=('CDM', 'CM'))
            if out and inn and out['group'] != inn['group']:
                add_sub(out, inn, 'protect')
                cs['protect'] += 1
    # 5) cân bằng lại thế trận khi đã gỡ hoà / hết lý do
    if diff == 0 and cur_ment in ('attack', 'defend') and 'red_own' not in trig and 'red_opp' not in trig and rng.random() < 0.5:
        if v['ment0'] != cur_ment:
            set_ment(v['ment0'], 'tactical')

    # 6) thay người theo thể lực / thẻ vàng / làm tươi lực lượng
    n_done = v['nsubs'] + len([a for a in acts if a['t'] == 'sub'])
    sched = max(0.0, min(1.0, (t - 48) / 30.0))
    due = int(math.floor(cs['target'] * sched + 0.35))
    need = due - n_done
    if t >= 90:           # hiệp phụ: dùng lượt thay thêm cho người mệt nhất
        need = 1 if (rng.random() < (0.85 if t == 90 else 0.15) and subs_left - len(acts) > 0) else 0
    first_half_stop = (t == 45 and free and not cs['ht_done'])
    if first_half_stop:
        cs['ht_done'] = True
        need = 1 if rng.random() < 0.10 + 0.22 * coach['trust'] else 0
    if need > 0 and can_sub(len(acts)) and (t >= 50 or first_half_stop) and (t >= 90 or rng.random() < 0.72 + 0.2 * coach['trust']):
        rem = max(1, cs['target'] - n_done)             # chia đều số lượt thay còn lại cho số lần dừng còn lại
        k = need if free else max(1, int(math.ceil(rem / float(max(1, stops_left)))))
        k = min(k, subs_left - len(acts) - reserve, 3)
        for _ in range(max(0, k)):
            if not can_sub(len(acts)) or subs_left - len(acts) - reserve <= 0:
                break
            tired = [e for e in pool if e['pid'] not in used_out and not e.get('is_gk')]
            if not tired:
                break
            risk = [e for e in tired if e['yc'] and e['agg'] >= 60 and t >= 55]
            if risk and rng.random() < 0.7:
                out, why = min(risk, key=lambda e: e['energy']), 'tactical'
            else:
                out = min(tired, key=lambda e: e['energy'] * 100.0 + 0.25 * (e['ovr'] - 70) + rng.uniform(-2, 2))
                why = 'fatigue' if out['energy'] < 0.88 else 'tactical'
            inn = _best_bench(v, (out['group'],), used_in) or _best_bench(v, ('MID', 'DEF', 'FWD'), used_in)
            if not inn:
                break
            # dự bị đang có phong độ cao: ưu tiên vào sân (đã nằm trong điểm chọn dự bị)
            add_sub(out, inn, why)
            cs['fresh'] += 1

    return acts


def _pens_subs(v, acts, used_out, used_in):
    on, subs_left = v['on'], v['subs_left']
    if subs_left - len([a for a in acts if a['t'] == 'sub']) > 0:
        gk_on = next((e for e in on if e['group'] == 'GK'), None)
        gkb = [b for b in v['bench'] if b['group'] == 'GK' and b['pid'] not in used_in]
        if gk_on and gkb and max(b['gkq'] for b in gkb) >= gk_on['gkq'] + 5:
            _add(acts, used_out, used_in, gk_on, max(gkb, key=lambda b: b['gkq']), 'tactical')
        n = subs_left - len([a for a in acts if a['t'] == 'sub'])
        while n > 0:
            outs = [e for e in on if e['group'] != 'GK' and e['pid'] not in used_out]
            bn = [b for b in v['bench'] if b['group'] != 'GK' and b['pid'] not in used_in]
            if not outs or not bn:
                break
            o = min(outs, key=lambda e: e['pen'])
            i = max(bn, key=lambda b: b['pen'])
            if i['pen'] < o['pen'] + 8:
                break
            _add(acts, used_out, used_in, o, i, 'tactical')
            n -= 1
    return acts


def _add(acts, used_out, used_in, out, inn, why):
    acts.append({'t': 'sub', 'out': out['pid'], 'in': inn['pid'], 'why': why})
    used_out.add(out['pid'])
    used_in.add(inn['pid'])


def formation_text(f):
    return "-".join(str(x) for x in f)


def describe_tactic(coach_name, minute, form, why, subs=None):
    """Câu bình luận: 'HLV X chuyển sang 3-4-3 để đuổi tỷ số - đưa Y vào thay Z (hậu vệ)'."""
    base = "HLV %s chuyển sang sơ đồ %s %s" % (coach_name, formation_text(form), REASON_TXT.get(why, ""))
    if subs:
        base += " - " + "; ".join(subs)
    return base
