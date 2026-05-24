-- LLM Gateway v2.0 foundation schema.
-- Apply manually before deploying code that depends on these objects.

BEGIN;

CREATE TABLE IF NOT EXISTS teams (
    id                 SERIAL PRIMARY KEY,
    team_key           TEXT UNIQUE NOT NULL,
    name               TEXT NOT NULL,
    description        TEXT,
    default_model      TEXT NOT NULL DEFAULT 'global.anthropic.claude-opus-4-7',
    monthly_limit_usd  NUMERIC(10, 4) NOT NULL DEFAULT 0,
    daily_limit_usd    NUMERIC(10, 4) NOT NULL DEFAULT 0,
    alert_threshold_pct INT NOT NULL DEFAULT 80 CHECK (alert_threshold_pct BETWEEN 1 AND 100),
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    archived_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id INT REFERENCES web_users(id),
    updated_by_user_id INT REFERENCES web_users(id)
);

CREATE INDEX IF NOT EXISTS idx_teams_team_key ON teams(team_key);
CREATE INDEX IF NOT EXISTS idx_teams_active ON teams(is_active) WHERE is_active = TRUE;

INSERT INTO teams (team_key, name, monthly_limit_usd, daily_limit_usd)
SELECT team_id, team_id, monthly_limit_usd, COALESCE(daily_limit_usd, 0)
FROM team_budget
ON CONFLICT (team_key) DO NOTHING;

ALTER TABLE IF EXISTS web_users
  ADD COLUMN IF NOT EXISTS team_id_fk INT REFERENCES teams(id),
  ADD COLUMN IF NOT EXISTS department TEXT,
  ADD COLUMN IF NOT EXISTS hire_date DATE,
  ADD COLUMN IF NOT EXISTS manager_user_id INT REFERENCES web_users(id),
  ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS invited_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_password_changed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS failed_login_count INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;

UPDATE web_users wu
SET team_id_fk = t.id
FROM teams t
WHERE wu.team_id = t.team_key
  AND wu.team_id_fk IS NULL;

ALTER TABLE IF EXISTS api_keys
  ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS revoked_by_user_id INT REFERENCES web_users(id),
  ADD COLUMN IF NOT EXISTS scopes JSONB DEFAULT '{"models": ["*"]}'::jsonb,
  ADD COLUMN IF NOT EXISTS key_prefix TEXT;

UPDATE api_keys
SET key_prefix = 'sha256:' || substring(key_hash from 1 for 12)
WHERE key_prefix IS NULL;

CREATE TABLE IF NOT EXISTS dlp_policies (
    id                 SERIAL PRIMARY KEY,
    name               TEXT NOT NULL,
    pattern_type       TEXT NOT NULL,
    pattern_regex      TEXT NOT NULL,
    redaction_token    TEXT NOT NULL,
    action             TEXT NOT NULL CHECK (action IN ('block', 'mask', 'block_and_mask')),
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    priority           INT NOT NULL DEFAULT 100,
    description        TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id INT REFERENCES web_users(id),
    updated_by_user_id INT REFERENCES web_users(id)
);

CREATE INDEX IF NOT EXISTS idx_dlp_active_priority ON dlp_policies(priority) WHERE is_active = TRUE;

INSERT INTO dlp_policies (name, pattern_type, pattern_regex, redaction_token, action, priority, description)
SELECT *
FROM (
    VALUES
      ('주민등록번호 (KRN)', 'krn', '\b\d{6}[-\s]?[1-8]\d{6}\b', '[REDACTED-KRN]', 'block_and_mask', 10, '주민/외국인 등록번호'),
      ('카드번호 (CARD)', 'card', '\b(?:\d{4}[- ]?){3}\d{4}\b', '[REDACTED-CARD]', 'block_and_mask', 20, '14~16자리 카드번호'),
      ('사업자번호 (BRN)', 'brn', '\b\d{3}-\d{2}-\d{5}\b', '[REDACTED-BRN]', 'block_and_mask', 30, '한국 사업자등록번호 표준 표기'),
      ('계좌번호 (ACCOUNT)', 'account', '\b\d{3,6}-\d{2,6}-\d{4,8}\b', '[REDACTED-ACCOUNT]', 'block_and_mask', 40, '일반 은행 계좌번호 패턴')
) AS seed(name, pattern_type, pattern_regex, redaction_token, action, priority, description)
WHERE NOT EXISTS (
    SELECT 1
    FROM dlp_policies existing
    WHERE existing.pattern_type = seed.pattern_type
      AND existing.pattern_regex = seed.pattern_regex
);

CREATE TABLE IF NOT EXISTS magic_link_tokens (
    token_hash        TEXT PRIMARY KEY,
    user_id           INT NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    purpose           TEXT NOT NULL CHECK (purpose IN ('invite', 'password_reset')),
    issued_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    consumed_at       TIMESTAMPTZ,
    issued_by_user_id INT REFERENCES web_users(id)
);

CREATE INDEX IF NOT EXISTS idx_magic_link_user ON magic_link_tokens(user_id) WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS budget_history (
    id                 BIGSERIAL PRIMARY KEY,
    scope              TEXT NOT NULL CHECK (scope IN ('team', 'user')),
    scope_id           TEXT NOT NULL,
    field              TEXT NOT NULL,
    old_value          NUMERIC(12, 4),
    new_value          NUMERIC(12, 4),
    changed_by_user_id INT REFERENCES web_users(id),
    changed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason             TEXT
);

CREATE INDEX IF NOT EXISTS idx_budget_history_scope ON budget_history(scope, scope_id, changed_at DESC);

COMMIT;
