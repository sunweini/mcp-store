"""官方用量校准：从 provider 官方接口同步 key 的 monthly_quota + remaining。

为什么需要校准：本地 monthly_quota 是录入时手填的，remaining 只在探活/搜索
响应顺带更新——套餐升降级、网关外消耗都会让两者偏离官方真实值。校准直接
读官方 usage 接口覆写，管理界面和 MCP key 挑选（按 remaining 排序、按
quota 算低配额档位）拿到的就是权威数字。

失败语义：官方请求失败只在该 key 记 error，绝不改存储记录——provider
瞬时故障不能把已有的配额数据抹掉（宁要旧值，不要空值）。

brave 无独立 usage 端点：借一次真实搜索的响应头（x-ratelimit-policy /
x-ratelimit-remaining）读月窗口配额，每次校准每 key 消耗 1 次官方配额。
"""
import json
import os

import httpx
import structlog

from redis_client import get_redis

logger = structlog.get_logger()

# usage 接口是轻量元数据读取，10s 足够；再慢说明 provider 有问题，
# 宁可记 error 也不拖住管理面请求（超时记 error、不改值，下次校准可重试）
CALIBRATE_TIMEOUT = 10


async def fetch_tavily_usage(key: str, client: httpx.AsyncClient | None = None) -> dict | None:
    """Tavily 官方用量：GET /usage，Bearer 鉴权。

    实测响应格式：{"key": {"usage": N}, "account": {"plan_usage": 112,
    "plan_limit": 1000, ...}}。取 account 级字段——key.usage 只是单 key
    累计，配额口径在 account（同账号多 key 共享套餐）。

    返回 {"quota": int, "remaining": int}；任何失败（非 200 / 空 body /
    字段缺失 / 网络异常）返回 None，由调用方决定不改值。

    client 仅供测试注入 MockTransport——生产调用方不传，内部自建
    （timeout=CALIBRATE_TIMEOUT）。
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=CALIBRATE_TIMEOUT)
    try:
        resp = await client.get("https://api.tavily.com/usage",
                                headers={"Authorization": f"Bearer {key}"})
        # 空 body 不能调 json()（serpapi 系接口同样踩过的坑，见 keys._probe_key）
        if resp.status_code != 200 or not resp.content:
            logger.warning("calibrate_fetch_failed", provider="tavily",
                           status=resp.status_code, service="gateway-admin")
            return None
        body = resp.json()
        account = body.get("account") or {}
        plan_limit = account.get("plan_limit")
        plan_usage = account.get("plan_usage")
        if not isinstance(plan_limit, int) or not isinstance(plan_usage, int):
            # 响应 schema 漂移（如返回了未知套餐结构）：拒绝写入猜测值
            logger.warning("calibrate_bad_schema", provider="tavily",
                           service="gateway-admin")
            return None
        # 超用套餐可能 usage > limit；remaining 负数会弄坏下游 ratio
        # 计算（usage 端点、MCP quota 指标），与探活一致 clamp 到 0
        return {"quota": plan_limit, "remaining": max(plan_limit - plan_usage, 0)}
    except Exception as e:
        # 网络/超时/JSON 解析统一归为拉取失败，不向调用方抛
        logger.warning("calibrate_fetch_failed", provider="tavily",
                       error=str(e), service="gateway-admin")
        return None
    finally:
        if owns_client:
            await client.aclose()


async def fetch_serpapi_usage(key: str, client: httpx.AsyncClient | None = None) -> dict | None:
    """SerpAPI 官方用量：GET /account?api_key=<key>。

    实测响应格式：{..., "searches_per_month": 250, "plan_searches_left": 237, ...}。
    注意 api_key 走 query param 是 SerpAPI 官方唯一鉴权方式（无 header 方案）。

    返回 {"quota": int, "remaining": int}；失败返回 None。
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=CALIBRATE_TIMEOUT)
    try:
        resp = await client.get("https://serpapi.com/account",
                                params={"api_key": key})
        body = resp.json() if resp.content else {}
        # serpapi 的错误会放进 200 响应体（与 /search 同样的怪癖，
        # 探活代码已踩过）：非 200 或 body 带 error 都算失败
        if resp.status_code != 200 or "error" in body:
            logger.warning("calibrate_fetch_failed", provider="serpapi",
                           status=resp.status_code, service="gateway-admin")
            return None
        quota = body.get("searches_per_month")
        remaining = body.get("plan_searches_left")
        if not isinstance(quota, int) or not isinstance(remaining, int):
            logger.warning("calibrate_bad_schema", provider="serpapi",
                           service="gateway-admin")
            return None
        return {"quota": quota, "remaining": max(remaining, 0)}
    except Exception as e:
        logger.warning("calibrate_fetch_failed", provider="serpapi",
                       error=str(e), service="gateway-admin")
        return None
    finally:
        if owns_client:
            await client.aclose()


