# LLM Gateway v3.0 Foundation Infra

이 폴더는 v3.0 Phase 0에서 필요한 AWS/RDS 준비물을 재현 가능한 스크립트로 모아둔 곳입니다.

## 실행 순서

1. S3 버킷 생성

```bash
AWS_REGION=ap-northeast-2 ./infra/v3/01-create-s3-buckets.sh
```

2. EC2 IAM role에 inline policy 추가

```bash
aws iam put-role-policy \
  --role-name ai-poc-gateway-role \
  --policy-name llm-gateway-v3-foundation \
  --policy-document file://infra/v3/02-iam-policy.json
```

3. Titan Embeddings 모델 접근 권한 확인

`infra/v3/05-request-bedrock-model-access.md`를 참고합니다.

4. pgvector 활성화

```bash
PGPASSWORD="$DB_PASSWORD" psql \
  "host=$DB_HOST port=${DB_PORT:-5432} dbname=$DB_NAME user=$DB_USER sslmode=require" \
  -f infra/v3/04-enable-pgvector.sql
```

5. v3 DB migration 적용

```bash
PGPASSWORD="$DB_PASSWORD" psql \
  "host=$DB_HOST port=${DB_PORT:-5432} dbname=$DB_NAME user=$DB_USER sslmode=require" \
  -f db-migrations/006_v3_foundation.sql
```

6. Bedrock Guardrail 생성 및 DB 반영

```bash
AWS_REGION=ap-northeast-2 ./infra/v3/03-create-bedrock-guardrail.sh
```

`DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`가 설정돼 있으면 스크립트가 `guardrails_config`를 직접 업데이트합니다. DB 환경 변수가 없으면 출력되는 `UPDATE` SQL을 복사해 운영 DB에 적용합니다.

## 검증 명령

```bash
aws s3api get-bucket-versioning --bucket kcs-llm-gateway-skills-prod
aws s3api get-public-access-block --bucket kcs-llm-gateway-kb-prod

PGPASSWORD="$DB_PASSWORD" psql \
  "host=$DB_HOST port=${DB_PORT:-5432} dbname=$DB_NAME user=$DB_USER sslmode=require" \
  -c "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

## 롤백

DB 롤백:

```bash
PGPASSWORD="$DB_PASSWORD" psql \
  "host=$DB_HOST port=${DB_PORT:-5432} dbname=$DB_NAME user=$DB_USER sslmode=require" \
  -f db-migrations/rollback/006_v3_foundation_rollback.sql
```

IAM 롤백:

```bash
aws iam delete-role-policy \
  --role-name ai-poc-gateway-role \
  --policy-name llm-gateway-v3-foundation
```

S3 버킷 삭제는 저장된 Skills/KB 원본 보존 정책과 충돌할 수 있으므로 운영자가 명시적으로 백업/비우기 후 수행합니다.
