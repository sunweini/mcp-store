"""Knowledge base tool tests.

Covers search 正常解析 / degraded+gap_warning 透传 / 空 query 报错 /
namespace 缺省、namespaces/health 正常路径、ingest 参数校验、以及
rag_client 的 503 退避与 400 不重试。
"""
import base64

import pytest
from structlog.testing import capture_logs

import rag_client as rag_client_module
from rag_client import RagError
from tools.knowledge_base import (
    knowledge_base_search,
    knowledge_base_namespaces,
    knowledge_base_health,
    knowledge_base_ingest,
    knowledge_base_ingest_file,
    knowledge_base_golden_suggest,
    knowledge_base_golden_add,
    knowledge_base_delete_document,
    DEFAULT_NAMESPACE,
    MAX_MEDIA_BYTES,
    MAX_MEDIA_COUNT,
)


# ── knowledge_base_search ───────────────────────────────────────────────────────


async def test_search_returns_structured_result(fake_client):
    fake_client.search_result = {
        "answer": "先调 POST /api/v1/getToken 获取 token[文档1]",
        "sources": [
            {
                "title": "章管家接口文档 - 概述与获取token接口",
                "doc_id": "stamp-project/章管家/接口文档/02_概述与获取token接口.md",
                "score": 0.919,
                "engine": "vector",
                "confidence": "★★★",
                "confidence_basis": "rerank",
                "source_path": "direct",
                "doc_type": "reference",
                "snippet": "第2章 获取token接口……",
            }
        ],
        "query": "怎么获取token",
        "namespace": "stamp-project",
        "degraded": False,
        "missing_components": [],
        "gap_warning": None,
    }

    result = await knowledge_base_search(
        query="怎么获取token", namespace="stamp-project", client=fake_client
    )

    sc = result.structured_content
    assert sc["status"] == "ok"
    assert sc["answer"].startswith("先调")
    assert sc["sources"][0]["doc_id"].endswith("02_概述与获取token接口.md")
    assert sc["degraded"] is False
    assert sc["gap_warning"] is None
    # 无图 source → 只有文本 content 块，无 image 块。
    assert all(b.type == "text" for b in result.content)


async def test_search_passes_images_through(fake_client):
    """source 的 images 字段透传（§4.3），空数组正常返回不报错。"""
    fake_client.search_result = {
        "sources": [
            {
                "title": "t",
                "doc_id": "imgtest-mcp/imgtest-mcp.md",
                "engine": "vector",
                "images": ["/api/v1/media/imgtest-mcp/imgtest-mcp_assets/img0.png"],
            }
        ],
        "degraded": False,
    }

    result = await knowledge_base_search(query="q", namespace="imgtest-mcp", client=fake_client)

    assert result.structured_content["sources"][0]["images"] == [
        "/api/v1/media/imgtest-mcp/imgtest-mcp_assets/img0.png"
    ]


async def test_search_images_absent_defaults_empty(fake_client):
    """backend 未带 images 或缺省 → []，不报错。"""
    fake_client.search_result = {"sources": [{"title": "t", "doc_id": "x.md"}], "degraded": False}

    result = await knowledge_base_search(query="q", client=fake_client)

    assert result.structured_content["sources"][0]["images"] == []


async def test_search_defaults_namespace(fake_client):
    """namespace 缺省时落到 stamp-project（非 default）。"""
    fake_client.search_result = {"sources": [], "degraded": False}

    await knowledge_base_search(query="q", client=fake_client)

    assert fake_client.search_calls[0]["namespace"] == DEFAULT_NAMESPACE


async def test_search_passes_filters_through(fake_client):
    """可选过滤字段（doc_type/tags/categories/use_graph）原样透传。"""
    fake_client.search_result = {"sources": []}

    await knowledge_base_search(
        query="q",
        namespace="stamp-project",
        top_k=10,
        doc_type="reference",
        tags=["章管家"],
        categories=["接口文档"],
        use_graph=False,
        client=fake_client,
    )

    call = fake_client.search_calls[0]
    assert call["top_k"] == 10
    assert call["doc_type"] == "reference"
    assert call["tags"] == ["章管家"]
    assert call["use_graph"] is False


