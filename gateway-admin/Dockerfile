FROM mcp-base:latest
WORKDIR /app

# 前端构建(node 层,package-lock 变了才重装)
COPY admin-ui/package.json admin-ui/package-lock.json ./admin-ui/
RUN cd admin-ui && npm ci
COPY admin-ui/ ./admin-ui/
RUN cd admin-ui && npm run build   # 产物 -> admin-ui/dist

# Python 依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 源码
COPY . ./

ENV ADMIN_PORT=8081
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081"]
