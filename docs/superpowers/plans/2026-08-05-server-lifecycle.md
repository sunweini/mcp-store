# 后端 MCP Server 禁用/停用实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** gateway-admin 可对后端 MCP server 做「禁用」（gateway 移除、容器继续跑）与「停用」（gateway 移除、容器手动停），「启用」恢复；gateway-proxy 按 status 决定是否挂载。

**Architecture:** `servers:<name>` hash 增 `status` 字段（active/disabled/stopped）。proxy registry 挂载/热更新时只挂 active；admin 新增 lifecycle 端点改 status 并 publish `server:changed`；前端 Servers 页加状态徽标与禁用/停用/启用按钮。

**Tech Stack:** FastMCP 4.0.0b1（proxy registry）、FastAPI + redis.asyncio（admin）、Vue 3（Servers.vue）、fakeredis + pytest（测试）。

## Global Constraints

- status 三值：active（默认）/ disabled / stopped；`servers:active` set 保持"已注册"语义不变
- 仅 status==active 挂载；disabled/stopped 卸载（tools/list + call 不可用）
- admin 不控制 docker（不挂 docker.sock），停/起容器为人工
- 旧数据无 status 字段时默认 active（兼容）
- 复用现有 `_publish_change(action, name)` pubsub（channel `server:changed`）
- 注释写"为什么"；require_admin 鉴权

---

### Task 1: gateway-proxy registry 按 status 挂载

**Files:**
- Modify: `gateway-proxy/registry.py`
- Test: `gateway-proxy/tests/test_registry.py`

**Interfaces:**
- Produces: `mount_all` 只挂 active；`watch_changes` 处理 disable/stop/enable/add/update；新增辅助 `_sync_one(gateway, name, info)`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_registry.py`）

```python
# ─── status 挂载控制 ─────────────────────────────────────────────

@pytest.fixture
def mount_log(monkeypatch):
    """记录 _mount_one/_unmount_one 调用，避免真连后端。"""
    import registry
    log = {"mount": [], "unmount": []}
    async def fake_mount(gw, name, url): log["mount"].append((name, url))
    async def fake_unmount(gw, name): log["unmount"].append(name)
    monkeypatch.setattr(registry, "_mount_one", fake_mount)
    monkeypatch.setattr(registry, "_unmount_one", fake_unmount)
    return log


class FakeGW: pass


async def test_mount_all_skips_non_active(fake_redis, mount_log):
    import registry
    await fake_redis.sadd("servers:active", "a", "b", "c")
    await fake_redis.hset("servers:a", mapping={"url": "http://a", "status": "active"})
    await fake_redis.hset("servers:b", mapping={"url": "http://b", "status": "disabled"})
    await fake_redis.hset("servers:c", mapping={"url": "http://c", "status": "stopped"})
    await registry.mount_all(FakeGW())
    assert mount_log["mount"] == [("a", "http://a")]


async def test_mount_all_default_active_when_no_status(fake_redis, mount_log):
    """旧数据无 status 字段 -> 默认 active（兼容）。"""
    import registry
    await fake_redis.sadd("servers:active", "old")
    await fake_redis.hset("servers:old", mapping={"url": "http://old"})
    await registry.mount_all(FakeGW())
    assert mount_log["mount"] == [("old", "http://old")]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway-proxy && uv run python -m pytest tests/test_registry.py -v -k mount_all`
Expected: FAIL（现有 mount_all 不按 status 过滤，b/c 也被挂载）

- [ ] **Step 3: 实现 status 挂载**（`registry.py`）

mount_all 循环内、`url` 检查后加：

```python
        status = info.get("status", "active")
        if status != "active":
            logger.warning("mount_all_skip", server=name, reason=f"status={status}", service="gateway-proxy")
            continue
```

mount_all 之后加辅助 + 改 watch_changes：

```python
async def _sync_one(gateway, name: str, info: dict) -> None:
    """按 status 同步挂载：先卸载，仅 active 且有 url 才挂载。

    先 unmount 再 mount 保证 disable->enable 切换后 provider 是新的。
    """
    await _unmount_one(gateway, name)
    url = info.get("url")
    if info.get("status", "active") == "active" and url:
        await _mount_one(gateway, name, url)
