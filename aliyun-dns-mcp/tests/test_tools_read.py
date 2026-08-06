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
    def __init__(self, fail=None):
        self.fail = fail

    async def describe_domains(self, page_size=100, page_num=1):
        if self.fail:
            raise self.fail
        return [{"domain_name": "example.com", "dns_servers": ["ns1"], "record_count": 2}]


class FakeClients:
    def __init__(self, client=None):
        self._client = client or FakeClient()

    def get(self, account_id):
        if account_id == "ghost":
            raise AlidnsError("account_not_found", "not managed")
        return self._client


class FakeStore:
    """记录 disable_account 调用（I3 工具层联动的假 store）。"""
    def __init__(self):
        self.disabled = []

    async def disable_account(self, account_id):
        self.disabled.append(account_id)


def make_ctx(checker=None, clients=None, store=None):
    return ToolContext(checker=checker or FakeChecker(), clients=clients or FakeClients(),
                       store=store)


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


@pytest.mark.asyncio
async def test_invalid_credential_disables_account():
    """I3 回归（spec §7.1）：工具层收到 invalid_credential → 账户被禁用。"""
    store = FakeStore()
    ctx = make_ctx(clients=FakeClients(client=FakeClient(fail=AlidnsError(
        "invalid_credential", "InvalidAccessKeyId.NotFound", "req-1"))), store=store)
    result = await list_domains("acct1", ctx=ctx)
    assert result["status"] == "error"
    assert result["error_type"] == "invalid_credential"
    assert store.disabled == ["acct1"]


@pytest.mark.asyncio
async def test_non_credential_error_does_not_disable():
    """I3 边界：非 invalid_credential 错误不触发禁用。"""
    store = FakeStore()
    ctx = make_ctx(clients=FakeClients(client=FakeClient(fail=AlidnsError(
        "throttled", "Throttling.User", "req-1"))), store=store)
    result = await list_domains("acct1", ctx=ctx)
    assert result["error_type"] == "throttled"
    assert store.disabled == []
