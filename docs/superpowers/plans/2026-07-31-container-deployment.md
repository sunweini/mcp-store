# MCP Gateway 容器化部署 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 MCP Gateway 三个服务(gateway-proxy / gateway-admin / zabbix-mcp)+ Redis 从 systemd+venv 迁移到 Docker Compose 容器部署,数据/配置/日志持久化到宿主。

**Architecture:** 一个 Python 3.12+uv+Node 基础镜像(`mcp-base`),三个服务镜像各自 `FROM mcp-base` + `uv sync`。docker-compose 编排四容器(proxy/admin/zabbix-mcp/redis),配置/数据/日志通过 volume 挂载到宿主 `/opt/mcp-gateway/{config,data,logs}`。服务间用容器名互访。

**Tech Stack:** Docker 26.0, Docker Compose v2, Python 3.12, FastMCP 4.0.0b1, uv, Node.js 20, Redis 7, structlog, FastAPI/uvicorn。

## Global Constraints

- Python >=3.12, FastMCP `4.0.0b1`, MCP protocol `2026-07-28`
- uv 装包 `--prerelease=allow`;镜像内 `uv sync --frozen --no-dev`
- 基础镜像 `FROM python:3.12-slim`;服务器 `linux/amd64`
- 配置/数据/日志必须挂载到宿主 `/opt/mcp-gateway/{config,data,logs}`,不进镜像
- 敏感凭据(ZABBIX_TOKEN, JWT_SECRET, ADMIN_INIT_PASSWORD)通过 env_file 注入
- 日志结构化 JSON(OBS-CORE-001),`LOG_FILE` 环境变量控制文件输出
- 服务间互访用容器名:`redis:6379`、`zabbix-mcp:8000`、`gateway-proxy:9465`
- 在 `container-deployment` 分支工作,每个 task 末尾 commit
- 服务器 SSH: `ssh -i ~/.ssh/id_loginmonitor -p 9166 root@10.33.17.72`

**Spec:** [docs/superpowers/specs/2026-07-31-container-deployment-design.md](../specs/2026-07-31-container-deployment-design.md)

---

## 文件结构

### 新建
| 文件 | 职责 |
|---|---|
| `gateway-admin/logging_config.py` | 共享 structlog+stdlib 配置,LOG_FILE 控制 file handler |
| `gateway-proxy/logging_config.py` | 同上(每个服务独立一份,因服务独立 uv 项目) |
| `zabbix-mcp/logging_config.py` | 同上 |
| `deploy/Dockerfile.base` | 基础镜像:python:3.12-slim + uv + node 20 + gcc |
| `gateway-proxy/Dockerfile` | proxy 镜像 |
| `gateway-proxy/.dockerignore` | 排除 .venv/tests/__pycache__ |
| `gateway-admin/Dockerfile` | admin 镜像(含 npm build) |
| `gateway-admin/.dockerignore` | 排除 .venv/tests/node_modules/dist |
| `zabbix-mcp/Dockerfile` | zabbix-mcp 镜像 |
| `zabbix-mcp/.dockerignore` | 排除 .venv/tests |
| `deploy/docker-compose.yml` | 四容器编排 |
| `deploy/config/proxy.env.example` | proxy 配置模板 |
| `deploy/config/admin.env.example` | admin 配置模板 |
| `deploy/config/zabbix.env.example` | zabbix-mcp 配置模板 |
| `deploy/init.sh` | 注册 zabbix-mcp + 建 token |
| `deploy/deploy.sh` | 一键部署脚本 |
| `deploy/README.md` | 容器部署指南 |

### 修改
| 文件 | 改动 |
|---|---|
| `gateway-admin/auth.py` | `ensure_default_admin` 读 `ADMIN_INIT_PASSWORD` |
| `gateway-admin/app.py` | structlog.configure 改用 `configure_logging()` |
| `gateway-proxy/server.py` | structlog.configure 改用 `configure_logging()` |
| `zabbix-mcp/server.py` | `_configure_logging()` 改用 `configure_logging()` |

### 删除
| 文件 | 原因 |
|---|---|
| `deploy/setup-server.sh` | systemd 方式已弃用 |
| `deploy/systemd/gateway-proxy.service` | 同上 |
| `deploy/systemd/gateway-admin.service` | 同上 |
| `deploy/systemd/`(空目录) | 同上 |
| `deploy/deploy.sh`(旧版) | 被容器版替换 |

---

### Task 1: ensure_default_admin 支持 ADMIN_INIT_PASSWORD

**Files:**
- Modify: `gateway-admin/auth.py:75-90`
- Test: `gateway-admin/tests/test_auth.py`