async def fetch_brave_usage(key: str, client: httpx.AsyncClient | None = None) -> dict | None:
    """Brave 官方用量：借一次真实搜索的响应头读配额（无独立 usage 端点）。

    每次搜索响应都带多窗口限流头（位置一一对应）：
      x-ratelimit-policy:    "1;w=1, 2000;w=2678400"  # limit;w=窗口秒数
      x-ratelimit-remaining: "0, 1977"
    取 window 最大的段（月窗口 w≈2678400）作为 quota/remaining——秒级
    窗口只限突发速率，对配额管理无意义。

    代价：必须发一次真实请求才有头（消耗 1 次官方配额）；校准为人工
    低频触发，可接受。

    返回 {"quota": int, "remaining": int}；缺头/格式不解析/长度不对齐/
    非 200/网络异常均返回 None（调用方语义：不改值）。
    """
    owns_client = client is None
    if owns_client:
        # 与 keys._probe_key 同一网络约束：api.search.brave.com 直连不通，
        # 必须走 SEARCH_PROXY；calibrate_provider 传入的共享 client 同理
        client = httpx.AsyncClient(timeout=CALIBRATE_TIMEOUT,
                                   proxy=os.environ.get("SEARCH_PROXY") or None)
    try:
        resp = await client.get("https://api.search.brave.com/res/v1/web/search",
                                params={"q": "test", "count": 1},
                                headers={"X-Subscription-Token": key})
        if resp.status_code != 200:
            logger.warning("calibrate_fetch_failed", provider="brave",
                           status=resp.status_code, service="gateway-admin")
            return None
        policy_raw = resp.headers.get("x-ratelimit-policy")
        remaining_raw = resp.headers.get("x-ratelimit-remaining")
        if not policy_raw or not remaining_raw:
            # 响应没带限流头（schema 变更/边缘节点异常）：拒绝写入猜测值
            logger.warning("calibrate_bad_schema", provider="brave",
                           reason="missing_ratelimit_headers", service="gateway-admin")
            return None
        try:
            # 官方分隔是 ", "，按 "," split + strip 兼容无空格变体
            policy = []
            for seg in policy_raw.split(","):
                limit_part, sep, window_part = seg.strip().partition(";w=")
                if not sep:
                    raise ValueError(f"bad policy segment: {seg!r}")
                policy.append((int(limit_part), int(window_part)))
            remaining = [int(p.strip()) for p in remaining_raw.split(",")]
        except ValueError:
            logger.warning("calibrate_bad_schema", provider="brave",
                           reason="unparsable_ratelimit_headers", service="gateway-admin")
            return None
        if len(policy) != len(remaining):
            # 位置对应是解析前提；长度不齐说明格式漂移，无法对齐两列
            logger.warning("calibrate_bad_schema", provider="brave",
                           reason="policy_remaining_length_mismatch", service="gateway-admin")
            return None
        # w 最大即月窗口；remaining 异常为负时 clamp 0（与其余 fetcher 一致）
        i = max(range(len(policy)), key=lambda idx: policy[idx][1])
        return {"quota": policy[i][0], "remaining": max(remaining[i], 0)}
    except Exception as e:
        logger.warning("calibrate_fetch_failed", provider="brave",
                       error=str(e), service="gateway-admin")
        return None
    finally:
        if owns_client:
            await client.aclose()


