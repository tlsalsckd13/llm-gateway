-- v2 operational cleanup.
--
-- Preserves:
-- - Current web_users: web_users.archived_at IS NULL
-- - Current teams: teams rows and team_budget rows
-- - Current team/user budget settings for non-archived users
-- - llm_usage usage/cost records
-- - Active API keys owned by non-archived users
--
-- Removes:
-- - All audit_log rows
-- - All budget_history rows
-- - Revoked API keys
-- - Archived web_users and dependent session/token/budget/API-key rows
--
-- Run the dry-run script first:
--   psql ... -f db-maintenance/v2_cleanup_history_and_archived_users_dry_run.sql

BEGIN;

CREATE TEMP TABLE cleanup_archived_users AS
SELECT id, email
FROM web_users
WHERE archived_at IS NOT NULL;

-- Full log/history reset requested.
DELETE FROM audit_log;
DELETE FROM budget_history;

-- Remove dead key records from the Admin API Keys list.
DELETE FROM api_keys
WHERE revoked_at IS NOT NULL;

-- Remove data owned by archived/deleted users.
DELETE FROM web_sessions s
USING cleanup_archived_users u
WHERE s.user_id = u.id;

DELETE FROM magic_link_tokens m
USING cleanup_archived_users u
WHERE m.user_id = u.id;

DELETE FROM api_keys k
USING cleanup_archived_users u
WHERE lower(k.user_id) = lower(u.email);

DELETE FROM user_budget b
USING cleanup_archived_users u
WHERE lower(b.user_id) = lower(u.email);

-- Break FK references from current settings to archived users before deleting them.
UPDATE web_users wu
SET manager_user_id = NULL
FROM cleanup_archived_users u
WHERE wu.manager_user_id = u.id;

UPDATE teams t
SET created_by_user_id = NULL
FROM cleanup_archived_users u
WHERE t.created_by_user_id = u.id;

UPDATE teams t
SET updated_by_user_id = NULL
FROM cleanup_archived_users u
WHERE t.updated_by_user_id = u.id;

UPDATE dlp_policies p
SET created_by_user_id = NULL
FROM cleanup_archived_users u
WHERE p.created_by_user_id = u.id;

UPDATE dlp_policies p
SET updated_by_user_id = NULL
FROM cleanup_archived_users u
WHERE p.updated_by_user_id = u.id;

UPDATE api_keys k
SET issued_by_user_id = NULL
FROM cleanup_archived_users u
WHERE k.issued_by_user_id = u.id;

UPDATE api_keys k
SET revoked_by_user_id = NULL
FROM cleanup_archived_users u
WHERE k.revoked_by_user_id = u.id;

UPDATE magic_link_tokens m
SET issued_by_user_id = NULL
FROM cleanup_archived_users u
WHERE m.issued_by_user_id = u.id;

-- Delete archived/deleted users only. Inactive but not archived users are kept.
DELETE FROM web_users wu
USING cleanup_archived_users u
WHERE wu.id = u.id;

DROP TABLE cleanup_archived_users;

COMMIT;

SELECT 'audit_log_remaining' AS target, count(*)::bigint AS rows
FROM audit_log
UNION ALL
SELECT 'budget_history_remaining', count(*)::bigint
FROM budget_history
UNION ALL
SELECT 'revoked_api_keys_remaining', count(*)::bigint
FROM api_keys
WHERE revoked_at IS NOT NULL
UNION ALL
SELECT 'archived_web_users_remaining', count(*)::bigint
FROM web_users
WHERE archived_at IS NOT NULL
UNION ALL
SELECT 'current_web_users_kept', count(*)::bigint
FROM web_users
WHERE archived_at IS NULL
UNION ALL
SELECT 'current_teams_kept', count(*)::bigint
FROM teams
WHERE archived_at IS NULL
UNION ALL
SELECT 'team_budget_rows_kept', count(*)::bigint
FROM team_budget
UNION ALL
SELECT 'user_budget_rows_kept', count(*)::bigint
FROM user_budget;
