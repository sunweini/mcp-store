"""指标绑定回归测试（审查 Important #1/#2 修复验证）。

tools/__init__.py 与 aliyun_client.py 用 `import telemetry`（运行时取值）
而非 `from telemetry import X`：monkeypatch telemetry 模块级指标后，
wrapper/_call 必须取到新值——若 from-import 旧实现（加载时绑定 None），
monkeypatch 不影响已绑定的名字，fake 收不到调用，测试失败。
"""
import pytest

import telemetry
from tools import _metrics_wrapper


class FakeCounter:
    """记录 add/record 调用的 fake 指标对象（证明绑定已生效）。"""

    def __init__(self):
        self.add_calls: list[tuple[int, dict | None]] = []
        self.record_calls: list[tuple[float, dict | None]] = []

    def add(self, value, attributes=None):
        self.add_calls.append((value, attributes))

    def record(self, value, attributes=None):
        self.record_calls.append((value, attributes))


@pytest.mark.asyncio
async def test_metrics_wrapper_records_to_runtime_telemetry(monkeypatch):
    """monkeypatch telemetry 指标 → wrapper 必须收到 add/record 调用。

    模拟 init_telemetry 之后（指标被赋值）的路径；运行时取值模式下
    monkeypatch 生效。旧 from-import 实现下此测试失败（绑定 None）。
    """
    fake = FakeCounter()
    monkeypatch.setattr(telemetry, "REQUESTS_TOTAL", fake)
    monkeypatch.setattr(telemetry, "IN_FLIGHT_REQUESTS", fake)
    monkeypatch.setattr(telemetry, "REQUEST_DURATION", fake)
    monkeypatch.setattr(telemetry, "ERRORS_TOTAL", fake)

    called = []

    @_metrics_wrapper("test_tool")
    async def fn():
        called.append(1)
        return {"status": "ok", "data": []}

    result = await fn()

    assert result == {"status": "ok", "data": []}
    assert called == [1]
    # REQUESTS_TOTAL.add(1) + IN_FLIGHT add(1) + add(-1) = 3 次 add
    assert [v for v, _ in fake.add_calls] == [1, 1, -1]
    # duration 已记录（finally 分支）
    assert fake.record_calls
    # tool_name 属性透传（低基数 label）
    assert fake.add_calls[0][1] == {"tool_name": "test_tool"}


@pytest.mark.asyncio
async def test_metrics_wrapper_exception_records_errors(monkeypatch):
    """异常路径记 ERRORS_TOTAL（error_type=异常类名）。"""
    fake = FakeCounter()
    monkeypatch.setattr(telemetry, "REQUESTS_TOTAL", fake)
    monkeypatch.setattr(telemetry, "IN_FLIGHT_REQUESTS", fake)
    monkeypatch.setattr(telemetry, "REQUEST_DURATION", fake)
    monkeypatch.setattr(telemetry, "ERRORS_TOTAL", fake)

    @_metrics_wrapper("boom_tool")
    async def fn():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await fn()

    error_adds = [attrs for _, attrs in fake.add_calls if attrs]
    assert {"tool_name": "boom_tool", "error_type": "ValueError"} in error_adds


@pytest.mark.asyncio
async def test_metrics_wrapper_error_dict_records_errors(monkeypatch):
    """工具返回 status=error 结构也记 ERRORS_TOTAL（error_type=tool_error）。"""
    fake = FakeCounter()
    monkeypatch.setattr(telemetry, "ERRORS_TOTAL", fake)
    monkeypatch.setattr(telemetry, "REQUESTS_TOTAL", fake)

    @_metrics_wrapper("fail_tool")
    async def fn():
        return {"status": "error", "error_type": "api_error", "message": "x", "request_id": None}

    await fn()

    error_adds = [(v, attrs) for v, attrs in fake.add_calls if attrs and attrs.get("error_type") == "tool_error"]
    assert error_adds == [(1, {"tool_name": "fail_tool", "error_type": "tool_error"})]


@pytest.mark.asyncio
async def test_aliyun_call_records_dependency_metrics(monkeypatch):
    """_call 成功记 DEPENDENCY_DURATION、失败记 DEPENDENCY_ERRORS_TOTAL（审查 #2 修复验证）。"""
    from types import SimpleNamespace

    from aliyun_client import AlidnsClient

    fake = FakeCounter()
    monkeypatch.setattr(telemetry, "DEPENDENCY_DURATION", fake)
    monkeypatch.setattr(telemetry, "DEPENDENCY_ERRORS_TOTAL", fake)

    client = AlidnsClient({"access_key_id": "ak", "access_key_secret": "sk"})
    # 替换 _sdk 为 stub，避免真实 SDK 依赖（_make_sdk 独立方法即为此设计）
    client._sdk = SimpleNamespace(describe_domains_with_options=lambda req: None)

    resp = await client._call("describe_domains_with_options", object(), "aliyun_client.test")
    assert resp is None
    assert fake.record_calls  # 成功也记延迟
    assert fake.add_calls == []  # 成功无错误

    class Boom(Exception):
        pass

    def fail(_req):
        raise Boom("boom")

    client._sdk = SimpleNamespace(fail_op=fail)
    with pytest.raises(Exception):
        await client._call("fail_op", object(), "aliyun_client.fail")
    assert fake.add_calls  # dependency_errors_total 已记录
    assert fake.add_calls[0][1] == {"dependency": "alidns_api", "error_type": "api_error"}
