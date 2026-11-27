-- Readwell schema v1. Config (sources, topics, views) lives in YAML; this is state only.

CREATE TABLE sources (
    id                   TEXT PRIMARY KEY,
    kind                 TEXT NOT NULL,
    enabled              INTEGER NOT NULL DEFAULT 1,
    in_config            INTEGER NOT NULL DEFAULT 1,   -- 0 once removed from YAML (rows kept for history)
    url                  TEXT,                          -- as configured
    resolved_url         TEXT,                          -- feed URL found by discovery
    resolved_at          TEXT,
    title                TEXT,                          -- feed's own title
    site_url             TEXT,
    next_run_at          TEXT,
    last_run_at          TEXT,
    last_ok_at           TEXT,
    last_status          INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    failing_since        TEXT,
    error_kind           TEXT,                          -- discovery | http | network | parse | empty
    last_error           TEXT,
    etag                 TEXT,
    last_modified        TEXT,
    cursor               TEXT,                          -- numbered / documents sources
    baseline_done        INTEGER NOT NULL DEFAULT 0,    -- inventory listings
    typical_count        INTEGER                        -- rolling typical items per fetch
);

CREATE TABLE clusters (
    id           INTEGER PRIMARY KEY,
    lead_item_id INTEGER,
    created_at   TEXT NOT NULL
);

CREATE TABLE items (
    id            INTEGER PRIMARY KEY,
    source_id     TEXT NOT NULL,
    guid          TEXT NOT NULL,
    url           TEXT,
    canonical_url TEXT,
    title         TEXT,
    author        TEXT,
    published_at  TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    removed_at    TEXT,
    summary       TEXT,
    content_html  TEXT,
    content_text  TEXT,
    word_count    INTEGER NOT NULL DEFAULT 0,
    content_hash  TEXT,
    title_key     TEXT,                              -- sorted meaningful title words
    doc_type      TEXT NOT NULL DEFAULT 'article',
    paywall       TEXT,
    archive_url   TEXT,
    cluster_id    INTEGER REFERENCES clusters(id),
    is_lead       INTEGER NOT NULL DEFAULT 1,
    state         TEXT NOT NULL DEFAULT 'normalized',
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    UNIQUE (source_id, guid)
);

CREATE INDEX items_canonical   ON items (canonical_url);
CREATE INDEX items_first_seen  ON items (first_seen_at);
CREATE INDEX items_source_seen ON items (source_id, first_seen_at);
CREATE INDEX items_cluster     ON items (cluster_id);
CREATE INDEX items_title_key   ON items (title_key);
CREATE INDEX items_lead_seen   ON items (is_lead, first_seen_at);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    source_id   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,              -- ok | not_modified | error
    http_status INTEGER,
    fetched     INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE INDEX runs_source ON runs (source_id, started_at);
