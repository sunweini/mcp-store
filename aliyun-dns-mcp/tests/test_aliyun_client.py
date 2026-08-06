"""AlidnsClient 封装测试：mock 内部 SDK 对象，验证参数映射与错误分类。"""
import pytest

from aliyun_client import AlidnsError, AlidnsClient, ClientFactory, classify_error, _redact_network_message
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
            # 一次性错误：取走后不再触发（重试路径用——首次抛错第二次成功）
            err = self.script.pop("domains_error")
            raise err
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


def _make_client(monkeypatch, sdk):
    """公共构造：monkeypatch _make_sdk 注入 fake，返回 AlidnsClient。"""
    monkeypatch.setattr("aliyun_client.AlidnsClient._make_sdk", lambda self: sdk)
    return AlidnsClient({"access_key_id": "a", "access_key_secret": "s",
                         "region": "cn-hangzhou", "enabled": True})


def test_classify_error():
    err = type("E", (), {"code": "InvalidAccessKeyId.NotFound"})()
    assert classify_error(err) == "invalid_credential"
    err2 = type("E", (), {"code": "Throttling.User"})()
    assert classify_error(err2) == "throttled"
    err3 = type("E", (), {"code": "SomethingElse"})()
    assert classify_error(err3) == "api_error"


def test_classify_network_error():
    """网络异常（含 URL query 的 ConnectionError 形态）必须分类为 network_error。"""
    url_msg = ("HTTPSConnectionPool(host='alidns.cn-hangzhou.aliyuncs.com', port=443): "
               "Max retries exceeded ...: GET https://alidns.cn-hangzhou.aliyuncs.com/?"
               "AccessKeyId=LTAI5t-demo-secret-value&Signature=abc&version=2015-01-09")
    assert classify_error(ConnectionError(url_msg)) == "network_error"
    assert classify_error(TimeoutError(url_msg)) == "network_error"


def test_redact_network_message_strips_query():
    """剥离消息中 "?" 之后的 query——AccessKeyId 明文不得出现在任何消息里。"""
    msg = ("HTTPSConnectionPool(host='alidns.cn-hangzhou.aliyuncs.com', port=443): "
           "Max retries exceeded ...: GET https://alidns.cn-hangzhou.aliyuncs.com/?"
           "AccessKeyId=LTAI5t-demo-secret-value&Signature=abc&version=2015-01-09")
    err = ConnectionError(msg)
    redacted = _redact_network_message(err)
    assert "AccessKeyId" not in redacted
    assert "LTAI5t-demo-secret-value" not in redacted
    assert redacted.startswith("ConnectionError: ")
    # 主机名保留（无敏感信息），query 全部丢弃
    assert "alidns.cn-hangzhou.aliyuncs.com" in redacted
    assert "?" not in redacted


@pytest.mark.asyncio
async def test_network_error_sanitized_in_log_and_span(monkeypatch, caplog):
    """I1 回归：网络错误消息不进日志/span/异常 message（含 URL 时必须剥离）。"""
    import logging
    from opentelemetry import trace as otel_trace

    url_msg = ("HTTPSConnectionPool(host='alidns.cn-hangzhou.aliyuncs.com', port=443): "
               "Max retries exceeded ...: GET https://alidns.cn-hangzhou.aliyuncs.com/?"
               "AccessKeyId=LTAI5t-demo-secret-value&Signature=abc&version=2015-01-09")
    sdk = FakeSDKClient(script={"domains_error": ConnectionError(url_msg)})
    client = _make_client(monkeypatch, sdk)
    caplog.set_level(logging.ERROR)
    with pytest.raises(AlidnsError) as e:
        await client.describe_domains()
    # 三路径（日志 / span 描述 / 工具响应 message）都不含凭证
    assert "LTAI5t-demo-secret-value" not in caplog.text
    assert "AccessKeyId" not in caplog.text
    assert e.value.error_type == "network_error"
    assert "AccessKeyId" not in e.value.message
    assert "LTAI5t-demo-secret-value" not in e.value.message


