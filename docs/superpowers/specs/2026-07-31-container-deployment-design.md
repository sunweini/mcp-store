# MCP Gateway 容器化部署设计

**日期**: 2026-07-31
**状态**: 已批准
**目标**: 将 MCP Gateway 三个服务(gateway-proxy / gateway-admin / zabbix-mcp)+ Redis 从 systemd+venv 部署迁移到 Docker Compose 容器部署,数据/配置/日志持久化到宿主。

## 背景

当前 10.33.17.72(CentOS 7, amd64)上的 MCP Gateway 用 systemd + uv venv 直跑:
- gateway-proxy :8082、gateway-admin :8081、zabbix-mcp :8000、Redis :6379
- 配置在 `/etc/mcp-gateway/*.env`,日志在 systemd journal,数据在 Redis 内存(RDB 未持久化)

痛点:依赖装在系统 venv、隔离弱、迁移要重跑 `uv sync`、日志不持久。改为容器化统一管理。

## 关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 基础镜像范围 | Python 3.12 + uv + Node.js 20 全功能 | 一个基础镜像覆盖所有服务(含 admin-ui 的 npm build),后续服务 Dockerfile 最简 |
| Redis | compose 自带 redis:7,替换宿主现有 Redis | 宿主 Redis 仅 gateway 使用(已验证:6 个 key 全是 gateway 的,连接只有 proxy/admin) |
| 日志本地化 | 服务代码加 RotatingFileHandler(LOG_FILE 环境变量控制) | 结构化 JSON 同时写 stdout+文件,符合 OBS-CORE-001,容器删了日志还在 |
| 镜像分发 | 服务器本地 docker build | 服务器 docker 26.0 可 build 可 pull,避免 arm64/amd64 cross-build |
| 旧 systemd 脚本 | 删除 | 完全替换为容器形式 |
| admin 初始密码 | 加 ADMIN_INIT_PASSWORD 环境变量 | 部署时直接设强密码,不用事后改 |
| proxy metrics 9465 | 对外暴露 | 方便 Prometheus 抓取 |

## 整体架构

```
                    宿主 10.33.17.72
   ┌─────────────────────────────────────────────┐
   │  docker compose (网络: mcp-net)             │
   │                                              │
   │  :8082 ── gateway-proxy ──┐                  │
   │  :9465    (metrics)       │ 转发 MCP          │
   │                           ▼                  │
   │  :8081 ── gateway-admin   zabbix-mcp :8000   │
   │           (API+UI)         (metrics :9464)    │
   │              │                               │
   │              ▼                               │
   │            redis :6379  ◄── 共享存储          │
   └─────────────────────────────────────────────┘

   本地持久化(挂载到宿主):
    /opt/mcp-gateway/config/  env 文件
    /opt/mcp-gateway/data/    redis RDB
    /opt/mcp-gateway/logs/    各服务日志
```

服务间用容器名互访(同一 compose 网络 `mcp-net`):
- proxy/admin 连 Redis: `redis://redis:6379/0`
- proxy 转发到 zabbix-mcp: `http://zabbix-mcp:8000/mcp`
- admin 拉 proxy metrics: `http://gateway-proxy:9465/metrics`

对外暴露:proxy `8082`、admin `8081`、proxy metrics `9465`。zabbix-mcp(`8000`/`9464`)和 redis(`6379`)不映射宿主端口,仅内部网络。

## 镜像分层

### 基础镜像 `deploy/Dockerfile.base` -> `mcp-base:latest`

```dockerfile
FROM python:3.12-slim
# 编译依赖(部分 Python 包需要 gcc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc build-essential curl && rm -rf /var/lib/apt/lists/*
# uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# Node.js 20(给 gateway-admin 的 admin-ui 构建)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*
WORKDIR /app
```

一次 build,所有服务 `FROM mcp-base`。

### 服务 Dockerfile

统一模式:先 COPY 依赖描述文件 + sync(利用 layer cache),再 `COPY . .`(配 `.dockerignore` 排除 `.venv`/`tests`/`__pycache__`/`.git`/`node_modules`/`dist`)。

**`gateway-proxy/Dockerfile`**:
```dockerfile
FROM mcp-base:latest
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . ./
CMD ["uv", "run", "python", "server.py"]
```

**`gateway-admin/Dockerfile`**:
```dockerfile
FROM mcp-base:latest
WORKDIR /app
# 先构建前端(layer cache:package.json 变了才重装)
COPY admin-ui/package.json ./admin-ui/
RUN cd admin-ui && npm install
COPY admin-ui/ ./admin-ui/
RUN cd admin-ui && npm run build   # 产物 -> admin-ui/dist
# Python 依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . ./
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081"]
```

**`zabbix-mcp/Dockerfile`**:
```dockerfile
FROM mcp-base:latest
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . ./
CMD ["uv", "run", "python", "server.py"]
```

