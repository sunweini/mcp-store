"""Brave Search REST API client.

Endpoints:
- GET https://api.search.brave.com/res/v1/web/search
- GET https://api.search.brave.com/res/v1/local/search
Auth: X-Subscription-Token header.
Error mapping: 401 → INVALID, 429 → RATE_LIMIT (spec 错误语义映射).
Brave 无用量/欠费 body 语义,其余 4xx/5xx/网络错误一律 raise(工具层记
EXHAUSTED)——比 tavily(需解析 body 判欠费)简单。
"""
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
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"X-Subscription-Token": key},
            transport=transport,
            # 生产网络 api.search.brave.com 直连不通（IPv4 被墙/IPv6 不通），
            # 必须经内网代理 http://10.16.12.12:7890；None 时 httpx 不启用
            # 代理（httpx >= 0.27 支持 proxy 与 MockTransport 共存，测试
            # 注入 transport 不受影响）
            proxy=proxy,
        )

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
            resp = await self._http.get(f"{API_BASE}/{path}", params=params)
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
        await self._http.aclose()
