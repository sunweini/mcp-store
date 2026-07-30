# gateway-admin Frontend Implementation Plan (Plan C)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the high-fidelity Vue 3 mockup (`docs/superpowers/mockups/gateway-admin.html`) into a proper Vite + Vue Router SPA at `gateway-admin/admin-ui/`. Builds to `dist/` served by the FastAPI backend at `:8081`.

**Architecture:** Vue 3 Composition API + Vite + Vue Router (4 routes: login/dashboard/servers/tokens). JWT stored in localStorage, `Authorization: Bearer <jwt>` on all /api calls. Design tokens (CSS custom properties) extracted from mockup, dark/light theme persisted. Each page is a single-file component consuming the shared design system. API calls via `fetch()` (native, no axios dep). No Pinia — local reactive state per view.

**Tech Stack:** Vue 3.5, Vue Router 4.5, Vite 6, vanilla CSS (no Tailwind), `fetch()` for API

## Global Constraints

- Design system MUST match the mockup exactly: same CSS tokens, same fonts (Space Grotesk + IBM Plex Sans + IBM Plex Mono), same color values
- Dark theme is default; light theme toggleable; preference saved to localStorage (`gw-theme`)
- All API calls target the same origin (`/api/*`) — the FastAPI backend serves both API + static files
- JWT from `/api/login` response, stored as `gw-jwt` in localStorage, sent as `Authorization: Bearer` header
- Router guard: unauthenticated users redirected to `/login`
- List/detail pages load data on mount from the API; dashboard polls periodically (optional)
- Build output: `gateway-admin/admin-ui/dist/` (served by `app.py` StaticFiles)
- No TypeScript (plain JS + Vue SFC)
- Comments explain "why" not "what" where appropriate
- Responsive: sidebar collapses below 900px, stat grid 4→2→1 cols
- `prefers-reduced-motion` respected

---

## File Structure

```
gateway-admin/admin-ui/
├── index.html
├── package.json
├── vite.config.js
├── public/
├── src/
│   ├── main.js              # createApp + router + theme init
│   ├── App.vue              # router-view
│   ├── router/
│   │   └── index.js         # 4 routes + auth guard
│   ├── api/
│   │   └── index.js          # fetch wrapper + JWT header
│   ├── composables/
│   │   ├── useTheme.js       # dark/light toggle + localStorage
│   │   └── useAuth.js        # login/logout + JWT session
│   ├── styles/
│   │   ├── tokens.css        # CSS custom properties (dark + light)
│   │   └── base.css          # reset + body + utilities
│   ├── components/
│   │   ├── Sidebar.vue       # left nav + brand + env status
│   │   ├── Topbar.vue        # breadcrumb + theme toggle + user chip
│   │   ├── StatusLed.vue      # pulsing status indicator
│   │   └── Modal.vue          # reusable modal wrapper
│   └── views/
│       ├── Login.vue         # login card + brand
│       ├── Dashboard.vue     # statusboard + stat cards + bars + failures + timeline
│       ├── Servers.vue       # expandable server cards + tools + health
│       └── Tokens.vue        # token table + create modal with permissions
└── dist/                     # build output (in ../admin-ui/ gitignored)
```

---

### Task 1: Vite Scaffold + Design System

**Files:**
- Create: `gateway-admin/admin-ui/package.json`
- Create: `gateway-admin/admin-ui/vite.config.js`
- Create: `gateway-admin/admin-ui/index.html`
- Create: `gateway-admin/admin-ui/src/main.js`
- Create: `gateway-admin/admin-ui/src/App.vue`
- Create: `gateway-admin/admin-ui/src/styles/tokens.css`
- Create: `gateway-admin/admin-ui/src/styles/base.css`
- Create: `gateway-admin/admin-ui/src/router/index.js`
- Create: `gateway-admin/admin-ui/src/composables/useTheme.js`
- Create: `gateway-admin/admin-ui/src/composables/useAuth.js`

**Interfaces:**
- Produces: Vite dev server on `:5173`, CSS design system with `data-theme="dark|light"` on `<html>`, router with auth guard, `useTheme()` (returns `{theme, toggleTheme}`), `useAuth()` (returns `{token, login, logout, isAuthed}`)

- [ ] **Step 1: Create package.json**

```bash
cd gateway-admin && mkdir -p admin-ui && cd admin-ui
```

```json
{
  "name": "gateway-admin-ui",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "vue": "^3.5",
    "vue-router": "^4.5"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5",
    "vite": "^6"
  }
}
```

