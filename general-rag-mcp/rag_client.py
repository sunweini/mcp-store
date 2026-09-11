"""general-rag 知识库 API client.

封装 general-rag 后端 REST API（OpenAI 风格，非流式，无鉴权）。
- 共享 httpx client 单例（C1）：连接池复用，禁止每调用新建
- per-request timeout：默认 60s（LLM 答案合成可能较慢，指南 §9 要求 ≥60s）
- 503 指数退避：读接口 1s/2s/4s 最多 3 次；ingest 写操作不重试（非幂等）
- 隐私约束：snippet 截断，日志不转储完整文档内容
"""
from typing import Any
import asyncio
import os
import time

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

logger = structlog.get_logger()
tracer = trace.get_tracer("general_rag_mcp.rag_client")

# NOTE: 只读/可重试的接口才有退避重试；ingest 写操作不重试。
_READ_RETRY_DELAYS = (1.0, 2.0, 4.0)

# golden suggest 的瞬态失败重试：LLM 批量调用偶发空内容/502，契约要求
# 间隔 >=5s 重试 <=3 次。间隔/次数做成模块常量，测试 monkeypatch 防真睡。
_GOLDEN_SUGGEST_MAX_ATTEMPTS = 3
_GOLDEN_SUGGEST_RETRY_DELAY = 5.0

# 隐私约束（指南 §3）：snippet 截断长度，避免完整转储文档内容进日志/返回体。
SNIPPET_LIMIT = 500


class RagError(Exception):
    """general-rag API 返回了业务错误（4xx/5xx），status_code 记录 HTTP 码。"""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class RagConnectionError(Exception):
    """网络层失败 — general-rag 服务不可达。"""


