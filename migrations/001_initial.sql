CREATE TABLE keywords (
    id text PRIMARY KEY,
    niche text NOT NULL,
    query text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('channel', 'video')),
    target_format text NOT NULL DEFAULT '',
    enabled boolean NOT NULL DEFAULT true,
    next_run_at timestamptz NOT NULL DEFAULT now(),
    failures integer NOT NULL DEFAULT 0
);

CREATE TABLE channels (
    id text PRIMARY KEY,
    metadata jsonb NOT NULL,
    metadata_at timestamptz NOT NULL,
    selection text NOT NULL CHECK (selection IN ('Qualified', 'Needs Review', 'Excluded')),
    selection_reason text NOT NULL,
    contact_email text,
    contact_source text,
    contact_status text NOT NULL DEFAULT 'Unchecked',
    draft_status text NOT NULL DEFAULT 'Not drafted',
    draft_due timestamptz NOT NULL DEFAULT now(),
    sheet_revision bigint NOT NULL DEFAULT 1,
    sheet_synced bigint NOT NULL DEFAULT 0
);

CREATE TABLE channel_matches (
    channel_id text NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    keyword_id text NOT NULL REFERENCES keywords(id),
    video_id text,
    matched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, keyword_id)
);

CREATE TABLE drafts (
    id uuid PRIMARY KEY,
    channel_id text NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    recipient text NOT NULL,
    subject text NOT NULL,
    body text NOT NULL,
    evidence_ids jsonb NOT NULL,
    fingerprint text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    review_token text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX drafts_channel_newest ON drafts(channel_id, created_at DESC, id DESC);

CREATE TABLE approvals (
    draft_id uuid PRIMARY KEY REFERENCES drafts(id) ON DELETE CASCADE,
    eligibility_evidence text NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE TABLE send_attempts (
    draft_id uuid PRIMARY KEY REFERENCES drafts(id),
    channel_id text NOT NULL REFERENCES channels(id),
    normalized_recipient text NOT NULL,
    status text NOT NULL CHECK (status IN ('Sending', 'Gmail Accepted', 'Needs Review', 'Rejected')),
    message_id text NOT NULL UNIQUE,
    gmail_id text,
    reason text,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE UNIQUE INDEX one_first_touch_channel ON send_attempts(channel_id)
    WHERE status <> 'Rejected';
CREATE UNIQUE INDEX one_first_touch_recipient ON send_attempts(normalized_recipient)
    WHERE status <> 'Rejected';

CREATE TABLE suppressions (
    normalized_recipient text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sender_control (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    paused boolean NOT NULL DEFAULT true
);
INSERT INTO sender_control (singleton) VALUES (true);

CREATE TABLE api_budget (
    quota_day date NOT NULL,
    bucket text NOT NULL CHECK (bucket IN ('search', 'general')),
    used integer NOT NULL,
    PRIMARY KEY (quota_day, bucket)
);

CREATE TABLE job_runs (
    id uuid PRIMARY KEY,
    job text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL,
    report jsonb NOT NULL DEFAULT '{}'::jsonb,
    sheet_synced boolean NOT NULL DEFAULT false
);