- [ ] **Step 2: Install deps**

```bash
npm install
```

- [ ] **Step 3: Create vite.config.js**

```js
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    // proxy /api to FastAPI backend on :8081
    proxy: { '/api': 'http://localhost:8081' }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  }
})
```

- [ ] **Step 4: Copy design tokens from mockup**

Extract the exact `:root`, `:root[data-theme="dark"]`, `:root[data-theme="light"]` blocks from `docs/superpowers/mockups/gateway-admin.html` into `src/styles/tokens.css`. Also copy the font imports, base reset, and utility classes (`mono`, `muted`, `faint`) into `src/styles/base.css`.

- [ ] **Step 5: Create useTheme.js composable**

```js
/* src/composables/useTheme.js */
import { ref, watchEffect } from 'vue'

const theme = ref(localStorage.getItem('gw-theme') || 'dark')

watchEffect(() => {
  document.documentElement.setAttribute('data-theme', theme.value)
  localStorage.setItem('gw-theme', theme.value)
})

export function useTheme() {
  const toggleTheme = () => theme.value = theme.value === 'dark' ? 'light' : 'dark'
  return { theme, toggleTheme }
}
```

- [ ] **Step 6: Create useAuth.js composable**

```js
/* src/composables/useAuth.js */
import { ref, computed } from 'vue'

const jwt = ref(localStorage.getItem('gw-jwt'))

function setJwt(token) {
  jwt.value = token
  localStorage.setItem('gw-jwt', token)
}

export function useAuth() {
  const isAuthed = computed(() => !!jwt.value)

  async function login(username, password) {
    const resp = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
    if (!resp.ok) throw new Error((await resp.json()).detail || '登录失败')
    const data = await resp.json()
    setJwt(data.token)
    return data
  }

  function logout() {
    jwt.value = null
    localStorage.removeItem('gw-jwt')
  }

  return { token: jwt, isAuthed, login, logout }
}
```

- [ ] **Step 7: Create router with auth guard**

```js
/* src/router/index.js */
import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', name: 'login', component: () => import('../views/Login.vue') },
  { path: '/', name: 'dashboard', component: () => import('../views/Dashboard.vue') },
  { path: '/servers', name: 'servers', component: () => import('../views/Servers.vue') },
  { path: '/tokens', name: 'tokens', component: () => import('../views/Tokens.vue') },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to, from, next) => {
  const jwt = localStorage.getItem('gw-jwt')
  if (to.name !== 'login' && !jwt) return next({ name: 'login' })
  if (to.name === 'login' && jwt) return next({ name: 'dashboard' })
  next()
})

export default router
```

- [ ] **Step 8: Create main.js + App.vue**

```js
/* src/main.js */
import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './styles/tokens.css'
import './styles/base.css'

createApp(App).use(router).mount('#app')
```

```html
<!-- src/App.vue -->
<template><router-view /></template>
```

```html
<!-- index.html -->
<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>MCP Gateway · Console</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet" />
</head>
<body><div id="app"></div><script type="module" src="/src/main.js"></script></body>
</html>
```

- [ ] **Step 9: Verify dev server starts**

```bash
cd gateway-admin/admin-ui && npm run dev &
sleep 3
curl -s http://localhost:5173 | grep "MCP Gateway"
kill %1
```
Expected: HTML page served, no errors.

- [ ] **Step 10: Commit**

```bash
git add gateway-admin/admin-ui
git commit -m "feat(admin-ui): Vite scaffold + design tokens + router + auth composables"
```

---

### Task 2: Login Page + Auth Flow

**Files:**
- Create: `gateway-admin/admin-ui/src/views/Login.vue`

**Interfaces:**
- Consumes: `useAuth()` composable, `$router`
- Produces: functional login page that calls `/api/login`

- [ ] **Step 1: Build Login.vue — extract from mockup**

Pull the login section from `docs/superpowers/mockups/gateway-admin.html` (the `<div v-if="!authed" class="login-wrap">` block) into `src/views/Login.vue`. Replace `v-model` vars with `ref()`, replace `login()` with `useAuth().login()`:

