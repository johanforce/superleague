#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WNL CONTEXT - yếu tố bối cảnh của một trận (mục 2 trong kế hoạch).
==================================================================
Mỗi trận có một "ctx" (dict nhỏ, lưu vào m['ctx']), sinh MỘT lần trước khi gọi engine, theo seed của trận
(cùng mùa + cùng id trận => cùng bối cảnh, dù đá nhanh hay xem live):

    ctx = {'weather': 'hot'|'rain'|'wind'|'cold'|'normal', 'temp': 33, 'pitch': 'good'|'fair'|'poor',
           'crowd': 0..1, 'ref': ref_id, 'fatigue': {'h': 0..1, 'a': 0..1}, 'altitude': bool, 'importance': 0..1,
           'hadv': {'h': x, 'a': y}}     # hadv: hệ số lợi thế sân nhà của CHỦ NHÀ (xem team_ctx)

NGUYÊN TẮC CỐT LÕI: tổng ảnh hưởng bị chặn.
    mult = clamp(1 + sum(effect_i), 0.90, 1.10)       # tối đa +-10% lên sức mạnh hiệu dụng (ATT / DEF)
Đội yếu hơn rõ rệt vẫn thua phần lớn trận dù bối cảnh có lợi (xem tests/test_context.py).

effects(ctx, refs, neutral, tech) -> dict mà engine dùng:
    att[2], dfn[2]   : hệ số nhân sức mạnh tấn công / phòng ngự (đã chặn 0.90 - 1.10)
    ha[2]            : hệ số nhân số cú sút theo lợi thế sân nhà (neutral => (1, 1))
    shots            : hệ số nhân tần suất sút chung (mưa/gió/nóng -> ít cú sút hơn một chút)
    drain            : hệ số nhân mức tụt thể lực trong trận
    poss_scale       : hệ số nhân độ nhạy kiểm soát bóng (sân xấu -> hai đội gần nhau hơn)
    pass_acc         : độ lệch tỷ lệ chuyền chính xác (dùng ở wnl_stats)
    yellow[2], red[2], foul, pen : hệ số theo trọng tài (+ áp lực khán đài)
    var_use          : mức dùng VAR của trọng tài
    injury[2]        : hệ số nguy cơ chấn thương (mệt mỏi dồn)