**Interfaces:**
- Produces: `ensure_default_admin()` 读取 `os.environ.get("ADMIN_INIT_PASSWORD", "admin123")`,未设回退 `admin123`(保持与现有部署兼容)。账号已存在则跳过(幂等)。

- [ ] **Step 1: 写失败测试**

在 `gateway-admin/tests/test_auth.py` 末尾追加:

```python
async def test_ensure_default_admin_uses_env_password(monkeypatch, fake_redis):
    """ADMIN_INIT_PASSWORD 设了 -> 用该密码建 admin。"""
    monkeypatch.setenv("ADMIN_INIT_PASSWORD", "strong-pass-456")
    from auth import ensure_default_admin, verify_password
    await ensure_default_admin()
    stored = await fake_redis.hgetall("admin:admin")
    assert verify_password("strong-pass-456", stored["password_hash"]) is True
    assert verify_password("admin123", stored["password_hash"]) is False


async def test_ensure_default_admin_falls_back_to_default(monkeypatch, fake_redis):
    """没设 ADMIN_INIT_PASSWORD -> 回退 admin123。"""
    monkeypatch.delenv("ADMIN_INIT_PASSWORD", raising=False)
    from auth import ensure_default_admin, verify_password
    await ensure_default_admin()
    stored = await fake_redis.hgetall("admin:admin")
    assert verify_password("admin123", stored["password_hash"]) is True


async def test_ensure_default_admin_idempotent(monkeypatch, fake_redis):
    """admin 已存在 -> 不覆盖。"""
    monkeypatch.setenv("ADMIN_INIT_PASSWORD", "new-pass")
    from auth import ensure_default_admin, hash_password, verify_password
    await fake_redis.hset("admin:admin", mapping={"password_hash": hash_password("existing")})
    await ensure_default_admin()
    stored = await fake_redis.hgetall("admin:admin")
    assert verify_password("existing", stored["password_hash"]) is True
    assert verify_password("new-pass", stored["password_hash"]) is False
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd gateway-admin && uv run pytest tests/test_auth.py::test_ensure_default_admin_uses_env_password -v
```
Expected: FAIL(当前硬编码 `admin123`,env 设了 `strong-pass-456` 仍用 `admin123`,`verify_password("strong-pass-456")` 为 False)

- [ ] **Step 3: 实现**

修改 `gateway-admin/auth.py` 的 `ensure_default_admin`:

```python
async def ensure_default_admin() -> None:
    """Create the default admin account if none exists.

    Password from ADMIN_INIT_PASSWORD env (fallback admin123 for backward
    compat with existing deployments). Idempotent: skips if admin:admin exists.
    """
    from redis_client import get_redis
    r = get_redis()
    if await r.exists("admin:admin"):
        return
    password = os.environ.get("ADMIN_INIT_PASSWORD", "admin123")
    await r.hset("admin:admin", mapping={
        "password_hash": hash_password(password),
        "role": "admin",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    logger.warning("default_admin_created", service="gateway-admin",
                   note="change password immediately")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd gateway-admin && uv run pytest tests/test_auth.py -v
```
Expected: PASS(全部,含新增 3 个 + 现有)

- [ ] **Step 5: commit**

```bash
git add gateway-admin/auth.py gateway-admin/tests/test_auth.py
git commit -m "feat(admin): ensure_default_admin reads ADMIN_INIT_PASSWORD env"
```

---

### Task 2: 日志 file handler 改造(三个服务)

**Files:**
- Create: `gateway-admin/logging_config.py`, `gateway-proxy/logging_config.py`, `zabbix-mcp/logging_config.py`
- Modify: `gateway-admin/app.py:18-24`, `gateway-proxy/server.py:22-29`, `zabbix-mcp/server.py:27-54`
- Test: `gateway-admin/tests/test_logging_config.py`

**Interfaces:**
- Produces: `configure_logging(processors: list) -> None`。配置 structlog 用 stdlib `LoggerFactory`,stdlib logging 同时输出到 stdout 和(若 `LOG_FILE` 设)`RotatingFileHandler(10MB x 5)`。未设 `LOG_FILE` 时仅 stdout(本地开发不变)。

- [ ] **Step 1: 写失败测试**

创建 `gateway-admin/tests/test_logging_config.py`:

