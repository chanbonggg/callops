ALTER TABLE callops.recordings ADD COLUMN deletion_state text NOT NULL DEFAULT 'NONE'
    CHECK (deletion_state IN ('NONE', 'REQUESTED', 'RETRY_WAIT', 'COMPLETE'));
CREATE TABLE callops.receipts (
    id uuid PRIMARY KEY, recording_id uuid NOT NULL REFERENCES callops.recordings,
    source_name text NOT NULL, cleaned boolean NOT NULL DEFAULT false
);
CREATE TABLE callops.classification_events (
    id uuid PRIMARY KEY, recording_id uuid NOT NULL REFERENCES callops.recordings,
    decision text NOT NULL, decided_at timestamptz NOT NULL
);
CREATE TABLE callops.rejected_receipts (
    source_name text PRIMARY KEY, identity text NOT NULL,
    expires_at timestamptz NOT NULL, cleanup_failed boolean NOT NULL DEFAULT false
);
CREATE TABLE callops.sessions (
    token_hash text PRIMARY KEY, csrf text NOT NULL, expires_at timestamptz NOT NULL
);
CREATE TABLE callops.login_limits (
    id integer PRIMARY KEY CHECK (id = 1), failures integer NOT NULL DEFAULT 0,
    retry_at timestamptz NOT NULL
);
CREATE TABLE callops.artifacts (
    id uuid PRIMARY KEY, recording_id uuid NOT NULL REFERENCES callops.recordings,
    version integer NOT NULL CHECK (version > 0), sha256 text NOT NULL,
    size_bytes bigint NOT NULL, expires_at timestamptz NOT NULL,
    available boolean NOT NULL DEFAULT true
);
CREATE TABLE callops.jobs (
    id uuid PRIMARY KEY, recording_id uuid NOT NULL REFERENCES callops.recordings,
    stage text NOT NULL CHECK (stage IN ('STT', 'ANALYSIS')),
    pipeline_version integer NOT NULL DEFAULT 1,
    input_artifact_id uuid REFERENCES callops.artifacts,
    status text NOT NULL DEFAULT 'WAITING_FOR_WORKER' CHECK (status IN
      ('WAITING_FOR_WORKER','WAITING_FOR_FILE','PREPARING','RUNNING','UPLOADING',
       'SUCCEEDED','RETRY_WAIT','FAILED_MANUAL','CANCEL_REQUESTED','CANCELLED')),
    failures integer NOT NULL DEFAULT 0, retry_at timestamptz NOT NULL,
    error_code text, active_attempt uuid,
    UNIQUE(recording_id, stage, pipeline_version),
    CHECK ((stage = 'STT') = (input_artifact_id IS NULL))
);
CREATE TABLE callops.attempts (
    id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES callops.jobs,
    worker_id text NOT NULL, lease_token uuid NOT NULL UNIQUE,
    lease_until timestamptz NOT NULL, preparation_deadline timestamptz NOT NULL,
    execution_deadline timestamptz, upload_deadline timestamptz,
    start_request_id uuid, result_request_id uuid UNIQUE,
    result_sha256 text, result_size bigint, spool_created_at timestamptz,
    result_artifact_id uuid REFERENCES callops.artifacts,
    cleanup_requested boolean NOT NULL DEFAULT false,
    cleanup_version integer NOT NULL DEFAULT 1, cleaned boolean NOT NULL DEFAULT false
);
ALTER TABLE callops.jobs ADD FOREIGN KEY (active_attempt) REFERENCES callops.attempts;
CREATE INDEX jobs_ready ON callops.jobs (status, retry_at);
