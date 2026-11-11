# Single Database Storage — Super League

`memory/super_league.db` is the only local simulator data file. It stores:

- The 552-player / 24-club source catalogue in `source_players`.
- Referees, managers, first names and surnames in `reference_data`.
- The active career and RNG state in `career_state`.
- Queryable player, club, match, finance, contract, transfer, academy, award, development and medical projections.
- Completed-season archives in `season_archives`.

No JSON or CSV sidecar is written to `memory/`. `--fresh` clears career and archive tables while preserving the source catalogue and human-name reference data. It does not create a persistent backup.

`config.json`, `config/` and `award_settings.json` remain outside `memory/` as application settings; they are not career data. Optional CSV/season export writes only to a user-selected folder outside the project. Google Sheets reads season data directly from SQLite; temporary screenshots and diagnostic logs are kept in the operating system temp directory.

SQLite is included in Python through `sqlite3`; no external database service is required.

Inspect the current database:

```bash
python src/main.py --db-status
```

Run the focused storage, archive and import/export tests:

```bash
python -m unittest test.test_database test.test_human_data test.test_season_archive test.test_hub_data_exchange -v
```
