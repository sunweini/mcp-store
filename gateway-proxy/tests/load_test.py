"""gateway-proxy 并发压测脚本（独立运行，非 pytest 测试）。

打 gateway /mcp（tools/call），验证并发加固（Task 1-7）效果：
  - 高并发下所有请求 HTTP 200（无 403/5xx —— token 缓存降级防 403 风暴）
  - 全量调用（success + denied）XADD 审计流：XLEN == 请求总数
  - denied 批次（invalid token）必须被拒（result.isError=true）且 HTTP 200，
    绝不放行 —— 若 token 缓存/权限链路退化导致 denied 放行即失败

两种运行模式：
  A. --mock（默认推荐，零外部依赖）：进程内起 mock 后端 + gateway
     （FakeRedis，不走真实 Redis/MySQL），无 API key 也全部通过。
     CI / 本地快速验证用。mock 模式 success 批次强制要求 isError=false。
  B. --url（打真实 gateway）：需先起好 gateway + 后端 MCP（如 tavily-mcp）
     + token；后端无真实 key 时 success 批次返回 isError（upstream error），
     默认不视为失败（--strict 开启才要求全成功）。

用法:
  # A. 进程内 mock，三档并发（本地压测主路径；--no-telemetry 避免
  #    console span 导出拖垮吞吐，强烈建议加）
  uv run python tests/load_test.py --mock --no-telemetry --concurrency 100  --requests 500
  uv run python tests/load_test.py --mock --no-telemetry --concurrency 500  --requests 2500
  uv run python tests/load_test.py --mock --no-telemetry --concurrency 1000 --requests 5000

  # B. 打真实 gateway（本地起 tavily-mcp + gateway + admin 注册 + token）
  uv run python tests/load_test.py --url http://localhost:8082/mcp \
      --token <token> --concurrency 500 --requests 2500 \
      --redis-url redis://localhost:6379/0   # 校验 XADD 数

退出码：0 = 全部断言通过；1 = 任一失败（HTTP 非 200 / 传输异常 / denied
放行 / rpc error / strict 下 upstream error / XADD 数不齐）。

本地压测前提（--url 模式）：
  1. 本地 Redis：redis-server（默认 6379）
  2. 起后端 MCP：cd tavily-mcp && REDIS_URL=redis://localhost:6379/0 uv run python server.py
  3. 起 gateway：cd gateway-proxy && REDIS_URL=redis://localhost:6379/0 uv run python server.py
  4. 注册 server + 建 token（经 gateway-admin: http://localhost:8081），
     或直接 seed Redis（免 admin）：
       redis-cli SADD servers:active tavily-mcp
       redis-cli HSET servers:tavily-mcp url http://localhost:9050/mcp status active
       TH=$(python3 -c 'import hashlib;print(hashlib.sha256(b"loadtest-token").hexdigest())')
       redis-cli HSET "tokens:$TH" id t1 name loadtest \
         permissions '{"tavily-mcp":{"read":true,"write":true}}'
  5. 运行：uv run python tests/load_test.py --url http://localhost:8082/mcp \
       --token loadtest-token

压测覆盖点（Task 6 deferred minor 说明）：
  KeyPool 的 pipeline 一次往返收益（on_success 把 hset+zadd+expire 合并为
  一次 RTT）在单测里是 FakeRedis 替身验证；真实收益需对真 Redis 验证。
  方法：压测进行中另开终端 `redis-cli MONITOR`，观察 on_success 写回为
  单条 pipeline EXEC 而非三次往返。本脚本不内置该检查（--redis-url 仅用于
  审计 XLEN 校验）。
  另：--negative-ratio 批次即"token 缓存降级验证"的请求级形态——denied
  必须走 HTTP 200 + isError 而非 403/放行。

注意：本文件会被 pytest 的 *_test.py 收集规则 import（load_test.py 匹配
*_test.py 模式，pytest 默认 python_files 含该模式），但模块顶层无任何
副作用（server/fakeredis/uvicorn 都在 main 内惰性 import），收集不产生
测试用例，pytest 运行不受影响。
"""
import argparse
import asyncio
import hashlib
import json
import os
import random
import sys
import time

import httpx


