-- DESTRUCTIVE DATA NORMALIZATION FOR THE CLOUD SQL POC.
--
-- This file does not drop or alter any database object. It permanently clears
-- Journey data and replaces all group-authorization rows with the canonical PoC
-- dataset used by the application:
--
--   GROUP_1: sam, ivan, adi  -> 100401, 100402
--   GROUP_2: abdur, ajir     -> 100403, 100404
--
-- Stop every agent connected to this database before running the file.
-- Execute with psql and ON_ERROR_STOP=1 so a failure cannot commit partial data.

BEGIN;

TRUNCATE TABLE
    journey_events,
    journey_operations,
    journeys,
    access_group_members,
    apm_group_assignments,
    access_groups,
    apm_group_access
RESTART IDENTITY;

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

-- Compatibility table currently queried by cloud_journey.models.ApmGroupAccess.
INSERT INTO apm_group_access (apm_id, group_name)
VALUES
    ('100401', 'GROUP_1'),
    ('100402', 'GROUP_1'),
    ('100403', 'GROUP_2'),
    ('100404', 'GROUP_2');

COMMIT;

SELECT id, name
FROM access_groups
ORDER BY id;

SELECT group_id, user_subject
FROM access_group_members
ORDER BY group_id, user_subject;

SELECT apm_id, group_id
FROM apm_group_assignments
ORDER BY apm_id;

SELECT apm_id, group_name
FROM apm_group_access
ORDER BY apm_id;