> 每个 `uv.lock` 必须存在且与 `pyproject.toml` 一致(`--frozen` 保证可复现)。`admin-ui` 用 `npm install`(无 `package-lock.json` 时 `npm ci` 会失败)。镜像内不含业务配置(env 挂载注入)。每个服务目录加 `.dockerignore`。

## compose.yml

文件 `deploy/docker-compose.yml`:

```yaml
services:
  redis:
    image: redis:7-alpine
    command: redis-server --save 60 1 --loglevel warning
    volumes:
      - ../data/redis:/data
    networks: [mcp-net]
    restart: unless-stopped

  gateway-proxy:
    build: ../gateway-proxy
    ports:
      - "8082:8082"
      - "9465:9465"
    env_file:
      - ../config/proxy.env
    environment:
      REDIS_URL: redis://redis:6379/0
      GATEWAY_PORT: "8082"
      GATEWAY_HOST: "0.0.0.0"
      PROMETHEUS_PORT: "9465"
      LOG_FILE: /app/logs/gateway-proxy.log
    volumes:
      - ../logs/proxy:/app/logs
    networks: [mcp-net]
    depends_on: [redis]
    restart: unless-stopped

  gateway-admin:
    build: ../gateway-admin
    ports:
      - "8081:8081"
    env_file:
      - ../config/admin.env
    environment:
      REDIS_URL: redis://redis:6379/0
      GATEWAY_PROXY_METRICS_URL: http://gateway-proxy:9465/metrics
      LOG_FILE: /app/logs/gateway-admin.log
    volumes:
      - ../logs/admin:/app/logs
    networks: [mcp-net]
    depends_on: [redis, gateway-proxy]
    restart: unless-stopped

  zabbix-mcp:
    build: ../zabbix-mcp
    env_file:
      - ../config/zabbix.env
    environment:
      MCP_HOST: "0.0.0.0"
      MCP_PORT: "8000"
      LOG_FORMAT: json
      LOG_FILE: /app/logs/zabbix-mcp.log
    volumes:
      - ../logs/zabbix-mcp:/app/logs
    networks: [mcp-net]
    depends_on: [redis]
    restart: unless-stopped

networks:
  mcp-net:
    driver: bridge
```

## 本地持久化目录

```
/opt/mcp-gateway/
├── config/
│   ├── proxy.env      # OTEL_EXPORTER_OTLP_ENDPOINT 等(可选,可为空)
│   ├── admin.env      # JWT_SECRET, JWT_EXPIRES, ADMIN_INIT_PASSWORD
│   └── zabbix.env     # ZABBIX_URL, ZABBIX_TOKEN, MCP_HOST, MCP_PORT, LOG_FORMAT
├── data/
│   └── redis/         # Redis RDB 持久化(redis-server --save 60 1)
└── logs/
    ├── proxy/         # gateway-proxy.log (+ 滚动)
    ├── admin/         # gateway-admin.log
    └── zabbix-mcp/    # zabbix-mcp.log
```

- 配置(含 `ZABBIX_TOKEN`)、数据、日志全部在宿主,容器删除后保留
- 敏感凭据通过 env_file 挂载,不进镜像
- Redis `--save 60 1` 每 60s 或 1 次变更落 RDB 到 `data/redis/dump.rdb`

## 日志改造

三个服务的 structlog 配置各加一个 `RotatingFileHandler`,行为:

- 不设 `LOG_FILE` 环境变量:只写 stdout(本地开发行为不变)
- 设了 `LOG_FILE`(容器内如 `/app/logs/gateway-proxy.log`):结构化 JSON **同时**写 stdout 和文件,`RotatingFileHandler(maxBytes=10MB, backupCount=5)`

改造点:
- `gateway-proxy`:定位其 structlog 配置(在 server.py 或独立 logging 模块),加 file handler
- `gateway-admin`:app.py 或独立 logging 模块
- `zabbix-mcp`:`server.py` 的 `_configure_logging()` 函数

共享一个 helper(可放各自目录的 `logging_config.py` 或 inline),避免重复。符合 OBS-CORE-001(结构化)、OBS-CORE-002(关联字段)。

## 配置 + admin 密码初始化

### admin 密码(改 `gateway-admin/auth.py`)

`ensure_default_admin()` 当前硬编码 `admin:admin`。改为:

```python
async def ensure_default_admin() -> None:
    """Create default admin from ADMIN_INIT_PASSWORD env (fallback admin:admin)."""
    r = get_redis()
    if await r.exists("admin:admin"):
        return
    password = os.environ.get("ADMIN_INIT_PASSWORD", "admin")
    await r.hset("admin:admin", mapping={
        "password_hash": hash_password(password),
        "created_at": ...,
    })
```

- `ADMIN_INIT_PASSWORD` 设了:首次启动用该密码建 admin
- 没设:回退 `admin:admin`
- 账号已存在则跳过(幂等,改密码不影响)

### config 文件模板

`deploy/config/` 下提供 `.example` 模板:
- `proxy.env.example`(空或 OTEL 注释)
- `admin.env.example`(`JWT_SECRET=<openssl rand>`, `ADMIN_INIT_PASSWORD=`)
- `zabbix.env.example`(`ZABBIX_URL=`, `ZABBIX_TOKEN=`, `MCP_HOST=0.0.0.0`, `MCP_PORT=8000`)

