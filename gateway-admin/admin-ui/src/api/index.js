/* src/api/index.js */
const BASE = ''  // same origin, Vite proxy handles /api

async function apiFetch(path, opts = {}) {
  const jwt = localStorage.getItem('gw-jwt')
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  if (jwt) headers['Authorization'] = `Bearer ${jwt}`
  const resp = await fetch(`${BASE}${path}`, { ...opts, headers })
  if (resp.status === 401) { localStorage.removeItem('gw-jwt'); window.location = '/login'; return }
  if (!resp.ok) throw new Error(`API ${resp.status}: ${path}`)
  if (resp.status === 204) return null
  return resp.json()
}

export function getMetricsSummary(server)  { return apiFetch(`/api/metrics/summary?${server ? `server=${server}` : ''}`) }
export function getMetricsByServer()        { return apiFetch('/api/metrics/by-server') }
export function getMetricsTimeseries(s, w)   { return apiFetch(`/api/metrics/timeseries?${s ? `server=${s}&` : ''}window=${w || '1h'}`) }
export function getFailures(server, limit, offset) {
  const p = new URLSearchParams({ limit, offset })
  if (server) p.set('server', server)
  return apiFetch(`/api/failures?${p}`)
}
export function getServers()                { return apiFetch('/api/servers') }
export function createServer(data)          { return apiFetch('/api/servers', { method:'POST', body:JSON.stringify(data) }) }
export function deleteServer(name)          { return apiFetch(`/api/servers/${name}`, { method:'DELETE' }) }
export function updateServer(name, data)    { return apiFetch(`/api/servers/${name}`, { method:'PUT', body:JSON.stringify(data) }) }
export function pingServer(name)            { return apiFetch(`/api/servers/${name}/status`) }
export function refreshTools(name)          { return apiFetch(`/api/servers/${name}/refresh-tools`, { method:'POST' }) }
export function lifecycleServer(name, action) { return apiFetch(`/api/servers/${name}/lifecycle`, { method: 'POST', body: JSON.stringify({ action }) }) }
export function getTokens()                 { return apiFetch('/api/tokens') }
export function createToken(data)           { return apiFetch('/api/tokens', { method:'POST', body:JSON.stringify(data) }) }
export function deleteToken(id)             { return apiFetch(`/api/tokens/${id}`, { method:'DELETE' }) }

// ── Search API keys ─────────────────────────────
export function getSearchKeys(provider)     { return apiFetch(`/api/search-keys/${provider}`) }
export function addSearchKey(provider, data) { return apiFetch(`/api/search-keys/${provider}`, { method:'POST', body:JSON.stringify(data) }) }
export function updateSearchKey(provider, keyId, data) { return apiFetch(`/api/search-keys/${provider}/${keyId}`, { method:'PUT', body:JSON.stringify(data) }) }
export function deleteSearchKey(provider, keyId) { return apiFetch(`/api/search-keys/${provider}/${keyId}`, { method:'DELETE' }) }
export function getSearchKeyUsage(provider)  { return apiFetch(`/api/search-keys/${provider}/usage`) }
// 官方用量校准：tavily/serpapi 拉官方接口同步 quota/remaining（brave 无接口，后端返回 supported=false）
export function calibrateKeys()              { return apiFetch('/api/search-keys/calibrate', { method:'POST' }) }

// ── Call audit log (MySQL calls 表) ──────────────
export function getCalls(params = {}) {
  const p = new URLSearchParams()
  if (params.server) p.set('server', params.server)
  if (params.status) p.set('status', params.status)
  if (params.limit) p.set('limit', params.limit)
  if (params.offset) p.set('offset', params.offset)
  return apiFetch(`/api/calls?${p}`)
}

// ── Aliyun DNS 账户 + 授权矩阵 ─────────────────
export function getAliyunAccounts()         { return apiFetch('/api/aliyun-accounts') }
export function createAliyunAccount(data)   { return apiFetch('/api/aliyun-accounts', { method:'POST', body:JSON.stringify(data) }) }
export function updateAliyunAccount(id, data) { return apiFetch(`/api/aliyun-accounts/${id}`, { method:'PUT', body:JSON.stringify(data) }) }
export function deleteAliyunAccount(id)     { return apiFetch(`/api/aliyun-accounts/${id}`, { method:'DELETE' }) }
export function getAliyunPerms(tokenId)     { return apiFetch(`/api/aliyun-perms/${tokenId}`) }
export function putAliyunPerms(tokenId, permissions) {
  return apiFetch(`/api/aliyun-perms/${tokenId}`, { method:'PUT', body:JSON.stringify({ permissions }) })
}