@pytest.mark.asyncio
async def test_network_error_span_event_uses_sanitized_message(monkeypatch):
    """I1 残余回归：span exception event 不得含 URL query（AccessKeyId 明文）。

    为什么单独抓 span：record_exception 会把 str(exc) 全文写进 event 的
    exception.message（且 stacktrace 首行同样含完整 URL），console span
    exporter 直接打 stdout——日志防线的最后一块拼图。
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # aliyun_client 模块级 tracer 在 import 瞬间绑定默认 no-op provider，
    # 仅 set_tracer_provider 无效——必须整体替换模块 tracer 才能捕获 span
    monkeypatch.setattr("aliyun_client.tracer", provider.get_tracer("test"))

    url_msg = ("HTTPSConnectionPool(host='alidns.cn-hangzhou.aliyuncs.com', port=443): "
               "Max retries exceeded ...: GET https://alidns.cn-hangzhou.aliyuncs.com/?"
               "AccessKeyId=LTAI5t-demo-secret-value&Signature=abc&version=2015-01-09")
    sdk = FakeSDKClient(script={"domains_error": ConnectionError(url_msg)})
    client = _make_client(monkeypatch, sdk)
    with pytest.raises(AlidnsError):
        await client.describe_domains()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "aliyun_client.describe_domains"
    events = [e for e in span.events if e.name == "exception"]
    assert events, "网络错误必须产生 exception span event"
    # 事件属性与 status 描述全链路无凭证、无 URL query（"?" 是 query 分界）
    for ev in events:
        assert "exception.stacktrace" not in ev.attributes  # add_event 不附 traceback
        for attr_value in ev.attributes.values():
            assert "LTAI5t-demo-secret-value" not in str(attr_value)
            assert "AccessKeyId" not in str(attr_value)
            assert "?" not in str(attr_value)
    assert "LTAI5t-demo-secret-value" not in span.status.description
    assert "AccessKeyId" not in span.status.description
    assert "?" not in span.status.description


@pytest.mark.asyncio
async def test_throttled_retries_once_then_succeeds(monkeypatch):
    """I2 回归（spec §7.1）：首次 throttled → 退避重试 1 次 → 成功。"""
    # 缩短退避：单测不真等 1s（重试语义与 sleep 时长无关）
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("aliyun_client.asyncio.sleep", _no_sleep)
    sdk = FakeSDKClient(script={"domains_error": type(
        "E", (Exception,), {"code": "Throttling.User", "message": "QPS limit"})()})
    client = _make_client(monkeypatch, sdk)
    domains = await client.describe_domains()
    assert domains == [{"domain_name": "example.com", "dns_servers": ["ns1"], "record_count": 2}]
    # 重试后成功：SDK 被调 2 次（首次抛 throttled + 重试成功）
    assert len(sdk.calls) == 2


@pytest.mark.asyncio
async def test_throttled_retry_fails_after_one_retry(monkeypatch):
    """I2 边界：重试后仍 throttled → 不再重试直接报错（防死循环）。"""
    async def _no_sleep(_):
        return None
    monkeypatch.setattr("aliyun_client.asyncio.sleep", _no_sleep)

    class AlwaysThrottleSDK(FakeSDKClient):
        def describe_domains_with_options(self, request, runtime):
            self.calls.append(("describe_domains", request))
            raise type("E", (Exception,), {"code": "Throttling.User", "message": "QPS limit"})()

    sdk = AlwaysThrottleSDK()
    client = _make_client(monkeypatch, sdk)
    with pytest.raises(AlidnsError) as e:
        await client.describe_domains()
    assert e.value.error_type == "throttled"
    # 首调 + 1 次重试，绝无第 3 次
    assert len(sdk.calls) == 2


@pytest.mark.asyncio
async def test_describe_domains_maps_response(monkeypatch):
    sdk = FakeSDKClient()
    monkeypatch.setattr("aliyun_client.AlidnsClient._make_sdk", lambda self: sdk)
    client = AlidnsClient({"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True})
    domains = await client.describe_domains(page_size=10, page_num=1)
    assert domains == [{"domain_name": "example.com", "dns_servers": ["ns1"], "record_count": 2}]
    assert sdk.calls[0][0] == "describe_domains"


@pytest.mark.asyncio
async def test_describe_domains_object_dns_servers(monkeypatch):
    """真实 SDK 响应形态回归：dns_servers 是对象（含 .dns_server list）非 list。

    生产实测：DescribeDomainsResponseBodyDomainsDomainDnsServers 对象直接
    list() 报 not iterable——fake 用 ["ns1"] 掩盖了真实形态（Task 4 漏网）。
    """
    class FakeDnsServers:
        dns_server = ["ns1", "ns2"]

    class FakeDomain:
        domain_name = "example.com"
        dns_servers = FakeDnsServers()
        record_count = 3

    class FakeBody:
        domains = type("L", (), {"domain": [FakeDomain()]})()

    class FakeSdk:
        def describe_domains_with_options(self, request, runtime):
            return FakeSDKResponse(FakeBody())

    monkeypatch.setattr("aliyun_client.AlidnsClient._make_sdk", lambda self: FakeSdk())
    client = AlidnsClient({"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True})
    domains = await client.describe_domains()
    assert domains == [{"domain_name": "example.com", "dns_servers": ["ns1", "ns2"], "record_count": 3}]


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
    # TypeError，code/request_id 全部丢失）；(Exception,) 基类保持 type() 动态风格。
    # 非 throttled 错误不触发重试，一次性语义由 FakeSDKClient pop 保证
    sdk = FakeSDKClient(script={"domains_error": type("E", (Exception,), {"code": "InvalidAccessKeyId.NotFound", "request_id": "req-1"})()})
    monkeypatch.setattr("aliyun_client.AlidnsClient._make_sdk", lambda self: sdk)
    client = AlidnsClient({"access_key_id": "a", "access_key_secret": "s", "region": "cn-hangzhou", "enabled": True})
    with pytest.raises(AlidnsError) as e:
        await client.describe_domains()
    assert e.value.error_type == "invalid_credential"
    assert e.value.request_id == "req-1"
    # 非 throttled 不重试：SDK 只被调 1 次
    assert len(sdk.calls) == 1


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