async def calibrate_provider(provider: str) -> dict:
    """校准单个 provider 的全部 key，返回摘要。

    摘要格式：{"provider", "supported", "keys": [{key_id, quota, remaining, error?}]}
    —— keys 里每项要么有 quota+remaining（已更新），要么有 error（未改值）。

    为什么在这里 publish：MCP 的 key_pool 收到 channel 任意消息即整体
    reload（keyspace 语义），配额变了不通知的话 MCP 内存池继续用旧
    quota/remaining 挑 key，低配额档位判断会失真。
    """
    # 运行时按模块全局名查找 fetcher（不用 dict 缓存函数引用）——
    # 测试 monkeypatch 模块属性才能生效
    if provider == "tavily":
        fetcher = fetch_tavily_usage
    elif provider == "serpapi":
        fetcher = fetch_serpapi_usage
    elif provider == "brave":
        # brave 无独立 usage 端点，fetcher 借真实搜索响应头读月窗口配额
        fetcher = fetch_brave_usage
    else:
        # 未知 provider 无官方用量接口：连 Redis 记录都不读，
        # 本地计数就是事实源，校准不产生任何副作用
        return {"provider": provider, "supported": False, "keys": []}

    r = get_redis()
    entries = await r.hgetall(f"search:keys:{provider}")
    keys_out: list[dict] = []
    updated_ids: list[str] = []
    # brave 必须走 SEARCH_PROXY（域名直连不通，与 keys._probe_key 同一
    # 网络约束）；tavily/serpapi 直连可达，不绕代理多一跳
    proxy = (os.environ.get("SEARCH_PROXY") or None) if provider == "brave" else None
    # 同一 provider 全部 key 共用一个 client：复用连接，超时统一
    async with httpx.AsyncClient(timeout=CALIBRATE_TIMEOUT, proxy=proxy) as client:
        for key_id, payload in entries.items():
            try:
                rec = json.loads(payload)
            except json.JSONDecodeError:
                # 与 list_keys 的脏数据策略一致：跳过损坏记录，不让整批校准 500
                keys_out.append({"key_id": key_id, "error": "corrupt record"})
                continue
            try:
                usage = await fetcher(rec.get("key", ""), client=client)
            except Exception as e:
                # fetcher 契约是失败返回 None，这里是兜底——任何异常都不改值
                logger.warning("calibrate_fetch_failed", provider=provider,
                               key_id=key_id, error=str(e), service="gateway-admin")
                usage = None
            if usage is None:
                keys_out.append({"key_id": key_id,
                                 "error": "official usage unavailable"})
                continue
            rec["monthly_quota"] = usage["quota"]
            rec["remaining"] = usage["remaining"]
            await r.hset(f"search:keys:{provider}", key_id,
                         json.dumps(rec, ensure_ascii=False))
            updated_ids.append(key_id)
            keys_out.append({"key_id": key_id, "quota": usage["quota"],
                             "remaining": usage["remaining"]})

    if updated_ids:
        # 一次 publish 即可（MCP 收到任意消息整体 reload）；key_id 只用于排障
        await _publish_calibrated(provider, updated_ids[-1])
    logger.info("calibrate_done", provider=provider, updated=len(updated_ids),
                errors=len(keys_out) - len(updated_ids), service="gateway-admin")
    return {"provider": provider, "supported": True, "keys": keys_out}


async def _publish_calibrated(provider: str, key_id: str) -> None:
    """复用 keys._publish 的 try/except 兜底（publish 失败不阻断校准）。

    顶层 import api.keys 会形成循环依赖（api.keys 顶层 import 本模块），
    所以延迟到调用时导入。
    """
    from api.keys import _publish
    await _publish("calibrate", provider, key_id)