def truncate(text: str, limit: int = SNIPPET_LIMIT) -> str:
    """截断文本到 limit 字符，用于 snippet 展示与日志脱敏。

    隐私约束（指南 §3）：不完整转储检索到的文档内容。
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


class RagClient:
    """general-rag REST API 客户端（无鉴权，内网信任环境）。"""

    def __init__(self, base_url: str, timeout: float = 60.0):
        # NOTE: 去掉末尾斜杠避免后续拼接出现双斜杠；base_url 已含 /api/v1。
        self._base_url = base_url.rstrip("/")
        # origin = 去掉 /api/v1 后缀，用于拼媒体等已是全路径的 URL（如 /api/v1/media/...）。
        self._origin = self._base_url.removesuffix("/api/v1")
        self._timeout = timeout
        self._http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        """关闭 httpx 连接池。"""
        if self._http:
            await self._http.aclose()
            self._http = None  # type: ignore[assignment]

    # ── 只读接口 ───────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        namespace: str,
        top_k: int = 5,
        doc_type: str | None = None,
        tags: list[str] | None = None,
        categories: list[str] | None = None,
        use_graph: bool = True,
        include_deprecated: bool = True,
    ) -> dict:
        """POST /search — 检索问答（核心）。namespace 总是显式传。

        include_deprecated=True（后端默认）：已下架文档照常返回、不降权，只在
        source.status 里标记，由调用方判断。传 False 才隐藏——这是**显式选择
        退出**，不是默认（理由见 general-rag 的 search_fulltext docstring）。
        总是下发该字段是刻意的：让"要不要下架内容"成为调用方看得见的显式决定，
        而不是依赖两侧默认值恰好一致。
        """
        payload: dict[str, Any] = {
            "query": query,
            "namespace": namespace,
            "top_k": top_k,
            "use_graph": use_graph,
            "include_deprecated": include_deprecated,
        }
        # NOTE: 仅在有值时下发可选过滤字段，避免空值被后端误解为过滤条件。
        if doc_type:
            payload["doc_type"] = doc_type
        if tags:
            payload["tags"] = tags
        if categories:
            payload["categories"] = categories
        return await self._request("POST", "/search", json=payload, retryable=True)

    async def namespaces(self) -> list[str]:
        """GET /namespaces — 列出可用命名空间。"""
        data = await self._request("GET", "/namespaces", retryable=True)
        return data.get("namespaces", [])

    async def health(self) -> dict:
        """GET /health — 组件状态。"""
        return await self._request("GET", "/health", retryable=True)

    # ── 写接口 ─────────────────────────────────────────────────────

    async def ingest(
        self,
        file_bytes: bytes,
        filename: str,
        namespace: str,
        doc_type: str | None = None,
        title: str | None = None,
        tags: str | None = None,
        categories: str | None = None,
    ) -> dict:
        """POST /ingest — 文件摄入（multipart）。写操作，不重试。"""
        files = {"file": (filename, file_bytes)}
        data: dict[str, str] = {"namespace": namespace}
        if doc_type:
            data["doc_type"] = doc_type
        if title:
            data["title"] = title
        if tags:
            data["tags"] = tags
        if categories:
            data["categories"] = categories
        return await self._request(
            "POST", "/ingest", data=data, files=files, retryable=False
        )

    async def ingest_path(
        self,
        file_path: str,
        namespace: str,
        doc_type: str | None = None,
        title: str | None = None,
        tags: str | None = None,
        categories: str | None = None,
    ) -> dict:
        """POST /ingest-path — 按服务器端绝对路径摄入（不传文件内容）。

        写入操作，不重试。只传一个短路径字符串，大文件内容不会被 LLM
        参数截断——这是 20KB+ 文件的推荐摄入路径。
        """
        payload: dict[str, Any] = {
            "file_path": file_path,
            "namespace": namespace,
        }
        if doc_type:
            payload["doc_type"] = doc_type
        if title:
            payload["title"] = title
        if tags:
            payload["tags"] = tags
        if categories:
            payload["categories"] = categories
        return await self._request(
            "POST", "/ingest-path", json=payload, retryable=False
        )

    # ── golden 集（生长/删除） ─────────────────────────────────────

    async def golden_suggest(self, namespace: str, doc_id: str) -> dict:
        """POST /golden/suggest — 反推口语问法初稿（只读，无副作用）。

        区别于 _request 的 retryable（只处理 503/连接错、不判 body），此方法
        自持重试循环：瞬态 502（LLM 批量空内容）或 200 但 candidates 为空 →
        间隔 >=5s 重试 <=3 次。404 = 文档不在库，红线，不重试。耗尽如实抛错。
        """
        payload = {"namespace": namespace, "doc_id": doc_id}
        for attempt in range(1, _GOLDEN_SUGGEST_MAX_ATTEMPTS + 1):
            try:
                # retryable=True：503(ES 骨干故障)/连接错走 _request 退避重试。
                # transient_statuses={502}：502 由本方法外层自持重试，_request 记
                # WARNING 而非 ERROR，避免瞬态被当噪点。
                body = await self._request(
                    "POST",
                    "/golden/suggest",
                    json=payload,
                    retryable=True,
                    transient_statuses={502},
                )
            except RagError as e:
                if e.status_code == 502 and attempt < _GOLDEN_SUGGEST_MAX_ATTEMPTS:
                    await asyncio.sleep(_GOLDEN_SUGGEST_RETRY_DELAY)
                    continue
                raise

            if body.get("candidates"):
                return body

            # 200 但 candidates 为空：瞬态空内容，重试；最后一次仍空则如实失败，
            # 不能把空结果当成功返回（红线：不编造）。
            if attempt < _GOLDEN_SUGGEST_MAX_ATTEMPTS:
                await asyncio.sleep(_GOLDEN_SUGGEST_RETRY_DELAY)
                continue
            raise RagError("golden suggest 重试耗尽（空 candidates）", 502)

    async def golden_add(
        self,
        namespace: str,
        doc_id: str,
        query: str,
        negatives: list[str] | None = None,
        source: str = "mcp",
    ) -> dict:
        """POST /golden/cases — 写入一条 golden case。写操作，不重试。"""
        payload: dict[str, Any] = {
            "namespace": namespace,
            "doc_id": doc_id,
            "query": query,
            "source": source,
        }
        if negatives:
            payload["negatives"] = negatives
        return await self._request("POST", "/golden/cases", json=payload, retryable=False)

    async def delete_document(
        self,
        namespace: str,
        doc_id: str | None = None,
        filename: str | None = None,
        dry_run: bool = True,
    ) -> dict:
        """POST /delete — 删除文档（真删不可逆）。写操作，不重试。

        doc_id 与 filename 二选一（doc_id 优先），后端校验。dry_run=true 返回
        preview（含 golden_impact），false 才真删。
        """
        payload: dict[str, Any] = {"namespace": namespace, "dry_run": dry_run}
        if doc_id:
            payload["doc_id"] = doc_id
        if filename:
            payload["filename"] = filename
        return await self._request("POST", "/delete", json=payload, retryable=False)

    # ── 媒体（取证路由，白名单图片） ─────────────────────────────

    async def get_media(self, url: str) -> bytes:
        """GET /api/v1/media/<relpath> — 拉取图片字节（取证路由）。

        media URL 已是全路径（含 /api/v1，如 /api/v1/media/<ns>/<stem>_assets/img0.png），
        用 _origin（去 /api/v1 后缀）拼接，避免双写 /api/v1。图片是可选增值：失败
        抛 RagError/RagConnectionError，由上一级降级回 URL，不阻断主检索。
        """
        full = (self._origin + url) if url.startswith("/api/v1") else (self._base_url.rstrip("/") + url)
        try:
            resp = await self._http.get(full, timeout=self._timeout)
        except httpx.HTTPError as e:
            raise RagConnectionError(f"media 拉取连接失败: {e}") from e
        if resp.status_code == 200:
            return resp.content
        logger.warning(
            "rag_api_media_missing",
            service="general-rag-mcp",
            path=url,
            status_code=resp.status_code,
        )
        # 越权/不存在在同一端点按 404 处理（契约：白名单图片，越权 404）。
        raise RagError(f"media 拉取失败 HTTP {resp.status_code}", resp.status_code)

    # ── 底层请求 ───────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        retryable: bool,
        transient_statuses: set[int] | None = None,
    ) -> dict:
        """发起请求；retryable=True 时对 503/连接错误做指数退避重试。

        指南 §7.5：503 是骨干故障，1s/2s/4s 最多 3 次；400 不重试。

        transient_statuses：调用方已在外层自持重试的状态码（如 golden_suggest 的
        502）。命中时记 WARNING、不置 span 错误，但仍抛 RagError 供调用方重试——
        区别于 503 的 `_request` 内部退避。避免"自愈的瞬态"被记成 ERROR 噪点。
        """
        url = f"{self._base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                with tracer.start_as_current_span(
                    f"rag_client.{path.lstrip('/').replace('/', '.')}"
                ) as span:
                    span.set_attributes({
                        "http.method": method,
                        "http.url": url,
                    })
                    start = time.monotonic()
                    resp = await self._http.request(
                        method, url, json=json, data=data, files=files
                    )
                    duration = time.monotonic() - start
                    span.set_attribute("http.status_code", resp.status_code)

                    if resp.status_code == 200:
                        body = resp.json()
                        return body

                    # 503 是骨干故障，读接口做退避重试。
                    if resp.status_code == 503 and retryable:
                        if attempt <= len(_READ_RETRY_DELAYS):
                            delay = _READ_RETRY_DELAYS[attempt - 1]
                            logger.warning(
                                "rag_api_unavailable_retry",
                                service="general-rag-mcp",
                                path=path,
                                attempt=attempt,
                                delay=delay,
                            )
                            await asyncio.sleep(delay)
                            continue

                    # transient_statuses（如 golden_suggest 的 502）：调用方外层自持重试，
                    # 这里记 WARNING、不置 span 错误，仍抛 RagError 供其循环。避免把
                    # "自愈的瞬态"记成 ERROR 噪点（对比 503 的 WARNING 处理）。
                    if transient_statuses and resp.status_code in transient_statuses:
                        logger.warning(
                            "rag_api_transient_retry",
                            service="general-rag-mcp",
                            path=path,
                            status_code=resp.status_code,
                        )
                        raise RagError(
                            f"general-rag 返回 HTTP {resp.status_code}", resp.status_code
                        )

                    # 其余错误：不重试，直接抛业务错误。
                    # NOTE: 日志不 dump 响应体（隐私，可能含文档内容）。
                    span.set_status(Status(StatusCode.ERROR, f"HTTP {resp.status_code}"))
                    logger.error(
                        "rag_api_error",
                        service="general-rag-mcp",
                        path=path,
                        status_code=resp.status_code,
                    )
                    raise RagError(
                        f"general-rag 返回 HTTP {resp.status_code}", resp.status_code
                    )

            except RagError:
                raise

            except httpx.TimeoutException as e:
                logger.error(
                    "rag_api_timeout",
                    service="general-rag-mcp",
                    path=path,
                    error=str(e),
                )
                raise RagConnectionError(f"general-rag 请求超时: {e}") from e

            except httpx.HTTPError as e:
                # 连接错误对读接口也算瞬时故障，退避重试。
                if retryable and attempt <= len(_READ_RETRY_DELAYS):
                    delay = _READ_RETRY_DELAYS[attempt - 1]
                    logger.warning(
                        "rag_api_connection_retry",
                        service="general-rag-mcp",
                        path=path,
                        attempt=attempt,
                        delay=delay,
                        error=str(e),
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(
                    "rag_api_connection_error",
                    service="general-rag-mcp",
                    path=path,
                    error=str(e),
                )
                raise RagConnectionError(str(e)) from e