Module này KHÔNG import engine / game nên test độc lập được.
"""
import random
import zlib

from .human_data import load_human_data

# ----------------------------------------------------------------- tham số
CAP_LO, CAP_HI = 0.90, 1.10
WEATHER_TXT = {'normal': "Thời tiết dễ chịu", 'hot': "Nắng nóng", 'rain': "Trời mưa", 'wind': "Gió mạnh", 'cold': "Trời lạnh"}
PITCH_TXT = {'good': "Mặt sân tốt", 'fair': "Mặt sân trung bình", 'poor': "Mặt sân xấu"}
CLIMATES = ('tropical', 'arid', 'temperate_n', 'temperate_s', 'cold')
# P(thời tiết | khí hậu, mùa) ; mùa: 0 = đông bắc bán cầu (12-2), 1 = xuân (3-5), 2 = hè (6-8), 3 = thu (9-11)
_W = {
    'tropical':    [{'normal': .35, 'rain': .45, 'hot': .15, 'wind': .05}] * 4,
    'arid':        [{'normal': .45, 'hot': .15, 'wind': .35, 'rain': .05}, {'normal': .35, 'hot': .45, 'wind': .15, 'rain': .05},
                    {'normal': .15, 'hot': .75, 'wind': .08, 'rain': .02}, {'normal': .35, 'hot': .45, 'wind': .15, 'rain': .05}],
    'temperate_n': [{'cold': .55, 'rain': .20, 'normal': .20, 'wind': .05}, {'normal': .45, 'rain': .35, 'wind': .15, 'cold': .05},
                    {'hot': .45, 'normal': .40, 'rain': .10, 'wind': .05}, {'normal': .40, 'rain': .40, 'wind': .15, 'cold': .05}],
    'cold':        [{'cold': .80, 'wind': .10, 'normal': .08, 'rain': .02}, {'cold': .45, 'rain': .25, 'normal': .20, 'wind': .10},
                    {'normal': .55, 'rain': .25, 'hot': .10, 'wind': .10}, {'cold': .40, 'rain': .30, 'wind': .20, 'normal': .10}],
}
_W['temperate_s'] = [_W['temperate_n'][2], _W['temperate_n'][3], _W['temperate_n'][0], _W['temperate_n'][1]]
_TEMP = {'tropical': 28, 'arid': 30, 'temperate_n': 14, 'temperate_s': 14, 'cold': 6}
_TEMP_BY_W = {'hot': 33, 'cold': 3, 'rain': 14, 'wind': 13, 'normal': 20}

# hệ số ảnh hưởng (cộng vào effect_i rồi chặn)
EFFECT = {
    'home_att': 0.020, 'home_def': 0.012,          # lợi thế sân nhà bổ sung theo khán đài (nhân với crowd - 0.5)
    'crowd_pressure_away': 0.010,                  # đội khách chịu áp lực
    'fatigue_att': -0.045, 'fatigue_def': -0.035,  # nhân với fatigue 0..1
    'hot_att': -0.006, 'rain_def': -0.006,
    'poor_pitch_tech': -0.030,                     # đội thiên về kỹ thuật bị phạt trên sân xấu (nhân tech -1..1)
}


def _h(*parts):
    return zlib.crc32("|".join(str(p) for p in parts).encode('utf-8')) & 0xffffffff


def clamp(x, lo=CAP_LO, hi=CAP_HI):
    return lo if x < lo else (hi if x > hi else x)


def capped(*effects):
    """mult = clamp(1 + sum(effects), 0.90, 1.10)."""
    return clamp(1.0 + sum(effects))


# ----------------------------------------------------------------- trọng tài
def make_refs(seed=2026, n=None):
    """Tạo danh sách trọng tài từ reference_data trong SQLite.

    Mỗi tên trong JSON xuất hiện tối đa một lần. Mức độ nghiêm khắc và các
    thuộc tính điều hành được sinh ổn định theo seed + ID trọng tài.
    Mặc định sử dụng toàn bộ danh sách 38 trọng tài.
    """
    names = list(load_human_data()["referees"])
    count = len(names) if n is None else max(0, min(int(n), len(names)))
    refs = []
    for ref_id, name in enumerate(names[:count]):
        rng = random.Random((int(seed) ^ 0x524546) + ref_id * 7919)
        refs.append({
            'id': ref_id,
            'name': name,
            'strict': round(min(1.45, max(0.6, rng.gauss(1.0, 0.22))), 2),
            'home_bias': round(max(-0.04, min(0.06, rng.gauss(0.01, 0.025))), 3),
            'var_use': round(min(1.6, max(0.4, rng.gauss(1.0, 0.3))), 2),
        })
    return refs


def ref_style(ref):
    s = ref['strict']
    return "khắt khe" if s >= 1.15 else ("dễ tính" if s <= 0.85 else "trung bình")


# ----------------------------------------------------------------- hồ sơ từng đội (cố định theo tên)
def team_profile(team):
    """Khí hậu, vùng, sức chứa sân nhà, độ cao... suy từ tên đội (ổn định, không cần lưu)."""
    h = _h('profile', team)
    clim = CLIMATES[h % 5]
    return {'climate': clim, 'region': (h >> 3) % 6, 'capacity': 12000 + ((h >> 7) % 70) * 1000,
            'altitude': ((h >> 15) % 100) < 8, 'home_adv0': round(0.85 + ((h >> 20) % 31) / 100.0, 2)}   # 0.85 - 1.15


def default_team_ctx(team):
    p = team_profile(team)
    return {'home_adv': p['home_adv0']}


# ----------------------------------------------------------------- sinh ctx
def weather_for(rng, host, month):
    prof = team_profile(host)
    season = 0 if month in (12, 1, 2) else 1 if month in (3, 4, 5) else 2 if month in (6, 7, 8) else 3
    table = _W[prof['climate']][season]
    w = rng.choices(list(table), list(table.values()))[0]
    temp = _TEMP_BY_W[w] + (_TEMP[prof['climate']] - 20) * 0.5 + rng.gauss(0, 2)
    return w, int(round(temp))


def pitch_for(rng, weather, importance):
    p_poor = 0.07 + (0.12 if weather == 'rain' else 0.0) - 0.05 * importance
    p_fair = 0.30
    x = rng.random()
    return 'poor' if x < p_poor else ('fair' if x < p_poor + p_fair else 'good')


def crowd_heat(rng, host, importance, rivalry, neutral):
    cap = team_profile(host)['capacity']
    base = 0.25 + 0.35 * (cap - 12000) / 70000.0
    v = base + 0.25 * importance + 0.15 * rivalry + rng.gauss(0, 0.07)
    return round(max(0.0, min(1.0, v)), 2)


def fatigue_of(team, opp, match_date, last_match, away, host_region_gap=0):
    """Mệt mỏi dồn 0..1: quãng nghỉ từ trận trước, hiệp phụ, di chuyển.
    last_match: {'date': iso, 'min': 90|120, 'away': bool} hoặc None. match_date: datetime.date."""
    f = 0.0
    if last_match:
        import datetime
        days = (match_date - datetime.date.fromisoformat(last_match['date'])).days
        if days < 0:
            days = 0
        f += max(0.0, (9 - days) / 9.0) * 0.55
        if last_match.get('min', 90) > 90:
            f += 0.20 * max(0.0, (10 - days) / 10.0)
        if last_match.get('away') and days <= 5:
            f += 0.05
    f += 0.04 * host_region_gap if away else 0.0
    return round(max(0.0, min(1.0, f)), 2)


def make_ctx(seed, home, away, month, importance=0.3, neutral=False, host=None, rivalry=0.0, refs=None,
             team_ctx=None, match_date=None, last_match=None, ko=False):
    """Sinh ctx của một trận. seed: số nguyên (ví dụ crc32 của mùa+id trận). host: nước chủ nhà sân trung lập."""
    rng = random.Random(seed)
    venue = host or home
    weather, temp = weather_for(rng, venue, month)
    pitch = pitch_for(rng, weather, importance)
    crowd = crowd_heat(rng, venue, importance, rivalry, neutral)
    ref = rng.choice(refs)['id'] if refs else 0
    tc = team_ctx or {}
    hadv = 0.0 if neutral else float((tc.get(home) or default_team_ctx(home))['home_adv'])
    pv, pa = team_profile(venue), team_profile(away)
    gap = 0
    fat = {}
    for side, t, is_away in (('h', home, False), ('a', away, True)):
        lm = (last_match or {}).get(t)
        g = 0
        if neutral or is_away:
            other = team_profile(t)
            g = min(abs(pv['region'] - other['region']), 6 - abs(pv['region'] - other['region']))
        fat[side] = fatigue_of(t, None, match_date, lm, is_away or neutral, g) if match_date else 0.0
    alt = bool(pv['altitude'] and not neutral and not pa['altitude'])
    if alt:
        fat['a'] = round(min(1.0, fat['a'] + 0.12), 2)
    return {'weather': weather, 'temp': temp, 'pitch': pitch, 'crowd': crowd, 'ref': ref, 'fatigue': fat,
            'altitude': alt, 'importance': round(importance, 2), 'hadv': round(hadv, 2)}


# ----------------------------------------------------------------- tác động lên engine
def effects(ctx, refs=None, neutral=False, tech=(0.0, 0.0), base_ha=(1.07, 0.95)):
    """Gộp bối cảnh thành hệ số cho engine. tech[i] trong [-1, 1]: mức thiên về kỹ thuật của đội i (poss_q cao)."""
    if not ctx:
        return None
    w, pitch, crowd = ctx['weather'], ctx['pitch'], ctx['crowd']
    ref = None
    if refs:
        ref = next((r for r in refs if r['id'] == ctx['ref']), refs[0])
    fat = ctx.get('fatigue', {'h': 0.0, 'a': 0.0})
    E = EFFECT
    att, dfn = [], []
    for i in (0, 1):
        side = 'h' if i == 0 else 'a'
        ea, ed = [], []
        if i == 0 and not neutral:
            ea.append(E['home_att'] * (crowd - 0.5))
            ed.append(E['home_def'] * (crowd - 0.5))
        if i == 1 and not neutral:
            ea.append(-E['crowd_pressure_away'] * crowd)
        ea.append(E['fatigue_att'] * fat.get(side, 0.0))
        ed.append(E['fatigue_def'] * fat.get(side, 0.0))
        if w == 'hot':
            ea.append(E['hot_att'])
        if w == 'rain':
            ed.append(E['rain_def'])
        if pitch == 'poor':
            ea.append(E['poor_pitch_tech'] * max(-1.0, min(1.0, tech[i])))
        elif pitch == 'fair':
            ea.append(0.4 * E['poor_pitch_tech'] * max(-1.0, min(1.0, tech[i])))
        att.append(capped(*ea))
        dfn.append(capped(*ed))
    # lợi thế sân nhà (số cú sút): hệ số riêng từng đội, scale theo khán đài
    if neutral:
        ha = (1.0, 1.0)
    else:
        k = ctx.get('hadv', 1.0) * (0.7 + 0.6 * crowd)
        ha = (1.0 + (base_ha[0] - 1.0) * k, 1.0 - (1.0 - base_ha[1]) * k)
    strict = ref['strict'] if ref else 1.0
    hb = ref['home_bias'] if ref else 0.0
    press = (0.5 + crowd) * (0.0 if neutral else 1.0)
    out = {
        'att': att, 'dfn': dfn, 'ha': ha,
        'shots': {'rain': 0.97, 'wind': 0.98, 'hot': 0.985}.get(w, 1.0),
        'drain': (1.30 if w == 'hot' else 0.95 if w == 'cold' else 1.0) * (1.10 if ctx.get('altitude') else 1.0),
        'poss_scale': {'poor': 1.18, 'fair': 1.06}.get(pitch, 1.0),
        'pass_acc': {'rain': -0.030, 'wind': -0.012}.get(w, 0.0) + {'poor': -0.030, 'fair': -0.010}.get(pitch, 0.0),
        'yellow': [strict * (1.0 - hb * press), strict * (1.0 + hb * press)],
        'red': [strict * (1.0 - hb * press), strict * (1.0 + hb * press)],
        'foul': strict, 'pen': 1.0 + 0.5 * (strict - 1.0) + hb * press,
        'var_use': ref['var_use'] if ref else 1.0,
        'injury': [1.0 + 0.6 * fat.get('h', 0.0) + (0.1 if w == 'hot' else 0.0),
                   1.0 + 0.6 * fat.get('a', 0.0) + (0.1 if w == 'hot' else 0.0)],
        'fit_pen': [8.0 * fat.get('h', 0.0), 8.0 * fat.get('a', 0.0)],      # điểm thể lực trừ vào đầu trận
    }
    return out


def effects_from_teams(ctx, refs, neutral, A=None, B=None, base_ha=(1.07, 0.95)):
    """Như effects() nhưng tự tính 'tech' (thiên về kỹ thuật) từ hai đội đã chuẩn bị (prepare_team)."""
    tech = [0.0, 0.0]
    for i, P in enumerate((A, B)):
        if P:
            xs = [e['poss_q'] for e in P['xi'] if e['group'] != 'GK']
            ph = [0.5 * e['pace'] + 0.5 * e['phy'] if 'pace' in e else 60 for e in P['xi'] if e['group'] != 'GK']
            if xs and ph:
                tech[i] = (sum(xs) / len(xs) - sum(ph) / len(ph)) / 15.0
    return effects(ctx, refs, neutral, tuple(tech), base_ha)


# ----------------------------------------------------------------- hiển thị
def describe(ctx, refs=None):
    """Dòng thông tin trận: 'Nắng nóng 33 độ - Mặt sân tốt - Khán đài sôi động - Trọng tài X (khắt khe)'."""
    if not ctx:
        return ""
    w = WEATHER_TXT[ctx['weather']]
    if ctx['weather'] in ('hot', 'cold'):
        w += " %d°C" % ctx['temp']
    c = ctx['crowd']
    crowd = "Khán đài sôi động" if c >= 0.7 else ("Khán đài khá đông" if c >= 0.45 else "Khán đài vắng")
    parts = [w, PITCH_TXT[ctx['pitch']], crowd]
    if refs:
        ref = next((r for r in refs if r['id'] == ctx['ref']), None)
        if ref:
            parts.append("Trọng tài %s (%s)" % (ref['name'], ref_style(ref)))
    fat = ctx.get('fatigue', {})
    if max(fat.get('h', 0), fat.get('a', 0)) >= 0.35:
        parts.append("Lịch dày: đội %s mệt mỏi" % ("chủ nhà" if fat.get('h', 0) >= fat.get('a', 0) else "khách"))
    if ctx.get('altitude'):
        parts.append("Sân ở độ cao lớn")
    return " - ".join(parts)


def update_home_adv(team_ctx, home, result, expected):
    """Lợi thế sân nhà đổi chậm theo kết quả thực (result 1/0.5/0, expected: xác suất kỳ vọng của chủ nhà)."""
    d = team_ctx.setdefault(home, default_team_ctx(home))
    d['home_adv'] = round(max(0.7, min(1.3, d['home_adv'] + 0.012 * (result - expected))), 3)


_VAR_WHY = {'offside': "việt vị", 'foul': "lỗi trước tình huống ghi bàn", 'handball': "chạm tay"}


def var_text(v, name):
    """Mô tả ngắn một tình huống VAR (v: phần tử của r['var'])."""
    k = v['k']
    if k == 'goal_overturned':
        return "Bàn thắng của %s bị huỷ (%s)" % (name, _VAR_WHY.get(v.get('why'), "vi phạm"))
    if k == 'penalty_awarded':
        return "Phạt đền cho đội của %s được cho sau khi xem VAR" % name
    if k == 'penalty_cancelled':
        return "Quyết định phạt đền bị huỷ sau khi xem VAR"
    if k == 'red_overturned':
        return "Thẻ đỏ của %s bị rút sau khi xem VAR" % name
    if k == 'red_upgraded':
        return "Thẻ vàng của %s được nâng thành thẻ đỏ sau khi xem VAR" % name
    w = v.get('what')
    if w == 'goal':
        return "Bàn thắng của %s được xem lại, giữ nguyên" % name
    if w == 'red':
        return "VAR xem lại pha vào bóng của %s, không có thẻ đỏ" % name
    return "VAR xem lại tình huống phạt đền, không có phạt đền"
