ALTER TABLE callops.recordings ADD COLUMN counterparty text
    CHECK (counterparty IS NULL OR (char_length(counterparty) BETWEEN 1 AND 100 AND btrim(counterparty) <> ''));
