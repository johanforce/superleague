# -*- coding: utf-8 -*-
"""Deterministic AI transfer director for computer-controlled clubs.

The director makes a bounded number of decisions at the beginning of each
configured transfer window. It deliberately reuses the public transfer and
contract services so squad, FFP, budget, ledger and history rules stay unified.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .core import _money


DEFAULT_DIRECTOR_CONFIG: dict[str, Any] = {
    "enabled": True,
    "target_squad_size": 25,
    "max_total_deals_per_window": 12,
    "max_deals_per_team_per_window": 1,
    "max_targets_per_team": 8,
    "min_target_score": 42.0,
    "sales_enabled": True,
    "max_sales_per_team_per_window": 1,
    "minimum_sale_score": 25.0,
    "fire_sale": {
        "enabled": True,
        "cash_threshold_m": {"big": 5.0, "medium": 3.0, "small": 2.0},
        "minimum_sale_score": 18.0,
    },
    "swaps": {
        "enabled": True,
        "max_total_per_window": 2,
        "minimum_incoming_ovr_gap": 8.0,
        "max_cash_adjustment_m": 0.0,
    },
    "loans": {
        "enabled": True,
        "max_total_per_window": 4,
        "max_per_team_per_window": 1,
        "max_prospect_age": 23,
        "minimum_potential_gap": 4.0,
        "minimum_ovr_vs_need": 5.0,
        "borrower_wage_share_pct": {"big": 65.0, "medium": 60.0, "small": 55.0},
        "loan_fee_market_value_pct": 1.0,
        "max_loan_fee_m": 0.5,
        "option_to_buy": False,
        "option_to_buy_market_value_multiplier": 1.15,
        "borrower_tiers": ["small", "medium"],
        "parent_tiers": ["big", "medium"],
    },
    "max_transfer_budget_share": 0.35,
    "minimum_cash_reserve_m": 10.0,
    "excluded_teams": [],
    "renewals_enabled": True,
    "max_renewals_per_team_per_season": 2,
    "tier_policies": {
        "big": {"target_squad_size": 27, "target_ovr": 96.0,
                "max_transfer_budget_share": 0.50, "minimum_cash_reserve_m": 25.0,
                "min_target_score": 38.0},
        "medium": {"target_squad_size": 25, "target_ovr": 92.0,
                   "max_transfer_budget_share": 0.35, "minimum_cash_reserve_m": 15.0,
                   "min_target_score": 42.0},
        "small": {"target_squad_size": 24, "target_ovr": 86.0,
                  "max_transfer_budget_share": 0.20, "minimum_cash_reserve_m": 10.0,
                  "min_target_score": 47.0},
    },
}


class AITransferDirector:
    """Run a one-pass, idempotent transfer-market plan for all eligible AI clubs."""

    def __init__(self, game, transfer_service, config: dict | None = None):
        self.game = game
        self.transfers = transfer_service
        self.config = {**DEFAULT_DIRECTOR_CONFIG, **(config or {})}

    def _cycle_key(self, date_text: str) -> str:
        return self.transfers._window_key(date_text)

    def _team_policy(self, team: str) -> dict:
        finance = self.game.s.get("club_finance", {}).get(team, {})
        tier = str(finance.get("tier", "small")).lower()
        policy = dict(self.config)
        tier_policy = self.config.get("tier_policies", {}).get(tier, {})
        policy.update(tier_policy if isinstance(tier_policy, dict) else {})
        return policy

    def _eligible_budget(self, team: str) -> float:
        finance = self.game.s.get("club_finance", {}).get(team, {})
        policy = self._team_policy(team)
        transfer_budget = max(0.0, float(finance.get("transfer_budget", 0.0)))
        cash_available = max(0.0, float(finance.get("balance", 0.0)) -
                             float(policy.get("minimum_cash_reserve_m", 10.0)))
        share = max(0.0, min(1.0, float(policy.get("max_transfer_budget_share", 0.35))))
        return _money(min(transfer_budget * share, cash_available))

    def _try_sign_target(self, buyer: str, target: dict, date_text: str) -> dict | None:
        state = self.game.s
        pid = str(target.get("player_id", ""))
        if not pid or pid not in state.get("players", {}):
            return None

        player = state["players"][pid]
        contract = state.get("contracts", {}).get(pid, {})
        seller = str(contract.get("team_id") or player.get("club") or "")
        is_free_agent = bool(target.get("free_agent") or contract.get("status") in ("EXPIRED", "FREE_AGENT")
                             or pid in set(state.get("free_agents", [])) or not seller)
        if not is_free_agent and seller not in state.get("rosters", {}):
            return None

        market_value = max(0.0, float(player.get("market_value", 0.0)))
        asking = 0.0 if is_free_agent else self.transfers.asking_price(pid)
        spend_limit = self._eligible_budget(buyer)
        # A free agent still has agent/signing costs. Existing-player deals also
        # need enough room for a non-refundable approach fee and the transfer fee.
        if asking > spend_limit:
            return None

        finance = state.get("club_finance", {}).get(buyer, {})
        wage_room = (float(finance.get("wage_budget", 0.0)) -
                     self.transfers._annual_wage_committed(buyer))
        current_wage = max(0.05, float(contract.get("wage_per_year", player.get("wage", 0.05))))
        if wage_room < current_wage * 1.20:
            return None

        before_history = len(state.get("transfer_history", []))
        permission = self.transfers.request_permission_to_talk(buyer, pid, date_text)
        if not permission.get("accepted"):
            return None
        negotiation = permission.get("negotiation") or {}
        negotiation_id = negotiation.get("id")
        if not negotiation_id:
            return None

        try:
            demand = negotiation.get("player_demand", {})
            wage = _money(float(demand.get("wage", current_wage)))
            years = int(demand.get("years", 3))
            if wage > wage_room:
                negotiation["status"] = "FAILED"
                return None

            personal = self.transfers.submit_contract_offer(
                str(negotiation_id), wage=wage, years=years,
                signing_bonus=0.0, date_text=date_text,
            )
            if personal.get("status") != "AGREED":
                negotiation["status"] = "FAILED"
                return None

            # Free-agent negotiations complete inside submit_contract_offer.
            if is_free_agent:
                added = state.get("transfer_history", [])[before_history:]
                return added[-1] if added else None

            # Re-check available cash after the approach fee was posted.
            buyer_finance = state.get("club_finance", {}).get(buyer, {})
            if (asking > float(buyer_finance.get("transfer_budget", 0.0)) or
                    asking > float(buyer_finance.get("balance", 0.0))):
                negotiation["status"] = "FAILED"
                return None

            offer = self.transfers.submit_transfer_offer(
                buyer, pid, asking, str(negotiation_id), date_text)
            if offer.get("status") != "ACCEPTED":
                negotiation["status"] = "FAILED"
                return None
            try:
                return self.transfers.complete_transfer(str(offer["id"]), date_text)
            except Exception:
                # A failed completion must not leave an AI-created accepted offer
                # in the inbox looking actionable. The approach fee remains paid.
                offer["status"] = "REJECTED"
                offer["resolved_date"] = date_text
                offer["ai_failure"] = True
                negotiation["status"] = "FAILED"
                raise
        except Exception:
            negotiation["status"] = "FAILED"
            raise

    def _run_ai_sales(self, teams: list[str], excluded: set[str], date_text: str,
                      max_total: int, deals: list[dict], moved_player_ids: set[str] | None = None) -> list[dict]:
        """Sell surplus players only when the seller stays legal and a buyer needs them.

        The existing permanent-transfer pipeline is used for personal talks,
        transfer offers, fees, replacement validation, FFP and ledger posting.
        A seller is never listed or stripped just to create activity.
        """
        if not self.config.get("sales_enabled", True) or len(deals) >= max_total:
            return []
        state = self.game.s
        moved_player_ids = moved_player_ids if moved_player_ids is not None else set()
        tier_priority = {"small": 0, "medium": 1, "big": 2}
        sellers = sorted(teams, key=lambda team: (
            tier_priority.get(str(state.get("club_finance", {}).get(team, {}).get("tier", "small")), 1),
            str(team)))
        buyers = sorted(teams, key=lambda team: (
            {"big": 0, "medium": 1, "small": 2}.get(
                str(state.get("club_finance", {}).get(team, {}).get("tier", "small")), 2),
            -float(state.get("club_finance", {}).get(team, {}).get("reputation", 50.0)), str(team)))
        season_key = self._cycle_key(date_text)
        max_sales = max(0, int(self.config.get("max_sales_per_team_per_window", 1)))
        min_sale_score = float(self.config.get("minimum_sale_score", 25.0))
        min_sale_score_at_target = float(self.config.get("minimum_sale_score_at_target", 38.0))
        sales = []
        for seller in sellers:
            if seller in excluded or len(deals) + len(sales) >= max_total:
                continue
            seller_policy = self._team_policy(seller)
            seller_target = max(23, int(seller_policy.get("target_squad_size", 25)))
            seller_roster = state.get("rosters", {}).get(seller, [])
            if len(seller_roster) < 23:
                continue
            finance = state.get("club_finance", {}).get(seller, {})
            fire_sale_cfg = self.config.get("fire_sale", {})
            tier = str(finance.get("tier", "small"))
            fire_sale_thresholds = fire_sale_cfg.get("cash_threshold_m", {})
            fire_sale_threshold = float(fire_sale_thresholds.get(tier, 2.0))
            is_fire_sale = bool(fire_sale_cfg.get("enabled", True) and
                                float(finance.get("balance", 0.0)) <= fire_sale_threshold)
            if is_fire_sale:
                required_sale_score = float(fire_sale_cfg.get("minimum_sale_score", 18.0))
            else:
                required_sale_score = (min_sale_score if len(seller_roster) > seller_target
                                       else min_sale_score_at_target)
            counts = state.setdefault("transfer_deal_counts", {})
            count_key = f"{season_key}:{seller}"
            if int(counts.get(count_key, 0)) >= max_sales:
                continue
            report = self.transfers.analyze_squad(
                seller, target_ovr_override=float(seller_policy.get("target_ovr", 75.0)))
            candidates = [row for row in report.get("surplus_candidates", [])
                          if float(row.get("sell_score", 0.0)) >= required_sale_score]
            for candidate in candidates:
                if len(deals) + len(sales) >= max_total:
                    break
                pid = str(candidate.get("player_id", ""))
                if not pid or pid not in seller_roster or pid in moved_player_ids:
                    continue
                player = state.get("players", {}).get(pid, {})
                meta = state.get("player_meta", {}).get(pid, {})
                # Do not sell a high-upside prospect just because the current
                # squad is crowded; the seller needs a material sporting or
                # wage reason to accept the outgoing transfer.
                if float(meta.get("age", 26)) <= 21 and float(meta.get("potential", 0)) >= float(player.get("overall_rating", 0)) + 8:
                    continue
                group = str(candidate.get("position", ""))
                from .transfers import position_group
                group = position_group(group)
                moved = None
                for buyer in buyers:
                    if buyer == seller or buyer in excluded:
                        continue
                    buyer_policy = self._team_policy(buyer)
                    if len(state.get("rosters", {}).get(buyer, [])) >= max(23, int(buyer_policy.get("target_squad_size", 25))):
                        continue
                    buyer_count_key = f"{season_key}:{buyer}"
                    if int(counts.get(buyer_count_key, 0)) >= max(1, int(self.config.get("max_deals_per_team_per_window", 1))):
                        continue
                    analysis = self.transfers.analyze_squad(
                        buyer, target_ovr_override=float(buyer_policy.get("target_ovr", 75.0)))
                    need = next((row for row in analysis.get("needs", [])
                                 if row.get("position") == group), None)
                    if not need:
                        continue
                    if len(state.get("rosters", {}).get(buyer, [])) >= 30:
                        continue
                    target = {
                        "player_id": pid, "free_agent": False,
                        "score": self.transfers.target_score(buyer, pid, need),
                    }
                    if float(target["score"]) < float(buyer_policy.get("min_target_score", 42.0)):
                        continue
                    try:
                        moved = self._try_sign_target(buyer, target, date_text)
                    except (ValueError, KeyError, TypeError):
                        moved = None
                    if moved:
                        moved = dict(moved)
                        moved["ai_sale"] = True
                        moved["ai_seller"] = seller
                        moved["ai_fire_sale"] = is_fire_sale
                        moved_player_ids.add(pid)
                        sales.append(moved)
                        state.setdefault("economy_notifications", []).extend([
                            {"team_id": seller, "date": date_text, "priority": "normal",
                             "message": (f"AI thanh lý khẩn cấp {candidate.get('name', pid)} tới {buyer} do khó khăn thanh khoản."
                                         if is_fire_sale else
                                         f"AI thanh lý {candidate.get('name', pid)} tới {buyer} để cân đối đội hình và quỹ lương.")},
                            {"team_id": buyer, "date": date_text, "priority": "normal",
                             "message": f"AI chiêu mộ {candidate.get('name', pid)} từ {seller} theo nhu cầu vị trí {group}."},
                        ])
                        break
                if moved:
                    break
        return sales

    def _run_ai_swaps(self, teams: list[str], excluded: set[str], date_text: str,
                      max_total: int, deals: list[dict],
                      processed_teams: set[str] | None = None,
                      moved_player_ids: set[str] | None = None) -> list[dict]:
        """Match mutually useful surplus players and execute safe AI swaps."""
        cfg = self.config.get("swaps", {})
        if not cfg.get("enabled", True) or len(deals) >= max_total:
            return []
        state = self.game.s
        processed_teams = processed_teams if processed_teams is not None else set()
        moved_player_ids = moved_player_ids if moved_player_ids is not None else set()
        window_key = self._cycle_key(date_text)
        counts = state.setdefault("transfer_deal_counts", {})
        max_per_team = max(0, min(int(self.config.get("max_deals_per_team_per_window", 1)),
                                  int(self.config.get("swaps", {}).get("max_per_team_per_window", 1))))
        if max_per_team <= 0:
            return []
        from .transfers import position_group
        min_gap = float(cfg.get("minimum_incoming_ovr_gap", 8.0))
        cash_limit = max(0.0, float(cfg.get("max_cash_adjustment_m", 0.0)))
        analyses = {}
        eligible = [team for team in teams if team not in excluded and team not in processed_teams
                    and len(state.get("rosters", {}).get(team, [])) >= 23
                    and int(counts.get(f"{window_key}:{team}", 0)) < max_per_team]
        for team in eligible:
            policy = self._team_policy(team)
            analyses[team] = self.transfers.analyze_squad(
                team, target_ovr_override=float(policy.get("target_ovr", 75.0)))
        swaps: list[dict] = []
        swap_cap = max(0, int(cfg.get("max_total_per_window", 2)))

        for index, team_a in enumerate(sorted(eligible)):
            if len(deals) + len(swaps) >= max_total or len(swaps) >= swap_cap:
                break
            if team_a in processed_teams or int(counts.get(f"{window_key}:{team_a}", 0)) >= max_per_team:
                continue
            analysis_a = analyses.get(team_a, {})
            needs_a = {str(row.get("position", "")): row for row in analysis_a.get("needs", [])}
            surplus_a = analysis_a.get("surplus_candidates", [])
            for team_b in sorted(eligible)[index + 1:]:
                if team_b in processed_teams or int(counts.get(f"{window_key}:{team_b}", 0)) >= max_per_team:
                    continue
                analysis_b = analyses.get(team_b, {})
                needs_b = {str(row.get("position", "")): row for row in analysis_b.get("needs", [])}
                surplus_b = analysis_b.get("surplus_candidates", [])
                if not needs_a or not needs_b or not surplus_a or not surplus_b:
                    continue
                pair = None
                for candidate_a in sorted(surplus_a, key=lambda row: (-float(row.get("sell_score", 0)), str(row.get("player_id", "")))):
                    pid_a = str(candidate_a.get("player_id", ""))
                    if not pid_a or pid_a in moved_player_ids or pid_a not in state.get("rosters", {}).get(team_a, []):
                        continue
                    if self.transfers._active_loan_for_player(pid_a):
                        continue
                    group_a = position_group(state.get("players", {}).get(pid_a, {}).get("position", ""))
                    need_b = needs_b.get(group_a)
                    if not need_b:
                        continue
                    ovr_a = float(state.get("players", {}).get(pid_a, {}).get("overall_rating", 50))
                    for candidate_b in sorted(surplus_b, key=lambda row: (-float(row.get("sell_score", 0)), str(row.get("player_id", "")))):
                        pid_b = str(candidate_b.get("player_id", ""))
                        if not pid_b or pid_b in moved_player_ids or pid_b not in state.get("rosters", {}).get(team_b, []):
                            continue
                        if self.transfers._active_loan_for_player(pid_b):
                            continue
                        group_b = position_group(state.get("players", {}).get(pid_b, {}).get("position", ""))
                        need_a = needs_a.get(group_b)
                        if not need_a:
                            continue
                        ovr_b = float(state.get("players", {}).get(pid_b, {}).get("overall_rating", 50))
                        if (ovr_a < float(need_b.get("min_ovr", 0.0)) - min_gap or
                                ovr_b < float(need_a.get("min_ovr", 0.0)) - min_gap):
                            continue
                        value_a = float(state.get("players", {}).get(pid_a, {}).get("market_value", 0.0))
                        value_b = float(state.get("players", {}).get(pid_b, {}).get("market_value", 0.0))
                        adjustment = _money(value_b - value_a)
                        if abs(adjustment) > cash_limit + 1e-9:
                            continue
                        pair = (pid_a, pid_b, adjustment)
                        break
                    if pair:
                        break
                if not pair:
                    continue
                pid_a, pid_b, adjustment = pair
                try:
                    history_rows = self.transfers.complete_swap(
                        team_a, pid_a, team_b, pid_b, date_text, cash_adjustment=adjustment)
                except (ValueError, KeyError, TypeError):
                    continue
                if not history_rows or len(history_rows) != 2:
                    continue
                moved_player_ids.update((pid_a, pid_b))
                processed_teams.update((team_a, team_b))
                first, second = history_rows
                swaps.append({
                    "date": date_text, "swap": True, "ai_swap": True,
                    "swap_id": first.get("swap_id"), "status": "Hoàn tất",
                    "from_team": team_a, "to_team": team_b,
                    "player_id": pid_a, "player_name": first.get("player_name", pid_a),
                    "incoming_player_id": pid_b,
                    "incoming_player_name": second.get("player_name", pid_b),
                    "players": [dict(row) for row in history_rows],
                    "cash_adjustment": adjustment,
                })
                break
        return swaps

    def _run_ai_loans(self, teams: list[str], excluded: set[str], date_text: str,
                      max_total: int, deals: list[dict],
                      processed_teams: set[str] | None = None,
                      moved_player_ids: set[str] | None = None) -> list[dict]:
        """Arrange bounded development loans for surplus young players.

        The parent club must have positional cover and the borrower must have a
        genuine position need. All final squad/payroll/cash checks are delegated
        to ``complete_loan`` so AI and manual loans share the same invariants.
        """
        cfg = self.config.get("loans", {})
        if not cfg.get("enabled", True) or len(deals) >= max_total:
            return []
        state = self.game.s
        processed_teams = processed_teams if processed_teams is not None else set()
        moved_player_ids = moved_player_ids if moved_player_ids is not None else set()
        window_key = self._cycle_key(date_text)
        deal_counts = state.setdefault("transfer_deal_counts", {})
        max_loans = max(0, int(cfg.get("max_total_per_window", 4)))
        max_per_team = max(0, min(int(cfg.get("max_per_team_per_window", 1)),
                                  int(self.config.get("max_deals_per_team_per_window", 1))))
        min_gap = float(cfg.get("minimum_potential_gap", 4.0))
        max_age = float(cfg.get("max_prospect_age", 23))
        min_ovr_gap = float(cfg.get("minimum_ovr_vs_need", 5.0))
        min_groups = self.transfers.config.get("squad_rules", {}).get(
            "minimum_groups", {"GK": 3, "DEF": 7, "MID": 7, "FWD": 4})
        from .transfers import position_group

        borrowers = sorted(
            (team for team in teams if team not in excluded and team not in processed_teams
             and str(state.get("club_finance", {}).get(team, {}).get("tier", "small"))
             in set(cfg.get("borrower_tiers", ["small", "medium"]))),
            key=lambda team: ( {"small": 0, "medium": 1, "big": 2}.get(
                str(state.get("club_finance", {}).get(team, {}).get("tier", "small")), 2), str(team)))
        parents = sorted(
            (team for team in teams if team not in excluded and team not in processed_teams
             and str(state.get("club_finance", {}).get(team, {}).get("tier", "small"))
             in set(cfg.get("parent_tiers", ["big", "medium"]))),
            key=lambda team: ({"big": 0, "medium": 1, "small": 2}.get(
                str(state.get("club_finance", {}).get(team, {}).get("tier", "small")), 2), str(team)))
        loans: list[dict] = []
        season_end = date.fromisoformat(date_text).replace(year=date.fromisoformat(date_text).year + 1,
                                                           month=6, day=30)
        for parent in parents:
            parent_roster = state.get("rosters", {}).get(parent, [])
            if len(parent_roster) < 23 or int(deal_counts.get(f"{window_key}:{parent}", 0)) >= max_per_team:
                continue
            parent_groups: dict[str, int] = {group: 0 for group in ("GK", "DEF", "MID", "FWD")}
            for pid in parent_roster:
                group = position_group(state.get("players", {}).get(pid, {}).get("position", ""))
                parent_groups[group] = parent_groups.get(group, 0) + 1
            candidates = []
            for pid in parent_roster:
                if pid in moved_player_ids or self.transfers._active_loan_for_player(pid):
                    continue
                player = state.get("players", {}).get(pid, {})
                contract = state.get("contracts", {}).get(pid, {})
                if contract.get("status") not in ("ACTIVE", "EXPIRING") or int(contract.get("years_remaining", 0)) < 2:
                    continue
                meta = state.get("player_meta", {}).get(pid, {})
                age = float(meta.get("age", 26))
                ovr = float(player.get("overall_rating", 50))
                potential = float(meta.get("potential", ovr))
                group = position_group(player.get("position", ""))
                if age > max_age or potential - ovr < min_gap:
                    continue
                # Only loan from a positional group with cover above the legal minimum.
                if parent_groups.get(group, 0) <= int(min_groups.get(group, 0)):
                    continue
                score = (potential - ovr) * 2.0 + max(0.0, 23.0 - age) * 1.25 + ovr * 0.1
                candidates.append((score, pid, group, age, ovr, potential))
            candidates.sort(key=lambda row: (-row[0], row[1]))
            for _score, pid, group, age, ovr, potential in candidates:
                if len(deals) + len(loans) >= max_total or len(loans) >= max_loans:
                    return loans
                if int(deal_counts.get(f"{window_key}:{parent}", 0)) >= max_per_team:
                    break
                for borrower in borrowers:
                    if borrower == parent or borrower in processed_teams:
                        continue
                    if int(deal_counts.get(f"{window_key}:{borrower}", 0)) >= max_per_team:
                        continue
                    policy = self._team_policy(borrower)
                    roster = state.get("rosters", {}).get(borrower, [])
                    if len(roster) >= min(30, max(23, int(policy.get("target_squad_size", 25)))):
                        continue
                    analysis = self.transfers.analyze_squad(
                        borrower, target_ovr_override=float(policy.get("target_ovr", 75.0)))
                    need = next((row for row in analysis.get("needs", [])
                                 if str(row.get("position", "")) == group), None)
                    if not need or ovr < float(need.get("min_ovr", 0.0)) - min_ovr_gap:
                        continue
                    finance = state.get("club_finance", {}).get(borrower, {})
                    parent_finance = state.get("club_finance", {}).get(parent, {})
                    borrower_tier = str(finance.get("tier", "small"))
                    share_cfg = cfg.get("borrower_wage_share_pct", {})
                    wage_share = float(share_cfg.get(borrower_tier, 55.0))
                    wage = max(0.0, float(state.get("contracts", {}).get(pid, {}).get(
                        "wage_per_year", state.get("players", {}).get(pid, {}).get("wage", 0.0))))
                    borrower_room = float(finance.get("wage_budget", 0.0)) - self.transfers._annual_wage_committed(borrower)
                    if wage * wage_share / 100.0 > borrower_room + 1e-9:
                        continue
                    fee_pct = max(0.0, float(cfg.get("loan_fee_market_value_pct", 1.0)))
                    fee = _money(min(float(cfg.get("max_loan_fee_m", 0.5)),
                                     float(state.get("players", {}).get(pid, {}).get("market_value", 0.0)) * fee_pct / 100.0))
                    if fee > float(finance.get("balance", 0.0)) or fee > float(finance.get("transfer_budget", 0.0)):
                        continue
                    if (self.transfers.is_transfer_banned(borrower) and fee > 0):
                        continue
                    option = None
                    if cfg.get("option_to_buy", False):
                        option = _money(float(state.get("players", {}).get(pid, {}).get("market_value", 0.0)) *
                                        float(cfg.get("option_to_buy_market_value_multiplier", 1.15)))
                    try:
                        agreement = self.transfers.complete_loan(
                            borrower, pid, fee, wage_share, season_end.isoformat(), date_text,
                            option_to_buy=option)
                    except (ValueError, KeyError, TypeError):
                        continue
                    agreement["ai_director"] = True
                    agreement["development_loan"] = True
                    history = next((row for row in reversed(state.get("transfer_history", []))
                                    if row.get("loan_id") == agreement.get("id")), None)
                    if history is None:
                        continue
                    history.update({"ai_loan": True, "development_loan": True,
                                    "prospect_age": age, "prospect_potential": potential})
                    loans.append(history)
                    moved_player_ids.add(str(pid))
                    processed_teams.update((parent, borrower))
                    state.setdefault("economy_notifications", []).extend([
                        {"team_id": parent, "date": date_text, "priority": "normal",
                         "message": f"Đã cho mượn tài năng trẻ {history.get('player_name', pid)} tới {borrower} để tích lũy thời gian thi đấu."},
                        {"team_id": borrower, "date": date_text, "priority": "normal",
                         "message": f"Mượn thành công {history.get('player_name', pid)} từ {parent}; CLB chịu {wage_share:.0f}% lương."},
                    ])
                    break
                if int(deal_counts.get(f"{window_key}:{parent}", 0)) >= max_per_team:
                    break
        return loans

    def _run_renewal_campaign(self, team: str, date_text: str) -> list[dict]:
        """Renew the most important expiring players within a strict yearly cap."""
        if not self.config.get("renewals_enabled", True):
            return []
        state = self.game.s
        season_key = str(int(state.get("season", 0)))
        campaigns = state.setdefault("ai_renewal_campaigns", {})
        season_campaign = campaigns.setdefault(season_key, {})
        team_campaign = season_campaign.setdefault(team, {"processed": [], "renewed": []})
        max_renewals = max(0, int(self.config.get("max_renewals_per_team_per_season", 2)))
        if len(team_campaign.get("renewed", [])) >= max_renewals:
            return []

        candidates = []
        for pid in state.get("rosters", {}).get(team, []):
            contract = state.get("contracts", {}).get(pid, {})
            if (int(contract.get("years_remaining", 99)) <= 1 and
                    contract.get("status") in ("ACTIVE", "EXPIRING")):
                if pid not in team_campaign.get("processed", []):
                    candidates.append((self.transfers.retention_score(team, pid), pid))
        candidates.sort(key=lambda item: (-item[0], str(item[1])))

        renewed = []
        for _score, pid in candidates:
            team_campaign.setdefault("processed", []).append(pid)
            if len(team_campaign.get("renewed", [])) >= max_renewals:
                break
            try:
                negotiation = self.transfers.begin_renewal(team, pid, date_text)
                terms = negotiation.get("recommended_offer", {})
                finance = state.get("club_finance", {}).get(team, {})
                wage_room = (float(finance.get("wage_budget", 0.0)) -
                             self.transfers._annual_wage_committed(team, exclude_player=pid))
                wage = float(terms.get("wage", 0.0))
                bonus = float(terms.get("signing_bonus", 0.0))
                market_value = float(state.get("players", {}).get(pid, {}).get("market_value", 0.0))
                agent_fee = _money((bonus + market_value) * 0.05)
                if (wage <= 0 or wage > wage_room or
                        bonus + agent_fee > float(finance.get("balance", 0.0)) or
                        bonus + agent_fee > float(finance.get("transfer_budget", 0.0))):
                    negotiation["status"] = "FAILED"
                    negotiation["failure_reason"] = "AI không đủ quỹ lương/ngân sách để gia hạn an toàn."
                    continue
                result = self.transfers.submit_contract_offer(
                    negotiation["id"], wage, int(terms.get("years", 1)), bonus, date_text)
                if result.get("status") == "AGREED":
                    row = {"player_id": pid, "team_id": team, "wage": wage,
                           "years": int(terms.get("years", 1)), "date": date_text}
                    team_campaign.setdefault("renewed", []).append(row)
                    renewed.append(row)
            except (ValueError, KeyError, TypeError):
                # One player's failed contract must not block other AI clubs.
                continue
        if renewed:
            state.setdefault("economy_notifications", []).append({
                "team_id": team, "date": date_text, "priority": "normal",
                "message": f"AI đã gia hạn {len(renewed)} hợp đồng quan trọng.",
            })
        # Keep only current and two prior campaigns.
        if len(campaigns) > 3:
            for old in sorted(campaigns, key=lambda value: int(value))[:-3]:
                campaigns.pop(old, None)
        return renewed

    def run_window(self, date_text: str) -> dict:
        """Execute at most one market pass per calendar window.

        A per-team processed marker is written after each decision, so re-entering
        the hook after a handled team cannot make it buy twice in the same window.
        """
        if not self.config.get("enabled", True):
            return {"status": "disabled", "deals": []}
        current = date.fromisoformat(str(date_text))
        if not self.transfers.window_service.is_open(current.month):
            return {"status": "not_window", "deals": []}
        cycle_key = self._cycle_key(date_text)
        if not cycle_key:
            return {"status": "not_window", "deals": []}

        state = self.game.s
        cycles = state.setdefault("ai_transfer_director_cycles", {})
        cycle = cycles.setdefault(cycle_key, {
            "started_date": date_text, "processed_teams": [], "deals": [], "completed": False,
        })
        if cycle.get("completed"):
            return {"status": "already_run", "deals": list(cycle.get("deals", []))}

        processed = set(str(team) for team in cycle.get("processed_teams", []))
        excluded = set(str(team) for team in self.config.get("excluded_teams", []))
        moved_player_ids = set(str(pid) for pid in cycle.get("moved_player_ids", []))
        # A player may complete at most one permanent move per transfer window,
        # including a deal made manually before the AI's pass starts.
        for history in state.get("transfer_history", []):
            history_date = str(history.get("date", ""))[:10]
            if len(history_date) < 10 or not history.get("player_id"):
                continue
            try:
                if self._cycle_key(history_date) == cycle_key and history.get("status") in ("Hoàn tất", "Pre-contract hoàn tất"):
                    moved_player_ids.add(str(history["player_id"]))
            except (TypeError, ValueError):
                continue
        renewals = []
        if self.transfers.window_service.window_name(current.month) == "SUMMER":
            for renewal_team in sorted(state.get("rosters", {})):
                if renewal_team in excluded:
                    continue
                renewals.extend(self._run_renewal_campaign(renewal_team, date_text))
        max_total = max(0, int(self.config.get("max_total_deals_per_window", 12)))
        max_per_team = max(0, int(self.config.get("max_deals_per_team_per_window", 1)))
        max_targets = max(1, int(self.config.get("max_targets_per_team", 8)))
        deals = list(cycle.get("deals", []))

        if max_total > 0 and max_per_team > 0 and not cycle.get("sales_processed"):
            teams_for_sales = [team for team in state.get("rosters", {}) if team not in excluded]
            sales = self._run_ai_sales(teams_for_sales, excluded, date_text, max_total, deals,
                                       moved_player_ids=moved_player_ids)
            if sales:
                deals.extend(sales)
                cycle["deals"] = list(deals)
                for sale in sales:
                    for participant in (str(sale.get("from_team", "")), str(sale.get("to_team", ""))):
                        if participant and participant not in processed:
                            cycle.setdefault("processed_teams", []).append(participant)
                            processed.add(participant)
            cycle["sales_processed"] = True
            cycle["moved_player_ids"] = sorted(moved_player_ids)

        if max_total > 0 and max_per_team > 0 and not cycle.get("swaps_processed"):
            processed_before_swaps = set(processed)
            swaps = self._run_ai_swaps(
                [team for team in state.get("rosters", {}) if team not in excluded],
                excluded, date_text, max_total, deals,
                processed_teams=processed, moved_player_ids=moved_player_ids)
            if swaps:
                deals.extend(swaps)
                cycle["deals"] = list(deals)
                newly_processed = sorted(processed - processed_before_swaps)
                cycle.setdefault("processed_teams", []).extend(newly_processed)
                cycle["moved_player_ids"] = sorted(moved_player_ids)
            cycle["swaps_processed"] = True

        if max_total > 0 and max_per_team > 0 and not cycle.get("loans_processed"):
            processed_before_loans = set(processed)
            loans = self._run_ai_loans(
                [team for team in state.get("rosters", {}) if team not in excluded],
                excluded, date_text, max_total, deals,
                processed_teams=processed, moved_player_ids=moved_player_ids)
            if loans:
                deals.extend(loans)
                cycle["deals"] = list(deals)
                newly_processed = sorted(processed - processed_before_loans)
                cycle.setdefault("processed_teams", []).extend(newly_processed)
                cycle["moved_player_ids"] = sorted(moved_player_ids)
            cycle["loans_processed"] = True

        if max_total <= 0 or max_per_team <= 0:
            cycle["processed_teams"] = sorted(set(cycle.get("processed_teams", [])) | set(state.get("rosters", {})))
            cycle["completed"] = True
            cycle["completed_date"] = date_text
            return {"status": "completed", "deals": list(cycle.get("deals", [])),
                    "renewals": renewals, "processed_teams": len(cycle["processed_teams"])}

        if len(deals) < max_total:
            tier_priority = {"big": 0, "medium": 1, "small": 2}
            teams = sorted(state.get("rosters", {}), key=lambda team: (
                tier_priority.get(str(state.get("club_finance", {}).get(team, {}).get("tier", "small")), 2),
                -float(state.get("club_finance", {}).get(team, {}).get("reputation", 50.0)),
                str(team)))
            deal_counts = state.setdefault("transfer_deal_counts", {})
            for team in teams:
                if team in processed:
                    continue
                # Mark before the work; even a no-op decision is not repeated in
                # this window if this method is invoked again by a later fixture.
                cycle.setdefault("processed_teams", []).append(team)
                processed.add(team)
                if team in excluded or len(deals) >= max_total:
                    continue
                team_count_key = f"{cycle_key}:{team}"
                if int(deal_counts.get(team_count_key, 0)) >= max_per_team:
                    continue
                policy = self._team_policy(team)
                target_size = max(23, int(policy.get("target_squad_size", 25)))
                min_score = float(policy.get("min_target_score", 42.0))
                if len(state.get("rosters", {}).get(team, [])) >= target_size:
                    continue
                analysis = self.transfers.analyze_squad(
                    team, target_ovr_override=float(policy.get("target_ovr", 75.0)))
                needs = analysis.get("needs", [])
                if not needs:
                    continue

                success = None
                for need in needs:
                    targets = self.transfers.suggest_targets(
                        team, position=str(need.get("position", "")), limit=max_targets,
                        need_override=need)
                    for target in targets:
                        target_player_id = str(target.get("player_id", ""))
                        if target_player_id in moved_player_ids:
                            continue
                        if float(target.get("score", 0.0)) < min_score:
                            continue
                        target_contract = state.get("contracts", {}).get(target_player_id, {})
                        target_seller = str(target_contract.get("team_id") or
                                            state.get("players", {}).get(target_player_id, {}).get("club") or "")
                        if target_seller in state.get("rosters", {}) and int(
                                deal_counts.get(f"{cycle_key}:{target_seller}", 0)) >= max_per_team:
                            continue
                        try:
                            success = self._try_sign_target(team, target, date_text)
                        except (ValueError, KeyError, TypeError):
                            success = None
                        if success:
                            moved_player_ids.add(str(success.get("player_id", "")))
                            cycle["moved_player_ids"] = sorted(moved_player_ids)
                            break
                    if success:
                        break
                if success:
                    deals.append(success)
                    cycle["deals"] = list(deals)
                    seller_team = str(success.get("from_team", ""))
                    if seller_team in state.get("rosters", {}):
                        if seller_team not in processed:
                            processed.add(seller_team)
                            cycle.setdefault("processed_teams", []).append(seller_team)
                    else:
                        # Free-agent signings have no seller leg in complete_transfer,
                        # so reserve the buyer's single AI market slot here.
                        deal_counts[team_count_key] = int(deal_counts.get(team_count_key, 0)) + 1
                    state.setdefault("economy_notifications", []).append({
                        "team_id": team, "date": date_text, "priority": "normal",
                        "message": (f"Giám đốc chuyển nhượng AI hoàn tất chiêu mộ "
                                    f"{success.get('player_name', success.get('player_id', 'cầu thủ'))}."),
                    })

        cycle["deals"] = list(deals)
        cycle["completed"] = len(cycle.get("processed_teams", [])) >= len(state.get("rosters", {})) or len(deals) >= max_total
        cycle["completed_date"] = date_text if cycle["completed"] else ""
        # Keep only recent cycles to avoid unbounded save growth.
        if len(cycles) > 12:
            for old_key in sorted(cycles)[:-12]:
                cycles.pop(old_key, None)
        return {"status": "completed" if cycle["completed"] else "partial", "deals": deals,
                "renewals": renewals, "processed_teams": len(cycle.get("processed_teams", []))}