```

watch_changes 的消息分支替换为：

```python
            action, name = parsed
            info = await r.hgetall(f"servers:{name}")
            if action == "remove":
                await _unmount_one(gateway, name)
            elif action in ("add", "update", "enable", "disable", "stop") and info:
                await _sync_one(gateway, name, info)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd gateway-proxy && uv run python -m pytest tests/test_registry.py -v`
Expected: PASS（新增 2 + 现有无回归）

- [ ] **Step 5: Commit**

```bash
git add gateway-proxy/registry.py gateway-proxy/tests/test_registry.py
git commit -m "feat(gateway-proxy): mount backends only when status=active (disable/stop unmount)"
```

---

### Task 2: gateway-admin lifecycle 端点

**Files:**
- Modify: `gateway-admin/api/servers.py`
- Test: `gateway-admin/tests/test_servers.py`

**Interfaces:**
- Produces: `POST /api/servers/{name}/lifecycle` body `{action: disable|stop|enable}` -> `{name, status}`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_servers.py`）

```python
# ─── lifecycle（禁用/停用/启用）─────────────────────────────────

def _seed_server(fake_redis, name="srv-a"):
    import json
    fake_redis.sadd("servers:active", name)
    fake_redis.hset(f"servers:{name}", mapping={"name": name, "url": "http://x", "status": "active"})
    return name


async def test_lifecycle_disable_sets_status(fake_redis, client, auth_headers):
    name = _seed_server(fake_redis)
    resp = client.post(f"/api/servers/{name}/lifecycle", json={"action": "disable"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    assert await fake_redis.hget(f"servers:{name}", "status") == "disabled"


async def test_lifecycle_stop_and_enable(fake_redis, client, auth_headers):
    name = _seed_server(fake_redis)
    client.post(f"/api/servers/{name}/lifecycle", json={"action": "stop"}, headers=auth_headers)
    assert await fake_redis.hget(f"servers:{name}", "status") == "stopped"
    resp = client.post(f"/api/servers/{name}/lifecycle", json={"action": "enable"}, headers=auth_headers)
    assert resp.json()["status"] == "active"


async def test_lifecycle_invalid_action_422(fake_redis, client, auth_headers):
    name = _seed_server(fake_redis)
    resp = client.post(f"/api/servers/{name}/lifecycle", json={"action": "boom"}, headers=auth_headers)
    assert resp.status_code == 422


async def test_lifecycle_missing_server_404(client, auth_headers):
    resp = client.post("/api/servers/nope/lifecycle", json={"action": "disable"}, headers=auth_headers)
    assert resp.status_code == 404
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway-admin && uv run python -m pytest tests/test_servers.py -v -k lifecycle`
Expected: FAIL（404 路由不存在）

- [ ] **Step 3: 实现 lifecycle 端点**（`servers.py`，refresh-tools 路由后追加）

```python
_LIFECYCLE = {"disable": "disabled", "stop": "stopped", "enable": "active"}


class LifecycleAction(BaseModel):
    action: str


@router.post("/{name}/lifecycle")
async def set_lifecycle(name: str, req: LifecycleAction, _: str = Depends(require_admin)):
    """禁用/停用/启用 server：只改 status + 通知 gateway 热更新。

    停/起容器为人工操作（admin 不控 docker），此端点只管 gateway 清单。
    """
    r = get_redis()
    if not await r.exists(f"servers:{name}"):
        raise HTTPException(status_code=404, detail="server not found")
    if req.action not in _LIFECYCLE:
        raise HTTPException(status_code=422, detail=f"action must be one of {list(_LIFECYCLE)}")
    status = _LIFECYCLE[req.action]
    await r.hset(f"servers:{name}", "status", status)
    await _publish_change(req.action, name)
    return {"name": name, "status": status}
```

