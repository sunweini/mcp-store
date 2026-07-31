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

# 1. 检查 docker
echo "[1/6] 检查 docker..."
command -v docker >/dev/null || { echo "ERROR: docker 未安装"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: docker compose v2 未安装"; exit 1; }
echo "  docker: $(docker --version)"

# 2. 生成 config(从模板,若不存在)
echo "[2/6] 检查 config..."
mkdir -p "$CONFIG_DIR" "$DATA_DIR/redis" "$LOGS_DIR/proxy" "$LOGS_DIR/admin" "$LOGS_DIR/zabbix-mcp"
for f in proxy.env admin.env zabbix.env; do
  if [ ! -f "$CONFIG_DIR/$f" ]; then
    cp "$CONFIG_DIR/$f.example" "$CONFIG_DIR/$f"
    echo "  已从模板生成 $f - 请编辑填入真实值"
  fi
done
# 生成 JWT_SECRET(若 admin.env 仍是占位)
if grep -q "CHANGE_ME_generate" "$CONFIG_DIR/admin.env" 2>/dev/null; then
  SECRET=$(openssl rand -base64 32)
  sed -i.bak "s|CHANGE_ME_generate_with_openssl_rand_base64_32|$SECRET|" "$CONFIG_DIR/admin.env" && rm -f "$CONFIG_DIR/admin.env.bak"
  echo "  已生成 JWT_SECRET"
fi
echo "  ⚠️  请确认 config/zabbix.env 的 ZABBIX_URL/ZABBIX_TOKEN 已填,admin.env 的 ADMIN_INIT_PASSWORD 已改"

# 3. build 基础镜像
echo "[3/6] build 基础镜像 mcp-base..."
docker build -t mcp-base:latest -f "$DEPLOY_DIR/Dockerfile.base" "$ROOT"

# 4. compose build
echo "[4/6] build 服务镜像..."
docker compose -f "$DEPLOY_DIR/docker-compose.yml" build

# 5. 启动
echo "[5/6] 启动容器..."
docker compose -f "$DEPLOY_DIR/docker-compose.yml" up -d
sleep 3
docker compose -f "$DEPLOY_DIR/docker-compose.yml" ps

# 6. 初始化(注册 zabbix-mcp + token)
echo "[6/6] 初始化..."
ADMIN_INIT_PASSWORD=$(grep '^ADMIN_INIT_PASSWORD=' "$CONFIG_DIR/admin.env" | cut -d= -f2-)
ADMIN_PASS="${ADMIN_INIT_PASSWORD:-admin123}" bash "$DEPLOY_DIR/init.sh" || echo "  init 需手动跑: bash deploy/init.sh"

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
