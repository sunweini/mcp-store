"""general-rag 知识库检索工具。

三个读工具（search / namespaces / health）+ 一个写工具（ingest）。

设计说明：函数定义在模块级（非 register() 内闭包），测试可直接 import
并注入 mock client；register() 只做薄包装注入真实 client。

隐私约束（指南 §3）：sources 的 snippet 截断（rag_client.truncate），
日志不转储完整文档内容。
"""
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
import structlog

from rag_client import RagClient, RagError, RagConnectionError, truncate

logger = structlog.get_logger()

# NOTE: 指南 §8 的 doc_type 枚举六值。逐字段照搬，后端做最终校验。
_DOC_TYPES = ("guide", "manual", "sop", "reference", "note", "faq")

# 缺省 namespace：当前生产唯一命名空间。指南 §7.1 要求"总是显式传
# namespace"，缺失时落到 stamp-project 而非可能被禁的 default。
DEFAULT_NAMESPACE = "stamp-project"


# ── 模块级工具实现 ──────────────────────────────────────────────────
# client 为 keyword-only 参数，测试注入 mock；register() 注入真实 client。


async def knowledge_base_search(
    query: str,
    namespace: str = DEFAULT_NAMESPACE,
    top_k: int = 5,
    doc_type: str | None = None,
    tags: list[str] | None = None,
    categories: list[str] | None = None,
    use_graph: bool = True,
    *,
    client: RagClient | None = None,
) -> dict:
    """检索内部知识库（章管家接口文档等）并返回带来源标记的答案。

    必须指定 namespace（缺省 stamp-project）。返回 answer + sources[] +
    degraded + gap_warning。当 degraded=true 或 gap_warning 非空时如实
    告知用户，不要臆测补充。
    """
    if client is None:
        return {"status": "error", "message": "rag client not initialized"}
    if not query or not query.strip():
        return {"status": "error", "message": "query 不能为空"}

    try:
        result = await client.search(
            query=query,
            namespace=namespace,
            top_k=top_k,
            doc_type=doc_type,
            tags=tags,
            categories=categories,
            use_graph=use_graph,
        )
    except RagError as e:
        # 400 多为 namespace 缺失/为 default：提示补全/纠正 namespace。
        hint = ""
        if e.status_code == 400:
            hint = "（可能是 namespace 非法或被禁用，请先用 knowledge_base_namespaces 确认）"
        return {"status": "error", "message": f"{e}{hint}"}
    except RagConnectionError as e:
        return {
            "status": "error",
            "message": f"知识库服务暂不可用：{e}，请稍后重试",
        }

    # 截断 snippet（隐私），保留结构化来源字段供 agent 引用。
    sources = []
    for s in result.get("sources", []):
        sources.append({
            "title": s.get("title"),
            "doc_id": s.get("doc_id"),
            "score": s.get("score"),
            "engine": s.get("engine"),
            "confidence": s.get("confidence"),
            "confidence_basis": s.get("confidence_basis"),
            "source_path": s.get("source_path"),
            "doc_type": s.get("doc_type"),
            "snippet": truncate(s.get("snippet")),
        })

    return {
        "status": "ok",
        "answer": result.get("answer"),
        "sources": sources,
        "query": result.get("query"),
        "namespace": result.get("namespace"),
        "degraded": result.get("degraded", False),
        "missing_components": result.get("missing_components", []),
        "gap_warning": result.get("gap_warning"),
    }


async def knowledge_base_namespaces(*, client: RagClient | None = None) -> dict:
    """列出知识库全部可用命名空间，供 knowledge_base_search 校验/提示。"""
    if client is None:
        return {"status": "error", "message": "rag client not initialized"}

    try:
        namespaces = await client.namespaces()
    except RagError as e:
        return {"status": "error", "message": str(e)}
    except RagConnectionError as e:
        return {"status": "error", "message": f"知识库服务暂不可用：{e}"}

    return {"status": "ok", "namespaces": namespaces}


