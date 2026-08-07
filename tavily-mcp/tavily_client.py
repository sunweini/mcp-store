"""Tavily REST API client — 共享 httpx client 薄封装。

Endpoints: POST https://api.tavily.com/{search|extract|crawl|map|research}
Auth: Bearer token. Usage: GET /usage (官方剩余配额).

Task 5（并发加固）改造：调用前每请求新建 AsyncClient（TCP+TLS 握手
每请求一次），现在进程级单例 httpx.AsyncClient（连接池复用，共享
client 禁止默认 Authorization 头——防 key 串用 R5），key 走请求级
headers，timeout 走 per-request 参数。公开方法签名（search(params)
等）与 factory 签名 (key, timeout) 均不变——只改内部实现。

Error classification (per spec 错误语义映射):
- 401/403 → INVALID（key 失效，永久剔除）
- 429 → RATE_LIMIT（Retry-After 头 → cooldown）
- 其余 4xx/5xx/网络错误 → raise（工具层不重试）
"""
import time

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from key_pool import ErrorKind

logger = structlog.get_logger()
tracer = trace.get_tracer("tavily_mcp.tavily_client")

API_BASE = "https://api.tavily.com"
RETRYABLE_IF_IDEMPOTENT = {"search", "extract", "map"}

# 进程级共享 client：连接池复用，禁止默认 Authorization 头（R5）
_shared_client: httpx.AsyncClient | None = None
_SHARED_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=50)


def get_shared_client() -> httpx.AsyncClient:
    """进程级单例 httpx.AsyncClient（连接池复用）。"""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            timeout=30.0,  # 兜底超时；工具层 per-request 覆盖
            limits=_SHARED_LIMITS,
        )
    return _shared_client


def classify_error(exc: Exception, status_code: int | None = None) -> ErrorKind | None:
    """Map HTTP error to pool ErrorKind, or None if not pool-relevant.

    status_code 未显式传入时，从异常对象自省（TavilyError.status_code），
    这样工具层拿到业务异常后直接 classify_error(exc) 即可分类。
    """
    if status_code is None:
        status_code = getattr(exc, "status_code", None)
    if status_code in (401, 403):
        return ErrorKind.INVALID
    if status_code == 429:
        return ErrorKind.RATE_LIMIT
    return None


class TavilyError(Exception):
    """Tavily API business error (non-2xx with body)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"tavily api error {status_code}: {detail}")


class TavilyClient:
    """Thin async client. transport injectable for tests (httpx ASGI/mock)."""

    def __init__(self, key: str, timeout: float = 5.0, transport=None):
        self._key = key
        self._timeout = timeout
        # transport 注入仅测试用：注入时自建 client（关闭权归调用方），
        # 否则用进程级共享 client（连接池复用，禁止默认凭证头——R5）
        self._http = (get_shared_client() if transport is None
                      else httpx.AsyncClient(timeout=timeout, transport=transport))

    async def search(self, params: dict) -> dict:
        return await self._post("search", params)

    async def extract(self, params: dict) -> dict:
        return await self._post("extract", params)

    async def crawl(self, params: dict) -> dict:
        return await self._post("crawl", params)

    async def map(self, params: dict) -> dict:
        return await self._post("map", params)

    async def research(self, params: dict) -> dict:
        return await self._post("research", params)

    async def usage(self) -> dict:
        # usage 返回的 remaining 是更新 key 配额的数据源，其 401/403/429
        # 同样应可被 classify_error 识别（剔除/冷却 key），故错误统一走
        # TavilyError 而非裸 raise_for_status（HTTPStatusError 无顶层
        # status_code，分类路径会漏掉它）
        with tracer.start_as_current_span("tavily_client.usage") as span:
            span.set_attributes({"http.method": "GET", "http.url": f"{API_BASE}/usage"})
            start = time.monotonic()
            resp = await self._http.get(
                f"{API_BASE}/usage",
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=self._timeout,
            )
            duration = time.monotonic() - start
            span.set_attribute("http.status_code", resp.status_code)
            if resp.status_code >= 400:
                span.set_status(Status(StatusCode.ERROR, f"tavily {resp.status_code}"))
                logger.error("tavily_api_error",
                             service="tavily-mcp",
                             endpoint="usage",
                             http_status=resp.status_code,
                             error=resp.text[:200],
                             duration_ms=round(duration * 1000))
                raise TavilyError(resp.status_code, resp.text[:200])
            return resp.json()

    async def _post(self, endpoint: str, params: dict) -> dict:
        with tracer.start_as_current_span(f"tavily_client.{endpoint}") as span:
            span.set_attributes({"http.method": "POST", "http.url": f"{API_BASE}/{endpoint}"})
            start = time.monotonic()
            resp = await self._http.post(
                f"{API_BASE}/{endpoint}", json=params,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=self._timeout,
            )
            duration = time.monotonic() - start
            span.set_attribute("http.status_code", resp.status_code)
            if resp.status_code >= 400:
                span.set_status(Status(StatusCode.ERROR, f"tavily {resp.status_code}"))
                logger.error("tavily_api_error",
                             service="tavily-mcp",
                             endpoint=endpoint,
                             http_status=resp.status_code,
                             error=resp.text[:200],
                             duration_ms=round(duration * 1000))
                raise TavilyError(resp.status_code, resp.text[:200])
            return resp.json()

    async def close(self) -> None:
        # 共享 client 进程级存活，不得关闭——只关测试注入的私有 client
        if self._http is not get_shared_client():
            await self._http.aclose()
