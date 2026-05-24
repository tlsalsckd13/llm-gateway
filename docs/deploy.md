# Deployment

## Normal Deployment

1. Create a feature branch.
2. Commit code changes.
3. Open a pull request to `main`.
4. Check CI results.
5. Merge to `main`.
6. GitHub Actions deploys to EC2 automatically.

## Manual Deployment

Use GitHub Actions:

```text
Actions -> Deploy to EC2 -> Run workflow
```

Optionally provide a specific commit SHA.

## Rollback

Use Git history as the rollback source:

```bash
git revert <bad-sha>
git push origin main
```

The revert commit triggers a new deployment.

## Database Migrations

Migrations are not applied automatically.

1. Add `db-migrations/00X_name.sql`.
2. Add a matching rollback file under `db-migrations/rollback/`.
3. Open a pull request.
4. Apply the migration manually from an operator shell.

Example:

```bash
set -a
source /opt/llm-gateway/.env
set +a

DB_DSN="host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USER sslmode=require"
PGPASSWORD="$DB_PASSWORD" psql "$DB_DSN" -f db-migrations/00X_name.sql
```