async def test_search_empty_query_returns_error(fake_client):
    result = await knowledge_base_search(query="   ", client=fake_client)
    assert result.structured_content["status"] == "error"
    assert "不能为空" in result.structured_content["message"]


async def test_search_transparently_passes_degraded_and_gap_warning(fake_client):
    """degraded / gap_warning 原样透传，让 agent 能如实告知用户。"""
    fake_client.search_result = {
        "answer": "未找到",
        "sources": [],
        "degraded": True,
        "missing_components": ["rerank"],
        "gap_warning": "知识库可能覆盖不足",
    }

    result = await knowledge_base_search(query="q", client=fake_client)

    sc = result.structured_content
    assert sc["degraded"] is True
    assert sc["missing_components"] == ["rerank"]
    assert sc["gap_warning"] == "知识库可能覆盖不足"


async def test_search_rag_error_400_hints_namespace(fake_client):
    fake_client.search_error = RagError("bad namespace", status_code=400)

    result = await knowledge_base_search(query="q", client=fake_client)

    sc = result.structured_content
    assert sc["status"] == "error"
    assert "knowledge_base_namespaces" in sc["message"]


# ── knowledge_base_search 内嵌 image content 块 ────────────────────────────────


async def test_search_embeds_image_content_block(fake_client):
    """source.images 命中 → content 里出现 image 块（base64 + mime 从扩展名推断）。"""
    fake_client.search_result = {
        "sources": [
            {
                "title": "t",
                "doc_id": "imgtest-mcp/imgtest-mcp.md",
                "images": ["/api/v1/media/imgtest-mcp/imgtest-mcp_assets/img0.png"],
            }
        ],
        "degraded": False,
    }
    fake_client.media_result = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

    result = await knowledge_base_search(query="q", namespace="imgtest-mcp", client=fake_client)

    image_blocks = [b for b in result.content if b.type == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0].mime_type == "image/png"
    assert base64.b64decode(image_blocks[0].data) == fake_client.media_result


async def test_search_media_fetch_failure_degrades(fake_client):
    """拉图失败 → 该图降级回 URL，不报错、无 image 块。"""
    fake_client.search_result = {
        "sources": [
            {
                "title": "t",
                "doc_id": "imgtest-mcp/imgtest-mcp.md",
                "images": ["/api/v1/media/imgtest-mcp/imgtest-mcp_assets/img0.png"],
            }
        ],
        "degraded": False,
    }
    fake_client.media_error = RagError("404", status_code=404)

    result = await knowledge_base_search(query="q", namespace="imgtest-mcp", client=fake_client)

    assert all(b.type == "text" for b in result.content)
    # URL 仍在 structured 里作文本降级。
    assert result.structured_content["sources"][0]["images"]


async def test_search_media_oversize_degrades(fake_client):
    """单张 >MAX_MEDIA_BYTES → 跳过，不报错。"""
    fake_client.search_result = {
        "sources": [
            {
                "title": "t",
                "doc_id": "imgtest-mcp/imgtest-mcp.md",
                "images": ["/api/v1/media/imgtest-mcp/imgtest-mcp_assets/img0.png"],
            }
        ],
        "degraded": False,
    }
    fake_client.media_result = b"x" * (MAX_MEDIA_BYTES + 1)

    result = await knowledge_base_search(query="q", namespace="imgtest-mcp", client=fake_client)

    assert [b for b in result.content if b.type == "image"] == []


async def test_search_media_respects_count_cap(fake_client):
    """最多返回 MAX_MEDIA_COUNT 张，多的只保留 URL。"""
    urls = [f"/api/v1/media/ns/doc_assets/img{i}.png" for i in range(5)]
    fake_client.search_result = {
        "sources": [
            {"title": "t", "doc_id": "imgtest-mcp/imgtest-mcp.md", "images": urls},
        ],
        "degraded": False,
    }
    fake_client.media_result = b"\x89PNG\x00"

    result = await knowledge_base_search(query="q", namespace="imgtest-mcp", client=fake_client)

    image_blocks = [b for b in result.content if b.type == "image"]
    assert len(image_blocks) == MAX_MEDIA_COUNT


