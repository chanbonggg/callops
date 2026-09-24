CREATE TABLE callops.recordings (
    id uuid PRIMARY KEY,
    sha256 text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    storage_key uuid NOT NULL UNIQUE,
    call_started_at timestamptz,
    call_time_source text,
    call_time_trusted boolean NOT NULL DEFAULT false,
    received_at timestamptz NOT NULL DEFAULT now(),
    duration_seconds double precision CHECK (duration_seconds > 0 AND duration_seconds < 'Infinity'::float8),
    classification_status text NOT NULL DEFAULT 'PENDING'
        CHECK (classification_status IN ('PENDING', 'DEFERRED', 'BUSINESS_APPROVED', 'PRIVATE_EXCLUDED', 'EXPIRED')),
    expires_at timestamptz NOT NULL DEFAULT (now() + interval '72 hours'),
    output_decision_version bigint NOT NULL DEFAULT 0 CHECK (output_decision_version >= 0),
    CHECK (NOT call_time_trusted OR (call_started_at IS NOT NULL AND call_time_source IS NOT NULL))
);
