# -*- coding: utf-8 -*-
"""Transfer, approach-fee and timed contract negotiation services.

The service is intentionally UI-agnostic. GUI actions call these methods; they
validate all constraints before mutating rosters and write every cash movement
through FinanceService.
"""
from __future__ import annotations

import copy
import math
from datetime import date, timedelta
from typing import Any

from .core import FinanceService, SquadRulesService, WageService, _money, position_group


class TransferMarketService:
    def __init__(self, game):
        self.game = game
        self.config = game.economy_config
        self.window_service = game.transfer_windows
        self.squad_rules = SquadRulesService(self.config)

    @staticmethod
    def _date_parts(date_text: str):
        d = date.fromisoformat(str(date_text))
        return d, d.year, d.month

    def _window_key(self, date_text: str) -> str:
        d, year, month = self._date_parts(date_text)
        name = self.window_service.window_name(month)
        if not name:
            return ""
        return f"{year}:{name}"

    def _window_open(self, date_text: str, *, free_agent=False) -> bool:
        d, _year, month = self._date_parts(date_text)
        if self.window_service.is_open(month):
            return True
        return bool(free_agent and self.config.get("transfer_windows", {}).get("allow_free_agent_outside_window", True))

    def _active_loan_for_player(self, player_id: str) -> dict | None:
        return next((row for row in self.game.s.get("loan_agreements", [])
                     if str(row.get("player_id")) == str(player_id)
                     and row.get("status") in ("ACTIVE", "RETURN_PENDING")), None)

    def _annual_wage_committed(self, team: str, exclude_player: str | None = None) -> float:
        """Annual payroll burden, including the parent/borrower split on loans."""
        state = self.game.s
        contracts = state.get("contracts", {})
        total = 0.0
        roster = state.get("rosters", {}).get(team, [])
        roster_has_contracts = any(pid in contracts for pid in roster)
        active_contract_statuses = {"ACTIVE", "EXPIRING", "EXPIRED_PENDING", "ON_LOAN"}
        for pid in roster:
            if str(pid) == str(exclude_player):
                continue
            contract = contracts.get(pid, {})
            if roster_has_contracts and contract.get("status", "ACTIVE") not in active_contract_statuses:
                continue
            wage = float(contract.get("wage_per_year", state.get("players", {}).get(pid, {}).get("wage", 0)))
            loan = self._active_loan_for_player(pid)
            if loan and str(loan.get("borrower_team")) == str(team):
                wage *= max(0.0, min(100.0, float(loan.get("wage_share_pct", 100.0)))) / 100.0
            total += wage
        if not roster_has_contracts:
            # Compatibility for old saves without contract rows on roster players.
            total = sum(float(state.get("players", {}).get(pid, {}).get("wage", 0))
                        for pid in roster if str(pid) != str(exclude_player))
        for loan in state.get("loan_agreements", []):
            if (str(loan.get("parent_team")) != str(team)
                    or loan.get("status") not in ("ACTIVE", "RETURN_PENDING")
                    or str(loan.get("player_id")) == str(exclude_player)):
                continue
            original = loan.get("original_contract", {})
            original_wage = float(original.get("wage_per_year", 0.0))
            parent_share = 1.0 - max(0.0, min(100.0, float(loan.get("wage_share_pct", 100.0)))) / 100.0
            total += original_wage * parent_share
        return _money(total)

    def _feature_enabled(self) -> bool:
        return bool(self.config.get("feature_flags", {}).get("ENABLE_TRANSFERS", False))

    def _assert_enabled(self):
        if not self._feature_enabled():
            raise ValueError("Thị trường chuyển nhượng đang tắt trong config/economy.json.")

    def _deal_limit_ok(self, team: str, date_text: str) -> bool:
        key = self._window_key(date_text)
        if not key:
            return False
        counts = self.game.s.setdefault("transfer_deal_counts", {})
        used = int(counts.get(f"{key}:{team}", 0))
        limit = int(self.config.get("transfer_windows", {}).get("max_deals_per_window", 8))
        return used < limit

    def is_transfer_banned(self, team: str) -> bool:
        state = self.game.s
        ban = state.get("ffp_bans", {}).get(team, {})
        if int(ban.get("windows_remaining", 0)) > 0:
            return True
        return bool(state.get("ffp_status", {}).get(team, {}).get("transfer_ban"))

    def on_transfer_window_closed(self, month: int):
        # A ban applies to a complete transfer window, not just one calendar month.
        if self.window_service.window_name(month) is None:
            return
        for team, ban in self.game.s.setdefault("ffp_bans", {}).items():
            remaining = int(ban.get("windows_remaining", 0))
            if remaining > 0:
                ban["windows_remaining"] = remaining - 1
                status = self.game.s.setdefault("ffp_status", {}).get(team)
                if status:
                    status["transfer_ban_windows_remaining"] = remaining - 1
                    status["transfer_ban"] = remaining - 1 > 0
                    status["status"] = "TRANSFER_BAN" if status["transfer_ban"] else (
                        "WARNING" if status.get("wage_warning") else "COMPLIANT")

    def advance_to_date(self, date_text: str) -> list[dict]:
        """Expire timed negotiations/offers when the simulated calendar advances."""
        current = date.fromisoformat(str(date_text))
        state = self.game.s
        events = []
        for item in state.get("contract_negotiations", []):
            if item.get("status") in ("IN_PROGRESS", "COUNTERED"):
                due = date.fromisoformat(str(item.get("due_date", date_text)))
                if current > due:
                    item["status"] = "FAILED"
                    item["failure_reason"] = "Hết thời gian đàm phán."
                    events.append({"type": "negotiation_expired", "id": item.get("id")})
                    state.setdefault("economy_notifications", []).append({
                        "team_id": item.get("team_id"), "date": date_text,
                        "priority": "important", "message": f"Đàm phán với {item.get('player_id')} đã hết hạn.",
                    })
        for offer in state.get("transfer_offers", []):
            if offer.get("status") in ("PENDING", "COUNTERED"):
                expiry = date.fromisoformat(str(offer.get("expires_date", date_text)))
                if current > expiry:
                    offer["status"] = "EXPIRED"
                    offer["resolved_date"] = date_text
                    events.append({"type": "offer_expired", "id": offer.get("id")})
        events.extend(self.process_due_transfer_payments(date_text))
        events.extend(self.expire_transfer_addons(date_text))
        events.extend(self.return_expired_loans(date_text))
        return events

    @staticmethod
    def _validated_deal_terms(deal_terms: dict | None) -> dict:
        """Normalize optional advanced fee terms without changing legacy offers."""
        raw = deal_terms if isinstance(deal_terms, dict) else {}
        try:
            count = int(raw.get("installments", 1))
            upfront_pct = float(raw.get("upfront_pct", 100.0))
            interval_days = int(raw.get("interval_days", 30))
            sell_on_pct = float(raw.get("sell_on_pct", 0.0))
            addons_raw = raw.get("addons", []) or []
            if not isinstance(addons_raw, list):
                raise ValueError("Danh sách add-on phải là một danh sách.")
            addons = []
            for entry in addons_raw:
                if not isinstance(entry, dict):
                    raise ValueError("Điều khoản add-on không hợp lệ.")
                metric = str(entry.get("metric", "")).upper().strip()
                threshold = int(entry.get("threshold", 0))
                amount = _money(entry.get("amount", 0.0))
                valid_days = int(entry.get("valid_days", 730))
                if metric not in ("APPEARANCES", "GOALS"):
                    raise ValueError("Add-on chỉ hỗ trợ APPEARANCES hoặc GOALS.")
                if not 1 <= threshold <= (1000 if metric == "APPEARANCES" else 500):
                    raise ValueError("Mốc add-on nằm ngoài giới hạn hợp lệ.")
                if amount <= 0 or amount > 100.0:
                    raise ValueError("Giá trị mỗi add-on phải lớn hơn 0 và không quá 100 M€.")
                if not 30 <= valid_days <= 1095:
                    raise ValueError("Thời hạn add-on phải từ 30 đến 1.095 ngày.")
                addons.append({"metric": metric, "threshold": threshold,
                               "amount": amount, "valid_days": valid_days})
            if len(addons) > 3:
                raise ValueError("Mỗi thương vụ tối đa 3 điều khoản add-on.")
        except (TypeError, ValueError) as exc:
            raise ValueError("Điều khoản chuyển nhượng không hợp lệ.") from exc
        if not 1 <= count <= 6:
            raise ValueError("Số đợt thanh toán phải nằm trong khoảng 1–6.")
        if not math.isfinite(upfront_pct) or not 20.0 <= upfront_pct <= 100.0:
            raise ValueError("Tỷ lệ trả trước phải nằm trong khoảng 20–100%.")
        if count == 1 and upfront_pct != 100.0:
            raise ValueError("Thương vụ trả một lần phải thanh toán 100% ngay.")
        if count > 1 and upfront_pct >= 100.0:
            raise ValueError("Thương vụ nhiều đợt cần có ít nhất một khoản trả sau.")
        if not 14 <= interval_days <= 180:
            raise ValueError("Khoảng cách giữa các đợt phải nằm trong khoảng 14–180 ngày.")
        if not math.isfinite(sell_on_pct) or not 0.0 <= sell_on_pct <= 30.0:
            raise ValueError("Sell-on phải nằm trong khoảng 0–30%.")
        return {"installments": count, "upfront_pct": round(upfront_pct, 2),
                "interval_days": interval_days, "sell_on_pct": round(sell_on_pct, 2),
                "addons": addons}

    @staticmethod
    def _new_settlement_id(state: dict) -> str:
        seq = int(state.get("transfer_settlement_seq", 0)) + 1
        state["transfer_settlement_seq"] = seq
        return f"SET-{seq:08d}"

    def _build_fee_schedule(self, offer: dict, date_text: str) -> list[dict]:
        fee = _money(offer.get("fee", 0.0))
        terms = self._validated_deal_terms(offer.get("deal_terms"))
        count = terms["installments"]
        first = _money(fee * terms["upfront_pct"] / 100.0)
        remaining = _money(fee - first)
        schedule = []
        paid_at_start = date.fromisoformat(date_text)
        for index in range(1, count + 1):
            if index == 1:
                amount = first
                due = paid_at_start
            elif index == count:
                amount = _money(fee - sum(row["amount"] for row in schedule))
                due = paid_at_start + timedelta(days=terms["interval_days"] * (index - 1))
            else:
                amount = _money(remaining / (count - 1))
                due = paid_at_start + timedelta(days=terms["interval_days"] * (index - 1))
            if amount <= 0:
                continue
            schedule.append({"id": self._new_settlement_id(self.game.s), "offer_id": offer.get("id"),
                             "player_id": offer.get("player_id"), "payer_team": offer.get("to_team"),
                             "recipient_team": offer.get("from_team"), "amount": amount,
                             "due_date": due.isoformat(), "kind": "TRANSFER_FEE",
                             "installment_number": index, "status": "PAID" if index == 1 else "PENDING",
                             "paid_date": date_text if index == 1 else "", "attempts": 0})
        if abs(sum(row["amount"] for row in schedule) - fee) > 0.001:
            raise ValueError("Lịch trả góp không khớp tổng phí chuyển nhượng.")
        return schedule

    def process_due_transfer_payments(self, date_text: str) -> list[dict]:
        """Settle due transfer instalments and sell-on obligations without overdrafts.

        Unfunded instalments are retried seven days later. Stable ledger keys make
        the payer and recipient legs safe to replay after a partial save failure.
        """
        current = date.fromisoformat(str(date_text))
        state = self.game.s
        events = []
        for payment in state.setdefault("transfer_settlements", []):
            if payment.get("status") != "PENDING":
                continue
            try:
                due = date.fromisoformat(str(payment.get("due_date", date_text)))
            except ValueError:
                payment["status"] = "CANCELLED"
                payment["failure_reason"] = "Ngày đến hạn không hợp lệ."
                continue
            if due > current:
                continue
            payer = str(payment.get("payer_team", ""))
            recipient = str(payment.get("recipient_team", ""))
            amount = _money(payment.get("amount", 0.0))
            if not payer or not recipient or payer == recipient or amount <= 0:
                payment["status"] = "CANCELLED"
                payment["failure_reason"] = "Thông tin thanh toán không hợp lệ."
                continue
            payer_finance = state.get("club_finance", {}).get(payer)
            recipient_finance = state.get("club_finance", {}).get(recipient)
            if not payer_finance or not recipient_finance:
                payment["status"] = "CANCELLED"
                payment["failure_reason"] = "Không tìm thấy ví của một trong hai CLB."
                continue
            if float(payer_finance.get("balance", 0.0)) + 1e-9 < amount:
                payment["attempts"] = int(payment.get("attempts", 0)) + 1
                payment["last_failure_date"] = date_text
                payment["last_failure_reason"] = "Bên mua chưa đủ tiền mặt; hệ thống sẽ thử lại sau 7 ngày."
                payment["due_date"] = (current + timedelta(days=7)).isoformat()
                if payment["attempts"] in (1, 4, 8):
                    state.setdefault("economy_notifications", []).append({
                        "team_id": payer, "date": date_text, "priority": "important",
                        "message": f"Chưa đủ tiền thanh toán đợt chuyển nhượng {payment.get('id')}; hạn mới {payment['due_date']}.",
                    })
                events.append({"type": "transfer_payment_deferred", "id": payment.get("id")})
                continue

            payment_id = str(payment.get("id"))
            month = int(date_text[5:7])
            season = int(date_text[:4]) if month >= 8 else int(date_text[:4]) - 1
            FinanceService.post(state, payer, "TRANSFER_IN", -amount, season=season, month=month,
                                description=f"Thanh toán chuyển nhượng · {payment_id}",
                                related_id=payment_id, date=date_text,
                                idempotency_key=f"settlement:{payment_id}:payer")
            FinanceService.post(state, recipient, "TRANSFER_OUT", amount, season=season, month=month,
                                description=f"Nhận thanh toán chuyển nhượng · {payment_id}",
                                related_id=payment_id, date=date_text,
                                idempotency_key=f"settlement:{payment_id}:recipient")
            # The cash ledger legs are idempotent; the non-cash transfer-budget
            # credit needs its own marker too. A retry after both ledger posts
            # but before the settlement status update must not credit the budget
            # twice.
            budget_key = f"settlement:{payment_id}:recipient-budget"
            budget_credits = state.setdefault("finance_budget_idempotency", [])
            if budget_key not in budget_credits:
                recipient_finance["transfer_budget"] = _money(
                    float(recipient_finance.get("transfer_budget", 0.0)) + amount)
                budget_credits.append(budget_key)
            payment["status"] = "PAID"
            payment["paid_date"] = date_text
            payment["last_failure_reason"] = ""
            clause_id = payment.get("sell_on_clause_id")
            if clause_id:
                for clause in state.get("sell_on_clauses", []):
                    if clause.get("id") == clause_id:
                        clause["status"] = "PAID"
                        clause["paid_date"] = date_text
                        break
            addon_clause_id = payment.get("addon_clause_id")
            if addon_clause_id:
                for clause in state.get("transfer_addon_clauses", []):
                    if clause.get("id") == addon_clause_id:
                        clause["status"] = "PAID"
                        clause["paid_date"] = date_text
                        break
            events.append({"type": "transfer_payment_paid", "id": payment_id, "amount": amount})
        return events

    def _cancel_active_addons_for_player(self, player_id: str, date_text: str,
                                         reason: str) -> list[dict]:
        """Cancel unearned add-ons when the player leaves the paying club.

        Triggered clauses remain payable; only ACTIVE clauses are cancelled.
        The reserved transfer budget is returned to the actual payer, which is
        important if a player has moved through a swap or another transfer.
        """
        state = self.game.s
        cancelled = []
        for clause in state.setdefault("transfer_addon_clauses", []):
            if str(clause.get("player_id")) != str(player_id) or clause.get("status") != "ACTIVE":
                continue
            payer = str(clause.get("payer_team", ""))
            finance = state.get("club_finance", {}).get(payer)
            reserved = _money(clause.get("reserved_amount", clause.get("amount", 0.0)))
            if finance and reserved > 0:
                finance["transfer_budget"] = _money(float(finance.get("transfer_budget", 0.0)) + reserved)
            clause["status"] = "CANCELLED"
            clause["cancelled_date"] = str(date_text)[:10]
            clause["failure_reason"] = str(reason)
            clause["released_budget"] = reserved
            cancelled.append(clause)
        return cancelled

    def expire_transfer_addons(self, date_text: str) -> list[dict]:
        """Expire unearned contingent fees and release their reserved budgets daily."""
        current = date.fromisoformat(str(date_text)[:10])
        state = self.game.s
        events = []
        for clause in state.setdefault("transfer_addon_clauses", []):
            if clause.get("status") != "ACTIVE":
                continue
            try:
                expiry = date.fromisoformat(str(clause.get("expires_date", date_text))[:10])
            except ValueError:
                clause["status"] = "CANCELLED"
                clause["failure_reason"] = "Ngày hết hạn add-on không hợp lệ."
                continue
            if current <= expiry:
                continue
            clause["status"] = "EXPIRED"
            clause["expired_date"] = current.isoformat()
            reserved = _money(clause.get("reserved_amount", clause.get("amount", 0.0)))
            payer = str(clause.get("payer_team", ""))
            finance = state.get("club_finance", {}).get(payer)
            if finance and reserved > 0:
                finance["transfer_budget"] = _money(float(finance.get("transfer_budget", 0.0)) + reserved)
            if payer:
                state.setdefault("economy_notifications", []).append({
                    "team_id": payer, "date": current.isoformat(), "priority": "normal",
                    "message": f"Add-on {clause.get('id')} hết hạn mà chưa đạt mốc; đã giải phóng {reserved:.3f} M€ ngân sách.",
                })
            events.append({"type": "transfer_addon_expired", "id": clause.get("id"),
                           "released_budget": reserved})
        return events

    def process_match_addons(self, match: dict) -> list[dict]:
        """Advance appearance/goal milestones and settle triggered add-ons once."""
        if not isinstance(match, dict) or not match.get("played"):
            return []
        state = self.game.s
        match_id = str(match.get("id", ""))
        if not match_id:
            return []
        handled = state.setdefault("transfer_addon_processed_matches", [])
        if match_id in handled:
            return []
        match_date = str(match.get("date", ""))
        if not match_date:
            return []
        participants_by_team: dict[str, set[str]] = {}
        goals_by_player: dict[str, int] = {}
        for event in match.get("goals", []) or []:
            scorer_id = str(event.get("scorer_id") or "")
            if scorer_id:
                goals_by_player[scorer_id] = goals_by_player.get(scorer_id, 0) + 1
        for side, team_key, minutes_key in (("h", "home", "minutes_h"), ("a", "away", "minutes_a")):
            team = str(match.get(team_key, ""))
            if not team:
                continue
            participants = {str(pid) for pid, mins in (match.get(minutes_key) or {}).items()
                            if int(mins or 0) > 0}
            participants.update(str(pid) for pid in (match.get("xi_" + side, []) or []) if pid)
            side_stats = ((match.get("player_stats") or {}).get(side, {}) or {})
            participants.update(str(pid) for pid in side_stats)
            participants_by_team[team] = participants

        events = []
        current = date.fromisoformat(match_date[:10])
        for clause in state.setdefault("transfer_addon_clauses", []):
            if clause.get("status") != "ACTIVE":
                continue
            try:
                if date.fromisoformat(str(clause.get("created_date", match_date))) > current:
                    continue
                expiry = date.fromisoformat(str(clause.get("expires_date", match_date)))
            except ValueError:
                clause["status"] = "CANCELLED"
                clause["failure_reason"] = "Ngày add-on không hợp lệ."
                continue
            if current > expiry:
                continue
            player_id = str(clause.get("player_id", ""))
            payer = str(clause.get("payer_team", ""))
            recipient = str(clause.get("recipient_team", ""))
            metric = str(clause.get("metric", "")).upper()
            participant = player_id in participants_by_team.get(payer, set())
            progress = int(clause.get("progress", 0))
            if metric == "APPEARANCES" and participant:
                progress += 1
            elif metric == "GOALS" and participant:
                progress += int(goals_by_player.get(player_id, 0))
            clause["progress"] = progress
            threshold = int(clause.get("threshold", 0))
            if threshold > 0 and progress >= threshold:
                amount = _money(clause.get("amount", 0.0))
                settlement_id = self._new_settlement_id(state)
                settlement = {"id": settlement_id, "player_id": player_id,
                              "payer_team": payer, "recipient_team": recipient,
                              "amount": amount, "due_date": match_date[:10],
                              "kind": "TRANSFER_ADDON", "addon_clause_id": clause.get("id"),
                              "status": "PENDING", "attempts": 0}
                state.setdefault("transfer_settlements", []).append(settlement)
                clause["status"] = "TRIGGERED"
                clause["triggered_date"] = match_date[:10]
                clause["settlement_id"] = settlement_id
                state.setdefault("economy_notifications", []).append({
                    "team_id": payer, "date": match_date[:10], "priority": "important",
                    "message": (f"Add-on {metric.lower()} của {clause.get('player_name', player_id)} đã đạt mốc "
                                f"{threshold}; phát sinh khoản thanh toán {amount:.3f} M€.")})
                events.append({"type": "transfer_addon_triggered", "id": clause.get("id"),
                               "amount": amount})
        handled.append(match_id)
        # Keep this idempotency marker bounded in long careers.
        if len(handled) > 3000:
            del handled[:-2000]
        events.extend(self.process_due_transfer_payments(match_date[:10]))
        events.extend(self.expire_transfer_addons(match_date[:10]))
        return events

    def _approach_limit_ok(self, buyer: str, date_text: str) -> bool:
        key = self._window_key(date_text)
        if not key:
            return True
        counts = self.game.s.setdefault("transfer_approach_counts", {})
        current = int(counts.get(f"{key}:{buyer}", 0))
        limit = int(self.config.get("transfer_windows", {}).get("max_approaches_per_window", 5))
        return current < limit

    def request_permission_to_talk(self, buyer_team: str, player_id: str, date_text: str) -> dict:
        self._assert_enabled()
        state = self.game.s
        if buyer_team not in state.get("rosters", {}):
            raise ValueError("CLB mua không tồn tại trong save.")
        player = state.get("players", {}).get(str(player_id))
        if not player:
            raise ValueError("Không tìm thấy cầu thủ.")
        if self._active_loan_for_player(str(player_id)):
            raise ValueError("Cầu thủ đang được cho mượn; không thể tiếp cận hoặc chuyển nhượng trước khi kết thúc khoản mượn.")
        contract = state.get("contracts", {}).get(str(player_id), {})
        seller_team = str(contract.get("team_id") or player.get("club") or "")
        if seller_team == buyer_team and contract.get("status") not in ("EXPIRED", "FREE_AGENT"):
            raise ValueError("Cầu thủ đã thuộc CLB này.")
        free_agent = (contract.get("status") in ("EXPIRED", "FREE_AGENT") or
                      str(player_id) in set(state.get("free_agents", [])) or
                      not seller_team)
        if free_agent:
            seller_team = ""
        if not self._window_open(date_text, free_agent=free_agent):
            raise ValueError("Ngoài cửa sổ chuyển nhượng; hiện chỉ có thể tiếp cận cầu thủ tự do theo cấu hình.")
        if buyer_team == seller_team:
            raise ValueError("Cầu thủ đã thuộc CLB này.")
        if not free_agent and not self._approach_limit_ok(buyer_team, date_text):
            raise ValueError("CLB đã dùng hết lượt xin phép tiếp cận trong cửa sổ này.")
        if not free_agent and not self._deal_limit_ok(buyer_team, date_text):
            raise ValueError("CLB đã đạt giới hạn thương vụ trong cửa sổ này.")
        cooldowns = state.setdefault("transfer_approach_cooldowns", {})
        cooldown_key = f"{buyer_team}:{player_id}"
        previous = cooldowns.get(cooldown_key)
        if previous:
            last_date = date.fromisoformat(str(previous))
            cooldown_days = int(self.config.get("transfer_windows", {}).get("same_player_cooldown_days", 14))
            if (date.fromisoformat(date_text) - last_date).days < cooldown_days:
                raise ValueError(f"Đã tiếp cận cầu thủ này gần đây; cần chờ đủ {cooldown_days} ngày.")

        fee = 0.0
        if seller_team:
            seller_contract = state.get("contracts", {}).get(str(player_id), {})
            seller_finance = state.get("club_finance", {}).get(seller_team, {})
            buyer_finance = state.get("club_finance", {}).get(buyer_team, {})
            years = int(seller_contract.get("years_remaining", 1))
            years_factor = 0.6 if years <= 1 else (0.9 if years == 2 else 1.2)
            role = str(seller_contract.get("squad_role", "ROTATION"))
            role_factor = {"STAR": 1.5, "FIRST_TEAM": 1.2, "ROTATION": 1.0, "BACKUP": 0.7, "PROSPECT": 0.7}.get(role, 1.0)
            market_value = float(player.get("market_value", 0.05))
            buyer_rep_factor = 1.0 + max(0.0, float(buyer_finance.get("reputation", 50)) -
                                        float(seller_finance.get("reputation", 50))) / 1000.0
            fee = max(0.1, market_value * 0.075 * years_factor * role_factor * buyer_rep_factor)
            if years <= 1:
                fee *= 0.2
            fee = _money(fee)

            # A selling club may refuse access to a key player, but refusal does not
            # take money. Squad minimum is enforced at transfer completion instead.
            team_tier = seller_finance.get("tier", "small")
            seller_is_top = False
            if str(seller_contract.get("squad_role")) == "STAR" and team_tier == "big":
                try:
                    current_tier = self.game.s.get("team_tiers", {}).get(seller_team)
                    if current_tier == "premier":
                        table = self.game.league_table("premier")
                        seller_is_top = any(row.get("team") == seller_team and i < 3 for i, row in enumerate(table))
                except Exception:
                    seller_is_top = False
            if seller_is_top:
                state.setdefault("economy_notifications", []).append({
                    "team_id": buyer_team, "date": date_text, "priority": "important",
                    "message": f"{seller_team} từ chối cho tiếp cận ngôi sao {player.get('common_name', player_id)}.",
                })
                return {"accepted": False, "reason": "CLB chủ từ chối tiếp cận ngôi sao.", "approach_fee": 0.0}
            buyer_balance = float(buyer_finance.get("balance", 0))
            transfer_budget = float(buyer_finance.get("transfer_budget", 0))
            if buyer_balance < fee or transfer_budget < fee:
                raise ValueError("Không đủ tiền mặt hoặc ngân sách để trả phí tiếp cận.")
            FinanceService.post(state, buyer_team, "TAMPERING_COMPENSATION", -fee,
                                season=int(state.get("season", 0)), month=int(date_text[5:7]),
                                description=f"Phí tiếp cận {player.get('common_name', player_id)}", related_id=str(player_id), date=date_text)
            FinanceService.post(state, seller_team, "TAMPERING_COMPENSATION", fee,
                                season=int(state.get("season", 0)), month=int(date_text[5:7]),
                                description=f"Bồi thường tiếp cận cầu thủ {player.get('common_name', player_id)}", related_id=str(player_id), date=date_text)
            buyer_finance["transfer_budget"] = _money(transfer_budget - fee)
            seller_finance["transfer_budget"] = _money(float(seller_finance.get("transfer_budget", 0)) + fee)
            counts = state.setdefault("transfer_approach_counts", {})
            window_key = self._window_key(date_text)
            if window_key:
                counter_key = f"{window_key}:{buyer_team}"
                counts[counter_key] = int(counts.get(counter_key, 0)) + 1
        cooldowns[cooldown_key] = date_text
        negotiation = self.start_contract_negotiation(
            buyer_team, str(player_id), date_text, negotiation_type="NEW_SIGNING" if free_agent else "TAPPING_UP",
            approach_fee=fee,
        )
        state.setdefault("economy_notifications", []).append({
            "team_id": buyer_team, "date": date_text, "priority": "normal",
            "message": f"Đã mở đàm phán cá nhân với {player.get('common_name', player_id)}; phí tiếp cận {fee:.3f} M€.",
        })
        return {"accepted": True, "approach_fee": fee, "seller_team": seller_team,
                "negotiation": negotiation}

    def request_pre_contract(self, buyer_team: str, player_id: str, date_text: str) -> dict:
        """Open a Bosman-style negotiation for a player in the final six months.

        The player stays with the selling club until the current season closes.
        To protect the 23-player minimum, the seller must be able to release the
        player without violating squad-size or positional rules.
        """
        self._assert_enabled()
        state = self.game.s
        current = date.fromisoformat(date_text)
        if current.month < 12:
            raise ValueError("Chỉ được ký trước hợp đồng từ tháng 12 trở đi.")
        if buyer_team not in state.get("rosters", {}):
            raise ValueError("CLB mua không tồn tại trong save.")
        player_id = str(player_id)
        player = state.get("players", {}).get(player_id)
        contract = state.get("contracts", {}).get(player_id, {})
        if self._active_loan_for_player(player_id):
            raise ValueError("Cầu thủ đang được cho mượn; không thể ký trước hợp đồng.")
        seller = str(contract.get("team_id") or player.get("club", "")) if player else ""
        if not player or not seller or seller == buyer_team or seller not in state.get("rosters", {}):
            raise ValueError("Cầu thủ không phải cầu thủ đang có hợp đồng ở CLB khác.")
        if int(contract.get("years_remaining", 99)) > 1 or contract.get("status") not in ("ACTIVE", "EXPIRING"):
            raise ValueError("Cầu thủ chưa bước vào sáu tháng cuối hợp đồng.")
        seller_trial = [pid for pid in state["rosters"][seller] if pid != player_id]
        if not self.squad_rules.validate(seller_trial, state.get("players", {}))["ok"]:
            raise ValueError("CLB chủ không thể để cầu thủ ra đi mà vẫn giữ đủ 23 người và các vị trí tối thiểu.")
        if len(state["rosters"][buyer_team]) >= int(self.config.get("squad_rules", {}).get("max_size", 30)):
            raise ValueError("CLB mua đã đủ 30 cầu thủ.")
        existing = [row for row in state.get("pre_contract_agreements", [])
                    if row.get("player_id") == player_id and row.get("status") in ("PENDING", "READY")]
        if existing:
            raise ValueError("Cầu thủ đã có thỏa thuận ký trước hợp đồng đang chờ.")
        negotiation = self.start_contract_negotiation(
            buyer_team, player_id, date_text, negotiation_type="PRE_CONTRACT", approach_fee=0.0)
        negotiation["joining_season"] = int(state.get("season", 0)) + 1
        negotiation["seller_team"] = seller
        state.setdefault("economy_notifications", []).append({
            "team_id": buyer_team, "date": date_text, "priority": "important",
            "message": f"Đã mở đàm phán ký trước hợp đồng với {player.get('common_name', player_id)}; không có phí chuyển nhượng hay phí tiếp cận.",
        })
        return {"accepted": True, "approach_fee": 0.0, "seller_team": seller,
                "negotiation": negotiation, "pre_contract": True}

    def _store_pre_contract_agreement(self, negotiation: dict, date_text: str) -> dict:
        state = self.game.s
        pid = str(negotiation["player_id"])
        player = state["players"][pid]
        agreement = {
            "player_id": pid, "player_name": player.get("common_name", pid),
            "from_team": negotiation.get("seller_team") or negotiation.get("from_team"),
            "to_team": negotiation["team_id"], "joining_season": int(negotiation.get("joining_season", int(state.get("season", 0))+1)),
            "agreed_date": date_text, "club_offer": copy.deepcopy(negotiation["club_offer"]),
            "status": "PENDING", "negotiation_id": negotiation["id"],
        }
        state.setdefault("pre_contract_agreements", []).append(agreement)
        negotiation["status"] = "PRE_CONTRACT_AGREED"
        negotiation["personal_agreement"] = True
        state.setdefault("economy_notifications", []).append({
            "team_id": agreement["to_team"], "date": date_text, "priority": "important",
            "message": f"Đã ký thỏa thuận trước hợp đồng với {agreement['player_name']}; cầu thủ sẽ gia nhập mùa {agreement['joining_season']}.",
        })
        return agreement

    def activate_pre_contracts(self, season: int) -> list[dict]:
        """Activate agreements whose players have become free agents."""
        state = self.game.s
        activated = []
        default_date = (state.get("slots") or [{}])[0].get("date", f"{int(season):04d}-08-01")
        for agreement in state.get("pre_contract_agreements", []):
            if agreement.get("status") != "PENDING" or int(agreement.get("joining_season", 0)) > int(season):
                continue
            pid, buyer = str(agreement.get("player_id")), str(agreement.get("to_team"))
            if any(pid in ids for ids in state.get("rosters", {}).values()):
                # The selling team may be unable to release this player without a
                # replacement. Keep the agreement pending; never violate squad rules.
                continue
            contract = state.get("contracts", {}).get(pid, {})
            if contract.get("status") not in ("EXPIRED", "FREE_AGENT") and pid not in state.get("free_agents", []):
                continue
            negotiation = next((n for n in state.get("contract_negotiations", [])
                               if n.get("id") == agreement.get("negotiation_id")), None)
            if not negotiation or not negotiation.get("club_offer"):
                agreement["status"] = "FAILED"
                continue
            negotiation["status"] = "AGREED"
            self._complete_free_agent_signing(negotiation, str(default_date))
            history = state.get("transfer_history", [])
            if history and history[-1].get("player_id") == pid:
                history[-1]["from_team"] = f"Ký trước hợp đồng ({agreement.get('from_team') or 'Free agent'})"
                history[-1]["status"] = "Pre-contract hoàn tất"
                history[-1]["pre_contract"] = True
            agreement["status"] = "COMPLETED"
            agreement["completed_date"] = str(default_date)
            activated.append(agreement)
        return activated

    def start_contract_negotiation(self, team: str, player_id: str, date_text: str,
                                   negotiation_type: str = "NEW_SIGNING", approach_fee: float = 0.0) -> dict:
        state = self.game.s
        player = state["players"][player_id]
        meta = state.get("player_meta", {}).get(player_id, {})
        age = float(meta.get("age", 26))
        ovr = float(player.get("overall_rating", 60))
        current_wage = max(0.05, float(player.get("wage", 0.05)))
        finance = state["club_finance"][team]
        reputation_discount = max(-0.10, min(0.08, (float(finance.get("reputation", 50)) - 65.0) / 400.0))
        max_wage = float(self.config.get("wages", {}).get("max_annual_wage", 35.0))
        demand_wage = _money(min(max_wage, current_wage * (1.0 + reputation_discount) * (1.08 if ovr >= 85 else 1.0)))
        if age <= 24: years = 4
        elif age <= 29: years = 3
        elif age <= 32: years = 2
        else: years = 1
        demand_bonus = _money(demand_wage * years * 0.08)
        days = 1 if age <= 20 else (5 if ovr < 80 else 10)
        start = date.fromisoformat(date_text)
        seq = int(state.get("contract_negotiation_seq", 0)) + 1
        state["contract_negotiation_seq"] = seq
        item = {
            "id": f"NEG-{int(state.get('season', 0))}-{seq:06d}", "player_id": player_id,
            "team_id": team, "from_team": str(state.get("contracts", {}).get(player_id, {}).get("team_id", "")),
            "type": negotiation_type, "start_date": date_text, "due_date": (start + timedelta(days=days)).isoformat(),
            "duration_days_required": days, "round": 0, "max_rounds": 3,
            "player_demand": {"wage": demand_wage, "years": years, "signing_bonus": demand_bonus,
                               "role": "FIRST_TEAM" if ovr >= 75 else "ROTATION"},
            "club_offer": None, "player_mood": max(30, min(95, int(55 + reputation_discount * 100))),
            "approach_fee": _money(approach_fee), "status": "IN_PROGRESS",
            "personal_agreement": False,
        }
        state.setdefault("contract_negotiations", []).append(item)
        return item

    def submit_contract_offer(self, negotiation_id: str, wage: float, years: int,
                              signing_bonus: float, date_text: str) -> dict:
        state = self.game.s
        item = next((n for n in state.get("contract_negotiations", []) if n.get("id") == negotiation_id), None)
        if not item or item.get("status") not in ("IN_PROGRESS", "COUNTERED"):
            raise ValueError("Cuộc đàm phán không còn hoạt động.")
        team = item["team_id"]
        finance = state["club_finance"][team]
        wage, signing_bonus, years = _money(wage), _money(signing_bonus), int(years)
        demand = item["player_demand"]
        exclude_pid = item["player_id"] if item.get("type") == "RENEWAL" else None
        wage_room = float(finance.get("wage_budget", 0)) - self._annual_wage_committed(team, exclude_pid)
        max_wage = float(self.config.get("wages", {}).get("max_annual_wage", 35.0))
        if wage <= 0 or wage > max_wage:
            raise ValueError(f"Lương đề nghị phải lớn hơn 0 và không vượt {max_wage:.2f} M€/năm.")
        if wage > wage_room:
            raise ValueError(f"Đề nghị lương vượt quỹ lương còn lại ({max(0,wage_room):.3f} M€/năm).")
        agent_estimate = _money((signing_bonus + float(state["players"][item["player_id"]].get("market_value", 0))) * 0.05)
        if signing_bonus + agent_estimate > float(finance.get("balance", 0)) or signing_bonus + agent_estimate > float(finance.get("transfer_budget", 0)):
            raise ValueError("Không đủ ngân sách cho lót tay và phí đại diện.")
        if years < 1 or years > 5:
            raise ValueError("Thời hạn hợp đồng phải từ 1 đến 5 năm.")
        item["round"] = int(item.get("round", 0)) + 1
        item["club_offer"] = {"wage": wage, "years": years, "signing_bonus": signing_bonus,
                              "role": demand.get("role", "ROTATION")}
        ratio = wage / max(0.001, float(demand.get("wage", wage)))
        years_ok = abs(years - int(demand.get("years", years))) <= 1
        if ratio >= 0.95 and years_ok:
            item["status"] = "AGREED"
            item["personal_agreement"] = True
            if item.get("type") == "NEW_SIGNING":
                self._complete_free_agent_signing(item, date_text)
            elif item.get("type") == "RENEWAL":
                self._complete_renewal(item, date_text)
            elif item.get("type") == "PRE_CONTRACT":
                agreement = self._store_pre_contract_agreement(item, date_text)
                return {"status": item["status"], "message": "Đã đạt thỏa thuận ký trước hợp đồng; cầu thủ sẽ gia nhập sau khi hợp đồng cũ kết thúc.", "agreement": agreement}
            return {"status": item["status"], "message": "Cầu thủ đã đồng ý điều khoản cá nhân."}
        if ratio >= 0.80 and int(item["round"]) < int(item.get("max_rounds", 3)):
            demand["wage"] = _money(float(demand["wage"]) * 0.96)
            item["player_mood"] = max(0, int(item.get("player_mood", 50)) - 5)
            item["status"] = "COUNTERED"
            return {"status": item["status"], "counter_wage": demand["wage"],
                    "message": "Cầu thủ phản hồi với mức lương yêu cầu thấp hơn một chút."}
        item["status"] = "FAILED"
        item["player_mood"] = max(0, int(item.get("player_mood", 50)) - 15)
        return {"status": item["status"], "message": "Đàm phán thất bại; cầu thủ từ chối đề nghị."}

    def _complete_free_agent_signing(self, negotiation: dict, date_text: str):
        state = self.game.s
        team, pid = negotiation["team_id"], negotiation["player_id"]
        player = state["players"][pid]
        roster = state["rosters"][team]
        if len(roster) >= int(self.config.get("squad_rules", {}).get("max_size", 30)):
            raise ValueError("Đội hình đã đủ 30 cầu thủ.")
        if any(pid in ids for other, ids in state.get("rosters", {}).items()):
            raise ValueError("Cầu thủ vẫn đang đăng ký ở một đội; không thể ký như cầu thủ tự do.")
        offer = negotiation["club_offer"]
        if offer["signing_bonus"]:
            FinanceService.post(state, team, "SIGNING_BONUS", -offer["signing_bonus"],
                                season=int(state.get("season", 0)), month=int(date_text[5:7]),
                                description=f"Lót tay ký {player.get('common_name', pid)}", related_id=pid, date=date_text)
            state["club_finance"][team]["transfer_budget"] = _money(
                float(state["club_finance"][team]["transfer_budget"]) - offer["signing_bonus"])
        agent_fee = _money((offer["signing_bonus"] + float(player.get("market_value", 0))) * 0.05)
        if agent_fee:
            FinanceService.post(state, team, "AGENT_FEE", -agent_fee, season=int(state.get("season", 0)),
                                month=int(date_text[5:7]), description=f"Phí đại diện · {player.get('common_name', pid)}",
                                related_id=pid, date=date_text)
            state["club_finance"][team]["transfer_budget"] = _money(
                float(state["club_finance"][team]["transfer_budget"]) - agent_fee)
        history = state.setdefault("contract_history", {}).setdefault(pid, [])
        if state.get("contracts", {}).get(pid):
            history.append(copy.deepcopy(state["contracts"][pid]))
        roster.append(pid)
        player["club"] = team
        player["wage"] = offer["wage"]
        state.setdefault("contracts", {})[pid] = {
            "id": f"CT-{int(state.get('season',0))}-{pid}-NEW", "player_id": pid, "team_id": team,
            "start_season": int(state.get("season", 0)), "end_season": int(state.get("season", 0)) + offer["years"] - 1,
            "years_total": offer["years"], "years_remaining": offer["years"],
            "wage_per_year": offer["wage"], "signing_bonus": offer["signing_bonus"],
            "squad_role": offer.get("role", "ROTATION"), "status": "ACTIVE", "initialized": False,
        }
        if pid in state.get("free_agents", []): state["free_agents"].remove(pid)
        self.release_expired_players(team)
        state.setdefault("transfer_history", []).append({
            "date": date_text, "player_id": pid, "player_name": player.get("common_name", pid),
            "from_team": "Free agent", "to_team": team, "fee": 0.0, "status": "Hoàn tất",
            "wage": offer["wage"], "years": offer["years"], "signing_bonus": offer["signing_bonus"],
        })

    def retention_score(self, team: str, player_id: str) -> float:
        """Estimate renewal priority from ability, form, influence and scarcity."""
        state = self.game.s
        player = state.get("players", {}).get(player_id, {})
        meta = state.get("player_meta", {}).get(player_id, {})
        dyn = state.get("dyn", {}).get(player_id, {})
        stats = state.get("season_stats", {}).get(player_id, {})
        ids = list(state.get("rosters", {}).get(team, []))
        ratings = sorted((float(state["players"].get(pid, {}).get("overall_rating", 50)) for pid in ids), reverse=True)
        ovr = float(player.get("overall_rating", 50))
        rank = next((i + 1 for i, value in enumerate(ratings) if value == ovr), len(ratings) or 1)
        skill = max(0.0, min(100.0, 100.0 * (1.0 - (rank - 1) / max(1, len(ratings)))))
        form = max(0.0, min(100.0, 50.0 + float(dyn.get("form", 0.0)) * 10.0))
        influence = max(0.0, min(100.0, 100.0 * (stats.get("apps", 0) / 38.0) * 0.65 + skill * 0.35))
        age = float(meta.get("age", 26))
        potential = float(meta.get("potential", ovr))
        potential_score = max(0.0, min(100.0, potential + max(0, 26-age)*1.5 - max(0, age-30)*2.0))
        try:
            group = self.squad_rules.validate(ids, state.get("players", {})).get("groups", {})
            pos_group = position_group(player.get("position", ""))
            scarcity = max(0.0, min(100.0, 100.0 - (float(group.get(pos_group, 0)) - 4) * 12.0))
        except Exception:
            scarcity = 50.0
        return round(0.35 * skill + 0.25 * form + 0.20 * influence + 0.10 * potential_score + 0.10 * scarcity, 2)

    def begin_renewal(self, team: str, player_id: str, date_text: str) -> dict:
        self._assert_enabled()
        contract = self.game.s.get("contracts", {}).get(player_id)
        if not contract or contract.get("team_id") != team or contract.get("status") not in ("ACTIVE", "EXPIRING", "EXPIRED_PENDING"):
            raise ValueError("Cầu thủ không có hợp đồng phù hợp để gia hạn.")
        if int(contract.get("years_remaining", 0)) > 1:
            raise ValueError("Chỉ mở gia hạn khi hợp đồng còn tối đa 1 năm.")
        item = self.start_contract_negotiation(team, player_id, date_text, negotiation_type="RENEWAL")
        score = self.retention_score(team, player_id)
        age = float(self.game.s.get("player_meta", {}).get(player_id, {}).get("age", 26))
        if age >= 33: years = 2 if age >= 35 else 3
        elif score >= 75: years = 5 if age <= 28 else 4
        elif score >= 55: years = 4 if age <= 29 else 3
        else: years = 3 if age < 32 else 1
        old_wage = float(contract.get("wage_per_year", 0.05))
        increase = 1.15 if score >= 75 else (1.03 if score >= 55 else 1.0)
        item["retention_score"] = score
        max_wage = float(self.config.get("wages", {}).get("max_annual_wage", 35.0))
        item["recommended_offer"] = {
            "wage": _money(min(max_wage, max(old_wage, float(item["player_demand"]["wage"])) * increase)),
            "years": years,
            "signing_bonus": _money(max(0.0, old_wage * years * 0.04)),
            "role": contract.get("squad_role", "ROTATION"),
        }
        item["player_demand"]["wage"] = item["recommended_offer"]["wage"]
        item["player_demand"]["years"] = item["recommended_offer"]["years"]
        # Renewal negotiations get a shorter deadline than new signings.
        item["duration_days_required"] = max(1, int(round(item["duration_days_required"] * 0.7)))
        item["due_date"] = (date.fromisoformat(date_text) + timedelta(days=item["duration_days_required"])).isoformat()
        return item

    def _complete_renewal(self, negotiation: dict, date_text: str):
        state = self.game.s
        pid, team = negotiation["player_id"], negotiation["team_id"]
        contract = state.get("contracts", {}).get(pid)
        if not contract or contract.get("team_id") != team:
            raise ValueError("Hợp đồng cũ không còn thuộc CLB này.")
        terms = negotiation["club_offer"]
        fee_bonus = float(terms.get("signing_bonus", 0))
        market_value = float(state["players"].get(pid, {}).get("market_value", 0))
        agent_fee = _money((fee_bonus + market_value) * 0.05)
        total = fee_bonus + agent_fee
        finance = state["club_finance"][team]
        if float(finance.get("balance", 0)) < total or float(finance.get("transfer_budget", 0)) < total:
            raise ValueError("Không đủ ngân sách cho lót tay/phí đại diện khi gia hạn.")
        state.setdefault("contract_history", {}).setdefault(pid, []).append(copy.deepcopy(contract))
        if fee_bonus:
            FinanceService.post(state, team, "SIGNING_BONUS", -fee_bonus, season=int(state.get("season", 0)),
                                month=int(date_text[5:7]), description=f"Lót tay gia hạn {state['players'][pid].get('common_name', pid)}",
                                related_id=pid, date=date_text)
        if agent_fee:
            FinanceService.post(state, team, "AGENT_FEE", -agent_fee, season=int(state.get("season", 0)),
                                month=int(date_text[5:7]), description=f"Phí đại diện gia hạn · {state['players'][pid].get('common_name', pid)}",
                                related_id=pid, date=date_text)
        finance["transfer_budget"] = _money(float(finance.get("transfer_budget", 0)) - total)
        contract.update({
            "id": f"CT-{int(state.get('season',0))}-{pid}-REN", "start_season": int(state.get("season", 0)),
            "end_season": int(state.get("season", 0)) + int(terms["years"]) - 1,
            "years_total": int(terms["years"]), "years_remaining": int(terms["years"]),
            "wage_per_year": float(terms["wage"]), "signing_bonus": fee_bonus,
            "squad_role": terms.get("role", contract.get("squad_role", "ROTATION")),
            "status": "ACTIVE", "initialized": False,
        })
        state["players"][pid]["wage"] = float(terms["wage"])
        negotiation["status"] = "AGREED"

    def terminate_contract(self, team: str, player_id: str, date_text: str,
                           compensation_pct: float | None = None) -> dict:
        """Terminate an active contract with configured severance, safely releasing the player.

        The default severance is 50% of the remaining guaranteed annual wages.
        A player is released as a free agent; if removing them would break the
        23–30 squad/position constraints, a youth replacement is staged and
        committed atomically with the termination.
        """
        self._assert_enabled()
        state = self.game.s
        pid = str(player_id)
        contract = state.get("contracts", {}).get(pid, {})
        contract_id = str(contract.get("id") or pid)
        termination_key = f"termination:{team}:{pid}:{contract_id}"
        previous = next((row for row in state.setdefault("contract_terminations", [])
                         if row.get("idempotency_key") == termination_key), None)
        if previous:
            return previous
        try:
            date.fromisoformat(str(date_text))
        except (TypeError, ValueError) as exc:
            raise ValueError("Ngày chấm dứt hợp đồng phải theo định dạng YYYY-MM-DD.") from exc
        if team not in state.get("rosters", {}) or pid not in state["rosters"].get(team, []):
            raise ValueError("Cầu thủ không thuộc đội hình CLB hiện tại.")
        if self._active_loan_for_player(pid):
            raise ValueError("Không thể chấm dứt hợp đồng của cầu thủ đang được cho mượn.")
        if str(contract.get("team_id")) != str(team) or contract.get("status") not in ("ACTIVE", "EXPIRING"):
            raise ValueError("Chỉ có thể chấm dứt hợp đồng còn hiệu lực và do CLB sở hữu.")
        try:
            pct = (float(self.config.get("contracts", {}).get("termination_compensation_pct", 50.0))
                   if compensation_pct is None else float(compensation_pct))
        except (TypeError, ValueError) as exc:
            raise ValueError("Tỷ lệ bồi thường chấm dứt hợp đồng không hợp lệ.") from exc
        if not math.isfinite(pct) or not 0.0 <= pct <= 100.0:
            raise ValueError("Tỷ lệ bồi thường phải nằm trong khoảng 0–100%.")
        remaining = max(0, int(contract.get("years_remaining", 0)))
        annual_wage = max(0.0, float(contract.get("wage_per_year", state["players"].get(pid, {}).get("wage", 0.0))))
        compensation = _money(annual_wage * remaining * pct / 100.0)

        replacement = self._prepare_seller_replacement(team, pid)
        roster_after = [row for row in state["rosters"][team] if row != pid]
        players_after = dict(state.get("players", {}))
        if replacement:
            players_after[replacement["player_id"]] = replacement["player"]
            roster_after.append(replacement["player_id"])
        validation = self.squad_rules.validate(roster_after, players_after)
        if not validation["ok"]:
            raise ValueError("Không thể chấm dứt hợp đồng vì đội hình sau thay thế không hợp lệ: " + "; ".join(validation["errors"]))

        finance = state.get("club_finance", {}).get(team, {})
        if compensation > float(finance.get("balance", 0.0)) + 1e-9:
            raise ValueError(f"CLB không đủ tiền bồi thường {compensation:.3f} M€; số dư hiện tại {float(finance.get('balance', 0.0)):.3f} M€.")

        if compensation:
            month = int(str(date_text)[5:7])
            season = int(str(date_text)[:4]) if month >= 8 else int(str(date_text)[:4]) - 1
            FinanceService.post(state, team, "CONTRACT_TERMINATION", -compensation,
                                season=season, month=month,
                                description=f"Bồi thường chấm dứt hợp đồng · {state['players'].get(pid, {}).get('common_name', pid)}",
                                related_id=pid, date=date_text,
                                idempotency_key=termination_key)

        state["rosters"][team].remove(pid)
        if replacement:
            self._commit_seller_replacement(team, replacement, date_text)
        contract.update({"status": "TERMINATED", "team_id": None, "years_remaining": 0,
                         "terminated_date": date_text, "termination_compensation": compensation,
                         "termination_compensation_pct": pct})
        player = state.get("players", {}).get(pid, {})
        player["club"] = ""
        free_agents = state.setdefault("free_agents", [])
        if pid not in free_agents:
            free_agents.append(pid)
        # Unaccepted approaches/offers cannot remain actionable after release.
        for negotiation in state.get("contract_negotiations", []):
            if str(negotiation.get("player_id")) == pid and negotiation.get("status") in ("IN_PROGRESS", "COUNTERED"):
                negotiation["status"] = "CANCELLED"
                negotiation["failure_reason"] = "Hợp đồng cũ đã được chấm dứt."
        for offer in state.get("transfer_offers", []):
            if str(offer.get("player_id")) == pid and offer.get("status") in ("PENDING", "COUNTERED", "ACCEPTED"):
                offer["status"] = "CANCELLED"
                offer["resolved_date"] = date_text
        for agreement in state.get("pre_contract_agreements", []):
            if str(agreement.get("player_id")) == pid and agreement.get("status") == "PENDING":
                agreement["status"] = "CANCELLED"
                agreement["cancelled_date"] = date_text
                agreement["cancel_reason"] = "Hợp đồng hiện tại đã bị chấm dứt."
        state["transfer_list"] = [row for row in state.get("transfer_list", [])
                                   if str(row.get("player_id")) != pid]
        for clause in state.get("sell_on_clauses", []):
            if str(clause.get("player_id")) == pid and clause.get("status") == "ACTIVE":
                clause["status"] = "CANCELLED"
                clause["cancelled_date"] = date_text
                clause["cancel_reason"] = "Cầu thủ được giải phóng khỏi hợp đồng thay vì chuyển nhượng có phí."
        self._cancel_active_addons_for_player(
            pid, date_text, "Cầu thủ đã được giải phóng khỏi CLB thanh toán.")
        row = {"idempotency_key": termination_key, "date": date_text, "team_id": team,
               "player_id": pid, "player_name": player.get("common_name", pid),
               "remaining_years": remaining, "annual_wage": _money(annual_wage),
               "compensation_pct": pct, "compensation": compensation,
               "replacement_player_id": replacement.get("player_id") if replacement else None,
               "status": "COMPLETED"}
        state["contract_terminations"].append(row)
        state.setdefault("economy_notifications", []).append({
            "team_id": team, "date": date_text, "priority": "important",
            "message": (f"Đã chấm dứt hợp đồng {player.get('common_name', pid)}; bồi thường {compensation:.3f} M€ và "
                        + ("đã đôn cầu thủ học viện thay thế." if replacement else "đội hình vẫn hợp lệ.")),
        })
        return row

    def release_expired_players(self, team: str) -> list[str]:
        """Release expired players only when 23–30 and positional minima stay valid."""
        state = self.game.s
        released = []
        roster = state.get("rosters", {}).get(team, [])
        for pid in list(roster):
            contract = state.get("contracts", {}).get(pid, {})
            if contract.get("status") != "EXPIRED_PENDING":
                continue
            if self._active_loan_for_player(pid):
                # An expired parent contract cannot be detached from an active
                # loan registration; it is released only after the player returns.
                continue
            trial = [x for x in roster if x != pid]
            if not self.squad_rules.validate(trial, state.get("players", {}))["ok"]:
                continue
            roster.remove(pid)
            contract["status"] = "EXPIRED"
            contract["team_id"] = None
            player = state.get("players", {}).get(pid, {})
            player["club"] = ""
            free_agents = state.setdefault("free_agents", [])
            if pid not in free_agents:
                free_agents.append(pid)
            released.append(pid)
            state.setdefault("economy_notifications", []).append({
                "team_id": team, "date": str(state.get("economy_last_date", "")),
                "priority": "normal", "message": f"{player.get('common_name', pid)} hết hợp đồng và trở thành cầu thủ tự do.",
            })
        return released

    def roll_contract_years(self, season: int) -> dict:
        """Run once at season end; contracts expire only when squad rules permit release."""
        state = self.game.s
        processed = state.setdefault("contracts_rolled_seasons", [])
        season = int(season)
        if season in processed:
            return {"expired": [], "pending": [], "released": []}
        expired, pending, released = [], [], []
        for team, ids in state.get("rosters", {}).items():
            for pid in list(ids):
                contract = state.get("contracts", {}).get(pid)
                if not isinstance(contract, dict) or contract.get("status") not in ("ACTIVE", "EXPIRING", "EXPIRED_PENDING", "ON_LOAN"):
                    continue
                remaining = max(0, int(contract.get("years_remaining", 1)) - 1)
                contract["years_remaining"] = remaining
                if remaining == 0:
                    contract["status"] = "EXPIRED_PENDING"
                    expired.append(pid)
                    pending.append(pid)
                elif remaining == 1 and contract.get("status") != "ON_LOAN":
                    contract["status"] = "EXPIRING"
                    state.setdefault("economy_notifications", []).append({
                        "team_id": team, "date": f"{season+1:04d}-05-31", "priority": "important",
                        "message": f"Hợp đồng của {state['players'].get(pid, {}).get('common_name', pid)} còn 1 năm.",
                    })
            released.extend(self.release_expired_players(team))
        processed.append(season)
        return {"expired": expired, "pending": [pid for pid in pending if pid not in released], "released": released}

    def analyze_squad(self, team: str, target_ovr_override: float | None = None) -> dict:
        """Return position needs, surplus players and expiring deals for an AI/UI selector."""
        state = self.game.s
        roster = list(state.get("rosters", {}).get(team, []))
        finance = state.get("club_finance", {}).get(team, {})
        tier = str(finance.get("tier", "small"))
        target_ovr = (float(target_ovr_override) if target_ovr_override is not None
                      else {"big": 82, "medium": 76, "small": 70}.get(tier, 70))
        min_groups = self.config.get("squad_rules", {}).get("minimum_groups", {"GK": 3, "DEF": 7, "MID": 7, "FWD": 4})
        by_group = {group: [] for group in ("GK", "DEF", "MID", "FWD")}
        expiring = []
        for pid in roster:
            player = state.get("players", {}).get(pid, {})
            group = position_group(player.get("position", ""))
            by_group.setdefault(group, []).append(pid)
            contract = state.get("contracts", {}).get(pid, {})
            if int(contract.get("years_remaining", 99)) <= 1:
                expiring.append(pid)
        needs = []
        for group in ("GK", "DEF", "MID", "FWD"):
            ids = by_group.get(group, [])
            ratings = sorted((float(state["players"].get(pid, {}).get("overall_rating", 50)) for pid in ids), reverse=True)
            current_min = int(min_groups.get(group, 0))
            average_core = sum(ratings[:2]) / max(1, min(2, len(ratings)))
            missing = max(0, current_min - len(ids))
            gap = max(0.0, target_ovr - average_core)
            if missing or gap >= 3:
                needs.append({"position": group, "priority": min(5, 3 + (2 if missing else 0) + (1 if gap >= 8 else 0)),
                              "count": len(ids), "minimum": current_min,
                              "average_top_two_ovr": round(average_core, 1), "min_ovr": target_ovr,
                              "shortage": missing})
        surplus = []
        if len(roster) >= 23:
            for pid in roster:
                player = state.get("players", {}).get(pid, {})
                contract = state.get("contracts", {}).get(pid, {})
                meta = state.get("player_meta", {}).get(pid, {})
                group = position_group(player.get("position", ""))
                count = len(by_group.get(group, []))
                if count <= int(min_groups.get(group, 0)):
                    continue
                surplus.append({"player_id": pid, "name": player.get("common_name", pid),
                                "position": player.get("position", ""),
                                "ovr": float(player.get("overall_rating", 50)),
                                "age": float(meta.get("age", 26)),
                                "wage": float(contract.get("wage_per_year", player.get("wage", 0))),
                                "market_value": float(player.get("market_value", 0)),
                                "sell_score": round(max(0.0, (float(meta.get("age", 26))-29)*1.5) +
                                                     max(0.0, 78-float(player.get("overall_rating", 50))) +
                                                     max(0.0, float(contract.get("wage_per_year", 0))-5)*0.8, 2)})
            surplus.sort(key=lambda row: row["sell_score"], reverse=True)
        return {"team": team, "tier": tier, "target_ovr": target_ovr, "squad_size": len(roster),
                "needs": sorted(needs, key=lambda row: (-row["priority"], row["position"])),
                "surplus_candidates": surplus, "expiring_contracts": expiring,
                "wage_room": _money(float(finance.get("wage_budget", 0)) - self._annual_wage_committed(team)),
                "transfer_budget": _money(finance.get("transfer_budget", 0))}

    def target_score(self, team: str, player_id: str, need: dict | None = None) -> float:
        state = self.game.s
        player = state.get("players", {}).get(player_id, {})
        finance = state.get("club_finance", {}).get(team, {})
        value = float(player.get("market_value", 0))
        wage = float(state.get("contracts", {}).get(player_id, {}).get("wage_per_year", player.get("wage", 0)))
        wage_room = self._annual_wage_committed(team)
        wage_room = float(finance.get("wage_budget", 0)) - wage_room
        if value > float(finance.get("transfer_budget", 0)) or wage > wage_room:
            return 0.0
        group = position_group(player.get("position", ""))
        if need and need.get("position") not in (None, group):
            return 0.0
        meta = state.get("player_meta", {}).get(player_id, {})
        ovr = float(player.get("overall_rating", 50))
        target_ovr = float((need or {}).get("min_ovr", 75))
        fit = max(0.0, min(100.0, 70.0 + (ovr-target_ovr)*4.0))
        age = float(meta.get("age", 26))
        age_fit = max(0.0, min(100.0, 100.0 - abs(age-26)*5.0))
        pot = float(meta.get("potential", ovr))
        value_score = max(0.0, min(100.0, 50.0 + (pot-ovr)*2.0 - math.log1p(max(0.0, value))*5.0))
        cost_score = max(0.0, min(100.0, 100.0 - value/max(1.0, float(finance.get("transfer_budget", 1)))*45.0))
        return round(0.35*fit + 0.25*value_score + 0.20*age_fit + 0.20*cost_score, 2)

    def suggest_targets(self, team: str, position: str | None = None, limit: int = 10,
                        need_override: dict | None = None) -> list[dict]:
        state = self.game.s
        analysis = self.analyze_squad(team)
        needs = analysis["needs"]
        need = (need_override if isinstance(need_override, dict) else
                (next((item for item in needs if item["position"] == position), None)
                 if position else (needs[0] if needs else None)))
        out = []
        for pid, player in state.get("players", {}).items():
            if self._active_loan_for_player(pid):
                continue
            contract = state.get("contracts", {}).get(pid, {})
            seller = contract.get("team_id") or player.get("club", "")
            free_agent = contract.get("status") in ("EXPIRED", "FREE_AGENT") or pid in state.get("free_agents", []) or not seller
            if seller == team and not free_agent:
                continue
            if not free_agent and seller not in state.get("rosters", {}):
                continue
            group = position_group(player.get("position", ""))
            if position and group != position:
                continue
            score = self.target_score(team, pid, need)
            if score <= 0:
                continue
            meta = state.get("player_meta", {}).get(pid, {})
            out.append({"player_id": pid, "name": player.get("common_name", pid), "club": "Free Agent" if free_agent else seller,
                        "position": player.get("position", ""), "group": group,
                        "age": float(meta.get("age", 26)), "ovr": float(player.get("overall_rating", 50)),
                        "potential": float(meta.get("potential", player.get("overall_rating", 50))),
                        "market_value": float(player.get("market_value", 0)),
                        "wage": float(contract.get("wage_per_year", player.get("wage", 0))),
                        "score": score, "free_agent": free_agent})
        return sorted(out, key=lambda row: (-row["score"], row["market_value"]))[:max(1, int(limit))]

    def complete_loan(self, borrower_team: str, player_id: str, loan_fee: float,
                      wage_share_pct: float, end_date: str, date_text: str,
                      option_to_buy: float | None = None) -> dict:
        """Register a temporary loan, preserving the parent's original contract.

        ``wage_share_pct`` is the portion paid by the borrowing club; the parent
        pays the remaining percentage while the player is away.
        """
        self._assert_enabled()
        state = self.game.s
        start = date.fromisoformat(str(date_text))
        end = date.fromisoformat(str(end_date))
        if not self.window_service.is_open(start.month):
            raise ValueError("Chỉ được hoàn tất hợp đồng cho mượn trong cửa sổ chuyển nhượng.")
        if end <= start or (end - start).days > 730:
            raise ValueError("Ngày kết thúc cho mượn phải sau ngày bắt đầu và không quá 2 năm.")
        pid = str(player_id)
        player = state.get("players", {}).get(pid)
        contract = state.get("contracts", {}).get(pid)
        if not player or not isinstance(contract, dict):
            raise ValueError("Không tìm thấy cầu thủ hoặc hợp đồng gốc.")
        parent = str(contract.get("team_id") or player.get("club") or "")
        if (not parent or parent == borrower_team or borrower_team not in state.get("rosters", {})
                or pid not in state.get("rosters", {}).get(parent, [])):
            raise ValueError("CLB cho mượn hoặc CLB mượn không hợp lệ.")
        if self._active_loan_for_player(pid) or contract.get("status") not in ("ACTIVE", "EXPIRING"):
            raise ValueError("Cầu thủ đã cho mượn hoặc không có hợp đồng đang hiệu lực.")
        try:
            fee = _money(loan_fee)
            wage_share = float(wage_share_pct)
            option = None if option_to_buy in (None, "") else _money(option_to_buy)
        except (TypeError, ValueError) as exc:
            raise ValueError("Phí hoặc tỷ lệ lương cho mượn không hợp lệ.") from exc
        if fee < 0 or not math.isfinite(wage_share) or not 0.0 <= wage_share <= 100.0:
            raise ValueError("Phí cho mượn phải >=0 và tỷ lệ lương phải từ 0–100%.")
        if option is not None and option <= 0:
            raise ValueError("Giá mua đứt tùy chọn phải lớn hơn 0.")
        if fee > 0 and self.is_transfer_banned(borrower_team):
            raise ValueError("CLB mượn đang bị cấm đăng ký thương vụ có phí theo FFP.")
        if not self._deal_limit_ok(borrower_team, date_text) or not self._deal_limit_ok(parent, date_text):
            raise ValueError("Một trong hai CLB đã đạt giới hạn thương vụ trong cửa sổ này.")
        if len(state["rosters"].get(borrower_team, [])) >= int(self.config.get("squad_rules", {}).get("max_size", 30)):
            raise ValueError("Đội hình CLB mượn đã đủ 30 cầu thủ.")
        borrower_finance = state.get("club_finance", {}).get(borrower_team, {})
        parent_finance = state.get("club_finance", {}).get(parent, {})
        if fee > float(borrower_finance.get("balance", 0.0)) or fee > float(borrower_finance.get("transfer_budget", 0.0)):
            raise ValueError("CLB mượn không đủ tiền mặt hoặc ngân sách cho phí cho mượn.")

        # A loan changes the wage split for the entire agreement. Validate both
        # clubs before touching the rosters so neither club silently exceeds its
        # annual payroll ceiling after registration.
        original_annual_wage = max(0.0, float(contract.get("wage_per_year", player.get("wage", 0.0))))
        borrower_share = original_annual_wage * wage_share / 100.0
        parent_share = original_annual_wage - borrower_share
        borrower_payroll_after = self._annual_wage_committed(borrower_team) + borrower_share
        parent_payroll_before = self._annual_wage_committed(parent)
        parent_payroll_after = max(0.0, parent_payroll_before - original_annual_wage) + parent_share
        if borrower_payroll_after > float(borrower_finance.get("wage_budget", 0.0)) + 1e-9:
            raise ValueError("Quỹ lương CLB mượn không đủ cho phần lương đã thỏa thuận.")
        if parent_payroll_after > float(parent_finance.get("wage_budget", 0.0)) + 1e-9:
            raise ValueError("CLB chủ không đủ quỹ lương để giữ lại phần lương trong thời gian cho mượn.")

        replacement = self._prepare_seller_replacement(parent, pid)
        borrower_trial = list(state["rosters"][borrower_team]) + [pid]
        parent_trial = [x for x in state["rosters"][parent] if x != pid]
        if replacement:
            parent_trial.append(replacement["player_id"])
        validation_players = dict(state["players"])
        if replacement:
            validation_players[replacement["player_id"]] = replacement["player"]
        if not self.squad_rules.validate(borrower_trial, state["players"])["ok"]:
            raise ValueError("Đội hình CLB mượn không hợp lệ sau khi nhận cầu thủ.")
        if not self.squad_rules.validate(parent_trial, validation_players)["ok"]:
            raise ValueError("CLB cho mượn không thể giữ đội hình hợp lệ sau khi mất cầu thủ.")

        loan_seq = int(state.get("loan_agreement_seq", 0)) + 1
        state["loan_agreement_seq"] = loan_seq
        loan_id = f"LOAN-{int(state.get('season', 0))}-{loan_seq:06d}"
        original_contract = copy.deepcopy(contract)
        if fee:
            FinanceService.post(state, borrower_team, "TRANSFER_IN", -fee,
                                season=int(state.get("season", 0)), month=start.month,
                                description=f"Phí mượn {player.get('common_name', pid)} từ {parent}",
                                related_id=loan_id, date=date_text,
                                idempotency_key=f"loan:{loan_id}:borrower")
            FinanceService.post(state, parent, "TRANSFER_OUT", fee,
                                season=int(state.get("season", 0)), month=start.month,
                                description=f"Phí cho mượn {player.get('common_name', pid)} tới {borrower_team}",
                                related_id=loan_id, date=date_text,
                                idempotency_key=f"loan:{loan_id}:parent")
            borrower_finance["transfer_budget"] = _money(float(borrower_finance.get("transfer_budget", 0)) - fee)
            parent_finance["transfer_budget"] = _money(float(parent_finance.get("transfer_budget", 0)) + fee)

        agreement = {
            "id": loan_id, "player_id": pid, "player_name": player.get("common_name", pid),
            "parent_team": parent, "borrower_team": borrower_team,
            "start_date": date_text, "end_date": end_date, "loan_fee": fee,
            "wage_share_pct": round(wage_share, 2), "option_to_buy": option,
            "original_contract": original_contract, "status": "ACTIVE",
        }
        state.setdefault("loan_agreements", []).append(agreement)
        state.setdefault("contract_history", {}).setdefault(pid, []).append(copy.deepcopy(contract))
        state["rosters"][parent].remove(pid)
        state["rosters"][borrower_team].append(pid)
        if replacement:
            self._commit_seller_replacement(parent, replacement, date_text)
        contract["team_id"] = borrower_team
        contract["status"] = "ON_LOAN"
        contract["loan_agreement_id"] = loan_id
        player["club"] = borrower_team
        window_key = self._window_key(date_text)
        if window_key:
            counts = state.setdefault("transfer_deal_counts", {})
            counts[f"{window_key}:{borrower_team}"] = int(counts.get(f"{window_key}:{borrower_team}", 0)) + 1
            counts[f"{window_key}:{parent}"] = int(counts.get(f"{window_key}:{parent}", 0)) + 1
        history_row = {
            "date": date_text, "loan_id": loan_id, "player_id": pid,
            "player_name": player.get("common_name", pid), "from_team": parent,
            "to_team": borrower_team, "fee": fee, "loan": True,
            "wage_share_pct": round(wage_share, 2), "loan_end_date": end_date,
            "option_to_buy": option, "status": "Cho mượn",
            "seller_replacement_player_id": replacement.get("player_id") if replacement else None,
        }
        state.setdefault("transfer_history", []).append(history_row)
        state.setdefault("economy_notifications", []).extend([
            {"team_id": parent, "date": date_text, "priority": "normal",
             "message": f"Đã cho {history_row['player_name']} tới {borrower_team} mượn đến {end_date}."},
            {"team_id": borrower_team, "date": date_text, "priority": "normal",
             "message": f"Đã mượn {history_row['player_name']} từ {parent} đến {end_date}."},
        ])
        return agreement

    def return_expired_loans(self, date_text: str) -> list[dict]:
        """Return due loans only when both clubs remain within squad rules."""
        current = date.fromisoformat(str(date_text))
        state = self.game.s
        returned = []
        for loan in state.get("loan_agreements", []):
            if loan.get("status") not in ("ACTIVE", "RETURN_PENDING"):
                continue
            if date.fromisoformat(str(loan.get("end_date", date_text))) > current:
                continue
            pid = str(loan.get("player_id", ""))
            borrower, parent = str(loan.get("borrower_team", "")), str(loan.get("parent_team", ""))
            if (pid not in state.get("rosters", {}).get(borrower, [])
                    or parent not in state.get("rosters", {})):
                loan["status"] = "RETURN_PENDING"
                loan["failure_reason"] = "Không tìm thấy đăng ký cầu thủ hoặc CLB chủ trong save."
                continue
            borrower_trial = [x for x in state["rosters"][borrower] if x != pid]
            parent_trial = list(state["rosters"][parent]) + [pid]
            borrower_ok = self.squad_rules.validate(borrower_trial, state.get("players", {}))["ok"]
            parent_ok = len(parent_trial) <= int(self.config.get("squad_rules", {}).get("max_size", 30)) and self.squad_rules.validate(
                parent_trial, state.get("players", {}))["ok"]
            if not borrower_ok or not parent_ok:
                first_notice = loan.get("status") != "RETURN_PENDING"
                loan["status"] = "RETURN_PENDING"
                loan["failure_reason"] = "Một CLB chưa đủ điều kiện đội hình để hoàn tất trả cầu thủ."
                if first_notice:
                    state.setdefault("economy_notifications", []).append({
                        "team_id": borrower, "date": date_text, "priority": "important",
                        "message": f"Hết hạn mượn {state['players'].get(pid, {}).get('common_name', pid)}, nhưng cần điều chỉnh đội hình trước khi trả cầu thủ.",
                    })
                continue

            current_contract = copy.deepcopy(state.get("contracts", {}).get(pid, {}))
            restored = copy.deepcopy(loan.get("original_contract", {}))
            if not restored:
                loan["status"] = "RETURN_PENDING"
                loan["failure_reason"] = "Thiếu snapshot hợp đồng gốc."
                continue
            restored["team_id"] = parent
            restored["years_remaining"] = max(0, int(current_contract.get("years_remaining", restored.get("years_remaining", 1))))
            if restored["years_remaining"] <= 0:
                restored["status"] = "EXPIRED_PENDING"
            else:
                restored["status"] = "EXPIRING" if restored["years_remaining"] == 1 else "ACTIVE"
            state.setdefault("contract_history", {}).setdefault(pid, []).append(current_contract)
            state["rosters"][borrower].remove(pid)
            state["rosters"][parent].append(pid)
            state["contracts"][pid] = restored
            state["players"][pid]["club"] = parent
            loan["status"] = "RETURNED"
            loan["returned_date"] = date_text
            loan["failure_reason"] = ""
            for row in state.get("transfer_history", []):
                if row.get("loan_id") == loan.get("id"):
                    row["status"] = "Cho mượn · đã trả"
                    row["loan_return_date"] = date_text
                    break
            self.release_expired_players(parent)
            state.setdefault("economy_notifications", []).extend([
                {"team_id": parent, "date": date_text, "priority": "normal",
                 "message": f"{state['players'].get(pid, {}).get('common_name', pid)} đã trở về sau thời hạn cho mượn."},
                {"team_id": borrower, "date": date_text, "priority": "normal",
                 "message": f"Đã trả {state['players'].get(pid, {}).get('common_name', pid)} về {parent}."},
            ])
            returned.append({"type": "loan_returned", "id": loan.get("id"), "player_id": pid})
        return returned

    def exercise_loan_option(self, borrower_team: str, player_id: str, date_text: str) -> dict:
        """Exercise a pre-agreed buy option and turn a loan registration permanent."""
        self._assert_enabled()
        state = self.game.s
        current = date.fromisoformat(str(date_text))
        pid = str(player_id)
        loan = self._active_loan_for_player(pid)
        if not loan or str(loan.get("borrower_team")) != str(borrower_team):
            raise ValueError("Không có hợp đồng cho mượn đang hiệu lực tại CLB này.")
        option = loan.get("option_to_buy")
        if option is None or float(option) <= 0:
            raise ValueError("Hợp đồng cho mượn không có điều khoản mua đứt.")
        if current > date.fromisoformat(str(loan.get("end_date"))):
            raise ValueError("Đã quá hạn thực hiện điều khoản mua đứt.")
        if not self.window_service.is_open(current.month):
            raise ValueError("Chỉ được thực hiện quyền mua đứt trong cửa sổ chuyển nhượng.")
        parent = str(loan.get("parent_team", ""))
        if pid not in state.get("rosters", {}).get(borrower_team, []):
            raise ValueError("Cầu thủ không còn đăng ký tại CLB mượn.")
        if float(option) > 0 and self.is_transfer_banned(borrower_team):
            raise ValueError("CLB mượn đang bị cấm mua cầu thủ có phí theo FFP.")
        if not self._deal_limit_ok(borrower_team, date_text) or not self._deal_limit_ok(parent, date_text):
            raise ValueError("Một trong hai CLB đã đạt giới hạn thương vụ trong cửa sổ này.")
        contract = state.get("contracts", {}).get(pid, {})
        years = max(0, int(contract.get("years_remaining", 0)))
        if years <= 0:
            raise ValueError("Hợp đồng gốc đã hết hạn; không thể thực hiện quyền mua đứt.")
        wage = float(loan.get("original_contract", {}).get("wage_per_year", contract.get("wage_per_year", 0.0)))
        buyer_finance = state.get("club_finance", {}).get(borrower_team, {})
        seller_finance = state.get("club_finance", {}).get(parent, {})
        wage_room = float(buyer_finance.get("wage_budget", 0.0)) - self._annual_wage_committed(
            borrower_team, exclude_player=pid)
        fee = _money(option)
        agent_fee = _money(fee * 0.05)
        if wage > wage_room:
            raise ValueError("Quỹ lương không đủ để nhận hợp đồng gốc khi mua đứt.")
        if fee + agent_fee > float(buyer_finance.get("balance", 0.0)):
            raise ValueError("Không đủ tiền mặt để mua đứt và trả phí đại diện.")
        if fee + agent_fee > float(buyer_finance.get("transfer_budget", 0.0)):
            raise ValueError("Ngân sách chuyển nhượng không đủ để mua đứt.")
        player = state.get("players", {}).get(pid, {})
        state.setdefault("contract_history", {}).setdefault(pid, []).append(copy.deepcopy(contract))
        FinanceService.post(state, borrower_team, "TRANSFER_IN", -fee,
                            season=int(state.get("season", 0)), month=current.month,
                            description=f"Mua đứt {player.get('common_name', pid)} theo điều khoản cho mượn",
                            related_id=loan.get("id"), date=date_text,
                            idempotency_key=f"loan-option:{loan.get('id')}:buyer")
        FinanceService.post(state, parent, "TRANSFER_OUT", fee,
                            season=int(state.get("season", 0)), month=current.month,
                            description=f"Phí bán đứt {player.get('common_name', pid)} theo điều khoản cho mượn",
                            related_id=loan.get("id"), date=date_text,
                            idempotency_key=f"loan-option:{loan.get('id')}:seller")
        buyer_finance["transfer_budget"] = _money(float(buyer_finance.get("transfer_budget", 0.0)) - fee - agent_fee)
        seller_finance["transfer_budget"] = _money(float(seller_finance.get("transfer_budget", 0.0)) + fee)
        if agent_fee:
            FinanceService.post(state, borrower_team, "AGENT_FEE", -agent_fee,
                                season=int(state.get("season", 0)), month=current.month,
                                description=f"Phí đại diện mua đứt · {player.get('common_name', pid)}",
                                related_id=loan.get("id"), date=date_text,
                                idempotency_key=f"loan-option:{loan.get('id')}:agent")
        state["contracts"][pid] = {
            "id": f"CT-{int(state.get('season', 0))}-{pid}-LOANOPT", "player_id": pid,
            "team_id": borrower_team, "start_season": int(state.get("season", 0)),
            "end_season": int(state.get("season", 0)) + years - 1,
            "years_total": years, "years_remaining": years, "wage_per_year": wage,
            "signing_bonus": 0.0, "squad_role": loan.get("original_contract", {}).get("squad_role", "ROTATION"),
            "status": "ACTIVE", "initialized": False,
        }
        player["club"] = borrower_team
        player["wage"] = wage
        loan["status"] = "OPTION_EXERCISED"
        loan["option_exercised_date"] = date_text
        loan["option_exercised_fee"] = fee
        for row in state.get("transfer_history", []):
            if row.get("loan_id") == loan.get("id"):
                row["status"] = "Cho mượn · mua đứt"
                row["option_exercised_date"] = date_text
                break

        triggered = 0
        option_sale_id = f"OPT-{loan.get('id')}"
        for clause in state.setdefault("sell_on_clauses", []):
            if (str(clause.get("player_id")) == pid and str(clause.get("owner_team")) == parent
                    and clause.get("status") == "ACTIVE"):
                amount = _money(fee * (float(clause.get("sell_on_pct", 0.0)) / 100.0))
                clause["status"] = "TRIGGERED"
                clause["triggered_offer_id"] = option_sale_id
                clause["triggered_date"] = date_text
                triggered += 1
                if amount > 0:
                    state.setdefault("transfer_settlements", []).append({
                        "id": self._new_settlement_id(state), "offer_id": option_sale_id,
                        "player_id": pid, "payer_team": parent,
                        "recipient_team": str(clause.get("beneficiary_team", "")),
                        "amount": amount, "due_date": date_text, "kind": "SELL_ON",
                        "status": "PENDING", "attempts": 0,
                        "sell_on_clause_id": clause.get("id"),
                    })
                else:
                    clause["status"] = "PAID"
                    clause["paid_date"] = date_text
        history = {
            "offer_id": option_sale_id, "date": date_text, "player_id": pid,
            "player_name": player.get("common_name", pid), "from_team": parent,
            "to_team": borrower_team, "fee": fee, "upfront_paid": fee,
            "deferred_fee": 0.0, "installment_count": 1, "status": "Hoàn tất",
            "wage": wage, "years": years, "signing_bonus": 0.0, "agent_fee": agent_fee,
            "loan_option_exercised": True, "sell_on_triggered": triggered,
            "loan_id": loan.get("id"),
        }
        state.setdefault("transfer_history", []).append(history)
        window_key = self._window_key(date_text)
        if window_key:
            counts = state.setdefault("transfer_deal_counts", {})
            counts[f"{window_key}:{borrower_team}"] = int(counts.get(f"{window_key}:{borrower_team}", 0)) + 1
            counts[f"{window_key}:{parent}"] = int(counts.get(f"{window_key}:{parent}", 0)) + 1
        self.process_due_transfer_payments(date_text)
        state.setdefault("economy_notifications", []).append({
            "team_id": borrower_team, "date": date_text, "priority": "important",
            "message": f"Đã mua đứt {player.get('common_name', pid)} từ {parent} với giá {fee:.3f} M€.",
        })
        return history

    def mark_for_sale(self, team: str, player_id: str, asking_price: float | None = None) -> dict:
        state = self.game.s
        if self._active_loan_for_player(str(player_id)):
            raise ValueError("Không thể rao bán cầu thủ đang được cho mượn.")
        if player_id not in state.get("rosters", {}).get(team, []):
            raise ValueError("Cầu thủ không thuộc đội này.")
        contract = state.get("contracts", {}).get(player_id, {})
        if contract.get("status") not in ("ACTIVE", "EXPIRING", "EXPIRED_PENDING"):
            raise ValueError("Cầu thủ không có trạng thái hợp đồng hợp lệ để rao bán.")
        price = _money(asking_price if asking_price is not None else self.asking_price(player_id))
        listing = {"team_id": team, "player_id": player_id, "asking_price": price,
                   "listed_date": str(state.get("economy_last_date", "")), "status": "LISTED"}
        listings = state.setdefault("transfer_list", [])
        listings[:] = [row for row in listings if row.get("player_id") != player_id]
        listings.append(listing)
        return listing

    def asking_price(self, player_id: str) -> float:
        state = self.game.s
        player = state["players"][player_id]
        contract = state.get("contracts", {}).get(player_id, {})
        role = str(contract.get("squad_role", "ROTATION"))
        role_factor = {"STAR": 1.4, "FIRST_TEAM": 1.15, "ROTATION": 1.0, "BACKUP": 0.8, "PROSPECT": 0.8}.get(role, 1.0)
        years = int(contract.get("years_remaining", 1))
        contract_factor = 0.7 if years <= 1 else (0.9 if years == 2 else 1.0 + min(0.2, (years-3)*0.05))
        seller = contract.get("team_id") or player.get("club")
        seller_finance = state.get("club_finance", {}).get(seller, {})
        need_factor = 1.2 if len(state.get("rosters", {}).get(seller, [])) <= 23 else 1.0
        status_factor = 0.9 if float(seller_finance.get("balance", 0)) < 80 else 1.0
        return _money(float(player.get("market_value", 0.05)) * role_factor * contract_factor * need_factor * status_factor)

    def submit_transfer_offer(self, buyer_team: str, player_id: str, fee: float,
                              negotiation_id: str, date_text: str,
                              deal_terms: dict | None = None) -> dict:
        self._assert_enabled()
        state = self.game.s
        if not self._window_open(date_text):
            raise ValueError("Chỉ được gửi offer mua cầu thủ trong cửa sổ chuyển nhượng.")
        negotiation = next((n for n in state.get("contract_negotiations", []) if n.get("id") == negotiation_id), None)
        if (not negotiation or negotiation.get("status") != "AGREED" or
                not negotiation.get("personal_agreement") or negotiation.get("team_id") != buyer_team or
                str(negotiation.get("player_id")) != str(player_id)):
            raise ValueError("Cần đạt thỏa thuận cá nhân với đúng cầu thủ/CLB trước khi gửi offer.")
        pid = str(player_id)
        if self._active_loan_for_player(pid):
            raise ValueError("Cầu thủ đang trong hợp đồng cho mượn; chỉ CLB sở hữu mới có thể bán sau khi cầu thủ trở về.")
        player = state["players"][pid]
        seller = state.get("contracts", {}).get(pid, {}).get("team_id") or player.get("club")
        if not seller or seller == buyer_team:
            raise ValueError("Cầu thủ không thuộc một CLB bán hợp lệ.")
        fee = _money(fee)
        normalized_deal_terms = self._validated_deal_terms(deal_terms)
        buyer_finance = state["club_finance"][buyer_team]
        if fee > 0 and self.is_transfer_banned(buyer_team):
            raise ValueError("CLB đang bị cấm mua cầu thủ có phí theo FFP.")
        if not self._deal_limit_ok(buyer_team, date_text):
            raise ValueError("CLB đã đạt giới hạn thương vụ trong cửa sổ này.")
        upfront_required = _money(fee * float(normalized_deal_terms.get("upfront_pct", 100.0)) / 100.0)
        addons_reserve = _money(sum(float(row["amount"]) for row in normalized_deal_terms.get("addons", [])))
        if addons_reserve > _money(fee * 0.50):
            raise ValueError("Tổng add-on không được vượt quá 50% phí chuyển nhượng.")
        if (fee + addons_reserve > float(buyer_finance.get("transfer_budget", 0)) or
                upfront_required > float(buyer_finance.get("balance", 0))):
            raise ValueError("Offer vượt ngân sách chuyển nhượng (bao gồm add-on dự phòng) hoặc tiền trả trước.")
        if len(state["rosters"][buyer_team]) >= int(self.config.get("squad_rules", {}).get("max_size", 30)):
            raise ValueError("Đội hình mua đã đủ 30 cầu thủ; cần bán/giải phóng trước.")
        past_offers = [o for o in state.get("transfer_offers", [])
                       if o.get("player_id") == pid and o.get("to_team") == buyer_team]
        if len(past_offers) >= 3:
            raise ValueError("Đã hết tối đa 3 vòng thương lượng cho cầu thủ này.")
        asking = self.asking_price(pid)
        if fee >= asking:
            status, counter = "ACCEPTED", None
        elif fee >= asking * 0.85:
            status, counter = "COUNTERED", _money((fee + asking) / 2.0)
        else:
            status, counter = "REJECTED", None
        seq = int(state.get("transfer_offer_seq", 0)) + 1
        state["transfer_offer_seq"] = seq
        offer = {
            "id": f"OFF-{int(state.get('season',0))}-{seq:06d}", "player_id": pid,
            "from_team": seller, "to_team": buyer_team, "fee": fee,
            "asking_price": asking, "counter_fee": counter, "status": status,
            "round": len(past_offers) + 1, "created_date": date_text, "expires_date": (date.fromisoformat(date_text)+timedelta(days=7)).isoformat(),
            "negotiation_id": negotiation_id, "deal_terms": normalized_deal_terms,
        }
        state.setdefault("transfer_offers", []).append(offer)
        if status == "ACCEPTED":
            offer["status"] = "ACCEPTED"
        elif status == "COUNTERED":
            negotiation["status"] = "TRANSFER_COUNTER"
        return offer

    def respond_to_transfer_counter(self, offer_id: str, accept: bool, date_text: str) -> dict:
        state = self.game.s
        offer = next((o for o in state.get("transfer_offers", []) if o.get("id") == offer_id), None)
        if not offer or offer.get("status") != "COUNTERED":
            raise ValueError("Không có phản giá đang chờ xử lý.")
        negotiation = next((n for n in state.get("contract_negotiations", []) if n.get("id") == offer.get("negotiation_id")), None)
        if not accept:
            offer["status"] = "REJECTED"
            offer["resolved_date"] = date_text
            if negotiation:
                negotiation["status"] = "FAILED"
            return offer
        fee = float(offer.get("counter_fee", 0))
        buyer_finance = state["club_finance"][offer["to_team"]]
        if fee > float(buyer_finance.get("transfer_budget", 0)) or fee > float(buyer_finance.get("balance", 0)):
            raise ValueError("Không đủ ngân sách để chấp nhận phản giá.")
        offer["fee"] = _money(fee)
        offer["status"] = "ACCEPTED"
        offer["resolved_date"] = date_text
        if negotiation:
            negotiation["status"] = "AGREED"
        return offer

    def _prepare_seller_replacement(self, seller: str, outgoing_player_id: str) -> dict | None:
        """Prepare a youth replacement if a 23-player seller would become invalid.

        The returned player is only committed after all transfer checks pass.
        This is the same-transaction replacement path allowed by the brief and
        keeps a 23-player club from being stranded at 22 after a completed sale.
        """
        state = self.game.s
        roster = list(state.get("rosters", {}).get(seller, []))
        trial = [pid for pid in roster if pid != outgoing_player_id]
        if self.squad_rules.validate(trial, state.get("players", {}))["ok"]:
            return None

        groups = {group: 0 for group in ("GK", "DEF", "MID", "FWD")}
        for pid in trial:
            groups[position_group(state.get("players", {}).get(pid, {}).get("position", ""))] += 1
        required = self.config.get("squad_rules", {}).get(
            "minimum_groups", {"GK": 3, "DEF": 7, "MID": 7, "FWD": 4})
        missing_groups = [group for group, minimum in required.items() if groups.get(group, 0) < int(minimum)]
        if len(trial) < int(self.config.get("squad_rules", {}).get("min_size", 23)) and not missing_groups:
            outgoing_group = position_group(state.get("players", {}).get(outgoing_player_id, {}).get("position", ""))
            missing_groups = [outgoing_group]
        if not missing_groups:
            return None

        academy = state.setdefault("academy", {}).setdefault(seller, [])
        candidates = []
        for index, entry in enumerate(academy):
            player_row = entry.get("player", {})
            meta = entry.get("meta", {})
            if position_group(player_row.get("position", "")) in missing_groups:
                candidates.append((float(meta.get("potential", 0)), float(player_row.get("overall_rating", 0)), index, entry))
        if candidates:
            _potential, _ovr, academy_index, entry = max(candidates)
            player = copy.deepcopy(entry.get("player", {}))
            meta = copy.deepcopy(entry.get("meta", {}))
        else:
            outgoing = state.get("players", {}).get(outgoing_player_id, {})
            target_group = missing_groups[0]
            default_position = {"GK": "GK", "DEF": "CB", "MID": "CM", "FWD": "ST"}[target_group]
            position = outgoing.get("position", default_position)
            if position_group(position) != target_group:
                position = default_position
            seq = int(state.get("transfer_replacement_seq", 0)) + 1
            player_id = f"TR-YOUTH-{int(state.get('season',0))}-{seq:04d}"
            while player_id in state.get("players", {}):
                seq += 1
                player_id = f"TR-YOUTH-{int(state.get('season',0))}-{seq:04d}"
            player, meta = self.game._make_youth_row(seller, position, int(state.get("season", 0)), player_id, academy=False)
            academy_index = None
        player_id = str(player.get("player_id", ""))
        if not player_id or player_id in state.get("players", {}):
            seq = int(state.get("transfer_replacement_seq", 0)) + 1
            player_id = f"TR-YOUTH-{int(state.get('season',0))}-{seq:04d}"
            while player_id in state.get("players", {}):
                seq += 1
                player_id = f"TR-YOUTH-{int(state.get('season',0))}-{seq:04d}"
            player["player_id"] = player_id
        player["club"] = seller
        player["is_default"] = "FALSE"
        group = position_group(player.get("position", ""))
        if group not in missing_groups:
            return None
        meta.update({"status": "active", "age": float(meta.get("age", 18)),
                     "potential": float(meta.get("potential", player.get("overall_rating", 55)))})
        wage_input = dict(player)
        wage_input.update({"age": meta["age"], "potential": meta["potential"]})
        tier = str(state.get("club_finance", {}).get(seller, {}).get("tier", "small"))
        wage = WageService(self.config).base_annual_wage(wage_input, tier, False)
        committed = self._annual_wage_committed(seller)
        budget = float(state.get("club_finance", {}).get(seller, {}).get("wage_budget", 0))
        if committed + wage > budget * 1.001:
            raise ValueError("CLB bán thiếu người thay thế đủ điều kiện và không còn quỹ lương cho cầu thủ học viện.")
        staged_players = dict(state.get("players", {}))
        staged_players[player_id] = player
        seller_after = trial + [player_id]
        check = self.squad_rules.validate(seller_after, staged_players)
        if not check["ok"]:
            raise ValueError("Không thể tìm cầu thủ học viện thay thế mà vẫn giữ đội hình hợp lệ: " + "; ".join(check["errors"]))
        return {"player_id": player_id, "player": player, "meta": meta,
                "wage": wage, "market_value": WageService(self.config).market_value(wage_input),
                "academy_index": academy_index}

    def _commit_seller_replacement(self, seller: str, replacement: dict, date_text: str):
        state = self.game.s
        pid = replacement["player_id"]
        state.setdefault("transfer_replacement_seq", 0)
        state["transfer_replacement_seq"] = int(state["transfer_replacement_seq"]) + 1
        if replacement.get("academy_index") is not None:
            academy = state.get("academy", {}).get(seller, [])
            index = int(replacement["academy_index"])
            if 0 <= index < len(academy):
                academy.pop(index)
        player = copy.deepcopy(replacement["player"])
        player["player_id"] = pid
        player["club"] = seller
        player["wage"] = float(replacement["wage"])
        player["market_value"] = float(replacement["market_value"])
        state.setdefault("players", {})[pid] = player
        meta = copy.deepcopy(replacement["meta"])
        meta["status"] = "active"
        state.setdefault("player_meta", {})[pid] = meta
        state.setdefault("dyn", {})[pid] = {"form": 0.0, "fit": 100.0}
        state.setdefault("season_stats", {})[pid] = self.game._empty_stat()
        state.setdefault("contracts", {})[pid] = {
            "id": f"CT-{int(state.get('season',0))}-{pid}-YOUTH", "player_id": pid, "team_id": seller,
            "start_season": int(state.get("season", 0)), "end_season": int(state.get("season", 0)) + 2,
            "years_total": 3, "years_remaining": 3, "wage_per_year": float(replacement["wage"]),
            "signing_bonus": 0.0, "squad_role": "PROSPECT", "status": "ACTIVE", "initialized": False,
        }
        state["rosters"][seller].append(pid)
        generated = state.setdefault("generated_players", [])
        if not any(row.get("player_id") == pid for row in generated):
            generated.append({**player, "potential": meta.get("potential"), "season_added": int(state.get("season", 0)),
                              "replaced_player_id": None, "reason": "transfer_replacement"})
        state.setdefault("economy_notifications", []).append({
            "team_id": seller, "date": date_text, "priority": "normal",
            "message": f"Đôn {player.get('common_name', pid)} từ học viện để thay thế vị trí vừa bán; đội hình vẫn đủ quân số.",
        })

    def complete_transfer(self, offer_id: str, date_text: str) -> dict:
        self._assert_enabled()
        state = self.game.s
        offer = next((o for o in state.get("transfer_offers", []) if o.get("id") == offer_id), None)
        if not offer:
            raise ValueError("Không tìm thấy offer.")
        if offer.get("status") == "COMPLETED":
            existing = next((row for row in state.get("transfer_history", [])
                             if row.get("offer_id") == offer_id), None)
            if existing:
                return existing
            raise ValueError("Offer đã hoàn tất nhưng thiếu bản ghi lịch sử; cần kiểm tra save.")
        if offer.get("status") != "ACCEPTED":
            raise ValueError("Offer chưa được chấp nhận.")
        if not self._window_open(date_text):
            raise ValueError("Cửa sổ chuyển nhượng đã đóng.")
        negotiation = next((n for n in state.get("contract_negotiations", []) if n.get("id") == offer.get("negotiation_id")), None)
        if not negotiation or negotiation.get("status") != "AGREED":
            raise ValueError("Chưa có thỏa thuận cá nhân hợp lệ.")
        pid, seller, buyer = str(offer["player_id"]), str(offer["from_team"]), str(offer["to_team"])
        fee = _money(offer["fee"])
        deal_terms = self._validated_deal_terms(offer.get("deal_terms"))
        if fee > 0 and self.is_transfer_banned(buyer):
            raise ValueError("CLB đang bị cấm mua cầu thủ có phí theo FFP.")
        if not self._deal_limit_ok(buyer, date_text) or not self._deal_limit_ok(seller, date_text):
            raise ValueError("Một trong hai CLB đã đạt giới hạn thương vụ trong cửa sổ này.")
        replacement = self._prepare_seller_replacement(seller, pid)
        if len(state["rosters"].get(buyer, [])) >= int(self.config.get("squad_rules", {}).get("max_size", 30)):
            raise ValueError("CLB mua đã đạt giới hạn 30 cầu thủ.")
        buyer_finance = state["club_finance"][buyer]
        seller_finance = state["club_finance"][seller]
        terms = negotiation["club_offer"]
        wage_room = float(buyer_finance.get("wage_budget", 0)) - self._annual_wage_committed(buyer)
        if float(terms["wage"]) > wage_room:
            raise ValueError("Quỹ lương không đủ để hoàn tất thương vụ.")
        signing_bonus = float(terms.get("signing_bonus", 0))
        estimated_agent_fee = _money((fee + signing_bonus) * 0.05)
        fee_schedule = self._build_fee_schedule(offer, date_text)
        upfront_amount = _money(fee_schedule[0]["amount"] if fee_schedule else 0.0)
        total_initial_cash = upfront_amount + signing_bonus + estimated_agent_fee
        addons_reserve = _money(sum(float(row["amount"]) for row in deal_terms.get("addons", [])))
        total_committed_budget = fee + addons_reserve + signing_bonus + estimated_agent_fee
        if float(buyer_finance.get("balance", 0)) < total_initial_cash:
            raise ValueError("Số dư tiền mặt không đủ cho đợt trả trước, lót tay và phí đại diện.")
        if float(buyer_finance.get("transfer_budget", 0)) < total_committed_budget:
            raise ValueError("Ngân sách chuyển nhượng không đủ cho toàn bộ phí, lót tay và đại diện.")
        buyer_after = list(state["rosters"][buyer]) + [pid]
        seller_after = [x for x in state["rosters"][seller] if x != pid]
        seller_validation_roster = seller_after + ([replacement["player_id"]] if replacement else [])
        seller_validation_players = dict(state["players"])
        if replacement:
            seller_validation_players[replacement["player_id"]] = replacement["player"]
        buyer_check = self.squad_rules.validate(buyer_after, state["players"])
        seller_check = self.squad_rules.validate(seller_validation_roster, seller_validation_players)
        if not buyer_check["ok"]:
            raise ValueError("Đội hình mua không hợp lệ: " + "; ".join(buyer_check["errors"]))
        if not seller_check["ok"]:
            raise ValueError("Đội hình bán không hợp lệ: " + "; ".join(seller_check["errors"]))

        player = state["players"][pid]
        contract = state["contracts"][pid]
        state.setdefault("contract_history", {}).setdefault(pid, []).append(copy.deepcopy(contract))
        # Commit only the upfront fee in cash; the full fee is reserved from the
        # buyer's transfer budget and later instalments are explicit liabilities.
        if upfront_amount:
            FinanceService.post(state, buyer, "TRANSFER_IN", -upfront_amount, season=int(state.get("season", 0)),
                                month=int(date_text[5:7]), description=f"Trả trước mua {player.get('common_name', pid)} từ {seller}",
                                related_id=pid, date=date_text,
                                idempotency_key=f"transfer:{offer_id}:upfront:buyer")
            FinanceService.post(state, seller, "TRANSFER_OUT", upfront_amount, season=int(state.get("season", 0)),
                                month=int(date_text[5:7]), description=f"Nhận trả trước bán {player.get('common_name', pid)} cho {buyer}",
                                related_id=pid, date=date_text,
                                idempotency_key=f"transfer:{offer_id}:upfront:seller")
        buyer_finance["transfer_budget"] = _money(float(buyer_finance.get("transfer_budget", 0)) - total_committed_budget)
        seller_finance["transfer_budget"] = _money(float(seller_finance.get("transfer_budget", 0)) + upfront_amount)
        if signing_bonus:
            FinanceService.post(state, buyer, "SIGNING_BONUS", -signing_bonus, season=int(state.get("season", 0)),
                                month=int(date_text[5:7]), description=f"Lót tay ký {player.get('common_name', pid)}", related_id=pid, date=date_text,
                                idempotency_key=f"transfer:{offer_id}:signing-bonus")
        if estimated_agent_fee:
            FinanceService.post(state, buyer, "AGENT_FEE", -estimated_agent_fee, season=int(state.get("season", 0)),
                                month=int(date_text[5:7]), description=f"Phí đại diện · {player.get('common_name', pid)}", related_id=pid, date=date_text,
                                idempotency_key=f"transfer:{offer_id}:agent-fee")

        state["rosters"][seller].remove(pid)
        state["rosters"][buyer].append(pid)
        if replacement:
            self._commit_seller_replacement(seller, replacement, date_text)
        contract["status"] = "TRANSFERRED_OUT"
        self.release_expired_players(buyer)
        self.release_expired_players(seller)
        player["club"] = buyer
        player["wage"] = float(terms["wage"])
        state["contracts"][pid] = {
            "id": f"CT-{int(state.get('season',0))}-{pid}-TR", "player_id": pid, "team_id": buyer,
            "start_season": int(state.get("season", 0)), "end_season": int(state.get("season", 0)) + int(terms["years"]) - 1,
            "years_total": int(terms["years"]), "years_remaining": int(terms["years"]),
            "wage_per_year": float(terms["wage"]), "signing_bonus": signing_bonus,
            "squad_role": terms.get("role", "ROTATION"), "status": "ACTIVE", "initialized": False,
        }
        self._cancel_active_addons_for_player(
            pid, date_text, "Cầu thủ rời CLB đã cam kết add-on trước khi đạt mốc thành tích.")

        state.setdefault("transfer_settlements", []).extend(fee_schedule)
        addon_clauses = []
        for index, addon in enumerate(deal_terms.get("addons", []), start=1):
            clause_seq = int(state.get("transfer_addon_clause_seq", 0)) + 1
            state["transfer_addon_clause_seq"] = clause_seq
            clause = {
                "id": f"ADD-{clause_seq:08d}", "player_id": pid,
                "player_name": player.get("common_name", pid),
                "payer_team": buyer, "recipient_team": seller,
                "metric": addon["metric"], "threshold": int(addon["threshold"]),
                "amount": float(addon["amount"]), "reserved_amount": float(addon["amount"]),
                "progress": 0, "created_date": date_text,
                "expires_date": (date.fromisoformat(date_text) + timedelta(days=int(addon["valid_days"]))).isoformat(),
                "source_offer_id": offer_id, "status": "ACTIVE", "sequence": index,
            }
            state.setdefault("transfer_addon_clauses", []).append(clause)
            addon_clauses.append(clause)
        # Existing sell-on obligations are payable by the current seller when
        # this player moves again. They are scheduled, never silently discarded.
        triggered_clauses = []
        for clause in state.setdefault("sell_on_clauses", []):
            if (str(clause.get("player_id")) == pid and str(clause.get("owner_team")) == seller
                    and clause.get("status") == "ACTIVE"):
                amount = _money(fee * (float(clause.get("sell_on_pct", 0.0)) / 100.0))
                clause["status"] = "TRIGGERED"
                clause["triggered_offer_id"] = offer_id
                clause["triggered_date"] = date_text
                if amount > 0:
                    settlement = {
                        "id": self._new_settlement_id(state), "offer_id": offer_id, "player_id": pid,
                        "payer_team": seller, "recipient_team": str(clause.get("beneficiary_team", "")),
                        "amount": amount, "due_date": date_text, "kind": "SELL_ON",
                        "status": "PENDING", "attempts": 0, "sell_on_clause_id": clause.get("id"),
                    }
                    state.setdefault("transfer_settlements", []).append(settlement)
                    triggered_clauses.append(clause)
                else:
                    clause["status"] = "PAID"
                    clause["paid_date"] = date_text

        sell_on_pct = float(deal_terms.get("sell_on_pct", 0.0))
        new_sell_on_clause = None
        if sell_on_pct > 0:
            clause_seq = int(state.get("sell_on_clause_seq", 0)) + 1
            state["sell_on_clause_seq"] = clause_seq
            new_sell_on_clause = {
                "id": f"SOL-{clause_seq:08d}", "player_id": pid, "owner_team": buyer,
                "beneficiary_team": seller, "sell_on_pct": sell_on_pct,
                "created_date": date_text, "source_offer_id": offer_id, "status": "ACTIVE",
            }
            state.setdefault("sell_on_clauses", []).append(new_sell_on_clause)

        offer["status"] = "COMPLETED"
        offer["completed_date"] = date_text
        window_key = self._window_key(date_text)
        if window_key:
            counts = state.setdefault("transfer_deal_counts", {})
            counts[f"{window_key}:{buyer}"] = int(counts.get(f"{window_key}:{buyer}", 0)) + 1
            counts[f"{window_key}:{seller}"] = int(counts.get(f"{window_key}:{seller}", 0)) + 1
        history_row = {
            "offer_id": offer_id, "date": date_text, "player_id": pid,
            "player_name": player.get("common_name", pid), "from_team": seller, "to_team": buyer,
            "fee": fee, "upfront_paid": upfront_amount,
            "deferred_fee": _money(fee - upfront_amount),
            "installment_count": len(fee_schedule), "status": "Hoàn tất",
            "wage": float(terms["wage"]), "years": int(terms["years"]),
            "signing_bonus": signing_bonus, "agent_fee": estimated_agent_fee,
            "approach_fee": float(negotiation.get("approach_fee", 0)),
            "sell_on_pct": sell_on_pct,
            "sell_on_triggered": len(triggered_clauses),
            "sell_on_clause_id": new_sell_on_clause.get("id") if new_sell_on_clause else None,
            "addons": [{"id": row["id"], "metric": row["metric"],
                        "threshold": row["threshold"], "amount": row["amount"]}
                       for row in addon_clauses],
            "addons_reserved": addons_reserve,
            "seller_replacement_player_id": replacement.get("player_id") if replacement else None,
        }
        state.setdefault("transfer_history", []).append(history_row)
        state.setdefault("economy_notifications", []).append({
            "team_id": buyer, "date": date_text, "priority": "important",
            "message": f"Hoàn tất chiêu mộ {player.get('common_name', pid)} từ {seller} với phí {fee:.3f} M€; trả trước {upfront_amount:.3f} M€.",
        })
        # Process same-day seller clauses only after the transfer fee's upfront
        # leg has been credited, allowing normal deals to settle immediately.
        self.process_due_transfer_payments(date_text)
        return history_row


    def complete_swap(self, team_a: str, player_a_id: str, team_b: str, player_b_id: str,
                      date_text: str, cash_adjustment: float = 0.0,
                      team_a_incoming_terms: dict | None = None,
                      team_b_incoming_terms: dict | None = None) -> list[dict]:
        """Execute a two-player swap with an optional cash adjustment.

        ``cash_adjustment`` is positive when team_a pays team_b and negative
        when team_b pays team_a. Both player contracts are agreed before either
        roster is mutated; roster, payroll, cash, budgets and FFP are validated
        as one transaction.
        """
        self._assert_enabled()
        state = self.game.s
        player_a_id, player_b_id = str(player_a_id), str(player_b_id)
        if team_a == team_b or team_a not in state.get("rosters", {}) or team_b not in state.get("rosters", {}):
            raise ValueError("Đổi người cần hai CLB khác nhau và đều tồn tại trong save.")
        if not self._window_open(date_text):
            raise ValueError("Chỉ được hoàn tất trao đổi cầu thủ trong cửa sổ chuyển nhượng.")
        try:
            adjustment = _money(cash_adjustment)
        except (TypeError, ValueError) as exc:
            raise ValueError("Khoản tiền cân bằng không hợp lệ.") from exc
        if abs(adjustment) > 500.0:
            raise ValueError("Khoản tiền cân bằng tối đa là 500 M€.")
        swap_key = f"{date_text}|{team_a}|{player_a_id}|{team_b}|{player_b_id}|{adjustment:.3f}"
        completed = state.setdefault("completed_swaps", [])
        old = next((row for row in completed if row.get("swap_key") == swap_key), None)
        if old:
            return [row for row in state.get("transfer_history", []) if row.get("swap_id") == old.get("swap_id")]
        if player_a_id == player_b_id or player_a_id not in state.get("rosters", {}).get(team_a, []) or player_b_id not in state.get("rosters", {}).get(team_b, []):
            raise ValueError("Mỗi CLB phải chọn một cầu thủ hiện thuộc đội hình của mình.")
        if self._active_loan_for_player(player_a_id) or self._active_loan_for_player(player_b_id):
            raise ValueError("Không thể trao đổi cầu thủ đang trong hợp đồng cho mượn.")
        contract_a = state.get("contracts", {}).get(player_a_id, {})
        contract_b = state.get("contracts", {}).get(player_b_id, {})
        if contract_a.get("team_id") != team_a or contract_b.get("team_id") != team_b:
            raise ValueError("Thông tin chủ sở hữu trong hợp đồng không khớp đội hình.")
        if contract_a.get("status") not in ("ACTIVE", "EXPIRING", "EXPIRED_PENDING") or contract_b.get("status") not in ("ACTIVE", "EXPIRING", "EXPIRED_PENDING"):
            raise ValueError("Một trong hai cầu thủ không có hợp đồng hợp lệ để trao đổi.")
        if not self._deal_limit_ok(team_a, date_text) or not self._deal_limit_ok(team_b, date_text):
            raise ValueError("Một trong hai CLB đã đạt giới hạn thương vụ trong cửa sổ này.")

        payer = team_a if adjustment > 0 else (team_b if adjustment < 0 else "")
        recipient = team_b if adjustment > 0 else (team_a if adjustment < 0 else "")
        cash_leg = abs(adjustment)
        if cash_leg > 0 and self.is_transfer_banned(payer):
            raise ValueError("CLB trả thêm tiền đang bị cấm mua theo FFP.")

        player_a = state["players"][player_a_id]
        player_b = state["players"][player_b_id]
        team_a_after = [pid for pid in state["rosters"][team_a] if pid != player_a_id] + [player_b_id]
        team_b_after = [pid for pid in state["rosters"][team_b] if pid != player_b_id] + [player_a_id]
        check_a = self.squad_rules.validate(team_a_after, state.get("players", {}))
        check_b = self.squad_rules.validate(team_b_after, state.get("players", {}))
        if not check_a["ok"]:
            raise ValueError("Đội hình sau trao đổi của CLB A không hợp lệ: " + "; ".join(check_a["errors"]))
        if not check_b["ok"]:
            raise ValueError("Đội hình sau trao đổi của CLB B không hợp lệ: " + "; ".join(check_b["errors"]))

        negotiation_a = self.start_contract_negotiation(team_a, player_b_id, date_text, negotiation_type="SWAP")
        negotiation_b = self.start_contract_negotiation(team_b, player_a_id, date_text, negotiation_type="SWAP")
        terms_a = team_a_incoming_terms if isinstance(team_a_incoming_terms, dict) else {}
        terms_b = team_b_incoming_terms if isinstance(team_b_incoming_terms, dict) else {}
        demand_a, demand_b = negotiation_a["player_demand"], negotiation_b["player_demand"]
        wage_a = _money(terms_a.get("wage", demand_a["wage"]))
        wage_b = _money(terms_b.get("wage", demand_b["wage"]))
        years_a = int(terms_a.get("years", demand_a["years"]))
        years_b = int(terms_b.get("years", demand_b["years"]))
        max_wage = float(self.config.get("wages", {}).get("max_annual_wage", 35.0))
        if (wage_a <= 0 or wage_b <= 0 or wage_a > max_wage or wage_b > max_wage or
                wage_a < float(demand_a["wage"]) * 0.95 or wage_b < float(demand_b["wage"]) * 0.95 or
                not 1 <= years_a <= 5 or not 1 <= years_b <= 5 or
                abs(years_a - int(demand_a["years"])) > 1 or abs(years_b - int(demand_b["years"])) > 1):
            negotiation_a["status"] = negotiation_b["status"] = "FAILED"
            raise ValueError("Một trong hai cầu thủ không chấp nhận mức lương/thời hạn hợp đồng đề nghị.")

        finance_a, finance_b = state["club_finance"][team_a], state["club_finance"][team_b]
        payroll_a = self._annual_wage_committed(team_a, exclude_player=player_a_id) + wage_a
        payroll_b = self._annual_wage_committed(team_b, exclude_player=player_b_id) + wage_b
        if payroll_a > float(finance_a.get("wage_budget", 0)) or payroll_b > float(finance_b.get("wage_budget", 0)):
            negotiation_a["status"] = negotiation_b["status"] = "FAILED"
            raise ValueError("Quỹ lương của một CLB không đủ sau khi đổi hợp đồng.")

        agent_a = _money(float(player_b.get("market_value", 0.0)) * 0.05)
        agent_b = _money(float(player_a.get("market_value", 0.0)) * 0.05)
        cost_a, cost_b = agent_a + (cash_leg if payer == team_a else 0.0), agent_b + (cash_leg if payer == team_b else 0.0)
        if (cost_a > float(finance_a.get("balance", 0)) or cost_b > float(finance_b.get("balance", 0)) or
                cost_a > float(finance_a.get("transfer_budget", 0)) or cost_b > float(finance_b.get("transfer_budget", 0))):
            negotiation_a["status"] = negotiation_b["status"] = "FAILED"
            raise ValueError("Không đủ tiền mặt/ngân sách cho khoản cân bằng và phí đại diện của thương vụ đổi người.")

        seq = int(state.get("swap_seq", 0)) + 1
        state["swap_seq"] = seq
        swap_id = f"SWAP-{int(state.get('season', 0))}-{seq:06d}"
        negotiation_a.update({"club_offer": {"wage": wage_a, "years": years_a, "signing_bonus": 0.0,
                                               "role": demand_a.get("role", "ROTATION")},
                              "status": "AGREED", "personal_agreement": True})
        negotiation_b.update({"club_offer": {"wage": wage_b, "years": years_b, "signing_bonus": 0.0,
                                               "role": demand_b.get("role", "ROTATION")},
                              "status": "AGREED", "personal_agreement": True})

        # The clubs exchange players with no temporary invalid roster state.
        state.setdefault("contract_history", {}).setdefault(player_a_id, []).append(copy.deepcopy(contract_a))
        state.setdefault("contract_history", {}).setdefault(player_b_id, []).append(copy.deepcopy(contract_b))
        state["rosters"][team_a] = team_a_after
        state["rosters"][team_b] = team_b_after
        season = int(state.get("season", 0))
        month = int(date_text[5:7])
        if agent_a:
            FinanceService.post(state, team_a, "AGENT_FEE", -agent_a, season=season, month=month,
                                description=f"Phí đại diện đổi người · {player_b.get('common_name', player_b_id)}",
                                related_id=swap_id, date=date_text, idempotency_key=f"{swap_id}:agent:{team_a}")
        if agent_b:
            FinanceService.post(state, team_b, "AGENT_FEE", -agent_b, season=season, month=month,
                                description=f"Phí đại diện đổi người · {player_a.get('common_name', player_a_id)}",
                                related_id=swap_id, date=date_text, idempotency_key=f"{swap_id}:agent:{team_b}")
        if cash_leg:
            FinanceService.post(state, payer, "TRANSFER_IN", -cash_leg, season=season, month=month,
                                description=f"Tiền cân bằng thương vụ đổi người · {swap_id}",
                                related_id=swap_id, date=date_text, idempotency_key=f"{swap_id}:cash:payer")
            FinanceService.post(state, recipient, "TRANSFER_OUT", cash_leg, season=season, month=month,
                                description=f"Nhận tiền cân bằng thương vụ đổi người · {swap_id}",
                                related_id=swap_id, date=date_text, idempotency_key=f"{swap_id}:cash:recipient")
        finance_a["transfer_budget"] = _money(float(finance_a.get("transfer_budget", 0)) - cost_a + (cash_leg if payer == team_b else 0.0))
        finance_b["transfer_budget"] = _money(float(finance_b.get("transfer_budget", 0)) - cost_b + (cash_leg if payer == team_a else 0.0))

        state["contracts"][player_a_id] = {
            "id": f"CT-{season}-{player_a_id}-SW{seq}", "player_id": player_a_id, "team_id": team_b,
            "start_season": season, "end_season": season + years_b - 1,
            "years_total": years_b, "years_remaining": years_b, "wage_per_year": wage_b,
            "signing_bonus": 0.0, "squad_role": demand_b.get("role", "ROTATION"),
            "status": "ACTIVE", "initialized": False,
        }
        state["contracts"][player_b_id] = {
            "id": f"CT-{season}-{player_b_id}-SW{seq}", "player_id": player_b_id, "team_id": team_a,
            "start_season": season, "end_season": season + years_a - 1,
            "years_total": years_a, "years_remaining": years_a, "wage_per_year": wage_a,
            "signing_bonus": 0.0, "squad_role": demand_a.get("role", "ROTATION"),
            "status": "ACTIVE", "initialized": False,
        }
        player_a["club"], player_a["wage"] = team_b, wage_b
        player_b["club"], player_b["wage"] = team_a, wage_a
        self._cancel_active_addons_for_player(
            player_a_id, date_text, "Cầu thủ rời CLB đã cam kết add-on trước khi đạt mốc thành tích.")
        self._cancel_active_addons_for_player(
            player_b_id, date_text, "Cầu thủ rời CLB đã cam kết add-on trước khi đạt mốc thành tích.")
        state.setdefault("season_stats", {})[player_a_id] = self.game._empty_stat()
        state.setdefault("season_stats", {})[player_b_id] = self.game._empty_stat()

        history_rows = []
        sell_on_count = {player_a_id: 0, player_b_id: 0}
        for pid, player, seller, buyer, notional_value, wage, years, agent in (
                (player_a_id, player_a, team_a, team_b, float(player_a.get("market_value", 0.0)), wage_b, years_b, agent_b),
                (player_b_id, player_b, team_b, team_a, float(player_b.get("market_value", 0.0)), wage_a, years_a, agent_a)):
            for clause in state.setdefault("sell_on_clauses", []):
                if (str(clause.get("player_id")) == pid and str(clause.get("owner_team")) == seller
                        and clause.get("status") == "ACTIVE"):
                    amount = _money(notional_value * (float(clause.get("sell_on_pct", 0.0)) / 100.0))
                    clause["status"] = "TRIGGERED"
                    clause["triggered_date"] = date_text
                    clause["triggered_swap_id"] = swap_id
                    sell_on_count[pid] += 1
                    if amount > 0:
                        state.setdefault("transfer_settlements", []).append({
                            "id": self._new_settlement_id(state), "player_id": pid,
                            "payer_team": seller, "recipient_team": str(clause.get("beneficiary_team", "")),
                            "amount": amount, "due_date": date_text, "kind": "SELL_ON",
                            "sell_on_clause_id": clause.get("id"), "swap_id": swap_id,
                            "status": "PENDING", "attempts": 0,
                        })
                    else:
                        clause["status"] = "PAID"
                        clause["paid_date"] = date_text
            row = {
                "swap_id": swap_id, "swap_key": swap_key, "date": date_text,
                "player_id": pid, "player_name": player.get("common_name", pid),
                "from_team": seller, "to_team": buyer, "fee": 0.0,
                "swap_market_value": _money(notional_value), "cash_adjustment": adjustment,
                "swap": True, "status": "Hoàn tất", "wage": wage, "years": years,
                "agent_fee": agent, "sell_on_triggered": sell_on_count[pid],
            }
            state.setdefault("transfer_history", []).append(row)
            history_rows.append(row)
        key_row = {"swap_id": swap_id, "swap_key": swap_key, "date": date_text,
                   "team_a": team_a, "team_b": team_b, "cash_adjustment": adjustment}
        completed.append(key_row)
        window_key = self._window_key(date_text)
        if window_key:
            counts = state.setdefault("transfer_deal_counts", {})
            counts[f"{window_key}:{team_a}"] = int(counts.get(f"{window_key}:{team_a}", 0)) + 1
            counts[f"{window_key}:{team_b}"] = int(counts.get(f"{window_key}:{team_b}", 0)) + 1
        state.setdefault("economy_notifications", []).extend([
            {"team_id": team_a, "date": date_text, "priority": "important",
             "message": f"Đã hoàn tất đổi {player_a.get('common_name', player_a_id)} lấy {player_b.get('common_name', player_b_id)} với {team_b}."},
            {"team_id": team_b, "date": date_text, "priority": "important",
             "message": f"Đã hoàn tất đổi {player_b.get('common_name', player_b_id)} lấy {player_a.get('common_name', player_a_id)} với {team_a}."},
        ])
        self.process_due_transfer_payments(date_text)
        return history_rows


