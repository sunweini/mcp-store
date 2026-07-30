<!-- src/components/Topbar.vue -->
<template>
  <header class="topbar">
    <div>
      <span class="crumb">{{ current.crumb }}</span>
      <h1>{{ current.label }}</h1>
    </div>
    <div class="topbar-actions">
      <button class="icon-btn" :title="theme === 'dark' ? '切换到浅色' : '切换到深色'" @click="toggleTheme">
        <svg v-if="theme === 'dark'" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.5"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5 5l1.6 1.6M17.4 17.4L19 19M19 5l-1.6 1.6M6.6 17.4L5 19"/></svg>
        <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a6.8 6.8 0 0 0 11 11z"/></svg>
      </button>
      <div class="user-chip"><span class="avatar">{{ initial }}</span><span class="uname">{{ username }}</span></div>
      <button class="mini-btn" @click="doLogout">退出</button>
    </div>
  </header>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useTheme } from '../composables/useTheme'
import { useAuth } from '../composables/useAuth'

const props = defineProps({
  page: { type: String, default: 'dashboard' },
})

const router = useRouter()
const { theme, toggleTheme } = useTheme()
const { logout } = useAuth()

const pages = [
  { id: 'dashboard', label: '监控面板', crumb: 'observe' },
  { id: 'servers', label: 'Servers', crumb: 'registry' },
  { id: 'tokens', label: 'Tokens', crumb: 'access' },
]

const current = computed(() => pages.find(p => p.id === props.page) || pages[0])

const username = computed(() => {
  try {
    const token = localStorage.getItem('gw-jwt')
    if (!token) return '?'
    // JWT payload is base64-encoded JSON in the middle segment
    const payload = JSON.parse(atob(token.split('.')[1]))
    return payload.sub || payload.username || '?'
  } catch { return '?' }
})

const initial = computed(() => (username.value[0] || '?').toUpperCase())

function doLogout() {
  logout()
  router.push({ name: 'login' })
}
</script>

<style scoped>
/* ── Extracted from docs/superpowers/mockups/gateway-admin.html ── */
.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 28px; border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--ink) 82%, transparent);
  backdrop-filter: blur(10px); position: sticky; top: 0; z-index: 20;
}
.topbar h1 { font-family: var(--font-display); font-size: 19px; font-weight: 700; letter-spacing: -0.01em; }
.topbar .crumb { font-family: var(--font-mono); font-size: 10.5px; color: var(--faint); letter-spacing: 0.08em; text-transform: uppercase; display: block; margin-bottom: 1px; }
.topbar-actions { display: flex; align-items: center; gap: 10px; }
.icon-btn {
  width: 34px; height: 34px; display: flex; align-items: center; justify-content: center;
  border-radius: 8px; border: 1px solid var(--border); background: var(--panel);
  color: var(--muted); transition: color 0.15s, border-color 0.15s;
}
.icon-btn:hover { color: var(--text); border-color: var(--border-strong); }
.user-chip { display: flex; align-items: center; gap: 9px; padding: 5px 12px 5px 6px; border-radius: 999px; border: 1px solid var(--border); background: var(--panel); }
.avatar { width: 26px; height: 26px; border-radius: 50%; background: linear-gradient(135deg, var(--accent), var(--read)); color: var(--accent-ink); display: flex; align-items: center; justify-content: center; font-family: var(--font-display); font-weight: 700; font-size: 11px; }
.user-chip .uname { font-size: 12.5px; font-weight: 600; }
.mini-btn { border-radius: 7px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 12px; padding: 5px 11px; transition: color 0.15s, border-color 0.15s, background 0.15s; }
.mini-btn:hover { color: var(--text); border-color: var(--border-strong); background: var(--panel-2); }
</style>