def _rpc_body(req_id: int, tool: str, arguments: dict) -> dict:
    """构造 2026-07-28 单次交换 envelope（缺 _meta 会被协议阶梯以 400 拒收）。"""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": arguments,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "gateway-load-test", "version": "1.0"},
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


def _headers(token: str, tool: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2026-07-28",
        "mcp-method": "tools/call",
        "mcp-name": tool,
    }


def _parse_body(resp: httpx.Response) -> dict | None:
    """解析响应体：FastMCP 默认 SSE 帧 / json_response 纯 JSON 两种都兼容。"""
    ct = resp.headers.get("content-type", "")
    try:
        if ct.startswith("application/json"):
            return resp.json()
        if "text/event-stream" in ct:
            data = None
            for line in resp.text.splitlines():
                if line.startswith("data: "):
                    data = line[6:]
            return json.loads(data) if data else None
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        return None


async def _do_one(
    client: httpx.AsyncClient,
    url: str,
    headers_ok: dict,
    headers_bad: dict,
    req: dict,
    timeout: float,
) -> dict:
    """单个 tools/call。返回 kind 分类，绝不抛异常（分类失败由断言兜底）。"""
    start = time.perf_counter()
    hdrs = headers_bad if req["negative"] else headers_ok
    try:
        resp = await client.post(url, json=req["body"], headers=hdrs, timeout=timeout)
    except httpx.HTTPError as e:
        return {"kind": "transport_error", "detail": type(e).__name__, "latency_ms": (time.perf_counter() - start) * 1000}
    latency_ms = (time.perf_counter() - start) * 1000
    if resp.status_code != 200:
        return {"kind": "http_error", "detail": str(resp.status_code), "latency_ms": latency_ms}
    body = _parse_body(resp)
    if body is None:
        return {"kind": "rpc_error", "detail": "unparseable-body", "latency_ms": latency_ms}
    if "error" in body:
        msg = (body["error"] or {}).get("message", "")
        return {"kind": "rpc_error", "detail": str(msg)[:80], "latency_ms": latency_ms}
    result = body.get("result") or {}
    is_err = bool(result.get("isError"))
    if req["negative"]:
        # denied 批次：必须被拒（isError=true），且拒绝文案可见（middleware
        # 产出 "Denied: <tool>" / "Permission denied: <error_type>" 两类，
        # 仅按关键词判断，不锁死具体文案）。
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        if not is_err:
            return {"kind": "negative_passed", "detail": "denied 批次被放行", "latency_ms": latency_ms}
        if "denied" not in text.lower():
            return {"kind": "negative_unexpected", "detail": text[:60], "latency_ms": latency_ms}
        return {"kind": "denied", "detail": "", "latency_ms": latency_ms}
    if is_err:
        return {"kind": "upstream_error", "detail": "backend isError", "latency_ms": latency_ms}
    return {"kind": "ok", "detail": "", "latency_ms": latency_ms}


