-- DESTRUCTIVE DATA REFRESH FOR THE CLOUD SQL POC.
--
-- This preserves the migrated schema but permanently removes every row from:
--   journeys, journey_events, journey_operations, and apm_group_access.
-- Run this only against the intended PoC database while the agent is stopped.
-- Execute with psql's ON_ERROR_STOP enabled so any error prevents a partial run.

BEGIN;

TRUNCATE TABLE
    journey_events,
    journey_operations,
    journeys,
    apm_group_access
RESTART IDENTITY;

INSERT INTO apm_group_access (apm_id, group_name)
VALUES
    ('100501', 'GROUP_1'),
    ('100502', 'GROUP_1'),
    ('100503', 'GROUP_2'),
    ('100504', 'GROUP_2');

COMMIT;

SELECT
    group_name,
    string_agg(apm_id, ', ' ORDER BY apm_id) AS apm_ids
FROM apm_group_access
GROUP BY group_name
ORDER BY group_name;
