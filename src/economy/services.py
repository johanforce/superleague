# -*- coding: utf-8 -*-
"""Runtime accounting, prize payout and stadium services for Super League.

All cash movement is posted through FinanceService. Event and ledger keys make
slot replay / save reload idempotent. Amounts are M€ rounded to 0.001.
"""
from __future__ import annotations

import copy
import json
import math
import random
import zlib
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from .core import FinanceService, WageService, _money, load_json_object
from .transfers import FinancialFairPlayService, TransferMarketService
from .ai_director import AITransferDirector


def _stable_rng(seed: int, key: str) -> random.Random:
    crc = zlib.crc32(str(key).encode("utf-8")) & 0xFFFFFFFF
    return random.Random((int(seed) ^ crc) & 0xFFFFFFFF)


def _month_key(year: int, month: int) -> str:
    return f"{int(year):04d}-{int(month):02d}"


def _add_month(key: str) -> str:
    year, month = (int(x) for x in key.split("-", 1))
    month += 1
    if month > 12:
        year += 1
        month = 1
    return _month_key(year, month)


class StadiumService:
    """Deterministic stadium assignment and match importance/attendance."""

    DEFAULTS = {
        "big": {"capacity": [55000, 85000], "ticket_price": [60, 90], "hospitality_ratio": [0.08, 0.12]},
        "medium": {"capacity": [35000, 55000], "ticket_price": [40, 60], "hospitality_ratio": [0.05, 0.08]},
        "small": {"capacity": [12000, 35000], "ticket_price": [25, 40], "hospitality_ratio": [0.02, 0.05]},
    }

    def __init__(self, config: dict, real_stadiums: dict | None = None, rivalries: dict | None = None):
        self.config = copy.deepcopy(config or {})
        self.real_stadiums = copy.deepcopy(real_stadiums or {})
        self.rivalries = rivalries or {}

    def ensure_stadiums(self, state: dict, seed: int) -> bool:
        changed = False
        stadiums = state.setdefault("stadiums", {})
        finance_map = state.setdefault("club_finance", {})
        for team, finance in finance_map.items():
            if team in stadiums and isinstance(stadiums[team], dict):
                stadium = stadiums[team]
            else:
                tier = finance.get("tier", "small")
                ranges = self.config.get(tier, self.DEFAULTS.get(tier, self.DEFAULTS["small"]))
                rng = _stable_rng(seed, f"stadium:{team}")
                cap_range = ranges.get("capacity", [12000, 35000])
                price_range = ranges.get("ticket_price", [25, 40])
                hosp_range = ranges.get("hospitality_ratio", [0.02, 0.05])
                capacity = rng.randint(int(cap_range[0]), int(cap_range[1]))
                stadium = {
                    "id": f"STD-{zlib.crc32(team.encode('utf-8')) & 0xffffffff:08X}",
                    "name": f"{team} Arena", "city": None, "country": None,
                    "capacity": capacity, "base_capacity": capacity, "tier_fit": tier,
                    "atmosphere": rng.randint(55, 95),
                    "hospitality_ratio": round(rng.uniform(float(hosp_range[0]), float(hosp_range[1])), 3),
                    "roofed": bool(rng.random() < 0.7), "home_team_id": team,
                    "upgrade_level": 0, "under_construction": None,
                    "source": "generated_for_fictional_club",
                }
                stadiums[team] = stadium
                changed = True
            finance["stadium_id"] = stadium.get("id")
            if not finance.get("ticket_price") or float(finance.get("ticket_price", 0)) <= 0:
                tier = finance.get("tier", "small")
                r = self.config.get(tier, self.DEFAULTS.get(tier, self.DEFAULTS["small"])).get("ticket_price", [25, 40])
                finance["ticket_price"] = round((float(r[0]) + float(r[1])) / 2, 2)
                changed = True
        return changed

    def _importance(self, game, match: dict) -> float:
        state = game.s
        home, away = str(match.get("home", "")), str(match.get("away", ""))
        fh = state.get("club_finance", {}).get(home, {})
        fa = state.get("club_finance", {}).get(away, {})
        big_clash = ((float(fh.get("reputation", 50)) + float(fa.get("reputation", 50))) / 2.0 - 35.0) / 60.0
        big_clash = max(0.0, min(1.0, big_clash))
        pair = "|".join(sorted((home.casefold(), away.casefold())))
        derby = 1.0 if pair in set(self.rivalries.get("pairs", [])) else 0.0
        stage = str(match.get("stage", "")).casefold()
        comp = str(match.get("comp", "")).casefold()
        stage_factor = 1.0 if ("final" in stage or "chung kết" in str(match.get("round", "")).casefold()) else (
            0.85 if ("semi" in stage or "bán kết" in str(match.get("round", "")).casefold()) else (
                0.65 if any(x in stage for x in ("qf", "r16", "knock")) else (0.30 if "c1" in comp else 0.0)))
        stakes = 0.15
        if "premier" in comp:
            try:
                table = game.league_table("premier")
                rank = {row.get("team"): i + 1 for i, row in enumerate(table)}
                if rank.get(home, 99) <= 3 and rank.get(away, 99) <= 3:
                    stakes = 1.0
                elif rank.get(home, 99) <= 4 or rank.get(away, 99) <= 4:
                    stakes = 0.7
            except Exception:
                pass
        round_text = str(match.get("round", ""))
        try:
            round_n = int("".join(ch for ch in round_text if ch.isdigit()) or "0")
        except ValueError:
            round_n = 0
        if round_n >= 12:
            stakes = max(stakes, 0.65)
        score = max(derby * 0.9, 0.40 * big_clash + 0.30 * stakes + 0.20 * stage_factor + 0.10 * 0.5)
        return round(max(0.0, min(1.0, score)), 3)

    def matchday(self, game, match: dict) -> dict:
        """Calculate and persist matchday facts. Does not post money itself."""
        if match.get("matchday"):
            return match["matchday"]
        home, away = str(match.get("home", "")), str(match.get("away", ""))
        stadium = game.s.get("stadiums", {}).get(home, {})
        finance = game.s.get("club_finance", {}).get(home, {})
        tier = finance.get("tier", "small")
        capacity = max(1000, int(stadium.get("capacity", 20000)))
        importance = self._importance(game, match)
        base_fill = 0.50 + float(finance.get("reputation", 50)) / 200.0 + float(stadium.get("atmosphere", 70)) / 500.0
        base_fill = max(0.45, min(0.95, base_fill))
        fill_rate = max(0.45, min(1.0, base_fill + 0.35 * importance))
        attendance = int(round(capacity * fill_rate))
        competition = str(match.get("comp", ""))
        multipliers = self.config.get("competition_multipliers", {})
        mult = float(multipliers.get("C1" if "c1" in competition.casefold() else
                                     "CUP" if competition == "Cup" else
                                     "PL" if "premier" in competition.casefold() else
                                     "FC1" if "championship 1" in competition.casefold() else "FC2", 1.0))
        base_ticket = float(finance.get("ticket_price", 40.0))
        ticket_price = base_ticket * (1.0 + 0.6 * importance) * mult
        ticket_revenue = attendance * ticket_price / 1_000_000.0
        hospitality_ratio = float(stadium.get("hospitality_ratio", 0.04))
        hospitality_price_factor = float(self.config.get("hospitality_price_factor", 4.0))
        hospitality = capacity * hospitality_ratio * ticket_price * hospitality_price_factor * (0.5 + 0.5 * importance) / 1_000_000.0
        per_fan_spend = float(self.config.get("per_fan_spend_eur", 8.0))
        concessions = attendance * per_fan_spend * (0.9 + 0.3 * importance) / 1_000_000.0
        sold_out = fill_rate >= 0.98
        sell_out_bonus = ticket_revenue * float(self.config.get("sell_out_bonus_rate", 0.07)) if sold_out else 0.0
        gross = _money(ticket_revenue + hospitality + concessions + sell_out_bonus)
        if match.get("neutral"):
            team_share = float(self.config.get("neutral_final_team_share", 0.70)) / 2.0
            gross_for_home = _money(gross * team_share)
            gross_for_away = _money(gross * team_share)
        else:
            gross_for_home = gross
            gross_for_away = 0.0
        result = {
            "stadium_id": stadium.get("id"), "stadium_name": stadium.get("name", f"{home} Arena"),
            "capacity": capacity, "attendance": attendance, "fill_rate": round(fill_rate, 4),
            "match_importance": importance,
            "importance_label": "Tâm điểm" if importance >= 0.75 else ("Hấp dẫn" if importance >= 0.5 else "Thường"),
            "ticket_price_eur": round(ticket_price, 2), "ticket_revenue_m": _money(ticket_revenue),
            "hospitality_m": _money(hospitality), "concessions_m": _money(concessions),
            "sell_out_bonus_m": _money(sell_out_bonus), "gross_revenue_m": gross,
            "home_income_m": gross_for_home, "away_income_m": gross_for_away,
            "sold_out": sold_out,
        }
        match["matchday"] = result
        # Flat fields simplify existing result/report tables and archive consumers.
        match["attendance"] = attendance
        match["fill_rate"] = result["fill_rate"]
        match["match_importance"] = importance
        return result


