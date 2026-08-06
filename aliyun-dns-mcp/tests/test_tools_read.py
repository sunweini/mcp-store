"""读工具测试：list_accounts / list_domains，注入 fake ctx。"""
import pytest
from fastmcp.exceptions import ToolError

from tools import ToolContext
from tools.accounts import list_accounts
from tools.domains import list_domains
from aliyun_client import AlidnsError


class FakeChecker:
    def __init__(self, accounts=None, denied=None):
        self.accounts = accounts or []
        self.denied = set(denied or [])

    async def require(self, account_id, mode):
        if account_id in self.denied:
            raise ToolError(f"permission denied: no_permission: account '{account_id}'")

    async def allowed_accounts(self):
        return self.accounts


class FakeClient:
    async def describe_domains(self, page_size=100, page_num=1):
        return [{"domain_name": "example.com", "dns_servers": ["ns1"], "record_count": 2}]


class FakeClients:
    def __init__(self, client=None):
        self._client = client or FakeClient()

    def get(self, account_id):
        if account_id == "ghost":
            raise AlidnsError("account_not_found", "not managed")
        return self._client


def make_ctx(checker=None, clients=None):
    return ToolContext(checker=checker or FakeChecker(), clients=clients or FakeClients())


@pytest.mark.asyncio
async def test_list_accounts_ok():
    ctx = make_ctx(checker=FakeChecker(accounts=[
        {"account_id": "acct1", "description": "账户1", "read": True, "write": False}]))
    result = await list_accounts(ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"][0]["account_id"] == "acct1"
    assert result["data"][0]["write"] is False


@pytest.mark.asyncio
async def test_list_accounts_invalid_token():
    class DenyChecker:
        async def allowed_accounts(self):
            raise ToolError("permission denied: invalid_token")
    ctx = make_ctx(checker=DenyChecker())
    with pytest.raises(ToolError):
        await list_accounts(ctx=ctx)


@pytest.mark.asyncio
async def test_list_domains_ok():
    ctx = make_ctx()
    result = await list_domains("acct1", ctx=ctx)
    assert result["status"] == "ok"
    assert result["data"][0]["domain_name"] == "example.com"


@pytest.mark.asyncio
async def test_list_domains_denied():
    ctx = make_ctx(checker=FakeChecker(denied={"acct1"}))
    with pytest.raises(ToolError):
        await list_domains("acct1", ctx=ctx)


@pytest.mark.asyncio
async def test_list_domains_account_missing():
    ctx = make_ctx(clients=FakeClients())
    result = await list_domains("ghost", ctx=ctx)
    assert result["status"] == "error"
    assert result["error_type"] == "account_not_found"
