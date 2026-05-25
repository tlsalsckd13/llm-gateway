-- Dry-run for v2 operational cleanup.
-- Shows what the cleanup script will remove while preserving current Users,
-- Teams, and Budget settings.

WITH archived_users AS (
    SELECT id, email
    FROM web_users
    WHERE archived_at IS NOT NULL
)
SELECT 'audit_log' AS target, count(*)::bigint AS rows
FROM audit_log
UNION ALL
SELECT 'budget_history', count(*)::bigint
FROM budget_history
UNION ALL
SELECT 'revoked_api_keys', count(*)::bigint
FROM api_keys
WHERE revoked_at IS NOT NULL
UNION ALL
SELECT 'archived_web_users', count(*)::bigint
FROM archived_users
UNION ALL
SELECT 'archived_user_sessions', count(*)::bigint
FROM web_sessions s
JOIN archived_users u ON u.id = s.user_id
UNION ALL
SELECT 'archived_user_magic_links', count(*)::bigint
FROM magic_link_tokens m
JOIN archived_users u ON u.id = m.user_id
UNION ALL
SELECT 'archived_user_api_keys', count(*)::bigint
FROM api_keys k
JOIN archived_users u ON lower(u.email) = lower(k.user_id)
UNION ALL
SELECT 'archived_user_budget_overrides', count(*)::bigint
FROM user_budget b
JOIN archived_users u ON lower(u.email) = lower(b.user_id)
UNION ALL
SELECT 'current_active_web_users_kept', count(*)::bigint
FROM web_users
WHERE archived_at IS NULL
UNION ALL
SELECT 'current_teams_kept', count(*)::bigint
FROM teams
WHERE archived_at IS NULL
UNION ALL
SELECT 'current_team_budget_rows_kept', count(*)::bigint
FROM team_budget;
