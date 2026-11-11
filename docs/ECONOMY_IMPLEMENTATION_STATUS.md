# Economy implementation status

Last updated: 2026-11-03 (market valuation model v2)

This status file reflects the current source tree. A checked item means the behavior is implemented and covered by code-level tests; it does not mean the entire Football Manager-style specification or full simulator suite is complete.

## Implemented and checked

- `FinanceService.post` supports idempotency keys, returns the original transaction on exact retry, and rejects key reuse with a different club/type/amount.
- Economy schema v2 migration is idempotent and preserves legacy player records; wage normalization caps the migration wage-budget bump at 10%.
- Player market value model v2 prices every registered and academy player from OVR, position-specific attributes, age curve and age-gated potential premium. Existing careers are revalued once by `market_value_model_version`; contract reference values stay synchronized and values refresh at season close.
- Monthly wages, operating costs, sponsors, merchandise, interest, debt principal and owner support are integrated with monthly accounting and guarded multi-season catch-up using August–July season attribution.
- Owner emergency support is capped by club tier and per-season injection count/amount. It is recorded as `OWNER_INJECTION`, not operating revenue, and excluded from P&L/FFP profit.
- Debt restructuring creates a configurable 12–240 month principal schedule. Principal is repaid only when the configured cash floor is preserved; skipped installments are deferred and notified. Principal is reported separately from operating expenses.
- Matchday revenue, competition payouts, position/broadcast pools, season financial reports and monthly rollups are connected to the game lifecycle.
- Contract payroll ignores expired/terminated rows; legacy wage fallback only applies when a roster has no contract rows at all.
- Transfer workflow includes approach permission/fee, personal negotiation/counter, offers/counters, free agents, pre-contracts, manual listing and bilateral ledger.
- Advanced transfer fees include 1–6 installments, retrying due payments by seven days without overdraft, sell-on clauses up to 30%, and appearance/goal add-ons with reserve, milestone settlement and expiry/refund rules. Recipient budget credits on due installments are idempotent on retry.
- Loans preserve parent contract data, split wage burden, validate payroll on both clubs, return daily, remain `RETURN_PENDING` if squad rules would be broken, and support a fixed option-to-buy.
- Swap deals exchange two players atomically with new agreed contracts, optional cash balancing, agent fees, roster/payroll/FFP validation, sell-on settlement and idempotent history. Active unearned add-ons are cancelled and reserves returned when players leave.
- Contract termination pays configurable severance (default 50% of remaining guaranteed wages), records an idempotent expense, releases the player as a free agent and cancels stale negotiations/offers/pre-contracts. Loaned players and unaffordable severance are blocked; compatible academy replacements are staged where possible.
- 23–30 squad validation is enforced on transfers, loans, swaps and terminations. A compatible academy prospect can be promoted during an outgoing move to preserve the 23-player/position minimum where affordable.
- Daily calendar ticks run between fixtures and through off-season: offer/negotiation expiry, add-on expiry, loan return and AI window decisions. Monthly accounting remains on its existing slot-level hook.
- AI transfer director has separate BIG/MEDIUM/SMALL squad, OVR, budget-share and cash-reserve policies. It can buy, sell surplus players, execute fire-sales when cash drops below tier thresholds, loan high-potential prospects to clubs with a positional need, and match mutually beneficial player swaps. It is bounded by a 12-deal total window cap, one deal per club/window, 2 AI swaps/window and 4 AI loans/window. AI summer renewals remain capped at two per club/season.
- FFP calculates wage/revenue, amortisation and aggregate rolling three-season losses, flags zero revenue with costs, enforces transfer bans and records a configurable point deduction for the following domestic league season. The deduction is applied to Premier phase-one/phase-two and FC1/FC2 league standings, including read-only live-table projections, without altering raw match results or C1 group tables.
- Financial reports exclude initial capital, owner injections and debt principal from operating P&L; financial GUI surfaces owner support, debt terms and FFP sanctions.
- Club GUI tabs cover Finance, Transfers, Contracts, Stadium, Reports and Notifications. Transfer actions include approach/sign, list, loan, loan option and swap; contract actions include renewal/termination; purchase UI captures installment, sell-on and add-on terms.
- Financial report is attached to the season archive; full season history can be synced using the existing Apps Script integration, but remote credentials/config remain user-local.

## Market valuation model v2 (2026-11-03)

- Replaced the old steep exponential valuation curve, which saturated at the 200 M€ ceiling for a large share of the starting roster.
- Current ability uses a quadratic OVR curve; role-specific attribute averages adjust the result by up to ±8%.
- Potential premium is age-gated: up to 45% through age 20, 30% through 23, 12% through 25, and none from age 26 onward.
- Age multipliers are linearly interpolated from ages 16 to 40 to avoid a sharp value cliff on birthdays.
- Season-end refresh updates registered players, academy prospects and matching contract reference values. Newly generated youth are valued on creation.
- With clean-start seed `20261103`, the 552 first-team players range from 12.77 to 87.64 M€ (median 42.34 M€); none reaches the 250 M€ safety cap.

