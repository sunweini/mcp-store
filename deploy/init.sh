#!/bin/bash
# 幂等初始化:首次部署或 admin 重置后运行,注册 zabbix-mcp + 建 token。
# 在宿主上运行(非容器内),因为需要通过 localhost:8081 调 admin API。
set -euo pipefail

ADMIN_HOST="${ADMIN_HOST:-http://localhost:8081}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-${ADMIN_INIT_PASSWORD:-admin123}}"
# 服务间互访走容器名+容器内端口(proxy 从 Redis registry 读 URL,注册即生效)
ZABBIX_MCP_URL="${ZABBIX_MCP_URL:-http://zabbix-mcp:9053/mcp}"
# 搜索 MCP:name:port(容器内端口),key 从 Redis 读,无 env
SEARCH_MCPS="tavily-mcp:9050 brave-mcp:9051 serpapi-mcp:9052"
TOKEN_NAME="${TOKEN_NAME:-gateway-full}"

echo "=== 登录 admin ==="
TOK=$(curl -s -m5 -X POST "$ADMIN_HOST/api/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["token"] if "token" in d else d)')
# JWT 以 eyJ 开头(header base64 解码为 {"alg":...);非此格式说明 login 失败(返回了 error dict)
case "$TOK" in eyJ*) ;; *) echo "ERROR: login 失败: $TOK" >&2; exit 1 ;; esac
echo "  token: ${TOK:0:20}..."

echo "=== 注册 zabbix-mcp(若不存在)==="
EXISTING=$(curl -s -m5 "$ADMIN_HOST/api/servers" -H "Authorization: Bearer $TOK" \
  | python3 -c "import sys,json; print(any(s['name']=='zabbix-mcp' for s in json.load(sys.stdin)))" 2>/dev/null || echo "False")
if [ "$EXISTING" = "True" ]; then
  echo "  zabbix-mcp 已注册,跳过"
else
  curl -s -m10 -X POST "$ADMIN_HOST/api/servers" \
    -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
    -d "{\"name\":\"zabbix-mcp\",\"url\":\"$ZABBIX_MCP_URL\",\"description\":\"Zabbix monitoring: alert patrol, maintenance, acknowledgment\"}" \
    > /dev/null
  echo "  已注册"
fi

echo "=== 注册搜索 MCP servers(若不存在)==="
# admin API 无 GET /api/servers/{name} 单查路由,沿用上面的 list+python 判断模式做幂等
# admin 在容器内可通过容器名访问搜索 MCP(compose 同一 mcp-net);本脚本在宿主运行,仅调 admin API
for srv in $SEARCH_MCPS; do
  name="${srv%%:*}"; port="${srv##*:}"
  EXISTING=$(curl -s -m5 "$ADMIN_HOST/api/servers" -H "Authorization: Bearer $TOK" \
    | python3 -c "import sys,json; print(any(s['name']=='$name' for s in json.load(sys.stdin)))" 2>/dev/null || echo "False")
  if [ "$EXISTING" = "True" ]; then
    echo "  $name 已注册,跳过"
  else
    # URL 用容器名+容器内端口,proxy 从 Redis 读此 URL 转发
    curl -s -m10 -X POST "$ADMIN_HOST/api/servers" \
      -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
      -d "{\"name\":\"$name\",\"url\":\"http://$name:$port/mcp\",\"description\":\"$name search MCP\"}" \
      > /dev/null
    echo "  已注册 $name -> http://$name:$port/mcp"
  fi
done

echo "=== 刷新工具列表 ==="
curl -s -m15 -X POST "$ADMIN_HOST/api/servers/zabbix-mcp/refresh-tools" \
  -H "Authorization: Bearer $TOK" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  zabbix-mcp tools: %d 个" % len(d.get("tools",[])))'
for srv in $SEARCH_MCPS; do
  name="${srv%%:*}"
  curl -s -m15 -X POST "$ADMIN_HOST/api/servers/$name/refresh-tools" \
    -H "Authorization: Bearer $TOK" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(\"  $name tools: %d 个\" % len(d.get(\"tools\",[])))"
done

echo "=== 创建 API token(read+write)==="
# 幂等:列出已有 token,同名跳过
EXISTING_TOK=$(curl -s -m5 "$ADMIN_HOST/api/tokens" -H "Authorization: Bearer $TOK" \
  | TOKEN_NAME="$TOKEN_NAME" python3 -c "import sys,json,os; print(any(t.get('name')==os.environ['TOKEN_NAME'] for t in json.load(sys.stdin)))" 2>/dev/null || echo "False")
if [ "$EXISTING_TOK" = "True" ]; then
  echo "  token '$TOKEN_NAME' 已存在(明文无法再取,如需新明文请删除后重建),跳过"
else
  # 权限覆盖 4 个已注册 server;tokens API 校验引用的 server 必须已注册,故在注册之后创建
  curl -s -m10 -X POST "$ADMIN_HOST/api/tokens" \
    -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
    -d "{\"name\":\"$TOKEN_NAME\",\"permissions\":{\"zabbix-mcp\":{\"read\":true,\"write\":true},\"tavily-mcp\":{\"read\":true,\"write\":true},\"brave-mcp\":{\"read\":true,\"write\":true},\"serpapi-mcp\":{\"read\":true,\"write\":true}}}" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  明文 token(只显示一次): %s" % d.get("token","?"))'
fi

echo ""
echo "=== MCP client 连接配置 ==="
echo "  URL: http://<server-ip>:8082/mcp"
echo "  Header: Authorization: Bearer <token>"
