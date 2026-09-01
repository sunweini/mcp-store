"""Knowledge base tool tests.

Covers search 正常解析 / degraded+gap_warning 透传 / 空 query 报错 /
namespace 缺省、namespaces/health 正常路径、ingest 参数校验、以及
rag_client 的 503 退避与 400 不重试。
"""
import pytest

from rag_client import RagError
from tools.knowledge_base import (
    knowledge_base_search,
    knowledge_base_namespaces,
    knowledge_base_health,
    knowledge_base_ingest,
    DEFAULT_NAMESPACE,
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

    assert result["status"] == "ok"
    assert result["answer"].startswith("先调")
    assert result["sources"][0]["doc_id"].endswith("02_概述与获取token接口.md")
    assert result["degraded"] is False
    assert result["gap_warning"] is None


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
    assert result["status"] == "error"
    assert "不能为空" in result["message"]


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

    assert result["degraded"] is True
    assert result["missing_components"] == ["rerank"]
    assert result["gap_warning"] == "知识库可能覆盖不足"


async def test_search_rag_error_400_hints_namespace(fake_client):
    fake_client.search_error = RagError("bad namespace", status_code=400)

    result = await knowledge_base_search(query="q", client=fake_client)

    assert result["status"] == "error"
    assert "knowledge_base_namespaces" in result["message"]


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
