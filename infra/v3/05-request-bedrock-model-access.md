# Bedrock Model Access: Titan Text Embeddings v2

v3.0 Knowledge Base는 Bedrock Knowledge Bases/OpenSearch를 쓰지 않고, 기존 RDS pgvector와 `amazon.titan-embed-text-v2:0` 임베딩 모델만 사용합니다.

## 확인/요청 순서

1. AWS Console에서 `ap-northeast-2` 리전을 선택합니다.
2. Amazon Bedrock > Model access로 이동합니다.
3. `Amazon` 제공자에서 `Titan Text Embeddings V2` 모델 접근 권한을 확인합니다.
4. 접근 권한이 없으면 request를 제출합니다.
5. 승인 후 EC2 또는 로컬 AWS CLI에서 아래 명령으로 확인합니다.

```bash
aws bedrock-runtime invoke-model \
  --region ap-northeast-2 \
  --model-id amazon.titan-embed-text-v2:0 \
  --content-type application/json \
  --accept application/json \
  --body '{"inputText":"테스트 임베딩 요청","dimensions":1024,"normalize":true}' \
  /tmp/titan-embed-response.json

cat /tmp/titan-embed-response.json
```

정상 응답에는 1024차원 `embedding` 배열이 포함됩니다.
