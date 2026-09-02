"""授权判定（authorize seam）：名字 → mode → grant，纯函数、数据注入。

设计（ADR-0001）：授权是一个独立概念，不归 middleware（审计构造），也不混在
auth.py（token 存取）。本模块持有授权判定逻辑，mode 作为数据注入（`tool_modes`），
不读任何全局——授权判定因此可被纯 dict 直接测试，不受 TOOL_REGISTRY 全局状态污染。

mode 权威在 `routing.TOOL_REGISTRY`（由 registry._introspect_tools 读后端
annotations.destructiveHint 填充）。`get_tool_modes()` 把该全局快照成 dict 供
调用方注入；向下的 authorize 只消费注入数据。
"""
from dataclasses import dataclass
from typing import Any

# NOTE: split_prefix 是纯前缀切分（不查 registry）——即便 server 未注册
# （禁用后 registry 卸载）也能解析出 server/tool，保证审计字段不落空。
from routing import split_prefix


@dataclass
class AuthResult:
    """一次授权判定的结果。

    allowed: 是否放行。
    error_type: 拒绝原因（invalid_token / invalid_target / permission_denied），
        allowed=True 时为 None。
    server / tool / mode: 从 mcp_name 解析出的目标。授权与审计共享这份解析
        结果，避免审计再各自调 resolve_target。
    """

    allowed: bool
    error_type: str | None = None
    server: str = ""
    tool: str = ""
    mode: str = "read"


# error_type 枚举：invalid_token（无 token）/ invalid_target（名字无法解析）/
# permission_denied（有 token 但无该 server+mode 权限）。
INVALID_TOKEN = "invalid_token"
INVALID_TARGET = "invalid_target"
PERMISSION_DENIED = "permission_denied"


def authorize(
    permissions: dict[str, dict[str, bool]] | None,
    mcp_name: str,
    tool_modes: dict[str, dict[str, str]],
) -> AuthResult:
    """判定一个命名的工具是否被该 token 授权，纯函数。

    permissions: token 的授权矩阵 {server: {read, write}}（None 表示无有效 token）。
    mcp_name: 命名空间前缀的工具名，如 "zabbix-mcp_create_maintenance"。
    tool_modes: 当前已知 {server: {tool: mode}}（mode = "read"/"write"），数据注入。

    返回 AuthResult。mode 从 tool_modes 查询；tool_modes 缺失该 server 时
    mode 默认 "read"（保持审计 op 不因 registry 缺失落空），此时是否放行由
    permissions[server]["read"] 决定。
    """
    # 无有效 token（None / 空 permissions）——区分"调用方没带 token"。
    if permissions is None:
        return AuthResult(False, INVALID_TOKEN)

    # 名字解析：split_prefix 纯切分（server/tool）。失败 = 工具名畸形或
    # 无命名空间前缀，属调用方错误，非权限问题。
    try:
        server, tool = split_prefix(mcp_name)
    except ValueError:
        return AuthResult(False, INVALID_TARGET)

    mode = _lookup_mode(tool_modes, server, tool)
    # mode 默认 read（未注册/未知时，与历史审计 op 降级一致）。
    granted = permissions.get(server, {})
    if not granted.get(mode, False):
        return AuthResult(False, PERMISSION_DENIED, server, tool, mode)
    return AuthResult(True, None, server, tool, mode)


def _lookup_mode(tool_modes: dict[str, dict[str, str]], server: str, tool: str) -> str:
    """从注入的 tool_modes 查该工具的 mode；缺失时默认 'read'。

    为什么默认 read 而非抛错：授权判定对"未知工具"退化到 read 是历史语义
    （审计 op、list 可见性都与之一致）；mode 权威缺失属于 registry 状态异常，
    由 registry 层保证（非空才注册），授权只消费注入数据。
    """
    return tool_modes.get(server, {}).get(tool, "read")


def get_tool_modes() -> dict[str, dict[str, str]]:
    """把 TOOL_REGISTRY 快照成 {server: {tool: mode}}，供调用方注入 authorize。

    授权模块读取授权依据（当前已知的 mode）的边界；authorize 本身不读全局。
    """
    from routing import TOOL_REGISTRY

    return {server: dict(modes) for server, modes in TOOL_REGISTRY.items()}
