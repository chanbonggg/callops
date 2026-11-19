ALTER TABLE callops.tasks
    ADD COLUMN recording_id uuid REFERENCES callops.recordings (id),
    DROP CONSTRAINT tasks_status_check,
    ADD CHECK (status IN ('OPEN', 'DONE', 'CANCELLED'));

CREATE INDEX tasks_recording ON callops.tasks (recording_id) WHERE recording_id IS NOT NULL;

CREATE TABLE callops.task_candidates (
    id uuid PRIMARY KEY,
    recording_id uuid NOT NULL REFERENCES callops.recordings (id),
    analysis_job_id uuid NOT NULL,
    transcript_id uuid NOT NULL,
    position integer NOT NULL CHECK (position BETWEEN 1 AND 10),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 160 AND btrim(title) <> ''),
    due_at timestamptz,
    segment_ids integer[] NOT NULL CHECK (cardinality(segment_ids) BETWEEN 1 AND 10),
    expires_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'LINKED')),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    decided_at timestamptz,
    task_id uuid REFERENCES callops.tasks (id),
    UNIQUE (analysis_job_id, position),
    FOREIGN KEY (analysis_job_id, recording_id) REFERENCES callops.jobs (id, recording_id),
    FOREIGN KEY (transcript_id, recording_id) REFERENCES callops.artifacts (id, recording_id),
    CHECK ((status = 'PENDING') = (decided_at IS NULL)),
    CHECK ((status IN ('APPROVED', 'LINKED')) = (task_id IS NOT NULL)),
    CHECK (status <> 'APPROVED' OR task_id = id)
);

CREATE INDEX task_candidates_recording ON callops.task_candidates (recording_id);
CREATE INDEX task_candidates_transcript ON callops.task_candidates (transcript_id);
CREATE INDEX task_candidates_task ON callops.task_candidates (task_id) WHERE task_id IS NOT NULL;
CREATE INDEX task_candidates_expiry ON callops.task_candidates (expires_at);
