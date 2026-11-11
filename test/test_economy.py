# -*- coding: utf-8 -*-
import copy
import tempfile
import unittest
from pathlib import Path

from economy import (
    FinanceService, WageService, SquadRulesService, TransferWindowService,
    EconomyRuntime, bootstrap_economy, load_economy_config, EventBus,
)
from economy.core import init_club_finance, position_group
from economy.transfers import FinancialFairPlayService
from sl_game import Game, POSITION_TEMPLATE

ROOT = Path(__file__).resolve().parents[1]


class EconomyCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config, cls.overrides = load_economy_config(ROOT)

    def test_wage_and_market_value_are_positive_and_ovr_monotonic(self):
        service = WageService(self.config)
        low = {"overall_rating": 65, "position": "CM", "age": 26, "potential": 65}
        middle = {**low, "overall_rating": 75, "potential": 75}
        high = {**low, "overall_rating": 85, "potential": 85}
        wages = [service.base_annual_wage(p, "medium") for p in (low, middle, high)]
        values = [service.market_value(p) for p in (low, middle, high)]
        self.assertTrue(all(value > 0 for value in wages + values))
        self.assertEqual(wages, sorted(wages))
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(abs(value / 0.05 - round(value / 0.05)) < 1e-8 for value in wages))
        self.assertLessEqual(service.base_annual_wage({**high, "overall_rating": 99, "potential": 99}, "big"), 35.0)

    def test_market_value_rewards_youth_potential_and_devalues_age(self):
        service = WageService(self.config)
        base = {
            "overall_rating": 80, "position": "CM", "age": 26, "potential": 80,
            "attacking_short_passing": 80, "skill_long_passing": 80, "mentality_vision": 80,
            "skill_ball_control": 80, "skill_dribbling": 80, "power_stamina": 80,
            "movement_reactions": 80,
        }
        prime_value = service.market_value(base)
        young_value = service.market_value({**base, "age": 19, "potential": 90})
        old_value = service.market_value({**base, "age": 35, "potential": 99})
        old_without_potential = service.market_value({**base, "age": 35, "potential": 80})
        self.assertGreater(young_value, prime_value)
        self.assertGreater(prime_value, old_value)
        self.assertEqual(old_value, old_without_potential,
                         "Potential should not create a transfer premium close to retirement.")

    def test_market_value_uses_position_relevant_attributes_and_has_sensible_bounds(self):
        service = WageService(self.config)
        base = {"overall_rating": 82, "position": "CM", "age": 26, "potential": 82}
        keys = ("attacking_short_passing", "skill_long_passing", "mentality_vision",
                "skill_ball_control", "skill_dribbling", "power_stamina", "movement_reactions")
        balanced = {**base, **{key: 82 for key in keys}}
        technical = {**base, **{key: 96 for key in keys}}
        weak = {**base, **{key: 60 for key in keys}}
        self.assertGreater(service.market_value(technical), service.market_value(balanced))
        self.assertGreater(service.market_value(balanced), service.market_value(weak))
        self.assertLessEqual(service.market_value({**base, "overall_rating": 99, "age": 18,
                                                   "potential": 99}), 250.0)
        self.assertGreater(service.market_value({**base, "overall_rating": 40, "age": 40,
                                                 "potential": 40}), 0.0)

    def test_market_value_age_curve_is_smooth(self):
        service = WageService(self.config)
        player = {"overall_rating": 85, "position": "CM", "potential": 85,
                  "attacking_short_passing": 85, "skill_long_passing": 85,
                  "mentality_vision": 85, "skill_ball_control": 85,
                  "skill_dribbling": 85, "power_stamina": 85, "movement_reactions": 85}
        before = service.market_value({**player, "age": 28.99})
        after = service.market_value({**player, "age": 29.01})
        self.assertLess(abs(before - after), 0.1)

    def test_market_value_model_migrates_existing_database_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261103, enable_google_sheets=False)
            pid = next(iter(game.s["players"]))
            game.s["players"][pid]["market_value"] = 999.0
            game.s["contracts"][pid]["market_value"] = 999.0
            game.s["market_value_model_version"] = 1
            game.save()

            # Opening the existing career should migrate values before returning
            # the game to the UI, then persist the new version into SQLite.
            resumed = Game(data_dir=tmp, fresh=False, seed=20261103, enable_google_sheets=False)
            expected = resumed.s["players"][pid]["market_value"]
            self.assertNotEqual(expected, 999.0)
            self.assertEqual(resumed.s["contracts"][pid]["market_value"], expected)
            self.assertEqual(resumed.s["market_value_model_version"], 2)
            self.assertTrue(all("market_value" in p for p in resumed.s["players"].values()))
            self.assertTrue(all("market_value" in entry.get("player", {})
                                for rows in resumed.s.get("academy", {}).values() for entry in rows))
            stored = resumed.database.load_career()
            self.assertEqual(stored["state"].get("market_value_model_version"), 2)
            self.assertFalse(resumed._ensure_economy_state(), "Valuation migration should be idempotent.")

    def test_finance_tiers_have_strict_initial_order(self):
        game = Game(data_dir=tempfile.mkdtemp(), fresh=True, seed=987654, enable_google_sheets=False)
        by_tier = {tier: [] for tier in ("big", "medium", "small")}
        for team, finance in game.s["club_finance"].items():
            by_tier[finance["tier"]].append(finance)
        self.assertTrue(by_tier["big"] and by_tier["medium"] and by_tier["small"])
        for key in ("balance", "transfer_budget", "wage_budget"):
            self.assertGreater(min(x[key] for x in by_tier["big"]),
                               max(x[key] for x in by_tier["medium"]))
            self.assertGreater(min(x[key] for x in by_tier["medium"]),
                               max(x[key] for x in by_tier["small"]))

    def test_fresh_game_gets_finance_contracts_and_player_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=10101, enable_google_sheets=False)
            self.assertEqual(len(game.s["club_finance"]), 24)
            self.assertEqual(len(game.s["contracts"]), 24 * 23)
            self.assertEqual(len(game.s["finance_ledger"]), 24)
            for team, ids in game.s["rosters"].items():
                self.assertEqual(len(ids), 23)
                active = [game.s["contracts"][pid] for pid in ids
                          if game.s["contracts"][pid]["status"] == "ACTIVE"]
                self.assertEqual(len(active), 23, team)
                for contract in active:
                    self.assertEqual(contract["years_total"], 3)
                    self.assertIn(contract["years_remaining"], (1, 2, 3))
                for pid in ids:
                    player = game.s["players"][pid]
                    self.assertGreater(player["wage"], 0)
                    self.assertGreater(player["market_value"], 0)
            self.assertEqual(game.s["economy_schema_version"], 2)

    def test_schema_v1_wage_budget_migration_caps_inflation_and_normalizes_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            team = next(t for t, tier in game.s["team_tiers"].items() if tier == "premier")
            ids = game.s["rosters"][team]
            # Simulate the P1 bootstrap result where a very high OVR squad had
            # pushed its annual wage budget to the full raw wage sum.
            game.s["economy_schema_version"] = 1
            game.s["club_finance"][team]["wage_budget"] = 1000.0
            for pid in ids:
                game.s["players"][pid]["wage"] = 35.0
                game.s["contracts"][pid]["wage_per_year"] = 35.0
            self.assertTrue(game._ensure_economy_state())
            finance = game.s["club_finance"][team]
            wages = [game.s["contracts"][pid]["wage_per_year"] for pid in ids]
            self.assertLessEqual(finance["wage_budget"], 253.0)
            self.assertLessEqual(sum(wages), finance["wage_budget"] * 0.805)
            self.assertTrue(all(game.s["players"][pid]["wage"] == game.s["contracts"][pid]["wage_per_year"] for pid in ids))
            snapshot = (finance["wage_budget"], list(wages))
            self.assertFalse(game._ensure_economy_state())
            self.assertEqual(snapshot, (finance["wage_budget"], [game.s["contracts"][pid]["wage_per_year"] for pid in ids]))
            self.assertEqual(game.s["economy_schema_version"], 2)

    def test_bootstrap_is_idempotent_and_does_not_change_seeded_finance(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            ledger_before = copy.deepcopy(game.s["finance_ledger"])
            contracts_before = copy.deepcopy(game.s["contracts"])
            changed = game._ensure_economy_state()
            self.assertFalse(changed)
            self.assertEqual(game.s["finance_ledger"], ledger_before)
            self.assertEqual(game.s["contracts"], contracts_before)

    def test_finance_post_updates_balance_and_ledger_together(self):
        state = {"club_finance": {"A": {"balance": 10.0, "history": []}}, "finance_ledger": []}
        tx = FinanceService.post(state, "A", "MATCHDAY", 2.3456, season=2026,
                                 month=9, description="Vé sân nhà")
        self.assertEqual(tx["amount"], 2.346)
        self.assertEqual(state["club_finance"]["A"]["balance"], 12.346)
        self.assertEqual(state["club_finance"]["A"]["history"], [tx["id"]])
        with self.assertRaises(ValueError):
            FinanceService.post(state, "A", "UNKNOWN", 2, season=2026, month=9, description="bad")

    def test_finance_post_is_idempotent_and_rejects_key_reuse(self):
        state = {"club_finance": {"A": {"balance": 10.0, "history": []}}, "finance_ledger": []}
        first = FinanceService.post(state, "A", "MATCHDAY", 2.0, season=2026, month=9,
                                    description="Doanh thu trận", idempotency_key="match:1:A")
        second = FinanceService.post(state, "A", "MATCHDAY", 2.0, season=2026, month=9,
                                     description="Retry", idempotency_key="match:1:A")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(state["club_finance"]["A"]["balance"], 12.0)
        self.assertEqual(len(state["finance_ledger"]), 1)
        with self.assertRaises(ValueError):
            FinanceService.post(state, "A", "MATCHDAY", 3.0, season=2026, month=9,
                                description="Sai payload", idempotency_key="match:1:A")

    def test_monthly_accounting_catches_up_long_gaps_and_uses_correct_season_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=5154, enable_google_sheets=False)
            team = next(iter(game.s["rosters"]))
            ids = list(game.s["rosters"][team])
            game.s["rosters"] = {team: ids}
            game.s["club_finance"] = {team: game.s["club_finance"][team]}
            game.s["contracts"] = {pid: game.s["contracts"][pid] for pid in ids}
            game.s["economy_last_processed_month"] = "2023-08"
            game.s["economy_processed_months"] = []
            game.economy_runtime.on_date_advanced(date="2026-12-15")
            self.assertEqual(game.s["economy_last_processed_month"], "2026-12")
            self.assertEqual(len(game.s["economy_processed_months"]), 40)
            september_2025 = next(tx for tx in game.s["finance_ledger"]
                                  if tx.get("teamId") == team and tx.get("relatedId") == "2025-09"
                                  and tx.get("type") == "WAGE")
            january_2026 = next(tx for tx in game.s["finance_ledger"]
                                if tx.get("teamId") == team and tx.get("relatedId") == "2026-01"
                                and tx.get("type") == "WAGE")
            august_2026 = next(tx for tx in game.s["finance_ledger"]
                               if tx.get("teamId") == team and tx.get("relatedId") == "2026-08"
                               and tx.get("type") == "WAGE")
            self.assertEqual(september_2025["season"], 2025)
            self.assertEqual(january_2026["season"], 2025)
            self.assertEqual(august_2026["season"], 2026)
            count = len(game.s["finance_ledger"])
            game.economy_runtime.on_date_advanced(date="2026-12-15")
            self.assertEqual(len(game.s["finance_ledger"]), count)

    def test_expired_contracts_are_not_charged_by_legacy_wage_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=5153, enable_google_sheets=False)
            team = next(iter(game.s["rosters"]))
            for pid in game.s["rosters"][team]:
                game.s["contracts"][pid]["status"] = "EXPIRED"
            before = sum(1 for tx in game.s["finance_ledger"] if tx.get("teamId") == team and tx.get("type") == "WAGE")
            game.economy_runtime._process_month("2026-09")
            after = [tx for tx in game.s["finance_ledger"] if tx.get("teamId") == team and tx.get("type") == "WAGE"]
            self.assertEqual(len(after), before)
            self.assertEqual(game.s["club_finance"][team]["monthly_rollup"]["2026-09"]["wage"], 0.0)

    def test_ffp_warns_when_revenue_is_zero_and_wages_exist(self):
        service = FinancialFairPlayService({"wage_revenue_ratio_limit": 0.75,
                                            "three_year_loss_limit": {"big": 90},
                                            "loss_window_seasons": 3})
        state = {
            "season_financial_reports": {"2026": {"clubs": {"A": {
                "revenue": 0.0, "wage_cost": 10.0, "profit_loss": -10.0
            }}}},
            "club_finance": {"A": {"tier": "big"}},
        }
        result = service.evaluate(state, "A", 2026)
        self.assertTrue(result["wage_warning"])
        self.assertIsNone(result["wage_revenue_ratio"])
        self.assertEqual(result["status"], "WARNING")

    def test_ffp_rolling_loss_uses_aggregate_three_year_window(self):
        service = FinancialFairPlayService({"wage_revenue_ratio_limit": 0.75,
                                            "three_year_loss_limit": {"big": 90},
                                            "transfer_ban_windows": 1,
                                            "loss_window_seasons": 3})
        state = {
            "season_financial_reports": {"2028": {"clubs": {"A": {
                "revenue": 100.0, "wage_cost": 10.0, "profit_loss": 5.0
            }}}},
            "club_finance": {"A": {"tier": "big"}},
            "ffp_history": {"A": [
                {"season": 2026, "profit_loss": -60.0},
                {"season": 2027, "profit_loss": -50.0},
            ]},
        }
        result = service.evaluate(state, "A", 2028)
        self.assertTrue(result["transfer_ban"])
        self.assertEqual(result["cumulative_loss_last_3"], 110.0)

    def test_ffp_point_deduction_is_idempotent_and_applies_next_season(self):
        service = FinancialFairPlayService({
            "wage_revenue_ratio_limit": 0.75,
            "three_year_loss_limit": {"small": 50.0},
            "transfer_ban_windows": 1, "loss_window_seasons": 3,
            "point_deduction": 3,
        })
        state = {
            "season_financial_reports": {"2028": {"clubs": {"A": {
                "revenue": 100.0, "wage_cost": 20.0, "profit_loss": -10.0
            }}}},
            "club_finance": {"A": {"tier": "small"}},
            "ffp_history": {"A": [
                {"season": 2026, "profit_loss": -35.0},
                {"season": 2027, "profit_loss": -30.0},
            ]},
        }
        first = service.evaluate(state, "A", 2028)
        second = service.evaluate(state, "A", 2028)
        self.assertEqual(first["points_deducted"], 3)
        self.assertEqual(first["point_deduction_effective_season"], 2029)
        self.assertEqual(second["points_deducted"], 3)
        self.assertEqual(len(state["ffp_point_deductions"]["2029"]), 1)

    def test_ffp_deduction_changes_domestic_table_but_not_raw_match_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=5157, enable_google_sheets=False)
            team = next(team for team, tier in game.s["season_tiers"].items()
                        if tier == "premier")
            game.s.setdefault("ffp_point_deductions", {}).setdefault(
                str(game.s["season"]), {})[team] = {"points": 3, "reason": "test"}
            table = game.league_phase1_table("premier")
            row = next(row for row in table if row["team"] == team)
            self.assertEqual(row["points_before_deduction"], 0)
            self.assertEqual(row["points_deduction"], 3)
            self.assertEqual(row["pts"], -3)

    def test_live_domestic_table_projects_ffp_point_deduction(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=5158, enable_google_sheets=False)
            slot = game.next_slot()
            specs = game.slot_fixture_specs(slot, include_played=False)
            spec = next(item for item in specs if (item.get("_meta") or {}).get("tier") in
                        ("premier", "fc1", "fc2"))
            preview = game.preview_slot_fixture(spec["spec_key"])
            team = preview["home"]
            game.s.setdefault("ffp_point_deductions", {}).setdefault(
                str(game.s["season"]), {})[team] = {"points": 4, "reason": "test"}
            snapshot = game.live_league_table(preview, [])
            self.assertTrue(snapshot["available"])
            row = next(row for row in snapshot["rows"] if row["team"] == team)
            self.assertEqual(row["points_before_deduction"], 0)
            self.assertEqual(row["points_deduction"], 4)
            self.assertEqual(row["pts"], -4)

    def test_owner_support_is_capped_and_excluded_from_operating_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=5154, enable_google_sheets=False)
            team = next(team for team, row in game.s["club_finance"].items()
                        if row["tier"] == "small")
            finance = game.s["club_finance"][team]
            finance["balance"] = -4.0
            injected = game.economy_runtime.stability.maybe_inject_owner_support(
                game, team, "2026-09")
            self.assertEqual(injected, 10.0)
            self.assertEqual(finance["balance"], 6.0)
            self.assertEqual(game.economy_runtime.stability.maybe_inject_owner_support(
                game, team, "2026-09"), 0.0)
            report = game.economy_runtime.build_season_report(2026)["clubs"][team]
            self.assertEqual(report["owner_support"], 10.0)
            self.assertEqual(report["revenue"], 0.0)
            self.assertEqual(report["profit_loss"], 0.0)

    def test_debt_restructure_schedules_affordable_idempotent_principal(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=5155, enable_google_sheets=False)
            team = next(team for team, row in game.s["club_finance"].items()
                        if row["tier"] == "small")
            finance = game.s["club_finance"][team]
            finance["debt"] = 60.0
            finance["balance"] = 100.0
            # Use the normal distress gate: cash is lowered after initial setup.
            finance["balance"] = 0.0
            terms = game.economy_runtime.stability.maybe_restructure_debt(
                game, team, "2026-09")
            self.assertIsNotNone(terms)
            self.assertEqual(terms["term_months"], 60)
            self.assertAlmostEqual(terms["annual_interest_rate"], 0.025)
            finance["balance"] = 100.0
            principal = game.economy_runtime.stability.process_monthly_debt_principal(
                game, team, "2026-10")
            self.assertEqual(principal, 1.0)
            self.assertEqual(finance["debt"], 59.0)
            ledger_count = len(game.s["finance_ledger"])
            second = game.economy_runtime.stability.process_monthly_debt_principal(
                game, team, "2026-10")
            self.assertEqual(second, 0.0)
            self.assertEqual(finance["debt"], 59.0)
            self.assertEqual(len(game.s["finance_ledger"]), ledger_count)

    def test_transfer_windows_follow_project_brief(self):
        service = TransferWindowService(self.config)
        self.assertTrue(all(service.is_open(month) for month in (6, 7, 8, 11, 12)))
        self.assertTrue(all(not service.is_open(month) for month in (1, 2, 3, 4, 5, 9, 10)))
        self.assertEqual(service.window_name(12), "WINTER")
        self.assertEqual(service.window_name(7), "SUMMER")

    def test_squad_rules_accept_current_23_player_rosters(self):
        game = Game(data_dir=tempfile.mkdtemp(), fresh=True, seed=42, enable_google_sheets=False)
        service = SquadRulesService(self.config)
        for team, ids in game.s["rosters"].items():
            result = service.validate(ids, game.s["players"])
            self.assertTrue(result["ok"], (team, result["errors"]))
        bad = service.validate(game.s["rosters"][next(iter(game.s["rosters"]))][:-1], game.s["players"])
        self.assertFalse(bad["ok"])


    def test_manual_club_override_is_exact_and_beats_division_mapping(self):
        state = {"club_finance": {}, "finance_ledger": []}
        overrides = {"clubs": {"Custom Club": {
            "tier": "big", "balance": 999.0, "transfer_budget": 150.0,
            "wage_budget": 280.0, "reputation": 97, "fan_base": 9_500_000,
        }}}
        finance = init_club_finance(
            state, "Custom Club", "fc2", "standard", self.config, overrides,
            seed=7, season=2026,
        )
        self.assertEqual(finance["tier"], "big")
        self.assertEqual(finance["balance"], 999.0)
        self.assertEqual(finance["transfer_budget"], 150.0)
        self.assertEqual(finance["wage_budget"], 280.0)
        self.assertEqual(finance["reputation"], 97.0)
        self.assertEqual(finance["fan_base"], 9_500_000)

    def test_game_publishes_match_and_calendar_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=8181, enable_google_sheets=False)
            received = []
            for event_name in ("MATCH_FINISHED", "ROUND_FINISHED", "DATE_ADVANCED"):
                game.subscribe_economy_event(
                    event_name,
                    lambda event=event_name, **payload: received.append((event, payload)),
                )
            game.advance_slot(persist=False)
            events = [event for event, _payload in received]
            self.assertIn("MATCH_FINISHED", events)
            self.assertIn("ROUND_FINISHED", events)
            self.assertIn("DATE_ADVANCED", events)
            self.assertEqual(events.count("MATCH_FINISHED"), 4)

    def test_game_publishes_daily_ticks_between_slots_without_repeating_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=8182, enable_google_sheets=False)
            days = []
            game.subscribe_economy_event("DAY_ADVANCED", lambda date, **_payload: days.append(date))
            first_slot = next(row for row in game.s["slots"] if not row["played"])
            first_date = first_slot["date"]
            team = next(iter(game.s["rosters"]))
            player_id = game.s["rosters"][team][0]
            negotiation = game.transfer_market.start_contract_negotiation(team, player_id, first_date)
            negotiation["due_date"] = first_date
            game.advance_slot(persist=False)
            self.assertTrue(days)
            self.assertEqual(days[-1], first_date)
            self.assertEqual(len(days), len(set(days)))
            self.assertEqual(negotiation["status"], "IN_PROGRESS", "Không expire trước hạn ngay trong ngày due.")
            previous_count = len(days)
            game.advance_slot(persist=False)
            self.assertEqual(len(days), len(set(days)))
            self.assertGreaterEqual(len(days), previous_count)
            self.assertEqual(negotiation["status"], "FAILED", "Daily tick phải expire vào ngày sau due_date.")
            self.assertEqual(game.s["economy_last_date"], days[-1])

    def test_event_bus_unsubscribe(self):
        bus = EventBus()
        calls = []
        def listener(**payload):
            calls.append(payload.get("value"))
        bus.subscribe("DEMO", listener)
        bus.publish("DEMO", value=1)
        bus.unsubscribe("DEMO", listener)
        bus.publish("DEMO", value=2)
        self.assertEqual(calls, [1])

    def test_feature_flag_off_is_non_destructive(self):
        state = {"existing": True}
        disabled = copy.deepcopy(self.config)
        disabled["feature_flags"]["ENABLE_ECONOMY"] = False
        changed = bootstrap_economy(state, rosters={}, players={}, player_meta={}, team_profiles={},
                                    team_tiers={}, config=disabled, overrides={}, seed=1, season=2026)
        self.assertFalse(changed)
        self.assertEqual(state, {"existing": True})



    def test_opening_finance_capital_is_not_counted_as_operating_revenue(self):
        from economy.services import EconomyRuntime
        game = Game(data_dir=tempfile.mkdtemp(), fresh=True, seed=191919, enable_google_sheets=False)
        team = next(iter(game.s["rosters"]))
        finance = game.s["club_finance"][team]
        opening = next(tx for tx in game.s["finance_ledger"]
                       if tx.get("teamId") == team and str(tx.get("relatedId", "")).startswith("finance-init:"))
        report = game.economy_runtime.build_season_report(int(game.s["season"]))
        row = report["clubs"][team]
        self.assertAlmostEqual(row["opening_capital"], float(opening["amount"]))
        self.assertNotIn("Vốn tài chính khởi tạo", str(row["by_type"]))
        self.assertEqual(row["revenue"], 0.0)
        self.assertEqual(row["profit_loss"], -row["costs"])

    def test_stadiums_are_seeded_and_matchday_revenue_is_recorded_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=5150, enable_google_sheets=False)
            self.assertEqual(len(game.s.get("stadiums", {})), 24)
            before = len(game.s["finance_ledger"])
            result = game.advance_slot(persist=False)
            self.assertTrue(result["matches"])
            match = result["matches"][0]
            self.assertIn("matchday", match)
            self.assertGreater(match["matchday"]["attendance"], 0)
            self.assertGreater(match["matchday"]["gross_revenue_m"], 0)
            after = len(game.s["finance_ledger"])
            self.assertGreater(after, before)
            # Simulate a crash after cash was posted but before the event's
            # processed marker was persisted. Ledger idempotency must still win.
            game.s["economy_processed_matches"].remove(str(match["id"]))
            game.economy_events.publish("MATCH_FINISHED", game=game, match=match)
            self.assertEqual(len(game.s["finance_ledger"]), after)

    def test_monthly_wage_and_operating_costs_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=5151, enable_google_sheets=False)
            game.advance_slot(persist=False)
            month = game.s.get("economy_last_processed_month")
            self.assertEqual(month, "2026-08")
            tx_types = [tx["type"] for tx in game.s["finance_ledger"]]
            self.assertIn("WAGE", tx_types)
            self.assertIn("SPONSOR", tx_types)
            self.assertIn("COMMERCIAL", tx_types)
            self.assertTrue(any(tx["description"].startswith("Chi phí vận hành") for tx in game.s["finance_ledger"]))
            count = len(game.s["finance_ledger"])
            game.economy_events.publish("DATE_ADVANCED", game=game, date="2026-08-28", month=8, season=2026)
            self.assertEqual(len(game.s["finance_ledger"]), count)
            # Simulate partial save/retry: remove only the month marker while
            # retaining already-posted ledger rows.
            game.s["economy_processed_months"].remove("2026-08")
            game.economy_runtime._process_month("2026-08")
            self.assertEqual(len(game.s["finance_ledger"]), count)

    def test_season_close_creates_position_broadcast_and_financial_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=5152, enable_google_sheets=False)
            tables = {}
            for tier in ("premier", "fc1", "fc2"):
                teams = [t for t, v in game.s["team_tiers"].items() if v == tier]
                tables[tier] = [{"team": t} for t in teams]
            report = game.economy_runtime.on_season_finishing({"season": 2026, "leagues": tables})
            self.assertEqual(len(report["clubs"]), 24)
            self.assertIn("financial_report", {"financial_report": report})
            self.assertTrue(any(tx["type"] == "BROADCAST" for tx in game.s["finance_ledger"]))
            self.assertTrue(any("thưởng vị trí" in tx["description"] for tx in game.s["finance_ledger"]))


    def test_season_close_revalues_players_and_keeps_contract_value_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261103, enable_google_sheets=False)
            team = next(iter(game.s["rosters"]))
            pid = game.s["rosters"][team][0]
            player = game.s["players"][pid]
            meta = game.s["player_meta"][pid]
            meta["age"] = 35.0
            meta["potential"] = 99.0
            player["market_value"] = 999.0
            game.s["contracts"][pid]["market_value"] = 999.0
            academy_entry = game.s["academy"][team][0]
            academy_entry["player"]["market_value"] = 999.0
            tables = {tier: [{"team": club} for club, value in game.s["team_tiers"].items()
                             if value == tier]
                      for tier in ("premier", "fc1", "fc2")}
            game.economy_runtime.on_season_finishing({"season": 2026, "leagues": tables})
            expected_input = dict(player)
            expected_input.update({"age": 35.0, "potential": 99.0})
            expected = WageService(game.economy_config).market_value(expected_input)
            self.assertEqual(player["market_value"], expected)
            self.assertEqual(game.s["contracts"][pid]["market_value"], expected)
            academy_player = academy_entry["player"]
            academy_meta = academy_entry["meta"]
            academy_input = dict(academy_player)
            academy_input.update({"age": academy_meta["age"], "potential": academy_meta["potential"]})
            self.assertEqual(academy_player["market_value"],
                             WageService(game.economy_config).market_value(academy_input))
            self.assertNotEqual(academy_player["market_value"], 999.0)

    def test_23_to_30_squad_range_and_contract_rollover(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=66110, enable_google_sheets=False)
            team = next(iter(game.s["rosters"]))
            ids = game.s["rosters"][team]
            # A club with a 24th player can release one expiring player without
            # dropping below the new 23-player floor.
            source = ids[0]
            extra = "TEST_EXTRA_ROLLOVER"
            player = copy.deepcopy(game.s["players"][source])
            player.update({"player_id": extra, "club": team})
            game.s["players"][extra] = player
            game.s["player_meta"][extra] = copy.deepcopy(game.s["player_meta"][source])
            game.s["dyn"][extra] = copy.deepcopy(game.s["dyn"].get(source, {}))
            game.s["season_stats"][extra] = game._empty_stat()
            contract = copy.deepcopy(game.s["contracts"][source])
            contract.update({"id": "CT-EXTRA", "player_id": extra, "team_id": team, "years_remaining": 2})
            contract["years_remaining"] = 3
            game.s["contracts"][extra] = contract
            ids.append(extra)
            # Avoid unrelated contracts expiring in the same focused fixture.
            for pid in ids:
                game.s["contracts"][pid]["years_remaining"] = 3
            expiring_pid = next(pid for pid in ids
                                if position_group(game.s["players"][pid].get("position")) in ("MID", "FWD"))
            game.s["contracts"][expiring_pid]["years_remaining"] = 1
            result = game.transfer_market.roll_contract_years(int(game.s["season"]))
            self.assertIn(expiring_pid, result["released"])
            self.assertEqual(len(game.s["rosters"][team]), 23)
            self.assertIn(expiring_pid, game.s["free_agents"])
            self.assertEqual(game.s["contracts"][expiring_pid]["status"], "EXPIRED")
            self.assertEqual(game.transfer_market.roll_contract_years(int(game.s["season"])),
                             {"expired": [], "pending": [], "released": []})
            game._check_state()

    def test_transfer_workflow_moves_player_and_balances_both_ledger_legs(self):
        from economy.core import position_group
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=112233, enable_google_sheets=False)
            service = game.transfer_market
            seller = next(t for t in game.s["rosters"] if game.s["team_tiers"][t] == "fc1")
            buyer = next(t for t in game.s["rosters"] if game.s["team_tiers"][t] == "premier")
            seller_ids = game.s["rosters"][seller]
            candidates = [pid for pid in seller_ids
                          if position_group(game.s["players"][pid]["position"]) in ("MID", "FWD")]
            player_id = min(candidates, key=lambda pid: float(game.s["players"][pid]["overall_rating"]))
            # Keep the test transfer affordable while retaining all other market logic.
            game.s["players"][player_id]["market_value"] = 50.0
            self.assertEqual(len(seller_ids), 23)

            deal_date = "2026-11-02"
            permission = service.request_permission_to_talk(buyer, player_id, deal_date)
            self.assertTrue(permission["accepted"])
            self.assertGreater(permission["approach_fee"], 0)
            negotiation = permission["negotiation"]
            demand = negotiation["player_demand"]
            personal = service.submit_contract_offer(
                negotiation["id"], demand["wage"], demand["years"], 0.0, deal_date)
            self.assertEqual(personal["status"], "AGREED")
            asking = service.asking_price(player_id)
            offer = service.submit_transfer_offer(buyer, player_id, asking, negotiation["id"], deal_date)
            self.assertEqual(offer["status"], "ACCEPTED")
            history = service.complete_transfer(offer["id"], deal_date)
            self.assertEqual(history["status"], "Hoàn tất")
            self.assertEqual(game.s["players"][player_id]["club"], buyer)
            self.assertNotIn(player_id, game.s["rosters"][seller])
            self.assertIn(player_id, game.s["rosters"][buyer])
            self.assertEqual(len(game.s["rosters"][seller]), 23)
            self.assertEqual(len(game.s["rosters"][buyer]), 24)
            self.assertTrue(history.get("seller_replacement_player_id"))
            self.assertIn(history["seller_replacement_player_id"], game.s["rosters"][seller])
            ledger = [tx for tx in game.s["finance_ledger"] if tx.get("relatedId") == player_id]
            self.assertTrue(any(tx["teamId"] == buyer and tx["type"] == "TRANSFER_IN" and tx["amount"] < 0 for tx in ledger))
            self.assertTrue(any(tx["teamId"] == seller and tx["type"] == "TRANSFER_OUT" and tx["amount"] > 0 for tx in ledger))
            game._check_state()

    def test_squad_analyzer_and_transfer_list_expose_affordable_market_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=554433, enable_google_sheets=False)
            service = game.transfer_market
            team = next(t for t in game.s["rosters"] if game.s["team_tiers"][t] == "premier")
            analysis = service.analyze_squad(team)
            self.assertEqual(analysis["squad_size"], 23)
            self.assertGreaterEqual(analysis["wage_room"], 0)
            targets = service.suggest_targets(team, limit=10)
            self.assertTrue(targets)
            self.assertTrue(all(row["score"] > 0 for row in targets))
            player_id = game.s["rosters"][team][0]
            listing = service.mark_for_sale(team, player_id)
            self.assertEqual(listing["team_id"], team)
            self.assertEqual(listing["player_id"], player_id)
            self.assertEqual(game.s["transfer_list"][-1]["status"], "LISTED")

    def test_renewal_uses_retention_score_and_replaces_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=4411, enable_google_sheets=False)
            service = game.transfer_market
            team = next(t for t, ids in game.s["rosters"].items()
                        if any(game.s["contracts"][pid]["years_remaining"] == 1 for pid in ids))
            pid = next(pid for pid in game.s["rosters"][team]
                       if game.s["contracts"][pid]["years_remaining"] == 1)
            old_contract_id = game.s["contracts"][pid]["id"]
            negotiation = service.begin_renewal(team, pid, "2026-11-02")
            recommended = negotiation["recommended_offer"]
            result = service.submit_contract_offer(
                negotiation["id"], recommended["wage"], recommended["years"], 0.0, "2026-11-02")
            self.assertEqual(result["status"], "AGREED")
            self.assertGreaterEqual(negotiation["retention_score"], 0)
            self.assertEqual(game.s["contracts"][pid]["years_remaining"], recommended["years"])
            self.assertNotEqual(game.s["contracts"][pid]["id"], old_contract_id)
            self.assertEqual(game.s["contract_history"][pid][-1]["id"], old_contract_id)

    def test_pre_contract_is_agreed_without_approach_fee_then_activates_next_season(self):
        from economy.core import position_group
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=77221, enable_google_sheets=False)
            service = game.transfer_market
            seller = next(t for t in game.s["rosters"] if game.s["team_tiers"][t] == "fc1")
            buyer = next(t for t in game.s["rosters"] if game.s["team_tiers"][t] == "premier")
            # Give seller a 24th player so a pre-agreed outgoing player can leave
            # at season end without violating the 23-player floor.
            seller_ids = game.s["rosters"][seller]
            source = seller_ids[0]
            extra = "TEST_EXTRA_PRECONTRACT"
            clone = copy.deepcopy(game.s["players"][source])
            clone.update({"player_id": extra, "club": seller})
            game.s["players"][extra] = clone
            game.s["player_meta"][extra] = copy.deepcopy(game.s["player_meta"][source])
            game.s["dyn"][extra] = copy.deepcopy(game.s["dyn"].get(source, {}))
            game.s["season_stats"][extra] = game._empty_stat()
            extra_contract = copy.deepcopy(game.s["contracts"][source])
            extra_contract.update({"id": "CT-EXTRA-PRE", "player_id": extra, "team_id": seller, "years_remaining": 3})
            game.s["contracts"][extra] = extra_contract
            seller_ids.append(extra)
            # Keep other deals away from expiry so this test isolates one
            # planned departure rather than a second unrelated free-agent exit.
            for pid in seller_ids:
                game.s["contracts"][pid]["years_remaining"] = 3
                game.s["contracts"][pid]["status"] = "ACTIVE"
            # Choose a player whose departure keeps all group minimums valid.
            player_id = next(pid for pid in seller_ids if pid != extra and
                             position_group(game.s["players"][pid]["position"]) in ("MID", "FWD"))
            game.s["contracts"][player_id]["years_remaining"] = 1
            game.s["contracts"][player_id]["status"] = "EXPIRING"
            before_ledger = len(game.s["finance_ledger"])
            permission = service.request_pre_contract(buyer, player_id, "2026-12-15")
            self.assertTrue(permission["pre_contract"])
            self.assertEqual(permission["approach_fee"], 0.0)
            negotiation = permission["negotiation"]
            demand = negotiation["player_demand"]
            result = service.submit_contract_offer(
                negotiation["id"], demand["wage"], demand["years"], 0.0, "2026-12-15")
            self.assertEqual(result["status"], "PRE_CONTRACT_AGREED")
            self.assertEqual(len(game.s["finance_ledger"]), before_ledger)
            self.assertIn(player_id, game.s["rosters"][seller])
            self.assertNotIn(player_id, game.s["rosters"][buyer])
            # Expiry releases player because seller has a safe 24 -> 23 path.
            rolled = service.roll_contract_years(int(game.s["season"]))
            self.assertIn(player_id, rolled["released"])
            # The next season is prepared before the pre-contract is activated.
            game.s["season_done"] = True
            game.next_season()
            self.assertEqual(game.s["contracts"][player_id]["team_id"], buyer)
            self.assertIn(player_id, game.s["rosters"][buyer])
            self.assertNotIn(player_id, game.s["rosters"][seller])
            agreement = game.s["pre_contract_agreements"][0]
            self.assertEqual(agreement["status"], "COMPLETED")
            self.assertTrue(game.s["transfer_history"][-1]["pre_contract"])
            game._check_state()

    def test_ai_transfer_director_executes_bounded_real_deals_once_per_window(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            original_suggest = service.suggest_targets
            with patch.object(service, "analyze_squad", return_value={
                "needs": [{"position": "DEF", "priority": 5, "min_ovr": 75}]
            }), patch.object(service, "suggest_targets", side_effect=lambda team, position=None, limit=10, need_override=None:
                          original_suggest(team, position=position, limit=limit, need_override=need_override)):
                result = game.economy_runtime.ai_transfer_director.run_window("2026-08-08")
            self.assertEqual(result["status"], "completed")
            self.assertGreater(len(result["deals"]), 0)
            self.assertLessEqual(len(result["deals"]), 12)
            self.assertTrue(all(row["status"] == "Hoàn tất" for row in result["deals"]))
            buyers = [row["to_team"] for row in result["deals"]]
            self.assertEqual(len(buyers), len(set(buyers)), "Mỗi CLB chỉ được mua tối đa một lần/cửa sổ.")
            history_count = len(game.s.get("transfer_history", []))
            second = game.economy_runtime.ai_transfer_director.run_window("2026-08-15")
            self.assertEqual(second["status"], "already_run")
            self.assertEqual(len(game.s.get("transfer_history", [])), history_count)
            for team, roster in game.s["rosters"].items():
                self.assertGreaterEqual(len(roster), 23, team)
                self.assertLessEqual(len(roster), 30, team)

    def test_ai_director_executes_mutually_beneficial_swap_once(self):
        from unittest.mock import patch
        from economy.core import position_group
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            director = game.economy_runtime.ai_transfer_director
            teams = [team for team, row in game.s["club_finance"].items() if row["tier"] == "medium"]
            team_a, team_b = teams[:2]
            counts_a = {group: 0 for group in ("GK", "DEF", "MID", "FWD")}
            counts_b = dict(counts_a)
            for pid in game.s["rosters"][team_a]:
                counts_a[position_group(game.s["players"][pid]["position"])] += 1
            for pid in game.s["rosters"][team_b]:
                counts_b[position_group(game.s["players"][pid]["position"])] += 1
            minimums = {"GK": 3, "DEF": 7, "MID": 7, "FWD": 4}
            candidates_a = [pid for pid in game.s["rosters"][team_a]
                            if counts_a[position_group(game.s["players"][pid]["position"])] >
                            minimums[position_group(game.s["players"][pid]["position"])]
                            and game.s["contracts"][pid].get("status") == "ACTIVE"]
            candidates_b = [pid for pid in game.s["rosters"][team_b]
                            if counts_b[position_group(game.s["players"][pid]["position"])] >
                            minimums[position_group(game.s["players"][pid]["position"])]
                            and game.s["contracts"][pid].get("status") == "ACTIVE"]
            pair = next(( (a, b) for a in candidates_a for b in candidates_b
                          if position_group(game.s["players"][a]["position"]) !=
                             position_group(game.s["players"][b]["position"])
                          and abs(float(game.s["players"][a].get("market_value", 0)) -
                                  float(game.s["players"][b].get("market_value", 0))) <= 15.0), None)
            self.assertIsNotNone(pair, "Seeded medium clubs should have a balanced cross-position swap pair.")
            pid_a, pid_b = pair
            group_a = position_group(game.s["players"][pid_a]["position"])
            group_b = position_group(game.s["players"][pid_b]["position"])
            original_analyze = service.analyze_squad

            def fake_analyze(team, target_ovr_override=None):
                if team == team_a:
                    return {"needs": [{"position": group_b, "priority": 5, "min_ovr": 70.0}],
                            "surplus_candidates": [{"player_id": pid_a, "sell_score": 90.0}]}
                if team == team_b:
                    return {"needs": [{"position": group_a, "priority": 5, "min_ovr": 70.0}],
                            "surplus_candidates": [{"player_id": pid_b, "sell_score": 90.0}]}
                return original_analyze(team, target_ovr_override=target_ovr_override)

            processed = set()
            moved = set()
            with patch.object(service, "analyze_squad", side_effect=fake_analyze):
                swaps = director._run_ai_swaps(
                    [team_a, team_b], set(), "2026-08-08", 12, [],
                    processed_teams=processed, moved_player_ids=moved)
                retry = director._run_ai_swaps(
                    [team_a, team_b], set(), "2026-08-15", 12, swaps,
                    processed_teams=processed, moved_player_ids=moved)
            self.assertEqual(len(swaps), 1)
            self.assertEqual(retry, [])
            self.assertTrue(swaps[0]["ai_swap"])
            self.assertEqual(swaps[0]["status"], "Hoàn tất")
            self.assertIn(pid_a, game.s["rosters"][team_b])
            self.assertIn(pid_b, game.s["rosters"][team_a])
            self.assertEqual(processed, {team_a, team_b})
            self.assertEqual(moved, {pid_a, pid_b})
            game._check_state()

    def test_ai_director_loans_high_potential_surplus_prospect_safely(self):
        from unittest.mock import patch
        from economy.core import position_group
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            director = game.economy_runtime.ai_transfer_director
            parent = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "big")
            borrower = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "small")
            counts = {group: 0 for group in ("GK", "DEF", "MID", "FWD")}
            for pid in game.s["rosters"][parent]:
                counts[position_group(game.s["players"][pid]["position"])] += 1
            minimums = {"GK": 3, "DEF": 7, "MID": 7, "FWD": 4}
            candidates = [pid for pid in game.s["rosters"][parent]
                          if counts[position_group(game.s["players"][pid]["position"])] >
                          minimums[position_group(game.s["players"][pid]["position"])]
                          and float(game.s["player_meta"][pid].get("age", 26)) <= 23
                          and float(game.s["player_meta"][pid].get("potential", 0)) -
                          float(game.s["players"][pid].get("overall_rating", 0)) >= 4
                          and int(game.s["contracts"][pid].get("years_remaining", 0)) >= 2]
            self.assertTrue(candidates, "Seed fixture should expose a loanable academy-age prospect.")
            player_id = candidates[0]
            group = position_group(game.s["players"][player_id]["position"])
            original_analyze = service.analyze_squad

            def fake_analyze(team, target_ovr_override=None):
                if team == borrower:
                    return {"needs": [{"position": group, "priority": 5, "min_ovr": 80.0}]}
                return original_analyze(team, target_ovr_override=target_ovr_override)

            processed = set()
            with patch.object(service, "analyze_squad", side_effect=fake_analyze):
                loans = director._run_ai_loans(
                    [parent, borrower], set(), "2026-08-08", 12, [],
                    processed_teams=processed, moved_player_ids=set())
                retry = director._run_ai_loans(
                    [parent, borrower], set(), "2026-08-15", 12, loans,
                    processed_teams=processed, moved_player_ids={player_id})
            self.assertEqual(len(loans), 1)
            self.assertEqual(retry, [])
            loan = loans[0]
            self.assertTrue(loan["ai_loan"])
            self.assertTrue(loan["development_loan"])
            self.assertEqual(loan["from_team"], parent)
            self.assertEqual(loan["to_team"], borrower)
            self.assertEqual(loan["loan_end_date"], "2027-06-30")
            self.assertEqual(game.s["contracts"][player_id]["status"], "ON_LOAN")
            self.assertNotIn(player_id, game.s["rosters"][parent])
            self.assertIn(player_id, game.s["rosters"][borrower])
            self.assertEqual(processed, {parent, borrower})
            game._check_state()

    def test_ai_window_runs_development_loans_before_remaining_buy_decisions(self):
        from unittest.mock import patch
        from economy.core import position_group
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            director = game.economy_runtime.ai_transfer_director
            director.config["sales_enabled"] = False
            parent = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "big")
            borrower = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "small")
            counts = {group: 0 for group in ("GK", "DEF", "MID", "FWD")}
            for pid in game.s["rosters"][parent]:
                counts[position_group(game.s["players"][pid]["position"])] += 1
            minimums = {"GK": 3, "DEF": 7, "MID": 7, "FWD": 4}
            player_id = next(pid for pid in game.s["rosters"][parent]
                             if counts[position_group(game.s["players"][pid]["position"])] >
                             minimums[position_group(game.s["players"][pid]["position"])]
                             and float(game.s["player_meta"][pid].get("age", 26)) <= 23
                             and float(game.s["player_meta"][pid].get("potential", 0)) -
                             float(game.s["players"][pid].get("overall_rating", 0)) >= 4
                             and int(game.s["contracts"][pid].get("years_remaining", 0)) >= 2)
            group = position_group(game.s["players"][player_id]["position"])

            def fake_analyze(team, target_ovr_override=None):
                if team == borrower:
                    return {"needs": [{"position": group, "priority": 5, "min_ovr": 80.0}],
                            "surplus_candidates": []}
                return {"needs": [], "surplus_candidates": []}

            with patch.object(service, "analyze_squad", side_effect=fake_analyze):
                result = director.run_window("2026-08-08")
            loans = [row for row in result["deals"] if row.get("ai_loan")]
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(loans), 1)
            self.assertEqual(loans[0]["player_id"], player_id)
            cycle = game.s["ai_transfer_director_cycles"]["2026:SUMMER"]
            self.assertTrue(cycle["loans_processed"])
            self.assertIn(parent, cycle["processed_teams"])
            self.assertIn(borrower, cycle["processed_teams"])
            second = director.run_window("2026-08-15")
            self.assertEqual(second["status"], "already_run")

    def test_ai_renewal_campaign_is_capped_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=404040, enable_google_sheets=False)
            team = next(iter(game.s["rosters"]))
            ids = list(game.s["rosters"][team])
            for pid in ids:
                game.s["contracts"][pid]["years_remaining"] = 3
                game.s["contracts"][pid]["status"] = "ACTIVE"
            for pid in ids[:3]:
                game.s["contracts"][pid]["years_remaining"] = 1
                game.s["contracts"][pid]["status"] = "EXPIRING"
            director = game.economy_runtime.ai_transfer_director
            renewed = director._run_renewal_campaign(team, "2026-08-08")
            self.assertLessEqual(len(renewed), 2)
            self.assertGreater(len(renewed), 0)
            second = director._run_renewal_campaign(team, "2026-08-15")
            self.assertEqual(second, [])
            self.assertEqual(len(game.s["ai_renewal_campaigns"][str(game.s["season"])][team]["renewed"]),
                             len(renewed))

    def test_transfer_and_ai_hooks_still_run_when_monthly_accounting_is_disabled(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=404042, enable_google_sheets=False)
            runtime = game.economy_runtime
            runtime.config["monthly_accounting_enabled"] = False
            with patch.object(runtime.transfers, "advance_to_date") as advance_mock, \
                 patch.object(runtime.ai_transfer_director, "run_window", return_value={"status": "completed"}) as ai_mock:
                runtime.on_day_advanced(game=game, date="2026-08-08")
                runtime.on_date_advanced(game=game, date="2026-08-08")
            advance_mock.assert_called_once_with("2026-08-08")
            ai_mock.assert_called_once_with("2026-08-08")
            self.assertNotIn("economy_last_processed_month", game.s)

    def test_ai_transfer_director_does_not_run_outside_a_transfer_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=404041, enable_google_sheets=False)
            result = game.economy_runtime.ai_transfer_director.run_window("2026-09-10")
            self.assertEqual(result["status"], "not_window")
            self.assertNotIn("ai_transfer_director_cycles", game.s)

    def test_automatic_negotiation_expiry_and_ffp_amortisation(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=5510, enable_google_sheets=False)
            team = next(iter(game.s["rosters"]))
            pid = game.s["rosters"][team][0]
            negotiation = game.transfer_market.start_contract_negotiation(team, pid, "2026-11-01")
            events = game.transfer_market.advance_to_date("2026-11-20")
            self.assertEqual(negotiation["status"], "FAILED")
            self.assertIn("negotiation_expired", [x["type"] for x in events])

            ffp = FinancialFairPlayService({"enabled": True, "wage_revenue_ratio_limit": 0.75,
                                            "three_year_loss_limit": {"small": 50},
                                            "transfer_ban_windows": 1})
            state = {
                "club_finance": {team: {"tier": "small"}},
                "season_financial_reports": {"2026": {"clubs": {team: {
                    "revenue": 100.0, "wage_cost": 70.0, "profit_loss": -40.0}}}},
                "ffp_history": {team: [
                    {"season": 2024, "profit_loss": -30.0},
                    {"season": 2025, "profit_loss": -30.0}]},
                "transfer_history": [{"to_team": team, "status": "Hoàn tất", "date": "2026-10-01",
                                       "fee": 60.0, "years": 2}],
            }
            report = ffp.evaluate(state, team, 2026)
            self.assertAlmostEqual(report["amortisation"], 30.0)
            self.assertAlmostEqual(report["wage_revenue_ratio"], 1.0)
            self.assertTrue(report["transfer_ban"])
            game.s["ffp_bans"] = state["ffp_bans"]
            game.s["ffp_status"] = state["ffp_status"]
            self.assertTrue(game.transfer_market.is_transfer_banned(team))
            game.transfer_market.on_transfer_window_closed(12)
            self.assertFalse(game.transfer_market.is_transfer_banned(team))


    def test_ai_director_uses_distinct_budget_and_squad_policies_by_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            director = game.economy_runtime.ai_transfer_director
            big = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "big")
            small = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "small")
            big_policy = director._team_policy(big)
            small_policy = director._team_policy(small)
            self.assertGreater(big_policy["target_squad_size"], small_policy["target_squad_size"])
            self.assertGreater(big_policy["max_transfer_budget_share"],
                               small_policy["max_transfer_budget_share"])
            self.assertGreater(director._eligible_budget(big), director._eligible_budget(small))

    def test_ai_market_window_never_moves_same_player_twice_or_exceeds_team_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            director = game.economy_runtime.ai_transfer_director
            result = director.run_window("2026-08-08")
            self.assertIn(result["status"], ("completed", "partial"))
            deals = result["deals"]
            moved_ids = [str(row.get("player_id")) for row in deals]
            self.assertEqual(len(moved_ids), len(set(moved_ids)))
            self.assertLessEqual(len(deals), int(director.config["max_total_deals_per_window"]))
            counts = game.s.get("transfer_deal_counts", {})
            for team in game.s["rosters"]:
                self.assertLessEqual(int(counts.get(f"2026:SUMMER:{team}", 0)),
                                     int(director.config["max_deals_per_team_per_window"]))
            self.assertTrue(all(len(ids) >= 23 for ids in game.s["rosters"].values()))
            game._check_state()

    def test_ai_director_sells_surplus_player_through_safe_transfer_pipeline(self):
        from unittest.mock import patch
        from economy.core import position_group
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            seller = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "small")
            buyer = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "big")
            groups = {name: 0 for name in ("GK", "DEF", "MID", "FWD")}
            for pid in game.s["rosters"][seller]:
                groups[position_group(game.s["players"][pid]["position"])] += 1
            minimums = {"GK": 3, "DEF": 7, "MID": 7, "FWD": 4}
            eligible = [pid for pid in game.s["rosters"][seller]
                        if groups[position_group(game.s["players"][pid]["position"])] >
                        minimums[position_group(game.s["players"][pid]["position"])]
                        and game.s["contracts"][pid].get("squad_role") != "STAR"]
            player_id = min(eligible, key=lambda pid: game.s["players"][pid].get("overall_rating", 50))
            group = position_group(game.s["players"][player_id]["position"])
            original_analyze = service.analyze_squad
            def fake_analyze(team, target_ovr_override=None):
                if team == seller:
                    return {"needs": [], "surplus_candidates": [{
                        "player_id": player_id, "name": game.s["players"][player_id]["common_name"],
                        "position": game.s["players"][player_id]["position"], "sell_score": 90.0,
                    }]}
                if team == buyer:
                    return {"needs": [{"position": group, "min_ovr": 70.0, "priority": 5}],
                            "surplus_candidates": []}
                return original_analyze(team, target_ovr_override=target_ovr_override)
            director = game.economy_runtime.ai_transfer_director
            game.s["club_finance"][seller]["balance"] = 0.0
            with patch.object(service, "analyze_squad", side_effect=fake_analyze), \
                 patch.object(service, "target_score", return_value=100.0):
                result = director._run_ai_sales([seller, buyer], set(), "2026-08-08", 12, [])
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0]["ai_sale"])
            self.assertEqual(result[0]["from_team"], seller)
            self.assertEqual(result[0]["to_team"], buyer)
            self.assertEqual(result[0]["status"], "Hoàn tất")
            self.assertTrue(result[0]["ai_fire_sale"])
            self.assertEqual(len(game.s["rosters"][seller]), 23)
            self.assertEqual(len(game.s["rosters"][buyer]), 24)
            self.assertNotIn(player_id, game.s["rosters"][seller])
            self.assertIn(player_id, game.s["rosters"][buyer])
            self.assertTrue(result[0].get("seller_replacement_player_id"))
            game._check_state()

    def test_swap_deal_exchanges_two_players_with_cash_and_idempotent_history(self):
        from economy.core import position_group
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            teams = list(game.s["rosters"])
            team_a, team_b = teams[0], teams[1]
            shared_group = next(group for group in ("MID", "DEF", "FWD", "GK")
                                if any(position_group(game.s["players"][pid]["position"]) == group
                                       for pid in game.s["rosters"][team_a])
                                and any(position_group(game.s["players"][pid]["position"]) == group
                                        for pid in game.s["rosters"][team_b]))
            player_a = next(pid for pid in game.s["rosters"][team_a]
                            if position_group(game.s["players"][pid]["position"]) == shared_group
                            and game.s["contracts"][pid].get("status") == "ACTIVE")
            player_b = next(pid for pid in game.s["rosters"][team_b]
                            if position_group(game.s["players"][pid]["position"]) == shared_group
                            and game.s["contracts"][pid].get("status") == "ACTIVE")
            beneficiary = next(team for team in teams if team not in (team_a, team_b))
            game.s.setdefault("sell_on_clauses", []).append({
                "id": "SOL-SWAP-TEST", "player_id": player_a, "owner_team": team_a,
                "beneficiary_team": beneficiary, "sell_on_pct": 5.0, "status": "ACTIVE"})
            a_before = float(game.s["club_finance"][team_a]["balance"])
            b_before = float(game.s["club_finance"][team_b]["balance"])
            expected_agent_a = round(float(game.s["players"][player_b].get("market_value", 0.0)) * 0.05, 3)
            expected_agent_b = round(float(game.s["players"][player_a].get("market_value", 0.0)) * 0.05, 3)
            expected_sell_on = round(float(game.s["players"][player_a].get("market_value", 0.0)) * 0.05, 3)
            # Existing unearned add-ons are tied to the paying club and must be
            # cancelled/refunded when the player leaves in a swap.
            game.s["club_finance"][team_a]["transfer_budget"] -= 1.25
            addon_clause = {"id": "ADD-SWAP-REFUND", "player_id": player_a,
                            "payer_team": team_a, "recipient_team": beneficiary,
                            "metric": "APPEARANCES", "threshold": 10, "amount": 1.25,
                            "reserved_amount": 1.25, "status": "ACTIVE"}
            game.s.setdefault("transfer_addon_clauses", []).append(addon_clause)
            a_budget_before = float(game.s["club_finance"][team_a]["transfer_budget"])
            rows = service.complete_swap(team_a, player_a, team_b, player_b, "2026-08-08",
                                         cash_adjustment=2.0)
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["swap"] and row["status"] == "Hoàn tất" for row in rows))
            self.assertIn(player_b, game.s["rosters"][team_a])
            self.assertNotIn(player_a, game.s["rosters"][team_a])
            self.assertIn(player_a, game.s["rosters"][team_b])
            self.assertNotIn(player_b, game.s["rosters"][team_b])
            self.assertEqual(game.s["contracts"][player_a]["team_id"], team_b)
            self.assertEqual(game.s["contracts"][player_b]["team_id"], team_a)
            self.assertAlmostEqual(game.s["club_finance"][team_a]["balance"],
                                   a_before - 2.0 - expected_agent_a - expected_sell_on, places=3)
            self.assertAlmostEqual(game.s["club_finance"][team_b]["balance"], b_before + 2.0 - expected_agent_b, places=3)
            self.assertEqual(game.s["sell_on_clauses"][-1]["status"], "PAID")
            self.assertEqual(addon_clause["status"], "CANCELLED")
            self.assertAlmostEqual(game.s["club_finance"][team_a]["transfer_budget"],
                                   a_budget_before + 1.25 - expected_agent_a - 2.0, places=3)
            history_count = len(game.s["transfer_history"])
            ledger_count = len(game.s["finance_ledger"])
            again = service.complete_swap(team_a, player_a, team_b, player_b, "2026-08-08",
                                          cash_adjustment=2.0)
            self.assertEqual(len(again), 2)
            self.assertEqual(len(game.s["transfer_history"]), history_count)
            self.assertEqual(len(game.s["finance_ledger"]), ledger_count)
            game._check_state()

    def test_loan_option_can_be_exercised_as_permanent_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            parent = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "small")
            borrower = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "medium")
            player_id = min(game.s["rosters"][parent], key=lambda pid: game.s["players"][pid].get("market_value", 0))
            agreement = service.complete_loan(
                borrower, player_id, loan_fee=1.0, wage_share_pct=75.0,
                end_date="2026-12-01", date_text="2026-08-08", option_to_buy=20.0)
            cash_before = game.s["club_finance"][borrower]["balance"]
            result = service.exercise_loan_option(borrower, player_id, "2026-08-09")
            self.assertTrue(result["loan_option_exercised"])
            self.assertEqual(result["fee"], 20.0)
            self.assertEqual(agreement["status"], "OPTION_EXERCISED")
            self.assertEqual(game.s["contracts"][player_id]["status"], "ACTIVE")
            self.assertEqual(game.s["contracts"][player_id]["team_id"], borrower)
            self.assertIn(player_id, game.s["rosters"][borrower])
            self.assertNotIn(player_id, game.s["rosters"][parent])
            self.assertLess(game.s["club_finance"][borrower]["balance"], cash_before)
            self.assertEqual(game.s["transfer_history"][-1]["status"], "Hoàn tất")
            game._check_state()

    def test_loan_splits_wages_and_returns_player_to_parent_club(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            parent = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "small")
            borrower = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "medium")
            player_id = min(game.s["rosters"][parent], key=lambda pid: game.s["players"][pid].get("market_value", 0))
            original_contract = copy.deepcopy(game.s["contracts"][player_id])
            original_wage = float(original_contract["wage_per_year"])
            initial_parent_roster = len(game.s["rosters"][parent])
            initial_borrower_roster = len(game.s["rosters"][borrower])
            agreement = service.complete_loan(
                borrower, player_id, loan_fee=1.25, wage_share_pct=70.0,
                end_date="2026-08-20", date_text="2026-08-08", option_to_buy=25.0)
            self.assertEqual(agreement["status"], "ACTIVE")
            self.assertEqual(game.s["contracts"][player_id]["status"], "ON_LOAN")
            self.assertEqual(game.s["contracts"][player_id]["team_id"], borrower)
            third_team = next(team for team in game.s["club_finance"] if team not in (parent, borrower))
            with self.assertRaises(ValueError):
                service.request_permission_to_talk(third_team, player_id, "2026-08-09")
            with self.assertRaises(ValueError):
                service.mark_for_sale(borrower, player_id)
            self.assertNotIn(player_id, game.s["rosters"][parent])
            self.assertIn(player_id, game.s["rosters"][borrower])
            self.assertEqual(len(game.s["rosters"][parent]), initial_parent_roster)
            self.assertEqual(len(game.s["rosters"][borrower]), initial_borrower_roster + 1)
            self.assertAlmostEqual(service._annual_wage_committed(borrower) -
                                   (sum(float(game.s["contracts"].get(pid, {}).get("wage_per_year", 0))
                                        for pid in game.s["rosters"][borrower] if pid != player_id)),
                                   original_wage * 0.70, places=3)
            self.assertAlmostEqual(service._annual_wage_committed(parent) -
                                   sum(float(game.s["contracts"].get(pid, {}).get("wage_per_year", 0))
                                       for pid in game.s["rosters"][parent]),
                                   original_wage * 0.30, places=3)

            game.economy_runtime._process_month("2026-08")
            parent_monthly = game.s["club_finance"][parent]["monthly_rollup"]["2026-08"]["wage"]
            borrower_monthly = game.s["club_finance"][borrower]["monthly_rollup"]["2026-08"]["wage"]
            self.assertAlmostEqual(parent_monthly, round(service._annual_wage_committed(parent) / 12.0, 3))
            self.assertAlmostEqual(borrower_monthly, round(service._annual_wage_committed(borrower) / 12.0, 3))

            # The runtime hook intentionally has no return contract; verify
            # the persisted loan state after the real daily lifecycle hook.
            game.economy_runtime.on_day_advanced(date="2026-08-20")
            self.assertEqual(agreement["status"], "RETURNED")
            self.assertEqual(game.s["contracts"][player_id]["team_id"], parent)
            self.assertEqual(game.s["contracts"][player_id]["wage_per_year"], original_contract["wage_per_year"])
            self.assertIn(player_id, game.s["rosters"][parent])
            self.assertNotIn(player_id, game.s["rosters"][borrower])
            game._check_state()

    def test_transfer_installment_schedule_validation_and_retry_without_overdraft(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            terms = service._validated_deal_terms({"installments": 3, "upfront_pct": 40,
                                                   "interval_days": 30, "sell_on_pct": 10,
                                                   "addons": [{"metric": "APPEARANCES", "threshold": 10,
                                                               "amount": 1.0, "valid_days": 365}]})
            self.assertEqual(terms["installments"], 3)
            self.assertEqual(terms["upfront_pct"], 40.0)
            self.assertEqual(terms["addons"][0]["metric"], "APPEARANCES")
            with self.assertRaises(ValueError):
                service._validated_deal_terms({"installments": 2, "upfront_pct": 10})
            with self.assertRaises(ValueError):
                service._validated_deal_terms({"installments": 7, "upfront_pct": 40})
            with self.assertRaises(ValueError):
                service._validated_deal_terms({"addons": [{"metric": "ASSISTS", "threshold": 5, "amount": 1}]})
            with self.assertRaises(ValueError):
                service._validated_deal_terms({"addons": [{"metric": "GOALS", "threshold": 1, "amount": 1}] * 4})

            buyer, seller = list(game.s["club_finance"])[:2]
            game.s["transfer_offers"] = [{"id": "TEST-OFFER", "fee": 12.345,
                "to_team": buyer, "from_team": seller, "player_id": "TEST-PLAYER",
                "deal_terms": terms}]
            schedule = service._build_fee_schedule(game.s["transfer_offers"][0], "2026-08-01")
            self.assertEqual([row["amount"] for row in schedule], [4.938, 3.704, 3.703])
            self.assertAlmostEqual(sum(row["amount"] for row in schedule), 12.345)
            self.assertEqual(schedule[1]["due_date"], "2026-08-31")
            self.assertEqual(schedule[2]["due_date"], "2026-09-30")

            # A due payment cannot push a club negative. It is postponed and
            # then posted exactly once after funds become available.
            payer, recipient = seller, buyer
            game.s["club_finance"][payer]["balance"] = 0.0
            payment = {"id": "SET-RETRY", "payer_team": payer, "recipient_team": recipient,
                       "amount": 2.5, "due_date": "2026-08-01", "kind": "TRANSFER_FEE",
                       "status": "PENDING", "attempts": 0}
            game.s.setdefault("transfer_settlements", []).append(payment)
            events = service.process_due_transfer_payments("2026-08-01")
            self.assertIn("transfer_payment_deferred", [row["type"] for row in events])
            self.assertEqual(game.s["club_finance"][payer]["balance"], 0.0)
            self.assertEqual(payment["due_date"], "2026-08-08")
            game.s["club_finance"][payer]["balance"] = 5.0
            before_recipient = game.s["club_finance"][recipient]["balance"]
            service.process_due_transfer_payments("2026-08-08")
            after_paid = game.s["club_finance"][payer]["balance"]
            self.assertEqual(after_paid, 2.5)
            self.assertEqual(game.s["club_finance"][recipient]["balance"], before_recipient + 2.5)
            ledger_count = len(game.s["finance_ledger"])
            service.process_due_transfer_payments("2026-08-08")
            self.assertEqual(len(game.s["finance_ledger"]), ledger_count)
            self.assertEqual(payment["status"], "PAID")
            recipient_budget_after = game.s["club_finance"][recipient]["transfer_budget"]
            # Simulate a crash after cash legs were saved but before the
            # settlement status flag was persisted.
            payment["status"] = "PENDING"
            service.process_due_transfer_payments("2026-08-08")
            self.assertEqual(game.s["club_finance"][recipient]["transfer_budget"],
                             recipient_budget_after)

    def test_loan_rejects_wage_budget_overrun_for_borrower(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=5156, enable_google_sheets=False)
            service = game.transfer_market
            parent = next(team for team, row in game.s["club_finance"].items()
                          if row["tier"] == "small")
            borrower = next(team for team, row in game.s["club_finance"].items()
                            if row["tier"] == "medium")
            player_id = min(game.s["rosters"][parent],
                            key=lambda pid: game.s["players"][pid].get("market_value", 0))
            annual_wage = float(game.s["contracts"][player_id]["wage_per_year"])
            committed = service._annual_wage_committed(borrower)
            game.s["club_finance"][borrower]["wage_budget"] = committed + annual_wage * 0.50 - 0.001
            with self.assertRaisesRegex(ValueError, "Quỹ lương CLB mượn"):
                service.complete_loan(borrower, player_id, loan_fee=0.0,
                                       wage_share_pct=50.0, end_date="2026-12-01",
                                       date_text="2026-08-08")
            self.assertNotIn(player_id, game.s["rosters"][borrower])

    def test_installment_transfer_and_sell_on_settle_on_next_sale(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            seller = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "small")
            first_buyer = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "big")
            second_buyer = next(team for team, row in game.s["club_finance"].items() if row["tier"] == "medium")
            # Select the cheapest player so the deterministic test stays within
            # the initial transfer budgets after paying the approach fee.
            player_id = min(game.s["rosters"][seller], key=service.asking_price)
            date1 = "2026-08-08"
            permission = service.request_permission_to_talk(first_buyer, player_id, date1)
            self.assertTrue(permission["accepted"])
            negotiation = permission["negotiation"]
            demand = negotiation["player_demand"]
            personal = service.submit_contract_offer(negotiation["id"], demand["wage"],
                                                      demand["years"], 0.0, date1)
            self.assertEqual(personal["status"], "AGREED")
            fee = service.asking_price(player_id)
            offer = service.submit_transfer_offer(
                first_buyer, player_id, fee, negotiation["id"], date1,
                {"installments": 3, "upfront_pct": 40, "interval_days": 30, "sell_on_pct": 10,
                 "addons": [{"metric": "APPEARANCES", "threshold": 1, "amount": 0.5, "valid_days": 365},
                            {"metric": "GOALS", "threshold": 1, "amount": 0.7, "valid_days": 365}]})
            self.assertEqual(offer["status"], "ACCEPTED")
            buyer_balance_before = game.s["club_finance"][first_buyer]["balance"]
            seller_balance_before = game.s["club_finance"][seller]["balance"]
            history = service.complete_transfer(offer["id"], date1)
            self.assertAlmostEqual(history["upfront_paid"], round(fee * 0.4, 3))
            self.assertEqual(history["installment_count"], 3)
            self.assertAlmostEqual(game.s["club_finance"][first_buyer]["balance"],
                                   buyer_balance_before - history["upfront_paid"] - history["agent_fee"])
            self.assertAlmostEqual(game.s["club_finance"][seller]["balance"],
                                   seller_balance_before + history["upfront_paid"])
            pending = [row for row in game.s["transfer_settlements"]
                       if row.get("offer_id") == offer["id"] and row.get("status") == "PENDING"]
            self.assertEqual(len(pending), 2)
            clause = next(row for row in game.s["sell_on_clauses"] if row["source_offer_id"] == offer["id"])
            self.assertEqual(clause["owner_team"], first_buyer)
            self.assertEqual(clause["beneficiary_team"], seller)
            addon_clauses = [row for row in game.s["transfer_addon_clauses"]
                             if row.get("source_offer_id") == offer["id"]]
            self.assertEqual(len(addon_clauses), 2)
            self.assertAlmostEqual(history["addons_reserved"], 1.2)

            # A single played match reaches both milestones. Replaying the same
            # match event must not post either contingent fee twice.
            milestone_match = {
                "id": "ADDON-MILESTONE-1", "played": True, "date": date1,
                "home": first_buyer, "away": seller,
                "minutes_h": {player_id: 90}, "minutes_a": {},
                "xi_h": [player_id], "xi_a": [],
                "player_stats": {"h": {}, "a": {}},
                "goals": [{"scorer_id": player_id, "team": first_buyer}],
            }
            addon_events = service.process_match_addons(milestone_match)
            self.assertEqual(sum(1 for row in addon_events if row["type"] == "transfer_addon_triggered"), 2)
            self.assertTrue(all(row["status"] == "PAID" for row in addon_clauses))
            addon_payments = [row for row in game.s["transfer_settlements"]
                              if row.get("kind") == "TRANSFER_ADDON"]
            self.assertEqual(len(addon_payments), 2)
            self.assertTrue(all(row["status"] == "PAID" for row in addon_payments))
            self.assertTrue(all(row["status"] == "PAID" for row in addon_clauses))
            self.assertTrue(all(row.get("paid_date") == date1 for row in addon_clauses))
            addon_ledger_count = len(game.s["finance_ledger"])
            service.process_match_addons(milestone_match)
            self.assertEqual(len(game.s["finance_ledger"]), addon_ledger_count)

            # A later sale triggers sell-on and cancels any unearned add-on
            # reserved by the former buying club, refunding that club at once.
            late_addon = {"id": "ADD-PLAYER-MOVE", "player_id": player_id,
                          "payer_team": first_buyer, "recipient_team": seller,
                          "metric": "APPEARANCES", "threshold": 20, "amount": 0.9,
                          "reserved_amount": 0.9, "status": "ACTIVE"}
            game.s["club_finance"][first_buyer]["transfer_budget"] -= 0.9
            game.s.setdefault("transfer_addon_clauses", []).append(late_addon)
            former_buyer_budget = float(game.s["club_finance"][first_buyer]["transfer_budget"])
            date2 = "2026-08-09"
            permission2 = service.request_permission_to_talk(second_buyer, player_id, date2)
            self.assertTrue(permission2["accepted"])
            negotiation2 = permission2["negotiation"]
            demand2 = negotiation2["player_demand"]
            personal2 = service.submit_contract_offer(negotiation2["id"], demand2["wage"],
                                                       demand2["years"], 0.0, date2)
            self.assertEqual(personal2["status"], "AGREED")
            fee2 = service.asking_price(player_id)
            offer2 = service.submit_transfer_offer(second_buyer, player_id, fee2,
                                                   negotiation2["id"], date2)
            self.assertEqual(offer2["status"], "ACCEPTED")
            second_history = service.complete_transfer(offer2["id"], date2)
            self.assertEqual(second_history["sell_on_triggered"], 1)
            self.assertEqual(clause["status"], "PAID")
            self.assertEqual(late_addon["status"], "CANCELLED")
            self.assertGreaterEqual(
                game.s["club_finance"][first_buyer]["transfer_budget"]
                - former_buyer_budget - second_history["upfront_paid"], 0.899)
            sell_on = next(row for row in game.s["transfer_settlements"]
                           if row.get("kind") == "SELL_ON" and row.get("offer_id") == offer2["id"])
            self.assertEqual(sell_on["status"], "PAID")
            self.assertAlmostEqual(sell_on["amount"], round(fee2 * 0.10, 3))
            self.assertIn(player_id, game.s["rosters"][second_buyer])
            game._check_state()


    def test_contract_termination_pays_half_remaining_wages_and_replaces_squad_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            team = next(iter(game.s["rosters"]))
            player_id = next(pid for pid in game.s["rosters"][team]
                             if game.s["contracts"][pid].get("status") == "ACTIVE")
            contract = game.s["contracts"][player_id]
            wage = float(contract["wage_per_year"])
            years = int(contract["years_remaining"])
            expected = round(wage * years * 0.5, 3)
            before = float(game.s["club_finance"][team]["balance"])
            row = service.terminate_contract(team, player_id, "2026-08-08")
            self.assertAlmostEqual(row["compensation"], expected)
            self.assertEqual(game.s["contracts"][player_id]["status"], "TERMINATED")
            self.assertIsNone(game.s["contracts"][player_id]["team_id"])
            self.assertEqual(game.s["players"][player_id]["club"], "")
            self.assertIn(player_id, game.s["free_agents"])
            self.assertNotIn(player_id, game.s["rosters"][team])
            self.assertEqual(len(game.s["rosters"][team]), 23)
            self.assertTrue(row.get("replacement_player_id"))
            self.assertAlmostEqual(game.s["club_finance"][team]["balance"], before - expected)
            txs = [tx for tx in game.s["finance_ledger"]
                   if tx.get("type") == "CONTRACT_TERMINATION" and tx.get("relatedId") == player_id]
            self.assertEqual(len(txs), 1)
            retry = service.terminate_contract(team, player_id, "2026-08-08")
            self.assertIs(retry, row)
            self.assertEqual(len([tx for tx in game.s["finance_ledger"]
                                  if tx.get("type") == "CONTRACT_TERMINATION" and tx.get("relatedId") == player_id]), 1)
            game._check_state()

    def test_contract_termination_rejects_insufficient_cash_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            team = next(iter(game.s["rosters"]))
            player_id = game.s["rosters"][team][0]
            finance = game.s["club_finance"][team]
            contract_before = copy.deepcopy(game.s["contracts"][player_id])
            roster_before = list(game.s["rosters"][team])
            ledger_before = len(game.s["finance_ledger"])
            finance["balance"] = 0.0
            with self.assertRaisesRegex(ValueError, "không đủ tiền bồi thường"):
                service.terminate_contract(team, player_id, "2026-08-08")
            self.assertEqual(game.s["contracts"][player_id], contract_before)
            self.assertEqual(game.s["rosters"][team], roster_before)
            self.assertEqual(len(game.s["finance_ledger"]), ledger_before)
            self.assertNotIn(player_id, game.s.get("free_agents", []))

    def test_untriggered_addon_expires_and_releases_reserved_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Game(data_dir=tmp, fresh=True, seed=20261102, enable_google_sheets=False)
            service = game.transfer_market
            payer = next(iter(game.s["club_finance"]))
            recipient = next(team for team in game.s["club_finance"] if team != payer)
            finance = game.s["club_finance"][payer]
            before = float(finance["transfer_budget"])
            clause = {"id": "ADD-EXPIRE", "player_id": "NO-APPEARANCE", "player_name": "Test",
                      "payer_team": payer, "recipient_team": recipient, "metric": "APPEARANCES",
                      "threshold": 10, "amount": 2.0, "reserved_amount": 2.0,
                      "progress": 0, "created_date": "2026-08-01", "expires_date": "2026-08-31",
                      "status": "ACTIVE"}
            game.s.setdefault("transfer_addon_clauses", []).append(clause)
            events = service.advance_to_date("2026-09-01")
            self.assertEqual(clause["status"], "EXPIRED")
            self.assertAlmostEqual(finance["transfer_budget"], before + 2.0)
            self.assertIn("transfer_addon_expired", [row["type"] for row in events])



if __name__ == "__main__":
    unittest.main()
