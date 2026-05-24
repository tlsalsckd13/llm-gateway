# LLM Gateway

KCS LLM Gateway PoC repository.

This service exposes Anthropic-compatible endpoints for Claude Code and routes requests through the EC2 gateway to AWS Bedrock. It also records usage, cost, budget status, and DLP decisions in PostgreSQL.

## Components

- `usage-collector`: FastAPI gateway adapter for Claude Code, DLP, budget checks, Bedrock calls, and usage logging.
- `gateway`: Portkey OSS gateway container used by the OpenAI-compatible internal path.
- `gateway-web`: Placeholder for the Admin/User web UI that will be added in the frontend phase.
- `db-migrations`: SQL migrations. They are reviewed in Git but applied manually.
- `.github/workflows`: CI and deployment workflows.

## Deployment

`main` is the deploy branch.

1. Push to `main`.
2. GitHub Actions assumes the AWS CI/CD role through OIDC.
3. The workflow builds and pushes the Docker image to ECR.
4. The workflow runs SSM Run Command on the EC2 instance.
5. EC2 pulls the image and runs `docker compose up -d`.

No static AWS access key is used for CI/CD.

## Local Build

For a local Docker build smoke test:

```bash
docker compose -f docker-compose.local.yml build usage-collector
```

Running the full app locally requires a `.env.local` file with database settings. Do not commit `.env.local`.

## Secrets

Never commit:

- `.env`
- `.aws-creds.env`
- API keys
- private keys or certificates
- database passwords

Before pushing to the public repository, run:

```bash
# Use a secret scanner or grep for API keys, AWS keys, private keys,
# database passwords, and session secrets before every public push.
```
