# 后端 MCP Server 禁用/停用设计

日期：2026-08-05
状态：待审阅
相关：`2026-07-30-mcp-gateway-design.md`、`gateway-proxy/registry.py`、`gateway-admin/api/servers.py`

## 背景

gateway 目前对所有注册 server 一律挂载并暴露。运维需要两种下线手段：

1. **禁用**：临时把某 server 从 gateway 清单移除（tools/list + call 不可用），**容器继续跑**，可随时恢复。
2. **停用**：把 server 从 gateway 清单移除，且**容器由运维手动停止**（释放资源）。恢复时手动起容器 + 启用。

两者在 gateway 行为相同（卸载），区别仅是状态标签（停用提示运维去停容器）。已确认：admin **不**获得 docker 控制权（不挂 docker.sock），停容器为人工操作。

## 需求

- 禁用后：该 server 的 tools/list 与 tools/call 均不在 gateway 清单、不可用
- 停用后：同上，且预期容器被手动停止
- 启用后：恢复挂载、可用
- 状态对运维可见（管理界面徽标）

## 状态模型

`servers:<name>` hash 增 `status` 字段：

| status | gateway 挂载 | tools/list + call | 容器 |
|---|---|---|---|
| `active`（默认） | 挂载 | 可用 | 跑 |
| `disabled`（禁用） | 卸载 | 不可用 | 继续跑 |
| `stopped`（停用） | 卸载 | 不可用 | 运维手动停 |

`servers:active` set 保持"已注册"语义（含所有状态），admin 列表仍可看到 disabled/stopped 以便恢复；挂载与否由 hash 的 `status` 决定。

## gateway-proxy 改动（registry.py）

- `mount_all`：遍历时读 `servers:<name>` 的 status，**仅 status==active 挂载**；非 active 跳过（日志记录）
- `watch_changes`：收到 `disable`/`stop` → `_unmount_one`；`add`/`update`/`enable` → 读 status，active 则 unmount+mount，否则 unmount
- 卸载后：tools/list 自动不含（FastMCP 只列已挂载 provider）；call 走现有 `UnknownServerError` 拒绝路径，无需额外拦截

## gateway-admin 改动（api/servers.py）

- 新端点 `POST /api/servers/{name}/lifecycle`，body `{action: disable|stop|enable}`（require_admin）：
  - disable → status=disabled
  - stop → status=stopped
  - enable → status=active
  - 写 hash + `_publish_change(action, name)`（复用现有 pubsub）
  - 不存在 → 404；action 非法 → 422
- `list_servers` 返回每行 `status`（默认 active 兼容旧数据）
- `create_server` 默认 status=active

## 前端（Servers.vue）

- 每行加状态徽标：active(绿)/disabled(黄)/stopped(灰)
- 操作按钮：active 时显示「禁用」「停用」；disabled/stopped 时显示「启用」
- 调 `POST /api/servers/{name}/lifecycle`，完成后刷新列表

## 错误处理

- lifecycle 对不存在 server → 404；非法 action → 422
- gateway watch_changes 单事件失败不影响热更新循环（现有 M1 隔离保留）
- 停用后若运维忘了停容器：gateway 已卸载，server 仍不可用（安全），仅资源未释放

## 测试

- registry：mount_all 只挂 active；watch_changes disable/stop→unmount、enable→mount
- servers.py：lifecycle 三动作改 status + publish；404/422；list 返回 status
- 现有 servers 测试无回归（status 默认 active 兼容）

## 部署影响

- 重建 gateway-proxy + gateway-admin
- 无 schema/配置变更（status 字段为 hash 内新增，旧数据默认 active）

## 非目标

- admin 不控制 docker（不挂 docker.sock），停/起容器为人工
- 不做定时自动停用/配额联动停用
- 不改 token 权限模型（禁用是 server 级，与 token 正交）
