#!/usr/bin/env bash
set -euo pipefail

TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

ROLE=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/iam/security-credentials/)

CREDS=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" \
  "http://169.254.169.254/latest/meta-data/iam/security-credentials/$ROLE")

AKID=$(echo "$CREDS" | jq -r .AccessKeyId)
SAK=$(echo "$CREDS" | jq -r .SecretAccessKey)
TOK=$(echo "$CREDS" | jq -r .Token)
EXP=$(echo "$CREDS" | jq -r .Expiration)

cat > /opt/llm-gateway/.aws-creds.env <<ENV
AWS_ACCESS_KEY_ID=$AKID
AWS_SECRET_ACCESS_KEY=$SAK
AWS_SESSION_TOKEN=$TOK
AWS_REGION=ap-northeast-2
ENV

chmod 600 /opt/llm-gateway/.aws-creds.env
echo "[$(date)] credentials refreshed, expires: $EXP"
