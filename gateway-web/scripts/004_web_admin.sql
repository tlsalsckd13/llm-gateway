CREATE TABLE IF NOT EXISTS web_users (
    id                 SERIAL PRIMARY KEY,
    email              TEXT UNIQUE NOT NULL,
    display_name       TEXT NOT NULL,
    role               TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    team_id            TEXT NOT NULL,
    password_hash      TEXT,
    is_active          BOOLEAN DEFAULT TRUE,
    failed_login_count INT DEFAULT 0,
    locked_until       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT now(),
    last_login_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_web_users_email ON web_users(email);
CREATE INDEX IF NOT EXISTS idx_web_users_role ON web_users(role);

CREATE TABLE IF NOT EXISTS web_sessions (
    session_id TEXT PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    issued_at  TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    ip_address INET,
    user_agent TEXT,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_web_sessions_user ON web_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_web_sessions_expires ON web_sessions(expires_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGSERIAL PRIMARY KEY,
    actor_user_id INT REFERENCES web_users(id),
    actor_role    TEXT NOT NULL,
    action        TEXT NOT NULL,
    target_type   TEXT,
    target_id     TEXT,
    metadata      JSONB,
    ip_address    INET,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor_user_id);

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS issued_by_user_id INT REFERENCES web_users(id);

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS issued_via TEXT DEFAULT 'manual';
