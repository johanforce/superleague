#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WNL ENGINE - bộ máy mô phỏng trận đấu (tách riêng khỏi wnl_game.py)
=====================================================================
File này KHÔNG biết gì về giải đấu, bảng xếp hạng hay giao diện. Nó chỉ làm 3 việc:

  1. make_player()      : biến 1 dòng CSV thành cầu thủ với các chỉ số tổng hợp (att, fin_q, ast_q, ...)
  2. prepare_team() +
     simulate()         : chọn đội hình, mô phỏng 1 trận theo từng quãng 15 phút, trả về kết quả chi tiết
  3. các hàm "động"     : cập nhật PHONG ĐỘ, THỂ LỰC, ĐÀ (momentum) của cầu thủ / đội sau mỗi trận

MÔ HÌNH TRẬN ĐẤU (tóm tắt)
--------------------------
Mỗi trận chia 6 quãng x 15' (+2 quãng hiệp phụ). Ở mỗi quãng, với mỗi đội:

    số cú sút ~ Poisson( base_shots/6
                         * exp(k_att * tanh((ATT_nhà*mệt - DEF_khách*mệt) / sat))   # chênh lệch tấn công - phòng ngự
                         * (tỷ lệ kiểm soát bóng / 0.5) ^ k_poss                    # kiểm soát bóng
                         * sân nhà * thế trận * đà thắng * hiệu ứng tỷ số * hiệu ứng thẻ đỏ )

    mỗi cú sút: chọn người sút (chỉ số dứt điểm, vị trí) -> có trúng đích không -> thủ môn có cản được không
      P(trúng đích) = p_sot_base + p_sot_fin*(fin_q-70) - p_sot_def*(DEF_khách-70)
      P(vào | trúng) = p_goal_base * exp(k_fin_gk * (fin_q - (gk + gk_offset)))

Các yếu tố động (lưu trong cache, cập nhật sau mỗi trận):
  * PHONG ĐỘ cầu thủ  form in [-1, 1] : nhân vào mọi chỉ số (+-form_weight). Lên khi ghi bàn/kiến tạo/giữ sạch lưới.
  * THỂ LỰC cầu thủ   fit in [0, 100] : giảm theo số phút thi đấu & chỉ số sức bền, hồi phục theo số ngày nghỉ.
  * ĐÀ (momentum) đội mom in [-1, 1]  : thắng bất ngờ -> tăng, thua bất ngờ -> giảm; ảnh hưởng nhẹ số cú sút.
  * Ăn ý (chemistry)  : nhiều cầu thủ cùng CLB trong đội hình xuất phát -> cộng điểm chuyền/tấn công.
  * Thế trận          : đội mạnh hơn rõ -> tấn công, đội yếu hơn rõ -> phòng ngự; thua thì dồn ép, dẫn thì lùi.
  * Mệt mỏi trong trận: năng lượng giảm dần theo phút (sức bền thấp mệt nhanh hơn), thay người "tươi" hơn.
  * Trọng tài         : độ nghiêm khắc ngẫu nhiên mỗi trận -> số thẻ; chỉ số hung hãn (aggression) -> dễ nhận thẻ.
  * Playstyles        : các "lối chơi" của cầu thủ trong CSV (Finesse Shot, Incisive Pass...) cộng điểm nhỏ.