class PrizeMoneyPayoutService:
    """Idempotent prize ledger posting using actual Super League competition names."""

    def __init__(self, prize_config: dict):
        self.config = copy.deepcopy(prize_config or {})

    def _award_once(self, game, key: str, team: str, amount: float, description: str, date_text: str):
        if amount <= 0:
            return None
        state = game.s
        awards = state.setdefault("economy_prize_awards", [])
        unique = f"{int(state.get('season', 0))}|{key}|{team}"
        if unique in awards:
            return None
        y, m = (int(date_text[:4]), int(date_text[5:7])) if len(date_text) >= 7 else (int(state.get("season", 0)), 8)
        tx_type = "BROADCAST" if str(key).startswith("broadcast:") else "PRIZE"
        tx = FinanceService.post(state, team, tx_type, amount, season=int(state.get("season", y)),
                                 month=m, description=description, related_id=key, date=date_text,
                                 idempotency_key=f"prize:{unique}")
        awards.append(unique)
        return tx

    @staticmethod
    def _comp_key(comp: str) -> str:
        value = comp.casefold()
        if value == "cup": return "CUP"
        if "premier" in value: return "PL"
        if "championship 1" in value: return "FC1_LEAGUE"
        if "championship 2" in value: return "FC2_LEAGUE"
        if value.startswith("c1"): return "C1"
        return ""

    def on_match_finished(self, game, match: dict) -> list[dict]:
        comp = str(match.get("comp", ""))
        key = self._comp_key(comp)
        if not key:
            return []
        rule = self.config.get(key, {})
        home, away = str(match.get("home", "")), str(match.get("away", ""))
        match_date = str(match.get("date", f"{game.s.get('season', 2026)}-08-01"))
        if key == "C1":
            for team in (home, away):
                self._award_once(game, "C1:participation", team, float(rule.get("participation", 0)),
                                 "C1 · tiền tham dự", match_date)
        elif key == "CUP":
            for team in (home, away):
                self._award_once(game, "CUP:participation", team, float(rule.get("participation", 0)),
                                 "Cup · tiền tham dự", match_date)
        result = []
        hg, ag = int(match.get("hg", 0)), int(match.get("ag", 0))
        per_win = float(rule.get("per_win", 0.0)); per_draw = float(rule.get("per_draw", 0.0))
        if hg == ag and per_draw:
            for team in (home, away):
                tx = self._award_once(game, f"match:{match.get('id')}:draw", team, per_draw,
                                      f"{comp} · hòa vs {away if team == home else home}", match_date)
                if tx: result.append(tx)
        elif hg != ag and per_win:
            winner = home if hg > ag else away
            loser = away if winner == home else home
            tx = self._award_once(game, f"match:{match.get('id')}:win", winner, per_win,
                                  f"{comp} · thắng vs {loser}", match_date)
            if tx: result.append(tx)
            loss = float(rule.get("per_loss", 0.0))
            tx = self._award_once(game, f"match:{match.get('id')}:loss", loser, loss,
                                  f"{comp} · thua vs {winner}", match_date)
            if tx: result.append(tx)
        stage = str(match.get("stage", "")).casefold()
        stages = rule.get("stages", {})
        stage_key = None
        if key == "C1":
            if "semi" in stage: stage_key = "semi_final"
            elif "final" in stage: stage_key = "final"
        elif key == "CUP":
            for needle, prize_key in (("r16", "round_of_16"), ("qf", "quarter_final"),
                                      ("sf", "semi_final"), ("final", "final")):
                if needle in stage:
                    stage_key = prize_key; break
        if stage_key:
            amount = float(stages.get(stage_key, 0.0))
            for team in (home, away):
                tx = self._award_once(game, f"stage:{key}:{stage_key}", team, amount,
                                      f"{comp} · vào {stage_key.replace('_', ' ')}", match_date)
                if tx: result.append(tx)
            if stage_key == "final":
                winner = str(match.get("winner") or (home if hg > ag else away if ag > hg else ""))
                bonus = float(stages.get("winner_bonus", 0.0))
                tx = self._award_once(game, f"stage:{key}:winner_bonus", winner, bonus,
                                      f"{comp} · vô địch", match_date) if winner else None
                if tx: result.append(tx)
        return result

    def on_season_finished(self, game, record: dict) -> None:
        state = game.s
        tables = record.get("leagues", {}) or {}
        season = int(record.get("season", state.get("season", 0)))
        # Final position prize and 50/25/25 broadcast split for each division.
        pools = self.config.get("broadcast_pools", {})
        for tier, table in tables.items():
            code = {"premier": "PL", "fc1": "FC1_LEAGUE", "fc2": "FC2_LEAGUE"}.get(tier)
            if not code: continue
            rule = self.config.get(code, {})
            positions = rule.get("position_prize", [])
            tv_pool = float(pools.get(tier, 0.0))
            if not table: continue
            rep = {team: float(state.get("club_finance", {}).get(team, {}).get("reputation", 50)) for team in (r.get("team") for r in table)}
            rep_total = sum(max(1.0, value) for value in rep.values()) or 1.0
            for index, row in enumerate(table):
                team = row.get("team")
                position_amount = float(positions[index]) if index < len(positions) else 0.0
                self._award_once(game, f"position:{tier}:{index+1}", team, position_amount,
                                 f"{code} · thưởng vị trí {index+1}", f"{season+1}-05-31")
                equal_share = tv_pool * 0.50 / len(table)
                position_share = tv_pool * 0.25 * (len(table) - index) / sum(range(1, len(table)+1))
                reputation_share = tv_pool * 0.25 * max(1.0, rep[team]) / rep_total
                self._award_once(game, f"broadcast:{tier}:equal", team, equal_share,
                                 f"{code} · bản quyền truyền hình chia đều", f"{season+1}-05-31")
                self._award_once(game, f"broadcast:{tier}:position", team, position_share,
                                 f"{code} · bản quyền theo thứ hạng", f"{season+1}-05-31")
                self._award_once(game, f"broadcast:{tier}:facility", team, reputation_share,
                                 f"{code} · bản quyền facility fee", f"{season+1}-05-31")


