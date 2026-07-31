FROM mcp-base:latest
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY . ./

ENV MCP_HOST=0.0.0.0
CMD ["uv", "run", "python", "server.py"]
