"""Alidns SDK 封装：每账户一个 client，同步 SDK 走 asyncio.to_thread。

用官方 SDK（alibabacloud-alidns20150109 + tea-openapi）而不是裸 HTTP：
RPC 签名/端点选择/错误对象解析交给 SDK，MCP 层只做错误分类与 trace。
SDK 是同步 API，异步工具里用 asyncio.to_thread 防阻塞 event loop。

安全：SDK RPC 请求 URL query 含 AccessKeyId——httpx logger 必须提到
WARNING（logging_config 处理），日志只记 account_id 不记凭证。
"""
from __future__ import annotations  # ClientFactory.__init__ 注解引用 AccountStore（仅类型），不引入运行时 import

import asyncio

import structlog
from opentelemetry import trace

logger = structlog.get_logger()
tracer = trace.get_tracer("aliyun-dns-mcp")

ALIDNS_ENDPOINT = "alidns.cn-hangzhou.aliyuncs.com"


class AlidnsError(Exception):
    """阿里云 API 调用失败（已分类）。error_type 供工具层映射对外错误。"""

    def __init__(self, error_type: str, message: str, request_id: str | None = None):
        super().__init__(message)
        # NOTE: 工具层错误映射用 e.message 拼返回结构（spec §7.1），
        # Exception 无该属性（Python 2 后移除），显式暴露
        self.message = message
        self.error_type = error_type
        self.request_id = request_id


def classify_error(exc: Exception) -> str:
    """SDK 异常 → error_type。

    错误码为示例，以实测为准（spec §7.1）——SDK 异常对象带 .code 与
    .message，这里组合文本匹配，避免裸 code 匹配漏掉变体。
    """
    code = str(getattr(exc, "code", ""))
    msg = str(exc)
    combined = (code + " " + msg).lower()
    if any(k in combined for k in ("invalidaccesskeyid", "forbidden", "signaturedoesnotmatch", "incompletesignature")):
        return "invalid_credential"
    if "throttling" in combined or "qps" in combined:
        return "throttled"
    if "domain" in combined and "exist" in combined:
        return "not_found"
    return "api_error"


class AlidnsClient:
    def __init__(self, credentials: dict):
        self._credentials = credentials
        self._sdk = self._make_sdk()

    def _make_sdk(self):
        """构造 SDK client。独立方法便于测试 monkeypatch。"""
        from alibabacloud_tea_openapi import models as open_api_models
        from alibabacloud_alidns20150109 import client as alidns_client
        return alidns_client.Client(open_api_models.Config(
            access_key_id=self._credentials["access_key_id"],
            access_key_secret=self._credentials["access_key_secret"],
            endpoint=ALIDNS_ENDPOINT,
        ))

    async def _call(self, api_name: str, request, span_name: str):
        def run():
            fn = getattr(self._sdk, api_name)
            return fn(request)

        with tracer.start_as_current_span(span_name) as span:
            span.set_attribute("operation.type", "aliyun_api")
            try:
                return await asyncio.to_thread(run)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
                err_type = classify_error(exc)
                logger.error("aliyun_api_error", service="aliyun-dns-mcp",
                             api=api_name, error_type=err_type, error=str(exc))
                raise AlidnsError(err_type, str(exc), getattr(exc, "request_id", None)) from exc

    @staticmethod
    def _body(resp):
        return resp.body

    async def describe_domains(self, page_size: int = 100, page_num: int = 1) -> list[dict]:
        from alibabacloud_alidns20150109 import models
        req = models.DescribeDomainsRequest(page_size=page_size, page_number=page_num)
        resp = await self._call("describe_domains_with_options", req, "aliyun_client.describe_domains")
        domains = self._body(resp).domains.domain or []
        return [{
            "domain_name": d.domain_name,
            "dns_servers": list(getattr(d, "dns_servers", None) or []),
            "record_count": getattr(d, "record_count", None),
        } for d in domains]

    async def describe_domain_records(self, domain_name: str, page_size: int = 100,
                                      page_num: int = 1) -> list[dict]:
        from alibabacloud_alidns20150109 import models
        req = models.DescribeDomainRecordsRequest(
            domain_name=domain_name, page_size=page_size, page_number=page_num)
        resp = await self._call("describe_domain_records_with_options", req,
                                "aliyun_client.describe_domain_records")
        records = self._body(resp).domain_records.record or []
        return [{
            "record_id": r.record_id,
            "rr": r.rr,
            "type": r.type,
            "value": r.value,
            "ttl": getattr(r, "ttl", None),
            "priority": getattr(r, "priority", None),
            "status": getattr(r, "status", None),
        } for r in records]

    async def add_domain_record(self, domain_name: str, rr: str, type: str, value: str,
                                ttl: int = 600, priority: int | None = None) -> str:
        from alibabacloud_alidns20150109 import models
        req = models.AddDomainRecordRequest(
            domain_name=domain_name, rr=rr, type=type, value=value, ttl=ttl)
        if priority is not None:
            req.priority = priority
        resp = await self._call("add_domain_record_with_options", req, "aliyun_client.add_domain_record")
        return self._body(resp).record_id

    async def update_domain_record(self, record_id: str, rr: str | None = None,
                                   type: str | None = None, value: str | None = None,
                                   ttl: int | None = None, priority: int | None = None) -> None:
        from alibabacloud_alidns20150109 import models
        req = models.UpdateDomainRecordRequest(record_id=record_id)
        # 只更新传入的字段：阿里云要求全量语义，但工具层允许部分更新——
        # 未传字段不覆盖（SDK 请求对象不设值即不发送）
        if rr is not None:
            req.rr = rr
        if type is not None:
            req.type = type
        if value is not None:
            req.value = value
        if ttl is not None:
            req.ttl = ttl
        if priority is not None:
            req.priority = priority
        await self._call("update_domain_record_with_options", req, "aliyun_client.update_domain_record")

    async def delete_domain_record(self, record_id: str) -> None:
        from alibabacloud_alidns20150109 import models
        req = models.DeleteDomainRecordRequest(record_id=record_id)
        await self._call("delete_domain_record_with_options", req, "aliyun_client.delete_domain_record")


class ClientFactory:
    """按账户缓存 AlidnsClient；凭证变化（热更新后）自动重建。

    缓存键比较的是完整凭证 dict——AccessKeySecret 轮换会触发重建，
    无需额外失效通知。
    """

    def __init__(self, store: AccountStore):
        self._store = store
        self._cache: dict[str, AlidnsClient] = {}
        self._cache_creds: dict[str, dict] = {}

    def get(self, account_id: str) -> AlidnsClient:
        creds = self._store.get_credentials(account_id)
        if not creds:
            raise AlidnsError("account_not_found", f"account '{account_id}' not managed")
        if not creds["enabled"]:
            raise AlidnsError("account_disabled", f"account '{account_id}' disabled")
        cached = self._cache.get(account_id)
        if cached is not None and self._cache_creds.get(account_id) == creds:
            return cached
        client = AlidnsClient(creds)
        self._cache[account_id] = client
        self._cache_creds[account_id] = creds
        return client
