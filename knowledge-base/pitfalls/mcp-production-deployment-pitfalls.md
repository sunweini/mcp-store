# MCP 生产环境部署踩坑（受限网络）

> 来源：10.33.17.72 正式环境部署实践（2026-07/08）。
> 适用：受限网络（无外网/部分域名可达）下的容器化 MCP 部署。

## 网络环境特征

- 内网服务器：部分外网域名可达（阿里云、baidu、serpapi.com），部分不可达（files.pythonhosted.org、api.search.brave.com）
- **宿主机与容器网络出口可能不同**：宿主机 curl 通的域名，容器内可能超时（Docker NAT/防火墙差异）
- IPv6 出口不可靠：DNS 返回 IPv6 地址（如 Meta 段）但路由不通 → 超时

## 踩坑与解法

### 1. uv.lock 官方源不可达

**症状**：`uv sync --frozen` 卡在 `files.pythonhosted.org` 下载超时（6 次重试失败）。
**根因**：uv.lock 里所有包 URL **写死官方源**，`UV_DEFAULT_INDEX` 只影响新解析，`--frozen` 用 lock 绝对 URL 下载。
**解法**：
```bash
rm -f uv.lock
UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv lock   # 重新生成指向阿里云
# 验证: grep -c mirrors.aliyun.com uv.lock 应 >0, files.pythonhosted 应为 0
```
- `uv lock --index`（不删 lock）**不重写已有 URL**——必须删了重新生成
- Dockerfile.base 配 `UV_DEFAULT_INDEX` 不够，lock 文件才是关键

### 2. 容器内域名可达性与宿主机不同

**症状**：宿主机 `curl api.tavily.com` 通（1.4s），容器内 15s 超时。
**排查**：`docker exec <容器> curl` 对照测试；注意 DNS 返回 IPv6 时用 `-4` 强制 IPv4 区分。
**特性**：可能是临时波动（AWS 服务偶发慢）也可能是真不通——多测几次再下结论。

### 3. Redis 数据目录权限（MISCONF 禁写）

**症状**：`MISCONF Redis is configured to save RDB snapshots...`——所有写操作被禁（key 添加、token 创建全 500）。
**根因**：redis 容器内用户 **uid=999(gid=1000)**，宿主数据目录 owner 不匹配 → bgsave 子进程写 temp RDB 文件 Permission denied → stop-writes-on-bgsave-error 禁写。
**解法**：
```bash
chown -R 999:1000 /path/to/data/redis/
chmod 700 /path/to/data/redis/
docker restart <redis容器>
# 验证: docker exec <redis> redis-cli bgsave && ls -la dump.rdb（时间戳应更新）
```
**坑**：
- 容器内 `id redis` 显示 uid 999，**不是 1000**（redis:7-alpine 镜像）
- `redis-cli save`（主进程同步写）可能成功但 `bgsave`（fork 子进程）失败——**必须验证 bgsave**
- 修复后要 `docker restart`，否则 stop-writes 状态残留

### 4. Redis 重启后 MCP 热更新永久失效

**症状**：Redis 重启后，MCP 的 `get_message` 永远抛错，key 池变空（key_count=0），只能重启 MCP 恢复。
**根因**：redis-py 的 pubsub 连接死后**不会自动重连**。
**解法**：`_listen` except 分支重建 pubsub（`aclose()` 旧的 → `self._redis.pubsub()` 新的 → 重新 subscribe），见 key-pool-pattern.md 第 4 节。

### 5. 容器外网代理

**场景**：某源 API（如 brave）双栈不通，但 HTTP 代理可达。
**做法**：
- httpx 原生支持 `proxy=` 参数（`httpx.AsyncClient(proxy="http://host:port")`）
- 环境变量 `SEARCH_PROXY` 控制，空则不用——不要硬编码进代码
- **持久化**：compose 里 `SEARCH_PROXY: "${SEARCH_PROXY:-}"` 从部署 env 读，但 environment 优先级高于 env_file，**空串会覆盖 proxy.env 真实值**——正确做法是服务 `env_file: ./config/proxy.env` 且不写 environment 覆盖
- 代理只给需要的服务（brave-mcp + admin 探活），其他直连

## 部署流程（git archive 方式）

```bash
# 1. 本地打包（git archive 只含已提交文件）
git archive --format=tar.gz -o /tmp/deploy.tar.gz HEAD

# 2. scp 到远程 + 解包
scp -i ~/.ssh/id_loginmonitor -P 9166 /tmp/deploy.tar.gz root@<host>:/tmp/
ssh root@<host> "tar xzf /tmp/deploy.tar.gz -C /opt/mcp-gateway-cfg/"

# 3. 注意：源码目录（非 deploy/）也要解包！
#    compose 的 build: ../tavily-mcp 引用部署根下的源码目录
#    cp -r /tmp/mcpstore-new/tavily-mcp /opt/mcp-gateway-cfg/

# 4. config/*.env 是运行时文件，解包覆盖 deploy/ 前先备份
cp -r deploy/config deploy/config.bak.$(date +%Y%m%d)

# 5. 重建
cd deploy && bash deploy.sh
```

**坑**：
- `git archive` 只打包已提交文件——未提交改动不会进去
- 解包 deploy/ 覆盖时，config/ 保留旧的（含密钥），只更新代码
- 注册 URL 存量迁移（如 zabbix 8000→9053）：compose + Redis 里 `servers:<name>` hash 的 url 字段**两处都要改**，proxy 靠热更新感知（等几秒或重启 proxy）

## 验证清单（部署后必做）

1. `docker compose ps` 全 Healthy
2. admin 探活每个 server（`GET /api/servers/<name>/status` → up:true）
3. 经 proxy 真实调用一次工具（工具列表 + 实际搜索）
4. 容器内出网验证（`docker exec <mcp> curl <api>`）
5. Redis bgsave 验证落盘