async def run_load(url: str, args: argparse.Namespace, audit_fn) -> tuple[list, list]:
    """并发打网关。返回 (results, failures)。audit_fn() 返回当前 stream XLEN。"""
    rng = random.Random(42)  # 固定种子：负样本分布可复现
    total = args.requests
    neg_n = round(total * args.negative_ratio)
    kinds = ["neg"] * neg_n + ["ok"] * (total - neg_n)
    rng.shuffle(kinds)

    headers_ok = _headers(args.token, args.tool)
    headers_bad = _headers(f"invalid-{args.token}", args.tool)  # 伪造 token：denied 批次
    sem = asyncio.Semaphore(args.concurrency)
    results: list = []

    async def one(i: int, negative: bool) -> None:
        async with sem:
            req = {"body": _rpc_body(i, args.tool, args.arguments), "negative": negative}
            results.append(await _do_one(client, url, headers_ok, headers_bad, req, args.timeout))

    limit = max(200, args.concurrency + 50)
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=limit, max_keepalive_connections=limit)) as client:
        t0 = time.perf_counter()
        await asyncio.gather(*[one(i, kinds[i] == "neg") for i in range(total)])
        elapsed = time.perf_counter() - t0

    # 审计校验：无消费者运行时 XLEN == 请求总数；gateway-admin 消费者在跑时
    # stream 会被持续排空，XLEN 恒小 —— 该场景用 --no-audit-check 跳过。
    xadd = await audit_fn() if args.audit_check else None

    failures: list = []
    counter: dict[str, int] = {}
    lat_ok: list = []
    for r in results:
        counter[r["kind"]] = counter.get(r["kind"], 0) + 1
        if r["kind"] in ("ok", "denied"):
            lat_ok.append(r["latency_ms"])
        if r["kind"] in ("http_error", "transport_error", "rpc_error", "negative_passed", "negative_unexpected"):
            failures.append(r)
        if r["kind"] == "upstream_error" and args.strict:
            failures.append(r)
    if args.audit_check and xadd != total:
        failures.append({"kind": "audit_mismatch", "detail": f"xadd={xadd} != total={total}", "latency_ms": 0})

    print(f"== {args.mode} 模式 | concurrency={args.concurrency} requests={total} "
          f"negative_ratio={args.negative_ratio} tool={args.tool} ==")
    ok_n = counter.get("ok", 0)
    hard_fail = sum(counter.get(k, 0) for k in
                    ("http_error", "transport_error", "rpc_error", "negative_passed", "negative_unexpected"))
    print(f"total={total} http200={total - counter.get('http_error', 0)} "
          f"ok={ok_n} denied={counter.get('denied', 0)} "
          f"upstream_error={counter.get('upstream_error', 0)} "
          # fail 直接取 failures 长度，与 FAIL 汇总永不漂移（含 audit_mismatch）
          f"fail={len(failures)} rate={ok_n / total * 100:.1f}%")
    if args.audit_check:
        print(f"audit: xadd={xadd}/{total}{'' if xadd == total else '  <-- 审计流不齐'}")
    if lat_ok:
        s = sorted(lat_ok)
        pct = lambda p: s[min(len(s) - 1, int(p * (len(s) - 1)))]
        print(f"latency(ms): p50={pct(0.50):.0f} p95={pct(0.95):.0f} "
              f"p99={pct(0.99):.0f} max={s[-1]:.0f} total_time={elapsed:.1f}s")
    return results, failures


async def run_mock(args: argparse.Namespace) -> int:
    """进程内 mock：FakeRedis gateway + FastMCP echo 后端，零外部依赖。"""
    import fakeredis.aioredis
    import redis_client

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    redis_client._redis = fake

    # observability 在 import 时读 PROMETHEUS_PORT，必须抢在 import server 前
    # 换到空闲端口，避免与本地真实 gateway 的 metrics 端口冲突。
    # --no-telemetry 禁用 span 导出（console exporter 每请求输出大 JSON，
    # 会让 mock echo 的 p50 从几十 ms 飙到 800ms+，压测失去意义）
    os.environ["PROMETHEUS_PORT"] = str(_free_port())
    if args.no_telemetry:
        os.environ["OTEL_SDK_DISABLED"] = "true"

    from fastmcp import FastMCP

    mock = FastMCP("loadtest-backend")

    @mock.tool()
    async def echo(query: str = "loadtest") -> dict:
        """Echo back the query（压测用固定语义后端）。"""
        return {"echo": query}

    backend_port = _free_port()
    gw_port = _free_port()
    import uvicorn

    bsrv = uvicorn.Server(uvicorn.Config(
        mock.http_app(stateless_http=True, json_response=True),
        host="127.0.0.1", port=backend_port, log_level="error", access_log=False,
    ))
    btask = asyncio.create_task(bsrv.serve())
    await _wait_port(backend_port)

    # seed registry + token（与 registry.mount_all / auth.verify_token 读法一致）
    await fake.sadd("servers:active", "loadtest-mcp")
    await fake.hset("servers:loadtest-mcp", mapping={
        "url": f"http://127.0.0.1:{backend_port}/mcp", "status": "active",
    })
    th = hashlib.sha256(args.token.encode()).hexdigest()
    await fake.hset(f"tokens:{th}", mapping={
        "id": "t1", "name": "loadtest",
        "permissions": json.dumps({"loadtest-mcp": {"read": True, "write": True}}),
    })

    # import server 必须在 redis patch + 端口 env 之后（模块级单例）
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import server  # noqa: E402

    app = server.gateway.http_app(stateless_http=True, json_response=True)
    gsrv = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=gw_port, log_level="error", access_log=False,
    ))
    failures = []
    try:
        async with server.gateway._lifespan_manager():
            gtask = asyncio.create_task(gsrv.serve())
            await _wait_port(gw_port)
            try:
                results, failures = await run_load(
                    f"http://127.0.0.1:{gw_port}/mcp", args,
                    audit_fn=lambda: fake.xlen("audit:calls"),
                )
            finally:
                gsrv.should_exit = True
                await asyncio.sleep(0.3)
                gtask.cancel()
                try:
                    await gtask
                except asyncio.CancelledError:
                    pass
    finally:
        bsrv.should_exit = True
        await asyncio.sleep(0.3)
        btask.cancel()
        try:
            await btask
        except asyncio.CancelledError:
            pass
    return _verdict(failures, results)


