# Aliyun DNS MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 `aliyun-dns-mcp`（6 个 tool 管理多阿里云账户的 DNS 解析，账户级 read/write 权限）+ gateway-admin 两页管理界面（账户 CRUD、token×账户授权矩阵），接入 MCP Gateway。

**Architecture:** MCP 是账户级权限的权威——gateway 的 FastMCP proxy transport 自动把调用方 `Authorization` 头转发给后端（已从 fastmcp 源码验证：`transports.py` 的 `get_http_headers() | self.headers`，且 `dependencies.py` 的默认排除列表不含 authorization），MCP 读头 → SHA-256 → Redis `tokens:{hash}` 拿 token_id → 查 `aliyndns:token_accounts:{token_id}` 做账户级 read/write 校验 → 用该账户 AccessKey 调 Alidns SDK。gateway 保持零改动，只做工具可见性粗闸（server 级 read/write，由授权矩阵保存时自动同步 union）。

**Tech Stack:** FastMCP 4.0.0b1（MCP Protocol 2026-07-28, stateless HTTP）、Python >=3.12、uv（--prerelease=allow）、alibabacloud-alidns20150109 + alibabacloud-tea-openapi（RPC 签名交给 SDK）、redis.asyncio + fakeredis、structlog + OpenTelemetry + Prometheus、gateway-admin（FastAPI + Vue 3）。

## Global Constraints

从 spec（docs/superpowers/specs/2026-08-06-aliyun-dns-mcp-design.md）逐条复制，所有 task 隐含遵守：

1. **端口**：容器内 9054（9050-9500 最小未用），不映射宿主；PROMETHEUS_PORT 容器内 9464（compose 宿主端 9469）。开发前先登记根 CLAUDE.md 端口表。
2. **Server 命名**：`aliyun-dns-mcp`（小写/连字符，禁下划线）；server 描述一句话；写 tool docstring 含 `⚠️ 写操作`。
3. **Tool 读写分离**：`destructiveHint=True` → write，否则 read；漏标当 read。
4. **uv.lock 必须指向阿里云镜像**：`rm -f uv.lock && UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv lock`，验证 `grep -c mirrors.aliyun.com uv.lock > 0` 且 `files.pythonhosted.org = 0`。
5. **凭证安全**：AccessKey/Secret/token 明文禁入日志与 metric label（OBS-CORE-003）；httpx logger 提到 WARNING（SDK RPC 请求 URL query 含 AccessKeyId）。
6. **结构化日志**：structlog key=value，`service="aliyun-dns-mcp"`，错误带 `error` key（OBS-CORE-001/002）。
7. **代码注释**：写"为什么"不写"做了什么"（OBS-CORE-005）。
8. **Redis schema（权威）**：
   - `aliyndns:accounts:{account_id}` Hash → `{access_key_id, access_key_secret, description, region, enabled, created_at[, probe_error]}`
   - `aliyndns:accounts:index` Set → 全部 account_id
   - `aliyndns:token_accounts:{token_id}` Hash → `{account_id: '{"read": bool, "write": bool}'}`
   - `aliyndns:changed` Pub/Sub → `{"action": "upsert"|"delete", "key": "<完整Redis key含aliyndns:前缀>"}`
9. **write ⇒ read 不变式**：UI/API 保存时强制（write=true ⇒ read=true）；MCP 侧防御式判定 read = `read or write`。
10. **FastMCP v4 拒绝 `*args/**kwargs` 工具包装**：register() 必须显式具名包装（模板 §1.5）。
11. **stateless HTTP**：`stateless_http=True`；模块级懒加载单例（lifespan 不可靠）；pubsub listener 与 server 同 event loop。
12. **redis-py ≥6**：`get_message()` 不传 `ignore_subscribe=True`（参数已改名）；pubsub 断线必须重建订阅。
13. **MCP 校验失败用 ToolError**（error_type 前缀嵌入消息）；阿里云 API 错误返回结构化 `{"status": "error", "error_type", "message", "request_id"}`。

---

### Task 1: 脚手架 aliyun-dns-mcp（模板 + 依赖 + 端口登记）

**Files:**
- Create: `aliyun-dns-mcp/`（复制 `templates/mcp-template/` 整个目录）
- Modify: `aliyun-dns-mcp/pyproject.toml`、`aliyun-dns-mcp/server.py`、`templates/../CLAUDE.md`（根目录 `CLAUDE.md` 端口表）

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: `aliyun-dns-mcp/` 可导入目录（`uv run python -c "import server"` 成功），依赖含 alibabacloud SDK

- [ ] **Step 1: 复制模板并改 pyproject**

```bash
cp -r templates/mcp-template aliyun-dns-mcp
```

编辑 `aliyun-dns-mcp/pyproject.toml`：

```toml
[tool.uv]
prerelease = "allow"

[project]
name = "aliyun-dns-mcp"
version = "0.1.0"
description = "阿里云 DNS 解析管理 MCP：多账户托管、域名/解析查询、增删改解析，账户级读写权限"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "fastmcp==4.0.0b1",
    "httpx>=0.27,<1.0",
    "structlog>=24.0",
    "redis>=5.0",
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry-exporter-prometheus",
    "prometheus-client",
    "alibabacloud-alidns20150109",
    "alibabacloud-tea-openapi",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "fakeredis>=2.20",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 2: 安装依赖 + 重建阿里云镜像 lock**

```bash
cd aliyun-dns-mcp
uv sync --all-extras
rm -f uv.lock
UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv lock
# 验证（必须）：
grep -c mirrors.aliyun.com uv.lock   # > 0
grep -c files.pythonhosted.org uv.lock   # 必须为 0
```

- [ ] **Step 3: 验证 SDK 已装 + 确认请求模型类名（写代码前必做）**

```bash
uv run python -c "
from alibabacloud_alidns20150109 import models
import inspect
names = [n for n in dir(models) if n.endswith(('Request', 'Response')) and any(k in n for k in ('Domains', 'Record'))]
print(sorted(names))
"
```

预期出现：`AddDomainRecordRequest`、`UpdateDomainRecordRequest`、`DeleteDomainRecordRequest`、`DescribeDomainsRequest`、`DescribeDomainRecordsRequest`、`DescribeDomainRecordsResponse`、`DescribeDomainsResponse` 等。若类名与预期不同，后续 Task 4/6 的模型引用以实测为准（记下来）。

- [ ] **Step 4: server.py 端口改 9054 + 登记根 CLAUDE.md**

`aliyun-dns-mcp/server.py`：`MCP_PORT = int(os.environ.get("MCP_PORT", "9054"))`（模板占位 905x 处）。

编辑根 `CLAUDE.md` 端口表，加一行：

```markdown
| 9054 | aliyun-dns-mcp | 阿里云 DNS 解析管理（6 tools，规划） |
```

- [ ] **Step 5: 冒烟测试导入**

Run: `cd aliyun-dns-mcp && uv run python -c "import server; print(server.mcp.name)"`
Expected: 输出 `Aliyun DNS MCP`（模板 server.py 的 FastMCP 名先保持模板默认即可，Task 7 改名）。

- [ ] **Step 6: Commit**

```bash
git add aliyun-dns-mcp/ CLAUDE.md
git commit -m "chore(aliyun-dns-mcp): 脚手架（模板 + SDK 依赖 + 端口 9054 登记）"
```

---

### Task 2: account_store.py（Redis 账户凭证 + token 权限加载 + 热更新）

**Files:**
- Create: `aliyun-dns-mcp/account_store.py`
- Test: `aliyun-dns-mcp/tests/test_account_store.py`

**Interfaces:**
- Consumes: `redis.asyncio.Redis` 客户端（decode_responses=True，由 server.py 注入）
- Produces:
  - `class AccountStore(redis)` — `async start()`（load_all + 启动 listener）、`async close()`、`async load_all()`、`get_credentials(account_id) -> dict | None`（含 `access_key_id/access_key_secret/description/region/enabled`）、`account_exists(account_id) -> bool`、`account_ids() -> set[str]`、`get_token_perms(token_id) -> dict[str, dict]`（`{account_id: {"read": bool, "write": bool}}`，无则 `{}`）
  - Redis key 常量：`ACCOUNTS_INDEX = "aliyndns:accounts:index"`、`CHANGE_CHANNEL = "aliyndns:changed"`

- [ ] **Step 1: 写失败测试**

`aliyun-dns-mcp/tests/test_account_store.py`：

```python
"""AccountStore 测试：启动加载、热更新、token 权限缓存。"""
import asyncio
import json
import pytest

from account_store import AccountStore, ACCOUNTS_INDEX, CHANGE_CHANNEL


def _seed_account(r, account_id="acct1", secret="sk-secret"):
    import time
    r.hset(f"aliyndns:accounts:{account_id}", mapping={
        "access_key_id": "LTAI-test",
        "access_key_secret": secret,
        "description": "测试账户",
        "region": "cn-hangzhou",
        "enabled": "true",
        "created_at": "2026-08-06T00:00:00Z",
    })
    r.sadd(ACCOUNTS_INDEX, account_id)


def _seed_token_perms(r, token_id="tokid_1", account_id="acct1"):
    r.hset(f"aliyndns:token_accounts:{token_id}", account_id,
           json.dumps({"read": True, "write": False}))


@pytest.mark.asyncio
async def test_load_all_reads_accounts_and_index(fake_redis):
    _seed_account(fake_redis)
    store = AccountStore(fake_redis)
    await store.load_all()
    assert store.account_ids() == {"acct1"}
    creds = store.get_credentials("acct1")
    assert creds["access_key_id"] == "LTAI-test"
    assert creds["enabled"] is True


@pytest.mark.asyncio
async def test_credentials_normalize_enabled(fake_redis):
    _seed_account(fake_redis)
    fake_redis.hset("aliyndns:accounts:acct1", "enabled", "false")
    store = AccountStore(fake_redis)
    await store.load_all()
    assert store.get_credentials("acct1")["enabled"] is False


@pytest.mark.asyncio
async def test_get_token_perms_lazy_load(fake_redis):
    _seed_token_perms(fake_redis)
    store = AccountStore(fake_redis)
    assert store.get_token_perms("tokid_1") == {}  # 未加载 → 空
    await store.ensure_token_loaded("tokid_1")
    assert store.get_token_perms("tokid_1") == {"acct1": {"read": True, "write": False}}
    # 未授权 token → 空 dict
    await store.ensure_token_loaded("tokid_none")
    assert store.get_token_perms("tokid_none") == {}


@pytest.mark.asyncio
async def test_hot_reload_on_channel_message(fake_redis):
    _seed_account(fake_redis, account_id="acct1")
    store = AccountStore(fake_redis)
    await store.start()
    # 新增账户 + publish → 监听循环 reload
    _seed_account(fake_redis, account_id="acct2")
    await fake_redis.publish(CHANGE_CHANNEL, json.dumps({"action": "upsert", "key": "aliyndns:accounts:acct2"}))
    # 等待 listener 处理（poll 至多 2s）
    for _ in range(20):
        await asyncio.sleep(0.1)
        if store.account_ids() == {"acct1", "acct2"}:
            break
    assert store.account_ids() == {"acct1", "acct2"}
    await store.close()
```

注意 `fake_redis` fixture 在 `tests/conftest.py` 定义（Step 2）。`test_hot_reload_on_channel_message` 用 `store.start()`（启动 listener 任务并加载全量）并 `store.close()` 清理任务。

- [ ] **Step 2: conftest + 跑测试确认失败**

创建 `aliyun-dns-mcp/tests/conftest.py`：

```python
"""Shared fixtures: fakeredis（与 gateway-admin 测试同模式）。"""
import pytest
import fakeredis.aioredis


@pytest.fixture
async def fake_redis():
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield fake
    await fake.aclose()
```

Run: `cd aliyun-dns-mcp && uv run python -m pytest tests/test_account_store.py -q`
Expected: FAIL（`ModuleNotFoundError: account_store`）

- [ ] **Step 3: 实现 account_store.py**

```python
"""Redis 账户凭证 + token 账户权限存储，Pub/Sub 热更新。

MCP 是账户级权限的权威，这里持有全部账户凭证与 token→账户权限映射的
内存缓存；gateway-admin 写入 Redis 后 PUBLISH aliyndns:changed，本类
监听并全量重载（小规模，全量加载成本可忽略；热更新免重启）。

安全：AccessKey/Secret 明文只存在于 Redis 值与内存，禁入日志/metric。
"""
import asyncio
import json

import structlog

logger = structlog.get_logger()

ACCOUNTS_INDEX = "aliyndns:accounts:index"
CHANGE_CHANNEL = "aliyndns:changed"

# 凭证字段白名单——load 时只取这些键，防脏数据注入意外字段
_CRED_FIELDS = ("access_key_id", "access_key_secret", "description", "region", "enabled")


