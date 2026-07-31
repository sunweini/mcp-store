FROM mcp-base:latest
WORKDIR /app

# 依赖层(变更少,利用 cache)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 源码
COPY . ./

ENV GATEWAY_HOST=0.0.0.0
CMD ["uv", "run", "python", "server.py"]
