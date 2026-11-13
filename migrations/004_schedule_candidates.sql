CREATE TABLE callops.schedule_candidates (
    id uuid PRIMARY KEY,
    recording_id uuid NOT NULL REFERENCES callops.recordings,
    analysis_job_id uuid NOT NULL REFERENCES callops.jobs,
    position integer NOT NULL CHECK (position BETWEEN 1 AND 10),
    segment_ids integer[] NOT NULL CHECK (cardinality(segment_ids) BETWEEN 1 AND 10),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 160 AND btrim(title) <> ''),
    time_hint text NOT NULL CHECK (char_length(time_hint) <= 240),
    start_at timestamptz,
    end_at timestamptz,
    status text NOT NULL CHECK (status IN ('NEEDS_CONFIRMATION','AWAITING_APPROVAL','APPROVED','REJECTED')),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    logical_item_id uuid UNIQUE,
    decided_by text,
    decided_at timestamptz,
    expires_at timestamptz NOT NULL,
    UNIQUE (analysis_job_id, position),
    CHECK (end_at > start_at),
    CHECK (status NOT IN ('AWAITING_APPROVAL','APPROVED') OR (start_at IS NOT NULL AND end_at IS NOT NULL)),
    CHECK (status <> 'NEEDS_CONFIRMATION' OR start_at IS NULL OR end_at IS NULL),
    CHECK (status <> 'APPROVED' OR logical_item_id IS NOT NULL),
    CHECK ((status IN ('APPROVED','REJECTED')) = (decided_at IS NOT NULL)),
    CHECK ((decided_at IS NULL) = (decided_by IS NULL))
);
CREATE INDEX schedule_candidates_recording ON callops.schedule_candidates (recording_id);
CREATE INDEX schedule_candidates_expiry ON callops.schedule_candidates (expires_at);

CREATE TABLE callops.schedule_approvals (
    candidate_id uuid NOT NULL REFERENCES callops.schedule_candidates ON DELETE CASCADE,
    revision integer NOT NULL CHECK (revision > 0),
    actor text NOT NULL,
    approved_at timestamptz NOT NULL,
    snapshot jsonb NOT NULL,
    snapshot_sha256 text NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (candidate_id, revision)
);
