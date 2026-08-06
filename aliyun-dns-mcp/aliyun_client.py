"""Alidns SDK 封装：每账户一个 client，同步 SDK 走 asyncio.to_thread。

用官方 SDK（alibabacloud-alidns20150109 + tea-openapi）而不是裸 HTTP：
RPC 签名/端点选择/错误对象解析交给 SDK，MCP 层只做错误分类与 trace。
SDK 是同步 API，异步工具里用 asyncio.to_thread 防阻塞 event loop。

安全：SDK RPC 请求 URL query 含 AccessKeyId。两层防线：
1. logging_config 把可能打印请求日志的库 logger（httpx/requests/urllib3/
   aiohttp）整体提到 WARNING；
2. 网络异常消息可能自带完整 URL（requests ConnectionError 消息含
   "GET https://.../?AccessKeyId=<明文>&Signature=..."）——_call 在
   分类为 network_error 时剥离 query，凭证永不进日志/工具响应
   （spec §8.1 敏感防线，I1）。
日志只记 account_id 不记凭证。
"""
from __future__ import annotations  # ClientFactory.__init__ 注解引用 AccountStore（仅类型），不引入运行时 import

import asyncio
import time
import urllib3.exceptions

import structlog
from opentelemetry import trace

# CRITICAL: 运行时模块访问（同 tools/__init__.py 的坑）——模块加载时 telemetry
# 可能尚未 init，from-import 会把指标绑定为 None；运行时取值免疫 import 顺序。
import telemetry

logger = structlog.get_logger()
tracer = trace.get_tracer("aliyun-dns-mcp")

ALIDNS_ENDPOINT = "alidns.cn-hangzhou.aliyuncs.com"

# 网络层异常基类集合（classify_error 用）——requests/urllib3/aiohttp 是 SDK
# 传递依赖（锁文件确认存在），延迟 import 避免模块加载即需要它们。
# urllib3 已在本模块显式 import：其异常在 requests/aiohttp 包装前往往
# 直接抛出（urllib3.exceptions.ConnectionError 等均继承 HTTPError，取基类
# 一网打尽），是最底层证据，必须可判定。
_NETWORK_EXC_TYPES = (
    TimeoutError,
    ConnectionError,
    urllib3.exceptions.HTTPError,
)


def _looks_like_network_exception(exc: Exception) -> bool:
    """判定异常是否来自网络层（模块加载后补测可导入的类型）。

    返回 True 则消息可能含完整 URL query（AccessKeyId 明文），必须走
    剥离逻辑。延迟探测而不是模块顶层 import requests：requests 是 SDK
    传递依赖，间接 import 会让本模块硬依赖它，破坏可测性（单测用假
    SDK 对象，环境未装 requests 时也应可运行）。
    """
    if isinstance(exc, _NETWORK_EXC_TYPES):
        return True
    # requests 异常类（requests.exceptions.*，含 ConnectionError/Timeout 等）
    # 与 aiohttp 异常（aiohttp.ClientError 族）延迟 import 判定——仅在有
    # 具体异常实例时才 import，正常路径零成本。
    for mod_name in ("requests.exceptions", "aiohttp"):
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except ImportError:
            continue
        if isinstance(exc, mod.RequestException if mod_name == "requests.exceptions" else mod.ClientError):
            return True
    return False


def _redact_network_message(exc: Exception) -> str:
    """网络异常 → 无凭证的安全消息：异常类型名 + 截断到 query 之前。

    为什么截断到 "?"：requests ConnectionError 消息格式为
    "HTTPSConnectionPool(host='...', port=443): Max retries exceeded ...: "
    "GET https://alidns.cn-hangzhou.aliyuncs.com/?AccessKeyId=<明文>&Signature=..."，
    "?" 之后的整段 query 只有签名参数，没有任何诊断价值——URL 主机名保留
    （无敏感信息），query 一律丢弃（不白名单参数名，防未来新增参数漏判）。
    """
    head = str(exc).split("?", 1)[0].strip()
    return f"{type(exc).__name__}: {head}" if head else type(exc).__name__


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
    # 网络层异常优先判定：请求根本没到达阿里云，任何文本匹配都无意义，
    # 且其消息可能含完整 URL query（AccessKeyId 明文，spec §8.1 敏感防线）
    if _looks_like_network_exception(exc):
        return "network_error"
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
        from darabonba.runtime import RuntimeOptions
        self._runtime = RuntimeOptions()
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

    async def _call(self, api_name: str, request, span_name: str, _retried: bool = False):
        def run():
            fn = getattr(self._sdk, api_name)
            # with_options 签名 (request, runtime) 第二个参数必传——缺它
            # 直接 TypeError（端到端验证实测：假凭证调用走到 SDK 层才发现）；
            # runtime 默认空对象即可（重试/超时用默认值），统一在公共入口
            # 注入，各工具方法不重复传
            return fn(request, self._runtime)

        # record_exception/set_status_on_exception=False：OTel SDK 默认在
        # with 块未捕获异常退出时自动 record_exception——自动事件的 stacktrace
        # 含 `raise ... from exc` 原始异常链（ConnectionError 消息带完整 URL
        # query），绕过剥离防线。本模块手动记录事件与状态（下方异常路径），
        # 关闭自动机制保证凭证防线唯一入口（I1 残余）。
        with tracer.start_as_current_span(span_name, record_exception=False,
                                          set_status_on_exception=False) as span:
            span.set_attribute("operation.type", "aliyun_api")
            start = time.monotonic()
            try:
                return await asyncio.to_thread(run)
            except Exception as exc:
                err_type = classify_error(exc)
                # I2（spec §7.1「THROTTLED 短退避重试 1 次」）：限流是瞬时
                # 状态（QPS 窗口），SDK 自身不带重试（RuntimeOptions 默认空），
                # 公共入口统一退避一次覆盖全部工具方法。_retried 防死循环
                # （重试后仍限流 → 直接报错，留给调用方/用户决策）。
                if err_type == "throttled" and not _retried:
                    await asyncio.sleep(1)
                    return await self._call(api_name, request, span_name, _retried=True)
                # OBS-MET-001: 依赖指标——失败记 dependency_errors_total
                dep_errors = telemetry.DEPENDENCY_ERRORS_TOTAL
                if dep_errors:
                    dep_errors.add(1, attributes={"dependency": "alidns_api", "error_type": err_type})
                # network_error 消息可能含完整 URL query（AccessKeyId 明文）：
                # span 描述/log/工具响应共用剥离后的安全消息（spec §8.1）
                safe_msg = _redact_network_message(exc) if err_type == "network_error" else str(exc)
                if err_type == "network_error":
                    # 不能用 record_exception：OTel 会把 str(exc) 全文写进 span
                    # event 的 exception.message，且 stacktrace 首行同样含完整
                    # URL（明文落 stdout console exporter）——手动 add_event 只
                    # 写剥离后的安全消息，零凭证（I1 残余）
                    span.add_event("exception", {
                        "exception.type": type(exc).__name__,
                        "exception.message": safe_msg,
                    })
                else:
                    span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR, safe_msg))
                logger.error("aliyun_api_error", service="aliyun-dns-mcp",
                             api=api_name, error_type=err_type, error=safe_msg)
                raise AlidnsError(err_type, safe_msg, getattr(exc, "request_id", None)) from exc
            finally:
                # 成功/失败都记延迟（histogram 带 status 区分）
                duration = time.monotonic() - start
                dep_duration = telemetry.DEPENDENCY_DURATION
                if dep_duration:
                    dep_duration.record(duration, attributes={"dependency": "alidns_api", "api": api_name})

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