```python
"""Tests for logging_config: LOG_FILE enables a rotated file handler."""
import logging


def test_log_file_env_writes_to_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "svc.log"))
    import structlog
    from logging_config import configure_logging
    configure_logging([
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ])
    log = structlog.get_logger()
    log.info("test_event", key="value")
    for h in logging.getLogger().handlers:
        h.flush()
    content = (tmp_path / "svc.log").read_text()
    assert "test_event" in content
    assert "value" in content


def test_no_log_file_env_only_stdout(tmp_path, monkeypatch):
    """未设 LOG_FILE -> 没有 FileHandler,不抛错。"""
    monkeypatch.delenv("LOG_FILE", raising=False)
    import structlog
    from logging_config import configure_logging
    configure_logging([structlog.processors.JSONRenderer()])
    log = structlog.get_logger()
    log.info("ok_event")  # 不应抛异常
    assert not (tmp_path / "svc.log").exists()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd gateway-admin && uv run pytest tests/test_logging_config.py -v
```
Expected: FAIL(`ModuleNotFoundError: No module named 'logging_config'`)

- [ ] **Step 3: 实现 logging_config.py**

创建 `gateway-admin/logging_config.py`(三个服务内容相同,各自一份):

```python
"""Structured logging setup: structlog + stdlib with optional file output.

OBS-CORE-001: structured key=value. LOG_FILE env enables a rotated file
handler alongside stdout so container logs persist on the host volume.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

import structlog


def configure_logging(processors: list) -> None:
    """Configure structlog to emit structured JSON to stdout (+ optional file).

    processors: service-specific chain (e.g. with merge_contextvars /
        add_trace_context) appended before the shared renderers.
    LOG_FILE env: if set, also write to this path, rotated 10MB x 5.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = os.environ.get("LOG_FILE")
    if log_file:
        handlers.append(
            RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        )
    # force=True: replace any prior handlers (tests / re-init safe)
    logging.basicConfig(
        level=logging.INFO, handlers=handlers, format="%(message)s", force=True
    )
    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
```

复制到另两个服务:

```bash
cp gateway-admin/logging_config.py gateway-proxy/logging_config.py
cp gateway-admin/logging_config.py zabbix-mcp/logging_config.py
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd gateway-admin && uv run pytest tests/test_logging_config.py -v
```
Expected: PASS

- [ ] **Step 5: 改 gateway-admin/app.py 用 helper**

`gateway-admin/app.py:18-24` 替换原 `structlog.configure(...)` 块为:

```python
from logging_config import configure_logging

configure_logging([
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.JSONRenderer(),
])
```

(删掉原来的 `structlog.configure(processors=[...])` 6 行,保留 `logger = structlog.get_logger()`)

- [ ] **Step 6: 改 gateway-proxy/server.py 用 helper**

`gateway-proxy/server.py:22-29` 替换为:

```python
from logging_config import configure_logging

configure_logging([
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.JSONRenderer(),
])
```

- [ ] **Step 7: 改 zabbix-mcp/server.py 用 helper**

`zabbix-mcp/server.py` 的 `_configure_logging()` 函数(27-54 行)替换为:

```python
def _configure_logging() -> None:
    """Configure structlog with OTel trace context injection.

    OBS-CORR-001: 每条日志自动注入 trace_id + span_id。
    OBS-CORE-001: 所有日志结构化 key=value。
    """

    def add_trace_context(logger, method_name, event_dict):
        """从当前 OTel span 提取 trace_id/span_id 注入日志。"""
        span = trace.get_current_span()
        sc = span.get_span_context()
        if sc and sc.is_valid:
            event_dict["trace_id"] = format(sc.trace_id, "032x")
            event_dict["span_id"] = format(sc.span_id, "016x")
        return event_dict

    from logging_config import configure_logging
    configure_logging([
        structlog.contextvars.merge_contextvars,
        add_trace_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer() if LOG_FORMAT == "json"
        else structlog.dev.ConsoleRenderer(),
    ])
```

- [ ] **Step 8: 验证三个服务仍能 import**

```bash
cd gateway-admin && uv run python -c "import app; print('admin ok')"
cd ../gateway-proxy && uv run python -c "import server; print('proxy ok')" 2>&1 | tail -1
cd ../zabbix-mcp && ZABBIX_URL=http://x/api_jsonrpc.php ZABBIX_TOKEN=t uv run python -c "import server; print('zabbix ok')" 2>&1 | tail -1
```
Expected: 三个都打印 ok(无 import 错误)。proxy/zabbix 可能有 OTel 日志输出,无妨。

- [ ] **Step 9: 运行全部测试确认无回归**

```bash
cd /Users/sunweini/mcpstore/gateway-admin && uv run pytest -q
cd ../gateway-proxy && uv run pytest -q
cd ../zabbix-mcp && uv run pytest -q
```
Expected: 全部 PASS

- [ ] **Step 10: commit**

