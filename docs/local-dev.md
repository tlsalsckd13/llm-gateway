# Local Development

## Build Smoke Test

```bash
docker compose -f docker-compose.local.yml build usage-collector
```

## Optional Runtime Test

Create `.env.local` with local or development database values:

```env
DB_HOST=
DB_PORT=5432
DB_NAME=llmgateway
DB_USER=
DB_PASSWORD=
AWS_REGION=ap-northeast-2
```

Then run:

```bash
docker compose -f docker-compose.local.yml up
```

Do not commit `.env.local`.

## Python Checks

```bash
python -m py_compile usage-collector/main.py
```
