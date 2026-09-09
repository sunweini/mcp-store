"""general-rag 知识库检索工具。

三个读工具（search / namespaces / health）+ 一个写工具（ingest）。

设计说明：函数定义在模块级（非 register() 内闭包），测试可直接 import
并注入 mock client；register() 只做薄包装注入真实 client。

隐私约束（指南 §3）：sources 的 snippet 截断（rag_client.truncate），
日志不转储完整文档内容。
"""
import base64

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ImageContent, ToolAnnotations
import structlog

from rag_client import RagClient, RagError, RagConnectionError, truncate

logger = structlog.get_logger()

# NOTE: 指南 §8 的 doc_type 枚举六值。逐字段照搬，后端做最终校验。
_DOC_TYPES = ("guide", "manual", "sop", "reference", "note", "faq")

# 内嵌图边界：避免一次吐几十张高清图撑爆会话。单张超限/拉取失败 → 降级回 URL
# （URL 留在 sources[].images，作文本降级）；最多返回前 N 张，多的只保留 URL。
MAX_MEDIA_COUNT = 3
MAX_MEDIA_BYTES = 2 * 1024 * 1024
_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
    "tiff": "image/tiff",
}


def _mime_from_url(url: str) -> str:
    """按 URL 扩展名推断 image mimeType；未知回退 image/png。"""
    ext = url.rsplit(".", 1)[-1].lower() if "." in url else ""
    return _MIME_BY_EXT.get(ext, "image/png")


async def _fetch_image_content(client: RagClient, url: str) -> ImageContent | None:
    """拉一张图并转成 MCP image content 块；拉取失败或超限返回 None（降级回 URL）。

    图片是可选增值：绝不因拉图失败阻断主检索，也不报错。
    """
    if client is None:
        return None
    try:
        data = await client.get_media(url)
    except (RagError, RagConnectionError):
        return None
    if not data or len(data) > MAX_MEDIA_BYTES:
        return None
    return ImageContent(
        type="image",
        data=base64.b64encode(data).decode("ascii"),
        mime_type=_mime_from_url(url),
    )


def _search_error(message: str) -> ToolResult:
    """把 search 的错误态统一包成 ToolResult（content=文本错误，structured=error dict）。"""
    return ToolResult(
        content=[TextContent(type="text", text=message)],
        structured_content={"status": "error", "message": message},
    )

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
) -> ToolResult:
    """检索内部知识库（章管家接口文档等）并返回带来源标记的答案。

    必须指定 namespace（缺省 stamp-project）。返回 answer + sources[] +
    degraded + gap_warning。当 degraded=true 或 gap_warning 非空时如实
    告知用户，不要臆测补充。

    sources[].images 里命中的图会拉字节转成 MCP image content 块随回答返回
    （客户端原生渲染）；单张 >2MB 或拉取失败降级回 URL 文本，最多返回 3 张。
    """
    if client is None:
        return _search_error("rag client not initialized")
    if not query or not query.strip():
        return _search_error("query 不能为空")

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
        return _search_error(f"{e}{hint}")
    except RagConnectionError as e:
        return _search_error(f"知识库服务暂不可用：{e}，请稍后重试")

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
            # images：该 chunk 关联的图片 URL 列表（§4.3），空数组=无图，直接透传。
            "images": s.get("images") or [],
            "snippet": truncate(s.get("snippet")),
        })

    ok_result = {
        "status": "ok",
        "answer": result.get("answer"),
        "sources": sources,
        "query": result.get("query"),
        "namespace": result.get("namespace"),
        "degraded": result.get("degraded", False),
        "missing_components": result.get("missing_components", []),
        "gap_warning": result.get("gap_warning"),
    }

    # 内嵌可渲染图（客户端 image content 块）：按命中顺序，最多 MAX_MEDIA_COUNT 张，
    # 单张 ≤MAX_MEDIA_BYTES；拉取失败/超限降级回 URL（URL 已留在 sources[].images）。
    image_blocks = []
    for s in sources:
        if len(image_blocks) >= MAX_MEDIA_COUNT:
            break
        for img_url in s.get("images") or []:
            if len(image_blocks) >= MAX_MEDIA_COUNT:
                break
            block = await _fetch_image_content(client, img_url)
            if block is not None:
                image_blocks.append(block)

    text = result.get("answer") or "（无答案）"
    return ToolResult(
        content=[TextContent(type="text", text=text), *image_blocks],
        structured_content=ok_result,
    )


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


