> Source: https://gofastmcp.com/servers/server

Servers
# The FastMCP Server

Copy pageCopy page

The core FastMCP server class for building MCP applications

Copy pageCopy page
The `FastMCP` class is the central piece of every FastMCP application. It acts as the container for your tools, resources, and prompts, managing communication with MCP clients and orchestrating the entire server lifecycle.

## [​

](#creating-a-server)Creating a Server

At its simplest, a FastMCP server just needs a name. Everything else has sensible defaults.

```
from fastmcp import FastMCP

mcp = FastMCP("MyServer")

```

Instructions help clients (and the LLMs behind them) understand what your server does and how to use it effectively.

```
mcp = FastMCP(
    "DataAnalysis",
    instructions="Provides tools for analyzing numerical datasets. Start with get_summary() for an overview.",
)

```

## [​

](#components)Components

FastMCP servers expose three types of components to clients, each serving a distinct role in the MCP protocol.
**Tools** are functions that clients invoke to perform actions or access external systems.

```
@mcp.tool
def multiply(a: float, b: float) -> float:
    """Multiplies two numbers together."""
    return a * b

```

**Resources** expose data that clients can read — passive data sources rather than invocable functions.

```
@mcp.resource("data://config")
def get_config() -> dict:
    return {"theme": "dark", "version": "1.0"}

```

**Prompts** are reusable message templates that guide LLM interactions.

```
@mcp.prompt
def analyze_data(data_points: list[float]) -> str:
    formatted_data = ", ".join(str(point) for point in data_points)
    return f"Please analyze these data points: {formatted_data}"

```

Each component type has detailed documentation: [Tools](/servers/tools), [Resources](/servers/resources) (including [Resource Templates](/servers/resources#resource-templates)), and [Prompts](/servers/prompts).

## [​

](#running-the-server)Running the Server

Start your server by calling `mcp.run()`. The `if __name__` guard ensures compatibility with MCP clients that launch your server as a subprocess.

```
from fastmcp import FastMCP

mcp = FastMCP("MyServer")

@mcp.tool
def greet(name: str) -> str:
    """Greet a user by name."""
    return f"Hello, {name}!"

if __name__ == "__main__":
    mcp.run()

```

FastMCP supports several transports:

- **STDIO** (default): For local integrations and CLI tools

- **HTTP**: For web services using the Streamable HTTP protocol

- **SSE**: Legacy web transport (deprecated)

```
# Run with HTTP transport
mcp.run(transport="http", host="127.0.0.1", port=9000)

```

The server can also be run using the FastMCP CLI. For detailed information on transports and deployment, see [Running Your Server](/deployment/running-server).

## [​

](#configuration-reference)Configuration Reference

The `FastMCP` constructor accepts parameters organized into four categories: identity, composition, behavior, and handlers.

### [​

](#identity)Identity

These parameters control how your server presents itself to clients.
[​

](#param-name)namestr | Nonedefault:"None"A human-readable name for your server, shown in client applications and logs. If omitted, FastMCP generates a random name[​

](#param-instructions)instructionsstr | NoneDescription of how to interact with this server. Clients surface these instructions to help LLMs understand the server’s purpose and available functionality[​

](#param-version)versionstr | int | float | NoneVersion string for your server. Defaults to the FastMCP library version if not provided[​

](#param-website-url)website_urlstr | NoneURL to a website with more information about your server. Displayed in client applications[​

](#param-icons)iconslist[Icon] | NoneList of icon representations for your server. See [Icons](/servers/icons) for details[​

](#param-experimental-capabilities)experimental_capabilitiesdict[str, dict[str, Any]] | NoneArbitrary experimental capabilities to advertise in the MCP `initialize` response. Use this to declare cross-server interop conventions or draft extensions that follow the MCP spec’s `experimental` field. Keys are capability names; values are free-form dicts. FastMCP’s built-in derived capabilities (`tools`, `resources`, etc.) are unaffected — this only populates `capabilities.experimental`

### [​

](#composition)Composition

These parameters control what your server is built from — its components, middleware, providers, and lifecycle.
[​

](#param-tools)toolsSequence[Tool | Callable] | NoneTools to register on the server. An alternative to the `@mcp.tool` decorator when you need to add tools programmatically[​

](#param-auth)authAuthProvider | NoneAuthentication provider for securing HTTP-based transports. See [Authentication](/servers/auth/authentication) for configuration[​

](#param-middleware)middlewareSequence[Middleware] | None[Middleware](/servers/middleware) that intercepts and transforms every MCP message flowing through the server — requests, responses, and notifications in both directions. Use for cross-cutting concerns like logging, error handling, and rate limiting[​

](#param-providers)providersSequence[Provider] | None[Providers](/servers/providers/overview) that supply tools, resources, and prompts dynamically. Providers are queried at request time, so they can serve components from databases, APIs, or other external sources[​

](#param-transforms)transformsSequence[Transform] | NoneServer-level [transforms](/servers/transforms/transforms) to apply to all components. Transforms modify how tools, resources, and prompts are presented to clients — for example, [search transforms](/servers/transforms/tool-search) replace large catalogs with on-demand discovery[​

](#param-lifespan)lifespanLifespan | LifespanCallable | NoneServer-level setup and teardown logic that runs when the server starts and stops. See [Lifespans](/servers/lifespan) for composable lifespans

### [​

](#behavior)Behavior

These parameters tune how the server processes requests and communicates with clients.
[​

](#param-on-duplicate)on_duplicateLiteral["warn", "error", "replace", "ignore"]default:"warn"How to handle duplicate component registrations[​

](#param-strict-input-validation)strict_input_validationbooldefault:"False"When `False` (default), FastMCP uses Pydantic’s flexible validation that coerces compatible inputs (e.g., `"10"` → `10` for int parameters). When `True`, validates inputs against the exact JSON Schema before calling your function, rejecting type mismatches. See [Validation Modes](/servers/tools#validation-modes) for details[​

](#param-mask-error-details)mask_error_detailsbool | NoneWhen `True`, replaces internal error details in tool/resource responses with a generic message to avoid leaking implementation details to clients. Defaults to the `FASTMCP_MASK_ERROR_DETAILS` environment variable[​

](#param-list-page-size)list_page_sizeint | Nonedefault:"None"Maximum items per page for list operations (`tools/list`, `resources/list`, etc.). Must be a positive integer when set. When `None`, all results are returned in a single response. See [Pagination](/servers/pagination) for details[​

](#param-tasks)tasksbool | Nonedefault:"False"Enable background task support. When `True`, tools and resources can return `CreateTaskResult` to run work asynchronously while the client polls for results[​

](#param-client-log-level)client_log_levelLoggingLevel | NoneDefault minimum log level for messages sent to MCP clients via `context.log()`. When set, messages below this level are suppressed. Handshake-era clients can override this per-session using the MCP `logging/setLevel` request; the modern protocol has no session to hold that level, so clients on it filter by level in their own log handler instead. One of `"debug"`, `"info"`, `"notice"`, `"warning"`, `"error"`, `"critical"`, `"alert"`, or `"emergency"`[​

](#param-dereference-schemas)dereference_schemasbooldefault:"True"Automatically dereference `$ref` pointers in JSON schemas generated from complex Pydantic models. Most clients require flat schemas without `$ref`, so this should usually stay enabled[​

](#param-cache-ttl)cache_ttlint | Nonedefault:"None"How long, in seconds, a client may treat this server’s cacheable responses as fresh (SEP-2549). When set, the hint applies uniformly to `tools/list`, `prompts/list`, `resources/list`, `resources/templates/list`, and `resources/read`. Clients must opt into caching to honor it — see [Response caching](/clients/client#response-caching). Must be a positive integer[​

](#param-cache-scope)cache_scopeLiteral["public", "private"] | Nonedefault:"None"Whether a cached response may be shared across authorization contexts (`"public"`) or reused only within the one that produced it (`"private"`, the default when a `cache_ttl` is set). Requires `cache_ttl`

### [​

](#storage)Storage

[​

](#param-session-state-store)session_state_storeAsyncKeyValue | NonePersistent key-value store for session state that survives across requests. Defaults to an in-memory store. Provide a custom implementation for persistence across server restarts

## [​

](#response-caching)Response Caching

A server whose listings and resource reads change slowly can tell clients how long they may reuse a response before fetching it again (SEP-2549). Set `cache_ttl` (seconds) on the server, and the hint is attached uniformly to every cacheable response — `tools/list`, `prompts/list`, `resources/list`, `resources/templates/list`, and `resources/read`.

```
from fastmcp import FastMCP

mcp = FastMCP("Weather", cache_ttl=300, cache_scope="public")

@mcp.tool
def forecast(city: str) -> str:
    return f"Sunny in {city}"

```

`cache_scope` controls whether a cached response may be shared across authorization contexts (`"public"`) or reused only within the one that produced it (`"private"`, the default when a TTL is set). A `cache_scope` without a `cache_ttl` does not enable caching and raises at construction.
The hint is inert on its own: a client only reuses a response if it opts into caching and negotiates the modern protocol. See [Response caching](/clients/client#response-caching) for the client side.

## [​

](#tag-based-filtering)Tag-Based Filtering

Tags let you categorize components and selectively expose them. This is useful for creating different views of your server for different environments or user types.

```
@mcp.tool(tags={"public", "utility"})
def public_tool() -> str:
    return "This tool is public"

@mcp.tool(tags={"internal", "admin"})
def admin_tool() -> str:
    return "This tool is for admins only"

```

The filtering logic works as follows:

- **Enable with `only=True`**: Switches to allowlist mode — only components with at least one matching tag are exposed

- **Disable**: Components with any matching tag are hidden

- **Precedence**: Later calls override earlier ones, so call `disable` after `enable` to exclude from an allowlist

To hide a component by default, disable it at the server level with `mcp.disable(names={"admin_tool"})`. This is a default rather than a guarantee — a later `enable()` call or a per-session visibility rule can bring the component back. When something must never be reachable, leave it unregistered or guard it with [authentication](/servers/auth/authentication) instead of relying on visibility.

```
# Only expose components tagged with "public"
mcp = FastMCP()
mcp.enable(tags={"public"}, only=True)

# Hide components tagged as "internal" or "deprecated"
mcp = FastMCP()
mcp.disable(tags={"internal", "deprecated"})

# Combine both: show admin tools but hide deprecated ones
mcp = FastMCP()
mcp.enable(tags={"admin"}, only=True).disable(tags={"deprecated"})

```

This filtering applies to all component types (tools, resources, resource templates, and prompts) and affects both listing and access.

## [​

](#custom-routes)Custom Routes

When running with HTTP transport, you can add custom web routes alongside your MCP endpoint using the `@custom_route` decorator.

```
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

mcp = FastMCP("MyServer")

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")

if __name__ == "__main__":
    mcp.run(transport="http")  # Health check at http://localhost:8000/health

```

Custom routes are useful for health checks, status endpoints, and simple webhooks. For more complex web applications, consider [mounting your MCP server into a FastAPI or Starlette app](/deployment/http#integration-with-web-frameworks).[What's New in FastMCP 4
Previous](/getting-started/whats-new)[Tools
Next](/servers/tools)⌘I