async def run_url(args: argparse.Namespace) -> int:
    """打真实 gateway 进程。"""
    async def audit_fn():
        import redis.asyncio as redis
        r = redis.from_url(args.redis_url, decode_responses=True, socket_timeout=5)
        try:
            return await r.xlen("audit:calls")
        finally:
            await r.aclose()

    results, failures = await run_load(args.url, args, audit_fn)
    return _verdict(failures, results)


def _verdict(failures: list, results: list) -> int:
    if failures:
        print(f"FAIL: {len(failures)} 项断言失败")
        seen = set()
        for f in failures:
            key = f["kind"]
            if key in seen:
                continue
            seen.add(key)
            print(f"  - {key}: {f.get('detail', '')}")
        return 1
    print(f"PASS: 全部 {len(results)} 请求通过（HTTP 200 / denied 正确拒绝 / 审计流齐）")
    return 0


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_port(port: int, timeout: float = 10.0) -> None:
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError(f"port {port} not ready")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="gateway-proxy 并发压测（MCP 2026-07-28 tools/call）")
    p.add_argument("--mock", action="store_true", help="进程内 mock 模式（FakeRedis + echo 后端，零外部依赖）")
    p.add_argument("--url", default="http://localhost:8082/mcp", help="gateway /mcp 地址")
    p.add_argument("--token", default="loadtest-token", help="API token（denied 批次自动用 invalid-<token> 伪造）")
    p.add_argument("--concurrency", type=int, default=100, help="并发数")
    p.add_argument("--requests", type=int, default=500, help="请求总数")
    p.add_argument("--negative-ratio", type=float, default=0.1, help="invalid-token 请求占比（0-1），验证 denied 路径")
    p.add_argument("--tool", default=None, help="目标工具名（默认 mock: loadtest-mcp_echo / url: tavily-mcp_tavily_search）")
    p.add_argument("--arguments", default=None, help="工具参数 JSON，默认 {\"query\": \"loadtest\"}")
    p.add_argument("--timeout", type=float, default=30.0, help="单请求超时秒数（默认 30：高并发排队会拉长尾延迟，10s 会误伤）")
    p.add_argument("--strict", action="store_true", help="url 模式下 success 批次要求 isError=false（需后端真实可用）")
    p.add_argument("--redis-url", default=None, help="审计 XLEN 校验用 Redis 地址（mock 自动用 FakeRedis；url 模式显式传）")
    p.add_argument("--no-audit-check", action="store_true", help="跳过审计流 XLEN 校验（消费者在跑时会持续排空 stream）")
    p.add_argument("--no-telemetry", action="store_true", help="禁用 OTel span/metrics 导出（mock 模式建议开，避免 console span 拖垮吞吐）")
    args = p.parse_args(argv)

    args.mode = "mock" if args.mock else "url"
    args.tool = args.tool or ("loadtest-mcp_echo" if args.mock else "tavily-mcp_tavily_search")
    args.arguments = json.loads(args.arguments) if args.arguments else {"query": "loadtest"}
    # mock 模式强制 strict 语义（后端确定性可用，isError 即失败）
    if args.mock:
        args.strict = True
    args.audit_check = not args.no_audit_check
    if not args.mock and args.audit_check and args.redis_url is None:
        args.audit_check = False
        print("WARN: --url 模式未传 --redis-url，审计 XLEN 校验跳过（传 --redis-url 或 --no-audit-check）")
    return args


async def amain(args: argparse.Namespace) -> int:
    if args.mock:
        return await run_mock(args)
    return await run_url(args)


def main() -> int:
    return asyncio.run(amain(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
