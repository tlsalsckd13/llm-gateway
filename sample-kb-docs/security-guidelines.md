# Security Guidelines

API Key는 `ai-poc-` prefix를 사용한다.

DLP 정책은 주민등록번호, 카드번호, 사업자번호, 계좌번호를 차단하거나 마스킹한다.

MCP credential은 AWS Secrets Manager에 저장하고 DB에는 ARN만 보관한다.
