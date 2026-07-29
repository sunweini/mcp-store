> Source: https://gofastmcp.com/clients/transports

Clients
# Client Transports

Copy pageCopy page

Configure how clients connect to and communicate with MCP servers.

Copy pageCopy page

Transports handle the underlying connection between your client and MCP servers. While the client can automatically select a transport based on what you pass to it, instantiating transports explicitly gives you full control over configuration.

## [​

](#stdio-transport)STDIO Transport

STDIO transport communicates with MCP servers through subprocess pipes. When using STDIO, your client launches and manages the server process, controlling its lifecycle and environment.

STDIO servers inherit only a small allowlist of environment variables — just enough to locate an interpreter and a home directory. Anything else in your shell, including API keys and other credentials, does not reach the server unless you pass it through `env` explicitly.The allowlist is platform-specific. On POSIX systems it is `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, and `USER`; on Windows it is `APPDATA`, `HOMEDRIVE`, `HOMEPATH`, `LOCALAPPDATA`, `PATH`, `PATHEXT`, `PROCESSOR_ARCHITECTURE`, `SYSTEMDRIVE`, `SYSTEMROOT`, `TEMP`, `USERNAME`, and `USERPROFILE`.

```
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

transport = StdioTransport(
    command="python",
    args=["my_server.py", "--verbose"],
    env={"API_KEY": "secret", "LOG_LEVEL": "DEBUG"},
    cwd="/path/to/server"
)
client = Client(transport)

```

For convenience, the client can infer STDIO transport from file paths, though this limits configuration options:

```
from fastmcp import Client

client = Client("my_server.py")  # Limited - no configuration options

```

### [​

](#environment-variables)Environment Variables

Values you pass through `env` are merged on top of the inherited allowlist, so you add configuration rather than replacing the base environment. Anything your server needs beyond those six variables has to be listed explicitly.
**Selective forwarding** passes only the variables your server needs:

```
import os
from fastmcp.client.transports import StdioTransport

required_vars = ["API_KEY", "DATABASE_URL", "REDIS_HOST"]
env = {var: os.environ[var] for var in required_vars if var in os.environ}

transport = StdioTransport(command="python", args=["server.py"], env=env)
client = Client(transport)

```

**Loading from .env files** keeps configuration separate from code:

```
from dotenv import dotenv_values
from fastmcp.client.transports import StdioTransport

env = {
    key: value
    for key, value in dotenv_values(".env").items()
    if value is not None
}
transport = StdioTransport(command="python", args=["server.py"], env=env)
client = Client(transport)

```

### [​

](#session-persistence)Session Persistence

STDIO transports maintain sessions across multiple client contexts by default (`keep_alive=True`). This reuses the same subprocess for multiple connections, improving performance.

```
from fastmcp.client.transports import StdioTransport

transport = StdioTransport(command="python", args=["server.py"])
client = Client(transport)

async def efficient_multiple_operations():
    async with client:
        await client.list_tools()

    async with client:  # Reuses the same subprocess
        await client.call_tool("process_data", {"file": "data.csv"})

```

For complete isolation between connections, disable session persistence:

```
transport = StdioTransport(command="python", args=["server.py"], keep_alive=False)

```

## [​

](#http-transport)HTTP Transport

HTTP transport connects to MCP servers running as web services. This is the recommended transport for production deployments.

```
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport(
    url="https://api.example.com/mcp",
    headers={
        "Authorization": "Bearer your-token-here",
        "X-Custom-Header": "value"
    }
)
client = Client(transport)

```

FastMCP also provides authentication helpers:

```
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

client = Client(
    "https://api.example.com/mcp",
    auth=BearerAuth("your-token-here")
)

```

### [​

](#ssl-verification)SSL Verification

By default, HTTPS connections verify the server’s SSL certificate. You can customize this behavior with the `verify` parameter, which accepts the same values as httpx2 (documented in [httpx’s SSL guide](https://www.python-httpx.org/advanced/ssl/), which httpx2 follows):

```
from fastmcp import Client

# Disable SSL verification (e.g., for self-signed certs in development)
client = Client("https://dev-server.internal/mcp", verify=False)

# Use a custom CA bundle
client = Client("https://corp-server.internal/mcp", verify="/path/to/ca-bundle.pem")

# Use a custom SSL context for full control
import ssl
ctx = ssl.create_default_context()
ctx.load_verify_locations("/path/to/internal-ca.pem")
client = Client("https://corp-server.internal/mcp", verify=ctx)

```

The `verify` parameter is also available directly on `StreamableHttpTransport` and `SSETransport`:

```
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport(
    url="https://dev-server.internal/mcp",
    verify=False,
)
client = Client(transport)

```

### [​

](#sse-transport)SSE Transport

Server-Sent Events transport is maintained for backward compatibility. Use Streamable HTTP for new deployments unless you have specific infrastructure requirements.

```
from fastmcp.client.transports import SSETransport

transport = SSETransport(
    url="https://api.example.com/sse",
    headers={"Authorization": "Bearer token"}
)
client = Client(transport)

```

## [​

](#in-memory-transport)In-Memory Transport

In-memory transport connects directly to a FastMCP server instance within the same Python process. This eliminates both subprocess management and network overhead, making it ideal for testing.

```
from fastmcp import FastMCP, Client
import os

mcp = FastMCP("TestServer")

@mcp.tool
def greet(name: str) -> str:
    prefix = os.environ.get("GREETING_PREFIX", "Hello")
    return f"{prefix}, {name}!"

client = Client(mcp)

async with client:
    result = await client.call_tool("greet", {"name": "World"})

```

Unlike STDIO transports, in-memory servers share the same memory space and environment variables as your client code.

## [​

](#multi-server-configuration)Multi-Server Configuration

Connect to multiple servers defined in a configuration dictionary:

```
from fastmcp import Client

config = {
    "mcpServers": {
        "weather": {
            "url": "https://weather.example.com/mcp",
            "transport": "http"
        },
        "assistant": {
            "command": "python",
            "args": ["./assistant.py"],
            "env": {"LOG_LEVEL": "INFO"}
        }
    }
}

client = Client(config)

async with client:
    # Tools are namespaced by server
    weather = await client.call_tool("weather_get_forecast", {"city": "NYC"})
    answer = await client.call_tool("assistant_ask", {"question": "What?"})

```

### [​

](#tool-transformations)Tool Transformations

FastMCP supports tool transformations within the configuration. You can change names, descriptions, tags, and arguments for tools from a server.

```
config = {
    "mcpServers": {
        "weather": {
            "url": "https://weather.example.com/mcp",
            "transport": "http",
            "tools": {
                "weather_get_forecast": {
                    "name": "miami_weather",
                    "description": "Get the weather for Miami",
                    "arguments": {
                        "city": {
                            "default": "Miami",
                            "hide": True,
                        }
                    }
                }
            }
        }
    }
}

```

To filter tools by tag, use `include_tags` or `exclude_tags` at the server level:

```
config = {
    "mcpServers": {
        "weather": {
            "url": "https://weather.example.com/mcp",
            "include_tags": ["forecast"]  # Only tools with this tag
        }
    }
}

```
[Client-Only Package
Previous](/clients/client-only-package)[fastmcp-remote
Next](/clients/fastmcp-remote)⌘I