```bash
git add gateway-admin/logging_config.py gateway-proxy/logging_config.py zabbix-mcp/logging_config.py \
        gateway-admin/app.py gateway-proxy/server.py zabbix-mcp/server.py \
        gateway-admin/tests/test_logging_config.py
git commit -m "feat: structured logging with optional LOG_FILE handler for containers"
```

---

### Task 3: 基础镜像 Dockerfile.base

**Files:**
- Create: `deploy/Dockerfile.base`

**Interfaces:**
- Produces: 镜像 `mcp-base:latest`。含 python:3.12-slim + uv + node 20 + gcc。WORKDIR /app。

- [ ] **Step 1: 写 Dockerfile.base**

创建 `deploy/Dockerfile.base`:

```dockerfile
# MCP Gateway 基础镜像:Python 运行时 + uv + Node.js(给 admin-ui 构建)
# 所有服务镜像 FROM mcp-base:latest
FROM python:3.12-slim

# 编译依赖(部分 Python 包需要 gcc;bcrypt 等有预编译 wheel,留作保险)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv(从官方镜像复制二进制,无需 install 脚本)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Node.js 20(给 gateway-admin 的 admin-ui 构建)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
```

- [ ] **Step 2: 本地 build 验证**

```bash
cd /Users/sunweini/mcpstore && docker build -t mcp-base:latest -f deploy/Dockerfile.base .
```
Expected: build 成功(若本地 arm64 也行,基础镜像只验证语法与层)。若本地无 docker,跳到 Task 12 在服务器 build。

- [ ] **Step 3: 验证镜像内工具**

```bash
docker run --rm mcp-base:latest sh -c "python3 --version && uv --version && node --version"
```
Expected: 三者版本号输出(Python 3.12.x、uv、Node v20.x)

- [ ] **Step 4: commit**

```bash
git add deploy/Dockerfile.base
git commit -m "build: add mcp-base image (python 3.12 + uv + node 20)"
```

---

### Task 4: gateway-proxy Dockerfile + .dockerignore

**Files:**
- Create: `gateway-proxy/Dockerfile`, `gateway-proxy/.dockerignore`

**Interfaces:**
- Consumes: `mcp-base:latest`(Task 3)
- Produces: 镜像 `mcp-gateway-proxy`。CMD `uv run python server.py`。读取 env: REDIS_URL, GATEWAY_PORT, GATEWAY_HOST, PROMETHEUS_PORT, LOG_FILE。

- [ ] **Step 1: 写 .dockerignore**

创建 `gateway-proxy/.dockerignore`:

```
.venv/
__pycache__/
*.pyc
tests/
.git/
.pytest_cache/
```

> `uv.lock` 不忽略(`uv sync --frozen` 需要它)。

- [ ] **Step 2: 写 Dockerfile**

创建 `gateway-proxy/Dockerfile`:

```dockerfile
FROM mcp-base:latest
WORKDIR /app

# 依赖层(变更少,利用 cache)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 源码
COPY . ./

ENV GATEWAY_HOST=0.0.0.0
CMD ["uv", "run", "python", "server.py"]
```

- [ ] **Step 3: 本地 build 验证**

```bash
cd /Users/sunweini/mcpstore/gateway-proxy && docker build -t mcp-gateway-proxy .
```
Expected: build 成功

- [ ] **Step 4: commit**

```bash
git add gateway-proxy/Dockerfile gateway-proxy/.dockerignore
git commit -m "build(proxy): add Dockerfile + .dockerignore"
```

---

### Task 5: gateway-admin Dockerfile + .dockerignore

**Files:**
- Create: `gateway-admin/Dockerfile`, `gateway-admin/.dockerignore`

**Interfaces:**
- Consumes: `mcp-base:latest`(Task 3)
- Produces: 镜像 `mcp-gateway-admin`。CMD `uvicorn app:app`。镜像内含 `admin-ui/dist`。

- [ ] **Step 1: 写 .dockerignore**

创建 `gateway-admin/.dockerignore`:

```
.venv/
__pycache__/
*.pyc
tests/
.git/
.pytest_cache/
admin-ui/node_modules/
```

> `admin-ui/dist` **不**忽略(构建产物要进镜像;虽然 Dockerfile 里会重新 build,但本地 dist 不该污染镜像层——实际 Dockerfile 内 build 覆盖。为干净,加 `admin-ui/dist`):
```
.venv/
__pycache__/
*.pyc
tests/
.git/
.pytest_cache/
admin-ui/node_modules/
admin-ui/dist/
```

- [ ] **Step 2: 写 Dockerfile**

创建 `gateway-admin/Dockerfile`:

