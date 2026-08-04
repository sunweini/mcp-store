"""请求明细 API：读 MySQL calls 表（全量 tools/call，成功+失败）。

与 /api/failures（Redis 失败流）互补：calls 含全部，failures 只含失败。
"""
from fastapi import APIRouter, Depends, Query

from auth import require_admin
from db import get_pool

router = APIRouter(prefix="/api/calls", tags=["calls"])


async def query_calls(server: str | None, status: str | None,
                      limit: int, offset: int) -> list[dict]:
    """SQL 分页查询 calls 表，倒序（最新 id 在前）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            where = []
            args = []
            if server:
                where.append("server = %s"); args.append(server)
            if status:
                where.append("status = %s"); args.append(status)
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            await cur.execute(
                f"SELECT id, time, server, tool, op, token_name, latency_ms, "
                f"status, error_type, trace FROM calls {clause} "
                f"ORDER BY id DESC LIMIT %s OFFSET %s",
                args + [limit, offset],
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in await cur.fetchall()]


@router.get("")
async def list_calls(
    server: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: str = Depends(require_admin),
):
    rows = await query_calls(server=server, status=status, limit=limit, offset=offset)
    return {"count": len(rows), "data": rows}
