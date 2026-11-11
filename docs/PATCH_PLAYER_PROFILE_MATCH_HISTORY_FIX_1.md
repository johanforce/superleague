# Patch notes — Player Profile & Past Match History Fix 1

## Source baseline
This build starts from the exact `SuperLeague_GOALS_FIX_v2.zip` package. It does not include later BlackSam philosophy changes or any later project modifications. The packaged `memory/super_league.db` is unchanged.

## Changes

1. **Player numeric display**: player attributes and counting statistics are normalized/displayed as whole numbers. Average match rating remains at two decimal places. Expected goals in player competition statistics are rounded to an integer for display.
2. **Duplicate season rows**: the player career ledger now prefers the canonical season archive when the same season also exists in the career cache. The development timeline similarly keeps one development marker per season. When the player moved clubs during a season, the season is still shown once while the career club count retains all clubs found in the original source rows.
3. **Competition totals**: match-level player statistics are aggregated into one row per season, club, and competition instead of showing a repeated row for each fixture.
4. **Golden Boy accounting**: legacy `young_player` awards and canonical `golden_boy` entries are normalized to the same award ID, so the player-profile counter and award history recognize them consistently.
5. **Past-match navigation**: clicks/double-clicks on completed fixtures, cup cards, C1 cards, and calendar matches open the saved read-only match report. The report shows persisted scorers, red cards, and other saved match events. Upcoming eligible fixtures keep their existing simulation workflow.

## Validation
- Python compilation passed for the changed modules and tests.
- Player profile + match report + season archive tests: 24 passed.
- Database storage + human-name tests: 8 passed.
- Simulator award-specific tests: 6 passed.
- The V2 database passed SQLite `PRAGMA quick_check`.
- A read-only smoke test built profiles for all 778 players from the original V2 SQLite database; no duplicate career/development seasons remained. All 8 archived Golden Boy winners (2026–2033) appeared exactly once in their player award histories.
- The packaged database is copied byte-for-byte from the original V2 ZIP (SHA-256: `10b91c7234e1b69ac4c804ab025fc9ba5343f8616934ab76ec814faa04f44cac`).
- Full unittest discovery reached the slow `test_hub_data_exchange` ZIP-export test and timed out in the execution environment; that test is not reported as passed.
