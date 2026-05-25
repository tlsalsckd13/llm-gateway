-- One-time cleanup for usage aggregates shown on Admin > Usage.
--
-- Preserves:
-- - web_users / teams / current budget settings
-- - active API keys and access controls
--
-- Deletes:
-- - llm_usage records used for Top Users, usage charts, and consumed budget totals

BEGIN;

DELETE FROM llm_usage;

COMMIT;

SELECT COUNT(*) AS llm_usage_remaining FROM llm_usage;