```html
<!-- src/views/Login.vue -->
<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="brand">
        <div class="brand-glyph">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="5" cy="12" r="2.4"/><circle cx="19" cy="5" r="2.4"/><circle cx="19" cy="19" r="2.4"/><path d="M7.3 11l9.4-4.9M7.3 13l9.4 4.9"/></svg>
        </div>
        <div>
          <div class="brand-name">MCP Gateway</div>
          <div class="brand-sub">Control Console</div>
        </div>
      </div>
      <div v-if="error" class="err-banner">{{ error }}</div>
      <div class="field"><label>用户名</label><input v-model="username" placeholder="admin" @keyup.enter="doLogin" /></div>
      <div class="field"><label>密码</label><input v-model="password" type="password" placeholder="••••••••" @keyup.enter="doLogin" /></div>
      <button class="btn btn-primary btn-block" @click="doLogin" :disabled="loading">{{ loading ? '登录中…' : '登 录' }}</button>
      <div class="login-foot">v2026-07-28 · stateless · fastmcp 4.0</div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '../composables/useAuth'

const router = useRouter()
const { login } = useAuth()
const username = ref('admin')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function doLogin() {
  error.value = ''
  if (!username.value || !password.value) { error.value = '请输入用户名和密码'; return }
  loading.value = true
  try {
    await login(username.value, password.value)
    router.push({ name: 'dashboard' })
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* Copy login-card + field + btn styles from mockup exactly */
</style>
```

- [ ] **Step 2: Verify login page renders at /login**

```bash
cd gateway-admin && JWT_SECRET=dev uv run uvicorn app:app --port 8081 &
sleep 3
curl -s -X POST http://localhost:8081/api/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('token','')[:20]+'...')"
kill %1
```
Expected: JWT token prefix `eyJ...`

- [ ] **Step 3: Commit**

```bash
git add gateway-admin/admin-ui/src/views/Login.vue
git commit -m "feat(admin-ui): login page with JWT auth flow"
```

---

### Task 3: Shell Layout (Sidebar + Topbar) + Dashboard Shell

**Files:**
- Create: `gateway-admin/admin-ui/src/components/Sidebar.vue`
- Create: `gateway-admin/admin-ui/src/components/Topbar.vue`
- Create: `gateway-admin/admin-ui/src/components/StatusLed.vue`
- Create: `gateway-admin/admin-ui/src/views/Dashboard.vue` (shell only)
- Modify: `gateway-admin/admin-ui/src/App.vue` (wrap views with shell)

**Interfaces:**
- Produces: functional sidebar (3 nav items + env status LED), topbar (page title + theme toggle + user chip + logout), dashboard shell view

- [ ] **Step 1: Create StatusLed.vue**

```html
<!-- src/components/StatusLed.vue -->
<template>
  <span class="led" :class="[status, pulse ? 'pulse' : '']"></span>
</template>
<script setup>
defineProps({ status: { type: String, default: 'ok' }, pulse: { type: Boolean, default: false } })
</script>
<style scoped>
/* led CSS from mockup: .led + .led.ok/.warn/.err/.off + .led.pulse + @keyframes led-ring */
</style>
```

- [ ] **Step 2: Create Sidebar.vue, Topbar.vue**

Extract the sidebar + topbar HTML/CSS from the mockup into two components. Sidebar accepts `page` prop (dashboard/servers/tokens) and emits `navigate(name)`. Topbar accepts `page` prop and shows the correct title + crumb.

- [ ] **Step 3: Create Dashboard.vue shell**

Empty dashboard view with the shell structure (statusboard area + stat grid area + 2-column panels + timeline) but no real data yet. Data comes in Task 4.

- [ ] **Step 4: Update App.vue to conditionally show shell vs login**

```html
<!-- src/App.vue -->
<template>
  <router-view v-if="$route.name === 'login'" />
  <div v-else class="shell">
    <Sidebar :page="currentPage" @navigate="nav" />
    <div class="main">
      <Topbar :page="currentPage" />
      <div class="content"><router-view /></div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Sidebar from './components/Sidebar.vue'
import Topbar from './components/Topbar.vue'

const route = useRoute()
const router = useRouter()
const currentPage = computed(() => route.name)
function nav(name) { router.push({ name }) }
</script>
```

- [ ] **Step 5: Verify dev server shows shell after login**

```bash
cd gateway-admin/admin-ui && npm run dev &
sleep 3
curl -s http://localhost:5173/ | grep "shell"
kill %1
```

- [ ] **Step 6: Commit**

```bash
git add gateway-admin/admin-ui/src/components/Sidebar.vue gateway-admin/admin-ui/src/components/Topbar.vue gateway-admin/admin-ui/src/components/StatusLed.vue gateway-admin/admin-ui/src/views/Dashboard.vue gateway-admin/admin-ui/src/App.vue
git commit -m "feat(admin-ui): shell layout (sidebar + topbar) + StatusLed + empty dashboard"
```

