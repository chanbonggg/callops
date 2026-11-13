CREATE TABLE callops.calendar_deliveries (
    logical_item_id uuid PRIMARY KEY,
    revision integer NOT NULL CHECK (revision > 0),
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('UNKNOWN', 'SUCCEEDED'))
);

CREATE TABLE callops.mock_calendar_events (
    event_id uuid PRIMARY KEY,
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$')
);
