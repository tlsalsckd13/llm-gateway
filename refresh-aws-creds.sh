#!/bin/bash
set -euo pipefail

# IMDSv2 토큰 발급
TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

# IAM Role 이름 조회
ROLE=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/iam/security-credentials/)

# Role의 임시 자격증명 가져오기
CREDS=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/iam/security-credentials/$ROLE)

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
