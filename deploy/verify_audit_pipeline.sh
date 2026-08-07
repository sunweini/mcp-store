#!/bin/bash
# 生产验证：审计流水线（proxy XADD → admin 消费者 → MySQL calls 表）
# 用法: bash deploy/verify_audit_pipeline.sh <ssh_host> [ssh_port]
#   ssh_host 如 root@10.33.17.72（本仓库生产机），ssh_port 默认 22
#   可用环境变量：SSH_KEY=/path/to/key 指定私钥
# 先决条件：已执行 deploy.sh 完成部署；本脚本只读，不修改生产状态。
# 退出码：1 = 关键检查失败（容器未 Up / 消费断档）；WARN 项不阻断退出。
set -euo pipefail

HOST="${1:?usage: verify_audit_pipeline.sh <ssh_host> [ssh_port]}"
PORT="${2:-22}"
SSHOPTS=(-p "$PORT")
[ -n "${SSH_KEY:-}" ] && SSHOPTS+=(-i "$SSH_KEY")

SSH() { ssh "${SSHOPTS[@]}" "$HOST" "$@"; }
# 生产 compose 固定路径（见 deploy/ 目录部署约定）
COMPOSE_FILE="/opt/mcp-gateway-cfg/deploy/docker-compose.yml"
REDIS_CID="docker ps --filter name=redis -q"
MYSQL_CID="docker ps --filter name=mysql -q"

echo "[1/5] 检查容器状态"
STATUS=$(SSH "docker compose -f $COMPOSE_FILE ps --format '{{.Service}} {{.State}}'" || true)
FAIL=0
for svc in gateway-proxy gateway-admin redis mysql; do
  if echo "$STATUS" | grep -qE "^${svc} (running|Up)"; then
    echo "  OK  $svc 运行中"
  else
    echo "  FAIL: $svc 未运行"
    FAIL=1
  fi
done
[ "$FAIL" -eq 0 ] || { echo "compose ps 输出："; echo "$STATUS"; exit 1; }

echo "[2/5] 检查审计 stream（XADD 侧，gateway-proxy 写入）"
XLEN=$(SSH "docker exec \$( $REDIS_CID ) redis-cli XLEN audit:calls" | tr -d '\r')
echo "  audit:calls XLEN = ${XLEN:-0}（MAXLEN 50000 滚动裁剪）"
if [ "${XLEN:-0}" -lt 0 ] 2>/dev/null || ! echo "$XLEN" | grep -qE '^[0-9]+$'; then
  echo "  FAIL: 无法读取 XLEN（redis 容器或 stream 异常）"
  exit 1
fi
[ "${XLEN:-0}" -gt 0 ] || echo "  WARN: stream 为空 —— 暂无调用流量，可先人工触发一次工具调用再重跑"

echo "[3/5] 检查死信流（audit:calls:dead，应接近 0）"
DEAD=$(SSH "docker exec \$( $REDIS_CID ) redis-cli XLEN audit:calls:dead" | tr -d '\r')
echo "  audit:calls:dead XLEN = ${DEAD:-0}"
if [ "${DEAD:-0}" -gt 0 ]; then
  echo "  WARN: 死信非空 —— 存在落库失败批次，需查 gateway-admin 日志定位"
fi

echo "[4/5] 检查 calls 表（消费者 XREADGROUP → INSERT 侧，gateway-admin 落库）"
# 密码从 mysql 容器自身 env 读（MYSQL_USER/MYSQL_PASSWORD/MYSQL_DATABASE 由
# config/mysql.env 注入），不落命令行、不回显，宿主无需知道生产密码。
# 注意 \$\( 必须留在双引号内（本地展开会拿到空容器 ID），由远端 shell 解析。
MYSQL_RUN() { SSH "docker exec \$(docker ps --filter name=mysql -q) sh -c 'mysql -u\"\$MYSQL_USER\" -p\"\$MYSQL_PASSWORD\" \"\$MYSQL_DATABASE\" -N -e \"$1\"'"; }
CNT=$(MYSQL_RUN 'SELECT COUNT(*) FROM calls' | tr -d '\r')
RECENT=$(MYSQL_RUN 'SELECT COUNT(*) FROM calls WHERE time >= NOW() - INTERVAL 5 MINUTE' | tr -d '\r')
echo "  calls 总行数 = ${CNT:-0}"
echo "  近 5 分钟新增 = ${RECENT:-0}"
if ! echo "$CNT" | grep -qE '^[0-9]+$'; then
  echo "  FAIL: 无法查询 calls 表（MySQL 容器未就绪或表未初始化）"
  exit 1
fi
if [ "${XLEN:-0}" -gt 0 ] && [ "${RECENT:-0}" -eq 0 ]; then
  echo "  FAIL: stream 有积压但近 5 分钟无落库 —— 消费者可能未运行（gateway-admin lifespan 后台 task）"
  echo "        检查: docker compose -f $COMPOSE_FILE logs gateway-admin | grep audit_consumer"
  exit 1
fi
[ "${CNT:-0}" -gt 0 ] || echo "  WARN: calls 表全空 —— 可能从无调用，或消费者从未成功建组"

echo "[5/5] UI 人工验证（失败面板轨迹）"
echo "  1) 浏览器登录 http://$HOST:8081 → 请求日志页应有数据（与 calls 表一致）"
echo "  2) 用无效 token 调一次工具 → 刷新失败面板：应见 error_type=invalid_token + message + journey 轨迹"
echo "  3) 核对失败面板 message 与 gateway-proxy 日志（logs/proxy/）一致"
echo
echo "PASS: 审计流水线验证完成（有 WARN 项请按提示跟进）"
