-- Librarian v2 schema — see docs/librarian-v2/SPEC.md §3.
-- Applied by store.py on first open; WAL is set per-connection, not here.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source_channel (
    id           INTEGER PRIMARY KEY,
    telegram_id  INTEGER NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'auth',        -- auth|manual|skip|dl
    trust_tier   TEXT NOT NULL DEFAULT 'normal',
    default_rule TEXT NOT NULL DEFAULT 'ask',         -- allow|ask|deny
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS series (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL DEFAULT 'tv',   -- movie|tv|anime|donghua|cartoon
    canonical_title     TEXT NOT NULL,
    year                INTEGER,
    ordering            TEXT NOT NULL DEFAULT 'aired', -- aired|dvd|absolute|tmdb|manual
    provider            TEXT,                          -- tmdb|tvdb|kitsu|cinemeta
    provider_id         TEXT,
    imdb_id             TEXT,
    status              TEXT NOT NULL DEFAULT 'unidentified', -- identified|unidentified|conflicted|manual
    episode_count_cached INTEGER NOT NULL DEFAULT 0,
    file_count_cached    INTEGER NOT NULL DEFAULT 0,
    missing_count_cached INTEGER NOT NULL DEFAULT 0,
    manual_map          TEXT,                          -- JSON, when ordering='manual'
    updated_at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_series_status ON series(status);

CREATE TABLE IF NOT EXISTS series_alias (
    id              INTEGER PRIMARY KEY,
    series_id       INTEGER NOT NULL REFERENCES series(id),
    alias_normalised TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'tv',
    source          TEXT NOT NULL DEFAULT 'apply',     -- provider|agent|owner|apply
    UNIQUE(alias_normalised, kind)
);

CREATE TABLE IF NOT EXISTS season (
    id            INTEGER PRIMARY KEY,
    series_id     INTEGER NOT NULL REFERENCES series(id),
    season_number INTEGER NOT NULL,
    UNIQUE(series_id, season_number)
);

CREATE TABLE IF NOT EXISTS episode (
    id              INTEGER PRIMARY KEY,
    series_id       INTEGER NOT NULL REFERENCES series(id),
    season_number   INTEGER NOT NULL,
    episode_number  INTEGER NOT NULL,
    absolute_number INTEGER,
    title           TEXT,
    air_date        TEXT,
    UNIQUE(series_id, season_number, episode_number)
);
CREATE INDEX IF NOT EXISTS idx_episode_series ON episode(series_id, season_number, episode_number);

CREATE TABLE IF NOT EXISTS media_file (
    id               INTEGER PRIMARY KEY,
    chat_id          INTEGER,
    msg_id           INTEGER,
    file_unique_id   TEXT,
    source_channel_id INTEGER REFERENCES source_channel(id),
    original_caption TEXT,
    current_caption  TEXT,
    parsed_title_raw TEXT,
    parsed_season    INTEGER,
    parsed_episode   INTEGER,
    parsed_absolute  INTEGER,
    parsed_year      INTEGER,
    height           INTEGER,
    size             INTEGER,
    match_status     TEXT NOT NULL DEFAULT 'unmatched', -- unmatched|proposed|matched|conflicted|ignored
    series_id        INTEGER REFERENCES series(id),
    episode_id       INTEGER REFERENCES episode(id),
    is_primary       INTEGER NOT NULL DEFAULT 0,
    evidence_score   REAL,
    match_method     TEXT,
    location_status  TEXT NOT NULL DEFAULT 'unknown',   -- located|unknown
    added_at         INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE(chat_id, msg_id)
);
-- "Unidentified in channel X" as an index scan (SPEC §3).
CREATE INDEX IF NOT EXISTS idx_media_unidentified
    ON media_file(source_channel_id) WHERE series_id IS NULL;
-- One primary file per episode slot; duplicates are versions (SPEC §3).
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_primary_slot
    ON media_file(episode_id) WHERE episode_id IS NOT NULL AND is_primary = 1;
CREATE INDEX IF NOT EXISTS idx_media_series ON media_file(series_id, match_status);

CREATE TABLE IF NOT EXISTS work_order (
    id                INTEGER PRIMARY KEY,
    scope_kind        TEXT NOT NULL,                   -- series|channel_unidentified
    series_id         INTEGER REFERENCES series(id),
    source_channel_id INTEGER REFERENCES source_channel(id),
    state             TEXT NOT NULL DEFAULT 'opened',  -- opened|planning|planned|applying|verifying|done|paused|killed|expired
    max_files         INTEGER NOT NULL DEFAULT 500,
    budget_applies    INTEGER NOT NULL DEFAULT 500,
    applies_used      INTEGER NOT NULL DEFAULT 0,
    auto_floor        REAL NOT NULL DEFAULT 0.95,
    approval_floor    REAL NOT NULL DEFAULT 0.80,
    cursor_episode_id INTEGER REFERENCES episode(id),
    opened_by         TEXT NOT NULL DEFAULT 'owner',   -- owner|bot
    expires_at        INTEGER,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    CHECK ((scope_kind = 'series' AND series_id IS NOT NULL AND source_channel_id IS NULL)
        OR (scope_kind = 'channel_unidentified' AND source_channel_id IS NOT NULL AND series_id IS NULL))
);

CREATE TABLE IF NOT EXISTS plan (
    id            INTEGER PRIMARY KEY,
    work_order_id INTEGER NOT NULL REFERENCES work_order(id),
    plan_hash     TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'draft',       -- draft|locked|approved|applied|void
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_item (
    id              INTEGER PRIMARY KEY,
    plan_id         INTEGER NOT NULL REFERENCES plan(id),
    media_file_id   INTEGER NOT NULL REFERENCES media_file(id),
    episode_id      INTEGER REFERENCES episode(id),
    new_caption     TEXT NOT NULL,
    evidence_score  REAL NOT NULL,
    score_breakdown TEXT NOT NULL DEFAULT '{}',        -- JSON
    tier            TEXT NOT NULL,                     -- auto|approval|refused
    refusal_reason  TEXT,
    UNIQUE(plan_id, media_file_id)
);

CREATE TABLE IF NOT EXISTS job (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,                     -- apply_caption|verify_fix|identify_series|sync_mirror|reconcile|digest|backup
    payload         TEXT NOT NULL DEFAULT '{}',        -- JSON
    state           TEXT NOT NULL DEFAULT 'ready',     -- ready|running|snoozed|dead|done
    claimed_by      TEXT,
    lease_expires_at INTEGER,
    heartbeat_at    INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 4,
    next_run_at     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_due ON job(state, next_run_at);

CREATE TABLE IF NOT EXISTS caption_revision (
    id            INTEGER PRIMARY KEY,
    media_file_id INTEGER NOT NULL REFERENCES media_file(id),
    before_caption TEXT,
    after_caption  TEXT,
    actor         TEXT NOT NULL,                       -- bot|owner|system
    work_order_id INTEGER REFERENCES work_order(id),
    applied_at    INTEGER NOT NULL,
    reverted_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_revision_file ON caption_revision(media_file_id, applied_at);

CREATE TABLE IF NOT EXISTS provider_cache (
    cache_key  TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    negative   INTEGER NOT NULL DEFAULT 0,
    fetched_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