class FinancialStabilityService:
    """Capped owner support and debt workout controls for financially distressed clubs.

    Owner support is recorded as financing, not operating revenue. Restructured
    debt is amortised monthly only when the club can pay without going below its
    configured cash floor; missed principal is deferred rather than overdrawing.
    """

    def __init__(self, config: dict | None = None):
        self.config = copy.deepcopy(config or {})

    @staticmethod
    def _season_for_month(month_key: str) -> int:
        year, month = (int(part) for part in month_key.split("-", 1))
        return year if month >= 8 else year - 1

    @staticmethod
    def _tier_value(config_value, tier: str, fallback: float) -> float:
        if isinstance(config_value, dict):
            return float(config_value.get(tier, fallback))
        if config_value is None:
            return float(fallback)
        return float(config_value)

    def maybe_restructure_debt(self, game, team: str, month_key: str) -> dict | None:
        cfg = self.config.get("debt_restructure", {})
        if not cfg.get("enabled", True):
            return None
        finance = game.s.get("club_finance", {}).get(team)
        if not isinstance(finance, dict):
            return None
        debt = max(0.0, float(finance.get("debt", 0.0)))
        if debt < float(cfg.get("minimum_debt_m", 10.0)):
            return None
        tier = str(finance.get("tier", "small"))
        cash_trigger = self._tier_value(cfg.get("cash_trigger_m"), tier, 5.0)
        if float(finance.get("balance", 0.0)) > cash_trigger:
            return None
        month_date = f"{month_key}-01"
        season = self._season_for_month(month_key)
        terms = finance.get("debt_terms", {})
        if terms.get("status") == "ACTIVE":
            return None
        processed = finance.setdefault("debt_restructured_seasons", [])
        if season in processed:
            return None
        term_months = max(12, min(240, int(cfg.get("term_months", 60))))
        rate = float(cfg.get("annual_interest_rate", 0.025))
        if not math.isfinite(rate) or not 0.0 <= rate <= 0.20:
            raise ValueError("Lãi suất nợ tái cơ cấu phải nằm trong khoảng 0–20%/năm.")
        terms = {
            "status": "ACTIVE", "principal_start": _money(debt),
            "principal_remaining": _money(debt),
            "monthly_principal": _money(debt / term_months),
            "term_months": term_months, "months_remaining": term_months,
            "annual_interest_rate": rate, "restructured_date": month_date,
            "missed_payments": 0, "last_paid_month": "",
        }
        finance["debt_terms"] = terms
        processed.append(season)
        game.s.setdefault("debt_restructure_history", []).append({
            "team_id": team, "season": season, "date": month_date,
            "principal": _money(debt), "term_months": term_months,
            "annual_interest_rate": rate,
        })
        game.s.setdefault("economy_notifications", []).append({
            "team_id": team, "date": month_date, "priority": "important",
            "message": (f"Chủ sở hữu đã tái cơ cấu khoản nợ {debt:.3f} M€: "
                        f"{term_months} tháng, lãi suất {rate*100:.2f}%/năm."),
        })
        return terms

    def process_monthly_debt_principal(self, game, team: str, month_key: str) -> float:
        finance = game.s.get("club_finance", {}).get(team, {})
        terms = finance.get("debt_terms", {})
        if terms.get("status") != "ACTIVE" or float(finance.get("debt", 0.0)) <= 0:
            return 0.0
        if str(terms.get("last_paid_month", "")) == month_key:
            return 0.0
        cfg = self.config.get("debt_restructure", {})
        tier = str(finance.get("tier", "small"))
        cash_floor = self._tier_value(cfg.get("minimum_cash_after_payment_m"), tier, 1.0)
        due = _money(min(float(finance.get("debt", 0.0)),
                         max(0.0, float(terms.get("monthly_principal", 0.0)))))
        if due <= 0:
            terms["status"] = "PAID"
            return 0.0
        if float(finance.get("balance", 0.0)) - due < cash_floor:
            terms["missed_payments"] = int(terms.get("missed_payments", 0)) + 1
            terms["last_deferred_month"] = month_key
            if terms["missed_payments"] in (1, 3, 6):
                game.s.setdefault("economy_notifications", []).append({
                    "team_id": team, "date": f"{month_key}-01", "priority": "important",
                    "message": f"CLB chưa đủ thanh khoản trả {due:.3f} M€ nợ gốc; kỳ trả được hoãn sang tháng sau.",
                })
            return 0.0
        year, month = (int(part) for part in month_key.split("-", 1))
        season = year if month >= 8 else year - 1
        payment_key = f"debt-principal:{team}:{month_key}"
        FinanceService.post(game.s, team, "DEBT_REPAYMENT", -due,
                            season=season, month=month,
                            description=f"Trả nợ gốc theo lịch tái cơ cấu · {month_key}",
                            related_id=payment_key, date=f"{month_key}-01",
                            idempotency_key=payment_key)
        finance["debt"] = _money(max(0.0, float(finance.get("debt", 0.0)) - due))
        terms["principal_remaining"] = finance["debt"]
        terms["months_remaining"] = max(0, int(terms.get("months_remaining", 0)) - 1)
        terms["last_paid_month"] = month_key
        terms["missed_payments"] = 0
        if finance["debt"] <= 0.001:
            finance["debt"] = 0.0
            terms["principal_remaining"] = 0.0
            terms["months_remaining"] = 0
            terms["status"] = "PAID"
        return due

    def maybe_inject_owner_support(self, game, team: str, month_key: str) -> float:
        cfg = self.config.get("owner_support", {})
        if not cfg.get("enabled", True):
            return 0.0
        finance = game.s.get("club_finance", {}).get(team)
        if not isinstance(finance, dict):
            return 0.0
        tier = str(finance.get("tier", "small"))
        trigger = self._tier_value(cfg.get("cash_trigger_m"), tier, 2.0)
        if float(finance.get("balance", 0.0)) >= trigger:
            return 0.0
        season = self._season_for_month(month_key)
        season_key = str(season)
        ledger = game.s.setdefault("owner_support_by_season", {}).setdefault(season_key, {})
        row = ledger.setdefault(team, {"total": 0.0, "injections": 0, "last_month": ""})
        max_count = max(0, int(cfg.get("max_injections_per_season", 2)))
        if int(row.get("injections", 0)) >= max_count or row.get("last_month") == month_key:
            return 0.0
        cap = self._tier_value(cfg.get("max_per_season_m"), tier, 8.0)
        reserve = self._tier_value(cfg.get("target_reserve_m"), tier, 10.0)
        remaining = max(0.0, cap - float(row.get("total", 0.0)))
        amount = _money(min(remaining, max(0.0, reserve - float(finance.get("balance", 0.0)))))
        if amount <= 0:
            return 0.0
        year, month = (int(part) for part in month_key.split("-", 1))
        payment_key = f"owner-support:{season}:{team}:{month_key}"
        FinanceService.post(game.s, team, "OWNER_INJECTION", amount,
                            season=season, month=month,
                            description="Hỗ trợ thanh khoản khẩn cấp từ chủ sở hữu",
                            related_id=payment_key, date=f"{month_key}-01",
                            idempotency_key=payment_key)
        row["total"] = _money(float(row.get("total", 0.0)) + amount)
        row["injections"] = int(row.get("injections", 0)) + 1
        row["last_month"] = month_key
        finance["owner_support_received"] = _money(
            float(finance.get("owner_support_received", 0.0)) + amount)
        game.s.setdefault("economy_notifications", []).append({
            "team_id": team, "date": f"{month_key}-01", "priority": "important",
            "message": f"Chủ sở hữu bơm {amount:.3f} M€ để hỗ trợ thanh khoản. Đây không phải doanh thu vận hành.",
        })
        return amount