（BaseModel/HTTPException/Depends/require_admin 已在文件顶部导入；create_server 默认 status=active 已有则不动，若无在 create 的 hset mapping 加 `"status": "active"`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd gateway-admin && uv run python -m pytest tests/test_servers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gateway-admin/api/servers.py gateway-admin/tests/test_servers.py
git commit -m "feat(gateway-admin): server lifecycle endpoint (disable/stop/enable)"
```

---

### Task 3: 前端 Servers.vue 状态徽标 + 生命周期按钮

**Files:**
- Modify: `gateway-admin/admin-ui/src/views/Servers.vue`
- Modify: `gateway-admin/admin-ui/src/api/index.js`

**Interfaces:**
- Consumes: Task 2 `POST /api/servers/{name}/lifecycle`、list 已返回 status

- [ ] **Step 1: api/index.js 加函数**

```javascript
export function lifecycleServer(name, action) { return apiFetch(`/api/servers/${name}/lifecycle`, { method: 'POST', body: JSON.stringify({ action }) }) }
```

- [ ] **Step 2: Servers.vue 加状态徽标**（head 区现有 status-chip 旁加 lifecycle 徽标）

模板在 `class="status-chip"` 健康 chip 后加：

```html
<span class="status-chip" :class="lcClass(s.status)">{{ lcLabel(s.status) }}</span>
```

script 加：

```javascript
import { ..., lifecycleServer } from '../api/index.js'
const LC = { active: ['运行', 'ok'], disabled: ['禁用', 'warn'], stopped: ['停用', 'dim'] }
function lcLabel(st) { return (LC[st] || LC.active)[0] }
function lcClass(st) { return (LC[st] || LC.active)[1] }
async function doLifecycle(s, action) {
  if (action === 'stop' && !confirm(`停用 ${s.name}：gateway 将移除该服务，容器需手动停止。继续？`)) return
  await lifecycleServer(s.name, action)
  await load()
}
```

操作按钮区（doDelete 前）加：

```html
<button v-if="s.status === 'active'" class="mini-btn" @click="doLifecycle(s, 'disable')">禁用</button>
<button v-if="s.status === 'active'" class="mini-btn" @click="doLifecycle(s, 'stop')">停用</button>
<button v-else class="mini-btn" @click="doLifecycle(s, 'enable')">启用</button>
```

style 加 `.status-chip.warn{...黄} .status-chip.dim{...灰}`（对齐现有 chip 样式 token）。

- [ ] **Step 3: 构建验证**

```bash
cd gateway-admin/admin-ui && npm run build
```
Expected: dist 生成无错误

- [ ] **Step 4: Commit**

```bash
git add gateway-admin/admin-ui/
git commit -m "feat(gateway-admin): Servers page lifecycle badge + disable/stop/enable buttons"
```

---

### Task 4: 部署 + 端到端验证

- [ ] **Step 1: 同步代码到生产**

```bash
git archive --format=tar.gz -o /tmp/lc-deploy.tar.gz HEAD
scp -i ~/.ssh/id_loginmonitor -P 9166 /tmp/lc-deploy.tar.gz root@10.33.17.72:/tmp/
ssh root@10.33.17.72 "mkdir -p /tmp/lc-new && tar xzf /tmp/lc-deploy.tar.gz -C /tmp/lc-new && cp -r /tmp/lc-new/gateway-proxy/* /opt/mcp-gateway-cfg/gateway-proxy/ && cp -r /tmp/lc-new/gateway-admin/* /opt/mcp-gateway-cfg/gateway-admin/ && cd /opt/mcp-gateway-cfg/deploy && docker compose up -d --build gateway-proxy gateway-admin"
```

- [ ] **Step 2: 端到端验证**

```bash
# 1. 禁用某 server -> gateway tools/list 不含其工具
#    POST /api/servers/tavily-mcp/lifecycle {"action":"disable"}
#    用 token 调 gateway tools/list -> 无 tavily-mcp_* 工具
# 2. 启用 -> 恢复
#    POST .../lifecycle {"action":"enable"} -> tools/list 含 tavily-mcp_*
# 3. 停用 -> tools/list 不含（容器需手动停，验证 gateway 移除即可）
```

- [ ] **Step 3: Commit 验证记录**

```bash
git commit --allow-empty -m "chore: verify server lifecycle e2e on production"
```

---

## Self-Review 记录

**Spec 覆盖**：
- 禁用（status=disabled，gateway 移除，容器跑）-> Task 1/2/3 ✅
- 停用（status=stopped，gateway 移除，容器手动）-> Task 1/2/3 ✅
- 启用恢复 -> Task 1/2/3 ✅
- mount_all 只挂 active -> Task 1
- watch_changes disable/stop->unmount、enable/add/update->sync -> Task 1
- lifecycle 端点 404/422 -> Task 2
- list_servers 返回 status（已有）-> 无需改（确认兼容）
- 前端徽标+按钮 -> Task 3
- 旧数据默认 active -> Task 1 测试

**类型一致性**：
- status 三值 active/disabled/stopped 全任务一致
- lifecycle action disable/stop/enable -> status 映射 _LIFECYCLE 一致
- _publish_change(action,name) 复用；watch_changes 识别同名 action

**坑位预判**：
1. watch_changes 现有 `if action in ("add","update") and info:` 分支要整体替换（含 else remove），别留两套逻辑
2. _mount_one 内部会写 tools 到 redis；_sync_one 卸载后不 mount 时 tools 残留 redis 但 list_servers 显示 status，可接受
3. Servers.vue 的 confirm 仅 stop 加（防误操作），disable/enable 不加
4. 前端 lcClass 用现有 chip 样式 token（ok/warn/dim），若 tokens.css 无 warn/dim 需补
