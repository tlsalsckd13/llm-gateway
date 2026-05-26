-- Roll back LLM Gateway v3.0 foundation schema to the v2.0-compatible shape.
-- Run only after confirming no v3 data needs to be retained.

BEGIN;

ALTER TABLE IF EXISTS skills
  DROP CONSTRAINT IF EXISTS fk_skills_latest_version;

DROP TABLE IF EXISTS eval_runs;
DROP TABLE IF EXISTS eval_prompts;
DROP TABLE IF EXISTS eval_prompt_sets;
DROP TABLE IF EXISTS guardrails_events;
DROP TABLE IF EXISTS guardrails_config;
DROP TABLE IF EXISTS mcp_call_log;
DROP TABLE IF EXISTS mcp_activations;
DROP TABLE IF EXISTS mcp_servers;
DROP TABLE IF EXISTS kb_chunks;
DROP TABLE IF EXISTS kb_documents;
DROP TABLE IF EXISTS knowledge_bases;
DROP TABLE IF EXISTS skill_activations;
DROP TABLE IF EXISTS skill_versions;
DROP TABLE IF EXISTS skills;
DROP TABLE IF EXISTS user_preferences;
DROP TABLE IF EXISTS agent_policies;

ALTER TABLE IF EXISTS api_keys
  ALTER COLUMN scopes SET DEFAULT '{"models":["*"]}'::jsonb;

UPDATE api_keys
SET scopes = COALESCE(scopes, '{}'::jsonb) - 'skills' - 'kb' - 'mcp' - 'guardrails_bypass';

UPDATE web_users
SET role = 'user'
WHERE role = 'team_owner';

ALTER TABLE IF EXISTS web_users
  DROP CONSTRAINT IF EXISTS web_users_role_check;

ALTER TABLE IF EXISTS web_users
  ADD CONSTRAINT web_users_role_check CHECK (role IN ('admin', 'user'));

DROP EXTENSION IF EXISTS vector;

COMMIT;
