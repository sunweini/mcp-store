# 多 API Key 池设计模式（search-mcp）

> 来源：tavily-mcp / brave-mcp / serpapi-mcp 开发实践（2026-08）。
> 适用：任何需要"多 key 管理 + 自动切换"的 MCP / 服务。

## 为什么需要

外部 API（搜索、LLM、地图等）通常按 key 配额计费：
- 免费/付费额度有限（tavily 月 1000 次、brave 月 2000 次、serpapi 月 100 次）
- 单 key 可能欠费、超限、被限流
- 配多个 key 需要：负载均衡（轮换）+ 失效自动剔除 + 配额感知

## 核心设计

### 1. 存储：Redis Hash + ZSet + Pub/Sub

```
search:keys:<provider>                 Hash — key 池
  <key_id> → JSON {
    "key": "tvly-...",                 # 明文（内网）
    "enabled": true,
    "monthly_quota": 1000,
    "status": "active|low_quota_warning|low_quota|invalid|exhausted|cooldown",
    "cooldown_until": null | ISO8601,
    "remaining": null | int,           # 官方剩余配额（有则用）
    "last_used_at": null | ISO8601,
    "last_error": null | str,
    "created_at": ISO8601
  }

search:usage:<provider>:<key_id>       ZSet — 本地用量计数
  member=epoch_ms, score=epoch_ms       # 按月窗口滚动统计

search:keys:channel                    Pub/Sub — key 变更通知
  {"provider": "tavily", "action": "upsert|delete", "key_id": "..."}
```

设计要点：
- **key_id 与 key 明文解耦**：key_id 是 `provider_uuid12`，日志/metrics 只带 key_id（高基数+敏感禁入 label）
- **管理端（gateway-admin）写，MCP 读**：管理端写后 PUBLISH，MCP 热更新不重启
- **Redis 是唯一事实源**：MCP 启动加载 + Pub/Sub 增量刷新

### 2. 轮询选择（配额感知）

```python
async def next_key(self) -> dict | None:
    """优先级：
    1. enabled 且 status 非 invalid/exhausted
    2. cooldown 未过期的跳过
    3. low_quota（剩余<5%）跳过——仅池内其余全不可用时兜底
    4. 多 key 时优先 remaining 高者，tie 按配置顺序
    """
```

关键语义：
- **low_quota（<5%）≠ 剔除**——跳过正常轮询但保留兜底，不浪费剩余配额
- **low_quota_warning（<10%）**——正常参与轮询，前台告警提示
- **remaining 无官方数据时**（多数源无用量接口）用 `quota - 本地当月计数` 估算；quota 未知则不触发阈值

### 3. 错误分类 → key 状态机

| 错误 | 分类 | 状态 | 说明 |
|---|---|---|---|
| 401/403 | INVALID | `invalid` 永久剔除 | key 失效 |
| 429 | RATE_LIMIT | `cooldown` 冷却 | 用 Retry-After 头（上限 600s） |
| 配额耗尽（body 关键词） | EXHAUSTED | `exhausted` 永久剔除 | remaining=0 |
| **超时/网络错误** | 不分类 | **不写 key 状态** | 瞬时问题不剔 key！ |

**最大的坑**：`classify_error(exc) or EXHAUSTED` 兜底会把超时误标为欠费，一次超时永久杀死有效 key。正确做法：**只有明确分类的错误才写 key 状态**。

### 4. 热更新 + 断线自愈

```python
async def _listen(self):
    while True:
        try:
            msg = await self._pubsub.get_message(timeout=30)
            if msg and msg.get("type") == "message":
                await self.reload()
        except Exception:
            await self._resubscribe()   # 关键：重建 pubsub 订阅
            await asyncio.sleep(5)

async def _resubscribe(self):
    # redis-py 的 pubsub 连接死后不会自动重连！
    # 不重建则 get_message 永远抛错，热更新永久失效（只能重启进程）
    try:
        await self._pubsub.aclose()
    except Exception:
        pass
    self._pubsub = self._redis.pubsub()
    await self._pubsub.subscribe("search:keys:channel")
```

坑：
- **redis-py ≥6 `get_message(ignore_subscribe=True)` 参数已改名** `ignore_subscribe_messages`——旧名必 TypeError 静默失效。省略该参数 + `type=="message"` 过滤最稳
- **subscribe 确认消息**（type="subscribe"）要过滤，否则每次订阅触发一次 reload

### 5. 成功路径不覆盖低配额状态

```python
async def on_success(self, key_id, remaining=None):
    # 不能无条件 status="active"——会覆盖 next_key 算出的
    # low_quota_warning/low_quota，前台永远看不到告警
    ratio = self._ratio(rec)
    if ratio is None: rec["status"] = "active"
    elif ratio < LOW_QUOTA_RATIO: rec["status"] = "low_quota"
    elif ratio < WARN_QUOTA_RATIO: rec["status"] = "low_quota_warning"
    else: rec["status"] = "active"
```

### 6. 探活（添加 key 时）

- 添加 key 发一次最小查询（`q="ping"`, max_results=1）验证有效性
- **消耗 1 次真实配额**——serpapi 月 100 次时频繁删加重加会吃光配额，前台提示
- 探活成功写 remaining（若源有用量接口），失败标 invalid 不入池
- 失败原因存 last_error 前台可见

## 三源 API 差异速查（tavily/brave/serpapi）

| 维度 | tavily | brave | serpapi |
|---|---|---|---|
| 认证 | `Authorization: Bearer` | `X-Subscription-Token` | query `api_key` |
| 端点 | POST api.tavily.com/{search\|extract\|crawl\|map\|research} | GET api.search.brave.com/res/v1/web\|local/search | GET serpapi.com/search.json?engine= |
| 无效 key 错误码 | 401/403 | **422 + body 含 "subscription token is invalid"**（实测非 401！） | 401 |
| 配额耗尽信号 | 429 | 429 | **200 + body 含 "account has exceeded quota"** |
| 用量接口 | GET /usage（官方 remaining） | 无 | 无 |
| 超时建议 | 5s 普通 / 60s crawl/research | 5s | **10s**（聚合多引擎慢，5s 实测超时） |
| 网络 | 直连（AWS） | **常需代理**（IPv4 被墙 + IPv6 不可达） | 直连 |

关键教训：
- **错误码不能裸匹配**：brave 422 有参数错误语义，只有 body 文本匹配才判 INVALID；serpapi 欠费是 200 + body
- **先实测再假设**：brave 无效 key 返回 422 而非 401，全靠真实 key 测试发现
- **敏感信息**：serpapi 的 api_key 在 URL query，httpx 默认 INFO 日志打印完整 URL 会泄漏——必须提 WARNING 或重写日志格式

## 可复用组件

三个 MCP 的 key_pool.py 逻辑相同（错误映射不同），复制三份（遵守"每 MCP 独立发布"约定）。新搜索源 MCP：
1. 复制 key_pool.py + 改 provider 名
2. 写 client（认证/端点/错误映射按上表）
3. server.py 配 QUOTA_DEFAULT + SEARCH_PROXY（如需代理）
4. 接入 gateway-admin（`/api/search-keys/{provider}` 自动支持，改 PROVIDERS 常量）
