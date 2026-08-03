# MCP Gateway 容器化部署

## 前置要求

- Docker 20.10+ 与 Docker Compose v2
- 服务器 linux/amd64
- 端口可用:8081(admin)、8082(proxy)、9465(metrics)

## 快速部署

```bash
# 1. 克隆仓库
git clone <repo-url> && cd mcpstore

# 2. 从模板生成 config 并编辑填入真实凭据(deploy.sh 之前必须完成)
#    - config/zabbix.env: ZABBIX_URL, ZABBIX_TOKEN
#    - config/admin.env: ADMIN_INIT_PASSWORD
cp deploy/config/zabbix.env.example deploy/config/zabbix.env
cp deploy/config/admin.env.example deploy/config/admin.env
cp deploy/config/proxy.env.example deploy/config/proxy.env
vim deploy/config/*.env

# 3. 一键部署(自动生成 JWT_SECRET、build、启动、初始化)
bash deploy/deploy.sh

# 4. 重新初始化(仅当 init 失败或需重建 token 时)
bash deploy/init.sh
```

## 配置文件

位于 `deploy/config/`,从 `.example` 模板生成:

| 文件 | 关键项 | 说明 |
|---|---|---|
| `zabbix.env` | `ZABBIX_URL`, `ZABBIX_TOKEN` | Zabbix API 连接(必填) |
| `admin.env` | `JWT_SECRET`, `ADMIN_INIT_PASSWORD` | admin 密码与 JWT 签名 |
| `proxy.env` | `OTEL_EXPORTER_OTLP_ENDPOINT` | 可选,OTel 收集器 |

## 持久化目录

| 宿主路径 | 容器路径 | 用途 |
|---|---|---|
| `deploy/config/` | env_file 挂载 | 配置(不进镜像) |
| `deploy/data/redis/` | `/data` | Redis RDB 持久化 |
| `deploy/logs/proxy/` | `/app/logs` | gateway-proxy 日志 |
| `deploy/logs/admin/` | `/app/logs` | gateway-admin 日志 |
| `deploy/logs/zabbix-mcp/` | `/app/logs` | zabbix-mcp 日志 |
| `deploy/logs/tavily-mcp/` | `/app/logs` | tavily-mcp 日志 |
| `deploy/logs/brave-mcp/` | `/app/logs` | brave-mcp 日志 |
| `deploy/logs/serpapi-mcp/` | `/app/logs` | serpapi-mcp 日志 |

## 架构

```
:8082 -> gateway-proxy -> zabbix-mcp:9053 (内部)
                      -> tavily-mcp:9050 (内部)
                      -> brave-mcp:9051  (内部)
                      -> serpapi-mcp:9052(内部)
:8081 -> gateway-admin (API + Vue UI)
:9465 -> gateway-proxy metrics
redis:6379 (内部,共享存储)
```

服务间用容器名互访。各 MCP、redis 不对外暴露。

三个搜索 MCP 的 API key 不配环境变量——通过 admin 界面
「API Keys」页写入 Redis（`search:keys:<provider>`），MCP 启动时从 KeyPool 读取。
搜索 MCP 需先在 UI 配好 key，工具调用才能成功。

## 运维命令

```bash
# 查看状态
docker compose -f deploy/docker-compose.yml ps

# 查看日志(容器 stdout)
docker compose -f deploy/docker-compose.yml logs -f gateway-proxy

# 本地日志文件(结构化 JSON)
tail -f deploy/logs/proxy/gateway-proxy.log

# 重启单个服务
docker compose -f deploy/docker-compose.yml restart zabbix-mcp

# 重新构建(代码更新后)
docker compose -f deploy/docker-compose.yml build gateway-proxy
docker compose -f deploy/docker-compose.yml up -d gateway-proxy

# 停止 / 清理
docker compose -f deploy/docker-compose.yml down
# 注意:config/data/logs 在宿主,down 不会删
```

## MCP client 连接

```json
{
  "mcpServers": {
    "gateway": {
      "url": "http://<server-ip>:8082/mcp",
      "headers": { "Authorization": "Bearer <api-token>" }
    }
  }
}
```

API token 在 `init.sh` 运行时创建(明文只显示一次)。tool 名带 namespace 前缀,如 `zabbix-mcp_list_active_problems`。

## 从 systemd 迁移

见 spec: `docs/superpowers/specs/2026-07-31-container-deployment-design.md` 的「迁移步骤」。
