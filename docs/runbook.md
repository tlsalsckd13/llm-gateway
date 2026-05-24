# Operations Runbook

## EC2 Restart Recovery

```bash
sudo /opt/llm-gateway/refresh-aws-creds.sh

set -a
source /opt/llm-gateway/.aws-creds.env
source /opt/llm-gateway/.env
set +a

cd /opt/llm-gateway
sudo -E docker compose up -d
sudo docker compose ps
curl -sS http://127.0.0.1:8080/health
```

## Logs

```bash
cd /opt/llm-gateway
sudo -E docker compose logs usage-collector --tail=100
```

## Deployment Failure

1. Check GitHub Actions logs.
2. Find the SSM Command ID in the deploy log.
3. Check AWS Systems Manager Run Command output.
4. Check EC2 container logs.

## Health Check

```bash
curl -sS http://127.0.0.1:8080/health
```
