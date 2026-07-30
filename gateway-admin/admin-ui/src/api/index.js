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
export function getTokens()                 { return apiFetch('/api/tokens') }
export function createToken(data)           { return apiFetch('/api/tokens', { method:'POST', body:JSON.stringify(data) }) }
export function deleteToken(id)             { return apiFetch(`/api/tokens/${id}`, { method:'DELETE' }) }
