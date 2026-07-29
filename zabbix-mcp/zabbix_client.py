"""Zabbix JSON-RPC 2.0 API client.

Uses API Token auth (Zabbix 5.4+) — no user.login session needed.
Every API call creates an OTel span for distributed tracing.
Structured logging via structlog with trace context injection.
"""
from typing import Any
import time

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from telemetry import DEPENDENCY_DURATION, DEPENDENCY_ERRORS_TOTAL

logger = structlog.get_logger()
tracer = trace.get_tracer("zabbix_mcp.zabbix_client")


# NOTE: Zabbix severity integer → human-readable name mapping.
# Used by tool layer to present severity as both number and string.
SEVERITY_MAP = {
    0: "not_classified",
    1: "information",
    2: "warning",
    3: "average",
    4: "high",
    5: "disaster",
}


class ZabbixAPIError(Exception):
    """Zabbix API returned a JSON-RPC error (business logic error)."""


class ZabbixAuthError(Exception):
    """Authentication failed (401/403 or invalid API token)."""


class ZabbixConnectionError(Exception):
    """Network-level failure — Zabbix server unreachable."""


class ZabbixClient:
    """Zabbix JSON-RPC 2.0 API 客户端。

    使用 API Token 认证（Zabbix 5.4+），
    不依赖 user.login session，适配无状态 MCP 协议。
    """

    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self._url = url
        self._token = token
        self._timeout = timeout
        self._http = httpx.AsyncClient(timeout=timeout)
        self._request_id = 0

    async def call(self, method: str, params: dict) -> Any:
        """发起 Zabbix JSON-RPC 请求。

        每次调用创建独立 OTel Span (OBS-TRACE-001: HTTP Outbound 必追踪)。
        Span name 按 zabbix_client.{method} 命名 (OBS-TRACE-004)。
        不自动重试 — 写操作不幂等，读操作由 tool 层决定是否重试。
        """
        self._request_id += 1

        with tracer.start_as_current_span(f"zabbix_client.{method}") as span:
            span.set_attributes({
                "http.method": "POST",
                "http.url": self._url,
                "zabbix.method": method,
            })

            payload = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": self._request_id,
                "auth": self._token,
            }

            # Record dependency metrics
            start_time = time.monotonic()
            try:
                resp = await self._http.post(self._url, json=payload)
                resp.raise_for_status()
                data = resp.json()

                span.set_attribute("http.status_code", resp.status_code)

                # Record successful dependency duration
                duration = time.monotonic() - start_time
                if DEPENDENCY_DURATION:
                    DEPENDENCY_DURATION.record(
                        duration,
                        attributes={"dependency": "zabbix_api", "operation": method},
                    )

                if "error" in data:
                    err = data["error"]
                    err_msg = err.get("message", str(err))

                    # Record dependency error
                    if DEPENDENCY_ERRORS_TOTAL:
                        DEPENDENCY_ERRORS_TOTAL.add(
                            1,
                            attributes={"dependency": "zabbix_api", "error_type": "api_error"},
                        )

                    # OBS-TRACE-002: record_exception + SetStatus 同时使用
                    span.set_status(Status(StatusCode.ERROR, err_msg))
                    span.record_exception(ZabbixAPIError(err_msg))

                    # OBS-LOG-001: 结构化日志 key=value
                    # OBS-LOG-003: error key 必带
                    logger.error(
                        "zabbix_api_error",
                        service="zabbix-mcp",
                        zabbix_method=method,
                        error=err_msg,
                        zabbix_error_code=err.get("code"),
                    )
                    raise ZabbixAPIError(err_msg)

                return data.get("result")

            except ZabbixAPIError:
                # NOTE: re-raise ZabbixAPIError without wrapping it
                # as ZabbixConnectionError — they are distinct failure modes
                raise

            except httpx.HTTPError as e:
                # Record connection error
                if DEPENDENCY_ERRORS_TOTAL:
                    DEPENDENCY_ERRORS_TOTAL.add(
                        1,
                        attributes={"dependency": "zabbix_api", "error_type": "connection_error"},
                    )
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                logger.error(
                    "zabbix_connection_error",
                    service="zabbix-mcp",
                    zabbix_method=method,
                    error=str(e),
                )
                raise ZabbixConnectionError(str(e)) from e

    async def close(self) -> None:
        """关闭 httpx 连接池。"""
        if self._http:
            await self._http.aclose()
            self._http = None  # type: ignore[assignment]
