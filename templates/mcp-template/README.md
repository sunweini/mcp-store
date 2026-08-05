# {{MCP_NAME}}

<!-- 一句话说明这个 MCP 是什么 -->

## 功能

| Tool | 说明 |
|---|---|
| | |

| Resource | 说明 |
|---|---|
| | |

| Prompt | 说明 |
|---|---|
| | |

## 快速开始

### 作为 MCP Client 连接

```json
{
  "mcpServers": {
    "{{mcp-name}}": {
      "url": "http://localhost:<登记端口>/mcp"
    }
  }
}
```

### 从源码运行

```bash
git clone <repo>
cd <mcp-name>
uv sync
uv run python server.py
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| | | |

## 协议

基于 MCP `2026-07-28` specification，stateless HTTP transport。