```dockerfile
FROM mcp-base:latest
WORKDIR /app

# 前端构建(node 层,package-lock 变了才重装)
COPY admin-ui/package.json admin-ui/package-lock.json ./admin-ui/
RUN cd admin-ui && npm ci
COPY admin-ui/ ./admin-ui/
RUN cd admin-ui && npm run build   # 产物 -> admin-ui/dist

# Python 依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 源码
COPY . ./

ENV ADMIN_PORT=8081
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081"]
```

- [ ] **Step 3: 本地 build 验证**

```bash
cd /Users/sunweini/mcpstore/gateway-admin && docker build -t mcp-gateway-admin .
```
Expected: build 成功(含 npm ci + vite build + uv sync)

- [ ] **Step 4: commit**

```bash
git add gateway-admin/Dockerfile gateway-admin/.dockerignore
git commit -m "build(admin): add Dockerfile + .dockerignore (npm build inline)"
```

---

### Task 6: zabbix-mcp Dockerfile + .dockerignore

**Files:**
- Create: `zabbix-mcp/Dockerfile`, `zabbix-mcp/.dockerignore`

**Interfaces:**
- Consumes: `mcp-base:latest`(Task 3)
- Produces: 镜像 `mcp-zabbix-mcp`。CMD `python server.py`。读取 env: ZABBIX_URL, ZABBIX_TOKEN, MCP_HOST, MCP_PORT, LOG_FILE。

- [ ] **Step 1: 写 .dockerignore**

创建 `zabbix-mcp/.dockerignore`:

```
.venv/
__pycache__/
*.pyc
tests/
.git/
.pytest_cache/
client.py
```

> `client.py`(测试用 client)不进生产镜像。

- [ ] **Step 2: 写 Dockerfile**

创建 `zabbix-mcp/Dockerfile`:

```dockerfile
FROM mcp-base:latest
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . ./

ENV MCP_HOST=0.0.0.0
CMD ["uv", "run", "python", "server.py"]
```

- [ ] **Step 3: 本地 build 验证**

```bash
cd /Users/sunweini/mcpstore/zabbix-mcp && docker build -t mcp-zabbix-mcp .
```
Expected: build 成功

- [ ] **Step 4: commit**

```bash
git add zabbix-mcp/Dockerfile zabbix-mcp/.dockerignore
git commit -m "build(zabbix-mcp): add Dockerfile + .dockerignore"
```

---

### Task 7: docker-compose.yml + config 模板

**Files:**
- Create: `deploy/docker-compose.yml`, `deploy/config/proxy.env.example`, `deploy/config/admin.env.example`, `deploy/config/zabbix.env.example`

**Interfaces:**
- Consumes: 三个服务镜像(Task 4-6)、redis:7-alpine
- Produces: `docker compose up -d` 拉起四容器。挂载宿主 `../config`、`../data/redis`、`../logs/<svc>`。

- [ ] **Step 1: 写 config 模板**

创建 `deploy/config/proxy.env.example`:

```
# gateway-proxy 可选配置
# OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

创建 `deploy/config/admin.env.example`:

```
# gateway-admin 配置
JWT_SECRET=CHANGE_ME_generate_with_openssl_rand_base64_32
JWT_EXPIRES=86400
ADMIN_INIT_PASSWORD=CHANGE_ME_strong_password
```

创建 `deploy/config/zabbix.env.example`:

```
# zabbix-mcp 配置(必填 ZABBIX_URL + ZABBIX_TOKEN)
ZABBIX_URL=http://your-zabbix/api_jsonrpc.php
ZABBIX_TOKEN=your-api-token
MCP_HOST=0.0.0.0
MCP_PORT=8000
LOG_FORMAT=json
```

- [ ] **Step 2: 写 docker-compose.yml**

创建 `deploy/docker-compose.yml`:

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

- [ ] **Step 3: 验证 compose 配置语法**

```bash
cd /Users/sunweini/mcpstore/deploy && docker compose config --quiet
```
Expected: 无输出(语法正确)。需要 config 文件存在,先建空的真实文件:
```bash
mkdir -p config data/redis logs/proxy logs/admin logs/zabbix-mcp
cp config/proxy.env.example config/proxy.env
cp config/admin.env.example config/admin.env
cp config/zabbix.env.example config/zabbix.env
```
再 `docker compose config --quiet`。Expected: 无错误输出。

- [ ] **Step 4: commit**

```bash
git add deploy/docker-compose.yml deploy/config/
git commit -m "feat(deploy): add docker-compose.yml + config templates"
```

---

### Task 8: init.sh 注册脚本

**Files:**
- Create: `deploy/init.sh`

**Interfaces:**
- Consumes: 已启动的 gateway-admin(8081)、compose 网络(zabbix-mcp 可达)
- Produces: 幂等脚本。登录 admin -> 注册 zabbix-mcp(URL=http://zabbix-mcp:8000/mcp)-> refresh-tools -> 创建 API token(read+write)-> 打印连接配置。

- [ ] **Step 1: 写 init.sh**

创建 `deploy/init.sh`:

```bash
#!/bin/bash
# 初始化 gateway:注册 zabbix-mcp + 创建 API token。幂等。
# 在宿主上运行(非容器内),通过 localhost:8081 调 admin API。
set -euo pipefail