部署脚本从模板生成真实 config(若不存在),敏感值由用户填。

## 数据初始化 `deploy/init.sh`

compose 起来后 Redis 是空的,需重新注册 zabbix-mcp + 建 token。脚本流程:

1. 用 `ADMIN_INIT_PASSWORD`(或 admin:admin)登录 admin -> 拿 JWT
2. POST `/api/servers` 注册 zabbix-mcp(URL=`http://zabbix-mcp:8000/mcp`)
3. POST `/api/servers/zabbix-mcp/refresh-tools` 拉取工具列表
4. POST `/api/tokens` 创建 API token(read+write),输出明文 token
5. 打印 MCP client 连接配置

幂等:已存在则跳过。

## 部署文档 + 迁移

### 文档

- 新建 `deploy/README.md`:容器部署指南(前置要求、build、配置、启动、验证、运维命令)
- 重写 `deploy/deploy.sh`:一键部署(检查 docker -> 生成 config 模板 -> 建 data/logs 目录 -> build base -> compose build -> up -> init -> 验证)
- 删除 `deploy/setup-server.sh`、`deploy/systemd/gateway-proxy.service`、`deploy/systemd/gateway-admin.service`

### 迁移步骤(一次性,从现有 systemd 切到容器)

1. 服务器 `git pull` 仓库
2. 停并禁用 systemd 服务:`systemctl stop gateway-proxy gateway-admin zabbix-mcp && systemctl disable gateway-proxy gateway-admin zabbix-mcp`
3. 停宿主 Redis:`systemctl stop redis`(清理 failed 的 redis.service unit)
4. 建 config(从模板,填 ZABBIX_TOKEN、JWT_SECRET、ADMIN_INIT_PASSWORD)
5. `docker build -t mcp-base:latest -f deploy/Dockerfile.base .`
6. `cd deploy && docker compose build && docker compose up -d`
7. 跑 `deploy/init.sh` 注册 zabbix-mcp + 建 token
8. 验证全链路(见下)

> 现有 Redis 的 6 个 key(zabbix-mcp 注册、admin、token)不迁移,容器 Redis 启动后由 `init.sh` 重新初始化。

## 验证标准

部署成功需全部通过:

1. `docker compose ps` 四个服务 `Up`
2. `curl http://10.33.17.72:8081/api/health` -> 200
3. `curl -X POST http://10.33.17.72:8082/mcp`(Bearer token)`tools/list` -> 返回 zabbix-mcp 的 8 个工具(namespaced `zabbix-mcp_*`)
4. `tools/call zabbix-mcp_list_active_problems` -> 真实返回 Zabbix 告警
5. `curl http://10.33.17.72:9465/metrics` -> Prometheus 指标
6. 宿主 `/opt/mcp-gateway/logs/{proxy,admin,zabbix-mcp}/*.log` 有结构化 JSON 日志
7. 宿主 `/opt/mcp-gateway/data/redis/dump.rdb` 存在(Redis 持久化生效)
8. 重启单个容器(`docker compose restart zabbix-mcp`)后,配置/数据/日志仍在

## 文件变更清单

### 新建
- `deploy/Dockerfile.base`
- `deploy/docker-compose.yml`
- `deploy/init.sh`
- `deploy/README.md`
- `deploy/deploy.sh`(重写为容器版)
- `deploy/config/proxy.env.example`
- `deploy/config/admin.env.example`
- `deploy/config/zabbix.env.example`
- `gateway-proxy/Dockerfile`
- `gateway-proxy/.dockerignore`
- `gateway-admin/Dockerfile`
- `gateway-admin/.dockerignore`
- `zabbix-mcp/Dockerfile`
- `zabbix-mcp/.dockerignore`
- `gateway-proxy/logging_config.py`(共享日志 helper,或 inline)
- `gateway-admin/logging_config.py`
- `zabbix-mcp/logging_config.py`(或 inline 到 `_configure_logging`)

### 修改
- `gateway-admin/auth.py`:`ensure_default_admin` 读 `ADMIN_INIT_PASSWORD`
- `gateway-proxy/server.py`(或 logging 配置):加 file handler
- `gateway-admin/app.py`(或 logging 配置):加 file handler
- `zabbix-mcp/server.py`:`_configure_logging()` 加 file handler
- 各 `uv.lock`:确保存在且与 `pyproject.toml` 一致

### 删除
- `deploy/setup-server.sh`
- `deploy/systemd/gateway-proxy.service`
- `deploy/systemd/gateway-admin.service`
- `deploy/systemd/`(空目录)
- 旧 `deploy/deploy.sh`(被新版替换)

## 非目标

- 不做多机部署 / 集群(单机够用)
- 不推外部 registry(本地 build)
- 不引入 Kubernetes
- 不迁移现有 Redis 数据(重新初始化)
