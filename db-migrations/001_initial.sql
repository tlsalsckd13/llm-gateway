-- Current PoC baseline schema, reconstructed from the running collector contract.
-- Apply manually only after checking the target database state.

CREATE TABLE IF NOT EXISTS llm_usage (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ DEFAULT now(),
    user_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    skill TEXT,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    cost_usd NUMERIC(12, 6) DEFAULT 0,
    request_id TEXT,
    blocked_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_user ON llm_usage(user_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_team ON llm_usage(team_id);

CREATE TABLE IF NOT EXISTS team_budget (
    team_id TEXT PRIMARY KEY,
    monthly_limit_usd NUMERIC(10, 2) NOT NULL,
    daily_limit_usd NUMERIC(10, 2),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_budget (
    user_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    monthly_limit_usd NUMERIC(10, 2),
    updated_at TIMESTAMPTZ DEFAULT now()
);
