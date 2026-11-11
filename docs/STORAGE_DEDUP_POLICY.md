# Storage de-duplication policy

## Canonical data sources

- Active season, active matches, current player-career detail, finance ledger and RNG: `career_state`.
- Completed season results, full saved match logs, award results, season development and finance summaries: `season_archives`.
- Detailed per-player per-season stats: `player_season_stats` (retained because it includes fields not fully represented by compact archive summaries).

## Writes after this change

- Season archives use incremental upsert. Identical JSON is not rewritten; omitted old seasons are not deleted by an ordinary save.
- The full-replacement archive method remains for explicit archive import/replace operations only.
- `matches`, `finance_transactions` and `awards` are no longer populated with copies of canonical data.
- `player_career` and `player_development` are not populated with new copies. A one-time compaction deletes only rows proven to match career/archive records exactly. Unmatched legacy rows remain.
- `player_season_stats`, player/club/contract projections, transfer history and medical records keep their existing behavior.

## Safety

- No season archive, match report, finance ledger entry, award, career snapshot or unique legacy row is removed by the compaction migration.
- Match reports continue reading current matches from `career_state` and past matches from `season_archives`.
- Reset remains the only normal path that intentionally clears all career/history data.
