ALTER TABLE keywords ADD COLUMN criteria jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE channel_matches
    ADD COLUMN selection text NOT NULL DEFAULT 'Needs Review'
        CHECK (selection IN ('Qualified', 'Needs Review', 'Excluded')),
    ADD COLUMN selection_reason text NOT NULL DEFAULT 'awaiting_recheck',
    ADD COLUMN location_match text NOT NULL DEFAULT 'Any location';

UPDATE channel_matches m
SET selection = c.selection, selection_reason = c.selection_reason
FROM channels c
WHERE c.id = m.channel_id;

CREATE INDEX channel_matches_keyword ON channel_matches(keyword_id);

UPDATE channels SET sheet_revision = sheet_revision + 1;
