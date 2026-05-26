#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-ap-northeast-2}"
GUARDRAIL_NAME="${GUARDRAIL_NAME:-kcs-llm-gateway-guardrail-prod}"
TMP_PAYLOAD="$(mktemp)"
trap 'rm -f "$TMP_PAYLOAD"' EXIT

cat >"$TMP_PAYLOAD" <<JSON
{
  "name": "$GUARDRAIL_NAME",
  "description": "LLM Gateway v3.0 prompt-injection and PII guardrail",
  "blockedInputMessaging": "요청이 보안 정책에 의해 차단되었습니다.",
  "blockedOutputsMessaging": "응답이 보안 정책에 의해 차단되었습니다.",
  "contentPolicyConfig": {
    "filtersConfig": [
      {
        "type": "PROMPT_ATTACK",
        "inputStrength": "HIGH",
        "outputStrength": "NONE"
      }
    ]
  },
  "sensitiveInformationPolicyConfig": {
    "piiEntitiesConfig": [
      { "type": "EMAIL", "action": "ANONYMIZE" },
      { "type": "PHONE", "action": "ANONYMIZE" },
      { "type": "NAME", "action": "ANONYMIZE" },
      { "type": "ADDRESS", "action": "ANONYMIZE" },
      { "type": "CREDIT_DEBIT_CARD_NUMBER", "action": "BLOCK" },
      { "type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCK" }
    ]
  }
}
JSON

EXISTING_ID="$(
  aws bedrock list-guardrails \
    --region "$AWS_REGION" \
    --query "guardrails[?name=='$GUARDRAIL_NAME'].id | [0]" \
    --output text 2>/dev/null || true
)"

if [ -n "$EXISTING_ID" ] && [ "$EXISTING_ID" != "None" ]; then
  GUARDRAIL_ID="$EXISTING_ID"
  GUARDRAIL_VERSION="$(
    aws bedrock get-guardrail \
      --region "$AWS_REGION" \
      --guardrail-identifier "$GUARDRAIL_ID" \
      --query 'version' \
      --output text
  )"
  echo "guardrail already exists: $GUARDRAIL_ID version=$GUARDRAIL_VERSION"
else
  CREATE_OUTPUT="$(
    aws bedrock create-guardrail \
      --region "$AWS_REGION" \
      --cli-input-json "file://$TMP_PAYLOAD"
  )"
  GUARDRAIL_ID="$(printf '%s' "$CREATE_OUTPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["guardrailId"])')"
  GUARDRAIL_VERSION="$(printf '%s' "$CREATE_OUTPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version","DRAFT"))')"
  echo "guardrail created: $GUARDRAIL_ID version=$GUARDRAIL_VERSION"
fi

cat <<SQL

-- Apply this after db-migrations/006_v3_foundation.sql:
UPDATE guardrails_config
SET bedrock_guardrail_id = '$GUARDRAIL_ID',
    bedrock_guardrail_version = '$GUARDRAIL_VERSION',
    updated_at = now()
WHERE name = 'default';
SQL

if [ -n "${DB_HOST:-}" ] && [ -n "${DB_NAME:-}" ] && [ -n "${DB_USER:-}" ] && [ -n "${DB_PASSWORD:-}" ]; then
  echo
  echo "DB_* environment variables detected; updating guardrails_config."
  PGPASSWORD="$DB_PASSWORD" psql \
    "host=$DB_HOST port=${DB_PORT:-5432} dbname=$DB_NAME user=$DB_USER sslmode=require" \
    -v "guardrail_id=$GUARDRAIL_ID" \
    -v "guardrail_version=$GUARDRAIL_VERSION" \
    -c "UPDATE guardrails_config SET bedrock_guardrail_id = :'guardrail_id', bedrock_guardrail_version = :'guardrail_version', updated_at = now() WHERE name = 'default';"
fi
