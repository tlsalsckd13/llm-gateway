ALTER TABLE api_keys DROP COLUMN IF EXISTS issued_via;
ALTER TABLE api_keys DROP COLUMN IF EXISTS issued_by_user_id;

DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS web_sessions;
DROP TABLE IF EXISTS web_users;
