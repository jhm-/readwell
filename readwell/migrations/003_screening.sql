-- Phase 3: screening, feedback, LLM ledger, Send to Supernote.

-- Screening outcome per item (cluster leads only).
--   NULL         not screened yet
--   muted        matched a source mute term
--   passthrough  source has filter mode 'none'
--   dropped      keyword prefilter found nothing worth an LLM call
--   deferred     waiting for budget (or for the LLM to come back)
--   screened     scores written
--   skipped      not screenable (removal notices, too old)
ALTER TABLE items ADD COLUMN screen_state TEXT;
ALTER TABLE items ADD COLUMN screen_reason TEXT;     -- the LLM's one-line "why it's here"
ALTER TABLE items ADD COLUMN screen_summary TEXT;    -- 2-3 sentence summary for OICs and gazettes
ALTER TABLE items ADD COLUMN screen_version TEXT;    -- topic-profile version it was scored under
ALTER TABLE items ADD COLUMN screened_at TEXT;
CREATE INDEX items_screen ON items (screen_state, first_seen_at);

-- stage: prefilter (0-1 keyword score) | llm (0-10) | keywords (0-10, filter mode 'keywords')
CREATE TABLE scores (
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    topic_id TEXT NOT NULL,
    stage    TEXT NOT NULL,
    score    REAL NOT NULL,
    at       TEXT NOT NULL,
    PRIMARY KEY (item_id, topic_id, stage)
);
CREATE INDEX scores_topic ON scores (topic_id, stage, score);

-- LLM results by content, so re-fetches and duplicates never pay twice.
CREATE TABLE screen_cache (
    content_hash TEXT NOT NULL,
    version      TEXT NOT NULL,   -- topic-set version + summary flag
    result       TEXT NOT NULL,   -- JSON {scores: {topic: n}, reason, summary}
    at           TEXT NOT NULL,
    PRIMARY KEY (content_hash, version)
);

-- 👍 / 👎 votes, kept forever: they are the examples the LLM learns from.
CREATE TABLE feedback (
    id       INTEGER PRIMARY KEY,
    item_id  INTEGER NOT NULL,
    topic_id TEXT NOT NULL,
    vote     INTEGER NOT NULL,    -- +1 / -1
    title    TEXT,                -- copied so the example survives content purges
    source_id TEXT,
    at       TEXT NOT NULL,
    UNIQUE (item_id, topic_id)
);

CREATE TABLE llm_calls (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    day           TEXT NOT NULL,  -- budget day (local date)
    model         TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    items         INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    error         TEXT
);
CREATE INDEX llm_calls_day ON llm_calls (day);

-- Send to Supernote queue.
CREATE TABLE sends (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    done_at      TEXT,
    path         TEXT,
    error        TEXT
);