Mọi hằng số nằm trong dict CFG bên dưới - chỉnh trực tiếp để thay đổi cân bằng trận đấu.
Chạy `python wnl_engine.py` để in bảng hiệu chỉnh (cần wnl_game.py + 2 file CSV cùng thư mục).
"""
import math
import random
import datetime
from collections import defaultdict

from . import context as wnl_context
from . import coach as wnl_coach

# =====================================================================
#  THAM SỐ (CFG)
# =====================================================================
CFG = {
    # --- cấu trúc trận ---
    'segments': 6,             # số quãng 90 phút (mỗi quãng 15')
    'et_segments': 2,          # số quãng hiệp phụ
    'et_tempo': 0.90,          # nhịp độ hiệp phụ
    # --- tần suất sút ---
    'base_shots': 12.8,        # số cú sút TB của 1 đội / 90' khi hai đội ngang nhau
    'k_att': 0.34,             # độ nhạy của chênh lệch ATT - DEF (đã bão hoà bằng tanh)
    'sat': 24.0,               # thang bão hoà của chênh lệch
    'k_poss': 0.25,            # ảnh hưởng của kiểm soát bóng lên số cú sút
    'poss_scale': 24.0,        # độ nhạy chênh lệch POSS -> % kiểm soát bóng
    'home_adv': (1.07, 0.95),  # nhân số cú sút (chủ nhà, khách) ở sân không trung lập
    # --- chất lượng cú sút ---
    'p_sot_base': 0.34, 'p_sot_fin': 0.002, 'p_sot_def': 0.0015,
    'p_goal_base': 0.31, 'k_fin_gk': 0.012, 'gk_offset': 3.0,
    'p_goal_range': (0.05, 0.62),
    'pen_share': 0.014,       # tỷ lệ cú sút là phạt đền
    'assist_prob': 0.78,
    # --- yếu tố động ---
    'form_weight': 0.06,       # phong độ +-1 => chỉ số +-6%
    'fit_weight': 0.08,        # thể lực 50% => chỉ số giảm 8%
    'mom_weight': 0.04,        # momentum +-1 => số cú sút +-4%
    'recent_form_weight': 0.035,# form 8 trận +-1 => số cú sút +-3.5%
    'venue_form_weight': 0.015, # form sân nhà/sân khách +-1 => số cú sút +-1.5%
    'form_window': 8,           # số trận gần nhất dùng cho phong độ đội
    'venue_form_window': 5,     # số trận gần nhất cùng sân nhà/sân khách
    'chem_points': 3.0,        # ăn ý tối đa cộng bao nhiêu điểm vào ATT/POSS
    'fatigue_weight': 0.45,    # mệt mỏi trong trận
    'form_att_pts': 1.2,       # mỗi tiền đạo thêm (so với 2.5) cộng điểm ATT
    'form_def_pts': 1.0,       # mỗi hậu vệ thêm (so với 4) cộng điểm DEF
    # --- thế trận ---
    'mentality': {'attack': (1.07, 1.06), 'balanced': (1.0, 1.0), 'defend': (0.88, 0.90)},
    'attack_gap': 7.0, 'defend_gap': -9.0,
    'trail_boost': 0.12, 'lead_ease': 0.08,
    # --- thẻ phạt / chấn thương ---
    'yellow_rate': 1.7, 'p_red_direct': 0.022, 'p_injury': 0.22,
    'ref_sigma': 0.25,
    # --- cập nhật phong độ / thể lực / đà ---
    'fit_loss': 17.0, 'fit_loss_sta': 9.0, 'fit_recover_days': 5.0,
    'form_halflife_days': 60.0, 'mom_halflife_days': 90.0,
    'form_alpha': 0.50,
    # --- HLV AI: cửa sổ quyết định + giới hạn thay người ---
    'win': 5,                  # độ dài cửa sổ quyết định (phút); thêm điểm ngắt sau bàn thắng / thẻ đỏ / chấn thương / hết hiệp
    'max_subs': 5, 'max_stops': 3, 'et_extra_subs': 1, 'et_extra_stops': 1,
    # --- VAR (xác suất nhân với var_use của trọng tài) ---
    'var': {'goal_overturn': 0.045,      # bàn thắng bị huỷ (việt vị / lỗi trước bàn / chạm tay) trên mỗi bàn
            'goal_check': 0.040,         # xem lại nhưng giữ nguyên bàn thắng
            'pen_var': 0.25,             # phạt đền được cho SAU khi xem video (trên mỗi quả phạt đền thật)
            'pen_cancel': 0.10,          # chỉ chấm phạt đền rồi huỷ (trên mỗi tình huống phạt đền)
            'red_overturn': 0.12,        # thẻ đỏ trực tiếp bị rút
            'yellow_upgrade': 0.005,     # thẻ vàng bị nâng thành đỏ
            'extra_check': 0.12},        # số lần xem lại không đổi quyết định / trận
}

POS_GROUP = {'GK': 'GK', 'CB': 'DEF', 'LB': 'DEF', 'RB': 'DEF', 'CDM': 'MID', 'CM': 'MID',
             'CAM': 'MID', 'LM': 'MID', 'RM': 'MID', 'LW': 'FWD', 'RW': 'FWD', 'ST': 'FWD'}
TARGET_COUNT = {'GK': 3, 'DEF': 7, 'MID': 7, 'FWD': 6}
DEFAULT_POS = {'GK': 'GK', 'DEF': 'CB', 'MID': 'CM', 'FWD': 'ST'}
FORMATIONS = [(4, 3, 3), (4, 4, 2), (3, 5, 2), (4, 5, 1), (5, 3, 2)]
GOAL_POS_W = {'FWD': 1.0, 'MID': 0.32, 'DEF': 0.07, 'GK': 0.0}
ASSIST_POS_W = {'FWD': 0.7, 'MID': 1.0, 'DEF': 0.35, 'GK': 0.0}
YELLOW_W = {'DEF': 1.0, 'MID': 1.1, 'FWD': 0.6, 'GK': 0.12}
INJURIES = [(0.55, "Căng cơ đùi", (1, 2)), (0.25, "Bong gân mắt cá", (2, 3)),
            (0.12, "Chấn thương đầu gối", (3, 5)), (0.06, "Rách cơ", (4, 6)),
            (0.02, "Đứt dây chằng chéo", (7, 12))]
REF_DATE = datetime.date(2026, 10, 7)

# playstyle (CSV cột "playstyles") -> chỉ số tổng hợp được cộng điểm. Bản "+" cộng gấp đôi.
STYLE_KEYS = {
    'Finesse Shot': 'fin_q', 'Power Shot': 'fin_q', 'Chip Shot': 'fin_q', 'Low Driven Shot': 'fin_q',
    'Acrobatic': 'fin_q', 'Power Header': 'fin_q', 'Dead Ball': 'fin_q',
    'Incisive Pass': 'ast_q', 'Pinged Pass': 'ast_q', 'Whipped Pass': 'ast_q', 'Long Ball Pass': 'ast_q',
    'Trivela': 'ast_q', 'Flair': 'ast_q',
    'Tiki Taka': 'poss_q', 'First Touch': 'poss_q', 'Technical': 'poss_q', 'Press Proven': 'poss_q',
    'Intercept': 'dfn', 'Anticipate': 'dfn', 'Block': 'dfn', 'Jockey': 'dfn', 'Slide Tackle': 'dfn',
    'Tackle': 'dfn', 'Bruiser': 'dfn', 'Aerial': 'aer', 'Rapid': 'spd', 'Quick Step': 'spd',
    'Relentless': 'sta', 'Far Reach': 'gk', 'Footwork': 'gk', 'Cross Claimer': 'gk', 'Rush Out': 'gk',
}
STYLE_BONUS = 2.0
COMP_KEYS = ('fin_q', 'ast_q', 'att', 'poss_q', 'dfn', 'gk', 'aer', 'spd', 'ovr')


# =====================================================================
#  CẦU THỦ
# =====================================================================
def to_int(v, d=50):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _age(birth):
    try:
        d, m, y = [int(x) for x in str(birth).split('-')]
        return round((REF_DATE - datetime.date(y, m, d)).days / 365.25, 1)
    except Exception:
        return 26.0


def derive(p):
    """Tính các chỉ số tổng hợp từ chỉ số gốc."""
    p['pas_all'] = 0.50 * p['pas'] + 0.25 * p['shp'] + 0.25 * p['lgp']
    p['aer'] = 0.4 * p['hea'] + 0.3 * p['jmp'] + 0.3 * p['str']
    p['spd'] = (2 * p['pace'] + p['acc'] + p['spr']) / 4.0
    p['fin_q'] = (0.32 * p['fin'] + 0.18 * p['sho'] + 0.14 * p['pos_'] + 0.14 * p['cmp'] +
                  0.10 * p['pwr'] + 0.06 * p['lsh'] + 0.06 * p['rea'])
    p['att'] = (0.24 * p['fin'] + 0.16 * p['sho'] + 0.12 * p['pos_'] + 0.14 * p['dri'] + 0.08 * p['pas_all'] +
                0.12 * p['spd'] + 0.08 * p['rea'] + 0.06 * p['hea'])
    p['ast_q'] = (0.32 * p['pas_all'] + 0.26 * p['vis'] + 0.14 * p['cro'] + 0.12 * p['dri'] +
                  0.10 * p['ctl'] + 0.06 * p['cmp'])
    p['poss_q'] = (0.28 * p['ctl'] + 0.28 * p['pas_all'] + 0.18 * p['vis'] + 0.10 * p['cmp'] +
                   0.10 * p['dri'] + 0.06 * p['rea'])
    p['dfn'] = (0.30 * p['def'] + 0.18 * p['awr'] + 0.16 * p['tkl'] + 0.08 * p['icp'] + 0.08 * p['phy'] +
                0.08 * p['aer'] + 0.06 * p['str'] + 0.06 * p['rea'])
    p['gk'] = 0.24 * p['div'] + 0.24 * p['han'] + 0.28 * p['grf'] + 0.18 * p['gkp'] + 0.06 * p['gkk']
    for s in p.get('styles', ()):
        plus = s.endswith('+')
        key = STYLE_KEYS.get(s.rstrip('+').strip())
        if key and key in p:
            p[key] += STYLE_BONUS * (2 if plus else 1)
    return p


def flat_player(pid, name, pos, group, base):
    """Cầu thủ 'trơn' (mọi chỉ số = base) dùng cho cầu thủ default / thủ môn bổ sung."""
    keys = ('fin', 'sho', 'pos_', 'pas', 'shp', 'lgp', 'vis', 'cro', 'dri', 'pace', 'acc', 'spr', 'ctl', 'def',
            'awr', 'tkl', 'icp', 'phy', 'div', 'han', 'grf', 'gkp', 'gkk', 'pen', 'cmp', 'rea', 'hea', 'lsh',
            'str', 'jmp', 'pwr', 'sta', 'agg')
    p = {k: base for k in keys}
    p.update({'pid': pid, 'name': name, 'pos': pos, 'group': group, 'ovr': base, 'club': '', 'age': 27.0,
              'styles': (), 'synthetic': True})
    return derive(p)


def make_player(r):
    pos = r['position']
    name = (r.get('common_name') or '').strip() or ("%s %s" % (r.get('first_name', ''), r.get('last_name', ''))).strip()
    g = lambda k: to_int(r.get(k))
    p = {'pid': r['player_id'], 'name': name, 'pos': pos, 'group': POS_GROUP.get(pos, 'MID'),
         'ovr': g('overall_rating'), 'club': r.get('club') or '', 'age': _age(r.get('birthdate')),
         'styles': tuple(x.strip() for x in (r.get('playstyles') or '').split(',') if x.strip()),
         'fin': g('attacking_finishing'), 'sho': g('shooting'), 'pos_': g('mentality_positioning'),
         'pas': g('passing'), 'shp': g('attacking_short_passing'), 'lgp': g('skill_long_passing'),
         'vis': g('mentality_vision'), 'cro': g('attacking_crossing'), 'dri': g('dribbling'),
         'pace': g('pace'), 'acc': g('movement_acceleration'), 'spr': g('movement_sprint_speed'),
         'ctl': g('skill_ball_control'), 'def': g('defending'), 'awr': g('defending_awareness'),
         'tkl': g('defending_standing_tackle'), 'icp': g('mentality_interceptions'), 'phy': g('physicality'),
         'div': g('goalkeeping_diving'), 'han': g('goalkeeping_handling'), 'grf': g('goalkeeping_reflexes'),
         'gkp': g('goalkeeping_positioning'), 'gkk': g('goalkeeping_kicking'), 'pen': g('mentality_penalties'),
         'cmp': g('mentality_composure'), 'rea': g('movement_reactions'), 'hea': g('attacking_heading_accuracy'),
         'lsh': g('power_long_shots'), 'str': g('power_strength'), 'jmp': g('power_jumping'),
         'pwr': g('power_shot_power'), 'sta': g('power_stamina'), 'agg': g('mentality_aggression'),
         'synthetic': False}
    return derive(p)


# =====================================================================
#  TIỆN ÍCH
# =====================================================================
def poisson(lam):
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= random.random()
        if p <= limit:
            return k
        k += 1


def mean(xs, default=50.0):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else default


def clip(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)



def recent_form_score(results, window=None):
    """Điểm phong độ đội trong [-1, 1], ưu tiên trận mới hơn.

    ``results`` là chuỗi W/D/L theo thứ tự cũ -> mới. 0 tương ứng trung bình
    1.5 điểm/trận; W liên tiếp đẩy lên nhẹ, L liên tiếp kéo xuống nhẹ.
    Hàm thuần để dùng cho test và không phụ thuộc trạng thái mùa giải.
    """
    n = int(window or CFG.get('form_window', 8))
    seq = [str(x).upper() for x in list(results or [])[-max(1, n):] if str(x).upper() in ('W', 'D', 'L')]
    if not seq:
        return 0.0
    value = {'W': 1.0, 'D': 0.5, 'L': 0.0}
    # Trận mới nhất có trọng số lớn hơn nhưng không lấn át toàn bộ cửa sổ.
    weights = [0.82 ** (len(seq) - 1 - i) for i in range(len(seq))]
    points = sum(value[r] * w for r, w in zip(seq, weights)) / sum(weights)
    score = (points - 0.5) * 2.0
    # Chuỗi 3+ trận chỉ tạo bonus nhỏ; cap để form không thắng chất lượng đội hình.
    tail = seq[-4:]
    streak = 0.0
    if len(tail) >= 3 and all(x == 'W' for x in tail[-3:]):
        streak = 0.08 + (0.04 if len(tail) == 4 and tail[-4] == 'W' else 0.0)
    elif len(tail) >= 3 and all(x == 'L' for x in tail[-3:]):
        streak = -0.08 - (0.04 if len(tail) == 4 and tail[-4] == 'L' else 0.0)
    return clip(score + streak, -1.0, 1.0)


def team_form_multiplier(P):
    """Hệ số cơ hội từ momentum + recent form + venue form, luôn bị cap +-10%."""
    raw = (1 + CFG['mom_weight'] * float(P.get('mom', 0.0))
           + CFG['recent_form_weight'] * float(P.get('recent_form', 0.0))
           + CFG['venue_form_weight'] * float(P.get('venue_form', 0.0)))
    return clip(raw, 0.90, 1.10)


def team_strength(P):
    """Chỉ số ATT/POSS/DEF tiền trận từ XI đã chọn, chưa tính bối cảnh."""
    xi = list(P.get('xi') or [])
    if not xi:
        return {'attack': 50.0, 'control': 50.0, 'defence': 50.0, 'gk': 50.0}
    gks = [e for e in xi if e.get('group') == 'GK']
    gk_q = gks[0]['gk'] if gks else 28.0
    rest = [e for e in xi if e not in gks[:1]]
    d = [e for e in rest if e.get('group') == 'DEF']
    m = [e for e in rest if e.get('group') == 'MID']
    f = [e for e in rest if e.get('group') == 'FWD']
    chem = float(P.get('chem', 0.0)) * CFG['chem_points']
    att = (0.45 * mean(e['att'] for e in f) + 0.35 * mean(0.5 * e['att'] + 0.5 * e['ast_q'] for e in m) +
           0.20 * mean(0.6 * e['ast_q'] + 0.4 * e['spd'] for e in d) +
           CFG['form_att_pts'] * (len(f) - 2.5) + chem)
    poss = 0.55 * mean(e['poss_q'] for e in m) + 0.25 * mean(e['poss_q'] for e in d) + 0.20 * mean(e['poss_q'] for e in f) + chem
    dfn = (0.50 * mean(e['dfn'] for e in d) + 0.30 * mean(e['dfn'] for e in m) + 0.20 * gk_q +
           CFG['form_def_pts'] * (len(d) - 4) + 0.5 * chem)
    return {'attack': round(att, 2), 'control': round(poss, 2), 'defence': round(dfn, 2), 'gk': round(gk_q, 2)}

def effective(p, form, fit):
    """Bản sao cầu thủ với chỉ số đã nhân hệ số phong độ + thể lực."""
    mult = (1 + CFG['form_weight'] * form) * (1 - CFG['fit_weight'] * max(0.0, (100 - fit) / 50.0))
    e = dict(p)
    for k in COMP_KEYS:
        e[k] = p[k] * mult
    e['fit0'], e['form'] = fit, form
    return e


# =====================================================================
#  CHỌN ĐỘI HÌNH
# =====================================================================
def select_xi(avail):
    """Trả về (11 cầu thủ [GK đầu tiên], sơ đồ)."""
    gks = [p for p in avail if p['group'] == 'GK']
    gk = max(gks, key=lambda p: p['ovr']) if gks else max(avail, key=lambda p: p['gk'])
    rest = [p for p in avail if p is not gk]
    by = {g: sorted([p for p in rest if p['group'] == g], key=lambda p: -p['ovr']) for g in ('DEF', 'MID', 'FWD')}
    best, best_score, best_f = None, -1, FORMATIONS[0]
    for f in FORMATIONS:
        need = dict(zip(('DEF', 'MID', 'FWD'), f))
        chosen, score = [], 0.0
        for g in ('DEF', 'MID', 'FWD'):
            take = by[g][:need[g]]
            chosen += take
            score += sum(p['ovr'] for p in take)
        short = 10 - len(chosen)
        if short > 0:
            left = sorted([p for p in rest if p not in chosen and p['group'] != 'GK'], key=lambda p: -p['ovr'])[:short]
            chosen += left
            score += sum(p['ovr'] - 12 for p in left)
        if len(chosen) == 10 and score > best_score:
            best, best_score, best_f = chosen, score, f
    if best is None:
        best = sorted(rest, key=lambda p: -p['ovr'])[:10]
    return [gk] + best, best_f


def prepare_team(name, squad, out, dyn_p, tstate, coach=None):
    """
    Chuẩn bị 1 đội cho trận đấu.
      squad : danh sách cầu thủ gốc            out   : set pid đang chấn thương/treo giò
      dyn_p : {pid: (phong độ, thể lực)}      tstate: {'mom': float, ...} trạng thái đội
      coach : hồ sơ HLV (wnl_coach.make_coach) - quyết định thay người / đổi chiến thuật ngay trong trận
    """
    eff_all = {}
    for p in squad:
        f, fit = dyn_p.get(p['pid'], (0.0, 100.0))
        eff_all[p['pid']] = effective(p, f, fit)
    pool = [e for e in eff_all.values() if e['pid'] not in out]
    if len(pool) < 14:      # thiếu người -> buộc phải gọi người đang nghỉ (khẩn cấp)
        pool += sorted([e for e in eff_all.values() if e['pid'] in out], key=lambda e: -e['ovr'])[:14 - len(pool)]
    n_gk = sum(1 for e in pool if e['group'] == 'GK')

    def rest_prob(e):
        if e['group'] == 'GK':
            return 0.08 if n_gk > 1 else 0.0
        return min(0.40, 0.05 + max(0.0, 80 - e['fit0']) / 160.0)      # thể lực thấp -> dễ bị cho nghỉ
    avail = [e for e in pool if random.random() > rest_prob(e)]
    if len(avail) < 14:
        avail = list(pool)
    xi, formation = select_xi(avail)
    bench = [e for e in pool if e not in xi]
    # ăn ý: các cặp cầu thủ cùng CLB trong đội hình xuất phát
    clubs = defaultdict(int)
    for e in xi:
        if e['club']:
            clubs[e['club']] += 1
    pairs = sum(n * (n - 1) // 2 for n in clubs.values())
    last = list(tstate.get('last', []) or [])
    venue_last = list(tstate.get('venue_last', []) or [])
    return {'name': name, 'players': eff_all, 'xi': xi, 'xi_ids': [e['pid'] for e in xi], 'bench': bench,
            'plan': [], 'gk': xi[0]['pid'], 'formation': formation, 'out': set(out),
            'mom': clip(float(tstate.get('mom', 0.0)), -1.0, 1.0),
            'recent_form': recent_form_score(last, CFG['form_window']),
            'venue_form': recent_form_score(venue_last, CFG['venue_form_window']),
            'chem': min(1.0, pairs / 12.0), 'fin': None,
            'coach': coach or wnl_coach.default_coach(), 'drain': 1.0}


# =====================================================================
#  SỰ KIỆN: THẺ PHẠT, CHẤN THƯƠNG, THAY NGƯỜI
# =====================================================================
def roll_injury():
    r, acc = random.random(), 0.0
    for pr, name, (lo, hi) in INJURIES:
        acc += pr
        if r <= acc:
            return name, random.randint(lo, hi)
    return INJURIES[0][1], 1


def on_pitch(P, minute):
    on = list(P['xi_ids'])
    for m, out, inn in P['fin']['subs']:
        if m <= minute and out in on:
            if inn is None:
                on.remove(out)
            else:
                on[on.index(out)] = inn
    return on


# =====================================================================
#  SỨC MẠNH ĐỘI TẠI MỘT THỜI ĐIỂM
# =====================================================================
def energy_at(P, pid, minute):
    e = P['players'][pid]
    start = P['fin']['start'].get(pid, 0)
    drain = 0.30 * (1.4 - e['sta'] / 100.0) * P.get('drain', 1.0)
    return max(0.3, e['fit0'] / 100.0 - max(0, minute - start) / 90.0 * drain)


def lines(P, minute):
    """ATT, POSS, DEF, GK, số người, năng lượng TB của đội tại phút `minute`."""
    ids = on_pitch(P, minute)
    pl = [P['players'][i] for i in ids]
    gk = [e for e in pl if e['pid'] == P['gk'] or e['group'] == 'GK']
    gk_q = gk[0]['gk'] if gk else 28.0
    rest = [e for e in pl if e not in gk]
    d = [e for e in rest if e['group'] == 'DEF']
    m = [e for e in rest if e['group'] == 'MID']
    f = [e for e in rest if e['group'] == 'FWD']
    chem = P['chem'] * CFG['chem_points']
    att = (0.45 * mean(e['att'] for e in f) + 0.35 * mean(0.5 * e['att'] + 0.5 * e['ast_q'] for e in m) +
           0.20 * mean(0.6 * e['ast_q'] + 0.4 * e['spd'] for e in d) +
           CFG['form_att_pts'] * (len(f) - 2.5) + chem)
    poss = 0.55 * mean(e['poss_q'] for e in m) + 0.25 * mean(e['poss_q'] for e in d) + \
        0.20 * mean(e['poss_q'] for e in f) + chem
    dfn = (0.50 * mean(e['dfn'] for e in d) + 0.30 * mean(e['dfn'] for e in m) + 0.20 * gk_q +
           CFG['form_def_pts'] * (len(d) - 4) + 0.5 * chem)
    energy = mean((energy_at(P, i, minute) for i in ids), 1.0)
    return {'att': att, 'poss': poss, 'def': dfn, 'gk': gk_q, 'n': len(ids), 'energy': energy, 'ids': ids}


def decide_mentality(a, b):
    ov = lambda L: (L['att'] + L['def']) / 2.0
    d = ov(a) - ov(b)
    if d > CFG['attack_gap']:
        return 'attack'
    if d < CFG['defend_gap']:
        return 'defend'
    return 'balanced'


# =====================================================================
#  MÔ PHỎNG TRẬN
# =====================================================================
def _pick_shooter(P, minute):
    pl = [P['players'][i] for i in on_pitch(P, minute) if P['players'][i]['group'] != 'GK' or i != P['gk']]
    pl = [e for e in pl if e['pid'] != P['gk']] or pl
    ws = [GOAL_POS_W[e['group']] * (e['fin_q'] / 70.0) ** 4 + 1e-6 for e in pl]
    return random.choices(pl, ws)[0]


def _pick_penalty_taker(P, minute):
    pl = [P['players'][i] for i in on_pitch(P, minute) if i != P['gk']]
    ws = [(e['pen'] / 70.0) ** 4 * (e['fin_q'] / 70.0) ** 2 + 1e-6 for e in pl]
    return random.choices(pl, ws)[0]


def _pick_assist(P, minute, scorer_pid):
    c = [P['players'][i] for i in on_pitch(P, minute) if i != scorer_pid and i != P['gk']]
    if not c:
        return None
    ws = [ASSIST_POS_W[e['group']] * (e['ast_q'] / 70.0) ** 3 + 1e-6 for e in c]
    return random.choices(c, ws)[0]['pid']


def _shootout(A, B):
    def kickers(P):
        pl = [P['players'][i] for i in on_pitch(P, 120) if i != P['gk'] or P['players'][i]['group'] == 'GK']
        pl = [e for e in pl if e['group'] != 'GK'] or pl
        return sorted(pl, key=lambda e: -(0.7 * e['pen'] + 0.3 * e['cmp']))

    def gk_of(P):
        g = [P['players'][i] for i in on_pitch(P, 120) if P['players'][i]['group'] == 'GK']
        return g[0]['gk'] if g else 30.0
    ka, kb = kickers(A), kickers(B)
    ga, gb = gk_of(A), gk_of(B)

    def p_score(k, gk):
        return clip(0.76 + (0.7 * k['pen'] + 0.3 * k['cmp'] - 70) / 250.0 - (gk - 60) / 500.0, 0.55, 0.92)
    sa = sb = 0
    for i in range(5):
        sa += random.random() < p_score(ka[i % len(ka)], gb)
        sb += random.random() < p_score(kb[i % len(kb)], ga)
    i = 5
    while sa == sb:
        sa += random.random() < p_score(ka[i % len(ka)], gb)
        sb += random.random() < p_score(kb[i % len(kb)], ga)
        i += 1
        if i > 60:
            sa += 1
    return sa, sb


def _new_fin(P):
    P['fin'] = {'subs': [], 'cards': [], 'injs': [], 'start': {pid: 0 for pid in P['xi_ids']}, 'end': {},
                'tactics': [], 'ments': [], 'why': {}}


def simulate(A, B, neutral=False, knockout=False, ctx=None, refs=None, aggregate_before=None):
    """
    Mô phỏng 1 trận giữa 2 đội đã chuẩn bị (prepare_team). A = chủ nhà, B = khách.
    Trận chạy theo từng CỬA SỔ ~5 phút (cắt ngắn hơn khi có bàn thắng / thẻ / chấn thương, và ở các lần nghỉ hiệp).
    Tại mỗi cửa sổ HLV mỗi đội (wnl_coach.decide) xem diễn biến rồi quyết định thay người / đổi thế trận.
      ctx  : bối cảnh trận (wnl_context.make_ctx): thời tiết, mặt sân, khán đài, trọng tài, mệt mỏi... (None = không có)
      refs : danh sách trọng tài (state['refs']) để tra hồ sơ trọng tài trong ctx
    Trả về dict kết quả chi tiết (tỷ số, bàn thắng, thẻ, chấn thương, thay người + lý do, đổi chiến thuật, VAR, ...).
    VAR được quyết định NGAY TẠI ĐÂY (bàn bị huỷ không vào tỷ số / danh sách ghi bàn; vẫn là một cú sút trúng đích).
    """
    teams = (A, B)
    aggregate_before = aggregate_before or (0, 0)
    VC = CFG['var']
    eff = wnl_context.effects_from_teams(ctx, refs, neutral, A, B, CFG['home_adv']) if ctx else None
    if eff:
        ym, rm, im, pen_m, vu = eff['yellow'], eff['red'], eff['injury'], eff['pen'], eff['var_use']
        ha, ma, md, shots_m = eff['ha'], eff['att'], eff['dfn'], eff['shots']
        poss_scale = CFG['poss_scale'] * eff['poss_scale']
        for i, P in enumerate(teams):
            P['drain'] = eff['drain']
            fp = eff['fit_pen'][i]
            if fp > 0:
                for e in P['players'].values():
                    e['fit0'] = max(20.0, e['fit0'] - fp)
    else:
        ref = math.exp(random.gauss(0, CFG['ref_sigma']))
        ym = rm = [ref, ref]
        im, pen_m, vu = [1.0, 1.0], 1.0, 1.0
        ha = (1.0, 1.0) if neutral else CFG['home_adv']
        ma = md = (1.0, 1.0)
        shots_m, poss_scale = 1.0, CFG['poss_scale']
    for P in teams:
        _new_fin(P)
    l0 = [lines(A, 0), lines(B, 0)]
    ment0 = [decide_mentality(l0[0], l0[1]), decide_mentality(l0[1], l0[0])]

    def draw_events(i):
        out = [(random.randint(1, 90), 'y') for _ in range(poisson(CFG['yellow_rate'] * ym[i]))]
        if random.random() < CFG['p_red_direct'] * rm[i]:
            out.append((random.randint(1, 90), 'r'))
        if random.random() < CFG['p_injury'] * im[i]:
            out.append((random.randint(1, 90), 'i'))
        return sorted(out)

    S = [{'on': list(P['xi_ids']), 'used': set(), 'gone': set(), 'nsubs': 0, 'stops': set(), 'yc': defaultdict(int),
          'sched': draw_events(i), 'cs': wnl_coach.new_state(P['coach'], random), 'ment': ment0[i], 'ment0': ment0[i],
          'trig': set(), 'last_eval': -99.0, 'bench_ids': {e['pid'] for e in P['bench']}}
         for i, P in enumerate(teams)]
    state = {'et': False}
    goals, score = [], [0, 0]
    stat = {'shots': [0, 0], 'sot': [0, 0], 'xg': [0.0, 0.0], 'poss': [0.0, 0.0], 'dur': 0.0}
    shot_log, seg_share, var_events = [], [], []
    sub_limit = lambda: CFG['max_subs'] + (CFG['et_extra_subs'] if state['et'] else 0)
    stop_limit = lambda: CFG['max_stops'] + (CFG['et_extra_stops'] if state['et'] else 0)

    # ------------------------------------------------------------------ thay người / rời sân
    def do_sub(i, stamp, out, inn, why, free=False, forced=False):
        P, s, fin = teams[i], S[i], teams[i]['fin']
        if out not in s['on'] or inn not in s['bench_ids'] or inn in s['used'] or inn in s['on'] or inn in s['gone']:
            return False
        if s['nsubs'] >= sub_limit():
            return False
        if not free and stamp not in s['stops']:
            if len(s['stops']) >= stop_limit() and not forced:
                return False
            s['stops'].add(stamp)
        fin['subs'].append((stamp, out, inn))
        fin['end'][out] = stamp
        fin['start'][inn] = stamp
        fin['why'][out] = why
        s['on'][s['on'].index(out)] = inn
        s['used'].add(inn)
        s['gone'].add(out)
        s['nsubs'] += 1
        return True

    def do_leave(i, stamp, pid):
        s, fin = S[i], teams[i]['fin']
        s['on'].remove(pid)
        s['gone'].add(pid)
        fin['end'][pid] = stamp
        fin['subs'].append((stamp, pid, None))

    def pick_on(i, wfun, avoid_yc=False):
        s, P = S[i], teams[i]
        ids = list(s['on'])
        ws = [wfun(P['players'][pid]) + 1e-6 for pid in ids]
        pid = random.choices(ids, ws)[0]
        for _ in range(3):
            if not avoid_yc or s['yc'][pid] == 0 or random.random() < 0.08:
                break
            pid = random.choices(ids, ws)[0]
        return pid

    def mark_red(i):
        S[i]['trig'].add('red_own')
        S[1 - i]['trig'].add('red_opp')

    def apply_events(t):
        """Áp dụng thẻ / chấn thương đã bốc thăm phút (phút <= t+1) - người nhận được chọn trong số người đang trên sân."""
        for i in (0, 1):
            s, P, fin = S[i], teams[i], teams[i]['fin']
            side = 'h' if i == 0 else 'a'
            while s['sched'] and s['sched'][0][0] <= t + 1 and s['on']:
                mn, kind = s['sched'].pop(0)
                mn = max(mn, int(t) + 1)
                if kind == 'y' or kind == 'r':
                    pid = pick_on(i, lambda e: YELLOW_W[e['group']] * (0.55 + 0.9 * e['agg'] / 100.0), avoid_yc=(kind == 'y'))
                    if kind == 'r':
                        if random.random() < VC['red_overturn'] * vu:
                            var_events.append({'m': mn, 't': side, 'k': 'red_overturned', 'p': pid, 'why': 'foul', 'res': 'overturned'})
                            continue
                        fin['cards'].append({'m': mn, 'p': pid, 'c': 'r'})
                        do_leave(i, mn, pid)
                        mark_red(i)
                    elif s['yc'][pid] == 0 and random.random() < VC['yellow_upgrade'] * vu:
                        var_events.append({'m': mn, 't': side, 'k': 'red_upgraded', 'p': pid, 'why': 'foul', 'res': 'upgraded'})
                        fin['cards'].append({'m': mn, 'p': pid, 'c': 'r', 'var': 'upgraded'})
                        do_leave(i, mn, pid)
                        mark_red(i)
                    else:
                        s['yc'][pid] += 1
                        fin['cards'].append({'m': mn, 'p': pid, 'c': 'y'})
                        if s['yc'][pid] == 2:
                            fin['cards'].append({'m': mn, 'p': pid, 'c': 'yr'})
                            do_leave(i, mn, pid)
                            mark_red(i)
                else:      # chấn thương
                    iw = lambda e: ({'GK': 0.3}.get(e['group'], 1.0) * (1 + 0.05 * max(0.0, e['age'] - 29)) *
                                    (1 + (100 - e['fit0']) / 150.0))
                    pid = pick_on(i, iw)
                    name, n = roll_injury()
                    fin['injs'].append({'m': mn, 'p': pid, 'name': name, 'n': n})
                    grp = P['players'][pid]['group']
                    cands = [P['players'][b] for b in s['bench_ids'] if b not in s['used'] and b not in s['on'] and b not in s['gone']]
                    c = [b for b in cands if b['group'] == grp] or [b for b in cands if b['group'] != 'GK']
                    inn = max(c, key=lambda b: b['ovr'] + 8.0 * b['form'])['pid'] if c else None
                    if inn and do_sub(i, mn, pid, inn, 'injury', forced=True):
                        pass
                    else:
                        do_leave(i, mn, pid)
                    s['trig'].add('injury_own')

    # ------------------------------------------------------------------ HLV
    def view_of(i, t, free, pens):
        P, s = teams[i], S[i]
        on = [P['players'][pid] for pid in s['on']]
        bench = [P['players'][b] for b in s['bench_ids'] if b not in s['used'] and b not in s['on'] and b not in s['gone']]
        mk = lambda e: {'pid': e['pid'], 'group': e['group'], 'pos': e['pos'], 'ovr': e['ovr'], 'form': e['form'], 'agg': e['agg'],
                        'pen': e['pen'], 'gkq': e['gk'], 'is_gk': e['group'] == 'GK', 'fit0': e['fit0']}
        von = []
        for e in on:
            d = mk(e)
            d['energy'] = energy_at(P, e['pid'], t)
            d['yc'] = s['yc'][e['pid']] > 0
            d['since'] = t - P['fin']['start'].get(e['pid'], 0)
            von.append(d)
        return {'t': t, 'gf': score[i], 'ga': score[1 - i], 'ko': knockout, 'et': state['et'], 'nsubs': s['nsubs'],
                'subs_left': sub_limit() - s['nsubs'], 'stops_left': stop_limit() - len(s['stops']), 'free_stop': free or pens,
                'trig': set(s['trig']), 'ment': s['ment'], 'ment0': s['ment0'], 'formation': wnl_coach.formation_of(von),
                'gk_on': any(e['group'] == 'GK' for e in on), 'on': von, 'bench': [mk(e) for e in bench], 'pens_next': pens}

    def coach_step(t, free=False, pens=False):
        views = {}
        for i in (0, 1):
            s = S[i]
            if free or pens or s['trig'] or t - s['last_eval'] >= 4.5:
                s['last_eval'] = t
                views[i] = view_of(i, t, free, pens)
        for i, v in views.items():
            s, P, fin = S[i], teams[i], teams[i]['fin']
            side = 'h' if i == 0 else 'a'
            stamp = 120 if t >= 120 else int(t) + 1
            before = wnl_coach.formation_of(v['on'])
            acts = wnl_coach.decide(P['coach'], s['cs'], v, random)
            why_f, changed = None, False
            for a in acts:
                if a['t'] == 'sub':
                    if do_sub(i, stamp, a['out'], a['in'], a['why'], free=v['free_stop'], forced=(a['why'] in ('red_card', 'injury'))):
                        changed = True
                        if why_f is None or a['why'] in ('chasing', 'protect', 'red_card'):
                            why_f = a['why']
                else:
                    s['ment'] = a['ment']
                    fin['ments'].append((stamp, side, a['ment'], a['why']))
                    if why_f is None:
                        why_f = a['why']
            if changed:
                after = wnl_coach.formation_of([{'group': P['players'][pid]['group']} for pid in s['on']])
                if after != before and why_f in ('chasing', 'protect', 'red_card', 'tactical'):
                    fin['tactics'].append((stamp, side, after, why_f))
        for s in S:
            s['trig'].clear()

    # ------------------------------------------------------------------ cú sút (+ VAR)
    def process_shot(x, minute):
        """Trả về True nếu có bàn thắng hợp lệ (làm đổi tỷ số)."""
        P, Q = teams[x], teams[1 - x]
        side = 'h' if x == 0 else 'a'
        if random.random() < CFG['pen_share'] * pen_m:
            sh = _pick_penalty_taker(P, minute)
            if random.random() < VC['pen_cancel'] * vu:
                var_events.append({'m': minute, 't': side, 'k': 'penalty_cancelled', 'p': sh['pid'], 'why': 'no_foul', 'res': 'cancelled'})
                return False
            stat['shots'][x] += 1
            gk_val = lines(Q, minute)['gk']
            pg = clip(0.77 + (sh['pen'] - 70) / 300.0 - (gk_val - 60) / 700.0, 0.55, 0.90)
            stat['sot'][x] += 1
            stat['xg'][x] += pg
            tag = None
            if random.random() < VC['pen_var'] * vu:
                tag = 'awarded'
                var_events.append({'m': minute, 't': side, 'k': 'penalty_awarded', 'p': sh['pid'], 'why': 'foul', 'res': 'awarded'})
            ent = {'m': minute, 't': side, 's': sh['pid'], 'a': None, 'o': 'save', 'pen': True, 'xg': round(pg, 2)}
            if tag:
                ent['var'] = tag
            if random.random() < pg:
                score[x] += 1
                goals.append({'m': minute, 't': side, 's': sh['pid'], 'a': None, 'pen': True, 'xg': round(pg, 2)})
                ent['o'] = 'goal'
                shot_log.append(ent)
                return True
            shot_log.append(ent)
            return False
        stat['shots'][x] += 1
        sh = _pick_shooter(P, minute)
        Lq = lines(Q, minute)
        p_sot = clip(CFG['p_sot_base'] + CFG['p_sot_fin'] * (sh['fin_q'] - 70) -
                     CFG['p_sot_def'] * (Lq['def'] - 70), 0.12, 0.62)
        lo, hi = CFG['p_goal_range']
        p_goal = clip(CFG['p_goal_base'] * math.exp(CFG['k_fin_gk'] * (sh['fin_q'] - (Lq['gk'] + CFG['gk_offset']))), lo, hi)
        xg = round(p_sot * p_goal, 2)
        stat['xg'][x] += p_sot * p_goal
        if random.random() < p_sot:
            stat['sot'][x] += 1
            if random.random() < p_goal:
                if random.random() < VC['goal_overturn'] * vu:      # VAR huỷ bàn thắng: vẫn là cú sút trúng đích
                    why = random.choices(['offside', 'foul', 'handball'], [0.6, 0.25, 0.15])[0]
                    shot_log.append({'m': minute, 't': side, 's': sh['pid'], 'a': None, 'o': 'goal_disallowed', 'pen': False,
                                     'xg': xg, 'var': 'overturned', 'why': why})
                    var_events.append({'m': minute, 't': side, 'k': 'goal_overturned', 'p': sh['pid'], 'why': why, 'res': 'overturned'})
                    return False
                tag = None
                if random.random() < VC['goal_check'] * vu:          # xem lại nhưng giữ nguyên bàn thắng
                    tag = 'checked'
                    var_events.append({'m': minute, 't': side, 'k': 'no_change', 'what': 'goal', 'p': sh['pid'], 'res': 'confirmed'})
                score[x] += 1
                a = _pick_assist(P, minute, sh['pid']) if random.random() < CFG['assist_prob'] else None
                g = {'m': minute, 't': side, 's': sh['pid'], 'a': a, 'xg': xg}
                ent = {'m': minute, 't': side, 's': sh['pid'], 'a': a, 'o': 'goal', 'pen': False, 'xg': xg}
                if tag:
                    g['var'] = ent['var'] = tag
                goals.append(g)
                shot_log.append(ent)
                return True
            shot_log.append({'m': minute, 't': side, 's': sh['pid'], 'a': None, 'o': 'save', 'pen': False, 'xg': xg})
        else:
            shot_log.append({'m': minute, 't': side, 's': sh['pid'], 'a': None,
                             'o': 'block' if random.random() < 0.4 else 'off', 'pen': False, 'xg': xg})
        return False

    def window(t0, t1, tempo):
        """Mô phỏng các cú sút trong (t0, t1]. Có bàn thắng thì dừng ngay tại phút đó (HLV được xem lại tình hình)."""
        mid = max((t0 + t1) / 2.0, t0 + 1.0)
        L = [lines(A, mid), lines(B, mid)]
        ff = [1 - CFG['fatigue_weight'] * (1 - L[i]['energy']) for i in (0, 1)]
        share0 = 1.0 / (1.0 + math.exp(-((L[0]['poss'] * ff[0] - L[1]['poss'] * ff[1]) + (0 if neutral else 1.5)) / poss_scale))
        shares = [share0, 1 - share0]
        dur = t1 - t0
        late = clip(t0 / 90.0, 0.0, 1.2)
        todo = []
        for x in (0, 1):
            y = 1 - x
            diff = L[x]['att'] * ff[x] * ma[x] - L[y]['def'] * ff[y] * md[y]
            lam = (CFG['base_shots'] * dur / 90.0) * math.exp(CFG['k_att'] * math.tanh(diff / CFG['sat']))
            lam *= (shares[x] / 0.5) ** CFG['k_poss']
            lam *= ha[x] * shots_m
            lam *= CFG['mentality'][S[x]['ment']][0] * CFG['mentality'][S[y]['ment']][1]
            # Form/momentum chỉ là hiệu ứng mềm; tuyệt đối không được áp đảo ATT/DEF.
            lam *= team_form_multiplier(teams[x])
            gd = score[x] - score[y]
            if gd < 0:
                lam *= 1 + CFG['trail_boost'] * min(2, -gd) * (0.4 + 0.8 * late)
            elif gd > 0:
                lam *= 1 - CFG['lead_ease'] * min(2, gd) * (0.4 + 0.8 * late)
            lam *= (L[x]['n'] / 11.0) ** 1.4 * (11.0 / max(1, L[y]['n'])) ** 0.6
            lam *= tempo
            for _ in range(poisson(max(0.01, lam))):
                todo.append((random.randint(t0 + 1, t1), random.random(), x))
        todo.sort()
        broke = None
        for minute, _, x in todo:
            if process_shot(x, minute):
                S[x]['trig'].add('goal_for')
                S[1 - x]['trig'].add('goal_against')
                broke = minute
                break
        end = broke if broke is not None else t1
        stat['poss'][0] += share0 * (end - t0)
        stat['poss'][1] += (1 - share0) * (end - t0)
        stat['dur'] += end - t0
        seg_share.append((t0, end, share0))
        return broke

    def play_period(t_start, t_end, tempo, free):
        t, first = t_start, True
        while t < t_end:
            apply_events(t)
            coach_step(t, free=(free and first))
            first = False
            nxt = min([s['sched'][0][0] for s in S if s['sched']] + [999]) - 1     # sự kiện đã bốc thăm: cắt cửa sổ trước phút đó
            t1 = min(t + CFG['win'], t_end)
            if t < nxt < t1:
                t1 = nxt
            g = window(t, t1, tempo)
            t = g if g is not None else t1

    play_period(0, 45, 1.0, False)
    play_period(45, 90, 1.0, True)
    et, pens = False, None
    if knockout and (score[0] + aggregate_before[0]) == (score[1] + aggregate_before[1]):
        et = state['et'] = True
        play_period(90, 105, CFG['et_tempo'], True)
        play_period(105, 120, CFG['et_tempo'], True)
        if (score[0] + aggregate_before[0]) == (score[1] + aggregate_before[1]):
            coach_step(120, pens=True)
            pens = list(_shootout(A, B))
    # xem lại không đổi quyết định (kiểm tra phạt đền / thẻ đỏ nhưng giữ nguyên)
    for _ in range(poisson(VC['extra_check'] * vu)):
        mn = random.randint(5, 90)
        i = random.randint(0, 1)
        on_now = [pid for pid in on_pitch(teams[i], mn) if teams[i]['players'][pid]['group'] != 'GK'] or on_pitch(teams[i], mn)
        var_events.append({'m': mn, 't': 'h' if i == 0 else 'a', 'k': 'no_change', 'what': random.choice(['penalty', 'penalty', 'red']),
                           'p': random.choice(on_now), 'res': 'confirmed'})
    goals.sort(key=lambda g: g['m'])
    shot_log.sort(key=lambda s: s['m'])
    var_events.sort(key=lambda v: v['m'])
    endt = 120 if et else 90
    mins, ga = [{}, {}], [defaultdict(int), defaultdict(int)]
    for x, P in enumerate(teams):
        fin = P['fin']
        for pid, st in fin['start'].items():
            mins[x][pid] = max(0, fin['end'].get(pid, endt) - st)
    for g in goals:
        scorer_side = 0 if g['t'] == 'h' else 1
        opp = teams[1 - scorer_side]
        for pid in on_pitch(opp, g['m']):
            ga[1 - scorer_side][pid] += 1
    pos = stat['poss'][0] / max(1e-9, stat['dur'])
    tactics = sorted([x for P in teams for x in P['fin']['tactics']], key=lambda x: x[0])
    ments = sorted([x for P in teams for x in P['fin']['ments']], key=lambda x: x[0])
    return {
        'hg': score[0], 'ag': score[1], 'et': et, 'pens': pens, 'aggregate_before': list(aggregate_before), 'goals': goals,
        'xi_h': A['xi_ids'], 'xi_a': B['xi_ids'], 'subs_h': A['fin']['subs'], 'subs_a': B['fin']['subs'],
        'cards_h': A['fin']['cards'], 'cards_a': B['fin']['cards'],
        'inj_h': A['fin']['injs'], 'inj_a': B['fin']['injs'],
        'mins_h': mins[0], 'mins_a': mins[1], 'ga_h': dict(ga[0]), 'ga_a': dict(ga[1]),
        'form_h': A['formation'], 'form_a': B['formation'], 'ment': ment0,
        'tactics': tactics, 'ments': ments, 'sub_why': {'h': dict(A['fin']['why']), 'a': dict(B['fin']['why'])},
        'var': var_events, 'ctx': ctx, 'ref_strict': (eff['foul'] if eff else None), 'coach': [A['coach']['name'], B['coach']['name']],
        'shot_log': shot_log, 'seg_share': seg_share,
        'stats': {'poss': [round(100 * pos), 100 - round(100 * pos)], 'shots': stat['shots'], 'sot': stat['sot'],
                  'xg': [round(stat['xg'][0], 2), round(stat['xg'][1], 2)]},
    }


# =====================================================================
#  YẾU TỐ ĐỘNG: PHONG ĐỘ, THỂ LỰC, ĐÀ
# =====================================================================
def match_fit_loss(minutes, sta, age):
    """Thể lực mất đi sau một trận (thang 0-100)."""
    f = minutes / 90.0
    return f * (CFG['fit_loss'] + CFG['fit_loss_sta'] * (1.3 - sta / 100.0) + max(0.0, age - 30) * 0.4)


def recover_fit(fit, days):
    return 100 - (100 - fit) * math.exp(-days / CFG['fit_recover_days'])


def decay_form(form, days):
    return form * 0.5 ** (days / CFG['form_halflife_days'])


def decay_mom(mom, days):
    return mom * 0.5 ** (days / CFG['mom_halflife_days'])


def perf_rating(group, goals, assists, conceded, result, yellow, red, minutes):
    """Điểm trận của cầu thủ (thang ~3-10, trung bình ~6.3). result: +1 thắng, 0 hòa, -1 thua."""
    r = 6.3 + 1.15 * goals + 0.75 * assists + 0.45 * result - 0.25 * yellow - 1.5 * red
    if minutes >= 60 and conceded == 0:
        r += {'GK': 0.9, 'DEF': 0.6, 'MID': 0.2}.get(group, 0.0)
    r -= {'GK': 0.35, 'DEF': 0.35, 'MID': 0.15}.get(group, 0.0) * conceded
    r += random.gauss(0, 0.45)
    return clip(r, 3.0, 10.0)


def next_form(form, perf, minutes):
    if minutes < 10:
        return form
    target = clip((perf - 6.4) / 1.6, -1.0, 1.0)
    alpha = CFG['form_alpha'] * min(1.0, minutes / 70.0)
    return clip(form + alpha * (target - form), -1.0, 1.0)


def next_mom(mom, W, We):
    """W: 1/0.5/0 kết quả thực, We: kỳ vọng. Thắng bất ngờ -> đà tăng mạnh."""
    return clip(0.6 * mom + 0.5 * (W - We) * 2.0, -1.0, 1.0)


def form_label(form):
    if form >= 0.55:
        return "Cực tốt"
    if form >= 0.2:
        return "Tốt"
    if form > -0.2:
        return "Bình thường"
    if form > -0.55:
        return "Kém"
    return "Rất kém"


# =====================================================================
#  HIỆU CHỈNH (chạy: python wnl_engine.py)
# =====================================================================
if __name__ == "__main__":
    import wnl_game as wg
    random.seed(1)
    g = wg.Game(cache_file="/tmp/wnl_calib.json", fresh=True)

    def pack(team):
        return prepare_team(team, g.players[team], set(), {}, {})

    def run(h, a, n=1500, neutral=True):
        w = d = l = 0
        gf = ga = sh = sa = 0.0
        for _ in range(n):
            r = simulate(pack(h), pack(a), neutral=neutral)
            gf += r['hg']
            ga += r['ag']
            sh += r['stats']['shots'][0]
            sa += r['stats']['shots'][1]
            if r['hg'] > r['ag']:
                w += 1
            elif r['hg'] == r['ag']:
                d += 1
            else:
                l += 1
        return "T %.0f%% H %.0f%% B %.0f%% | bàn %.2f-%.2f | sút %.1f-%.1f" % (100 * w / n, 100 * d / n, 100 * l / n,
                                                                          gf / n, ga / n, sh / n, sa / n)
    names = g.names
    for k in (2, 3, 16, 50, 100, 192):
        print("%-8s vs %-16s %s" % (names[0], names[k - 1], run(names[0], names[k - 1])))
    print("%-8s vs %-16s %s" % (names[49], names[99], run(names[49], names[99])))
    print("%-8s vs %-16s %s" % (names[99], names[149], run(names[99], names[149])))