ADMIN_HOST="${ADMIN_HOST:-http://localhost:8081}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-${ADMIN_INIT_PASSWORD:-admin123}}"
ZABBIX_MCP_URL="${ZABBIX_MCP_URL:-http://zabbix-mcp:8000/mcp}"
TOKEN_NAME="${TOKEN_NAME:-gateway-full}"

echo "=== 登录 admin ==="
TOK=$(curl -s -m5 -X POST "$ADMIN_HOST/api/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["token"] if "token" in d else d)')
echo "  token: ${TOK:0:20}..."

echo "=== 注册 zabbix-mcp(若不存在)==="
EXISTING=$(curl -s -m5 "$ADMIN_HOST/api/servers" -H "Authorization: Bearer $TOK" \
  | python3 -c "import sys,json; print(any(s['name']=='zabbix-mcp' for s in json.load(sys.stdin)))")
if [ "$EXISTING" = "True" ]; then
  echo "  zabbix-mcp 已注册,跳过"
else
  curl -s -m10 -X POST "$ADMIN_HOST/api/servers" \
    -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
    -d "{\"name\":\"zabbix-mcp\",\"url\":\"$ZABBIX_MCP_URL\",\"description\":\"Zabbix monitoring: alert patrol, maintenance, acknowledgment\"}" \
    > /dev/null
  echo "  已注册"
fi

echo "=== 刷新工具列表 ==="
curl -s -m15 -X POST "$ADMIN_HOST/api/servers/zabbix-mcp/refresh-tools" \
  -H "Authorization: Bearer $TOK" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"  tools: {len(d.get(\"tools\",[]))} 个")'

echo "=== 创建 API token(read+write)==="
# 幂等:列出已有 token,同名跳过
EXISTING_TOK=$(curl -s -m5 "$ADMIN_HOST/api/tokens" -H "Authorization: Bearer $TOK" \
  | python3 -c "import sys,json; print(any(t.get('name')=='$TOKEN_NAME' for t in json.load(sys.stdin)))" 2>/dev/null || echo "False")
if [ "$EXISTING_TOK" = "True" ]; then
  echo "  token '$TOKEN_NAME' 已存在(明文无法再取,如需新明文请删除后重建),跳过"
else
  curl -s -m10 -X POST "$ADMIN_HOST/api/tokens" \
    -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
    -d "{\"name\":\"$TOKEN_NAME\",\"permissions\":{\"zabbix-mcp\":{\"read\":true,\"write\":true}}}" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"  明文 token(只显示一次): {d.get(\"token\",\"?\")}")'
fi

echo ""
echo "=== MCP client 连接配置 ==="
echo "  URL: http://<server-ip>:8082/mcp"
echo "  Header: Authorization: Bearer <token>"
```

- [ ] **Step 2: 给执行权限**

```bash
chmod +x deploy/init.sh
```

- [ ] **Step 3: 语法检查**

```bash
bash -n deploy/init.sh
```
Expected: 无输出(语法正确)

- [ ] **Step 4: commit**

```bash
git add deploy/init.sh
git commit -m "feat(deploy): add init.sh for zabbix-mcp registration + token creation"
```

---

### Task 9: deploy.sh 一键部署脚本

**Files:**
- Create: `deploy/deploy.sh`(覆盖旧版)

**Interfaces:**
- Produces: 幂等部署脚本。检查 docker -> 生成 config(从模板,若不存在)-> 建目录 -> build base -> compose build -> up -> init。

- [ ] **Step 1: 写 deploy.sh**

创建 `deploy/deploy.sh`(覆盖):

```bash
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
```

- [ ] **Step 2: 给执行权限 + 语法检查**

```bash
chmod +x deploy/deploy.sh
bash -n deploy/deploy.sh
```
Expected: 无输出(语法正确)

- [ ] **Step 3: commit**

```bash
git add deploy/deploy.sh
git commit -m "feat(deploy): rewrite deploy.sh for container deployment"
```

---

### Task 10: 部署文档 README.md

**Files:**
- Create: `deploy/README.md`

- [ ] **Step 1: 写 README.md**

创建 `deploy/README.md`:

```markdown
# MCP Gateway 容器化部署