---

### Task 4: Dashboard Page (live data)

**Files:**
- Create: `gateway-admin/admin-ui/src/api/index.js`
- Modify: `gateway-admin/admin-ui/src/views/Dashboard.vue` (add real data)

**Interfaces:**
- Consumes: `/api/metrics/summary?server=`, `/api/metrics/by-server`, `/api/metrics/timeseries?server=`, `/api/failures?server=&limit=&offset=`
- Produces: fully functional dashboard with live sparkline + stat cards + by-server bars + failure feed + journey detail + read/write split + timeline

- [ ] **Step 1: Create API helper**

```js
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
```

- [ ] **Step 2: Build Dashboard.vue with real data**

Port the dashboard template + script logic from `docs/superpowers/mockups/gateway-admin.html` into `src/views/Dashboard.vue`. Replace mock `servers`/`srvStats`/`failures` with API calls on mount. Key changes from mockup:
- `onMounted` → call `getMetricsSummary()`, `getMetricsByServer()`, `getFailures()`, `getServers()`
- `scopeStats` computed → use reactive API response data instead of mock `srvStats`
- `scopedFailures` computed → use API response instead of mock `failures` array
- sparkline + timeline data → from `getMetricsTimeseries()`
- `selFail` ref → click a failure item in the feed, show journey detail (journey comes from API response)
- `ping(s)` → call `api.pingServer(s.name)` then refresh

Copy ALL CSS from the mockup dashboard section into the component's `<style scoped>`.

- [ ] **Step 3: Verify dashboard renders with real API**

```bash
cd gateway-admin && JWT_SECRET=dev REDIS_URL=redis://localhost:6379/0 uv run uvicorn app:app --port 8081 &
cd gateway-admin/admin-ui && npm run dev &
sleep 3
# Login to get JWT
TOKEN=$(curl -s -X POST http://localhost:8081/api/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
# Check metrics API
curl -s http://localhost:8081/api/metrics/summary -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -10
kill %1 %2
```
Expected: metrics summary JSON with `requests`/`errors`/... fields (may be 0 if no proxy traffic).

- [ ] **Step 4: Commit**

```bash
git add gateway-admin/admin-ui/src/api/index.js gateway-admin/admin-ui/src/views/Dashboard.vue
git commit -m "feat(admin-ui): dashboard with live API data + failure feed + journey detail"
```

---

### Task 5: Servers Page + Tokens Page

**Files:**
- Create: `gateway-admin/admin-ui/src/views/Servers.vue`
- Create: `gateway-admin/admin-ui/src/views/Tokens.vue`
- Create: `gateway-admin/admin-ui/src/components/Modal.vue`

**Interfaces:**
- Consumes: `/api/servers` (CRUD + /status + /refresh-tools), `/api/tokens` (CRUD)
- Produces: functional servers page (expandable cards + tools + modal add/edit/delete + health probe), tokens page (table + create modal with permissions + one-time reveal + delete)

- [ ] **Step 1: Create Modal.vue** (reusable)

```html
<!-- src/components/Modal.vue -->
<template>
  <div v-if="show" class="modal-mask" @click.self="$emit('close')">
    <div class="modal">
      <div class="modal-head"><h3>{{ title }}</h3>
        <button class="icon-btn" @click="$emit('close')"><!-- X icon --></button>
      </div>
      <div class="modal-body"><slot /></div>
      <div class="modal-foot"><slot name="footer" /></div>
    </div>
  </div>
</template>
<script setup>
defineProps({ title: String, show: Boolean })
defineEmits(['close'])
</script>
<style scoped>
/* modal CSS from mockup: .modal-mask + .modal + .modal-head + .modal-body + .modal-foot */
</style>
```

- [ ] **Step 2: Build Servers.vue**

Port the servers section from the mockup. Key API interactions:
- `onMounted` → `getServers()` loads list
- "添加 Server" button → opens Modal with name/url/description fields → `createServer(data)`
- Expand row → shows tools list (from server.tools), health status (from server.health), MCP URL
- "立即探活" → `pingServer(name)` → update health display
- "编辑" → opens Modal pre-filled → `updateServer(name, data)`
- "删除" → confirm → `deleteServer(name)`
- Tool rows: mode-badge (R/W) from server.tools[].mode, description from server.tools[].description

- [ ] **Step 3: Build Tokens.vue**