async def test_search_images_absent_produce_no_image_block(fake_client):
    """无 images → content 只有文本块。"""
    fake_client.search_result = {"sources": [{"title": "t", "doc_id": "x.md"}], "degraded": False}

    result = await knowledge_base_search(query="q", client=fake_client)

    assert all(b.type == "text" for b in result.content)


# ── knowledge_base_namespaces / health ──────────────────────────────────────────


async def test_namespaces_returns_list(fake_client):
    fake_client.namespaces_result = ["stamp-project"]

    result = await knowledge_base_namespaces(client=fake_client)

    assert result["status"] == "ok"
    assert result["namespaces"] == ["stamp-project"]


async def test_health_returns_components(fake_client):
    fake_client.health_result = {
        "status": "ok",
        "es": "ok",
        "neo4j": "ok",
        "llm": "configured",
    }

    result = await knowledge_base_health(client=fake_client)

    assert result["status"] == "ok"
    assert result["health"]["es"] == "ok"


# ── knowledge_base_ingest ───────────────────────────────────────────────────────


async def test_ingest_returns_result(fake_client):
    fake_client.ingest_result = {
        "status": "ok",
        "namespace": "stamp-project",
        "doc_id": "stamp-project/接口说明.md",
        "chunks": 2,
    }

    result = await knowledge_base_ingest(
        file_bytes=b"hello", filename="接口说明.md", namespace="stamp-project",
        client=fake_client,
    )

    assert result["status"] == "ok"
    assert result["chunks"] == 2


async def test_ingest_empty_bytes_returns_error(fake_client):
    result = await knowledge_base_ingest(
        file_bytes=b"", filename="x.md", namespace="stamp-project", client=fake_client
    )
    assert result["status"] == "error"
    assert "不能为空" in result["message"]


async def test_ingest_oversize_returns_error(fake_client):
    big = b"x" * (20 * 1024 * 1024 + 1)
    result = await knowledge_base_ingest(
        file_bytes=big, filename="x.md", namespace="stamp-project", client=fake_client
    )
    assert result["status"] == "error"
    assert "20MB" in result["message"]


# ── knowledge_base_ingest_file（服务器路径，大文件首选） ──────────────────────────


async def test_ingest_file_returns_result(fake_client):
    fake_client.ingest_path_result = {
        "status": "ok",
        "namespace": "kingdee-galaxy",
        "doc_id": "kingdee-galaxy/PUR_Requisition_采购申请单.md",
        "chunks": 16,
    }

    result = await knowledge_base_ingest_file(
        file_path="/opt/general-rag/ingest/PUR_Requisition_采购申请单.md",
        namespace="kingdee-galaxy",
        client=fake_client,
    )

    assert result["status"] == "ok"
    assert result["chunks"] == 16
    # 参数原样透传给 client.ingest_path
    call = fake_client.ingest_path_calls[0]
    assert call["file_path"] == "/opt/general-rag/ingest/PUR_Requisition_采购申请单.md"
    assert call["namespace"] == "kingdee-galaxy"


async def test_ingest_file_empty_path_returns_error(fake_client):
    result = await knowledge_base_ingest_file(
        file_path="", namespace="kingdee-galaxy", client=fake_client
    )
    assert result["status"] == "error"
    assert "file_path 不能为空" in result["message"]


async def test_ingest_file_rag_error_passthrough(fake_client):
    fake_client.ingest_path_error = RagError("文件不存在", status_code=400)

    result = await knowledge_base_ingest_file(
        file_path="/nonexistent.md", namespace="kingdee-galaxy", client=fake_client
    )
    assert result["status"] == "error"
    assert "文件不存在" in result["message"]


