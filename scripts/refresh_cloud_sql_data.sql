-- DESTRUCTIVE DATA REFRESH FOR THE CLOUD SQL POC.
--
-- This preserves the migrated schema but permanently removes Journey and
-- normalized authorization rows, then reloads the canonical PoC data.
-- Run this only against the intended PoC database while the agent is stopped.
-- Execute with psql's ON_ERROR_STOP enabled so any error prevents a partial run.

BEGIN;

TRUNCATE TABLE
    journey_events,
    journey_operations,
    journeys,
    access_group_members,
    apm_group_assignments,
    access_groups
RESTART IDENTITY;

DO $$
BEGIN
    IF TO_REGCLASS('public.apm_group_access') IS NOT NULL THEN
        EXECUTE 'TRUNCATE TABLE public.apm_group_access';
    END IF;
END $$;

INSERT INTO access_groups (id, name)
VALUES
    ('GROUP_1', 'Cloud Journey Group 1'),
    ('GROUP_2', 'Cloud Journey Group 2');

INSERT INTO access_group_members (group_id, user_subject)
VALUES
    ('GROUP_1', 'sam'),
    ('GROUP_1', 'ivan'),
    ('GROUP_1', 'adi'),
    ('GROUP_2', 'abdur'),
    ('GROUP_2', 'ajir');

INSERT INTO apm_group_assignments (apm_id, group_id)
VALUES
    ('100401', 'GROUP_1'),
    ('100402', 'GROUP_1'),
    ('100403', 'GROUP_2'),
    ('100404', 'GROUP_2');

COMMIT;

SELECT
    group_id,
    string_agg(apm_id, ', ' ORDER BY apm_id) AS apm_ids
FROM apm_group_assignments
GROUP BY group_id
ORDER BY group_id;