## Verification for market value v2 (2026-11-03)

- `python -m unittest test.test_economy -q`: **57/57 passed**.
- `python -m unittest test.test_database test.test_season_archive test.test_hub_data_exchange test.test_match_reports -q`: **27/27 passed**.
- Targeted simulator checks (full season/roster invariants, academy-age migration, save/resume, and fresh reset): **4/4 passed**.
- Python compile checks passed for changed runtime and test modules; the final ZIP passed `unzip -t` and contains 81 files with no bytecode cache or preloaded career database.

## Test evidence (increment 5.1)

- `PYTHONPATH=src python -m unittest test.test_economy -q`: **57 tests passed** (includes v2 market-value monotonicity, age/potential, position-attribute weighting, one-time migration, academy and season-end refresh). Includes AI buys/sales/fire-sale, AI development loans, AI swaps, one-deal-per-team/idempotency, loan payroll, return/option, swap atomicity, installments/sell-on/add-ons and settlement retry, contract termination/replacement, owner support caps, debt schedule/idempotency, FFP point deduction in saved and live tables, ledger idempotency and daily expiry.
- `PYTHONPATH=src python -m unittest test.test_season_archive test.test_google_sheets test.test_hub_data_exchange test.test_human_data test.test_award_history_sheets -q`: **49 tests passed**.
- **6 targeted simulator regressions passed:** save/resume, weekday/rest calendar, live league table, live phase-2 table, academy age/RNG migration, and a full-season 246-match run. The full-season roster assertion follows configured rules (23–30 players and minimum GK/DEF/MID/FWD coverage); transfers and contract expiry may legitimately change exact position counts.
- Python compile passed for `src/sl_game.py`, `src/sl_gui.py`, `src/season_archive.py`, `src/economy/*.py` and `scripts/soak_economy.py`.
- Five-season deterministic soak completed with seed `20261102`. All 24 clubs remained within 23–30 players and all balances remained nonnegative. AI activity by season 2026–2030 was:
  - market deals: 8 / 6 / 2 / 3 / 2;
  - permanent AI deals: 3 / 4 / 2 / 2 / 0;
  - development loans: 2 / 0 / 0 / 0 / 0;
  - AI swaps: 3 / 2 / 0 / 1 / 2;
  - renewals: 48 / 48 / 48 / 23 / 17.
- Soak cash range: minimum club cash by season 136.113 / 126.497 / 135.106 / 132.159 / 130.914 M€; maximum club cash 626.652 / 803.718 / 976.866 / 1,144.052 / 1,309.893 M€. Owner support was used in seasons 2027 and 2028 (8.300 and 9.162 M€ total). No FFP point deductions, transfer bans or debt restructures were triggered under this seed. Cash remains excessively accumulative; this soak demonstrates guardrails and market workflow, not long-run wealth equilibrium.
- Full `test.test_simulator` has not passed as a complete suite; prior long-running combined execution exceeded the 300-second environment cap. A 10–20-season soak has not been validated. No complete-suite claim is made.

## Remaining work (explicit backlog)

- **AI and market:** age/potential/personality profiles are still basic; no probabilistic 10–25% event model, deadline-day panic logic, AI target board/watchlist, or fully staged 2–4 day personal negotiation rounds. AI loans/swaps are implemented but are rule-based and still need long-run behavior tuning.
- **Squad and academy:** league-wide auto-repair at window close, configurable 1–3 player annual youth intake beyond fixed-size academy replenishment, and robust termination/release fallback when no compatible/affordable academy prospect exists.
- **Finance and FFP:** long-run wealth tuning, debt distress scenarios under multi-season simulation, and richer owner-injection/debt workout governance. The current five-season clean-start soak did not trigger debt restructuring or FFP sanctions, so those paths are unit-tested, not soak-observed.
- **Daily simulation:** daily fitness, training, morale and injury recovery; multi-round negotiation timeline; complete inbox accept/reject/counter actions.
- **Stadium and presentation:** real-club stadium mapping, curated rivalries, expansion/maintenance/naming rights, ticket-price control, full dashboard charts/PDF reports, responsive/i18n and persistent UI state.
- **Scale validation:** full simulator suite and 10–20-season soak.

## Clean Start usage

This document records implementation status for developers; it is not an installation or migration guide. For the current Clean Start package, use the root `README.md`. Extract the ZIP into a new folder. The project starts with the bundled player/name catalogues and creates a new SQLite career on first launch. Reset and `--fresh` are destructive and do not retain local backups.
