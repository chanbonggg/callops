ALTER TABLE callops.artifacts ADD UNIQUE (id, recording_id);
ALTER TABLE callops.jobs ADD UNIQUE (id, recording_id);
ALTER TABLE callops.attempts ADD UNIQUE (id, job_id);

ALTER TABLE callops.attempts ADD COLUMN recording_id uuid;
UPDATE callops.attempts a SET recording_id = j.recording_id FROM callops.jobs j WHERE j.id = a.job_id;
ALTER TABLE callops.attempts
    ALTER COLUMN recording_id SET NOT NULL,
    DROP CONSTRAINT attempts_job_id_fkey,
    DROP CONSTRAINT attempts_result_artifact_id_fkey,
    ADD FOREIGN KEY (job_id, recording_id) REFERENCES callops.jobs (id, recording_id),
    ADD FOREIGN KEY (result_artifact_id, recording_id) REFERENCES callops.artifacts (id, recording_id);

ALTER TABLE callops.jobs
    DROP CONSTRAINT jobs_input_artifact_id_fkey,
    DROP CONSTRAINT jobs_active_attempt_fkey,
    ADD FOREIGN KEY (input_artifact_id, recording_id) REFERENCES callops.artifacts (id, recording_id),
    ADD FOREIGN KEY (active_attempt, id) REFERENCES callops.attempts (id, job_id);

ALTER TABLE callops.schedule_candidates
    DROP CONSTRAINT schedule_candidates_analysis_job_id_fkey,
    ADD FOREIGN KEY (analysis_job_id, recording_id) REFERENCES callops.jobs (id, recording_id);

CREATE INDEX attempts_job_recording ON callops.attempts (job_id, recording_id);
CREATE INDEX artifacts_recording ON callops.artifacts (recording_id);
