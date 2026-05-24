-- Roll back the v2.0 foundation schema additions.
-- This keeps legacy v1.0 tables such as team_budget, user_budget, web_users, and api_keys.

BEGIN;

DROP TABLE IF EXISTS budget_history;
DROP TABLE IF EXISTS magic_link_tokens;
DROP TABLE IF EXISTS dlp_policies;

ALTER TABLE IF EXISTS api_keys
  DROP COLUMN IF EXISTS key_prefix,
  DROP COLUMN IF EXISTS scopes,
  DROP COLUMN IF EXISTS revoked_by_user_id,
  DROP COLUMN IF EXISTS last_used_at,
  DROP COLUMN IF EXISTS expires_at;

ALTER TABLE IF EXISTS web_users
  DROP COLUMN IF EXISTS locked_until,
  DROP COLUMN IF EXISTS failed_login_count,
  DROP COLUMN IF EXISTS last_password_changed_at,
  DROP COLUMN IF EXISTS invited_at,
  DROP COLUMN IF EXISTS archived_at,
  DROP COLUMN IF EXISTS manager_user_id,
  DROP COLUMN IF EXISTS hire_date,
  DROP COLUMN IF EXISTS department,
  DROP COLUMN IF EXISTS team_id_fk;

DROP TABLE IF EXISTS teams;

COMMIT;