## 前置要求

- Docker 20.10+ 与 Docker Compose v2
- 服务器 linux/amd64
- 端口可用:8081(admin)、8082(proxy)、9465(metrics)

## 快速部署

```bash
# 1. 克隆仓库
git clone <repo-url> && cd mcpstore

# 2. 一键部署(自动生成 config、build、启动、初始化)
bash deploy/deploy.sh

# 3. 首次部署前,编辑 config 填入真实凭据
#    - config/zabbix.env: ZABBIX_URL, ZABBIX_TOKEN
#    - config/admin.env: ADMIN_INIT_PASSWORD
vim deploy/config/*.env

# 4. 重新初始化(注册 zabbix-mcp + 建 token)
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

## 架构

```
:8082 -> gateway-proxy -> zabbix-mcp:8000 (内部)
:8081 -> gateway-admin (API + Vue UI)
:9465 -> gateway-proxy metrics
redis:6379 (内部,共享存储)
```

服务间用容器名互访。zabbix-mcp、redis 不对外暴露。

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
```

- [ ] **Step 2: commit**

```bash
git add deploy/README.md
git commit -m "docs(deploy): container deployment guide"
```

---

### Task 11: 删除旧 systemd 脚本

**Files:**
- Delete: `deploy/setup-server.sh`, `deploy/systemd/gateway-proxy.service`, `deploy/systemd/gateway-admin.service`

- [ ] **Step 1: 删除文件**

```bash
cd /Users/sunweini/mcpstore
git rm deploy/setup-server.sh
git rm deploy/systemd/gateway-proxy.service deploy/systemd/gateway-admin.service
rmdir deploy/systemd 2>/dev/null || true
```

- [ ] **Step 2: 确认 deploy/ 结构**

```bash
ls -R deploy/
```
Expected: 无 systemd/ 目录,有 Dockerfile.base、docker-compose.yml、init.sh、deploy.sh、README.md、config/、(data/logs 运行时生成)

- [ ] **Step 3: commit**

```bash
git add -A deploy/
git commit -m "chore(deploy): remove deprecated systemd scripts"
```

---

### Task 12: 服务器迁移 + 全链路验证

**Files:** 无(在服务器 10.33.17.72 执行)

**Interfaces:**
- Consumes: 所有前置 task。服务器 docker 26.0、SSH key `~/.ssh/id_loginmonitor`。

- [ ] **Step 1: 推送分支到服务器仓库**

```bash
cd /Users/sunweini/mcpstore
git push origin container-deployment 2>/dev/null || echo "若无可推,用 scp 传仓库"
```
若服务器没 clone 仓库,用 tar 传:
```bash
git archive container-deployment | gzip > /tmp/mcpstore.tgz
scp -i ~/.ssh/id_loginmonitor -P 9166 /tmp/mcpstore.tgz root@10.33.17.72:/opt/mcp-gateway-src.tgz
```

- [ ] **Step 2: 服务器上停 systemd 服务 + 宿主 Redis**

```bash
ssh -i ~/.ssh/id_loginmonitor -p 9166 root@10.33.17.72 "set -e
systemctl stop gateway-proxy gateway-admin zabbix-mcp 2>/dev/null || true
systemctl disable gateway-proxy gateway-admin zabbix-mcp 2>/dev/null || true
systemctl stop redis 2>/dev/null || true
systemctl disable redis 2>/dev/null || true
# 清理 failed unit
systemctl reset-failed 2>/dev/null || true
echo 'systemd 服务已停'"
```

- [ ] **Step 3: 服务器上准备代码 + config**

```bash
ssh -i ~/.ssh/id_loginmonitor -p 9166 root@10.33.17.72 "set -e
cd /opt
rm -rf mcp-gateway-cfg && mkdir -p mcp-gateway-cfg
cd mcp-gateway-cfg
tar xzf /opt/mcp-gateway-src.tgz
# 准备 config
cd deploy
mkdir -p config data/redis logs/proxy logs/admin logs/zabbix-mcp
# 从旧 /etc/mcp-gateway 迁移 zabbix.env(含真实 ZABBIX_TOKEN)
if [ -f /etc/mcp-gateway/zabbix.env ]; then cp /etc/mcp-gateway/zabbix.env config/zabbix.env; else cp config/zabbix.env.example config/zabbix.env; fi
if [ -f /etc/mcp-gateway/admin.env ]; then cp /etc/mcp-gateway/admin.env config/admin.env; else cp config/admin.env.example config/admin.env; fi
cp config/proxy.env.example config/proxy.env
echo 'config 就绪'; ls config/"
```

