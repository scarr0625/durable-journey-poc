-- Run against the existing Cloud SQL database before deploying this revision.
-- Review duplicate APM rows and the owner-subject mapping before running this file.

BEGIN;

ALTER TABLE journeys
    ADD COLUMN IF NOT EXISTS owner_subject VARCHAR(256);

-- Legacy PoC rows used requested_by as the simulated identity. Replace these
-- values with the stable Agent Runtime user_id where it differs.
UPDATE journeys
SET owner_subject = requested_by
WHERE owner_subject IS NULL;

-- This query must return zero rows before the unique index can be created.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM journeys
        GROUP BY apm_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Duplicate journeys.apm_id values exist; resolve them before migration';
    END IF;
END $$;

ALTER TABLE journeys
    ALTER COLUMN owner_subject SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_journeys_owner_subject
    ON journeys (owner_subject);

CREATE UNIQUE INDEX IF NOT EXISTS uq_journeys_apm_id
    ON journeys (apm_id);

COMMIT;