async def knowledge_base_ingest_file(
    file_path: str,
    namespace: str,
    doc_type: str | None = None,
    title: str | None = None,
    tags: str | None = None,
    categories: str | None = None,
    *,
    client: RagClient | None = None,
) -> dict:
    """按服务器端绝对路径摄入文件（推荐用于 20KB+ 大文件）。

    🔧 大文件首选：file_bytes 参数要 LLM 在工具调用里生成完整内容，
    20-44KB 内容会被模型输出限制截断（发生过多起：只入库文件开头
    几百字符）。改用 file_path 后，工具参数只是一个短路径字符串，
    内容在 MCP server 所在机器上由后端直接读取，永不截断。

    ⚠️ 写操作 — 执行前必须向用户确认 namespace 与文件路径后再调用。

    调用前提：文件已存在于 MCP server（后端 API）同一主机可读位置
    （如 rsync 到 /opt/general-rag/ingest/<文件> 或 data 目录）。
    namespace 不存在时默认会自动创建。
    """
    if client is None:
        return {"status": "error", "message": "rag client not initialized"}
    if not file_path or not file_path.strip():
        return {"status": "error", "message": "file_path 不能为空"}

    try:
        result = await client.ingest_path(
            file_path=file_path,
            namespace=namespace,
            doc_type=doc_type,
            title=title,
            tags=tags,
            categories=categories,
        )
    except RagError as e:
        return {"status": "error", "message": f"{e}（可能是路径不存在/格式不支持/扫描件）"}
    except RagConnectionError as e:
        return {"status": "error", "message": f"知识库服务暂不可用：{e}"}

    return {"status": "ok", **result}


async def knowledge_base_golden_suggest(
    namespace: str,
    doc_id: str,
    *,
    client: RagClient | None = None,
) -> dict:
    """反推 golden 问法初稿（只读，无副作用）——**仅用于摄入新文档后**。

    返回 1-2 条口语问法 + 负样本候选，**仅供展示，不得直接写入**——两步确认的
    初稿须经用户确认后才能走 knowledge_base_golden_add 落库。

    LLM 偶发空内容时后端会 502，调用方应重试；本工具已做 >=5s 间隔 <=3 次重试，
    仍失败则如实报错（不编造）。对"标题复读"式明显假问法（与文档标题几乎一样）
    应提醒用户或建议弃用；文档不在库（404）时不产初稿（红线：不能硬凑）。
    """
    if client is None:
        return {"status": "error", "message": "rag client not initialized"}
    if not namespace or not namespace.strip():
        return {"status": "error", "message": "namespace 不能为空"}
    if not doc_id or not doc_id.strip():
        return {"status": "error", "message": "doc_id 不能为空"}

    try:
        result = await client.golden_suggest(namespace=namespace, doc_id=doc_id)
    except RagError as e:
        if e.status_code == 404:
            return {
                "status": "error",
                "message": "文档不在库，不能反推问法（红线：不为不在库的文档硬凑问法）",
            }
        if e.status_code == 502:
            return {
                "status": "error",
                "message": "golden 问法反推暂不可用（LLM 多次重试仍失败），未编造；请稍后重试",
            }
        return {"status": "error", "message": str(e)}
    except RagConnectionError as e:
        return {"status": "error", "message": f"知识库服务暂不可用：{e}"}

    return {"status": "ok", **result}


async def knowledge_base_golden_add(
    namespace: str,
    doc_id: str,
    query: str,
    negatives: list[str] | None = None,
    *,
    client: RagClient | None = None,
) -> dict:
    """写入一条 golden case（**必走两步确认**）——⚠️ 写操作。

    强制三步流（调用方须遵守）：
    1. 先经 knowledge_base_golden_suggest（或调用方自拟初稿）；
    2. **向用户展示初稿（问法 + 期望命中 + 负样本候选），获明确确认**——用户可改可删；
    3. 确认后才调用本工具写入。

    红线：禁止未经确认写入；禁止"标题复读"式假问法；问法的答案必须在该文档里
    （超纲不写）。期望命中或负样本 doc_id 不在库会被后端拒收（幽灵引用）。
    """
    if client is None:
        return {"status": "error", "message": "rag client not initialized"}
    if not namespace or not namespace.strip():
        return {"status": "error", "message": "namespace 不能为空"}
    if not doc_id or not doc_id.strip():
        return {"status": "error", "message": "doc_id 不能为空"}
    if not query or not query.strip():
        return {"status": "error", "message": "query 不能为空"}

    # 防御性净化：negatives 非 list 置空，去空串负样本。幽灵引用语义校验交后端。
    clean_negatives = []
    if isinstance(negatives, list):
        clean_negatives = [n for n in negatives if isinstance(n, str) and n.strip()]

    try:
        result = await client.golden_add(
            namespace=namespace,
            doc_id=doc_id,
            query=query,
            negatives=clean_negatives,
        )
    except RagError as e:
        return {
            "status": "error",
            "message": f"{e}（可能是幽灵引用：期望命中或负样本 doc_id 不在库）",
        }
    except RagConnectionError as e:
        return {"status": "error", "message": f"知识库服务暂不可用：{e}"}

    return {"status": "ok", **result}


