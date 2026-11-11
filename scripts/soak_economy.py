#!/usr/bin/env python3
"""Run a deterministic full-season economy soak in a temporary save directory.

Example:
    PYTHONPATH=src python scripts/soak_economy.py --seasons 5 --seed 20261102

The script never reads or writes the user's career save. It creates a fresh
career in a temporary directory, simulates complete seasons, and prints JSON.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from pathlib import Path

from sl_game import Game


def season_snapshot(game: Game, season: int) -> dict:
    state = game.s
    balances = {
        team: round(float(finance.get("balance", 0.0)), 3)
        for team, finance in state.get("club_finance", {}).items()
    }
    roster_sizes = {
        team: len(ids) for team, ids in state.get("rosters", {}).items()
    }
    report = state.get("season_financial_reports", {}).get(str(season), {})
    clubs = report.get("clubs", {}) if isinstance(report, dict) else {}
    losses = [team for team, row in clubs.items()
              if float(row.get("profit_loss", 0.0)) < 0]
    ratios = [float(row.get("wage_revenue_ratio")) for row in clubs.values()
              if row.get("wage_revenue_ratio") is not None]
    profits = [float(row.get("profit_loss", 0.0)) for row in clubs.values()]
    revenues = [float(row.get("revenue", 0.0)) for row in clubs.values()]
    costs = [float(row.get("costs", 0.0)) for row in clubs.values()]
    ai_cycles = state.get("ai_transfer_director_cycles", {})
    ai_rows = [deal for key, cycle in ai_cycles.items()
               if str(key).startswith(f"{int(season)}:")
               for deal in cycle.get("deals", [])]
    ai_deals = len(ai_rows)
    ai_loans = sum(1 for row in ai_rows if row.get("ai_loan") or row.get("loan"))
    ai_swaps = sum(1 for row in ai_rows if row.get("ai_swap") or row.get("swap"))
    renewal_rows = state.get("ai_renewal_campaigns", {}).get(str(int(season)), {})
    ai_renewals = sum(len(team_row.get("renewed", [])) for team_row in renewal_rows.values())
    ledger_rows = [tx for tx in state.get("finance_ledger", [])
                   if int(tx.get("season", -1)) == int(season)]
    owner_support = sum(float(tx.get("amount", 0.0)) for tx in ledger_rows
                        if tx.get("type") == "OWNER_INJECTION")
    debt_principal = -sum(float(tx.get("amount", 0.0)) for tx in ledger_rows
                          if tx.get("type") == "DEBT_REPAYMENT")
    sanctions = state.get("ffp_point_deductions", {}).get(str(int(season) + 1), {})
    bans = state.get("ffp_bans", {})
    restructures = [row for row in state.get("debt_restructure_history", [])
                    if int(row.get("season", -1)) == int(season)]
    return {
        "season": int(season),
        "minimum_balance_m_eur": min(balances.values(), default=0.0),
        "maximum_balance_m_eur": max(balances.values(), default=0.0),
        "clubs_with_season_loss": len(losses),
        "average_revenue_m_eur": round(sum(revenues) / len(revenues), 3) if revenues else 0.0,
        "average_cost_m_eur": round(sum(costs) / len(costs), 3) if costs else 0.0,
        "average_profit_m_eur": round(sum(profits) / len(profits), 3) if profits else 0.0,
        "maximum_profit_m_eur": round(max(profits), 3) if profits else 0.0,
        "maximum_wage_revenue_ratio": round(max(ratios), 3) if ratios else None,
        "ai_transfer_deals_in_window_year": ai_deals,
        "ai_permanent_deals": ai_deals - ai_loans - ai_swaps,
        "ai_development_loans": ai_loans,
        "ai_player_swaps": ai_swaps,
        "ai_contract_renewals": ai_renewals,
        "ffp_points_deducted_for_next_season": sum(int(row.get("points", 0)) for row in sanctions.values()),
        "ffp_sanctioned_clubs_for_next_season": sorted(sanctions),
        "ffp_active_transfer_bans": sum(1 for row in bans.values()
                                         if int(row.get("windows_remaining", 0)) > 0),
        "owner_support_m_eur": round(owner_support, 3),
        "debt_principal_paid_m_eur": round(debt_principal, 3),
        "debt_restructured_clubs": len(restructures),
        "minimum_squad_size": min(roster_sizes.values(), default=0),
        "maximum_squad_size": max(roster_sizes.values(), default=0),
        "club_count": len(balances),
        "squad_size_distribution": dict(Counter(roster_sizes.values())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", type=int, default=5, help="Number of full seasons (1–20).")
    parser.add_argument("--seed", type=int, default=20261102, help="Deterministic game seed.")
    args = parser.parse_args()
    if not 1 <= args.seasons <= 20:
        parser.error("--seasons must be between 1 and 20")

    results = []
    with tempfile.TemporaryDirectory(prefix="superleague-economy-soak-") as tmp:
        game = Game(data_dir=tmp, fresh=True, seed=args.seed, enable_google_sheets=False)
        for index in range(args.seasons):
            season = int(game.s.get("season", 0))
            game.simulate_full_season(persist_each_slot=False)
            results.append(season_snapshot(game, season))
            if index + 1 < args.seasons:
                game.next_season()

    payload = {
        "seed": args.seed,
        "seasons_requested": args.seasons,
        "seasons_completed": len(results),
        "temporary_save_only": True,
        "results": results,
        "all_cash_balances_nonnegative": all(row["minimum_balance_m_eur"] >= 0 for row in results),
        "all_squads_within_23_30": all(
            23 <= row["minimum_squad_size"] <= row["maximum_squad_size"] <= 30
            for row in results
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["all_cash_balances_nonnegative"] and payload["all_squads_within_23_30"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
