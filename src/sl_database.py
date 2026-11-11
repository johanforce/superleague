# -*- coding: utf-8 -*-
"""SQLite persistence layer for Super League.

SQLite is the sole local data store. Static source catalogues, human reference
lists, active career state and completed-season archives all live in this file.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sl_match import news as match_news

SCHEMA_VERSION = 5
PROJECTION_COMPACTION_VERSION = 1


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _canonical_json(value: Any) -> str:
    """Stable comparison form used only when detecting exact projection copies."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True, default=str)


class SuperLeagueDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.schema_version_before = self._read_schema_version()
        # Single-file storage mode: never create a .corrupt_* sidecar or silently
        # reset damaged data. Report the database error so the user can decide.
        self._initialize()
        self.projection_rebuild_required = self.schema_version_before != SCHEMA_VERSION

    def _read_schema_version(self):
        if not self.path.exists(): return None
        try:
            conn = sqlite3.connect(str(self.path), timeout=5)
            try:
                row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
                return int(row[0]) if row else None
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return None

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _session(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self):
        with self._session() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS career_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    save_version INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    rng_state_json TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS source_players (
                    player_id TEXT PRIMARY KEY, team TEXT, player_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS player_snapshots (
                    season INTEGER NOT NULL, player_id TEXT NOT NULL, source_file TEXT NOT NULL,
                    player_json TEXT NOT NULL, PRIMARY KEY(season, player_id)
                );
                CREATE TABLE IF NOT EXISTS reference_data (
                    data_key TEXT PRIMARY KEY, data_json TEXT NOT NULL, source_file TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_source_players_team ON source_players(team);
                CREATE TABLE IF NOT EXISTS players (
                    player_id TEXT PRIMARY KEY, team TEXT, status TEXT NOT NULL,
                    player_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_players_team ON players(team);
                CREATE TABLE IF NOT EXISTS player_metadata (
                    player_id TEXT PRIMARY KEY, age REAL, potential REAL, peak_ovr REAL,
                    status TEXT, metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contracts (
                    contract_id TEXT PRIMARY KEY, player_id TEXT, club TEXT,
                    expiry_season INTEGER, contract_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_contracts_player ON contracts(player_id);
                CREATE TABLE IF NOT EXISTS club_finances (
                    club_name TEXT PRIMARY KEY, cash_m_eur REAL, debt_m_eur REAL,
                    finance_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clubs (
                    club_name TEXT PRIMARY KEY, club_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS matches (
                    match_id TEXT PRIMARY KEY, season INTEGER, status TEXT,
                    competition TEXT, match_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_matches_season ON matches(season);
                CREATE TABLE IF NOT EXISTS season_archives (
                    season INTEGER PRIMARY KEY, archive_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS player_season_stats (
                    season INTEGER NOT NULL, player_id TEXT NOT NULL, club TEXT,
                    stats_json TEXT NOT NULL, PRIMARY KEY(season, player_id)
                );
                CREATE TABLE IF NOT EXISTS player_career (
                    player_id TEXT NOT NULL, season INTEGER NOT NULL, club TEXT NOT NULL,
                    career_json TEXT NOT NULL, PRIMARY KEY(player_id, season, club)
                );
                CREATE TABLE IF NOT EXISTS player_development (
                    player_id TEXT NOT NULL, season INTEGER NOT NULL, development_json TEXT NOT NULL,
                    PRIMARY KEY(player_id, season)
                );
                CREATE TABLE IF NOT EXISTS medical_records (
                    record_id TEXT PRIMARY KEY, player_id TEXT, status TEXT, medical_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS academy_players (
                    team TEXT NOT NULL, player_id TEXT NOT NULL, player_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL, PRIMARY KEY(team, player_id)
                );
                CREATE TABLE IF NOT EXISTS news_items (
                    news_id TEXT PRIMARY KEY, season INTEGER, news_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS finance_transactions (
                    transaction_id TEXT PRIMARY KEY, season INTEGER, club TEXT,
                    transaction_date TEXT, transaction_type TEXT, amount_m_eur REAL,
                    transaction_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_finance_club_season ON finance_transactions(club, season);
                CREATE TABLE IF NOT EXISTS transfer_history (
                    transfer_id TEXT PRIMARY KEY, season INTEGER,
                    player_id TEXT, from_club TEXT, to_club TEXT, transfer_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS awards (
                    award_id TEXT PRIMARY KEY, season INTEGER,
                    award_type TEXT, recipient TEXT, team TEXT, award_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_awards_season ON awards(season);
            """)
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))

    def has_career(self) -> bool:
        with self._session() as db:
            row = db.execute("SELECT 1 FROM career_state WHERE id=1").fetchone()
            return row is not None

    def load_career(self):
        with self._session() as db:
            row = db.execute("SELECT save_version,state_json,rng_state_json FROM career_state WHERE id=1").fetchone()
            if not row:
                return None
            return {
                "version": int(row["save_version"]),
                "state": json.loads(row["state_json"]),
                "rng_state": json.loads(row["rng_state_json"]) if row["rng_state_json"] else None,
            }

    def set_source_players(self, players: list[dict], replace: bool = True) -> int:
        """Store the immutable 24-club player catalogue inside this database."""
        rows = []
        for index, player in enumerate(players or []):
            if not isinstance(player, dict):
                continue
            player_id = str(player.get("player_id", player.get("id", index)))
            team = str(player.get("team", player.get("club", "")))
            rows.append((player_id, team, _json(player)))
        with self._session() as db:
            if replace:
                db.execute("DELETE FROM source_players")
            if rows:
                db.executemany("INSERT OR REPLACE INTO source_players VALUES(?,?,?)", rows)
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('source_catalog_count',?)",
                       (str(len(rows)),))
        return len(rows)

    def set_reference_data(self, data_key: str, value: dict | list, source_file: str = "embedded") -> None:
        """Store static reference datasets (names, officials, managers) in SQLite."""
        with self._session() as db:
            db.execute("INSERT OR REPLACE INTO reference_data VALUES(?,?,?)",
                       (str(data_key), _json(value), str(source_file)))

    def load_reference_data(self, data_key: str) -> Any | None:
        with self._session() as db:
            row = db.execute("SELECT data_json FROM reference_data WHERE data_key=?",
                             (str(data_key),)).fetchone()
        return json.loads(row[0]) if row else None

    def save_season_archive(self, record: dict) -> None:
        """Upsert a compact completed-season archive into the single database."""
        if not isinstance(record, dict):
            raise ValueError("Season archive phải là object.")
        try:
            season = int(record.get("season"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Season archive thiếu season hợp lệ.") from exc
        with self._session() as db:
            db.execute("INSERT OR REPLACE INTO season_archives(season,archive_json) VALUES(?,?)",
                       (season, _json(record)))

    def replace_season_archives(self, records: list[dict]) -> int:
        """Replace all archived seasons in one transaction; career state is untouched."""
        prepared = []
        seen = set()
        for record in records or []:
            if not isinstance(record, dict):
                continue
            try:
                season = int(record.get("season"))
            except (TypeError, ValueError):
                continue
            if season in seen:
                raise ValueError(f"Trùng archive mùa {season}.")
            seen.add(season)
            prepared.append((season, _json(record)))
        with self._session() as db:
            db.execute("DELETE FROM season_archives")
            if prepared:
                db.executemany("INSERT INTO season_archives(season,archive_json) VALUES(?,?)", prepared)
        return len(prepared)

    @staticmethod
    def _prepare_season_archives(records: list[dict]) -> list[tuple[int, str]]:
        prepared = []
        seen = set()
        for record in records or []:
            if not isinstance(record, dict):
                continue
            try:
                season = int(record.get("season"))
            except (TypeError, ValueError):
                continue
            if season in seen:
                raise ValueError(f"Trùng archive mùa {season}.")
            seen.add(season)
            prepared.append((season, _json(record)))
        return prepared

    @classmethod
    def _upsert_season_archives(cls, db, records: list[dict]) -> int:
        """Insert new archives and update only seasons whose JSON actually changed."""
        prepared = cls._prepare_season_archives(records)
        if prepared:
            db.executemany(
                """INSERT INTO season_archives(season,archive_json) VALUES(?,?)
                   ON CONFLICT(season) DO UPDATE SET archive_json=excluded.archive_json
                   WHERE season_archives.archive_json <> excluded.archive_json""",
                prepared,
            )
        return len(prepared)

    def upsert_season_archives(self, records: list[dict]) -> int:
        """Incrementally save a set of season archives without deleting older seasons."""
        with self._session() as db:
            return self._upsert_season_archives(db, records)

    def _compact_redundant_projections(self, db, state: dict, archives: list[dict], season: int) -> dict:
        """Remove only projection rows proven to be exact copies of canonical data.

        Career state and season_archives are authoritative. Several older tables
        duplicate them for ad-hoc SQL access; no in-project reader depends on those
        copies. Rows that cannot be matched exactly are deliberately retained as
        supplemental legacy data rather than guessed away.
        """
        marker = db.execute(
            "SELECT value FROM metadata WHERE key='projection_compaction_version'"
        ).fetchone()
        if marker and str(marker[0]) == str(PROJECTION_COMPACTION_VERSION):
            return {"skipped": True}

        archive_rows = [row for row in (archives or []) if isinstance(row, dict)]

        # Match table: active matches are in career_state; completed seasons are in
        # season_archives. Both copies can be recreated from those canonical sources.
        match_sources = set()
        for archive in archive_rows:
            try:
                year = int(archive.get("season", season) or season)
            except (TypeError, ValueError):
                continue
            for match in archive.get("matches", []) or []:
                if not isinstance(match, dict):
                    continue
                mid = str(match.get("id", f"{match.get('date','')}-{match.get('home','')}-{match.get('away','')}"))
                match_sources.add((f"{year}:{mid}", _canonical_json(match)))
        for match in state.get("matches", []) or []:
            if not isinstance(match, dict):
                continue
            mid = str(match.get("id", f"{match.get('date','')}-{match.get('home','')}-{match.get('away','')}"))
            match_sources.add((f"{season}:{mid}", _canonical_json(match)))
        delete_match_ids = []
        for row in db.execute("SELECT match_id,match_json FROM matches").fetchall():
            try:
                payload = _canonical_json(json.loads(row["match_json"]))
            except (TypeError, ValueError):
                continue
            if (str(row["match_id"]), payload) in match_sources:
                delete_match_ids.append((row["match_id"],))
        if delete_match_ids:
            db.executemany("DELETE FROM matches WHERE match_id=?", delete_match_ids)

        # Finance transactions: exact mirror of career_state.finance_ledger.
        finance_sources = {}
        for index, tx in enumerate(state.get("finance_ledger", []) or []):
            if not isinstance(tx, dict):
                continue
            txid = str(tx.get("id", tx.get("idempotencyKey", f"{season}-{index}")))
            finance_sources[txid] = _canonical_json(tx)
        delete_finance_ids = []
        for row in db.execute("SELECT transaction_id,transaction_json FROM finance_transactions").fetchall():
            try:
                payload = _canonical_json(json.loads(row["transaction_json"]))
            except (TypeError, ValueError):
                continue
            if finance_sources.get(str(row["transaction_id"])) == payload:
                delete_finance_ids.append((row["transaction_id"],))
        if delete_finance_ids:
            db.executemany("DELETE FROM finance_transactions WHERE transaction_id=?", delete_finance_ids)

        # Player career rows: remove only exact rows represented by state/archive.
        career_sources = {}
        for pid, rows in (state.get("player_career") or {}).items():
            for item in rows or []:
                if not isinstance(item, dict):
                    continue
                try:
                    year = int(item.get("season", season) or season)
                except (TypeError, ValueError):
                    year = season
                key = (str(pid), year, str(item.get("club", "")))
                career_sources[key] = _canonical_json(item)
        for archive in archive_rows:
            try:
                year = int(archive.get("season", season) or season)
            except (TypeError, ValueError):
                continue
            for item in archive.get("career_player_stats", []) or []:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("player_id", ""))
                if pid:
                    career_sources[(pid, year, str(item.get("team", "")))] = _canonical_json(item)
        delete_career_keys = []
        for row in db.execute("SELECT player_id,season,club,career_json FROM player_career").fetchall():
            key = (str(row["player_id"]), int(row["season"]), str(row["club"]))
            try:
                payload = _canonical_json(json.loads(row["career_json"]))
            except (TypeError, ValueError):
                continue
            if career_sources.get(key) == payload:
                delete_career_keys.append(key)
        if delete_career_keys:
            db.executemany(
                "DELETE FROM player_career WHERE player_id=? AND season=? AND club=?",
                delete_career_keys,
            )

        # Development rows: retain any legacy row not found in the archive/current
        # development source. This matters for older saves with incomplete archives.
        development_sources = {}
        for archive in archive_rows:
            try:
                year = int(archive.get("season", season) or season)
            except (TypeError, ValueError):
                continue
            for item in archive.get("development", []) or []:
                if isinstance(item, dict) and item.get("player_id"):
                    development_sources[(str(item["player_id"]), year)] = _canonical_json(item)
        for item in state.get("last_development", []) or []:
            if isinstance(item, dict) and item.get("player_id"):
                development_sources[(str(item["player_id"]), season)] = _canonical_json(item)
        delete_development_keys = []
        for row in db.execute(
                "SELECT player_id,season,development_json FROM player_development").fetchall():
            key = (str(row["player_id"]), int(row["season"]))
            try:
                payload = _canonical_json(json.loads(row["development_json"]))
            except (TypeError, ValueError):
                continue
            if development_sources.get(key) == payload:
                delete_development_keys.append(key)
        if delete_development_keys:
            db.executemany(
                "DELETE FROM player_development WHERE player_id=? AND season=?",
                delete_development_keys,
            )

        # Awards are a fully reproducible index of season_archives.
        award_sources = set()
        for archive in archive_rows:
            try:
                year = int(archive.get("season", season) or season)
            except (TypeError, ValueError):
                continue
            for award_type in ("season_awards", "competition_awards", "individual_awards",
                               "team_honors", "team_awards"):
                value = archive.get(award_type)
                if isinstance(value, dict):
                    entries = list(value.items())
                elif isinstance(value, list):
                    entries = [(str(item.get("player", item.get("team", item.get("name", index)))), item)
                               for index, item in enumerate(value) if isinstance(item, dict)]
                else:
                    entries = []
                for recipient, award in entries:
                    team = str(award.get("team", award.get("club", ""))) if isinstance(award, dict) else ""
                    award_sources.add((year, award_type, str(recipient), team, _canonical_json(award)))
        delete_award_ids = []
        for row in db.execute(
                "SELECT award_id,season,award_type,recipient,team,award_json FROM awards").fetchall():
            try:
                payload = _canonical_json(json.loads(row["award_json"]))
            except (TypeError, ValueError):
                continue
            key = (int(row["season"] or 0), str(row["award_type"]), str(row["recipient"]),
                   str(row["team"]), payload)
            if key in award_sources:
                delete_award_ids.append((row["award_id"],))
        if delete_award_ids:
            db.executemany("DELETE FROM awards WHERE award_id=?", delete_award_ids)

        # player_season_stats is intentionally not compacted: it retains detailed
        # season-level fields (morale, injuries, GK stats) absent from archive summaries.
        db.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('projection_compaction_version',?)",
            (str(PROJECTION_COMPACTION_VERSION),),
        )
        return {
            "matches": len(delete_match_ids),
            "finance_transactions": len(delete_finance_ids),
            "player_career": len(delete_career_keys),
            "player_development": len(delete_development_keys),
            "awards": len(delete_award_ids),
            "preserved_player_season_stats": True,
        }

    def import_player_snapshots(self, directory: str | Path):
        import csv
        directory = Path(directory)
        if not directory.is_dir(): return 0
        imported = 0
        with self._session() as db:
            for path in sorted(directory.glob("players_*.csv")):
                try: season = int(path.stem.split("_")[-1])
                except (TypeError, ValueError): continue
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    for index, row in enumerate(csv.DictReader(handle)):
                        pid = str(row.get("player_id") or row.get("id") or row.get("ID") or index)
                        db.execute("INSERT OR REPLACE INTO player_snapshots VALUES(?,?,?,?)",
                                   (season, pid, path.name, _json(row)))
                        imported += 1
        return imported

    def import_reference_data(self, data_key: str, path: str | Path):
        path = Path(path)
        if not path.is_file(): return False
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError): return False
        with self._session() as db:
            db.execute("INSERT OR REPLACE INTO reference_data VALUES(?,?,?)",
                       (str(data_key), _json(value), path.name))
        return True

    def load_source_players(self) -> list[dict]:
        with self._session() as db:
            rows = db.execute("SELECT player_json FROM source_players ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]

    def load_season_archives(self) -> list[dict]:
        with self._session() as db:
            rows = db.execute("SELECT archive_json FROM season_archives ORDER BY season").fetchall()
        return [json.loads(row[0]) for row in rows]

    def _replace_news_projection(self, db, state: dict, default_season: int | None = None) -> int:
        """Rebuild the short-lived news projection from the retained two-week feed."""
        season = int(default_season if default_season is not None else state.get("season", 0) or 0)
        db.execute("DELETE FROM news_items")
        news_values = []
        for collection in (state.get("news"), state.get("last_news")):
            if isinstance(collection, dict):
                news_values.extend(collection.values())
            elif isinstance(collection, list):
                news_values.extend(collection)
        news_reference_date = match_news.reference_date_from_state(state)
        news_values = match_news.retain_recent_news(news_values, news_reference_date)
        for index, row in enumerate(news_values):
            item_season = int(row.get("season", season) or season)
            news_id = str(row.get("id") or
                          f"{item_season}:{index}:{row.get('date','')}:{row.get('kind','news')}:{row.get('match_id','')}")
            db.execute("INSERT OR REPLACE INTO news_items VALUES(?,?,?)",
                       (news_id, item_season, _json(row)))
        return len(news_values)

    def sync_news_window(self, state: dict) -> int:
        """Immediately persist startup news pruning without rewriting other career data."""
        if not isinstance(state, dict):
            return 0
        reference_date = match_news.reference_date_from_state(state)
        retained = match_news.retain_recent_news(state.get("news", []), reference_date)
        state["news"] = retained
        with self._session() as db:
            row = db.execute("SELECT state_json FROM career_state WHERE id=1").fetchone()
            if not row:
                return 0
            saved_state = json.loads(row["state_json"])
            if not isinstance(saved_state, dict):
                return 0
            saved_state["news"] = retained
            db.execute("UPDATE career_state SET state_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=1",
                       (_json(saved_state),))
            return self._replace_news_projection(db, state)

    def save_career(self, blob: dict, source_players=None, season_archives=None):
        state = blob.get("state") or {}
        # Apply retention at the persistence boundary too, so older news cannot
        # survive when a caller writes through the database API directly.
        if "news" in state:
            news_reference_date = match_news.reference_date_from_state(state)
            state["news"] = match_news.retain_recent_news(state.get("news"), news_reference_date)
        season = int(state.get("season", 0) or 0)
        archive_records = (season_archives if season_archives is not None
                           else state.get("history", [])) or []
        with self._session() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO career_state(id,save_version,state_json,rng_state_json,updated_at)
                VALUES(1,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET save_version=excluded.save_version,
                state_json=excluded.state_json,rng_state_json=excluded.rng_state_json,
                updated_at=CURRENT_TIMESTAMP""",
                (int(blob.get("version", 1)), _json(state), _json(blob.get("rng_state"))))

            # Archives are the canonical long-term match-history store. Upsert only
            # changed seasons; never delete and rewrite every completed season on
            # each ordinary career save.
            self._upsert_season_archives(db, archive_records)

            # One-time migration of old query projections that duplicated the
            # career snapshot or completed-season archives. Non-matching legacy
            # rows are retained rather than deleted on assumption.
            self._compact_redundant_projections(db, state, archive_records, season)

            # Rebuild queryable projections from the complete canonical snapshot.
            db.execute("DELETE FROM players")
            player_rows = []
            for pid, player in (state.get("players") or {}).items():
                team = (state.get("player_team") or {}).get(str(pid))
                if not team:
                    for club, ids in (state.get("rosters") or {}).items():
                        if str(pid) in {str(x) for x in (ids or [])}:
                            team = club; break
                player_rows.append((str(pid), team, "active", _json(player)))
            for collection, status in ((state.get("generated_players") or [], "generated"),
                                       (state.get("retired_players") or [], "retired"),
                                       (state.get("academy_alumni") or [], "alumni")):
                for player in collection:
                    pid = str(player.get("player_id", player.get("id", "")))
                    if pid:
                        player_rows.append((pid, player.get("team"), status, _json(player)))
            if player_rows:
                db.executemany("INSERT OR REPLACE INTO players VALUES(?,?,?,?)", player_rows)

            db.execute("DELETE FROM player_metadata")
            meta_rows = []
            for pid, meta in (state.get("player_meta") or {}).items():
                meta_rows.append((str(pid), meta.get("age"), meta.get("potential"), meta.get("peak_ovr"),
                                  str(meta.get("status", "active")), _json(meta)))
            if meta_rows: db.executemany("INSERT OR REPLACE INTO player_metadata VALUES(?,?,?,?,?,?)", meta_rows)

            db.execute("DELETE FROM contracts")
            contract_rows = []
            contracts = state.get("contracts") or {}
            if isinstance(contracts, dict):
                for pid, contract in contracts.items():
                    if isinstance(contract, dict):
                        cid = str(contract.get("contract_id", contract.get("id", pid)))
                        contract_rows.append((cid, str(pid), str(contract.get("club", contract.get("team_id", contract.get("team", "")))),
                                              contract.get("expiry_season", contract.get("end_season")), _json(contract)))
            elif isinstance(contracts, list):
                for index, contract in enumerate(contracts):
                    if isinstance(contract, dict):
                        pid = str(contract.get("player_id", "")); cid = str(contract.get("contract_id", contract.get("id", index)))
                        contract_rows.append((cid, pid, str(contract.get("club", contract.get("team_id", contract.get("team", "")))),
                                              contract.get("expiry_season", contract.get("end_season")), _json(contract)))
            if contract_rows: db.executemany("INSERT OR REPLACE INTO contracts VALUES(?,?,?,?,?)", contract_rows)

            db.execute("DELETE FROM club_finances")
            finance_map = state.get("club_finance") or state.get("club_finances") or state.get("club_economy") or {}
            finance_rows = []
            if isinstance(finance_map, dict):
                for club, finance in finance_map.items():
                    if isinstance(finance, dict):
                        cash = finance.get("cash", finance.get("balance", finance.get("closing_cash_m_eur")))
                        debt = finance.get("debt", finance.get("closing_debt_m_eur"))
                        finance_rows.append((str(club), cash, debt, _json(finance)))
            if finance_rows: db.executemany("INSERT OR REPLACE INTO club_finances VALUES(?,?,?,?)", finance_rows)

            db.execute("DELETE FROM clubs")
            club_names = set((state.get("rosters") or {}).keys())
            for key in ("club_finance", "club_finances", "club_economy", "team_tiers", "club_stats"):
                value = state.get(key)
                if isinstance(value, dict): club_names.update(value.keys())
            club_rows = []
            for club in sorted(club_names):
                payload = {k: (v.get(club) if isinstance(v, dict) else None)
                           for k, v in state.items()
                           if k in {"rosters", "club_finance", "club_finances", "club_economy", "team_tiers", "club_stats", "contracts"}}
                club_rows.append((str(club), _json(payload)))
            if club_rows: db.executemany("INSERT OR REPLACE INTO clubs VALUES(?,?)", club_rows)

            transfer_rows = state.get("transfer_history", []) or []
            for index, tx in enumerate(transfer_rows):
                transfer_id = str(tx.get("id", tx.get("transfer_id", f"{season}:{index}")))
                db.execute("INSERT OR REPLACE INTO transfer_history VALUES(?,?,?,?,?,?)",
                           (transfer_id, int(tx.get("season", season) or season), str(tx.get("player_id", "")),
                            str(tx.get("from_club", tx.get("from", ""))),
                            str(tx.get("to_club", tx.get("to", ""))), _json(tx)))

            # Detailed season stats are intentionally retained as a separate
            # historical projection: they include fields not present in compact
            # season archive summaries (e.g. morale and injury totals).
            db.execute("DELETE FROM player_season_stats WHERE season=?", (season,))
            for pid, stats in (state.get("season_stats") or {}).items():
                club = next((team for team, ids in (state.get("rosters") or {}).items()
                             if str(pid) in {str(x) for x in (ids or [])}), "")
                db.execute("INSERT OR REPLACE INTO player_season_stats VALUES(?,?,?,?)",
                           (season, str(pid), club, _json(stats)))

            for row in state.get("medical_records", []) or []:
                record_id = str(row.get("id", f"{row.get('player_id','')}:{row.get('start_date','')}"))
                db.execute("INSERT OR REPLACE INTO medical_records VALUES(?,?,?,?)",
                           (record_id, str(row.get("player_id", "")), str(row.get("status", "")), _json(row)))
            db.execute("DELETE FROM academy_players")
            for team, entries in (state.get("academy") or {}).items():
                for entry in entries or []:
                    player = entry.get("player", {}) or {}; meta = entry.get("meta", {}) or {}
                    pid = str(player.get("player_id", player.get("id", "")))
                    if pid:
                        db.execute("INSERT OR REPLACE INTO academy_players VALUES(?,?,?,?)",
                                   (str(team), pid, _json(player), _json(meta)))
            # news_items is a short-lived projection, never a season archive.
            self._replace_news_projection(db, state, default_season=season)

            if source_players is not None:
                db.executemany("INSERT OR REPLACE INTO source_players VALUES(?,?,?)",
                               [(str(p.get("player_id", p.get("id", i))), str(p.get("team", p.get("club", ""))), _json(p))
                                for i, p in enumerate(source_players)])
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('last_saved_season',?)", (str(season),))
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('storage_mode','sqlite_primary')")
            db.commit()
        self.projection_rebuild_required = False

    def reset_career(self):
        """Clear all career projections for an explicit --fresh/new-career action."""
        with self._session() as db:
            for table in ("career_state", "players", "player_metadata", "contracts", "club_finances",
                          "clubs", "matches", "season_archives", "finance_transactions",
                          "transfer_history", "awards", "player_season_stats", "player_career",
                          "player_development", "medical_records", "academy_players", "news_items",
                          "player_snapshots"):
                db.execute(f'DELETE FROM "{table}"')
            db.execute("DELETE FROM metadata WHERE key IN ('last_saved_season','storage_mode')")

    def status(self) -> dict:
        with self._session() as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            counts = {name: int(db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in tables}
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            mode = db.execute("SELECT value FROM metadata WHERE key='storage_mode'").fetchone()
        return {"path": str(self.path), "integrity": integrity,
                "schema_version": int(version[0]) if version else None,
                "storage_mode": mode[0] if mode else "not_initialized",
                "has_career": self.has_career(), "tables": counts}
