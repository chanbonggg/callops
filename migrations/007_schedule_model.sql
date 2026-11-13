ALTER TABLE callops.manual_schedules RENAME TO schedules;
ALTER TABLE callops.schedules
    ALTER COLUMN request_id DROP NOT NULL,
    ADD COLUMN source text NOT NULL DEFAULT 'manual',
    ADD COLUMN recording_id uuid REFERENCES callops.recordings,
    ADD COLUMN approved_candidate_id uuid,
    ADD COLUMN approved_revision integer,
    ADD UNIQUE (id, recording_id),
    ADD CHECK ((approved_candidate_id IS NULL) = (approved_revision IS NULL)),
    ADD CHECK (deleted_at IS NULL OR approved_candidate_id IS NULL),
    ADD CHECK (
        (source = 'manual' AND request_id IS NOT NULL AND recording_id IS NULL AND approved_candidate_id IS NULL)
        OR (source = 'call' AND request_id IS NULL AND (recording_id IS NOT NULL OR deleted_at IS NOT NULL))
    );
CREATE INDEX schedules_recording ON callops.schedules (recording_id) WHERE recording_id IS NOT NULL;

-- A draft or rejection may have overwritten candidate fields since the last approval.
WITH previous AS (
    SELECT c.logical_item_id, c.recording_id, a.candidate_id, a.revision, a.snapshot,
           a.revision IS NOT NULL AND r.classification_status = 'BUSINESS_APPROVED'
               AND r.deletion_state = 'NONE' AS keep_content
    FROM callops.schedule_candidates c
    JOIN callops.recordings r ON r.id = c.recording_id
    LEFT JOIN LATERAL (
        SELECT candidate_id, revision, snapshot
        FROM callops.schedule_approvals WHERE candidate_id = c.id
        ORDER BY revision DESC LIMIT 1
    ) a ON true
    WHERE c.logical_item_id IS NOT NULL
)
INSERT INTO callops.schedules
    (id, source, recording_id, title, start_at, end_at, revision, deleted_at, approved_candidate_id, approved_revision)
SELECT logical_item_id, 'call', recording_id,
       CASE WHEN keep_content THEN snapshot->>'title' END,
       CASE WHEN keep_content THEN (snapshot->>'start_at')::timestamptz END,
       CASE WHEN keep_content THEN (snapshot->>'end_at')::timestamptz END,
       COALESCE(revision, 1), CASE WHEN NOT keep_content THEN now() END,
       CASE WHEN keep_content THEN candidate_id END,
       CASE WHEN keep_content THEN revision END
FROM previous;

-- Legacy delivery records may outlive the candidate that first allocated their ID.
INSERT INTO callops.schedules (id, source, deleted_at)
SELECT retained.id, 'call', now()
FROM (
    SELECT logical_item_id AS id FROM callops.calendar_deliveries
    UNION SELECT event_id FROM callops.mock_calendar_events
) retained
WHERE NOT EXISTS (SELECT 1 FROM callops.schedules s WHERE s.id = retained.id);

ALTER TABLE callops.calendar_deliveries ADD COLUMN candidate_id uuid;
UPDATE callops.calendar_deliveries d SET candidate_id = c.id
FROM callops.schedule_candidates c WHERE c.logical_item_id = d.logical_item_id;
ALTER TABLE callops.calendar_deliveries ADD FOREIGN KEY (logical_item_id) REFERENCES callops.schedules;
ALTER TABLE callops.mock_calendar_events ADD FOREIGN KEY (event_id) REFERENCES callops.schedules;

ALTER TABLE callops.schedule_candidates
    DROP CONSTRAINT schedule_candidates_logical_item_id_key,
    ADD UNIQUE (id, logical_item_id),
    ADD FOREIGN KEY (logical_item_id, recording_id) REFERENCES callops.schedules (id, recording_id);
CREATE INDEX schedule_candidates_logical_item ON callops.schedule_candidates (logical_item_id);

ALTER TABLE callops.schedule_approvals ADD COLUMN schedule_id uuid;
UPDATE callops.schedule_approvals a SET schedule_id = c.logical_item_id
FROM callops.schedule_candidates c WHERE c.id = a.candidate_id;
ALTER TABLE callops.schedule_approvals
    ALTER COLUMN schedule_id SET NOT NULL,
    DROP CONSTRAINT schedule_approvals_candidate_id_fkey,
    ADD UNIQUE (schedule_id, candidate_id, revision),
    ADD FOREIGN KEY (candidate_id, schedule_id) REFERENCES callops.schedule_candidates (id, logical_item_id) ON DELETE CASCADE;

ALTER TABLE callops.schedules
    ADD FOREIGN KEY (id, approved_candidate_id, approved_revision)
    REFERENCES callops.schedule_approvals (schedule_id, candidate_id, revision)
    ON DELETE SET NULL (approved_candidate_id, approved_revision);
