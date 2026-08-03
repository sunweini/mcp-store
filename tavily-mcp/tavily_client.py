"""Tavily REST API client.

Endpoints: POST https://api.tavily.com/{search|extract|crawl|map|research}
Auth: Bearer token. Usage: GET /usage (官方剩余配额).

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
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {key}"},
            transport=transport,
        )

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
        resp = await self._http.get(f"{API_BASE}/usage")
        resp.raise_for_status()
        return resp.json()

    async def _post(self, endpoint: str, params: dict) -> dict:
        with tracer.start_as_current_span(f"tavily_client.{endpoint}") as span:
            span.set_attributes({"http.method": "POST", "http.url": f"{API_BASE}/{endpoint}"})
            start = time.monotonic()
            resp = await self._http.post(f"{API_BASE}/{endpoint}", json=params)
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
        await self._http.aclose()