async def test_rag_client_ingest_path_never_retries(mock_rag):
    """ingest-path 是写操作，503 也不重试。"""
    mock_rag.enqueue(503, {})

    with pytest.raises(RagError):
        await mock_rag.ingest_path(
            file_path="/opt/general-rag/ingest/x.md", namespace="stamp-project"
        )
    assert mock_rag._responses == []


# ── rag_client 重试语义 ─────────────────────────────────────────────────────────


async def test_rag_client_retries_on_503(mock_rag):
    """503 读接口指数退避重试，最终成功。"""
    mock_rag.enqueue(503, {})
    mock_rag.enqueue(200, {"sources": [], "degraded": False})

    result = await mock_rag.search(query="q", namespace="stamp-project")

    assert result["degraded"] is False
    # 两个响应都被消费（1 次 503 + 1 次成功）。
    assert mock_rag._responses == []


async def test_rag_client_no_retry_on_400(mock_rag):
    """400 不重试，直接抛 RagError。"""
    mock_rag.enqueue(400, {"detail": "namespace required"})

    with pytest.raises(RagError) as exc:
        await mock_rag.search(query="q", namespace="stamp-project")

    assert exc.value.status_code == 400
    # 仅消费一个响应，未重试。
    assert mock_rag._responses == []


async def test_rag_client_ingest_never_retries(mock_rag):
    """ingest 是写操作，503 也不重试。"""
    mock_rag.enqueue(503, {})

    with pytest.raises(RagError):
        await mock_rag.ingest(file_bytes=b"x", filename="x.md", namespace="stamp-project")

    assert mock_rag._responses == []


# ── knowledge_base_golden_suggest（反推问法初稿，只读） ───────────────────────────


async def test_golden_suggest_returns_candidates(fake_client):
    fake_client.golden_suggest_result = {
        "status": "ok",
        "namespace": "stamp-project",
        "doc_id": "stamp-project/a.md",
        "candidates": [{"query": "怎么开通章管家", "negatives": ["stamp-project/b.md"]}],
    }

    result = await knowledge_base_golden_suggest(
        namespace="stamp-project", doc_id="stamp-project/a.md", client=fake_client
    )

    assert result["status"] == "ok"
    assert result["candidates"][0]["query"] == "怎么开通章管家"
    assert fake_client.golden_suggest_calls[0]["namespace"] == "stamp-project"


async def test_golden_suggest_empty_doc_id_returns_error(fake_client):
    result = await knowledge_base_golden_suggest(
        namespace="stamp-project", doc_id="   ", client=fake_client
    )
    assert result["status"] == "error"
    assert "doc_id 不能为空" in result["message"]


async def test_golden_suggest_404_red_line_message(fake_client):
    """404 = 文档不在库，红线：不能反推，不得硬凑。"""
    fake_client.golden_suggest_error = RagError("not found", status_code=404)

    result = await knowledge_base_golden_suggest(
        namespace="stamp-project", doc_id="missing.md", client=fake_client
    )
    assert result["status"] == "error"
    assert "不能反推" in result["message"]


async def test_golden_suggest_502_exhausted_honest_error(fake_client):
    """502 耗尽仍败 → 如实报错不编造。"""
    fake_client.golden_suggest_error = RagError("llm failed", status_code=502)

    result = await knowledge_base_golden_suggest(
        namespace="stamp-project", doc_id="a.md", client=fake_client
    )
    assert result["status"] == "error"
    assert "未编造" in result["message"]


# ── knowledge_base_golden_add（两步确认写入，写） ────────────────────────────────


async def test_golden_add_cleans_negatives_and_passes_source(fake_client):
    fake_client.golden_add_result = {"status": "ok", "case": {"id": "stamp-project-0001"}}

    result = await knowledge_base_golden_add(
        namespace="stamp-project",
        doc_id="stamp-project/a.md",
        query="怎么开通章管家",
        negatives=["stamp-project/b.md", "", "  ", "stamp-project/c.md"],
        client=fake_client,
    )

    assert result["status"] == "ok"
    assert result["case"]["id"].endswith("0001")
    call = fake_client.golden_add_calls[0]
    # 空串/纯空白负样本被去除；source 由 client 端固定 "mcp"。
    assert call["negatives"] == ["stamp-project/b.md", "stamp-project/c.md"]


