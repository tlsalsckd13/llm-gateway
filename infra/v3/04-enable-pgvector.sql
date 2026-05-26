-- Run on the LLM Gateway RDS database before db-migrations/006_v3_foundation.sql
-- if the vector extension is not enabled yet.

CREATE EXTENSION IF NOT EXISTS vector;

SELECT extname, extversion
FROM pg_extension
WHERE extname = 'vector';