class AccountStore:
    def __init__(self, redis):
        self._redis = redis
        self._accounts: dict[str, dict] = {}
        self._token_perms_cache: dict[str, dict[str, dict]] = {}
        self._listener_task: asyncio.Task | None = None
        self._listening = False

    async def start(self) -> None:
        """加载全量 + 启动热更新监听。listener 必须与 server 同 event loop。"""
        await self.load_all()
        self._listening = True
        self._listener_task = asyncio.create_task(self._listen())

    async def close(self) -> None:
        self._listening = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

    async def load_all(self) -> None:
        r = self._redis
        accounts = {}
        for account_id in await r.smembers(ACCOUNTS_INDEX):
            data = await r.hgetall(f"aliyndns:accounts:{account_id}")
            if not data:
                continue
            accounts[account_id] = self._normalize_creds(data)
        self._accounts = accounts
        # 权限映射懒加载缓存：账户变更会连带清缓存（权限值依赖账户存在性）
        self._token_perms_cache.clear()
        logger.info("account_store_loaded", service="aliyun-dns-mcp", accounts=len(accounts))

    @staticmethod
    def _normalize_creds(data: dict) -> dict:
        return {
            "access_key_id": data.get("access_key_id", ""),
            "access_key_secret": data.get("access_key_secret", ""),
            "description": data.get("description", ""),
            "region": data.get("region", "cn-hangzhou"),
            "enabled": data.get("enabled", "true") == "true",
        }

    # ── 同步读（内存缓存）────────────────────────────────────────
    def get_credentials(self, account_id: str) -> dict | None:
        return self._accounts.get(account_id)

    def account_exists(self, account_id: str) -> bool:
        return account_id in self._accounts

    def account_ids(self) -> set[str]:
        return set(self._accounts)

    def get_token_perms(self, token_id: str) -> dict[str, dict]:
        """token 的账户级权限 {account_id: {"read", "write"}}；未加载返回 {}。

        只读缓存不插入——未加载的 key 保持缺失，ensure_token_loaded 据此
        判断需要真正加载（若在此插入空 dict，加载入口会误判已加载而跳过）。
        """
        return self._token_perms_cache.get(token_id, {})

    async def load_token_perms(self, token_id: str) -> dict[str, dict]:
        """从 Redis 加载某个 token 的权限（缓存未命中时调用）。"""
        raw = await self._redis.hgetall(f"aliyndns:token_accounts:{token_id}")
        perms = {}
        for account_id, payload in raw.items():
            try:
                p = json.loads(payload)
                perms[account_id] = {"read": bool(p.get("read")), "write": bool(p.get("write"))}
            except json.JSONDecodeError:
                logger.warning("token_perms_corrupt", service="aliyun-dns-mcp",
                               token_id=token_id, account_id=account_id)
        self._token_perms_cache[token_id] = perms
        return perms

    async def ensure_token_loaded(self, token_id: str) -> None:
        """确保某 token 的权限已加载（懒加载入口，auth 校验前调用）。"""
        if token_id not in self._token_perms_cache:
            await self.load_token_perms(token_id)

    # ── 热更新监听 ────────────────────────────────────────────────
    async def _listen(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(CHANGE_CHANNEL)
        while self._listening:
            try:
                msg = await pubsub.get_message(timeout=30)
                if msg and msg.get("type") == "message":
                    await self.load_all()
            except Exception:
                # redis-py 连接死后不自动重连：必须重建 pubsub 订阅，
                # 否则热更新永久失效只能重启进程（serpapi 踩坑教训）
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(CHANGE_CHANNEL)
                await asyncio.sleep(5)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd aliyun-dns-mcp && uv run python -m pytest tests/test_account_store.py -q`
Expected: PASS（4 tests）

- [ ] **Step 5: Commit**

```bash
git add aliyun-dns-mcp/account_store.py aliyun-dns-mcp/tests/
git commit -m "feat(aliyun-dns-mcp): AccountStore Redis 账户/权限加载 + pubsub 热更新"
```

---

### Task 3: auth.py（token 验证 + 账户级 read/write 校验）

**Files:**
- Create: `aliyun-dns-mcp/auth.py`
- Test: `aliyun-dns-mcp/tests/test_auth.py`

**Interfaces:**
- Consumes: `AccountStore`（Task 2）、redis client、`fastmcp.server.dependencies.get_http_headers`
- Produces:
  - `def hash_token(token: str) -> str` — SHA-256 hex（与 gateway-proxy auth.hash_token 一致）
  - `def extract_token(headers: dict | None) -> str | None` — 从 headers 取 Bearer token
  - `class PermissionChecker(store: AccountStore, redis)`:
    - `async require(account_id: str, mode: str) -> None` — 校验通过返回，失败 `raise ToolError`（消息含 error_type 前缀：`invalid_token` / `no_permission`）
    - `async allowed_accounts() -> list[dict]` — `[{account_id, description, read, write}]`（只含已托管且 enabled 的账户）

- [ ] **Step 1: 写失败测试**

`aliyun-dns-mcp/tests/test_auth.py`：

```python
"""PermissionChecker 测试：token 验证 + 账户级 read/write 判定。"""
import hashlib
import json

import pytest
from fastmcp.exceptions import ToolError

from account_store import AccountStore, ACCOUNTS_INDEX
from auth import PermissionChecker, hash_token, extract_token


def _seed_token(r, token="tok_abc", token_id="tokid_1"):
    r.hset(f"tokens:{hash_token(token)}", mapping={
        "id": token_id, "name": "test-token", "permissions": "{}",
    })


def _seed_account(r, account_id="acct1"):
    r.hset(f"aliyndns:accounts:{account_id}", mapping={
        "access_key_id": "LTAI-test", "access_key_secret": "sk", "description": "账户1",
        "region": "cn-hangzhou", "enabled": "true",
    })
    r.sadd(ACCOUNTS_INDEX, account_id)


def _seed_token_perms(r, token_id, mapping):
    r.hset(f"aliyndns:token_accounts:{token_id}",
           mapping={a: json.dumps(p) for a, p in mapping.items()})


def test_hash_token_sha256():
    h = hash_token("tok_abc")
    assert len(h) == 64
    assert h == hashlib.sha256(b"tok_abc").hexdigest()


def test_extract_token():
    assert extract_token({"authorization": "Bearer abc123"}) == "abc123"
    assert extract_token({"authorization": "bearer abc123"}) == "abc123"
    assert extract_token({}) is None
    assert extract_token(None) is None


def make_checker(redis):
    store = AccountStore(redis)
    return PermissionChecker(store, redis), store


@pytest.mark.asyncio
async def test_require_missing_header_denied(fake_redis, monkeypatch):
    checker, _ = make_checker(fake_redis)
    monkeypatch.setattr("auth.get_http_headers", lambda include_all=False: {})
    with pytest.raises(ToolError) as e:
        await checker.require("acct1", "read")
    assert "invalid_token" in str(e.value)


@pytest.mark.asyncio
async def test_require_invalid_token_denied(fake_redis, monkeypatch):
    checker, _ = make_checker(fake_redis)
    monkeypatch.setattr("auth.get_http_headers",
                        lambda include_all=False: {"authorization": "Bearer bad-token"})
    with pytest.raises(ToolError) as e:
        await checker.require("acct1", "read")
    assert "invalid_token" in str(e.value)


@pytest.mark.asyncio
async def test_require_read_allowed_write_denied(fake_redis, monkeypatch):
    _seed_token(fake_redis)
    _seed_account(fake_redis)
    _seed_token_perms(fake_redis, "tokid_1", {"acct1": {"read": True, "write": False}})
    checker, store = make_checker(fake_redis)
    await store.load_all()
    monkeypatch.setattr("auth.get_http_headers",
                        lambda include_all=False: {"authorization": "Bearer tok_abc"})
    await checker.require("acct1", "read")  # 不抛
    with pytest.raises(ToolError) as e:
        await checker.require("acct1", "write")
    assert "no_permission" in str(e.value)


@pytest.mark.asyncio
async def test_require_unlisted_account_denied(fake_redis, monkeypatch):
    _seed_token(fake_redis)
    _seed_account(fake_redis)
    _seed_token_perms(fake_redis, "tokid_1", {"acct2": {"read": True, "write": False}})
    checker, store = make_checker(fake_redis)
    await store.load_all()
    monkeypatch.setattr("auth.get_http_headers",
                        lambda include_all=False: {"authorization": "Bearer tok_abc"})
    with pytest.raises(ToolError) as e:
        await checker.require("acct1", "read")
    assert "no_permission" in str(e.value)


@pytest.mark.asyncio
async def test_allowed_accounts_filters_disabled(fake_redis, monkeypatch):
    _seed_token(fake_redis)
    _seed_account(fake_redis, "acct1")
    _seed_account(fake_redis, "acct2")
    fake_redis.hset("aliyndns:accounts:acct2", "enabled", "false")
    _seed_token_perms(fake_redis, "tokid_1", {
        "acct1": {"read": True, "write": True},
        "acct2": {"read": True, "write": False},
    })
    checker, store = make_checker(fake_redis)
    await store.load_all()
    monkeypatch.setattr("auth.get_http_headers",
                        lambda include_all=False: {"authorization": "Bearer tok_abc"})
    result = await checker.allowed_accounts()
    assert [a["account_id"] for a in result] == ["acct1"]
    assert result[0]["read"] is True and result[0]["write"] is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd aliyun-dns-mcp && uv run python -m pytest tests/test_auth.py -q`
Expected: FAIL（`ModuleNotFoundError: auth`）

- [ ] **Step 3: 实现 auth.py**

```python
"""Token 验证 + 账户级 read/write 校验（MCP 是账户级权限的权威）。

gateway 只做 server 级工具可见性粗闸；本模块基于 proxy 转发来的
Authorization 头验证 token（与 gateway 同一套 Redis tokens:{hash} 存储，
hash 算法一致），再查 AccountStore 的账户级权限做精细校验——这是
防御纵深：绕过 gateway 直连（部署禁止，容器不映射宿主）也会被拒。
"""
import hashlib

import structlog
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

from account_store import AccountStore

logger = structlog.get_logger()


def hash_token(token: str) -> str:
    """SHA-256 hex digest（与 gateway-proxy auth.hash_token 一致）。"""
    return hashlib.sha256(token.encode()).hexdigest()


def extract_token(headers: dict | None) -> str | None:
    """从 headers 取 Bearer token；无 Authorization 头返回 None。"""
    if not headers:
        return None
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _deny(error_type: str, message: str) -> ToolError:
    """统一错误构造：消息以 error_type 开头，client 可解析。"""
    return ToolError(f"permission denied: {error_type}: {message}")


class PermissionChecker:
    def __init__(self, store: AccountStore, redis):
        self._store = store
        self._redis = redis

    async def _token_id(self, headers: dict) -> str | None:
        """验证 Authorization token 并返回 token_id；无效返回 None。"""
        token = extract_token(headers)
        if not token:
            return None
        data = await self._redis.hgetall(f"tokens:{hash_token(token)}")
        if not data:
            return None
        return data.get("id")

    async def require(self, account_id: str, mode: str) -> None:
        """校验调用者对该账户有 mode（read/write）权限；失败 raise ToolError。

        read 判定为 read or write——write 隐含 read 是不变式，但 Redis 可能
        被手改出违规数据，这里防御式判定（spec §3.1）。
        """
        headers = get_http_headers(include_all=True)
        token_id = await self._token_id(headers)
        if not token_id:
            raise _deny("invalid_token", "missing or invalid Authorization token")
        await self._store.ensure_token_loaded(token_id)
        perms = self._store.get_token_perms(token_id)
        acct_perm = perms.get(account_id)
        if not acct_perm:
            raise _deny("no_permission", f"account '{account_id}' not granted to this token")
        allowed = bool(acct_perm.get("write")) if mode == "write" else bool(acct_perm.get("read") or acct_perm.get("write"))
        if not allowed:
            raise _deny("no_permission", f"token lacks {mode} permission on account '{account_id}'")

    async def allowed_accounts(self) -> list[dict]:
        """当前 token 可访问的账户清单（list_accounts 用），含 read/write 标记。"""
        headers = get_http_headers(include_all=True)
        token_id = await self._token_id(headers)
        if not token_id:
            raise _deny("invalid_token", "missing or invalid Authorization token")
        await self._store.ensure_token_loaded(token_id)
        out = []
        for account_id, perm in self._store.get_token_perms(token_id).items():
            creds = self._store.get_credentials(account_id)
            if not creds or not creds["enabled"]:
                continue  # 只列托管中且启用的账户
            out.append({
                "account_id": account_id,
                "description": creds["description"],
                "read": bool(perm.get("read") or perm.get("write")),
                "write": bool(perm.get("write")),
            })
        return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd aliyun-dns-mcp && uv run python -m pytest tests/test_auth.py -q`
Expected: PASS（7 tests）

- [ ] **Step 5: Commit**

```bash
git add aliyun-dns-mcp/auth.py aliyun-dns-mcp/tests/test_auth.py
git commit -m "feat(aliyun-dns-mcp): PermissionChecker token 验证 + 账户级 read/write 校验"
```

---

### Task 4: aliyun_client.py（Alidns SDK 封装 + 错误分类 + 客户端工厂）

**Files:**
- Create: `aliyun-dns-mcp/aliyun_client.py`
- Test: `aliyun-dns-mcp/tests/test_aliyun_client.py`

**Interfaces:**
- Consumes: `AccountStore.get_credentials`（Task 2）；SDK（Task 1 已装，请求模型类名以 Task 1 Step 3 实测为准）
- Produces:
  - `class AlidnsError(Exception)` — `.error_type`（`invalid_credential`/`throttled`/`not_found`/`api_error`/`network_error`）、`.request_id`
  - `class AlidnsClient(credentials: dict)` — `async describe_domains(page_size=100, page_num=1) -> list[dict]`、`async describe_domain_records(domain_name, page_size=100, page_num=1) -> list[dict]`、`async add_domain_record(domain_name, rr, type, value, ttl=600, priority=None) -> str`、`async update_domain_record(record_id, rr=None, type=None, value=None, ttl=None, priority=None) -> None`、`async delete_domain_record(record_id) -> None`；同步 SDK 调用走 `asyncio.to_thread`
  - `def classify_error(exc) -> str` — SDK 异常 → error_type
  - `class ClientFactory(store: AccountStore)` — `get(account_id) -> AlidnsClient`（凭证缓存，变化自动重建；账户缺失/禁用 raise AlidnsError(`account_not_found`/`account_disabled`)）

- [ ] **Step 1: 写失败测试**

`aliyun-dns-mcp/tests/test_aliyun_client.py`：

```python
"""AlidnsClient 封装测试：mock 内部 SDK 对象，验证参数映射与错误分类。"""
import pytest

from aliyun_client import AlidnsError, AlidnsClient, ClientFactory, classify_error
from account_store import AccountStore


class FakeSDKResponse:
    """模拟 SDK 响应对象（.body 链）。"""
    def __init__(self, body):
        self.body = body


class FakeSDKClient:
    """记录调用、按脚本返回/抛错，模拟 alibabacloud 同步 client。"""
    def __init__(self, script=None):
        self.calls = []
        self.script = script or {}

    def describe_domains_with_options(self, request):
        self.calls.append(("describe_domains", request))
        if "domains_error" in self.script:
            raise self.script["domains_error"]
        domain = type("D", (), {"domain_name": "example.com", "dns_servers": ["ns1"], "record_count": 2})()
        return FakeSDKResponse(type("B", (), {"domains": type("L", (), {"domain": [domain]})})())

    def describe_domain_records_with_options(self, request):
        self.calls.append(("describe_domain_records", request))
        rec = type("R", (), {"record_id": "r1", "rr": "@", "type": "A", "value": "1.2.3.4",
                             "ttl": 600, "priority": None, "status": "ENABLE"})()
        return FakeSDKResponse(type("B", (), {"domain_records": type("L", (), {"record": [rec]})})())

    def add_domain_record_with_options(self, request):
        self.calls.append(("add_domain_record", request))
        return FakeSDKResponse(type("B", (), {"record_id": "new-1"})())


class FakeCredentialsStore:
    def __init__(self, creds):
        self._creds = creds

    def get_credentials(self, account_id):
        return self._creds.get(account_id)


def test_classify_error():
    err = type("E", (), {"code": "InvalidAccessKeyId.NotFound"})()
    assert classify_error(err) == "invalid_credential"
    err2 = type("E", (), {"code": "Throttling.User"})()
    assert classify_error(err2) == "throttled"
    err3 = type("E", (), {"code": "SomethingElse"})()
    assert classify_error(err3) == "api_error"


@pytest.mark.asyncio
async def test_describe_domains_maps_response(monkeypatch):
    sdk = FakeSDKClient()
    monkeypatch.setattr("aliyun_client.AlidnsClient._make_sdk", lambda self: sdk)
    client = AlidnsClient({"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True})
    domains = await client.describe_domains(page_size=10, page_num=1)
    assert domains == [{"domain_name": "example.com", "dns_servers": ["ns1"], "record_count": 2}]
    assert sdk.calls[0][0] == "describe_domains"


@pytest.mark.asyncio
async def test_add_domain_record_returns_id(monkeypatch):
    sdk = FakeSDKClient()
    monkeypatch.setattr("aliyun_client.AlidnsClient._make_sdk", lambda self: sdk)
    client = AlidnsClient({"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True})
    record_id = await client.add_domain_record("example.com", "www", "A", "1.2.3.4", ttl=600)
    assert record_id == "new-1"
    req = sdk.calls[0][1]
    assert req.domain_name == "example.com" and req.rr == "www" and req.type == "A"


@pytest.mark.asyncio
async def test_aliyun_error_wrapped(monkeypatch):
    sdk = FakeSDKClient(script={"domains_error": type("E", (), {"code": "Throttling.User", "request_id": "req-1"})()})
    monkeypatch.setattr("aliyun_client.AlidnsClient._make_sdk", lambda self: sdk)
    client = AlidnsClient({"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True})
    with pytest.raises(AlidnsError) as e:
        await client.describe_domains()
    assert e.value.error_type == "throttled"
    assert e.value.request_id == "req-1"


def test_client_factory_caches_and_rebuilds():
    creds_a = {"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True}
    store = FakeCredentialsStore({"acct1": creds_a})
    factory = ClientFactory(store)
    c1 = factory.get("acct1")
    c2 = factory.get("acct1")
    assert c1 is c2  # 缓存
    # 凭证变化（模拟热更新）→ 重建
    store._creds["acct1"] = {**creds_a, "access_key_secret": "new"}
    c3 = factory.get("acct1")
    assert c3 is not c1


def test_client_factory_missing_or_disabled():
    store = FakeCredentialsStore({})
    factory = ClientFactory(store)
    with pytest.raises(AlidnsError) as e:
        factory.get("ghost")
    assert e.value.error_type == "account_not_found"
    store2 = FakeCredentialsStore({"acct1": {"access_key_id": "a", "access_key_secret": "s",
                                             "region": "cn-hangzhou", "enabled": False}})
    factory2 = ClientFactory(store2)
    with pytest.raises(AlidnsError) as e2:
        factory2.get("acct1")
    assert e2.value.error_type == "account_disabled"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd aliyun-dns-mcp && uv run python -m pytest tests/test_aliyun_client.py -q`
Expected: FAIL（`ModuleNotFoundError: aliyun_client`）

- [ ] **Step 3: 实现 aliyun_client.py**

```python
"""Alidns SDK 封装：每账户一个 client，同步 SDK 走 asyncio.to_thread。

用官方 SDK（alibabacloud-alidns20150109 + tea-openapi）而不是裸 HTTP：
RPC 签名/端点选择/错误对象解析交给 SDK，MCP 层只做错误分类与 trace。
SDK 是同步 API，异步工具里用 asyncio.to_thread 防阻塞 event loop。

安全：SDK RPC 请求 URL query 含 AccessKeyId——httpx logger 必须提到
WARNING（logging_config 处理），日志只记 account_id 不记凭证。
"""
import asyncio

import structlog
from opentelemetry import trace

logger = structlog.get_logger()
tracer = trace.get_tracer("aliyun-dns-mcp")

ALIDNS_ENDPOINT = "alidns.cn-hangzhou.aliyuncs.com"


class AlidnsError(Exception):
    """阿里云 API 调用失败（已分类）。error_type 供工具层映射对外错误。"""

    def __init__(self, error_type: str, message: str, request_id: str | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.request_id = request_id


def classify_error(exc: Exception) -> str:
    """SDK 异常 → error_type。

    错误码为示例，以实测为准（spec §7.1）——SDK 异常对象带 .code 与
    .message，这里组合文本匹配，避免裸 code 匹配漏掉变体。
    """
    code = str(getattr(exc, "code", ""))
    msg = str(exc)
    combined = (code + " " + msg).lower()
    if any(k in combined for k in ("invalidaccesskeyid", "forbidden", "signaturedoesnotmatch", "incompleteSignature")):
        return "invalid_credential"
    if "throttling" in combined or "qps" in combined:
        return "throttled"
    if "domain" in combined and "exist" in combined:
        return "not_found"
    return "api_error"


class AlidnsClient:
    def __init__(self, credentials: dict):
        self._credentials = credentials
        self._sdk = self._make_sdk()

    def _make_sdk(self):
        """构造 SDK client。独立方法便于测试 monkeypatch。"""
        from alibabacloud_tea_openapi import models as open_api_models
        from alibabacloud_alidns20150109 import client as alidns_client
        return alidns_client.Client(open_api_models.Config(
            access_key_id=self._credentials["access_key_id"],
            access_key_secret=self._credentials["access_key_secret"],
            endpoint=self._credentials.get("region") == "cn-hangzhou" and ALIDNS_ENDPOINT or ALIDNS_ENDPOINT,
        ))

    async def _call(self, api_name: str, request, span_name: str):
        def run():
            fn = getattr(self._sdk, api_name)
            return fn(request)

        with tracer.start_as_current_span(span_name) as span:
            span.set_attribute("operation.type", "aliyun_api")
            try:
                return await asyncio.to_thread(run)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
                err_type = classify_error(exc)
                logger.error("aliyun_api_error", service="aliyun-dns-mcp",
                             api=api_name, error_type=err_type, error=str(exc))
                raise AlidnsError(err_type, str(exc), getattr(exc, "request_id", None)) from exc

    @staticmethod
    def _body(resp):
        return resp.body

    async def describe_domains(self, page_size: int = 100, page_num: int = 1) -> list[dict]:
        from alibabacloud_alidns20150109 import models
        req = models.DescribeDomainsRequest(page_size=page_size, page_number=page_num)
        resp = await self._call("describe_domains_with_options", req, "aliyun_client.describe_domains")
        domains = self._body(resp).domains.domain or []
        return [{
            "domain_name": d.domain_name,
            "dns_servers": list(getattr(d, "dns_servers", None) or []),
            "record_count": getattr(d, "record_count", None),
        } for d in domains]

    async def describe_domain_records(self, domain_name: str, page_size: int = 100,
                                      page_num: int = 1) -> list[dict]:
        from alibabacloud_alidns20150109 import models
        req = models.DescribeDomainRecordsRequest(
            domain_name=domain_name, page_size=page_size, page_number=page_num)
        resp = await self._call("describe_domain_records_with_options", req,
                                "aliyun_client.describe_domain_records")
        records = self._body(resp).domain_records.record or []
        return [{
            "record_id": r.record_id,
            "rr": r.rr,
            "type": r.type,
            "value": r.value,
            "ttl": getattr(r, "ttl", None),
            "priority": getattr(r, "priority", None),
            "status": getattr(r, "status", None),
        } for r in records]

    async def add_domain_record(self, domain_name: str, rr: str, type: str, value: str,
                                ttl: int = 600, priority: int | None = None) -> str:
        from alibabacloud_alidns20150109 import models
        req = models.AddDomainRecordRequest(
            domain_name=domain_name, rr=rr, type=type, value=value, ttl=ttl)
        if priority is not None:
            req.priority = priority
        resp = await self._call("add_domain_record_with_options", req, "aliyun_client.add_domain_record")
        return self._body(resp).record_id

    async def update_domain_record(self, record_id: str, rr: str | None = None,
                                   type: str | None = None, value: str | None = None,
                                   ttl: int | None = None, priority: int | None = None) -> None:
        from alibabacloud_alidns20150109 import models
        req = models.UpdateDomainRecordRequest(record_id=record_id)
        # 只更新传入的字段：阿里云要求全量语义，但工具层允许部分更新——
        # 未传字段不覆盖（SDK 请求对象不设值即不发送）
        if rr is not None:
            req.rr = rr
        if type is not None:
            req.type = type
        if value is not None:
            req.value = value
        if ttl is not None:
            req.ttl = ttl
        if priority is not None:
            req.priority = priority
        await self._call("update_domain_record_with_options", req, "aliyun_client.update_domain_record")

    async def delete_domain_record(self, record_id: str) -> None:
        from alibabacloud_alidns20150109 import models
        req = models.DeleteDomainRecordRequest(record_id=record_id)
        await self._call("delete_domain_record_with_options", req, "aliyun_client.delete_domain_record")


class ClientFactory:
    """按账户缓存 AlidnsClient；凭证变化（热更新后）自动重建。

    缓存键比较的是完整凭证 dict——AccessKeySecret 轮换会触发重建，
    无需额外失效通知。
    """

    def __init__(self, store: AccountStore):
        self._store = store
        self._cache: dict[str, AlidnsClient] = {}
        self._cache_creds: dict[str, dict] = {}

    def get(self, account_id: str) -> AlidnsClient:
        creds = self._store.get_credentials(account_id)
        if not creds:
            raise AlidnsError("account_not_found", f"account '{account_id}' not managed")
        if not creds["enabled"]:
            raise AlidnsError("account_disabled", f"account '{account_id}' disabled")
        cached = self._cache.get(account_id)
        if cached is not None and self._cache_creds.get(account_id) == creds:
            return cached
        client = AlidnsClient(creds)
        self._cache[account_id] = client
        self._cache_creds[account_id] = creds
        return client
```

注：`_make_sdk` 里 endpoint 表达式多余（恒为 ALIDNS_ENDPOINT），**实现时简化为** `endpoint=ALIDNS_ENDPOINT`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd aliyun-dns-mcp && uv run python -m pytest tests/test_aliyun_client.py -q`
Expected: PASS（6 tests）

- [ ] **Step 5: Commit**

```bash
git add aliyun-dns-mcp/aliyun_client.py aliyun-dns-mcp/tests/test_aliyun_client.py
git commit -m "feat(aliyun-dns-mcp): AlidnsClient SDK 封装 + 错误分类 + ClientFactory 缓存"
```

---

### Task 5: 读工具（list_accounts + list_domains）+ tools 注册骨架

**Files:**
- Create: `aliyun-dns-mcp/tools/__init__.py`、`aliyun-dns-mcp/tools/accounts.py`、`aliyun-dns-mcp/tools/domains.py`
- Test: `aliyun-dns-mcp/tests/test_tools_read.py`

**Interfaces:**
- Consumes: `PermissionChecker`（Task 3）、`ClientFactory` + `AlidnsError`（Task 4）
- Produces:
  - `tools/__init__.py`：`def register_tools(mcp, get_ctx, metrics=None) -> None`；`_metrics_wrapper(tool_name)`（记录 REQUESTS_TOTAL/REQUEST_DURATION/ERRORS_TOTAL/IN_FLIGHT，从 `telemetry` 导入，缺失则 None——**telemetry.py 在 Task 7 实现，本任务先用 None 占位并加 try/except ImportError 保护**）
  - `tools/accounts.py`：`async list_accounts(*, ctx=None) -> dict`；`def register(mcp, get_ctx, metrics=None)`
  - `tools/domains.py`：`async list_domains(account_id: str, *, ctx=None) -> dict`；`def register(...)`
  - `ToolContext` dataclass（放 `tools/__init__.py`）：`.checker`、`.clients`

- [ ] **Step 1: 写失败测试**

`aliyun-dns-mcp/tests/test_tools_read.py`：

```python
"""读工具测试：list_accounts / list_domains，注入 fake ctx。"""
import pytest
from fastmcp.exceptions import ToolError

from tools import ToolContext
from tools.accounts import list_accounts
from tools.domains import list_domains
from aliyun_client import AlidnsError


class FakeChecker:
    def __init__(self, accounts=None, denied=None):
        self.accounts = accounts or []
        self.denied = set(denied or [])

    async def require(self, account_id, mode):
        if account_id in self.denied:
            raise ToolError(f"permission denied: no_permission: account '{account_id}'")

    async def allowed_accounts(self):
        return self.accounts


class FakeClient:
    async def describe_domains(self, page_size=100, page_num=1):
        return [{"domain_name": "example.com", "dns_servers": ["ns1"], "record_count": 2}]


class FakeClients:
    def __init__(self, client=None):
        self._client = client or FakeClient()

    def get(self, account_id):
        if account_id == "ghost":
            raise AlidnsError("account_not_found", "not managed")
        return self._client


def make_ctx(checker=None, clients=None):
    return ToolContext(checker=checker or FakeChecker(), clients=clients or FakeClients())


@pytest.mark.asyncio
async def test_list_accounts_ok():
    ctx = make_ctx(checker=FakeChecker(accounts=[
        {"account_id": "acct1", "description": "账户1", "read": True, "write": False}]))
    result = await list_accounts(ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"][0]["account_id"] == "acct1"
    assert result["data"][0]["write"] is False


@pytest.mark.asyncio
async def test_list_accounts_invalid_token():
    class DenyChecker:
        async def allowed_accounts(self):
            raise ToolError("permission denied: invalid_token")
    ctx = make_ctx(checker=DenyChecker())
    with pytest.raises(ToolError):
        await list_accounts(ctx=ctx)


@pytest.mark.asyncio
async def test_list_domains_ok():
    ctx = make_ctx()
    result = await list_domains("acct1", ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"][0]["domain_name"] == "example.com"


@pytest.mark.asyncio
async def test_list_domains_denied():
    ctx = make_ctx(checker=FakeChecker(denied={"acct1"}))
    with pytest.raises(ToolError):
        await list_domains("acct1", ctx=ctx)


@pytest.mark.asyncio
async def test_list_domains_account_missing():
    ctx = make_ctx(clients=FakeClients())
    result = await list_domains("ghost", ctx=ctx)
    assert result["status"] == "error"
    assert result["error_type"] == "account_not_found"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd aliyun-dns-mcp && uv run python -m pytest tests/test_tools_read.py -q`
Expected: FAIL（`ModuleNotFoundError: tools`）

- [ ] **Step 3: 实现 tools/__init__.py**

```python
"""工具注册模块：模块级函数 + 显式具名包装（FastMCP v4 拒绝 *args 包装）。

工具函数定义在 tools/*.py 模块级（可独立测试），register() 只做薄包装：
注入真实 ctx（checker/clients），复制 docstring 作为 tool 描述。
"""
import functools
import time

import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from dataclasses import dataclass

logger = structlog.get_logger()

try:
    from telemetry import REQUESTS_TOTAL, REQUEST_DURATION, ERRORS_TOTAL, IN_FLIGHT_REQUESTS
except ImportError:
    # telemetry 未就绪（Task 7 前）时指标为 None，record 前 guard
    REQUESTS_TOTAL = REQUEST_DURATION = ERRORS_TOTAL = IN_FLIGHT_REQUESTS = None


@dataclass
class ToolContext:
    """工具依赖上下文：checker（账户级鉴权）+ clients（Alidns 客户端工厂）。"""
    checker: object
    clients: object


def _metrics_wrapper(tool_name: str):
    """记录 tool 级 Prometheus 指标（zabbix 模式）。"""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if REQUESTS_TOTAL:
                REQUESTS_TOTAL.add(1, attributes={"tool_name": tool_name})
            if IN_FLIGHT_REQUESTS:
                IN_FLIGHT_REQUESTS.add(1)
            start = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                if isinstance(result, dict) and result.get("status") == "error" and ERRORS_TOTAL:
                    ERRORS_TOTAL.add(1, attributes={"tool_name": tool_name, "error_type": "tool_error"})
                return result
            except Exception as e:
                if ERRORS_TOTAL:
                    ERRORS_TOTAL.add(1, attributes={"tool_name": tool_name, "error_type": type(e).__name__})
                raise
            finally:
                duration = time.monotonic() - start
                if REQUEST_DURATION:
                    REQUEST_DURATION.record(duration, attributes={"tool_name": tool_name})
                if IN_FLIGHT_REQUESTS:
                    IN_FLIGHT_REQUESTS.add(-1)
        return wrapper
    return decorator


def register_tools(mcp: FastMCP, get_ctx, metrics=None) -> None:
    """注册全部工具。get_ctx: callable 返回 ToolContext（server.py 注入）。"""
    from tools import accounts, domains, records
    accounts.register(mcp, get_ctx, metrics=metrics or _metrics_wrapper)
    domains.register(mcp, get_ctx, metrics=metrics or _metrics_wrapper)
    records.register(mcp, get_ctx, metrics=metrics or _metrics_wrapper)
```

- [ ] **Step 4: 实现 tools/accounts.py 与 tools/domains.py**

`tools/accounts.py`：

```python
"""账户工具：list_accounts（当前 token 可访问的托管账户）。"""
import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tools import ToolContext

logger = structlog.get_logger()


async def list_accounts(*, ctx: ToolContext | None = None) -> dict:
    """列出当前 token 可访问的阿里云账户及其读写权限。

    返回 [{account_id, description, read, write}]，只含已托管且启用的账户；
    不暴露 AccessKey 等凭证信息。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized"}
    accounts = await ctx.checker.allowed_accounts()
    return {"status": "ok", "data": accounts, "count": len(accounts)}


def register(mcp: FastMCP, get_ctx, metrics=None) -> None:
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_list_accounts() -> dict:
        return await list_accounts(ctx=get_ctx())

    _mcp_list_accounts.__doc__ = list_accounts.__doc__
    mcp.tool(
        name="list_accounts",
        description=list_accounts.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("list_accounts")(_mcp_list_accounts))
```

`tools/domains.py`：

```python
"""域名工具：list_domains（按账户查域名列表）。"""
import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tools import ToolContext
from aliyun_client import AlidnsError

logger = structlog.get_logger()


async def list_domains(account_id: str, *, ctx: ToolContext | None = None) -> dict:
    """查询指定阿里云账户的域名列表（DescribeDomains）。

    返回 [{domain_name, dns_servers, record_count}]，取前 100 条。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized"}
    await ctx.checker.require(account_id, "read")
    try:
        client = ctx.clients.get(account_id)
        domains = await client.describe_domains(page_size=100, page_num=1)
    except AlidnsError as e:
        return {"status": "error", "error_type": e.error_type,
                "message": e.message, "request_id": e.request_id}
    return {"status": "ok", "data": domains, "count": len(domains)}


def register(mcp: FastMCP, get_ctx, metrics=None) -> None:
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_list_domains(account_id: str) -> dict:
        return await list_domains(account_id, ctx=get_ctx())

    _mcp_list_domains.__doc__ = list_domains.__doc__
    mcp.tool(
        name="list_domains",
        description=list_domains.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("list_domains")(_mcp_list_domains))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd aliyun-dns-mcp && uv run python -m pytest tests/test_tools_read.py -q`
Expected: PASS（5 tests）。注：`register_tools` 引用了尚未创建的 `tools.records`——本任务只测 accounts/domains 函数本身，不触发 register_tools；Task 6 创建 records 后补一条注册冒烟。

- [ ] **Step 6: Commit**

```bash
git add aliyun-dns-mcp/tools/ aliyun-dns-mcp/tests/test_tools_read.py
git commit -m "feat(aliyun-dns-mcp): 读工具 list_accounts/list_domains + 注册骨架"
```

---

### Task 6: 记录工具（list_records + add_record + update_record + delete_record）

**Files:**
- Create: `aliyun-dns-mcp/tools/records.py`
- Test: `aliyun-dns-mcp/tests/test_tools_records.py`

**Interfaces:**
- Consumes: `ToolContext`、`AlidnsError`（Task 4/5）
- Produces:
  - `async list_records(account_id: str, domain_name: str, *, ctx=None) -> dict`
  - `async add_record(account_id: str, domain_name: str, rr: str, type: str, value: str, ttl: int = 600, priority: int | None = None, *, ctx=None) -> dict`
  - `async update_record(account_id: str, record_id: str, rr: str | None = None, type: str | None = None, value: str | None = None, ttl: int | None = None, priority: int | None = None, *, ctx=None) -> dict`
  - `async delete_record(account_id: str, record_id: str, *, ctx=None) -> dict`
  - 返回约定：成功 `{"status": "ok", "data": {...}}`；AlidnsError → `{"status": "error", "error_type", "message", "request_id"}`；鉴权失败 ToolError 上抛；update_record 至少一个更新字段否则返回 error `invalid_params`

- [ ] **Step 1: 写失败测试**

`aliyun-dns-mcp/tests/test_tools_records.py`：

```python
"""记录工具测试：list/add/update/delete_record，注入 fake ctx。"""
import pytest
from fastmcp.exceptions import ToolError

from tools import ToolContext
from tools.records import list_records, add_record, update_record, delete_record
from aliyun_client import AlidnsError


class FakeChecker:
    def __init__(self, denied=None):
        self.denied = set(denied or [])

    async def require(self, account_id, mode):
        if account_id in self.denied:
            raise ToolError(f"permission denied: no_permission: account '{account_id}'")


class FakeClient:
    def __init__(self, fail_add=False):
        self.fail_add = fail_add

    async def describe_domain_records(self, domain_name, page_size=100, page_num=1):
        return [{"record_id": "r1", "rr": "@", "type": "A", "value": "1.2.3.4",
                 "ttl": 600, "priority": None, "status": "ENABLE"}]

    async def add_domain_record(self, domain_name, rr, type, value, ttl=600, priority=None):
        if self.fail_add:
            raise AlidnsError("throttled", "Throttling.User", "req-1")
        return "new-1"

    async def update_domain_record(self, record_id, **kwargs):
        return None

    async def delete_domain_record(self, record_id):
        return None


class FakeClients:
    def __init__(self, client=None):
        self._client = client or FakeClient()

    def get(self, account_id):
        return self._client


def make_ctx(checker=None, client=None):
    return ToolContext(checker=checker or FakeChecker(), clients=FakeClients(client))


@pytest.mark.asyncio
async def test_list_records_ok():
    ctx = make_ctx()
    result = await list_records("acct1", "example.com", ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"][0]["record_id"] == "r1"


@pytest.mark.asyncio
async def test_add_record_ok():
    ctx = make_ctx()
    result = await add_record("acct1", "example.com", "www", "A", "1.2.3.4", ttl=300, ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"]["record_id"] == "new-1"


@pytest.mark.asyncio
async def test_add_record_write_denied():
    ctx = make_ctx(checker=FakeChecker(denied={"acct1"}))
    with pytest.raises(ToolError):
        await add_record("acct1", "example.com", "www", "A", "1.2.3.4", ctx=ctx)


@pytest.mark.asyncio
async def test_add_record_aliyun_error_mapped():
    ctx = make_ctx(client=FakeClient(fail_add=True))
    result = await add_record("acct1", "example.com", "www", "A", "1.2.3.4", ctx=ctx)
    assert result["status"] == "error"
    assert result["error_type"] == "throttled"
    assert result["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_update_record_ok():
    ctx = make_ctx()
    result = await update_record("acct1", "r1", value="5.6.7.8", ttl=60, ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"]["record_id"] == "r1"


@pytest.mark.asyncio
async def test_update_record_no_fields_rejected():
    ctx = make_ctx()
    result = await update_record("acct1", "r1", ctx=ctx)
    assert result["status"] == "error"
    assert result["error_type"] == "invalid_params"


@pytest.mark.asyncio
async def test_delete_record_ok():
    ctx = make_ctx()
    result = await delete_record("acct1", "r1", ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"]["record_id"] == "r1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd aliyun-dns-mcp && uv run python -m pytest tests/test_tools_records.py -q`
Expected: FAIL（`ModuleNotFoundError: tools.records`）

- [ ] **Step 3: 实现 tools/records.py**

```python
"""解析记录工具：list/add/update/delete（写操作走 ⚠️ 用户确认流程）。"""
import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tools import ToolContext
from aliyun_client import AlidnsError

logger = structlog.get_logger()


async def list_records(account_id: str, domain_name: str, *, ctx: ToolContext | None = None) -> dict:
    """查询指定账户、主域名的 DNS 解析记录列表（DescribeDomainRecords）。

    返回 [{record_id, rr, type, value, ttl, priority, status}]，取前 100 条。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized"}
    await ctx.checker.require(account_id, "read")
    try:
        client = ctx.clients.get(account_id)
        records = await client.describe_domain_records(domain_name, page_size=100, page_num=1)
    except AlidnsError as e:
        return {"status": "error", "error_type": e.error_type,
                "message": e.message, "request_id": e.request_id}
    return {"status": "ok", "data": records, "count": len(records)}


async def add_record(account_id: str, domain_name: str, rr: str, type: str, value: str,
                     ttl: int = 600, priority: int | None = None, *,
                     ctx: ToolContext | None = None) -> dict:
    """新增 DNS 解析记录（AddDomainRecord）。

    ⚠️ 写操作 — 执行前必须向用户确认参数后再调用。

    type 支持 A/AAAA/CNAME/TXT/MX/NS/SRV/CAA 等阿里云全部类型；
    priority 仅 MX/SRV 需要。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized"}
    await ctx.checker.require(account_id, "write")
    try:
        client = ctx.clients.get(account_id)
        record_id = await client.add_domain_record(
            domain_name, rr, type, value, ttl=ttl, priority=priority)
    except AlidnsError as e:
        return {"status": "error", "error_type": e.error_type,
                "message": e.message, "request_id": e.request_id}
    logger.info("record_added", service="aliyun-dns-mcp", account_id=account_id,
                domain_name=domain_name, rr=rr, type=type)
    return {"status": "ok", "data": {"record_id": record_id}}


async def update_record(account_id: str, record_id: str,
                        rr: str | None = None, type: str | None = None,
                        value: str | None = None, ttl: int | None = None,
                        priority: int | None = None, *,
                        ctx: ToolContext | None = None) -> dict:
    """修改 DNS 解析记录（UpdateDomainRecord）。

    ⚠️ 写操作 — 执行前必须向用户确认参数后再调用。

    至少传一个更新字段；未传的字段保持不变。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized"}
    if all(v is None for v in (rr, type, value, ttl, priority)):
        return {"status": "error", "error_type": "invalid_params",
                "message": "至少提供一个更新字段 (rr/type/value/ttl/priority)"}
    await ctx.checker.require(account_id, "write")
    try:
        client = ctx.clients.get(account_id)
        await client.update_domain_record(record_id, rr=rr, type=type, value=value,
                                          ttl=ttl, priority=priority)
    except AlidnsError as e:
        return {"status": "error", "error_type": e.error_type,
                "message": e.message, "request_id": e.request_id}
    logger.info("record_updated", service="aliyun-dns-mcp", account_id=account_id, record_id=record_id)
    return {"status": "ok", "data": {"record_id": record_id}}


async def delete_record(account_id: str, record_id: str, *, ctx: ToolContext | None = None) -> dict:
    """删除 DNS 解析记录（DeleteDomainRecord）。

    ⚠️ 写操作 — 删除不可撤销，执行前必须向用户确认。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized"}
    await ctx.checker.require(account_id, "write")
    try:
        client = ctx.clients.get(account_id)
        await client.delete_domain_record(record_id)
    except AlidnsError as e:
        return {"status": "error", "error_type": e.error_type,
                "message": e.message, "request_id": e.request_id}
    logger.info("record_deleted", service="aliyun-dns-mcp", account_id=account_id, record_id=record_id)
    return {"status": "ok", "data": {"record_id": record_id}}


def register(mcp: FastMCP, get_ctx, metrics=None) -> None:
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_list_records(account_id: str, domain_name: str) -> dict:
        return await list_records(account_id, domain_name, ctx=get_ctx())

    _mcp_list_records.__doc__ = list_records.__doc__
    mcp.tool(name="list_records", description=list_records.__doc__,
             annotations=ToolAnnotations(readOnlyHint=True))(_wrap("list_records")(_mcp_list_records))

    async def _mcp_add_record(account_id: str, domain_name: str, rr: str, type: str,
                              value: str, ttl: int = 600,
                              priority: int | None = None) -> dict:
        return await add_record(account_id, domain_name, rr, type, value,
                                ttl=ttl, priority=priority, ctx=get_ctx())

    _mcp_add_record.__doc__ = add_record.__doc__
    mcp.tool(name="add_record", description=add_record.__doc__,
             annotations=ToolAnnotations(destructiveHint=True))(_wrap("add_record")(_mcp_add_record))

    async def _mcp_update_record(account_id: str, record_id: str,
                                 rr: str | None = None, type: str | None = None,
                                 value: str | None = None, ttl: int | None = None,
                                 priority: int | None = None) -> dict:
        return await update_record(account_id, record_id, rr=rr, type=type, value=value,
                                   ttl=ttl, priority=priority, ctx=get_ctx())

    _mcp_update_record.__doc__ = update_record.__doc__
    mcp.tool(name="update_record", description=update_record.__doc__,
             annotations=ToolAnnotations(destructiveHint=True))(_wrap("update_record")(_mcp_update_record))

    async def _mcp_delete_record(account_id: str, record_id: str) -> dict:
        return await delete_record(account_id, record_id, ctx=get_ctx())

    _mcp_delete_record.__doc__ = delete_record.__doc__
    mcp.tool(name="delete_record", description=delete_record.__doc__,
             annotations=ToolAnnotations(destructiveHint=True))(_wrap("delete_record")(_mcp_delete_record))
```

- [ ] **Step 4: 注册冒烟——6 个工具全部注册成功**

在 Step 5 测试通过后补一条注册测试到 `tests/test_tools_read.py`（或新文件 `tests/test_register.py`）：

```python
"""注册冒烟：register_tools 注册 6 个工具，含读写标注。"""
from fastmcp import FastMCP

from tools import register_tools, ToolContext


def test_register_tools_six_tools():
    mcp = FastMCP("Test")
    ctx = ToolContext(checker=object(), clients=object())
    register_tools(mcp, lambda: ctx, metrics=None)
    tools = {t.name: t for t in mcp.get_tools().values()}
    assert set(tools) == {"list_accounts", "list_domains", "list_records",
                          "add_record", "update_record", "delete_record"}
    assert tools["add_record"].annotations.destructiveHint is True
    assert tools["list_domains"].annotations.readOnlyHint is True
    assert "⚠️ 写操作" in (tools["add_record"].description or "")
```

Run: `cd aliyun-dns-mcp && uv run python -m pytest tests/ -q`
Expected: PASS（全部 tests）

- [ ] **Step 5: Commit**

```bash
git add aliyun-dns-mcp/tools/records.py aliyun-dns-mcp/tests/
git commit -m "feat(aliyun-dns-mcp): 记录工具 list/add/update/delete_record + 注册冒烟"
```

---

### Task 7: server.py 装配 + telemetry/logging + 文档 + 启动冒烟

**Files:**
- Create: `aliyun-dns-mcp/telemetry.py`（复制 zabbix-mcp/telemetry.py 后改名）、`aliyun-dns-mcp/logging_config.py`（复制 zabbix-mcp/logging_config.py）、`aliyun-dns-mcp/README.md`、`aliyun-dns-mcp/CLAUDE.md`、`aliyun-dns-mcp/RELEASE.md`
- Modify: `aliyun-dns-mcp/server.py`（模板骨架 → 真实装配）、`aliyun-dns-mcp/tests/test_register.py`（如已建，不动）

**Interfaces:**
- Consumes: `AccountStore`（Task 2）、`PermissionChecker`（Task 3）、`ClientFactory`（Task 4）、`register_tools`/`ToolContext`（Task 5）
- Produces: 可启动的 `aliyun-dns-mcp`（`uv run python server.py` 起服务，`REDIS_URL` 必填），`/metrics` 暴露 `aliyndns_*` 指标

- [ ] **Step 1: 复制并改造 telemetry.py 与 logging_config.py**

```bash
cp ../zabbix-mcp/telemetry.py aliyun-dns-mcp/telemetry.py
cp ../zabbix-mcp/logging_config.py aliyun-dns-mcp/logging_config.py
```

对 `aliyun-dns-mcp/telemetry.py` 应用以下精确改动（其余行保持原样）：
1. `PROMETHEUS_PORT` 默认保持 `9464`
2. 模块级指标全局变量名保持 `REQUESTS_TOTAL / REQUEST_DURATION / ERRORS_TOTAL / DEPENDENCY_DURATION / DEPENDENCY_ERRORS_TOTAL / IN_FLIGHT_REQUESTS`（tools/__init__.py 已按此导入）
3. `init_telemetry(service_name: str = "aliyun-dns-mcp")`
4. `meter = metrics.get_meter("aliyun_dns_mcp")`
5. 指标名全部改 `aliyndns_` 前缀：
   - `aliyndns_requests_total` — "Total MCP tool calls"
   - `aliyndns_request_duration_seconds` — "MCP tool call latency"
   - `aliyndns_errors_total` — "Total MCP tool errors"
   - `aliyndns_dependency_duration_seconds` — "Aliyun Alidns API call latency"
   - `aliyndns_dependency_errors_total` — "Total Alidns API errors"
   - `aliyndns_in_flight_requests` — "Currently processing MCP requests"
6. 日志里的 `service_name` 引用不变（参数已改默认值）

`logging_config.py` 复制后无改动（OBS-CORE-001 结构化日志 + LOG_FILE 支持，模板同款）。

- [ ] **Step 2: 重写 server.py**

```python
"""Aliyun DNS MCP Server — entry point.

提供阿里云 DNS 解析管理：多账户托管、域名/解析查询、增删改解析记录。
账户级 read/write 权限由本 server 校验（MCP 是权威）：gateway 的 proxy
transport 自动转发 Authorization 头，本服务验证 token 并查账户级权限。

Observability: structlog + OTel（日志注入 trace_id/span_id）+ Prometheus。
Env: REDIS_URL（必填）、MCP_HOST/MCP_PORT、LOG_FORMAT、PROMETHEUS_PORT、
OTEL_EXPORTER_OTLP_ENDPOINT、OTEL_SERVICE_NAME。
"""
import asyncio
import os

import structlog
from fastmcp import FastMCP
import redis.asyncio as redis

from account_store import AccountStore
from auth import PermissionChecker
from aliyun_client import ClientFactory
from tools import ToolContext

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "9054"))
REDIS_URL = os.environ.get("REDIS_URL", "")
LOG_FORMAT = os.environ.get("LOG_FORMAT", "console")


def _configure_logging() -> None:
    from logging_config import configure_logging
    configure_logging([
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer() if LOG_FORMAT == "json"
        else structlog.dev.ConsoleRenderer(),
    ])


logger = structlog.get_logger()

# 进程级单例（stateless 模式 lifespan 不可靠，模块级 init）。
_store = None
_checker = None
_clients = None


def _init_runtime() -> None:
    """初始化 AccountStore/PermissionChecker/ClientFactory。启动时调用一次。"""
    global _store, _checker, _clients
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL environment variable is required")
    client = redis.from_url(REDIS_URL, decode_responses=True)
    _store = AccountStore(client)
    _checker = PermissionChecker(_store, client)
    _clients = ClientFactory(_store)


def _get_ctx() -> ToolContext:
    if _checker is None or _clients is None:
        raise RuntimeError("runtime not initialized — call _init_runtime()")
    return ToolContext(checker=_checker, clients=_clients)


_configure_logging()

try:
    from telemetry import init_telemetry
    init_telemetry("aliyun-dns-mcp")
except Exception as exc:
    # 可观测性降级不应杀服务
    logger.warning("telemetry_init_failed", service="aliyun-dns-mcp", error=str(exc))

mcp = FastMCP(
    "Aliyun DNS MCP",
    instructions=(
        "阿里云 DNS 解析管理：list_accounts 查看可访问账户，"
        "list_domains/list_records 查询，add_record/update_record/delete_record "
        "增删改解析记录。所有写操作需要用户确认，且受账户级读写权限控制。"
    ),
)

from tools import register_tools
register_tools(mcp, _get_ctx)


if __name__ == "__main__":
    _init_runtime()

    async def _run() -> None:
        # listener 必须与 server 同 event loop（serpapi 教训：跨 loop 用
        # redis 连接直接 RuntimeError）
        await _store.start()
        await mcp.run_async(
            transport="streamable-http",
            stateless_http=True,
            host=MCP_HOST,
            port=MCP_PORT,
        )

    asyncio.run(_run())
```

- [ ] **Step 3: 删除模板残留（resource/prompt 与示例 tool）**

模板 server.py 里的 `list_items`/`create_item` 示例 tool、`info://version` resource、`help_prompt` prompt 全部删除（Step 2 的重写已不含它们——确认 `mcp.get_tools()` 只有 6 个注册工具）。

- [ ] **Step 4: 写 MCP 文档三件套**

`aliyun-dns-mcp/CLAUDE.md`（要点：架构 = gateway 零改动 + MCP 权威账户级权限；Redis schema 三件套；坑 = get_http_headers 必须 include_all=True、pubsub 重建、httpx logger WARNING、SDK 同步走 to_thread、uv.lock 阿里云镜像；本地开发命令）。参考 zabbix-mcp/CLAUDE.md 结构写完整版，含配置表：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `REDIS_URL` | 无（必填） | Redis（账户凭证 + 权限 + 热更新 pubsub） |
| `MCP_HOST` | `0.0.0.0` | 监听地址 |
| `MCP_PORT` | `9054` | MCP 端口（根 CLAUDE.md 登记） |
| `LOG_FORMAT` | `console` | `console`（开发）/ `json`（生产） |
| `PROMETHEUS_PORT` | `9464` | Prometheus /metrics 端口 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 无 | OTLP collector（未设则 console span） |
| `OTEL_SERVICE_NAME` | `aliyun-dns-mcp` | 服务名（trace/metrics label） |

`aliyun-dns-mcp/README.md`：功能说明（6 tools 表、权限模型两维度、接入 gateway 流程、管理界面入口）。
`aliyun-dns-mcp/RELEASE.md`：复制模板后改服务名/端口/发布步骤（uv.lock 阿里云镜像、Dockerfile、compose）。

- [ ] **Step 5: 全量测试 + 启动冒烟**

Run: `cd aliyun-dns-mcp && uv run python -m pytest tests/ -q`
Expected: PASS（全部）

启动冒烟（无 Redis 会报错——先起本地 redis）：

```bash
redis-server --daemonize yes
cd aliyun-dns-mcp
REDIS_URL=redis://localhost:6379/0 timeout 5 uv run python server.py
```

Expected: 输出 `gateway_started` 类日志后因 timeout 退出（退出码 124 正常）；日志含 `account_store_loaded`、`otel_metrics_configured`。若 `timeout` 不可用，改用 `uv run python server.py & sleep 3; kill %1`。

- [ ] **Step 6: Commit**

```bash
git add aliyun-dns-mcp/
git commit -m "feat(aliyun-dns-mcp): server 装配 + telemetry/logging + 文档三件套"
```

---

### Task 8: gateway-admin 后端 API（账户 CRUD + 授权矩阵 + union 同步）

**Files:**
- Create: `gateway-admin/api/aliyun_accounts.py`、`gateway-admin/api/aliyun_perms.py`
- Modify: `gateway-admin/app.py`（注册 router）、`gateway-admin/api/tokens.py`（delete_token 清理授权）、`gateway-admin/pyproject.toml`（加 SDK 依赖）
- Test: `gateway-admin/tests/test_aliyun_accounts.py`、`gateway-admin/tests/test_aliyun_perms.py`

**Interfaces:**
- Consumes: `get_redis`、`require_admin`（gateway-admin 现有）、`hash_token`/`generate_token`（api/tokens.py）
- Produces:
  - `GET /api/aliyun-accounts` → `[{account_id, description, region, enabled, access_key_masked, created_at}]`
  - `POST /api/aliyun-accounts`（AccountCreate: account_id/description/access_key_id/access_key_secret/region/enabled/probe）→ 探活（probe=True 时）+ hset + sadd index + PUBLISH；返回含 `probe_error`（探活失败不阻断）
  - `PUT /api/aliyun-accounts/{account_id}`（AccountUpdate: 部分字段 + probe）→ 凭证变更时可选探活 + hset + PUBLISH
  - `DELETE /api/aliyun-accounts/{account_id}` → hdel + srem + PUBLISH
  - `GET /api/aliyun-perms/{token_id}` → `{token_id, permissions: {account_id: {read, write}}}`
  - `PUT /api/aliyun-perms/{token_id}`（PermsPut: permissions dict）→ 校验账户存在 + write⇒read 强制 + hset/delete + **union 同步** `tokens:{token_hash}` 的 `aliyun-dns-mcp` read/write + PUBLISH

- [ ] **Step 1: gateway-admin 加 SDK 依赖 + lock**

`gateway-admin/pyproject.toml` dependencies 加：

```toml
    "alibabacloud-alidns20150109",
    "alibabacloud-tea-openapi",
```

```bash
cd gateway-admin
uv sync --all-extras
rm -f uv.lock
UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv lock
grep -c mirrors.aliyun.com uv.lock   # > 0
```

- [ ] **Step 2: 写失败测试（账户 CRUD）**

`gateway-admin/tests/test_aliyun_accounts.py`：

```python
"""阿里云账户管理 API 测试。"""
import json
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


@pytest.fixture
def no_probe(monkeypatch):
    """默认探活不真发：monkeypatch _probe 为成功。"""
    import api.aliyun_accounts as mod
    async def fake_probe(ak_id, ak_secret, region):
        return {"ok": True}
    monkeypatch.setattr(mod, "_probe", fake_probe)


def test_create_account(no_probe, client, fake_redis, auth_headers):
    resp = client.post("/api/aliyun-accounts", json={
        "account_id": "prod-main",
        "description": "生产主账户",
        "access_key_id": "LTAI123",
        "access_key_secret": "sk-secret",
        "region": "cn-hangzhou",
    }, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["account_id"] == "prod-main"
    assert data["probe_error"] is None
    # Redis 已写入 + index + 发布
    assert (await fake_redis.hget("aliyndns:accounts:prod-main", "access_key_id")) == "LTAI123"
    assert await fake_redis.sismember("aliyndns:accounts:index", "prod-main")
    # 明文 secret 不进响应
    assert "access_key_secret" not in data


def test_update_account(no_probe, client, fake_redis, auth_headers):
    client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "access_key_id": "a", "access_key_secret": "s"}, headers=auth_headers)
    resp = client.put("/api/aliyun-accounts/acct1", json={"description": "新描述", "enabled": False},
                      headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert (await fake_redis.hget("aliyndns:accounts:acct1", "description")) == "新描述"


def test_delete_account(no_probe, client, fake_redis, auth_headers):
    client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "access_key_id": "a", "access_key_secret": "s"}, headers=auth_headers)
    resp = client.delete("/api/aliyun-accounts/acct1", headers=auth_headers)
    assert resp.status_code == 204
    assert not await fake_redis.sismember("aliyndns:accounts:index", "acct1")


def test_create_account_probe_failure_marks_probe_error(monkeypatch, client, fake_redis, auth_headers):
    import api.aliyun_accounts as mod
    async def bad_probe(ak_id, ak_secret, region):
        return {"ok": False, "error": "InvalidAccessKeyId.NotFound"}
    monkeypatch.setattr(mod, "_probe", bad_probe)
    resp = client.post("/api/aliyun-accounts", json={
        "account_id": "bad-key",
        "access_key_id": "LTAI-x", "access_key_secret": "sk",
    }, headers=auth_headers)
    assert resp.status_code == 201  # 探活失败不阻断添加
    assert resp.json()["probe_error"] == "InvalidAccessKeyId.NotFound"


def test_create_account_duplicate_rejected(no_probe, client, fake_redis, auth_headers):
    client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "access_key_id": "a", "access_key_secret": "s"}, headers=auth_headers)
    resp = client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "access_key_id": "a", "access_key_secret": "s"}, headers=auth_headers)
    assert resp.status_code == 422


def test_list_accounts_masks_secrets(no_probe, client, fake_redis, auth_headers):
    client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "description": "d",
        "access_key_id": "LTAI1234567890abcdef", "access_key_secret": "sk"}, headers=auth_headers)
    resp = client.get("/api/aliyun-accounts", headers=auth_headers)
    data = resp.json()
    assert len(data) == 1
    assert data[0]["account_id"] == "acct1"
    assert "LTAI1234567890abcdef" not in data[0]["access_key_masked"]
    assert "access_key_secret" not in data[0]


def test_delete_account_cleans_token_perms(no_probe, client, fake_redis, auth_headers):
    """删除账户时清理所有 token 授权映射中的该账户引用（防僵尸授权）。"""
    client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "access_key_id": "a", "access_key_secret": "s"}, headers=auth_headers)
    # 直接写 Redis 造授权映射（不经 API，模拟已有授权）
    import json as _json
    await fake_redis.hset("aliyndns:token_accounts:tokid_1", "acct1",
                          _json.dumps({"read": True, "write": False}))
    await fake_redis.hset("aliyndns:token_accounts:tokid_1", "acct2",
                          _json.dumps({"read": True, "write": False}))
    resp = client.delete("/api/aliyun-accounts/acct1", headers=auth_headers)
    assert resp.status_code == 204
    remaining = await fake_redis.hgetall("aliyndns:token_accounts:tokid_1")
    assert "acct1" not in remaining
    assert "acct2" in remaining
```

- [ ] **Step 3: 写失败测试（授权矩阵 + union 同步）**

`gateway-admin/tests/test_aliyun_perms.py`：

```python
"""token×账户授权矩阵 API 测试（含 union 同步 gateway token）。"""
import json
import pytest
from auth import create_jwt
from api.tokens import hash_token


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def _seed_server(client, auth_headers, name="aliyun-dns-mcp"):
    client.post("/api/servers", json={"name": name, "url": "http://aliyun-dns-mcp:9054/mcp",
                                      "description": "dns"}, headers=auth_headers)


def _seed_account(client, auth_headers, account_id="acct1"):
    client.post("/api/aliyun-accounts", json={
        "account_id": account_id, "access_key_id": "a", "access_key_secret": "s",
        "probe": False}, headers=auth_headers)


def _seed_token(client, auth_headers, name="ro") -> str:
    resp = client.post("/api/tokens", json={
        "name": name, "permissions": {"aliyun-dns-mcp": {"read": True, "write": False}}},
        headers=auth_headers)
    return resp.json()["id"]


def test_get_perms_empty(client, fake_redis, auth_headers):
    _seed_server(client, auth_headers)
    token_id = _seed_token(client, auth_headers)
    resp = client.get(f"/api/aliyun-perms/{token_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["permissions"] == {}


def test_put_perms_write_implies_read(client, fake_redis, auth_headers):
    _seed_server(client, auth_headers)
    _seed_account(client, auth_headers)
    token_id = _seed_token(client, auth_headers)
    resp = client.put(f"/api/aliyun-perms/{token_id}", json={
        "permissions": {"acct1": {"read": False, "write": True}}}, headers=auth_headers)
    assert resp.status_code == 200
    # write ⇒ read 强制
    assert resp.json()["permissions"]["acct1"] == {"read": True, "write": True}


def test_put_perms_unknown_account_rejected(client, fake_redis, auth_headers):
    _seed_server(client, auth_headers)
    _seed_account(client, auth_headers)
    token_id = _seed_token(client, auth_headers)
    resp = client.put(f"/api/aliyun-perms/{token_id}", json={
        "permissions": {"ghost": {"read": True, "write": False}}}, headers=auth_headers)
    assert resp.status_code == 422


def test_put_perms_syncs_union_to_gateway_token(client, fake_redis, auth_headers):
    _seed_server(client, auth_headers)
    _seed_account(client, auth_headers, "acct1")
    _seed_account(client, auth_headers, "acct2")
    token_id = _seed_token(client, auth_headers)
    # 原 token：aliyun-dns-mcp read+write（可见性粗闸先开）
    client.put(f"/api/aliyun-perms/{token_id}", json={
        "permissions": {
            "acct1": {"read": True, "write": False},
            "acct2": {"read": True, "write": True},
        }}, headers=auth_headers)
    # 找 token hash 并验证 union
    token_hash = await fake_redis.get(f"token_id:{token_id}")
    data = await fake_redis.hgetall(f"tokens:{token_hash}")
    perms = json.loads(data["permissions"])
    assert perms["aliyun-dns-mcp"] == {"read": True, "write": True}  # 任一账户有 write
    # 授权映射已写
    raw = await fake_redis.hget(f"aliyndns:token_accounts:{token_id}", "acct2")
    assert json.loads(raw) == {"read": True, "write": True}


def test_put_perms_clear_all_removes_mapping(client, fake_redis, auth_headers):
    _seed_server(client, auth_headers)
    _seed_account(client, auth_headers)
    token_id = _seed_token(client, auth_headers)
    client.put(f"/api/aliyun-perms/{token_id}", json={
        "permissions": {"acct1": {"read": True, "write": True}}}, headers=auth_headers)
    resp = client.put(f"/api/aliyun-perms/{token_id}", json={"permissions": {}}, headers=auth_headers)
    assert resp.status_code == 200
    assert not await fake_redis.exists(f"aliyndns:token_accounts:{token_id}")
    token_hash = await fake_redis.get(f"token_id:{token_id}")
    perms = json.loads((await fake_redis.hgetall(f"tokens:{token_hash}"))["permissions"])
    assert perms["aliyun-dns-mcp"] == {"read": False, "write": False}  # 全清 → 无权限
```

- [ ] **Step 4: 跑测试确认失败**

Run: `cd gateway-admin && uv run python -m pytest tests/test_aliyun_accounts.py tests/test_aliyun_perms.py -q`
Expected: FAIL（`ModuleNotFoundError: api.aliyun_accounts`）

- [ ] **Step 5: 实现 api/aliyun_accounts.py**

```python
"""阿里云 DNS 账户管理 API。

Owns aliyndns:accounts:* Redis keys；aliyun-dns-mcp 读这些 key 做账户
凭证与热更新。写 → PUBLISH aliyndns:changed 让 MCP 免重启刷新。
AccessKey/Secret 明文存内网 Redis（与 gateway token 存储同策略），
明文只在 POST create 响应返回一次……不，这里 POST 也不返回明文——
探活用 SDK 后丢弃，响应只回 account_id/description/掩码。
"""
import json
import time

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import require_admin
from redis_client import get_redis

logger = structlog.get_logger()

router = APIRouter(prefix="/api/aliyun-accounts", tags=["aliyun-accounts"])

ACCOUNTS_INDEX = "aliyndns:accounts:index"
CHANGE_CHANNEL = "aliyndns:changed"


class AccountCreate(BaseModel):
    account_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9-]+$")
    description: str = ""
    access_key_id: str = Field(min_length=1)
    access_key_secret: str = Field(min_length=1)
    region: str = "cn-hangzhou"
    enabled: bool = True
    probe: bool = True


class AccountUpdate(BaseModel):
    description: str | None = None
    access_key_id: str | None = None
    access_key_secret: str | None = None
    region: str | None = None
    enabled: bool | None = None
    probe: bool = True


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _mask(key_id: str) -> str:
    if len(key_id) <= 12:
        return key_id[:4] + "…"
    return f"{key_id[:4]}…{key_id[-4:]}"


async def _publish(action: str, account_id: str) -> None:
    """PUBLISH 变更通知让 MCP 热更新。主操作已成功即成功，publish 失败只 warning。"""
    try:
        r = get_redis()
        await r.publish(CHANGE_CHANNEL, json.dumps(
            {"action": action, "key": f"aliyndns:accounts:{account_id}"}))
    except Exception as e:
        logger.warning("aliyun_account_publish_failed", account_id=account_id,
                       error=str(e), service="gateway-admin")


async def _probe(access_key_id: str, access_key_secret: str, region: str) -> dict:
    """用该凭证调 DescribeDomains(PageSize=1) 验证有效性（查询免费）。

    同步 SDK → asyncio.to_thread；失败返回 {"ok": False, "error": 原因}。
    """
    try:
        import asyncio
        from alibabacloud_tea_openapi import models as open_api_models
        from alibabacloud_alidns20150109 import client as alidns_client
        from alibabacloud_alidns20150109 import models as alidns_models

        def run():
            c = alidns_client.Client(open_api_models.Config(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                endpoint="alidns.cn-hangzhou.aliyuncs.com",
            ))
            c.describe_domains_with_options(
                alidns_models.DescribeDomainsRequest(page_size=1, page_number=1), {})

        await asyncio.to_thread(run)
        return {"ok": True}
    except Exception as e:
        code = getattr(e, "code", "")
        msg = str(e)
        return {"ok": False, "error": code or msg[:200]}


@router.get("")
async def list_accounts(_: str = Depends(require_admin)):
    r = get_redis()
    out = []
    for account_id in await r.smembers(ACCOUNTS_INDEX):
        data = await r.hgetall(f"aliyndns:accounts:{account_id}")
        if not data:
            continue
        out.append({
            "account_id": account_id,
            "description": data.get("description", ""),
            "region": data.get("region", "cn-hangzhou"),
            "enabled": data.get("enabled", "true") == "true",
            "access_key_masked": _mask(data.get("access_key_id", "")),
            "probe_error": data.get("probe_error"),
            "created_at": data.get("created_at", ""),
        })
    return out


@router.post("", status_code=201)
async def create_account(req: AccountCreate, _: str = Depends(require_admin)):
    r = get_redis()
    if await r.exists(f"aliyndns:accounts:{req.account_id}"):
        raise HTTPException(status_code=422, detail="account_id 已存在")
    probe_error = None
    if req.probe:
        result = await _probe(req.access_key_id, req.access_key_secret, req.region)
        if not result["ok"]:
            # 探活失败不阻断添加（管理员可能先入库后修复）；错误提示前台可见
            probe_error = result["error"]
    await r.hset(f"aliyndns:accounts:{account_id}", mapping={
        "access_key_id": req.access_key_id,
        "access_key_secret": req.access_key_secret,
        "description": req.description,
        "region": req.region,
        "enabled": "true" if req.enabled else "false",
        "probe_error": probe_error or "",
        "created_at": _now_iso(),
    })
    await r.sadd(ACCOUNTS_INDEX, req.account_id)
    await _publish("upsert", req.account_id)
    logger.info("aliyun_account_created", account_id=req.account_id, service="gateway-admin")
    return {
        "account_id": req.account_id,
        "description": req.description,
        "region": req.region,
        "enabled": req.enabled,
        "probe_error": probe_error,
    }

@router.put("/{account_id}")
async def update_account(account_id: str, req: AccountUpdate, _: str = Depends(require_admin)):
    r = get_redis()
    if not await r.exists(f"aliyndns:accounts:{account_id}"):
        raise HTTPException(status_code=404, detail="account not found")
    data = await r.hgetall(f"aliyndns:accounts:{account_id}")
    updates = {}
    if req.description is not None:
        updates["description"] = req.description
    if req.access_key_id is not None:
        updates["access_key_id"] = req.access_key_id
    if req.access_key_secret is not None:
        updates["access_key_secret"] = req.access_key_secret
    if req.region is not None:
        updates["region"] = req.region
    if req.enabled is not None:
        updates["enabled"] = "true" if req.enabled else "false"
    # 凭证变更时探活（可选，默认开）
    probe_error = data.get("probe_error")
    if req.probe and (req.access_key_id or req.access_key_secret):
        result = await _probe(
            updates.get("access_key_id", data.get("access_key_id", "")),
            updates.get("access_key_secret", data.get("access_key_secret", "")),
            updates.get("region", data.get("region", "cn-hangzhou")),
        )
        probe_error = None if result["ok"] else result["error"]
    updates["probe_error"] = probe_error or ""
    if updates:
        await r.hset(f"aliyndns:accounts:{account_id}", mapping=updates)
    await _publish("upsert", account_id)
    logger.info("aliyun_account_updated", account_id=account_id, service="gateway-admin")
    return {
        "account_id": account_id,
        "description": updates.get("description", data.get("description", "")),
        "enabled": updates.get("enabled", data.get("enabled", "true")) == "true",
        "probe_error": probe_error,
    }


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: str, _: str = Depends(require_admin)):
    r = get_redis()
    removed = await r.delete(f"aliyndns:accounts:{account_id}")
    if not removed:
        raise HTTPException(status_code=404, detail="account not found")
    await r.srem(ACCOUNTS_INDEX, account_id)
    # 清理授权引用：删除账户时从所有 token 的授权映射移除该账户
    # （防僵尸授权——MCP 侧虽有 account_not_found 兜底，数据应保持干净）
    async for key in r.scan_iter(match="aliyndns:token_accounts:*"):
        await r.hdel(key, account_id)
    await _publish("delete", account_id)
    logger.info("aliyun_account_deleted", account_id=account_id, service="gateway-admin")
    return None
```

- [ ] **Step 6: 实现 api/aliyun_perms.py**

```python
"""token×账户 read/write 授权矩阵 API。

Owns aliyndns:token_accounts:{token_id}（账户级权限权威，MCP 读取执行）。
保存时计算 union 同步 gateway token（tokens:{hash}）的 aliyun-dns-mcp
read/write——保证有任一账户写权限的 token 能看到写工具（gateway 的
工具可见性粗闸，spec §3.2）。
"""
import json

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin
from redis_client import get_redis

logger = structlog.get_logger()

router = APIRouter(prefix="/api/aliyun-perms", tags=["aliyun-perms"])

CHANGE_CHANNEL = "aliyndns:changed"
SERVER_NAME = "aliyun-dns-mcp"


class PermSpec(BaseModel):
    read: bool = False
    write: bool = False


class PermsPut(BaseModel):
    permissions: dict[str, PermSpec]


async def _publish(token_id: str) -> None:
    try:
        r = get_redis()
        await r.publish(CHANGE_CHANNEL, json.dumps(
            {"action": "upsert", "key": f"aliyndns:token_accounts:{token_id}"}))
    except Exception as e:
        logger.warning("aliyun_perm_publish_failed", token_id=token_id,
                       error=str(e), service="gateway-admin")


@router.get("/{token_id}")
async def get_perms(token_id: str, _: str = Depends(require_admin)):
    r = get_redis()
    if not await r.get(f"token_id:{token_id}"):
        raise HTTPException(status_code=404, detail="token not found")
    mapping = {}
    for account_id, payload in (await r.hgetall(f"aliyndns:token_accounts:{token_id}")).items():
        try:
            mapping[account_id] = json.loads(payload)
        except json.JSONDecodeError:
            continue  # 脏数据不暴露，跳过（keys.py 同策略）
    return {"token_id": token_id, "permissions": mapping}


@router.put("/{token_id}")
async def put_perms(token_id: str, req: PermsPut, _: str = Depends(require_admin)):
    r = get_redis()
    token_hash = await r.get(f"token_id:{token_id}")
    if not token_hash:
        raise HTTPException(status_code=404, detail="token not found")
    # 校验账户存在 + 强制 write⇒read 不变式
    normalized = {}
    for account_id, p in req.permissions.items():
        if not await r.exists(f"aliyndns:accounts:{account_id}"):
            raise HTTPException(status_code=422, detail=f"account '{account_id}' not managed")
        write = bool(p.write)
        normalized[account_id] = {"read": bool(p.read) or write, "write": write}
    if normalized:
        await r.hset(f"aliyndns:token_accounts:{token_id}",
                     mapping={a: json.dumps(v, ensure_ascii=False) for a, v in normalized.items()})
    else:
        await r.delete(f"aliyndns:token_accounts:{token_id}")
    # union → gateway token 的 server 级 read/write（工具可见性）
    token_data = await r.hgetall(f"tokens:{token_hash}")
    perms = json.loads(token_data.get("permissions", "{}"))
    perms[SERVER_NAME] = {
        "read": any(v["read"] for v in normalized.values()),
        "write": any(v["write"] for v in normalized.values()),
    }
    await r.hset(f"tokens:{token_hash}", "permissions", json.dumps(perms))
    await _publish(token_id)
    logger.info("aliyun_perms_updated", token_id=token_id, accounts=len(normalized),
                service="gateway-admin")
    return {"token_id": token_id, "permissions": normalized}
```

- [ ] **Step 7: app.py 注册 + tokens.py 清理授权**

`gateway-admin/app.py` 的 router 导入与注册加两行：

```python
from api import servers, tokens, dashboard, keys, calls, aliyun_accounts, aliyun_perms
app.include_router(aliyun_accounts.router)
app.include_router(aliyun_perms.router)
```

`gateway-admin/api/tokens.py` 的 `delete_token` 在删除 token 后补授权清理（避免僵尸授权残留——MCP 侧有防御（账户不在 store 即拒绝），但清理保持数据整洁）：

```python
    await r.delete(f"tokens:{token_hash}")
    await r.delete(f"token_id:{token_id}")
    await r.delete(f"aliyndns:token_accounts:{token_id}")
    return None
```

- [ ] **Step 8: 跑测试确认通过**

Run: `cd gateway-admin && uv run python -m pytest tests/test_aliyun_accounts.py tests/test_aliyun_perms.py -q`
Expected: PASS。再跑全量：`uv run python -m pytest tests/ -q`（确认无回归）。

- [ ] **Step 9: Commit**

```bash
git add gateway-admin/api/ gateway-admin/app.py gateway-admin/pyproject.toml gateway-admin/uv.lock gateway-admin/tests/
git commit -m "feat(gateway-admin): 阿里云账户 CRUD + token×账户授权矩阵（union 同步 gateway token）"
```

---

### Task 9: gateway-admin 前端（账户页 + token 授权矩阵）

**Files:**
- Create: `gateway-admin/admin-ui/src/views/AliyunAccounts.vue`
- Modify: `gateway-admin/admin-ui/src/views/Tokens.vue`（行内"授权"按钮 + 授权矩阵 Modal）、`gateway-admin/admin-ui/src/api/index.js`、`gateway-admin/admin-ui/src/router/index.js`、`gateway-admin/admin-ui/src/components/Sidebar.vue`

**Interfaces:**
- Consumes: Task 8 的 API（`/api/aliyun-accounts`、`/api/aliyun-perms`）
- Produces: `/aliyun-accounts` 路由（账户 CRUD 页）、Token 列表行内授权矩阵弹窗

- [ ] **Step 1: api/index.js 加 7 个函数**

`gateway-admin/admin-ui/src/api/index.js` 末尾追加：

```javascript
// ── Aliyun DNS 账户 + 授权矩阵 ─────────────────
export function getAliyunAccounts()         { return apiFetch('/api/aliyun-accounts') }
export function createAliyunAccount(data)   { return apiFetch('/api/aliyun-accounts', { method:'POST', body:JSON.stringify(data) }) }
export function updateAliyunAccount(id, data) { return apiFetch(`/api/aliyun-accounts/${id}`, { method:'PUT', body:JSON.stringify(data) }) }
export function deleteAliyunAccount(id)     { return apiFetch(`/api/aliyun-accounts/${id}`, { method:'DELETE' }) }
export function getAliyunPerms(tokenId)     { return apiFetch(`/api/aliyun-perms/${tokenId}`) }
export function putAliyunPerms(tokenId, permissions) {
  return apiFetch(`/api/aliyun-perms/${tokenId}`, { method:'PUT', body:JSON.stringify({ permissions }) })
}
```

- [ ] **Step 2: 新建 AliyunAccounts.vue（账户 CRUD 页）**

`gateway-admin/admin-ui/src/views/AliyunAccounts.vue`（完整文件）：

```vue
<template>
  <div>
    <div class="table-actions">
      <button class="btn btn-primary" @click="openCreate()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
        添加阿里云账户
      </button>
    </div>
    <div v-if="error" class="err-banner">{{ error }}</div>
    <div v-if="loading" class="muted" style="padding:24px 0">加载中…</div>
    <table v-else class="tbl">
      <thead>
        <tr>
          <th>Account ID</th><th>描述</th><th>AccessKey</th><th>状态</th><th>探活</th><th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="a in accounts" :key="a.account_id">
          <td class="cell-name mono">{{ a.account_id }}</td>
          <td>{{ a.description }}</td>
          <td class="mono" style="font-size:11.5px">{{ a.access_key_masked }}</td>
          <td>
            <span class="perm-chip" :class="a.enabled ? '' : 'danger'">{{ a.enabled ? '启用' : '禁用' }}</span>
          </td>
          <td>
            <span v-if="a.probe_error" class="perm-chip danger" :title="a.probe_error">失败</span>
            <span v-else class="perm-chip">正常</span>
          </td>
          <td>
            <div class="row-actions">
              <button class="mini-btn" @click="openEdit(a)">编辑</button>
              <button class="mini-btn danger" @click="doDelete(a)">删除</button>
            </div>
          </td>
        </tr>
        <tr v-if="!accounts.length && !loading">
          <td colspan="6" style="text-align:center;color:var(--muted);padding:24px 0">暂无账户 · 点击"添加阿里云账户"创建第一个</td>
        </tr>
      </tbody>
    </table>

    <Modal :show="!!modal" title="阿里云账户" @close="modal = null">
      <div class="field"><label>Account ID</label><input v-model="modal.account_id" :disabled="!!modal.original" placeholder="prod-main（小写字母/数字/连字符）" /></div>
      <div class="field"><label>描述</label><input v-model="modal.description" placeholder="生产主账户" /></div>
      <div class="field"><label>AccessKey ID</label><input v-model="modal.access_key_id" placeholder="LTAI..." /></div>
      <div class="field"><label>AccessKey Secret</label>
        <input v-model="modal.access_key_secret" type="password" placeholder="编辑时留空保持不变" /></div>
      <div class="field"><label>Region</label><input v-model="modal.region" placeholder="cn-hangzhou" /></div>
      <label class="perm-toggle read" style="margin:6px 0">
        <input type="checkbox" v-model="modal.enabled" />
        <span class="switch"></span><span class="plabel">启用</span>
      </label>
      <div v-if="modal.probe_error" class="err-banner" style="margin-top:8px">探活失败：{{ modal.probe_error }}（已保存，可修复凭证后重试）</div>
      <template #footer>
        <button class="btn btn-ghost" @click="modal = null">取消</button>
        <button class="btn btn-primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getAliyunAccounts, createAliyunAccount, updateAliyunAccount, deleteAliyunAccount } from '../api/index.js'
import Modal from '../components/Modal.vue'

const accounts = ref([])
const loading = ref(false)
const error = ref('')
const saving = ref(false)
const modal = ref(null)

async function load() {
  loading.value = true; error.value = ''
  try { accounts.value = await getAliyunAccounts() }
  catch (e) { error.value = '加载失败: ' + e.message }
  finally { loading.value = false }
}

function openCreate() {
  modal.value = { account_id: '', description: '', access_key_id: '', access_key_secret: '',
                  region: 'cn-hangzhou', enabled: true, original: null, probe_error: null }
}
function openEdit(a) {
  modal.value = { ...a, access_key_id: '', access_key_secret: '', original: a.account_id }
}

async function save() {
  const m = modal.value
  if (!m.account_id) { error.value = 'Account ID 必填'; return }
  if (!m.original && (!m.access_key_id || !m.access_key_secret)) { error.value = '新增时 AccessKey ID/Secret 必填'; return }
  saving.value = true; error.value = ''
  try {
    if (m.original) {
      const body = { description: m.description, region: m.region, enabled: m.enabled }
      if (m.access_key_id) body.access_key_id = m.access_key_id
      if (m.access_key_secret) body.access_key_secret = m.access_key_secret
      const res = await updateAliyunAccount(m.original, body)
      m.probe_error = res.probe_error
    } else {
      const res = await createAliyunAccount({
        account_id: m.account_id, description: m.description,
        access_key_id: m.access_key_id, access_key_secret: m.access_key_secret,
        region: m.region, enabled: m.enabled,
      })
      m.probe_error = res.probe_error
    }
    if (m.probe_error) { error.value = `已保存，但探活失败：${m.probe_error}` }
    else { modal.value = null }
    await load()
  } catch (e) { error.value = '保存失败: ' + e.message }
  finally { saving.value = false }
}

async function doDelete(a) {
  if (!confirm(`确定删除账户 "${a.account_id}"？此操作不可撤销。`)) return
  try { await deleteAliyunAccount(a.account_id); await load() }
  catch (e) { error.value = '删除失败: ' + e.message }
}

onMounted(load)
</script>
```

- [ ] **Step 3: router + Sidebar 加路由**

`router/index.js` 加：

```javascript
  { path: '/aliyun-accounts', name: 'aliyun-accounts', component: () => import('../views/AliyunAccounts.vue') },
```

`Sidebar.vue` navItems 加：

```javascript
  { id: 'aliyun-accounts', label: '阿里云 DNS', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 10h4l2-5 3 12 2-4h5"/></svg>' },
```

- [ ] **Step 4: Tokens.vue 加"授权"按钮 + 矩阵 Modal**

修改 `Tokens.vue`：

1. 表格行操作（`row-actions` div 内）加按钮：

```vue
              <button class="mini-btn" @click="openPerms(t)">授权</button>
              <button class="mini-btn danger" @click="doDelete(t)">删除</button>
```

2. 关闭 Modal 组件前（`</Modal>` 之前）加授权矩阵 Modal：

```vue
    <!-- ═════ ALIYUN ACCOUNT PERMISSION MATRIX MODAL ═════ -->
    <Modal :show="!!permModal" :title="'账户授权 — ' + (permModal?.token_name || '')" @close="permModal = null">
      <div v-if="!permAccounts.length" class="muted" style="font-size:12px;padding:8px 0">
        暂无阿里云账户，请先到「阿里云 DNS」页添加账户
      </div>
      <div v-else>
        <p style="font-size:12px;color:var(--muted);margin-bottom:12px">
          勾选该 token 可访问的账户与读写权限（可写自动含可读）。保存时自动同步 gateway 的 server 级读写权限。
        </p>
        <table class="tbl">
          <thead><tr><th>账户</th><th style="width:120px;text-align:center">Read</th><th style="width:120px;text-align:center">Write</th></tr></thead>
          <tbody>
            <tr v-for="a in permAccounts" :key="a.account_id">
              <td class="cell-name mono">{{ a.account_id }}<span class="muted" style="font-size:11px"> · {{ a.description }}</span></td>
              <td style="text-align:center">
                <label class="perm-toggle read"><input type="checkbox" v-model="permModal.perms[a.account_id].read" /><span class="switch"></span></label>
              </td>
              <td style="text-align:center">
                <label class="perm-toggle write"><input type="checkbox" v-model="permModal.perms[a.account_id].write" @change="() => { if (permModal.perms[a.account_id].write) permModal.perms[a.account_id].read = true }" /><span class="switch"></span></label>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <template #footer>
        <button class="btn btn-ghost" @click="permModal = null">取消</button>
        <button class="btn btn-primary" :disabled="savingPerms" @click="savePerms">{{ savingPerms ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>
```

3. script 加状态与函数（import 加 `getAliyunAccounts, getAliyunPerms, putAliyunPerms`）：

```javascript
const permModal = ref(null)
const permAccounts = ref([])
const savingPerms = ref(false)

async function openPerms(t) {
  savingPerms.value = false
  try {
    const [accounts, perms] = await Promise.all([getAliyunAccounts(), getAliyunPerms(t.id)])
    permAccounts.value = accounts
    const map = {}
    accounts.forEach(a => { map[a.account_id] = perms.permissions[a.account_id] || { read: false, write: false } })
    permModal.value = { token_id: t.id, token_name: t.name, perms: map }
  } catch (e) {
    error.value = '加载授权矩阵失败: ' + e.message
  }
}

async function savePerms() {
  const m = permModal.value
  savingPerms.value = true; error.value = ''
  try {
    const active = {}
    for (const [accountId, p] of Object.entries(m.perms)) {
      if (p.read || p.write) active[accountId] = { read: p.read, write: p.write }
    }
    await putAliyunPerms(m.token_id, active)
    permModal.value = null
    await loadTokens()
  } catch (e) {
    error.value = '保存授权失败: ' + e.message
  } finally {
    savingPerms.value = false
  }
}
```

- [ ] **Step 5: 构建验证**

```bash
cd gateway-admin/admin-ui
npm install
npm run build
```

Expected: `vite build` 成功产出 `dist/`，无编译错误。若报 vue 语法错，按错误修正（本 plan 代码已对齐现有组件写法）。

- [ ] **Step 6: Commit**

```bash
git add gateway-admin/admin-ui/
git commit -m "feat(gateway-admin): 阿里云账户管理页 + token 授权矩阵弹窗"
```

---

### Task 10: 部署接入（compose + 根文档）+ 端到端验证

**Files:**
- Modify: `deploy/docker-compose.yml`、根 `CLAUDE.md`（端口表 + MCP 列表）、`deploy/README.md`

**Interfaces:**
- Consumes: 全部前序任务产物
- Produces: 可部署的 compose 服务 `aliyun-dns-mcp`（9054 容器内 + 9469→9464 metrics）；端到端验证记录

- [ ] **Step 1: compose 加 aliyun-dns-mcp 服务**

`deploy/docker-compose.yml` 在 serpapi-mcp 服务后加：

```yaml
  # 阿里云 DNS 管理 MCP：容器内 9054 不映射宿主（仅容器内网互访）；
  # metrics 9469 -> 容器内 9464（宿主端避开 proxy 9465 / 搜索 MCP 9466-9468）
  # 无凭证 env——账户 AccessKey 由 gateway-admin「阿里云 DNS」页写入 Redis
  aliyun-dns-mcp:
    build: ../aliyun-dns-mcp
    ports:
      - "9469:9464"
    environment:
      REDIS_URL: redis://redis:6379/0
      MCP_HOST: "0.0.0.0"
      MCP_PORT: "9054"
      LOG_FORMAT: json
      LOG_FILE: /app/logs/aliyun-dns-mcp.log
      PROMETHEUS_PORT: "9464"
    volumes:
      - ./logs/aliyun-dns-mcp:/app/logs
    networks: [mcp-net]
    depends_on: [redis]
    restart: unless-stopped
```

根 `CLAUDE.md`：
- 端口表加 `| 9054 | aliyun-dns-mcp | 阿里云 DNS 解析管理（6 tools） |`
- 「已开发 MCP」表加 `| aliyun-dns-mcp/ | Aliyun DNS MCP | 阿里云 DNS 多账户解析管理（6 tools，账户级读写权限） | ✅ 开发完成 |`
- 「接入 Gateway 流程」§5 示例后补一行：aliyun-dns-mcp 注册后需在「阿里云 DNS」页配账户、在 Token 列表配账户授权矩阵

`deploy/README.md` 服务清单加 aliyun-dns-mcp 一行。

- [ ] **Step 2: 端到端验证（关键假设验证：Authorization 头透传）**

本地起全链路（需要本地 redis + 两个 uv 服务 + admin-ui dev 或 build 产物可选）：

```bash
redis-server --daemonize yes

# 终端 1：gateway-proxy
cd gateway-proxy && REDIS_URL=redis://localhost:6379/0 uv run python server.py

# 终端 2：aliyun-dns-mcp
cd aliyun-dns-mcp && REDIS_URL=redis://localhost:6379/0 uv run python server.py

# 终端 3：gateway-admin
cd gateway-admin && REDIS_URL=redis://localhost:6379/0 JWT_SECRET=dev uv run uvicorn app:app --port 8081
```

用 curl 走管理 API 完成配置（替换 `<admin-jwt>` 为登录返回的 token）：

```bash
# 1. 登录拿 JWT
JWT=$(curl -s -X POST localhost:8081/api/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<admin密码>"}' | jq -r .token)

# 2. 注册 server（url 指向本地 9054）
curl -s -X POST localhost:8081/api/servers -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"aliyun-dns-mcp","url":"http://localhost:9054/mcp","description":"dns test"}'

# 3. 添加测试账户（probe:false 跳过真实探活）
curl -s -X POST localhost:8081/api/aliyun-accounts -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"test-acct","access_key_id":"LTAI-test","access_key_secret":"sk-test","probe":false}'

# 4. 创建 token（先开 server 级粗闸 read）
TOK=$(curl -s -X POST localhost:8081/api/tokens -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"dns-ro","permissions":{"aliyun-dns-mcp":{"read":true,"write":false}}}' | jq -r .token)
TOKID=$(curl -s localhost:8081/api/tokens -H "Authorization: Bearer $JWT" | jq -r '.[] | select(.name=="dns-ro") | .id')

# 5. 配授权矩阵：test-acct 只读（union 应保持 read:true write:false）
curl -s -X PUT localhost:8081/api/aliyun-perms/$TOKID -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"permissions":{"test-acct":{"read":true,"write":false}}}'
```

**关键验证 1（Authorization 透传）**——不带 token 直连 MCP 应拒绝，带 token 应通过鉴权（返回阿里云 API 错误而非权限错误，说明 token 到达了 MCP 并被验证）：

```bash
# 通过 gateway 调用 list_accounts（应返回 test-acct，read:true）
curl -s -X POST localhost:8082/mcp -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"aliyun-dns-mcp_list_accounts","arguments":{}}}'
```

Expected：返回结果含 `test-acct`（若网关代理返回 SSE 格式，解析 `data:` 行）。**若返回 invalid_token/权限错误，说明 Authorization 未透传——回退方案见 spec §9（gateway 加自定义 transport 转发），记录现象后提交 issue 而非继续。**

**关键验证 2（账户级读写）**：
- 对 `test-acct` 调写工具 `aliyun-dns-mcp_delete_record` → 应被 MCP 拒绝 `no_permission`（token 只有 read）
- 授权矩阵把 test-acct 改 write（PUT 同接口）→ 再调写工具 → 应通过 MCP 鉴权（进到阿里云 API 调用，报 InvalidAccessKey 或签名错——说明到 API 层了）

- [ ] **Step 3: 验证结果记录到 RELEASE.md**

`aliyun-dns-mcp/RELEASE.md` 加「端到端验证」节：验证日期、命令结果摘要、Authorization 透传结论（通过 / 回退方案），供发布前复查。

- [ ] **Step 4: 全仓测试 + Commit**

```bash
cd aliyun-dns-mcp && uv run python -m pytest tests/ -q
cd ../gateway-admin && uv run python -m pytest tests/ -q
cd ../gateway-proxy && uv run python -m pytest tests/ -q
```

Expected：三个服务测试全 PASS（gateway-proxy 应无回归——本仓库零改动，跑一遍确认）。

```bash
git add deploy/ CLAUDE.md README.md aliyun-dns-mcp/RELEASE.md
git commit -m "feat(deploy): aliyun-dns-mcp compose 接入 + 端口登记 + 端到端验证记录"
```

---

## Self-Review

对照 spec 逐节核查：

| spec 要求 | 对应任务 |
|---|---|
| 6 tools + 读写标注 + ⚠️ 写操作 | Task 5/6（list_accounts/list_domains/list_records/add/update/delete） |
| 账户级 read/write（write⇒read 不变式 + MCP 防御式判定） | Task 3 auth.require（read = read or write）；Task 8 put_perms 强制不变式 |
| Authorization 头读取（get_http_headers include_all=True） | Task 3 auth.py + Global Constraint 13 |
| token 验证（tokens:{hash}） | Task 3 `_token_id` |
| Redis schema（accounts/token_accounts/changed） | Task 2 常量 + Task 8 写入方 |
| Pub/Sub 热更新 + 断线重建 | Task 2 `_listen` + Constraint 12 |
| Alidns SDK + 错误分类 + 实测调整 | Task 4 + Task 1 Step 3 模型类名实测 |
| 工具可见性粗闸 union 同步 | Task 8 put_perms |
| gateway 零改动（§6.3） | Task 10 Step 2 端到端验证关键假设；风险触发才改（spec §9） |
| gateway-admin 账户 CRUD + 探活 | Task 8（probe 用 SDK DescribeDomains） |
| gateway-admin 授权矩阵 | Task 8 api/aliyun_perms.py + Task 9 UI |
| 凭证安全（禁入日志/metric、httpx WARNING） | Constraint 5 + Task 7 CLAUDE.md + serpapi 同款防线 |
| 端口 9054 登记 + compose | Task 1 Step 4 + Task 10 Step 1 |
| uv.lock 阿里云镜像 | Task 1 Step 2 + Task 8 Step 1 |
| 文档三件套 | Task 7 Step 4 |
| 可观测性（aliyndns_* 指标 + structlog + OTel） | Task 7 Step 1（telemetry 改名） |

**类型一致性抽查**：`ToolContext(checker, clients)` 在 Task 5 定义、Task 5/6 工具使用、Task 7 `_get_ctx` 构造——一致。`AlidnsError.error_type` 值集合（invalid_credential/throttled/not_found/api_error）与 Task 6 工具 error_type 映射一致。`AccountStore.ensure_token_loaded` 在 Task 2 定义、Task 3 调用——一致。gateway-admin 侧 `putAliyunPerms`（Task 9 UI）↔ `PUT /api/aliyun-perms`（Task 8）↔ `PermsPut` 请求体结构——一致。

**遗留待实测项**（实现中必须处理，不是占位符——任务内有明确步骤）：SDK 请求模型类名（Task 1 Step 3）、SDK 错误码分类（Task 4 Step 3 注释）、Authorization 透传行为（Task 10 Step 2，有回退方案）。