async def test_golden_add_non_list_negatives_coerced(fake_client):
    fake_client.golden_add_result = {"status": "ok", "case": {"id": "x-0001"}}

    await knowledge_base_golden_add(
        namespace="stamp-project",
        doc_id="stamp-project/a.md",
        query="q",
        negatives="not-a-list",  # type: ignore[arg-type]
        client=fake_client,
    )

    assert fake_client.golden_add_calls[0]["negatives"] == []


async def test_golden_add_empty_query_returns_error(fake_client):
    result = await knowledge_base_golden_add(
        namespace="stamp-project", doc_id="stamp-project/a.md", query="  ", client=fake_client
    )
    assert result["status"] == "error"
    assert "query 不能为空" in result["message"]


async def test_golden_add_ghost_reference_passthrough(fake_client):
    """幽灵引用（期望命中/负样本 doc_id 不在库）= 后端 400，透传并提示。"""
    fake_client.golden_add_error = RagError("ghost", status_code=400)

    result = await knowledge_base_golden_add(
        namespace="stamp-project",
        doc_id="not-in-kb.md",
        query="q",
        client=fake_client,
    )
    assert result["status"] == "error"
    assert "幽灵引用" in result["message"]


# ── knowledge_base_delete_document（三步流删除，写） ─────────────────────────────


async def test_delete_document_preserves_preview_status(fake_client):
    """dry_run=true 后端返 status:"preview"，不得覆盖为 ok。"""
    fake_client.delete_result = {
        "status": "preview",
        "namespace": "stamp-project",
        "doc_id": "stamp-project/a.md",
        "chunks": 5,
        "golden_impact": {"cases": 2, "case_ids": ["stamp-project-0001", "stamp-project-0002"]},
    }

    result = await knowledge_base_delete_document(
        namespace="stamp-project", doc_id="stamp-project/a.md", client=fake_client
    )

    assert result["status"] == "preview"
    assert result["golden_impact"]["cases"] == 2


async def test_delete_document_defaults_dry_run_true(fake_client):
    fake_client.delete_result = {"status": "preview", "dry_run": True}

    await knowledge_base_delete_document(
        namespace="stamp-project", doc_id="stamp-project/a.md", client=fake_client
    )

    assert fake_client.delete_calls[0]["dry_run"] is True


async def test_delete_document_requires_target(fake_client):
    result = await knowledge_base_delete_document(namespace="stamp-project", client=fake_client)
    assert result["status"] == "error"
    assert "必须提供 doc_id 或 filename" in result["message"]


async def test_delete_document_dry_run_false_passes_flag(fake_client):
    fake_client.delete_result = {"status": "ok", "chunks_deleted": 5}

    result = await knowledge_base_delete_document(
        namespace="stamp-project", doc_id="stamp-project/a.md", dry_run=False, client=fake_client
    )

    assert result["status"] == "ok"
    assert fake_client.delete_calls[0]["dry_run"] is False


# ── rag_client golden/delete 重试语义 ────────────────────────────────────────────


async def test_golden_suggest_retries_on_502(mock_rag, monkeypatch):
    """502（LLM 瞬态失败）后重试成功；间隔 mock 为 0 防真睡。"""
    monkeypatch.setattr(rag_client_module, "_GOLDEN_SUGGEST_RETRY_DELAY", 0)
    mock_rag.enqueue(502, {})
    mock_rag.enqueue(200, {"status": "ok", "candidates": [{"query": "q", "negatives": []}]})

    result = await mock_rag.golden_suggest(namespace="stamp-project", doc_id="a.md")

    assert result["candidates"][0]["query"] == "q"
    assert mock_rag._responses == []


