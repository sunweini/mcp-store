"""AlidnsClient 封装测试：mock 内部 SDK 对象，验证参数映射与错误分类。"""
import pytest

from aliyun_client import AlidnsError, AlidnsClient, ClientFactory, classify_error
from account_store import AccountStore


class FakeSDKResponse:
    """模拟 SDK 响应对象（.body 链）。"""
    def __init__(self, body):
        self.body = body


class FakeSDKClient:
    """记录调用、按脚本返回/抛错，模拟 alibabacloud 同步 client。

    真实 SDK 的 with_options(request, runtime) 第二个参数必传（缺它
    TypeError，端到端验证实测）——fake 签名保持同构，防回归。
    """
    def __init__(self, script=None):
        self.calls = []
        self.script = script or {}

    def describe_domains_with_options(self, request, runtime):
        self.calls.append(("describe_domains", request))
        if "domains_error" in self.script:
            raise self.script["domains_error"]
        domain = type("D", (), {"domain_name": "example.com", "dns_servers": ["ns1"], "record_count": 2})()
        return FakeSDKResponse(type("B", (), {"domains": type("L", (), {"domain": [domain]})})())

    def describe_domain_records_with_options(self, request, runtime):
        self.calls.append(("describe_domain_records", request))
        rec = type("R", (), {"record_id": "r1", "rr": "@", "type": "A", "value": "1.2.3.4",
                             "ttl": 600, "priority": None, "status": "ENABLE"})()
        return FakeSDKResponse(type("B", (), {"domain_records": type("L", (), {"record": [rec]})})())

    def add_domain_record_with_options(self, request, runtime):
        self.calls.append(("add_domain_record", request))
        return FakeSDKResponse(type("B", (), {"record_id": "new-1"})())

    def update_domain_record_with_options(self, request, runtime):
        self.calls.append(("update_domain_record", request))

    def delete_domain_record_with_options(self, request, runtime):
        self.calls.append(("delete_domain_record", request))


class FakeCredentialsStore:
    def __init__(self, creds):
        self._creds = creds

    def get_credentials(self, account_id):
        return self._creds.get(account_id)


def test_classify_error():
    err = type("E", (), {"code": "InvalidAccessKeyId.NotFound"})()
    assert classify_error(err) == "invalid_credential"
    err2 = type("E", (), {"code": "Throttling.User"})()
    assert classify_error(err2) == "throttled"
    err3 = type("E", (), {"code": "SomethingElse"})()
    assert classify_error(err3) == "api_error"


@pytest.mark.asyncio
async def test_describe_domains_maps_response(monkeypatch):
    sdk = FakeSDKClient()
    monkeypatch.setattr("aliyun_client.AlidnsClient._make_sdk", lambda self: sdk)
    client = AlidnsClient({"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True})
    domains = await client.describe_domains(page_size=10, page_num=1)
    assert domains == [{"domain_name": "example.com", "dns_servers": ["ns1"], "record_count": 2}]
    assert sdk.calls[0][0] == "describe_domains"


@pytest.mark.asyncio
async def test_add_domain_record_returns_id(monkeypatch):
    sdk = FakeSDKClient()
    monkeypatch.setattr("aliyun_client.AlidnsClient._make_sdk", lambda self: sdk)
    client = AlidnsClient({"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True})
    record_id = await client.add_domain_record("example.com", "www", "A", "1.2.3.4", ttl=600)
    assert record_id == "new-1"
    req = sdk.calls[0][1]
    assert req.domain_name == "example.com" and req.rr == "www" and req.type == "A"


@pytest.mark.asyncio
async def test_aliyun_error_wrapped(monkeypatch):
    # 错误对象必须派生 BaseException 才能被 raise（空 bases 的对象 raise 会变
    # TypeError，code/request_id 全部丢失）；(Exception,) 基类保持 type() 动态风格
    sdk = FakeSDKClient(script={"domains_error": type("E", (Exception,), {"code": "Throttling.User", "request_id": "req-1"})()})
    monkeypatch.setattr("aliyun_client.AlidnsClient._make_sdk", lambda self: sdk)
    client = AlidnsClient({"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True})
    with pytest.raises(AlidnsError) as e:
        await client.describe_domains()
    assert e.value.error_type == "throttled"
    assert e.value.request_id == "req-1"


@pytest.mark.asyncio
async def test_with_options_gets_runtime(monkeypatch):
    """回归：with_options 第二个参数 runtime 必传（端到端验证实测 TypeError），
    公共入口 _call 统一注入，fake 签名同构防回归。"""
    sdk = FakeSDKClient()
    monkeypatch.setattr("aliyun_client.AlidnsClient._make_sdk", lambda self: sdk)
    client = AlidnsClient({"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True})
    await client.delete_domain_record("r-1")
    # fake 记录 (name, request)；runtime 参数存在性由 fake 签名本身保证（缺它即 TypeError）
    assert sdk.calls[0][0] == "delete_domain_record"
    assert sdk.calls[0][1].record_id == "r-1"


def test_client_factory_caches_and_rebuilds():
    creds_a = {"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True}
    store = FakeCredentialsStore({"acct1": creds_a})
    factory = ClientFactory(store)
    c1 = factory.get("acct1")
    c2 = factory.get("acct1")
    assert c1 is c2  # 缓存
    # 凭证变化（模拟热更新）→ 重建
    store._creds["acct1"] = {**creds_a, "access_key_secret": "new"}
    c3 = factory.get("acct1")
    assert c3 is not c1


def test_client_factory_missing_or_disabled():
    store = FakeCredentialsStore({})
    factory = ClientFactory(store)
    with pytest.raises(AlidnsError) as e:
        factory.get("ghost")
    assert e.value.error_type == "account_not_found"
    store2 = FakeCredentialsStore({"acct1": {"access_key_id": "a", "access_key_secret": "s",
                                             "region": "cn-hangzhou", "enabled": False}})
    factory2 = ClientFactory(store2)
    with pytest.raises(AlidnsError) as e2:
        factory2.get("acct1")
    assert e2.value.error_type == "account_disabled"
