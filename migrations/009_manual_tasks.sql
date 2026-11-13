CREATE TABLE callops.tasks (
    id uuid PRIMARY KEY,
    request_id uuid NOT NULL UNIQUE,
    title text CHECK (char_length(title) BETWEEN 1 AND 160 AND btrim(title) <> ''),
    due_at timestamptz,
    status text DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'DONE')),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    completed_at timestamptz,
    deleted_at timestamptz,
    CHECK (
        (deleted_at IS NULL AND title IS NOT NULL AND status IS NOT NULL
            AND (status = 'DONE') = (completed_at IS NOT NULL))
        OR (deleted_at IS NOT NULL AND title IS NULL AND due_at IS NULL
            AND status IS NULL AND completed_at IS NULL)
    )
);
