"""SerpAPI REST client — 共享 httpx client 薄封装。

GET https://serpapi.com/search.json?engine=<engine>&...&api_key=<key>
Error mapping (spec): 401 → INVALID; 429 → RATE_LIMIT; 200 但响应体含
"account has exceeded quota" 类文本 → EXHAUSTED（欠费）。

Task 5（并发加固）改造：进程级单例 httpx.AsyncClient（连接池复用）。
serpapi 的 api_key 是 URL query 参数（非 header）——共享 client 天然
无凭证头（R5 key 串用防护），key 仍走请求级 query，timeout 走
per-request 参数。公开方法签名与 factory 签名不变。

OBS: api_key 是 URL query 参数——任何日志不得记录带 query 的完整 URL
（明文 key 会随 URL 落入日志）；httpx 异常日志只记 status 与截断 body。
"""
import json
import time

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from key_pool import ErrorKind

logger = structlog.get_logger()
tracer = trace.get_tracer("serpapi_mcp.serpapi_client")

API_BASE = "https://serpapi.com"
EXHAUSTED_KEYWORDS = ("account has exceeded quota", "quota exceeded", "insufficient credits")

# 进程级共享 client：连接池复用。api_key 走 query 不走 header，
# 共享 client 无默认凭证（R5）——query 路径的泄漏防线见模块 docstring
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


def classify_error(exc: Exception, status_code: int | None = None,
                   body_text: str = "") -> ErrorKind | None:
    """Map HTTP error to pool ErrorKind, or None if not pool-relevant.

    三参数版（tavily/brave 是两参数）：serpapi 欠费返回 200 + error body
    （非 4xx/5xx），只能靠 body 文本关键词判 EXHAUSTED——工具层必须把
    resp.text 传进来。顺序：状态码优先（401/429），body 关键词兜底。
    """
    if status_code == 401:
        return ErrorKind.INVALID
    if status_code == 429:
        return ErrorKind.RATE_LIMIT
    if body_text and any(kw in body_text.lower() for kw in EXHAUSTED_KEYWORDS):
        return ErrorKind.EXHAUSTED
    return None


class SerpapiError(Exception):
    """SerpAPI API business error (non-2xx or 200-with-error-body)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"serpapi error {status_code}: {detail}")


class SerpapiClient:
    """Thin async client. transport injectable for tests (httpx mock)."""

    def __init__(self, key: str, timeout: float = 5.0, transport=None):
        self._key = key
        self._timeout = timeout
        # transport 注入仅测试用：注入时自建 client（关闭权归调用方），
        # 否则用进程级共享 client（连接池复用；api_key 走 query，共享
        # client 无凭证头——R5）
        self._http = (get_shared_client() if transport is None
                      else httpx.AsyncClient(timeout=timeout, transport=transport))

    async def search(self, engine: str, params: dict) -> dict:
        # dict(params) 拷贝：engine/api_key 追加到副本上，不污染调用方
        # 的 dict（工具层复用同一 params 重试时参数不得错乱）
        params = dict(params)
        params["engine"] = engine
        params["api_key"] = self._key
        with tracer.start_as_current_span(f"serpapi_client.{engine}") as span:
            # span 的 http.url 只记 path（API_BASE 本身无 query）——
            # httpx 拼入 query 后的完整 URL 不进 span/log（OBS：key 禁入日志）
            span.set_attributes({"http.method": "GET", "http.url": f"{API_BASE}/search.json"})
            start = time.monotonic()
            try:
                resp = await self._http.get(f"{API_BASE}/search.json", params=params,
                                            timeout=self._timeout)
            except httpx.HTTPError as exc:
                # 网络层异常（连接失败/超时/ProxyError 等）：httpx 的异常
                # repr 含完整 URL（带 api_key query），直接 str(exc) 会泄
                # 明文 key 到日志——只记异常类型名，不记 message
                span.set_status(Status(StatusCode.ERROR, "serpapi transport error"))
                logger.error("serpapi_transport_error",
                             service="serpapi-mcp", engine=engine,
                             error=type(exc).__name__,
                             duration_ms=round((time.monotonic() - start) * 1000))
                raise
            duration = time.monotonic() - start
            span.set_attribute("http.status_code", resp.status_code)
            try:
                body = resp.json()
            except json.JSONDecodeError:
                body = {}
            if resp.status_code >= 400:
                span.set_status(Status(StatusCode.ERROR, f"serpapi {resp.status_code}"))
                # 只记 status + 截断 body（body 不含 query，安全）；
                # resp.text 是响应体而非请求 URL
                logger.error("serpapi_api_error",
                             service="serpapi-mcp", engine=engine,
                             http_status=resp.status_code, error=resp.text[:200],
                             duration_ms=round(duration * 1000))
                raise SerpapiError(resp.status_code, resp.text[:200])
            # 200 但业务错误（配额耗尽） — classify_error 在工具层用 body 判 EXHAUSTED
            if "error" in body:
                span.set_status(Status(StatusCode.ERROR, "serpapi business error"))
                raise SerpapiError(resp.status_code, json.dumps(body)[:200])
            return body

    async def close(self) -> None:
        # 共享 client 进程级存活，不得关闭——只关测试注入的私有 client
        if self._http is not get_shared_client():
            await self._http.aclose()