class EconomyRuntime:
    """Attach accounting/prize/stadium hooks to an existing Game instance."""

    def __init__(self, game, project_root: Path):
        self.game = game
        self.root = Path(project_root)
        self.config = load_json_object(self.root / "config" / "economy_operations.json", {})
        self.stadium_cfg = load_json_object(self.root / "config" / "stadiums.json", {})
        self.rivalries = load_json_object(self.root / "config" / "rivalries.json", {"pairs": []})
        prize_path = self.root / "config" / str(game.economy_config.get("prize_money_file", "prize_money.json"))
        self.prizes = PrizeMoneyPayoutService(load_json_object(prize_path, {}))
        self.stadiums = StadiumService(self.stadium_cfg, rivalries=self.rivalries)
        self.ffp_config = load_json_object(self.root / "config" / "ffp.json", {})
        self.ffp = FinancialFairPlayService(self.ffp_config)
        self.stability_config = load_json_object(self.root / "config" / "financial_stability.json", {})
        self.stability = FinancialStabilityService(self.stability_config)
        self.transfers = TransferMarketService(game)
        director_config = load_json_object(self.root / "config" / "ai_transfer_director.json", {})
        self.ai_transfer_director = AITransferDirector(game, self.transfers, director_config)
        self._attached = False

    def attach(self):
        if self._attached:
            return False
        game = self.game
        if not game.economy_config.get("feature_flags", {}).get("ENABLE_ECONOMY", False):
            return False
        changed = self.stadiums.ensure_stadiums(game.s, game.seed)
        game.transfer_market = self.transfers
        game.ffp_service = self.ffp
        game.economy_events.subscribe("DAY_ADVANCED", self.on_day_advanced)
        game.economy_events.subscribe("DATE_ADVANCED", self.on_date_advanced)
        game.economy_events.subscribe("TRANSFER_WINDOW_CLOSED", self.on_transfer_window_closed)
        game.economy_events.subscribe("MATCH_FINISHED", self.on_match_finished)
        game.economy_events.subscribe("COMPETITION_FINISHED", self.on_competition_finished)
        game.economy_events.subscribe("SEASON_FINISHED", self.on_season_finished_event)
        self._attached = True
        return bool(changed)

    def on_transfer_window_closed(self, game=None, month: int = 0, **_payload):
        if self.game.economy_config.get("feature_flags", {}).get("ENABLE_FFP", False):
            self.transfers.on_transfer_window_closed(int(month))

    def on_day_advanced(self, game=None, date: str = "", **_payload):
        if not date:
            return
        # Expiry and AI market decisions run on every calendar day, including
        # days without a fixture and the off-season transfer window.
        if self.game.economy_config.get("feature_flags", {}).get("ENABLE_TRANSFERS", False):
            self.transfers.advance_to_date(date)
            flags = self.game.economy_config.get("feature_flags", {})
            if flags.get("ENABLE_AI_TRANSFER_DIRECTOR", False):
                self.ai_transfer_director.run_window(date)

    def on_date_advanced(self, game=None, date: str = "", **_payload):
        if not date or not self.config.get("monthly_accounting_enabled", True):
            return
        state = self.game.s
        try:
            current = _month_key(int(date[:4]), int(date[5:7]))
        except (ValueError, TypeError):
            return
        last = state.get("economy_last_processed_month")
        if not last:
            months = [current]
        else:
            months, cursor = [], _add_month(str(last))
            # A long gap (e.g. a user resumes a career after several seasons)
            # must not permanently stall accounting at the old 36-month cap.
            # Bound pathological/corrupt dates explicitly instead of silently
            # returning forever without advancing the accounting cursor.
            for _ in range(1200):
                if cursor > current:
                    break
                months.append(cursor)
                cursor = _add_month(cursor)
            if cursor <= current:
                raise ValueError("Khoảng thời gian hạch toán vượt giới hạn an toàn 100 năm.")
            if not months or months[-1] != current:
                return
        for month_key in months:
            self._process_month(month_key)
            state["economy_last_processed_month"] = month_key

    def _process_month(self, month_key: str):
        state = self.game.s
        processed = state.setdefault("economy_processed_months", [])
        if month_key in processed:
            return
        year, month = (int(x) for x in month_key.split("-"))
        # Financial season follows the project's August–July season calendar.
        # During catch-up the active save may already be several seasons ahead,
        # so using state['season'] here would misfile every historical month.
        season = year if month >= 8 else year - 1
        ops = self.config.get("annual_costs", {})
        income = self.config.get("annual_income", {})
        for team, ids in state.get("rosters", {}).items():
            finance = state.get("club_finance", {}).get(team)
            if not finance: continue
            contracts = state.get("contracts", {})
            roster_has_contracts = any(pid in contracts for pid in ids)
            if roster_has_contracts:
                # Central calculation includes incoming-loan wages and the
                # parent club's retained share for outgoing loans.
                wages = self.transfers._annual_wage_committed(team)
            else:
                # Compatibility for legacy saves that predate contract records.
                wages = sum(float(state.get("players", {}).get(pid, {}).get("wage", 0)) for pid in ids)
            monthly_wage = _money(wages / 12.0)
            if monthly_wage:
                FinanceService.post(state, team, "WAGE", -monthly_wage, season=season, month=month,
                                    description=f"Lương cầu thủ · {_month_key(year, month)}", related_id=month_key,
                                    date=f"{year:04d}-{month:02d}-01",
                                    idempotency_key=f"monthly:{month_key}:{team}:WAGE")
            tier = str(finance.get("tier", "small"))
            operating = _money(float(ops.get(tier, 30.0)) / 12.0)
            if operating:
                FinanceService.post(state, team, "OTHER", -operating, season=season, month=month,
                                    description=f"Chi phí vận hành · {_month_key(year, month)}", related_id=month_key,
                                    date=f"{year:04d}-{month:02d}-01",
                                    idempotency_key=f"monthly:{month_key}:{team}:OPERATING")
            sponsor_annual = float(income.get("sponsor", {}).get(tier, 0.0))
            sponsor_annual *= max(0.5, float(finance.get("reputation", 50)) / 70.0)
            sponsor_month = _money(sponsor_annual / 12.0)
            if sponsor_month:
                FinanceService.post(state, team, "SPONSOR", sponsor_month, season=season, month=month,
                                    description=f"Tài trợ định kỳ · {_month_key(year, month)}", related_id=month_key,
                                    date=f"{year:04d}-{month:02d}-01",
                                    idempotency_key=f"monthly:{month_key}:{team}:SPONSOR")
            merch_base = float(income.get("merchandise", {}).get(tier, 0.0))
            fan_scale = min(1.5, max(0.5, float(finance.get("fan_base", 1_000_000)) /
                                    float(income.get("fan_base_reference", {}).get(tier, 1_000_000))))
            merch_month = _money(merch_base * fan_scale / 12.0)
            if merch_month:
                FinanceService.post(state, team, "COMMERCIAL", merch_month, season=season, month=month,
                                    description=f"Doanh thu thương mại · {_month_key(year, month)}", related_id=month_key,
                                    date=f"{year:04d}-{month:02d}-01",
                                    idempotency_key=f"monthly:{month_key}:{team}:COMMERCIAL")
            self.stability.maybe_restructure_debt(self.game, team, month_key)
            debt = float(finance.get("debt", 0.0))
            if debt > 0:
                debt_terms = finance.get("debt_terms", {})
                interest_rate = float(debt_terms.get(
                    "annual_interest_rate", self.config.get("annual_debt_interest", 0.04)))
                interest = _money(debt * interest_rate / 12.0)
                if interest:
                    FinanceService.post(state, team, "OTHER", -interest, season=season, month=month,
                                        description=f"Lãi nợ · {_month_key(year, month)}", related_id=month_key,
                                        date=f"{year:04d}-{month:02d}-01",
                                        idempotency_key=f"monthly:{month_key}:{team}:DEBT_INTEREST")
            debt_principal = self.stability.process_monthly_debt_principal(
                self.game, team, month_key)
            owner_injection = self.stability.maybe_inject_owner_support(
                self.game, team, month_key)
            finance.setdefault("monthly_rollup", {})[month_key] = {
                "wage": monthly_wage, "operating": operating,
                "sponsor": sponsor_month, "merchandise": merch_month,
                "debt_interest": interest if debt > 0 else 0.0,
                "debt_principal": debt_principal,
                "owner_injection": owner_injection,
            }
        processed.append(month_key)

    def on_match_finished(self, game=None, match: dict | None = None, **_payload):
        if not match or not match.get("played"):
            return
        state = self.game.s
        match_id = str(match.get("id"))
        handled = state.setdefault("economy_processed_matches", [])
        if match_id in handled:
            return
        matchday = self.stadiums.matchday(self.game, match)
        home, away = str(match.get("home", "")), str(match.get("away", ""))
        date_text = str(match.get("date", ""))
        season = int(match.get("season", state.get("season", 0)))
        month = int(date_text[5:7]) if len(date_text) >= 7 else 8
        if matchday.get("home_income_m", 0):
            FinanceService.post(state, home, "MATCHDAY", matchday["home_income_m"], season=season,
                                 month=month, description=f"Doanh thu sân nhà · {away}",
                                 related_id=match_id, date=date_text,
                                 idempotency_key=f"matchday:{match_id}:{home}")
        if matchday.get("away_income_m", 0):
            FinanceService.post(state, away, "MATCHDAY", matchday["away_income_m"], season=season,
                                 month=month, description=f"Chia doanh thu sân trung lập · {home}",
                                 related_id=match_id, date=date_text,
                                 idempotency_key=f"matchday:{match_id}:{away}")
        self.prizes.on_match_finished(self.game, match)
        self.transfers.process_match_addons(match)
        handled.append(match_id)

    def on_competition_finished(self, game=None, competition: str = "", result: dict | None = None, **_payload):
        state = self.game.s
        seen = state.setdefault("economy_competitions_finished", [])
        unique = f"{int(state.get('season', 0))}:{competition}"
        if unique not in seen:
            seen.append(unique)

    def on_season_finishing(self, record: dict) -> dict:
        """Settle last summer months, position/broadcast money and save a report."""
        # Advance monthly accounting through July before the next season starts.
        current_season = int(record.get("season", self.game.s.get("season", 0)))
        state = self.game.s
        last = state.get("economy_last_processed_month")
        target = _month_key(current_season + 1, 7)
        if last:
            cursor = _add_month(str(last))
            for _ in range(1200):
                if cursor > target:
                    break
                self._process_month(cursor)
                state["economy_last_processed_month"] = cursor
                cursor = _add_month(cursor)
            if cursor <= target:
                raise ValueError("Không thể quyết toán mùa: khoảng catch-up vượt 100 năm.")
        self.prizes.on_season_finished(self.game, record)
        # Contract clocks advance once per completed season. An expired player is
        # released only if the remaining squad still satisfies 23–30 and position rules.
        if self.game.economy_config.get("feature_flags", {}).get("ENABLE_TRANSFERS", False):
            self.transfers.roll_contract_years(current_season)
        # Market values are refreshed after development/ageing at season end.
        wage_service = WageService(self.game.economy_config)
        for pid, player in state.get("players", {}).items():
            meta = state.get("player_meta", {}).get(pid, {})
            view = dict(player)
            view["age"] = meta.get("age", player.get("age", 26))
            view["potential"] = meta.get("potential", player.get("overall_rating", 60))
            player["market_value"] = wage_service.market_value(view)
            contract = state.get("contracts", {}).get(pid)
            if isinstance(contract, dict):
                contract["market_value"] = player["market_value"]
                if contract.get("status") == "ACTIVE" and int(contract.get("years_remaining", 1)) <= 1:
                    contract["status"] = "EXPIRING"
        for _team, entries in state.get("academy", {}).items():
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or not isinstance(entry.get("player"), dict):
                    continue
                academy_player = entry["player"]
                academy_meta = entry.get("meta", {}) if isinstance(entry.get("meta"), dict) else {}
                view = dict(academy_player)
                view["age"] = academy_meta.get("age", 18)
                view["potential"] = academy_meta.get("potential", academy_player.get("overall_rating", 60))
                academy_player["market_value"] = wage_service.market_value(view)
        report = self.build_season_report(current_season)
        state.setdefault("season_financial_reports", {})[str(current_season)] = report
        ffp_results = {}
        if (self.ffp_config.get("enabled", True) and
                self.game.economy_config.get("feature_flags", {}).get("ENABLE_FFP", False)):
            for team in state.get("rosters", {}):
                ffp_results[team] = self.ffp.evaluate(state, team, current_season)
        report["ffp"] = ffp_results
        record["financial_report"] = copy.deepcopy(report)
        return report

    def on_season_finished_event(self, game=None, record: dict | None = None, **_payload):
        if not record:
            return
        season = str(record.get("season", ""))
        if season not in self.game.s.get("season_financial_reports", {}):
            self.on_season_finishing(record)

    def prepare_season_end_sanctions(self) -> dict:
        """Evaluate FFP before final standings freeze champions and promotions."""
        season = int(self.game.s.get("season", 0))
        state = self.game.s
        report = self.build_season_report(season)
        state.setdefault("season_financial_reports", {})[str(season)] = report
        enabled = (self.ffp_config.get("enabled", True) and
                   self.game.economy_config.get("feature_flags", {}).get("ENABLE_FFP", False))
        if not enabled:
            return {}
        results = {team: self.ffp.evaluate(state, team, season)
                   for team in state.get("rosters", {})}
        report["ffp"] = results
        return results

    def build_season_report(self, season: int) -> dict:
        state = self.game.s
        rows = {}
        for team in state.get("rosters", {}):
            txs = [tx for tx in state.get("finance_ledger", [])
                   if tx.get("teamId") == team and int(tx.get("season", -1)) == int(season)]
            by_type = defaultdict(float)
            opening_capital = 0.0
            owner_support = 0.0
            debt_principal_paid = 0.0
            for tx in txs:
                amount = float(tx.get("amount", 0.0))
                related_id = str(tx.get("relatedId") or "")
                description = str(tx.get("description") or "")
                # Initial shareholder/club capital is a balance-sheet funding
                # entry, not operating revenue or an expense for profit/FFP.
                if related_id.startswith("finance-init:") or description.startswith("Vốn tài chính khởi tạo"):
                    opening_capital += amount
                    continue
                # Owner rescue is balance-sheet funding, never operating
                # revenue and must not improve the FFP loss calculation.
                if str(tx.get("type", "")) == "OWNER_INJECTION":
                    owner_support += amount
                    continue
                # Principal repayment is a balance-sheet movement; only the
                # interest charged in OTHER belongs in the operating P&L.
                if str(tx.get("type", "")) == "DEBT_REPAYMENT":
                    debt_principal_paid += -amount
                    continue
                by_type[str(tx.get("type", "OTHER"))] += amount
            revenue = sum(v for v in by_type.values() if v > 0)
            costs = -sum(v for v in by_type.values() if v < 0)
            finance = state.get("club_finance", {}).get(team, {})
            wages = -by_type.get("WAGE", 0.0)
            rows[team] = {
                "balance": _money(finance.get("balance", 0.0)), "debt": _money(finance.get("debt", 0.0)),
                "opening_capital": _money(opening_capital),
                "owner_support": _money(owner_support),
                "debt_principal_paid": _money(debt_principal_paid),
                "revenue": _money(revenue), "costs": _money(costs), "profit_loss": _money(revenue - costs),
                "wage_cost": _money(wages), "wage_revenue_ratio": round(wages / revenue, 4) if revenue else None,
                "by_type": {key: _money(value) for key, value in sorted(by_type.items())},
                "transaction_count": len(txs),
            }
        return {"season": int(season), "currency": "M€", "clubs": rows,
                "generated_at": f"{int(season)+1:04d}-07-31"}
