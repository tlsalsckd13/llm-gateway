#!/usr/bin/env bash
set -euo pipefail

sudo /opt/llm-gateway/refresh-aws-creds.sh

set -a
source /opt/llm-gateway/.aws-creds.env
source /opt/llm-gateway/.env
set +a

cd /opt/llm-gateway
sudo -E docker compose up -d
sudo docker compose ps
curl -sS http://127.0.0.1:8080/health
