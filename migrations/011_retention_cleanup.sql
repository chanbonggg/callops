ALTER TABLE callops.recordings
    ADD COLUMN audio_deletion_state text NOT NULL DEFAULT 'NONE'
        CHECK (audio_deletion_state IN ('NONE', 'REQUESTED', 'RETRY_WAIT', 'COMPLETE'));

ALTER TABLE callops.artifacts
    ADD COLUMN deletion_state text NOT NULL DEFAULT 'NONE',
    ADD CONSTRAINT artifacts_deletion_state_check CHECK (
        deletion_state = 'NONE' OR (deletion_state IN ('REQUESTED', 'RETRY_WAIT', 'COMPLETE') AND NOT available)
    );

CREATE INDEX recordings_audio_expiry ON callops.recordings (received_at)
    WHERE classification_status = 'BUSINESS_APPROVED' AND deletion_state = 'NONE' AND audio_deletion_state = 'NONE';
CREATE INDEX artifacts_expiry ON callops.artifacts (expires_at) WHERE deletion_state = 'NONE';
CREATE INDEX attempts_unclean_recording ON callops.attempts (recording_id) WHERE NOT cleaned;
