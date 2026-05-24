#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -x /opt/llm-gateway/refresh-aws-creds.sh ]; then
  sudo /opt/llm-gateway/refresh-aws-creds.sh
fi

set -a
[ -f /opt/llm-gateway/.aws-creds.env ] && source /opt/llm-gateway/.aws-creds.env
[ -f /opt/llm-gateway/.env ] && source /opt/llm-gateway/.env
set +a

AWS_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-ap-northeast-2}}"
ECR_REGISTRY="${ECR_REGISTRY:-061051247564.dkr.ecr.ap-northeast-2.amazonaws.com}"

aws ecr get-login-password --region "$AWS_REGION" \
  | sudo docker login --username AWS --password-stdin "$ECR_REGISTRY"

sudo -E docker compose pull
sudo -E docker compose up -d --remove-orphans
sudo docker compose ps

curl -sf http://localhost:8080/health
