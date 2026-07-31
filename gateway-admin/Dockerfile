FROM mcp-base:latest
WORKDIR /app

# 前端构建(node 层,package-lock 变了才重装)
COPY admin-ui/package.json admin-ui/package-lock.json ./admin-ui/
# npm install 而非 npm ci:npm ci 会用 lock 里的 npmjs resolved URL,生产服务器访问不了 npmjs;
# npm install 用基础镜像配的 npmmirror registry
RUN cd admin-ui && npm install --no-fund --no-audit
COPY admin-ui/ ./admin-ui/
RUN cd admin-ui && npm run build   # 产物 -> admin-ui/dist

# Python 依赖
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

# 源码
COPY . ./

CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081"]
