ALTER TABLE callops.mock_calendar_events
    ADD COLUMN version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    ADD COLUMN deleted boolean NOT NULL DEFAULT false,
    ADD COLUMN title text,
    ADD COLUMN start_at timestamptz,
    ADD COLUMN end_at timestamptz,
    ADD CHECK (
        (title IS NULL AND start_at IS NULL AND end_at IS NULL)
        OR (NOT deleted AND title IS NOT NULL AND char_length(title) BETWEEN 1 AND 160
            AND btrim(title) <> '' AND start_at IS NOT NULL AND end_at IS NOT NULL AND end_at > start_at)
    );

ALTER TABLE callops.calendar_deliveries
    ADD COLUMN synced_schedule_revision integer CHECK (synced_schedule_revision > 0),
    ADD COLUMN synced_event_version bigint NOT NULL DEFAULT 1 CHECK (synced_event_version > 0);

-- Older deliveries have a checksum but no provider payload. Keep the known baseline
-- only when the schedule still references the approval used for the original event.
UPDATE callops.calendar_deliveries d SET synced_schedule_revision=s.revision
FROM callops.schedules s, callops.mock_calendar_events e
WHERE d.logical_item_id=s.id AND e.event_id=s.id AND s.deleted_at IS NULL
    AND d.candidate_id=s.approved_candidate_id AND d.revision=s.approved_revision;