Port the tokens section from the mockup. Key API interactions:
- `onMounted` → `getTokens()` + `getServers()` (for permission reference)
- "创建 Token" button → opens Modal with name + per-server read/write toggle switches
- Each server gets a toggle block: `☑ Read  ☐ Write` — uses `switch` CSS from mockup
- Submit → `createToken({name, permissions: {zabbix: {read: T, write: F}, ...}})` → on success, show the token plaintext ONCE (alert or modal), then close
- List: masked token (token_masked), name, permission chips, created date
- "删除" → confirm → `deleteToken(id)`

- [ ] **Step 4: Verify both pages work with API**

```bash
cd gateway-admin && JWT_SECRET=dev REDIS_URL=redis://localhost:6379/0 uv run uvicorn app:app --port 8081 &
TOKEN=$(curl -s -X POST http://localhost:8081/api/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
# Create a test server
curl -s -X POST http://localhost:8081/api/servers -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"name":"zabbix","url":"http://localhost:8000/mcp","description":"test"}' | python3 -m json.tool
# Create a test token
curl -s -X POST http://localhost:8081/api/tokens -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"name":"readonly","permissions":{"zabbix":{"read":true,"write":false}}}' | python3 -m json.tool
# List tokens (should show masked)
curl -s http://localhost:8081/api/tokens -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
kill %1
```
Expected: All API calls succeed.

- [ ] **Step 5: Commit**

```bash
git add gateway-admin/admin-ui/src/views/Servers.vue gateway-admin/admin-ui/src/views/Tokens.vue gateway-admin/admin-ui/src/components/Modal.vue
git commit -m "feat(admin-ui): servers page + tokens page with CRUD modals"
```

---

### Task 6: Build + Integration Test

**Files:**
- Modify: `gateway-admin/admin-ui/vite.config.js` (verify outDir)
- Create: `gateway-admin/admin-ui/.gitignore`

- [ ] **Step 1: Build for production**

```bash
cd gateway-admin/admin-ui && npm run build
ls dist/  # should have index.html + assets/
```
Expected: `dist/index.html`, `dist/assets/` with hashed JS/CSS bundles.

- [ ] **Step 2: Start FastAPI and verify it serves the SPA**

```bash
cd gateway-admin && JWT_SECRET=dev REDIS_URL=redis://localhost:6379/0 uv run uvicorn app:app --port 8081 &
sleep 3
# Verify static files served
curl -s http://localhost:8081/ | head -c 200
# Verify /login deep link works (SPA fallback — StaticFiles with html=True)
curl -s http://localhost:8081/login | head -c 200
# Verify API still works
TOKEN=$(curl -s -X POST http://localhost:8081/api/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
curl -s http://localhost:8081/api/servers -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
kill %1
```
Expected: All 3 curls return valid HTML (SPA) or JSON (API). No 404s.

- [ ] **Step 3: Create .gitignore for admin-ui**

```
node_modules/
dist/
.vite/
```

- [ ] **Step 4: Final commit for Plan C**

```bash
git add gateway-admin/admin-ui/.gitignore
git commit -m "feat(admin-ui): production build + integration verification

Vite build outputs to dist/, served by FastAPI StaticFiles.
Verified: SPA loads at /, API at /api/*, deep links work."
```

---

## Self-Review

**1. Mockup coverage:**
- [x] Login page → Task 2
- [x] Dashboard (statusboard + sparkline + stats + bars + failures + journey + read/write + timeline) → Task 4
- [x] Servers page (expandable cards + tools + health + probe + modal CRUD) → Task 5
- [x] Tokens page (table + create modal with permissions + mask + delete) → Task 5
- [x] Dark/light theme → Task 1 (useTheme composable)
- [x] Sidebar + topbar shell → Task 3
- [x] API integration (fetch wrapper + JWT) → Task 4
- [x] Production build + integration → Task 6

**2. Placeholder scan:** No TBD/TODO. CSS values not copied inline in plan steps (referenced from mockup — implementer reads mockup for exact values).

**3. Type consistency:** API fetch wrapper returns Promise<Object|Array|null>. All views use `onMounted` + `ref()` patterns. Auth flow: login sets JWT → router guard checks localStorage → API calls attach header.

Plan focuses on the structural transformation (CDN prototype → Vite SPA) with exact file paths and API contract references. The implementer references `docs/superpowers/mockups/gateway-admin.html` for exact CSS/HTML, extracting components from it verbatim-with-Vue-adaptations.
