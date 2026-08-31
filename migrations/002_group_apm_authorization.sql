-- Add the database-backed group-to-APM authorization mapping.
-- If an equivalent enterprise table already exists, adapt the application model
-- to that table instead of maintaining two sources of truth.

BEGIN;

CREATE TABLE IF NOT EXISTS apm_group_access (
    apm_id VARCHAR(64) PRIMARY KEY,
    group_name VARCHAR(128) NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_apm_group_access_group_name
    ON apm_group_access (group_name);

-- Demo mappings for the unauthenticated PoC. Existing mappings win.
INSERT INTO apm_group_access (apm_id, group_name)
VALUES
    ('100401', 'GROUP_1'),
    ('100402', 'GROUP_1'),
    ('100403', 'GROUP_2'),
    ('100404', 'GROUP_2')
ON CONFLICT (apm_id) DO NOTHING;

COMMIT;
