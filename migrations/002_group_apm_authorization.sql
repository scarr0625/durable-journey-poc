-- Normalize group authorization for an existing Durable Cloud Journey database.
-- The authoritative tables are access_groups, access_group_members, and
-- apm_group_assignments.

BEGIN;

CREATE TABLE IF NOT EXISTS access_groups (
    id VARCHAR(128) PRIMARY KEY,
    name VARCHAR(256) NOT NULL
);

CREATE TABLE IF NOT EXISTS access_group_members (
    group_id VARCHAR(128) NOT NULL
        REFERENCES access_groups (id) ON DELETE CASCADE,
    user_subject VARCHAR(256) NOT NULL,
    PRIMARY KEY (group_id, user_subject)
);

CREATE INDEX IF NOT EXISTS ix_access_group_members_user_subject
    ON access_group_members (user_subject);

CREATE TABLE IF NOT EXISTS apm_group_assignments (
    apm_id VARCHAR(64) PRIMARY KEY,
    group_id VARCHAR(128) NOT NULL
        REFERENCES access_groups (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_apm_group_assignments_group_id
    ON apm_group_assignments (group_id);

INSERT INTO access_groups (id, name)
VALUES
    ('GROUP_1', 'Cloud Journey Group 1'),
    ('GROUP_2', 'Cloud Journey Group 2')
ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name;

INSERT INTO access_group_members (group_id, user_subject)
VALUES
    ('GROUP_1', 'sam'),
    ('GROUP_1', 'ivan'),
    ('GROUP_1', 'adi'),
    ('GROUP_2', 'abdur'),
    ('GROUP_2', 'ajir')
ON CONFLICT (group_id, user_subject) DO NOTHING;

INSERT INTO apm_group_assignments (apm_id, group_id)
VALUES
    ('100401', 'GROUP_1'),
    ('100402', 'GROUP_1'),
    ('100403', 'GROUP_2'),
    ('100404', 'GROUP_2')
ON CONFLICT (apm_id) DO UPDATE SET group_id = EXCLUDED.group_id;

ALTER TABLE journeys
    ADD COLUMN IF NOT EXISTS access_group_id VARCHAR(128);

-- Preserve existing Journeys whose APM IDs are not configured by assigning a
-- deterministic legacy group based on the original owner.
INSERT INTO access_groups (id, name)
SELECT DISTINCT
    'LEGACY_' || UPPER(MD5(owner_subject)),
    'Legacy owner ' || LEFT(owner_subject, 16)
FROM journeys AS j
WHERE NOT EXISTS (
    SELECT 1 FROM apm_group_assignments AS aga WHERE aga.apm_id = j.apm_id
)
ON CONFLICT (id) DO NOTHING;

INSERT INTO access_group_members (group_id, user_subject)
SELECT DISTINCT
    'LEGACY_' || UPPER(MD5(owner_subject)),
    owner_subject
FROM journeys AS j
WHERE NOT EXISTS (
    SELECT 1 FROM apm_group_assignments AS aga WHERE aga.apm_id = j.apm_id
)
ON CONFLICT (group_id, user_subject) DO NOTHING;

INSERT INTO apm_group_assignments (apm_id, group_id)
SELECT
    apm_id,
    'LEGACY_' || UPPER(MD5(owner_subject))
FROM journeys AS j
WHERE NOT EXISTS (
    SELECT 1 FROM apm_group_assignments AS aga WHERE aga.apm_id = j.apm_id
)
ON CONFLICT (apm_id) DO NOTHING;

UPDATE journeys AS j
SET access_group_id = aga.group_id
FROM apm_group_assignments AS aga
WHERE aga.apm_id = j.apm_id
  AND j.access_group_id IS DISTINCT FROM aga.group_id;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM journeys WHERE access_group_id IS NULL) THEN
        RAISE EXCEPTION
            'Some journeys have no access group; resolve them before deployment';
    END IF;
END $$;

ALTER TABLE journeys
    ALTER COLUMN access_group_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'journeys'::regclass
          AND conname = 'fk_journeys_access_group_id'
    ) THEN
        ALTER TABLE journeys
            ADD CONSTRAINT fk_journeys_access_group_id
            FOREIGN KEY (access_group_id)
            REFERENCES access_groups (id)
            ON DELETE RESTRICT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_journeys_access_group_id
    ON journeys (access_group_id);

COMMIT;
