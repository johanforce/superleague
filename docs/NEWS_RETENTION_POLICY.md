# News retention policy

The in-game news feed keeps only dated items from the current simulation week and the immediately preceding simulation week (Monday-Sunday, using the same reference date as the fixtures dashboard).

The policy is enforced at each boundary:

- On startup, match-news backfill only recreates articles for matches inside the retained two-week window. Older news is pruned and immediately persisted to both `career_state` and the SQLite `news_items` projection.
- Before each career save, the game prunes the canonical news list.
- At the SQLite persistence boundary, `news_items` is rebuilt on every save, so stale rows cannot accumulate even when the database API is called directly.

Completed match logs, season archives, player statistics, awards, finance, transfers, and other historical records are not deleted by this policy. The dynamic young-player news card continues to derive content from recent match logs and player statistics, not from stored news articles.
