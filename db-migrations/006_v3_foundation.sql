-- LLM Gateway v3.0 foundation schema.
-- Apply manually before deploying code that depends on Skills, KB, MCP, Guardrails, or Eval.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE IF EXISTS web_users
  DROP CONSTRAINT IF EXISTS web_users_role_check;

ALTER TABLE IF EXISTS web_users
  ADD CONSTRAINT web_users_role_check CHECK (role IN ('admin', 'team_owner', 'user'));

CREATE TABLE IF NOT EXISTS agent_policies (
    id                 SERIAL PRIMARY KEY,
    scope              TEXT NOT NULL CHECK (scope IN ('org', 'team')),
    team_id            INT REFERENCES teams(id),
    title              TEXT NOT NULL,
    body               TEXT NOT NULL,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    version            INT NOT NULL DEFAULT 1,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id INT REFERENCES web_users(id),
    updated_by_user_id INT REFERENCES web_users(id),
    CONSTRAINT chk_agent_policy_scope CHECK (
      (scope = 'org' AND team_id IS NULL) OR
      (scope = 'team' AND team_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_policies_org
  ON agent_policies(scope)
  WHERE scope = 'org' AND is_active = TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_policies_team
  ON agent_policies(team_id)
  WHERE scope = 'team' AND is_active = TRUE;

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id                 INT PRIMARY KEY REFERENCES web_users(id) ON DELETE CASCADE,
    system_prompt           TEXT,
    default_kb_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    default_skills_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
    default_mcp_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS skills (
    id                 SERIAL PRIMARY KEY,
    slug               TEXT NOT NULL,
    name               TEXT NOT NULL,
    description        TEXT NOT NULL,
    owner_scope        TEXT NOT NULL CHECK (owner_scope IN ('org', 'team', 'user')),
    owner_team_id      INT REFERENCES teams(id),
    owner_user_id      INT REFERENCES web_users(id),
    latest_version_id  INT,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    status             TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    archived_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id INT REFERENCES web_users(id),
    CONSTRAINT chk_skill_owner_consistency CHECK (
      (owner_scope = 'org' AND owner_team_id IS NULL AND owner_user_id IS NULL) OR
      (owner_scope = 'team' AND owner_team_id IS NOT NULL AND owner_user_id IS NULL) OR
      (owner_scope = 'user' AND owner_team_id IS NULL AND owner_user_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_skills_slug_scope
  ON skills(slug, owner_scope, COALESCE(owner_team_id, 0), COALESCE(owner_user_id, 0));

CREATE INDEX IF NOT EXISTS idx_skills_status_scope ON skills(status, owner_scope);
CREATE INDEX IF NOT EXISTS idx_skills_active ON skills(is_active) WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS skill_versions (
    id                  SERIAL PRIMARY KEY,
    skill_id            INT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    version             TEXT NOT NULL,
    s3_key              TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    frontmatter         JSONB NOT NULL,
    body_excerpt        TEXT,
    uploaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    uploaded_by_user_id INT REFERENCES web_users(id),
    UNIQUE (skill_id, version)
);

DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM pg_constraint WHERE conname = 'fk_skills_latest_version'
  ) THEN
    ALTER TABLE skills
      ADD CONSTRAINT fk_skills_latest_version
      FOREIGN KEY (latest_version_id) REFERENCES skill_versions(id);
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS skill_activations (
    id              SERIAL PRIMARY KEY,
    skill_id        INT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    subject_scope   TEXT NOT NULL CHECK (subject_scope IN ('team','user')),
    subject_team_id INT REFERENCES teams(id),
    subject_user_id INT REFERENCES web_users(id),
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    activated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_skill_activation_subject CHECK (
      (subject_scope = 'team' AND subject_team_id IS NOT NULL AND subject_user_id IS NULL) OR
      (subject_scope = 'user' AND subject_team_id IS NULL AND subject_user_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_activations_subject
  ON skill_activations(skill_id, subject_scope, COALESCE(subject_team_id, 0), COALESCE(subject_user_id, 0));

CREATE TABLE IF NOT EXISTS knowledge_bases (
    id                SERIAL PRIMARY KEY,
    team_id           INT NOT NULL UNIQUE REFERENCES teams(id),
    name              TEXT NOT NULL,
    embedding_model   TEXT NOT NULL DEFAULT 'amazon.titan-embed-text-v2:0',
    embedding_dim     INT NOT NULL DEFAULT 1024,
    chunk_size        INT NOT NULL DEFAULT 800,
    chunk_overlap     INT NOT NULL DEFAULT 100,
    top_k_default     INT NOT NULL DEFAULT 5,
    s3_prefix         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','degraded','archived')),
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    last_ingested_at  TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_bases_active ON knowledge_bases(is_active) WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS kb_documents (
    id                       SERIAL PRIMARY KEY,
    kb_id                    INT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    team_id                  INT NOT NULL REFERENCES teams(id),
    s3_key                   TEXT NOT NULL,
    mime                     TEXT NOT NULL,
    sha256                   TEXT NOT NULL,
    size_bytes               BIGINT NOT NULL,
    title                    TEXT,
    tags                     TEXT[],
    uploaded_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    uploaded_by_user_id      INT REFERENCES web_users(id),
    ingestion_status         TEXT NOT NULL DEFAULT 'pending' CHECK (ingestion_status IN ('pending','running','succeeded','failed','removed')),
    ingestion_error          TEXT,
    chunk_count              INT,
    embedding_token_cost_usd NUMERIC(10,6),
    ingested_at              TIMESTAMPTZ,
    removed_at               TIMESTAMPTZ,
    UNIQUE (kb_id, sha256)
);

CREATE INDEX IF NOT EXISTS idx_kb_docs_kb_status ON kb_documents(kb_id, ingestion_status);
CREATE INDEX IF NOT EXISTS idx_kb_docs_team ON kb_documents(team_id);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id           BIGSERIAL PRIMARY KEY,
    document_id  INT NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    kb_id        INT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    team_id      INT NOT NULL REFERENCES teams(id),
    chunk_index  INT NOT NULL,
    content      TEXT NOT NULL,
    token_count  INT,
    embedding    vector(1024) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding
  ON kb_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_team ON kb_chunks(team_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_kb ON kb_chunks(kb_id);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id                 SERIAL PRIMARY KEY,
    slug               TEXT UNIQUE NOT NULL,
    name               TEXT NOT NULL,
    description        TEXT,
    transport          TEXT NOT NULL CHECK (transport IN ('stdio','sse','http')),
    endpoint           TEXT,
    secret_arn         TEXT,
    owner_scope        TEXT NOT NULL CHECK (owner_scope IN ('org','team')),
    owner_team_id      INT REFERENCES teams(id),
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    tool_cache         JSONB,
    tool_cache_at      TIMESTAMPTZ,
    status             TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','degraded')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id INT REFERENCES web_users(id),
    CONSTRAINT chk_mcp_owner CHECK (
      (owner_scope = 'org' AND owner_team_id IS NULL) OR
      (owner_scope = 'team' AND owner_team_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_mcp_servers_status ON mcp_servers(status, owner_scope);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_active ON mcp_servers(is_active) WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS mcp_activations (
    id              SERIAL PRIMARY KEY,
    mcp_server_id   INT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    subject_scope   TEXT NOT NULL CHECK (subject_scope IN ('team','user')),
    subject_team_id INT REFERENCES teams(id),
    subject_user_id INT REFERENCES web_users(id),
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    activated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_mcp_activation_subject CHECK (
      (subject_scope = 'team' AND subject_team_id IS NOT NULL AND subject_user_id IS NULL) OR
      (subject_scope = 'user' AND subject_team_id IS NULL AND subject_user_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_activations_subject
  ON mcp_activations(mcp_server_id, subject_scope, COALESCE(subject_team_id, 0), COALESCE(subject_user_id, 0));

CREATE TABLE IF NOT EXISTS mcp_call_log (
    id               BIGSERIAL PRIMARY KEY,
    request_id       TEXT NOT NULL,
    user_id          INT REFERENCES web_users(id),
    mcp_server_id    INT REFERENCES mcp_servers(id),
    tool_name        TEXT NOT NULL,
    arguments_sha256 TEXT NOT NULL,
    response_status  TEXT NOT NULL,
    latency_ms       INT,
    error_message    TEXT,
    called_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mcp_call_log_user_time ON mcp_call_log(user_id, called_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_request ON mcp_call_log(request_id);

CREATE TABLE IF NOT EXISTS guardrails_config (
    id                         SERIAL PRIMARY KEY,
    name                       TEXT UNIQUE NOT NULL DEFAULT 'default',
    bedrock_guardrail_id       TEXT,
    bedrock_guardrail_version  TEXT,
    enabled_input              BOOLEAN NOT NULL DEFAULT TRUE,
    enabled_output             BOOLEAN NOT NULL DEFAULT TRUE,
    block_on_input_violation   BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by_user_id         INT REFERENCES web_users(id)
);

CREATE TABLE IF NOT EXISTS guardrails_events (
    id              BIGSERIAL PRIMARY KEY,
    request_id      TEXT NOT NULL,
    user_id         INT REFERENCES web_users(id),
    side            TEXT NOT NULL CHECK (side IN ('input','output')),
    violation_types TEXT[],
    action          TEXT NOT NULL CHECK (action IN ('blocked','redacted','warned')),
    input_sha256    TEXT,
    output_sha256   TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_guardrails_events_user_time ON guardrails_events(user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_guardrails_events_request ON guardrails_events(request_id);

CREATE TABLE IF NOT EXISTS eval_prompt_sets (
    id                 SERIAL PRIMARY KEY,
    name               TEXT NOT NULL,
    description        TEXT,
    created_by_user_id INT REFERENCES web_users(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval_prompts (
    id                SERIAL PRIMARY KEY,
    set_id            INT NOT NULL REFERENCES eval_prompt_sets(id) ON DELETE CASCADE,
    prompt            TEXT NOT NULL,
    expected_keywords TEXT[],
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id               BIGSERIAL PRIMARY KEY,
    set_id           INT NOT NULL REFERENCES eval_prompt_sets(id),
    config_label     TEXT NOT NULL,
    model            TEXT NOT NULL,
    skills_used      TEXT[],
    kb_used          BOOLEAN NOT NULL DEFAULT FALSE,
    mcp_used         TEXT[],
    response         TEXT,
    latency_ms       INT,
    cost_usd         NUMERIC(12,6),
    keyword_hit_rate NUMERIC(5,2),
    ran_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    ran_by_user_id   INT REFERENCES web_users(id)
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_set ON eval_runs(set_id, ran_at DESC);

ALTER TABLE IF EXISTS api_keys
  ALTER COLUMN scopes SET DEFAULT '{"models":["*"],"skills":true,"kb":true,"mcp":[],"guardrails_bypass":false}'::jsonb;

UPDATE api_keys
SET scopes = COALESCE(scopes, '{}'::jsonb)
          || CASE WHEN scopes IS NULL OR NOT scopes ? 'models' THEN '{"models":["*"]}'::jsonb ELSE '{}'::jsonb END
          || CASE WHEN scopes IS NULL OR NOT scopes ? 'skills' THEN '{"skills":true}'::jsonb ELSE '{}'::jsonb END
          || CASE WHEN scopes IS NULL OR NOT scopes ? 'kb' THEN '{"kb":true}'::jsonb ELSE '{}'::jsonb END
          || CASE WHEN scopes IS NULL OR NOT scopes ? 'mcp' THEN '{"mcp":[]}'::jsonb ELSE '{}'::jsonb END
          || CASE WHEN scopes IS NULL OR NOT scopes ? 'guardrails_bypass' THEN '{"guardrails_bypass":false}'::jsonb ELSE '{}'::jsonb END
WHERE scopes IS NULL
   OR NOT scopes ? 'models'
   OR NOT scopes ? 'skills'
   OR NOT scopes ? 'kb'
   OR NOT scopes ? 'mcp'
   OR NOT scopes ? 'guardrails_bypass';

INSERT INTO agent_policies (scope, team_id, title, body, created_by_user_id, updated_by_user_id)
SELECT 'org',
       NULL,
       'KCS 조직 기본 정책',
       $$당신은 KCS(한국평가정보) 임직원의 업무 보조 AI입니다.
- 답변은 한국어를 우선합니다.
- 사내 보안 규정: 고객 개인정보, 신용평가 결과, 미공시 재무정보를 외부에 노출하지 않습니다.
- 코드 작성 시 신용평가/금융 도메인 컴플라이언스를 고려합니다.
- 불확실한 경우 추측하지 않고 "확인 필요"라고 답합니다.$$,
       NULL,
       NULL
WHERE NOT EXISTS (
    SELECT 1 FROM agent_policies
    WHERE scope = 'org' AND is_active = TRUE
);

INSERT INTO guardrails_config (name)
VALUES ('default')
ON CONFLICT (name) DO NOTHING;

COMMIT;
