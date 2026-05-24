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

sudo mkdir -p /opt/llm-gateway/nginx/conf.d /opt/llm-gateway/nginx/certs /opt/llm-gateway/gateway-logs /opt/llm-gateway/configs

if ! grep -q '^SESSION_SECRET=' /opt/llm-gateway/.env; then
  SESSION_SECRET_VALUE="$(openssl rand -hex 32)"
  printf '\nSESSION_SECRET=%s\n' "$SESSION_SECRET_VALUE" | sudo tee -a /opt/llm-gateway/.env >/dev/null
  unset SESSION_SECRET_VALUE
  sudo chmod 600 /opt/llm-gateway/.env
fi

if [ ! -s /opt/llm-gateway/nginx/certs/server.crt ] || [ ! -s /opt/llm-gateway/nginx/certs/server.key ]; then
  sudo openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -subj "/CN=gateway.kcs-poc.local" \
    -keyout /opt/llm-gateway/nginx/certs/server.key \
    -out /opt/llm-gateway/nginx/certs/server.crt
  sudo chmod 600 /opt/llm-gateway/nginx/certs/server.key
  sudo chmod 644 /opt/llm-gateway/nginx/certs/server.crt
fi

sudo tee /opt/llm-gateway/nginx/conf.d/gateway.conf >/dev/null <<'NGINX'
upstream collector { server usage-collector:8080; }
upstream web       { server gateway-web:8090; }

server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name _;

    ssl_certificate     /etc/nginx/certs/server.crt;
    ssl_certificate_key /etc/nginx/certs/server.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    client_max_body_size 4m;

    location /v1/ {
        proxy_pass http://collector;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /admin/ {
        proxy_pass http://web;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /api/ {
        proxy_pass http://web;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /static/ {
        proxy_pass http://web;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
    }

    location = /healthz {
        return 200 "ok\n";
        add_header Content-Type text/plain;
    }
}
NGINX

aws ecr get-login-password --region "$AWS_REGION" \
  | sudo docker login --username AWS --password-stdin "$ECR_REGISTRY"

sudo -E docker compose pull
sudo -E docker compose up -d --remove-orphans
sudo docker compose ps

curl -sf http://localhost:8080/health
curl -sf http://127.0.0.1:8090/healthz
curl -skf https://localhost/healthz

for container in usage-collector gateway-web nginx-proxy; do
  running="$(sudo docker inspect "$container" --format '{{.State.Running}}')"
  if [ "$running" != "true" ]; then
    echo "$container is not running"
    sudo docker logs "$container" --tail=80 || true
    exit 1
  fi
done
