# Zabbix MCP

Zabbix 监控系统的 MCP server，为 AI agent 提供告警巡检、维护期管理和告警确认能力。

## 功能

| Tool | 类型 | 说明 |
|---|---|---|
| `list_active_problems` | 读 | 查询活跃告警（按时间降序） |
| `problem_summary` | 读 | 告警摘要报告 |
| `list_maintenances` | 读 | 查看维护期列表 |
| `list_unacknowledged` | 读 | 查未确认告警 |
| `create_maintenance` | ⚠️ 写 | 创建维护期（含周期性） |
| `delete_maintenance` | ⚠️ 写 | 删除/结束维护期 |
| `acknowledge_event` | ⚠️ 写 | 确认单条告警 |
| `batch_acknowledge` | ⚠️ 写 | 批量确认告警 |

## 快速开始

### 连接配置

```json
{
  "mcpServers": {
    "zabbix": {
      "url": "http://localhost:9053/mcp"
    }
  }
}
```

### 环境变量

```bash
export ZABBIX_URL="http://your-zabbix/api_jsonrpc.php"
export ZABBIX_TOKEN="your-api-token"
```

### 从源码运行

```bash
git clone <repo>
cd zabbix-mcp
uv sync
export ZABBIX_URL="..." ZABBIX_TOKEN="..."
uv run python server.py
```

## 协议

基于 MCP `2026-07-28` specification，stateless HTTP transport。
Zabbix 版本：6.4+（API Token 认证）。
