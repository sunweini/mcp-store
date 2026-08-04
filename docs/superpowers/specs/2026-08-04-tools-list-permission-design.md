# tools/list 权限过滤设计（gateway-proxy）

日期：2026-08-04
状态：待审阅
相关：`2026-07-30-mcp-gateway-design.md`（gateway 架构）、`permission_middleware.py`（现有 tools/call 鉴权）

## 背景

gateway-proxy 现有权限体系只拦 `tools/call`：token 未授权的 server 调用被拒（permission_denied），但 `tools/list` 对任何请求（含匿名/无效 token）返回全部工具清单。

问题：
1. **信息泄露**——未授权客户端能看到全部工具名与描述
2. **体验噪音**——read-only token 的 AI 客户端会看到并尝试调用无权限的写工具（被 tools/call 拒绝，白跑一次）
3. **文档与现实脱节**——`permission_middleware.py` docstring 声称"gateway returns an empty tool list for unauthenticated clients"，实际未实现

目标：tools/list 按 token 权限**动态过滤**，清单只包含该 token 有权调用的工具。

## 需求（已确认）

1. **匿名/无效 token** → tools/list 返回**空清单**（不报错）
2. **工具级过滤**：token 对某工具可见 ⇔ token 对该工具的 (server, mode) 有权限
   - token 只 read 某 server → 只见该 server 的**读工具**（写工具不可见）
   - token 只 write → 只见**写工具**
   - read+write → 全见
   - 多 server 各自过滤后合并
3. **tools/call 行为不变**（本次不动调用拦截逻辑）

## 架构决策

- **实现位置：PermissionMiddleware 加 `on_list_tools` hook**（方案 A）。FastMCP v4 middleware 原生支持（knowledge-base 19-middleware.md），返回 `list[Tool]` 可过滤。
- 备选方案 B（独立 middleware）否：token 验证逻辑重复。方案 C（on_message 全方法鉴权）否：伤及 ping/initialize/探活，超范围。
- **零新权限逻辑**——纯组合现有组件：`_extract_token`、`verify_token`、`resolve_target`、`check_permission`。

## 实现

### PermissionMiddleware.on_list_tools

```python
async def on_list_tools(self, context, call_next):
    """Filter tools/list by token permissions.

    Anonymous/invalid token -> empty list. Otherwise only tools whose
    (server, mode) the token grants are visible. Tool-level granularity:
    a read-only token never sees write tools.
    """
    tools = await call_next(context)
    token = _extract_token(get_http_headers())
    token_info = await verify_token(token) if token else None
    if token_info is None:
        return []
    visible = []
    for t in tools:
        try:
            server, _tool, mode = resolve_target(t.name)
        except UnknownServerError:
            continue  # 未注册前缀：不确定来源，安全默认不列出
        if check_permission(token_info, server, mode):
            visible.append(t)
    return visible
```

### 判定语义

| token 配置 | tools/list 结果 |
|---|---|
| 无 / 无效 | 空清单 |
| server 只 read | 该 server 的读工具 |
| server 只 write | 该 server 的写工具 |
| server read+write | 该 server 全部工具 |
| 空 permissions | 空清单 |
| 多 server 混合 | 各 server 过滤后合并 |

### 关键点

1. **mode 数据源**：gateway 启动时 `registry.py` 探活后端 tools/list，按 `annotations.destructiveHint` 分类每工具 read/write 存 TOOL_REGISTRY。过滤与 tools/call 判定**同源**，不会出现列表可见性与调用权限语义分裂。
2. **未注册前缀**：`resolve_target` 抛 `UnknownServerError` → 该工具跳过不列出（安全默认）。
3. **性能**：每次 tools/list 一次 Redis token 查询（与 tools/call 同等开销），工具数量级 <100，遍历过滤可忽略。
4. **docstring 对齐**：permission_middleware.py 模块与类 docstring 中"Non-tools/call requests pass through untouched"/"Only tools/call is intercepted"表述更新为"tools/list filtered by token permissions"。
5. **新增 import**：`from auth import verify_token, check_permission`（verify_token 已导入，补 check_permission）+ `from routing import resolve_target, UnknownServerError`。
6. **ping/initialize 不动**——保持匿名可探活（gateway 健康检查依赖）。

## 错误处理

- token 验证 Redis 异常 → verify_token 现有行为（返回 None → 空清单），不抛给客户端
- resolve_target 异常 → 跳过单工具，不影响其余

## 测试（gateway-proxy/tests）

- 匿名（无 Authorization）→ 空清单
- 无效 token → 空清单
- zabbix 只 read → 只见 zabbix 读工具；**断言写工具缺席**
- zabbix read+write → 全见
- zabbix 只 write → 只见写工具
- 多 server 混合权限 → 合并正确
- 未注册前缀工具 → 跳过不列
- tools/call 回归：越权调用仍 permission_denied（现有测试保持通过）

## 部署影响

- 只改 `gateway-proxy/`（permission_middleware.py + tests）
- 正式环境：重建 gateway-proxy 容器即生效；admin/前端/4 个 MCP 零改动
- 无 schema/配置变更

## 非目标

- 不改 tools/call 拦截逻辑
- 不做 resources/prompts 过滤（当前后端无这两类组件）
- 不做匿名 401 报错（选择空清单，避免 client 断连/重试风暴）
- 不做每工具独立权限（权限粒度保持 server 级 read/write）