class FinancialFairPlayService:
    """Configurable wage/revenue and rolling-loss checks; no retroactive score edits."""
    def __init__(self, config: dict):
        self.config = copy.deepcopy(config or {})

    def evaluate(self, state: dict, team: str, season: int) -> dict:
        report = state.get("season_financial_reports", {}).get(str(season), {})
        row = report.get("clubs", {}).get(team, {})
        ratio_limit = float(self.config.get("wage_revenue_ratio_limit", 0.75))
        revenue = float(row.get("revenue", 0.0))
        wage_cost = float(row.get("wage_cost", 0.0))
        amortisation = 0.0
        for transfer in state.get("transfer_history", []):
            if transfer.get("to_team") != team or transfer.get("status") != "Hoàn tất":
                continue
            transfer_season = int(str(transfer.get("date", "0"))[:4] or 0)
            if transfer_season == int(season):
                book_value = float(transfer.get("swap_market_value", 0.0) if transfer.get("swap")
                                   else transfer.get("fee", 0.0))
                amortisation += book_value / max(1, int(transfer.get("years", 1) or 1))
        wage_ratio = (wage_cost + amortisation) / revenue if revenue > 0 else None
        seasons = state.setdefault("ffp_history", {}).setdefault(team, [])
        prior = next((r for r in seasons if int(r.get("season", -1)) == int(season)), None)
        entry = {"season": int(season), "profit_loss": float(row.get("profit_loss", 0.0)),
                 "wage_revenue_ratio": wage_ratio, "ratio_limit": ratio_limit}
        if prior:
            prior.update(entry)
        else:
            seasons.append(entry)
        seasons.sort(key=lambda r: int(r.get("season", 0)))
        last_three = [r for r in seasons if int(season) - 2 <= int(r.get("season", 0)) <= int(season)]
        tier = state.get("club_finance", {}).get(team, {}).get("tier", "small")
        loss_limits = self.config.get("three_year_loss_limit", {"big": 90, "medium": 65, "small": 50})
        loss_limit = float(loss_limits.get(tier, 50.0))
        cumulative_loss = sum(max(0.0, -float(r.get("profit_loss", 0))) for r in last_three)
        loss_seasons = sum(1 for r in last_three if float(r.get("profit_loss", 0)) < 0)
        # Apply the rolling three-season loss test to the cumulative deficit,
        # not only when all three seasons are individually loss-making. A club
        # can have one profitable year and still breach the aggregate limit.
        loss_window = int(self.config.get("loss_window_seasons", 3))
        loss_breach = bool(len(last_three) >= loss_window and cumulative_loss > loss_limit)
        ban = loss_breach
        bans = state.setdefault("ffp_bans", {})
        existing_ban = bans.get(team, {})
        if ban:
            windows_remaining = max(int(existing_ban.get("windows_remaining", 0)),
                                    int(self.config.get("transfer_ban_windows", 1)))
            bans[team] = {"windows_remaining": windows_remaining, "issued_season": int(season),
                          "reason": "Lỗ lũy kế ba mùa vượt ngưỡng"}
        else:
            windows_remaining = int(existing_ban.get("windows_remaining", 0))
        ban = windows_remaining > 0
        # Point deductions are one-time per club/season and only follow a full
        # rolling-loss breach (not a warning-only wage ratio). This prevents a
        # repeated FFP evaluation during the season-close hook from stacking
        # the penalty.
        deduction_cfg = self.config.get("point_deduction", 0)
        if isinstance(deduction_cfg, dict):
            deduction_points = int(deduction_cfg.get(tier, 0))
        else:
            deduction_points = int(deduction_cfg or 0)
        deduction_points = max(0, min(30, deduction_points))
        # Discipline is issued after the audited season closes and is applied
        # to the following domestic league campaign, avoiding a circular result
        # where a deduction changes the prize money used to determine the breach.
        effective_season = int(season) + 1
        deduction_map = state.setdefault("ffp_point_deductions", {}).setdefault(str(effective_season), {})
        if loss_breach and deduction_points > 0 and team not in deduction_map:
            deduction_map[team] = {
                "points": deduction_points,
                "reason": "Lỗ lũy kế ba mùa vượt ngưỡng FFP",
                "issued_season": int(season),
                "effective_season": effective_season,
                "cumulative_loss": _money(cumulative_loss),
                "loss_limit": _money(loss_limit),
            }
        point_sanction = deduction_map.get(team, {})
        warning = (float(wage_ratio) > ratio_limit if wage_ratio is not None
                   else (wage_cost + amortisation) > 0.0)
        result = {"team": team, "season": int(season), "wage_revenue_ratio": wage_ratio,
                  "wage_cost_plus_amortisation": _money(wage_cost + amortisation),
                  "amortisation": _money(amortisation), "ratio_limit": ratio_limit,
                  "wage_warning": warning, "cumulative_loss_last_3": _money(cumulative_loss),
                  "loss_limit": loss_limit, "loss_seasons": loss_seasons,
                  "transfer_ban": ban, "transfer_ban_windows_remaining": windows_remaining,
                  "points_deducted": int(point_sanction.get("points", 0)),
                  "point_deduction_reason": point_sanction.get("reason", ""),
                  "point_deduction_effective_season": int(point_sanction.get("effective_season", effective_season)),
                  "status": "TRANSFER_BAN" if ban else ("WARNING" if warning else "COMPLIANT")}
        state.setdefault("ffp_status", {})[team] = result
        return result
