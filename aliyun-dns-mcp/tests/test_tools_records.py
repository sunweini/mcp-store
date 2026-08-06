"""记录工具测试：list/add/update/delete_record，注入 fake ctx。"""
import pytest
from fastmcp.exceptions import ToolError

from tools import ToolContext
from tools.records import list_records, add_record, update_record, delete_record
from aliyun_client import AlidnsError


class FakeChecker:
    def __init__(self, denied=None):
        self.denied = set(denied or [])

    async def require(self, account_id, mode):
        if account_id in self.denied:
            raise ToolError(f"permission denied: no_permission: account '{account_id}'")


class FakeClient:
    def __init__(self, fail_add=False):
        self.fail_add = fail_add

    async def describe_domain_records(self, domain_name, page_size=100, page_num=1):
        return [{"record_id": "r1", "rr": "@", "type": "A", "value": "1.2.3.4",
                 "ttl": 600, "priority": None, "status": "ENABLE"}]

    async def add_domain_record(self, domain_name, rr, type, value, ttl=600, priority=None):
        if self.fail_add:
            raise AlidnsError("throttled", "Throttling.User", "req-1")
        return "new-1"

    async def update_domain_record(self, record_id, **kwargs):
        return None

    async def delete_domain_record(self, record_id):
        return None


class FakeClients:
    def __init__(self, client=None):
        self._client = client or FakeClient()

    def get(self, account_id):
        return self._client


def make_ctx(checker=None, client=None):
    return ToolContext(checker=checker or FakeChecker(), clients=FakeClients(client))


@pytest.mark.asyncio
async def test_list_records_ok():
    ctx = make_ctx()
    result = await list_records("acct1", "example.com", ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"][0]["record_id"] == "r1"


@pytest.mark.asyncio
async def test_add_record_ok():
    ctx = make_ctx()
    result = await add_record("acct1", "example.com", "www", "A", "1.2.3.4", ttl=300, ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"]["record_id"] == "new-1"


@pytest.mark.asyncio
async def test_add_record_write_denied():
    ctx = make_ctx(checker=FakeChecker(denied={"acct1"}))
    with pytest.raises(ToolError):
        await add_record("acct1", "example.com", "www", "A", "1.2.3.4", ctx=ctx)


@pytest.mark.asyncio
async def test_add_record_aliyun_error_mapped():
    ctx = make_ctx(client=FakeClient(fail_add=True))
    result = await add_record("acct1", "example.com", "www", "A", "1.2.3.4", ctx=ctx)
    assert result["status"] == "error"
    assert result["error_type"] == "throttled"
    assert result["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_update_record_ok():
    ctx = make_ctx()
    result = await update_record("acct1", "r1", value="5.6.7.8", ttl=60, ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"]["record_id"] == "r1"


@pytest.mark.asyncio
async def test_update_record_no_fields_rejected():
    ctx = make_ctx()
    result = await update_record("acct1", "r1", ctx=ctx)
    assert result["status"] == "error"
    assert result["error_type"] == "invalid_params"


@pytest.mark.asyncio
async def test_delete_record_ok():
    ctx = make_ctx()
    result = await delete_record("acct1", "r1", ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"]["record_id"] == "r1"