- [ ] **Step 4: build + 启动**

```bash
ssh -i ~/.ssh/id_loginmonitor -p 9166 root@10.33.17.72 "cd /opt/mcp-gateway-cfg/deploy
# 若 admin.env 仍是占位,生成 JWT_SECRET
grep -q CHANGE_ME admin.env && sed -i \"s|CHANGE_ME_generate_with_openssl_rand_base64_32|\$(openssl rand -base64 32)|\" admin.env
docker build -t mcp-base:latest -f Dockerfile.base ..
docker compose build
docker compose up -d
sleep 5
docker compose ps"
```
Expected: 四容器 Up

- [ ] **Step 5: 跑 init.sh**

```bash
ssh -i ~/.ssh/id_loginmonitor -p 9166 root@10.33.17.72 "cd /opt/mcp-gateway-cfg/deploy
ADMIN_PASS=\$(grep '^ADMIN_INIT_PASSWORD=' config/admin.env | cut -d= -f2-) bash init.sh"
```
Expected: 注册 zabbix-mcp、拉到 8 个 tool、创建 token 并打印明文

- [ ] **Step 6: 全链路验证**

```bash
ssh -i ~/.ssh/id_loginmonitor -p 9166 root@10.33.17.72 "set -e
echo '--- 容器状态 ---'
docker compose -f /opt/mcp-gateway-cfg/deploy/docker-compose.yml ps
echo '--- admin health ---'
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8081/api/health
echo '--- proxy tools/list(用新 token)---'
# 从 init 输出取 token,或重新登录建一个测试 token
TOK=\$(curl -s -X POST http://localhost:8081/api/login -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"'\$(grep ADMIN_INIT_PASSWORD /opt/mcp-gateway-cfg/deploy/config/admin.env | cut -d= -f2-)'\\"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"token\"])')
curl -s -X POST http://localhost:8082/mcp -H \"Authorization: Bearer \$TOK\" -H 'Accept: application/json, text/event-stream' -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}' | head -c 200
echo ''
echo '--- metrics ---'
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:9465/metrics
echo '--- 本地日志文件 ---'
ls /opt/mcp-gateway-cfg/deploy/logs/*/ 2>/dev/null
echo '--- redis RDB ---'
ls /opt/mcp-gateway-cfg/deploy/data/redis/dump.rdb 2>/dev/null && echo 'RDB OK' || echo 'RDB 待生成'"
```
Expected:
- 四容器 Up
- admin health 200
- tools/list 返回 zabbix-mcp_* 工具
- metrics 200
- logs/ 下三个 .log 文件
- dump.rdb 存在(或稍后生成)

- [ ] **Step 7: 端到端 tool 调用**

用创建的 API token(Step 5 输出)调 `zabbix-mcp_list_active_problems`,确认真实返回 Zabbix 告警。

- [ ] **Step 8: 持久化验证**

```bash
ssh -i ~/.ssh/id_loginmonitor -p 9166 root@10.33.17.72 "cd /opt/mcp-gateway-cfg/deploy
docker compose restart zabbix-mcp
sleep 3
# config/data/logs 仍在
ls config/ data/redis/ logs/zabbix-mcp/"
```
Expected: 重启后配置/数据/日志文件仍在

- [ ] **Step 9: commit 验证记录(可选)**

无代码改动。在本地记录部署成功:
```bash
cd /Users/sunweini/mcpstore
# 更新记忆(由实现者记录)
```

---

## Self-Review 结果

**Spec 覆盖:**
- 基础镜像 -> Task 3 ✓
- 三个服务 Dockerfile -> Task 4/5/6 ✓
- compose.yml -> Task 7 ✓
- 本地持久化目录 -> Task 7(compose volumes)+ Task 9(deploy.sh 建目录)✓
- 日志改造(LOG_FILE handler)-> Task 2 ✓
- admin 密码 ADMIN_INIT_PASSWORD -> Task 1 ✓
- init.sh(注册 + token)-> Task 8 ✓
- 部署文档 -> Task 10 ✓
- 删除 systemd 脚本 -> Task 11 ✓
- 迁移步骤 -> Task 12 ✓
- proxy metrics 9465 对外 -> Task 7 compose ports ✓

**类型一致性:** `configure_logging(processors: list)` 在 Task 2 定义,三个服务调用处签名一致。`ensure_default_admin()` 签名未变(无参),调用处(app.py lifespan)不需改。

**修正:** Task 4 的 .dockerignore 初稿误列 `uv.lock`,已在 step 内说明删除该行——最终 .dockerignore 不含 `uv.lock`(sync --frozen 需要)。
