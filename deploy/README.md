# MCP Gateway 容器化部署

## 前置要求

- Docker 20.10+ 与 Docker Compose v2
- 服务器 linux/amd64
- 端口可用:8081(admin)、8082(proxy)、9465(proxy metrics)、
  9466/9467/9468(搜索 MCP metrics: tavily/brave/serpapi)

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
| `proxy.env` | `OTEL_EXPORTER_OTLP_ENDPOINT`, `SEARCH_PROXY` | 可选;OTel 收集器;brave 出网代理(见下) |

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
:9466 -> tavily-mcp metrics (容器内 9464)
:9467 -> brave-mcp metrics (容器内 9464)
:9468 -> serpapi-mcp metrics (容器内 9464)
redis:6379 (内部,共享存储)
```

服务间用容器名互访。各 MCP 的 MCP 端口、redis 不对外暴露；
仅 metrics 端口映射到宿主（见上），供宿主 Prometheus scrape。

三个搜索 MCP 的 API key 不配环境变量——通过 admin 界面
「API Keys」页写入 Redis（`search:keys:<provider>`），MCP 启动时从 KeyPool 读取。
搜索 MCP 需先在 UI 配好 key，工具调用才能成功。

### brave 代理（SEARCH_PROXY）

生产网络 api.search.brave.com 直连不通（IPv4 被墙、IPv6 不通），**仅
brave 需要走内网代理**（tavily/serpapi 直连通）。`brave-mcp` 与
`gateway-admin` 两个容器都从 `config/proxy.env` 的 `SEARCH_PROXY` 读取
代理（admin 探活 brave key 时也要直连 brave API，所以两处都配）：

```bash
# 1. 在 proxy.env 填入代理（注意:该文件也是 gateway-proxy 的共享配置，
#    均为通用变量,三个服务共读无副作用）
vim deploy/config/proxy.env
#   SEARCH_PROXY=http://10.16.12.12:7890

# 2. 正常部署即可——代理随 config 持久化,rebuild/重部署不丢
bash deploy/deploy.sh
```

未配置（proxy.env 中 `SEARCH_PROXY=` 留空）时两个容器保持直连。

## Metrics scrape 配置

| 目标 | 地址 | 指标 |
|---|---|---|
| gateway-proxy | `http://<host>:9465/metrics` | 请求量/延迟（proxy 层） |
| tavily-mcp | `http://<host>:9466/metrics` | `search_*` 指标族（quota 告警） |
| brave-mcp | `http://<host>:9467/metrics` | `search_*` 指标族 |
| serpapi-mcp | `http://<host>:9468/metrics` | `search_*` 指标族 |

`search_quota_ratio{provider, level}` 按 provider 聚合（level:
warning<10% / critical<5% / exhausted=0），配合 alertmanager 告警；
`search_quota_remaining` / `search_key_pool_size` / `search_key_invalid_total`
在 key 池 reload（热更新/启动）与 key 失效剔除后刷新，scrape 周期建议
<= 60s 对齐告警时效。各容器内统一监听 9464，宿主端口映射互不冲突。

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

## 存量环境迁移（zabbix-mcp 端口 8000 → 9053）

本版本将 zabbix-mcp 容器端口从 8000 迁移到 **9053**（与三个搜索 MCP
共用 9050-9500 端口段）。**已部署过旧版本的环境升级后必须校正 admin 里
注册的 zabbix-mcp URL**，否则 gateway-proxy 会持续 502：

```bash
# 1. 登录拿 admin token（username 默认 admin）
TOK=$(curl -s -m5 -X POST http://localhost:8081/api/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"<ADMIN_INIT_PASSWORD>\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')

# 2. 校正 zabbix-mcp URL 到 9053（PUT 会通知 proxy 热加载）
curl -s -X PUT http://localhost:8081/api/servers/zabbix-mcp \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"url":"http://zabbix-mcp:9053/mcp","description":"Zabbix 告警巡检/维护期/告警确认（8 tools）"}'

# 3. 验证
curl -s http://localhost:8081/api/servers/zabbix-mcp/status \
  -H "Authorization: Bearer $TOK"    # 期望 {"up": true, ...}
```

> 注意：`PUT /api/servers/{name}` 的 `description` 为必填（`ServerUpdate`
> 无默认值），必须随请求体一并提交，否则 422。
>
> 或直接删除后重新注册（`DELETE /api/servers/zabbix-mcp` →
> `POST /api/servers` 带新 URL），效果相同。升级后首次访问
> `zabbix-mcp_*` 工具前务必完成本步，否则 proxy 502。
>
> 三个搜索 MCP（tavily/brave/serpapi）为新增服务，无存量迁移问题；
> 全新部署（`deploy.sh`）不受影响，init.sh 已按新 URL 注册。
