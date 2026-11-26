CREATE TABLE callops.rag_questions (
    id uuid PRIMARY KEY,
    owner text NOT NULL,
    request jsonb,
    evidence jsonb NOT NULL DEFAULT '[]',
    source_artifacts uuid[] NOT NULL DEFAULT '{}',
    source_pins jsonb NOT NULL DEFAULT '{}',
    result jsonb,
    error_code text,
    display_until timestamptz,
    expires_at timestamptz NOT NULL
);
CREATE INDEX rag_questions_expiry ON callops.rag_questions (expires_at);
CREATE TABLE callops.rag_schedule (
    worker_id text PRIMARY KEY,
    question_streak integer NOT NULL CHECK (question_streak BETWEEN 0 AND 2)
);

ALTER TABLE callops.artifacts ADD COLUMN rag_indexed boolean NOT NULL DEFAULT false;
CREATE TABLE callops.rag_chunks (
    artifact_id uuid NOT NULL REFERENCES callops.artifacts ON DELETE CASCADE,
    position integer NOT NULL CHECK (position >= 0),
    excerpts jsonb NOT NULL,
    chunk_version integer NOT NULL DEFAULT 1 CHECK (chunk_version = 1),
    model_digest text NOT NULL CHECK (model_digest ~ '^[0-9a-f]{64}$'),
    embedding callops.vector(1024) NOT NULL,
    PRIMARY KEY (artifact_id, position)
);

ALTER TABLE callops.jobs
    ALTER COLUMN recording_id DROP NOT NULL,
    DROP CONSTRAINT jobs_stage_check,
    DROP CONSTRAINT jobs_check,
    DROP CONSTRAINT jobs_recording_id_stage_pipeline_version_key,
    ADD COLUMN rag_question_id uuid REFERENCES callops.rag_questions ON DELETE CASCADE,
    ADD COLUMN rag_offset integer CHECK (rag_offset >= 0),
    ADD COLUMN rag_source_hash text,
    ADD COLUMN rag_input_hash text,
    ADD COLUMN rag_input bytea CHECK (octet_length(rag_input) <= 81920),
    ADD COLUMN rag_tries integer NOT NULL DEFAULT 0 CHECK (rag_tries BETWEEN 0 AND 2),
    ADD UNIQUE (id, rag_question_id),
    ADD CONSTRAINT jobs_input_scope_check CHECK (
        (stage IN ('STT', 'ANALYSIS') AND recording_id IS NOT NULL AND rag_question_id IS NULL
            AND rag_offset IS NULL AND rag_source_hash IS NULL AND rag_input_hash IS NULL
            AND rag_input IS NULL AND rag_tries = 0
            AND ((stage = 'STT') = (input_artifact_id IS NULL)))
        OR (stage = 'RAG_EMBED' AND recording_id IS NOT NULL AND input_artifact_id IS NOT NULL
            AND rag_question_id IS NULL AND rag_offset IS NOT NULL AND rag_source_hash IS NOT NULL)
        OR (stage IN ('RAG_EMBED', 'RAG_ANSWER') AND rag_question_id IS NOT NULL
            AND recording_id IS NULL AND input_artifact_id IS NULL AND rag_offset IS NULL AND rag_source_hash IS NULL)
    );
CREATE UNIQUE INDEX jobs_recording_pipeline ON callops.jobs (recording_id, stage, pipeline_version)
    WHERE stage IN ('STT', 'ANALYSIS');
CREATE UNIQUE INDEX jobs_rag_batch ON callops.jobs (input_artifact_id, rag_offset)
    WHERE rag_offset IS NOT NULL;
CREATE UNIQUE INDEX jobs_rag_question_stage ON callops.jobs (rag_question_id, stage)
    WHERE rag_question_id IS NOT NULL;

ALTER TABLE callops.attempts
    ALTER COLUMN recording_id DROP NOT NULL,
    ADD COLUMN rag_question_id uuid,
    ADD COLUMN rag_result_id uuid UNIQUE,
    ADD FOREIGN KEY (job_id, rag_question_id) REFERENCES callops.jobs (id, rag_question_id) ON DELETE CASCADE,
    ADD CONSTRAINT attempts_input_scope_check CHECK (
        num_nonnulls(recording_id, rag_question_id) = 1
        AND (rag_question_id IS NULL OR result_artifact_id IS NULL)
    );
CREATE INDEX attempts_rag_question ON callops.attempts (rag_question_id) WHERE rag_question_id IS NOT NULL;
