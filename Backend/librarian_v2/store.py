"""Librarian v2 store — the only module that talks to the SQLite database.

Design rules (docs/librarian-v2/SPEC.md §3):
- Series progress counters are materialised and maintained inside the same
  transaction as the change that affects them (recount of ONE series is a
  small indexed query; the v1 sin was scanning the whole ledger per render).
- A file's link to a series/episode is a plain column update; unlinking is
  setting it back to NULL. Nothing is ever deleted here except cache rows.
- All timestamps are UTC epoch seconds. `now` is passed in by callers so
  behaviour is deterministic under test.
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def open_store(path):
    """Open (creating if needed) the v2 store at `path` and apply the schema."""
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # explicit transactions via Store.tx()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Store(conn)


class Store:
    def __init__(self, conn):
        self.conn = conn

    # -- plumbing ---------------------------------------------------------
    @contextmanager
    def tx(self, immediate=False):
        self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def _exec(self, sql, args=()):
        return self.conn.execute(sql, args)

    # -- channels -----------------------------------------------------------
    def upsert_channel(self, telegram_id, title, kind="auth",
                       trust_tier="normal", default_rule="ask", now=0):
        with self.tx():
            row = self._one("SELECT id FROM source_channel WHERE telegram_id=?",
                            (telegram_id,))
            if row:
                self._exec("UPDATE source_channel SET title=?, kind=?,"
                           " trust_tier=?, default_rule=? WHERE id=?",
                           (title, kind, trust_tier, default_rule, row["id"]))
                return row["id"]
            cur = self._exec(
                "INSERT INTO source_channel (telegram_id, title, kind,"
                " trust_tier, default_rule, created_at) VALUES (?,?,?,?,?,?)",
                (telegram_id, title, kind, trust_tier, default_rule, now))
            return cur.lastrowid

    def list_channels(self):
        return self._all("SELECT * FROM source_channel ORDER BY id")

    def get_channel_by_telegram_id(self, telegram_id):
        return self._one("SELECT * FROM source_channel WHERE telegram_id=?",
                         (telegram_id,))

    # -- series -------------------------------------------------------------
    def upsert_series(self, canonical_title, kind="tv", year=None,
                      ordering="aired", provider=None, provider_id=None,
                      imdb_id=None, status="identified", now=0):
        """Find by (provider, provider_id) when given, else by title+year."""
        with self.tx():
            row = None
            if provider and provider_id:
                row = self._one("SELECT id FROM series WHERE provider=?"
                                " AND provider_id=?", (provider, provider_id))
            if row is None:
                row = self._one("SELECT id FROM series WHERE canonical_title=?"
                                " AND year IS ?", (canonical_title, year))
            if row:
                self._exec(
                    "UPDATE series SET canonical_title=?, kind=?, year=?,"
                    " ordering=?, provider=?, provider_id=?, imdb_id=?,"
                    " status=?, updated_at=? WHERE id=?",
                    (canonical_title, kind, year, ordering, provider,
                     provider_id, imdb_id, status, now, row["id"]))
                return row["id"]
            cur = self._exec(
                "INSERT INTO series (canonical_title, kind, year, ordering,"
                " provider, provider_id, imdb_id, status, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (canonical_title, kind, year, ordering, provider,
                 provider_id, imdb_id, status, now))
            return cur.lastrowid

    def get_series(self, series_id):
        return self._one("SELECT * FROM series WHERE id=?", (series_id,))

    def find_series_by_title(self, title, year=None):
        if year is not None:
            return self._one("SELECT * FROM series WHERE canonical_title=?"
                             " AND year=?", (title, year))
        return self._one("SELECT * FROM series WHERE canonical_title=?",
                         (title,))

    def set_series_status(self, series_id, status, now):
        with self.tx():
            self._exec("UPDATE series SET status=?, updated_at=? WHERE id=?",
                       (status, now, series_id))

    def set_manual_map(self, series_id, manual_map, now):
        with self.tx():
            self._exec("UPDATE series SET ordering='manual', status='manual',"
                       " manual_map=?, updated_at=? WHERE id=?",
                       (json.dumps(manual_map), now, series_id))

    # -- aliases --------------------------------------------------------------
    def add_alias(self, series_id, alias_normalised, kind="tv", source="apply"):
        with self.tx():
            self._exec("INSERT OR IGNORE INTO series_alias (series_id,"
                       " alias_normalised, kind, source) VALUES (?,?,?,?)",
                       (series_id, alias_normalised, kind, source))

    def find_series_by_alias(self, alias_normalised, kind=None):
        if kind:
            row = self._one("SELECT series_id FROM series_alias WHERE"
                            " alias_normalised=? AND kind=?",
                            (alias_normalised, kind))
        else:
            row = self._one("SELECT series_id FROM series_alias WHERE"
                            " alias_normalised=?", (alias_normalised,))
        return self.get_series(row["series_id"]) if row else None

    # -- episodes ---------------------------------------------------------------
    def replace_episode_list(self, series_id, episodes, now):
        """episodes: [{season_number, episode_number, absolute_number,
        title, air_date}]. Replaces the stored list for the series and
        refreshes counters, in one transaction. Files keep their episode
        links only where the slot still exists (FK set null on delete is
        NOT used — episodes are keyed stably by (season, episode), so we
        upsert instead of delete+insert)."""
        with self.tx():
            for ep in episodes:
                self._exec(
                    "INSERT INTO season (series_id, season_number)"
                    " VALUES (?,?) ON CONFLICT(series_id, season_number)"
                    " DO NOTHING", (series_id, ep["season_number"]))
                self._exec(
                    "INSERT INTO episode (series_id, season_number,"
                    " episode_number, absolute_number, title, air_date)"
                    " VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(series_id, season_number, episode_number)"
                    " DO UPDATE SET absolute_number=excluded.absolute_number,"
                    " title=excluded.title, air_date=excluded.air_date",
                    (series_id, ep["season_number"], ep["episode_number"],
                     ep.get("absolute_number"), ep.get("title"),
                     ep.get("air_date")))
            self._recount_series(series_id, now)

    def list_episodes(self, series_id):
        return self._all("SELECT * FROM episode WHERE series_id=?"
                         " ORDER BY season_number, episode_number",
                         (series_id,))

    def get_episode(self, series_id, season_number, episode_number):
        return self._one("SELECT * FROM episode WHERE series_id=?"
                         " AND season_number=? AND episode_number=?",
                         (series_id, season_number, episode_number))

    def get_episode_by_absolute(self, series_id, absolute_number):
        return self._one("SELECT * FROM episode WHERE series_id=?"
                         " AND absolute_number=?", (series_id, absolute_number))

    # -- counters ---------------------------------------------------------------
    def _recount_series(self, series_id, now):
        """Recompute one series' counters. Caller holds the transaction."""
        total = self._one("SELECT COUNT(*) AS n FROM episode WHERE series_id=?",
                          (series_id,))["n"]
        filled = self._one(
            "SELECT COUNT(*) AS n FROM episode e WHERE e.series_id=? AND"
            " EXISTS (SELECT 1 FROM media_file m WHERE m.episode_id=e.id"
            " AND m.is_primary=1)", (series_id,))["n"]
        self._exec("UPDATE series SET episode_count_cached=?,"
                   " file_count_cached=?, missing_count_cached=?,"
                   " updated_at=? WHERE id=?",
                   (total, filled, total - filled, now, series_id))

    def recount_series(self, series_id, now):
        with self.tx():
            self._recount_series(series_id, now)

    # -- files --------------------------------------------------------------------
    def upsert_media_file(self, chat_id, msg_id, now, **fields):
        """Insert or refresh a media_file row keyed by (chat_id, msg_id).
        Never touches match fields on refresh unless explicitly passed."""
        allowed = ("file_unique_id", "source_channel_id", "original_caption",
                   "current_caption", "parsed_title_raw", "parsed_season",
                   "parsed_episode", "parsed_absolute", "parsed_year",
                   "height", "size", "location_status")
        vals = {k: fields[k] for k in allowed if k in fields}
        with self.tx():
            row = self._one("SELECT id FROM media_file WHERE chat_id=?"
                            " AND msg_id=?", (chat_id, msg_id))
            if row:
                if vals:
                    sets = ", ".join(f"{k}=?" for k in vals)
                    self._exec(f"UPDATE media_file SET {sets}, updated_at=?"
                               " WHERE id=?",
                               (*vals.values(), now, row["id"]))
                return row["id"]
            cols = ["chat_id", "msg_id", "added_at", "updated_at", *vals.keys()]
            cur = self._exec(
                f"INSERT INTO media_file ({', '.join(cols)})"
                f" VALUES ({', '.join('?' * len(cols))})",
                (chat_id, msg_id, now, now, *vals.values()))
            return cur.lastrowid

    def get_file(self, file_id):
        return self._one("SELECT * FROM media_file WHERE id=?", (file_id,))

    def get_file_by_ref(self, chat_id, msg_id):
        return self._one("SELECT * FROM media_file WHERE chat_id=?"
                         " AND msg_id=?", (chat_id, msg_id))

    def list_unidentified(self, channel_id=None, limit=60, after_id=0):
        if channel_id:
            return self._all(
                "SELECT * FROM media_file WHERE series_id IS NULL"
                " AND source_channel_id=? AND id>? ORDER BY id LIMIT ?",
                (channel_id, after_id, limit))
        return self._all(
            "SELECT * FROM media_file WHERE series_id IS NULL AND id>?"
            " ORDER BY id LIMIT ?", (after_id, limit))

    def count_unidentified(self, channel_id=None):
        if channel_id:
            return self._one("SELECT COUNT(*) AS n FROM media_file WHERE"
                             " series_id IS NULL AND source_channel_id=?",
                             (channel_id,))["n"]
        return self._one("SELECT COUNT(*) AS n FROM media_file WHERE"
                         " series_id IS NULL")["n"]

    def assign_file(self, file_id, series_id, episode_id, is_primary,
                    method, score, now):
        """Link a file to a series/episode slot; counters for the old and
        new series are refreshed in the same transaction."""
        with self.tx():
            old = self._one("SELECT series_id FROM media_file WHERE id=?",
                            (file_id,))
            self._exec(
                "UPDATE media_file SET series_id=?, episode_id=?,"
                " is_primary=?, match_status='matched', match_method=?,"
                " evidence_score=?, updated_at=? WHERE id=?",
                (series_id, episode_id, 1 if is_primary else 0, method,
                 score, now, file_id))
            touched = {series_id}
            if old and old["series_id"]:
                touched.add(old["series_id"])
            for sid in touched:
                if sid:
                    self._recount_series(sid, now)

    def unassign_file(self, file_id, now, status="unmatched"):
        with self.tx():
            old = self._one("SELECT series_id FROM media_file WHERE id=?",
                            (file_id,))
            self._exec(
                "UPDATE media_file SET series_id=NULL, episode_id=NULL,"
                " is_primary=0, match_status=?, updated_at=? WHERE id=?",
                (status, now, file_id))
            if old and old["series_id"]:
                self._recount_series(old["series_id"], now)

    def set_file_caption(self, file_id, caption, now):
        with self.tx():
            self._exec("UPDATE media_file SET current_caption=?, updated_at=?"
                       " WHERE id=?", (caption, now, file_id))

    # -- work orders --------------------------------------------------------------
    def create_order(self, scope_kind, now, series_id=None, channel_id=None,
                     max_files=500, budget_applies=500, auto_floor=0.95,
                     approval_floor=0.80, opened_by="owner", expires_at=None):
        with self.tx():
            cur = self._exec(
                "INSERT INTO work_order (scope_kind, series_id,"
                " source_channel_id, max_files, budget_applies, auto_floor,"
                " approval_floor, opened_by, expires_at, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (scope_kind, series_id, channel_id, max_files, budget_applies,
                 auto_floor, approval_floor, opened_by, expires_at, now, now))
            return cur.lastrowid

    def get_order(self, order_id):
        return self._one("SELECT * FROM work_order WHERE id=?", (order_id,))

    def list_orders(self, states=None, limit=50):
        if states:
            marks = ", ".join("?" * len(states))
            return self._all(f"SELECT * FROM work_order WHERE state IN"
                             f" ({marks}) ORDER BY id DESC LIMIT ?",
                             (*states, limit))
        return self._all("SELECT * FROM work_order ORDER BY id DESC LIMIT ?",
                         (limit,))

    def set_order_state(self, order_id, state, now):
        with self.tx():
            self._exec("UPDATE work_order SET state=?, updated_at=?"
                       " WHERE id=?", (state, now, order_id))

    def set_order_cursor(self, order_id, episode_id, now):
        with self.tx():
            self._exec("UPDATE work_order SET cursor_episode_id=?,"
                       " updated_at=? WHERE id=?", (episode_id, now, order_id))

    def add_order_applies(self, order_id, n, now):
        with self.tx():
            self._exec("UPDATE work_order SET applies_used=applies_used+?,"
                       " updated_at=? WHERE id=?", (n, now, order_id))

    # -- plans ----------------------------------------------------------------------
    @staticmethod
    def plan_hash(items):
        """sha256 over the canonical item list (SPEC §6): sorted by file id,
        only the fields that change the outcome."""
        canon = sorted(
            ({"f": it["media_file_id"], "e": it.get("episode_id"),
              "c": it["new_caption"]} for it in items),
            key=lambda d: d["f"])
        blob = json.dumps(canon, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def create_plan(self, order_id, items, now):
        """items: [{media_file_id, episode_id, new_caption, evidence_score,
        score_breakdown, tier, refusal_reason}]"""
        digest = self.plan_hash(items)
        with self.tx():
            cur = self._exec("INSERT INTO plan (work_order_id, plan_hash,"
                             " created_at) VALUES (?,?,?)",
                             (order_id, digest, now))
            plan_id = cur.lastrowid
            for it in items:
                self._exec(
                    "INSERT INTO plan_item (plan_id, media_file_id,"
                    " episode_id, new_caption, evidence_score,"
                    " score_breakdown, tier, refusal_reason)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (plan_id, it["media_file_id"], it.get("episode_id"),
                     it["new_caption"], it["evidence_score"],
                     json.dumps(it.get("score_breakdown", {})),
                     it["tier"], it.get("refusal_reason")))
            return plan_id

    def get_plan(self, plan_id):
        return self._one("SELECT * FROM plan WHERE id=?", (plan_id,))

    def list_plan_items(self, plan_id):
        return self._all("SELECT * FROM plan_item WHERE plan_id=?"
                         " ORDER BY media_file_id", (plan_id,))

    def set_plan_state(self, plan_id, state):
        with self.tx():
            self._exec("UPDATE plan SET state=? WHERE id=?", (state, plan_id))

    def latest_plan_for_order(self, order_id):
        return self._one("SELECT * FROM plan WHERE work_order_id=?"
                         " ORDER BY id DESC LIMIT 1", (order_id,))

    # -- revisions --------------------------------------------------------------------
    def add_revision(self, file_id, before, after, actor, order_id, now):
        with self.tx():
            cur = self._exec(
                "INSERT INTO caption_revision (media_file_id, before_caption,"
                " after_caption, actor, work_order_id, applied_at)"
                " VALUES (?,?,?,?,?,?)",
                (file_id, before, after, actor, order_id, now))
            return cur.lastrowid

    def list_revisions(self, limit=50, file_id=None):
        if file_id:
            return self._all("SELECT * FROM caption_revision WHERE"
                             " media_file_id=? ORDER BY id DESC LIMIT ?",
                             (file_id, limit))
        return self._all("SELECT * FROM caption_revision ORDER BY id DESC"
                         " LIMIT ?", (limit,))

    def mark_revision_reverted(self, revision_id, now):
        with self.tx():
            self._exec("UPDATE caption_revision SET reverted_at=? WHERE id=?",
                       (now, revision_id))

    def latest_revision_for_file(self, file_id):
        return self._one("SELECT * FROM caption_revision WHERE media_file_id=?"
                         " AND reverted_at IS NULL ORDER BY id DESC LIMIT 1",
                         (file_id,))

    # -- provider cache -----------------------------------------------------------------
    def cache_get(self, key, now):
        row = self._one("SELECT * FROM provider_cache WHERE cache_key=?"
                        " AND expires_at>?", (key, now))
        if row is None:
            return None
        return {"payload": json.loads(row["payload"]),
                "negative": bool(row["negative"])}

    def cache_put(self, key, payload, ttl_seconds, now, negative=False):
        with self.tx():
            self._exec(
                "INSERT INTO provider_cache (cache_key, payload, negative,"
                " fetched_at, expires_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload,"
                " negative=excluded.negative, fetched_at=excluded.fetched_at,"
                " expires_at=excluded.expires_at",
                (key, json.dumps(payload), 1 if negative else 0, now,
                 now + ttl_seconds))

    # -- sync watermarks ------------------------------------------------------------------
    def get_watermark(self, key):
        row = self._one("SELECT value FROM sync_state WHERE key=?", (key,))
        return row["value"] if row else None

    def set_watermark(self, key, value):
        with self.tx():
            self._exec("INSERT INTO sync_state (key, value) VALUES (?,?)"
                       " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, str(value)))

    # -- totals for the page ---------------------------------------------------------------
    def totals(self):
        fixed = self._one("SELECT COUNT(*) AS n FROM caption_revision"
                          " WHERE reverted_at IS NULL")["n"]
        return {
            "unidentified": self.count_unidentified(),
            "series": self._one("SELECT COUNT(*) AS n FROM series")["n"],
            "files": self._one("SELECT COUNT(*) AS n FROM media_file")["n"],
            "revisions": fixed,
        }
