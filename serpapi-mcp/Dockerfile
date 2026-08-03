# 继承仓库基础镜像 mcp-base(python3.12-slim + uv + 阿里云 apt/pypi 镜像源,见 deploy/Dockerfile.base)
# 生产网络受限:apt/pypi 走阿里云,uv 二进制来自 ghcr(可达)
FROM mcp-base:latest
WORKDIR /app

# uv.lock 由 uv lock 生成且与 pyproject 一致(uv lock --check 通过),用 --frozen 保证可复现构建
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY . ./

ENV MCP_HOST=0.0.0.0
CMD ["uv", "run", "python", "server.py"]
