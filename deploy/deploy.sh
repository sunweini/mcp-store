#!/bin/bash
# MCP Gateway 容器化一键部署
# Usage: bash deploy.sh
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$DEPLOY_DIR")"
CONFIG_DIR="$DEPLOY_DIR/config"
DATA_DIR="$DEPLOY_DIR/data"
LOGS_DIR="$DEPLOY_DIR/logs"

echo "=== MCP Gateway 容器化部署 ==="
echo "  deploy dir: $DEPLOY_DIR"

# docker/compose v2 是容器运行前提(v1 已 EOL)
echo "[1/6] 检查 docker..."
command -v docker >/dev/null || { echo "ERROR: docker 未安装"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: docker compose v2 未安装"; exit 1; }
echo "  docker: $(docker --version)"

# config 含密钥不进镜像,首次运行从 .example 模板生成
echo "[2/6] 检查 config..."
mkdir -p "$CONFIG_DIR" "$DATA_DIR/redis" "$LOGS_DIR/proxy" "$LOGS_DIR/admin" "$LOGS_DIR/zabbix-mcp"
for f in proxy.env admin.env zabbix.env; do
  if [ ! -f "$CONFIG_DIR/$f" ]; then
    cp "$CONFIG_DIR/$f.example" "$CONFIG_DIR/$f"
    echo "  已从模板生成 $f - 请编辑填入真实值"
  fi
done
# 首次部署自动生成 JWT_SECRET,避免用户手动操作
if grep -q "CHANGE_ME_generate" "$CONFIG_DIR/admin.env" 2>/dev/null; then
  SECRET=$(openssl rand -base64 32)
  sed -i.bak "s|CHANGE_ME_generate_with_openssl_rand_base64_32|$SECRET|" "$CONFIG_DIR/admin.env" && rm -f "$CONFIG_DIR/admin.env.bak"
  echo "  已生成 JWT_SECRET"
fi
# 拒绝占位符配置进入运行时:用户未编辑 .env 会带默认占位值启动,导致鉴权失效或连不上 Zabbix
grep -q 'CHANGE_ME_strong_password\|your-api-token\|your-zabbix' "$CONFIG_DIR/admin.env" "$CONFIG_DIR/zabbix.env" 2>/dev/null && {
  echo "ERROR: config 仍含占位符,请先编辑 config/*.env 填入真实值" >&2; exit 1; }
echo "  ⚠️  请确认 config/zabbix.env 的 ZABBIX_URL/ZABBIX_TOKEN 已填,admin.env 的 ADMIN_INIT_PASSWORD 已改"

# 所有服务镜像 FROM mcp-base,需先构建
echo "[3/6] build 基础镜像 mcp-base..."
docker build -t mcp-base:latest -f "$DEPLOY_DIR/Dockerfile.base" "$ROOT"

# 根据 compose 定义构建三个服务镜像
echo "[4/6] build 服务镜像..."
docker compose -f "$DEPLOY_DIR/docker-compose.yml" build

# --wait 等 healthy(compose v2):有 healthcheck 的服务需通过才返回,无 healthcheck 视为 running 即 healthy
echo "[5/6] 启动容器..."
docker compose -f "$DEPLOY_DIR/docker-compose.yml" up -d --wait
docker compose -f "$DEPLOY_DIR/docker-compose.yml" ps

# 注册 zabbix-mcp 并创建 API token,失败则需人工排查
echo "[6/6] 初始化..."
ADMIN_INIT_PASSWORD=$(grep '^ADMIN_INIT_PASSWORD=' "$CONFIG_DIR/admin.env" | cut -d= -f2-)
ADMIN_PASS="${ADMIN_INIT_PASSWORD:-admin123}" bash "$DEPLOY_DIR/init.sh" || {
  echo "ERROR: init 失败,请检查 admin 是否就绪后重跑: bash deploy/init.sh" >&2
  exit 1
}

echo ""
echo "=== 部署完成 ==="
echo "  Admin UI:  http://localhost:8081"
echo "  Proxy:     http://localhost:8082/mcp"
echo "  Metrics:   http://localhost:9465/metrics"
echo "  日志:       $LOGS_DIR/{proxy,admin,zabbix-mcp}/"
echo "  数据:       $DATA_DIR/redis/"
echo ""
echo "  管理命令:"
echo "    docker compose -f $DEPLOY_DIR/docker-compose.yml logs -f"
echo "    docker compose -f $DEPLOY_DIR/docker-compose.yml restart gateway-proxy"
