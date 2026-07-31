FROM mcp-base:latest
WORKDIR /app

# 依赖层(变更少,利用 cache);README.md 是 pyproject readme 字段引用,uv sync 构建本地包时需要
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

# 源码
COPY . ./

ENV GATEWAY_HOST=0.0.0.0
CMD ["uv", "run", "python", "server.py"]
