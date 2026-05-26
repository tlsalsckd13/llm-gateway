#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-ap-northeast-2}"
SKILLS_BUCKET="${SKILLS_BUCKET:-kcs-llm-gateway-skills-prod}"
KB_BUCKET="${KB_BUCKET:-kcs-llm-gateway-kb-prod}"

create_bucket() {
  local bucket="$1"

  if aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    echo "bucket already exists: $bucket"
  else
    echo "creating bucket: $bucket"
    if [ "$AWS_REGION" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$bucket" --region "$AWS_REGION"
    else
      aws s3api create-bucket \
        --bucket "$bucket" \
        --region "$AWS_REGION" \
        --create-bucket-configuration "LocationConstraint=$AWS_REGION"
    fi
  fi

  aws s3api put-public-access-block \
    --bucket "$bucket" \
    --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

  aws s3api put-bucket-versioning \
    --bucket "$bucket" \
    --versioning-configuration Status=Enabled

  aws s3api put-bucket-encryption \
    --bucket "$bucket" \
    --server-side-encryption-configuration \
      '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

  aws s3api put-bucket-tagging \
    --bucket "$bucket" \
    --tagging 'TagSet=[{Key=project,Value=llm-gateway},{Key=phase,Value=v3-foundation}]'
}

create_bucket "$SKILLS_BUCKET"
create_bucket "$KB_BUCKET"

echo "S3 v3 foundation buckets are ready."
