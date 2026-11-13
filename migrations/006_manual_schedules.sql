CREATE TABLE callops.manual_schedules (
    id uuid PRIMARY KEY,
    request_id uuid NOT NULL UNIQUE,
    title text CHECK (char_length(title) BETWEEN 1 AND 160 AND btrim(title) <> ''),
    start_at timestamptz,
    end_at timestamptz,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    deleted_at timestamptz,
    CHECK (
        (deleted_at IS NULL AND title IS NOT NULL AND start_at IS NOT NULL AND end_at IS NOT NULL AND end_at > start_at)
        OR (deleted_at IS NOT NULL AND title IS NULL AND start_at IS NULL AND end_at IS NULL)
    )
);
