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
    error_type: 拒绝原因（invalid_token / invalid_target / unknown_mode /
        permission_denied），allowed=True 时为 None。
    server / tool: 从 mcp_name 解析出的目标。授权与审计共享这份解析结果，
        避免审计再各自调 resolve_target。
    mode: 该工具的读写分类（"read"/"write"）；**mode 未知时为 None**
        （fail-closed：未知即拒绝，不再伪装成 read）。审计 op 用
        "unknown" 如实呈现。
    """

    allowed: bool
    error_type: str | None = None
    server: str = ""
    tool: str = ""
    mode: str | None = None


# error_type 枚举：invalid_token（无 token）/ invalid_target（名字无法解析）/
# unknown_mode（名字可解析但该工具的 mode 未注册）/ permission_denied（有
# token 但无该 server+mode 权限）。
INVALID_TOKEN = "invalid_token"
INVALID_TARGET = "invalid_target"
UNKNOWN_MODE = "unknown_mode"
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

    返回 AuthResult。**fail-closed**：tool_modes 缺失该 server/tool（mode 未知）
    时直接拒绝（UNKNOWN_MODE），不再退化成默认 read 放行——未知 write 工具
    被只读 token 访问正是历史越权 bug 的路径。
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
    if mode is None:
        # fail-closed：mode 未知（registry 未含该 server/tool）→ 拒绝。
        # server/tool 仍解析出，供审计定位"哪个工具的 mode 未同步"。
        return AuthResult(False, UNKNOWN_MODE, server, tool, None)

    granted = permissions.get(server, {})
    if not granted.get(mode, False):
        return AuthResult(False, PERMISSION_DENIED, server, tool, mode)
    return AuthResult(True, None, server, tool, mode)


def _lookup_mode(tool_modes: dict[str, dict[str, str]], server: str, tool: str) -> str | None:
    """从注入的 tool_modes 查该工具的 mode；未知返回 None（fail-closed）。

    为什么返回 None 而非默认 read：历史默认 read 是 fail-open——mode 元数据
    缺失时把未知工具当 read 判定，只读 token 即可越权访问未知 write 工具。
    mode 权威由 registry 层保证（introspect 非空才注册），授权只消费注入
    数据并对"未知"诚实拒绝。
    """
    return tool_modes.get(server, {}).get(tool)


def get_tool_modes() -> dict[str, dict[str, str]]:
    """把 TOOL_REGISTRY 快照成 {server: {tool: mode}}，供调用方注入 authorize。

    授权模块读取授权依据（当前已知的 mode）的边界；authorize 本身不读全局。
    """
    from routing import TOOL_REGISTRY

    return {server: dict(modes) for server, modes in TOOL_REGISTRY.items()}
