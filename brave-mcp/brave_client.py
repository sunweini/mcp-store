"""Brave Search REST API client — 共享 httpx client 薄封装。

Endpoints:
- GET https://api.search.brave.com/res/v1/web/search
- GET https://api.search.brave.com/res/v1/local/search
Auth: X-Subscription-Token header.
Error mapping: 401 → INVALID, 429 → RATE_LIMIT (spec 错误语义映射).
Brave 无用量/欠费 body 语义,其余 4xx/5xx/网络错误一律 raise(工具层记
EXHAUSTED)——比 tavily(需解析 body 判欠费)简单。

Task 5（并发加固）改造：进程级单例 httpx.AsyncClient（连接池复用），
key 走请求级 headers（共享 client 禁止默认 X-Subscription-Token 头——
防 key 串用 R5），timeout 走 per-request 参数。代理：生产必须走
SEARCH_PROXY（api.search.brave.com 直连不通），共享 client 创建时从
env 读——proxy 属于共享 client，不再经 BraveClient 逐请求传递。
公开方法签名与 factory 签名不变。
"""
import os
import time

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from key_pool import ErrorKind

logger = structlog.get_logger()
tracer = trace.get_tracer("brave_mcp.brave_client")

API_BASE = "https://api.search.brave.com/res/v1"
TIMEOUT = 5.0

# 进程级共享 client：连接池复用，禁止默认凭证头（R5）。代理从 env 读
# ——调用时读（测试可 monkeypatch）；单例首次创建后固定，env 后续变化
# 不生效（生产 env 启动即定）
_shared_client: httpx.AsyncClient | None = None
_SHARED_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=50)


def get_shared_client() -> httpx.AsyncClient:
    """进程级单例 httpx.AsyncClient（连接池复用，含 SEARCH_PROXY 代理）。"""
    global _shared_client
    if _shared_client is None:
        # 生产网络 api.search.brave.com 直连不通（IPv4 被墙/IPv6 不通），
        # 必须经内网代理；空串/未设置 = 直连（httpx 不接受空串 proxy）
        proxy = os.environ.get("SEARCH_PROXY", "") or None
        _shared_client = httpx.AsyncClient(
            timeout=30.0,  # 兜底超时；工具层 per-request 覆盖
            limits=_SHARED_LIMITS,
            proxy=proxy,
        )
    return _shared_client


def classify_error(exc: Exception, status_code: int | None = None) -> ErrorKind | None:
    """Map HTTP error to pool ErrorKind, or None if not pool-relevant.

    status_code 未显式传入时,从异常对象自省(BraveError.status_code),
    这样工具层拿到业务异常后直接 classify_error(exc) 即可分类。
    """
    if status_code is None:
        status_code = getattr(exc, "status_code", None)
    if status_code == 401:
        return ErrorKind.INVALID
    if status_code == 429:
        return ErrorKind.RATE_LIMIT
    # Brave 对无效 token 实测返回 422（body detail 含 "The provided
    # subscription token is invalid."，2026-08-03 冒烟实测）。但 422 同时
    # 有参数错误语义（如非法 offset/count），只能按 body 文本精确匹配，
    # 裸码匹配会把参数类 422 误剔有效 key（评审 I-1 裁决）
    if status_code == 422:
        detail = getattr(exc, "detail", "") or ""
        if "subscription token is invalid" in detail.lower():
            return ErrorKind.INVALID
    return None


class BraveError(Exception):
    """Brave API business error (non-2xx with body)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        # detail 单独存属性：classify_error 需按 body 文本区分 422 语义
        # （invalid token vs 参数错误），仅靠 __str__ 拼接不可靠
        self.detail = detail
        super().__init__(f"brave api error {status_code}: {detail}")


class BraveClient:
    """Thin async client. transport injectable for tests (httpx mock)."""

    def __init__(self, key: str, timeout: float = TIMEOUT, transport=None,
                 proxy: str | None = None):
        self._key = key
        self._timeout = timeout
        # transport 注入仅测试用：注入时自建 client（关闭权归调用方），
        # 否则用进程级共享 client（连接池复用，禁止默认凭证头——R5）。
        # proxy 参数保留以兼容旧签名，但共享 client 形态下代理已在
        # get_shared_client() 里从 env 读定，逐请求传 proxy 不再适用
        self._http = (get_shared_client() if transport is None
                      else httpx.AsyncClient(timeout=timeout,
                                             transport=transport,
                                             proxy=proxy))

    async def web_search(self, params: dict) -> dict:
        return await self._get("web/search", params)

    async def local_search(self, params: dict) -> dict:
        return await self._get("local/search", params)

    async def _get(self, path: str, params: dict) -> dict:
        # Brave 的查询参数走 URL query string(GET 端点),与 tavily 的
        # POST json body 不同;错误统一走 BraveError 而非裸 raise_for_status
        # (HTTPStatusError 无顶层 status_code,分类路径会漏掉它)
        with tracer.start_as_current_span(f"brave_client.{path.replace('/', '.')}") as span:
            span.set_attributes({"http.method": "GET", "http.url": f"{API_BASE}/{path}"})
            start = time.monotonic()
            resp = await self._http.get(
                f"{API_BASE}/{path}", params=params,
                headers={"X-Subscription-Token": self._key},
                timeout=self._timeout,
            )
            duration = time.monotonic() - start
            span.set_attribute("http.status_code", resp.status_code)
            if resp.status_code >= 400:
                span.set_status(Status(StatusCode.ERROR, f"brave {resp.status_code}"))
                logger.error("brave_api_error",
                             service="brave-mcp", path=path,
                             http_status=resp.status_code, error=resp.text[:200],
                             duration_ms=round(duration * 1000))
                raise BraveError(resp.status_code, resp.text[:200])
            return resp.json()

    async def close(self) -> None:
        # 共享 client 进程级存活，不得关闭——只关测试注入的私有 client
        if self._http is not get_shared_client():
            await self._http.aclose()