async def test_golden_suggest_retries_on_empty_candidates(mock_rag, monkeypatch):
    """200 但 candidates 为空 → 视作瞬态空内容，重试。"""
    monkeypatch.setattr(rag_client_module, "_GOLDEN_SUGGEST_RETRY_DELAY", 0)
    mock_rag.enqueue(200, {"status": "ok", "candidates": []})
    mock_rag.enqueue(200, {"status": "ok", "candidates": [{"query": "q", "negatives": []}]})

    result = await mock_rag.golden_suggest(namespace="stamp-project", doc_id="a.md")

    assert len(result["candidates"]) == 1
    assert mock_rag._responses == []


async def test_golden_suggest_no_retry_on_404(mock_rag):
    """404 = 文档不在库，红线：不重试。"""
    mock_rag.enqueue(404, {"detail": "not found"})

    with pytest.raises(RagError) as exc:
        await mock_rag.golden_suggest(namespace="stamp-project", doc_id="missing.md")

    assert exc.value.status_code == 404
    assert mock_rag._responses == []


async def test_golden_suggest_retries_but_exhausts(mock_rag, monkeypatch):
    """连续 502 至多次数耗尽 → 仍 502，不编造。"""
    monkeypatch.setattr(rag_client_module, "_GOLDEN_SUGGEST_RETRY_DELAY", 0)
    for _ in range(3):
        mock_rag.enqueue(502, {})

    with pytest.raises(RagError) as exc:
        await mock_rag.golden_suggest(namespace="stamp-project", doc_id="a.md")

    assert exc.value.status_code == 502
    assert mock_rag._responses == []


async def test_golden_suggest_502_logs_warning_not_error(mock_rag, monkeypatch):
    """瞬态 502 记 WARNING 而非 ERROR——重试语义下不把自愈瞬态当硬错误噪点。"""
    monkeypatch.setattr(rag_client_module, "_GOLDEN_SUGGEST_RETRY_DELAY", 0)
    with capture_logs() as caplogs:
        mock_rag.enqueue(502, {})
        mock_rag.enqueue(200, {"status": "ok", "candidates": [{"query": "q", "negatives": []}]})
        await mock_rag.golden_suggest(namespace="stamp-project", doc_id="a.md")

    events = {log.get("event") for log in caplogs}
    assert "rag_api_transient_retry" in events
    assert "rag_api_error" not in events


async def test_search_502_still_logs_error(mock_rag):
    """其他读接口（未声明 transient_statuses）502 仍记 ERROR，未被软化。"""
    with capture_logs() as caplogs:
        mock_rag.enqueue(502, {})
        with pytest.raises(RagError):
            await mock_rag.search(query="q", namespace="stamp-project")

    events = {log.get("event") for log in caplogs}
    assert "rag_api_error" in events


async def test_golden_suggest_empty_candidates_exhausts(mock_rag, monkeypatch):
    """最后一次 200 但仍空 candidates → 视作失败，不把空结果当成功返回。"""
    monkeypatch.setattr(rag_client_module, "_GOLDEN_SUGGEST_RETRY_DELAY", 0)
    for _ in range(3):
        mock_rag.enqueue(200, {"status": "ok", "candidates": []})

    with pytest.raises(RagError) as exc:
        await mock_rag.golden_suggest(namespace="stamp-project", doc_id="a.md")

    assert exc.value.status_code == 502
    assert mock_rag._responses == []


async def test_golden_add_never_retries(mock_rag):
    """golden_add 是写操作，503 也不重试。"""
    mock_rag.enqueue(503, {})

    with pytest.raises(RagError):
        await mock_rag.golden_add(namespace="stamp-project", doc_id="a.md", query="q")

    assert mock_rag._responses == []


async def test_delete_document_never_retries(mock_rag):
    """delete_document 是写操作（真删不可逆），503 也不重试。"""
    mock_rag.enqueue(503, {})

    with pytest.raises(RagError):
        await mock_rag.delete_document(namespace="stamp-project", doc_id="a.md")

    assert mock_rag._responses == []
