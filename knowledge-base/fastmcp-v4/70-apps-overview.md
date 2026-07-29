> Source: https://gofastmcp.com/apps/overview

Apps
# Apps

Copy pageCopy page

Give your tools interactive UIs rendered directly in the conversation.

Copy pageCopy page

A FastMCP app is a tool that returns an interactive UI instead of text. When the host calls it, the user sees a chart, a table, a form, or a whole dashboard rendered right inside the conversation, with working sort, search, tooltips, and state.

The dashboard above is a [Prefab](https://prefab.prefect.io) showcase — a taste of what you can deliver from a FastMCP tool. Every card, chart, slider, dialog, and carousel is a Python component. Build a composition like this, add `@mcp.tool(app=True)`, and the host renders it inside the conversation.
Under the hood, FastMCP builds on the [MCP Apps extension](https://modelcontextprotocol.io/docs/extensions/apps) and uses Prefab to describe UIs in Python.

```
pip install "fastmcp[apps]"

```

[Prefab](https://prefab.prefect.io) is under active development with frequent breaking changes. FastMCP sets a minimum `prefab-ui` version but does not pin an upper bound — **pin `prefab-ui` to a specific version in your own dependencies** before deploying.

## [​

](#pick-your-path)Pick your path

Four patterns cover almost everything you’d want to build. Most apps start with Interactive Tools; you only reach for the others when you’ve hit a specific limit.

### [​

](#interactive-tools-—-start-here)[Interactive Tools](/apps/prefab) — start here

Add `app=True` to a tool and return a Prefab component. Charts, tables, dashboards, and client-side interactivity (toggles, tabs, filtering) all work without any server round-trips.

```
@mcp.tool(app=True)
def team_directory() -> DataTable:
    return DataTable(columns=[...], rows=employees, search=True)

```

### [​

](#fastmcpapp-—-when-the-ui-calls-back-to-the-server)[FastMCPApp](/apps/fastmcp-app) — when the UI calls back to the server

Forms that save data, buttons that trigger backend work, search that hits a database. `FastMCPApp` manages the wiring between UI actions and backend tools, with stable tool identifiers that survive server composition.

### [​

](#generative-ui-—-when-the-llm-writes-the-ui)[Generative UI](/apps/generative) — when the LLM writes the UI

Register one provider and the model can write Prefab code tailored to the current data and request. The user watches the UI build up as the model generates it.

```
mcp.add_provider(GenerativeUI())

```

### [​

](#custom-html-—-when-you-need-full-control)[Custom HTML](/apps/low-level) — when you need full control

Write your own HTML, CSS, and JavaScript. Use a specific framework, drop in a map or 3D viewer, embed video. You’re talking to the MCP Apps protocol directly.

## [​

](#what’s-next)What’s next

- **[Quickstart](/apps/quickstart)** — build a working app in a minute

- **[Examples](/apps/examples)** — complete working servers you can run today

- **[Providers](/apps/providers/approval)** — ready-made capabilities (approvals, choice pickers, file upload, forms) you add with one line

- **[Development](/apps/development)** — preview app tools locally with `fastmcp dev apps`

[OpenTelemetry
Previous](/servers/telemetry)[Quickstart
Next](/apps/quickstart)⌘I