async def knowledge_base_delete_document(
    namespace: str,
    doc_id: str | None = None,
    filename: str | None = None,
    dry_run: bool = True,
    *,
    client: RagClient | None = None,
) -> dict:
    """删除文档（**必走三步流**，真删不可逆）——⚠️ 写操作。

    强制三步流（调用方须遵守）：
    1. 先以 dry_run=true 预览，把将删清单**连同 golden_impact（牵动 N 条 golden
       case）**展示给用户；
    2. 用户明确确认后才 dry_run=false 真删；
    3. 若 golden_impact.cases > 0：删除完成后**提醒用户**牵动的 case 已悬空，建议
       后续经人工确认后用 golden case 删除端点清理（本工具不得自动删 case）。

    禁止跳过预览直接真删。dry_run=true 的响应保留后端 status:"preview"（勿覆盖为 ok）。
    参数 doc_id 与 filename 二选一（doc_id 优先）。
    """
    if client is None:
        return {"status": "error", "message": "rag client not initialized"}
    if not namespace or not namespace.strip():
        return {"status": "error", "message": "namespace 不能为空"}
    if not doc_id and not filename:
        return {"status": "error", "message": "必须提供 doc_id 或 filename 之一"}

    try:
        result = await client.delete_document(
            namespace=namespace, doc_id=doc_id, filename=filename, dry_run=dry_run
        )
    except RagError as e:
        return {"status": "error", "message": str(e)}
    except RagConnectionError as e:
        return {"status": "error", "message": f"知识库服务暂不可用：{e}"}

    # 不盲包 status:"ok"：dry_run=true 时后端返回 status:"preview"，原样透传。
    return result


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

    async def _mcp_ingest_file(
        file_path: str,
        namespace: str,
        doc_type: str | None = None,
        title: str | None = None,
        tags: str | None = None,
        categories: str | None = None,
    ) -> dict:
        return await knowledge_base_ingest_file(
            file_path=file_path,
            namespace=namespace,
            doc_type=doc_type,
            title=title,
            tags=tags,
            categories=categories,
            client=get_client(),
        )

    _mcp_ingest_file.__doc__ = knowledge_base_ingest_file.__doc__
    mcp.tool(
        name="knowledge_base_ingest_file",
        description=knowledge_base_ingest_file.__doc__,
        annotations=ToolAnnotations(destructiveHint=True),
    )(_wrap("knowledge_base_ingest_file")(_mcp_ingest_file))

    async def _mcp_golden_suggest(namespace: str, doc_id: str) -> dict:
        return await knowledge_base_golden_suggest(
            namespace=namespace, doc_id=doc_id, client=get_client()
        )

    _mcp_golden_suggest.__doc__ = knowledge_base_golden_suggest.__doc__
    mcp.tool(
        name="knowledge_base_golden_suggest",
        description=knowledge_base_golden_suggest.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("knowledge_base_golden_suggest")(_mcp_golden_suggest))

    async def _mcp_golden_add(
        namespace: str,
        doc_id: str,
        query: str,
        negatives: list[str] | None = None,
    ) -> dict:
        return await knowledge_base_golden_add(
            namespace=namespace,
            doc_id=doc_id,
            query=query,
            negatives=negatives,
            client=get_client(),
        )

    _mcp_golden_add.__doc__ = knowledge_base_golden_add.__doc__
    mcp.tool(
        name="knowledge_base_golden_add",
        description=knowledge_base_golden_add.__doc__,
        annotations=ToolAnnotations(destructiveHint=True),
    )(_wrap("knowledge_base_golden_add")(_mcp_golden_add))

    async def _mcp_delete_document(
        namespace: str,
        doc_id: str | None = None,
        filename: str | None = None,
        dry_run: bool = True,
    ) -> dict:
        return await knowledge_base_delete_document(
            namespace=namespace,
            doc_id=doc_id,
            filename=filename,
            dry_run=dry_run,
            client=get_client(),
        )

    _mcp_delete_document.__doc__ = knowledge_base_delete_document.__doc__
    mcp.tool(
        name="knowledge_base_delete_document",
        description=knowledge_base_delete_document.__doc__,
        annotations=ToolAnnotations(destructiveHint=True),
    )(_wrap("knowledge_base_delete_document")(_mcp_delete_document))