async def knowledge_base_health(*, client: RagClient | None = None) -> dict:
    """探测知识库组件状态（es/neo4j/llm/embedding/rerank 是否可用）。"""
    if client is None:
        return {"status": "error", "message": "rag client not initialized"}

    try:
        health = await client.health()
    except RagError as e:
        return {"status": "error", "message": str(e)}
    except RagConnectionError as e:
        return {"status": "error", "message": f"知识库服务暂不可用：{e}"}

    return {"status": "ok", "health": health}


async def knowledge_base_ingest(
    file_bytes: bytes,
    filename: str,
    namespace: str,
    doc_type: str | None = None,
    title: str | None = None,
    tags: str | None = None,
    categories: str | None = None,
    *,
    client: RagClient | None = None,
) -> dict:
    """上传文件摄入知识库（docx/pdf/pptx/txt/md，≤20MB）。
    ⚠️ 写操作 — 执行前必须向用户确认 namespace 与文件名后再调用。

    namespace 不存在时默认会自动创建。扫描件 PDF（无文字层）会被后端拒绝。
    """
    if client is None:
        return {"status": "error", "message": "rag client not initialized"}
    if not file_bytes:
        return {"status": "error", "message": "file_bytes 不能为空"}

    # 20MB 上限（指南 §5.0）。
    if len(file_bytes) > 20 * 1024 * 1024:
        return {"status": "error", "message": "文件超过 20MB 上限"}

    try:
        result = await client.ingest(
            file_bytes=file_bytes,
            filename=filename,
            namespace=namespace,
            doc_type=doc_type,
            title=title,
            tags=tags,
            categories=categories,
        )
    except RagError as e:
        return {"status": "error", "message": f"{e}（可能是格式不支持/扫描件/缺 namespace）"}
    except RagConnectionError as e:
        return {"status": "error", "message": f"知识库服务暂不可用：{e}"}

    return {"status": "ok", **result}


# ── MCP registration ───────────────────────────────────────────────────────────


def register(mcp: FastMCP, get_client, metrics=None) -> None:
    """Register general-rag tools on the FastMCP server.

    get_client: callable returning the RagClient from app state (module-level
    懒加载单例)。metrics: optional _metrics_wrapper factory。
    """
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_search(
        query: str,
        namespace: str = DEFAULT_NAMESPACE,
        top_k: int = 5,
        doc_type: str | None = None,
        tags: list[str] | None = None,
        categories: list[str] | None = None,
        use_graph: bool = True,
    ) -> dict:
        return await knowledge_base_search(
            query=query,
            namespace=namespace,
            top_k=top_k,
            doc_type=doc_type,
            tags=tags,
            categories=categories,
            use_graph=use_graph,
            client=get_client(),
        )

    _mcp_search.__doc__ = knowledge_base_search.__doc__
    mcp.tool(
        name="knowledge_base_search",
        description=knowledge_base_search.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("knowledge_base_search")(_mcp_search))

    async def _mcp_namespaces() -> dict:
        return await knowledge_base_namespaces(client=get_client())

    _mcp_namespaces.__doc__ = knowledge_base_namespaces.__doc__
    mcp.tool(
        name="knowledge_base_namespaces",
        description=knowledge_base_namespaces.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("knowledge_base_namespaces")(_mcp_namespaces))

    async def _mcp_health() -> dict:
        return await knowledge_base_health(client=get_client())

    _mcp_health.__doc__ = knowledge_base_health.__doc__
    mcp.tool(
        name="knowledge_base_health",
        description=knowledge_base_health.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("knowledge_base_health")(_mcp_health))

    async def _mcp_ingest(
        file_bytes: bytes,
        filename: str,
        namespace: str,
        doc_type: str | None = None,
        title: str | None = None,
        tags: str | None = None,
        categories: str | None = None,
    ) -> dict:
        return await knowledge_base_ingest(
            file_bytes=file_bytes,
            filename=filename,
            namespace=namespace,
            doc_type=doc_type,
            title=title,
            tags=tags,
            categories=categories,
            client=get_client(),
        )

    _mcp_ingest.__doc__ = knowledge_base_ingest.__doc__
    mcp.tool(
        name="knowledge_base_ingest",
        description=knowledge_base_ingest.__doc__,
        annotations=ToolAnnotations(destructiveHint=True),
    )(_wrap("knowledge_base_ingest")(_mcp_ingest))
