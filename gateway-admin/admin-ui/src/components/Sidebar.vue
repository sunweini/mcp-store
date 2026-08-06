<!-- src/components/Sidebar.vue -->
<template>
  <aside class="sidebar">
    <div class="side-brand">
      <div class="brand-glyph">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="5" cy="12" r="2.4"/><circle cx="19" cy="5" r="2.4"/><circle cx="19" cy="19" r="2.4"/><path d="M7.3 11l9.4-4.9M7.3 13l9.4 4.9"/></svg>
      </div>
      <div class="brand-name" style="font-size:15px">Gateway</div>
    </div>
    <nav class="nav">
      <button
        v-for="p in navItems"
        :key="p.id"
        class="nav-item"
        :class="{ active: page === p.id }"
        @click="$emit('navigate', p.id)"
      >
        <span v-html="p.icon"></span>{{ p.label }}
      </button>
    </nav>
    <div class="side-foot">
      <div class="side-env">
        <StatusLed status="ok" :pulse="true" /> production
      </div>
    </div>
  </aside>
</template>

<script setup>
import StatusLed from './StatusLed.vue'

defineProps({
  page: { type: String, default: 'dashboard' },
})

defineEmits(['navigate'])

const navItems = [
  { id: 'dashboard', label: '监控面板', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 13l4-6 4 8 4-10 3 5h3"/></svg>' },
  { id: 'servers', label: 'Servers', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>' },
  { id: 'tokens', label: 'Tokens', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="8" cy="15" r="4.5"/><path d="M11.2 11.8L20 3M16 7l2.5 2.5M13.5 9.5L16 12"/></svg>' },
  { id: 'api-keys', label: 'API Keys', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/><circle cx="12" cy="12" r="3.5"/></svg>' },
  { id: 'calls', label: '请求日志', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 12h4M3 6h4M3 18h4M10 12h11M10 6h11M10 18h11"/></svg>' },
  { id: 'aliyun-accounts', label: '阿里云 DNS', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 10h4l2-5 3 12 2-4h5"/></svg>' },
]
</script>

<style scoped>
/* ── Extracted from docs/superpowers/mockups/gateway-admin.html ── */
.sidebar {
  width: 216px; flex-shrink: 0; background: var(--ink-2);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; padding: 20px 12px;
  position: sticky; top: 0; height: 100vh;
}
.side-brand { display: flex; align-items: center; gap: 10px; padding: 2px 8px 20px; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
.side-brand .brand-glyph { width: 32px; height: 32px; border-radius: 8px; }
.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-item {
  display: flex; align-items: center; gap: 11px; width: 100%;
  padding: 9px 12px; border-radius: 8px; border: none; background: transparent;
  color: var(--muted); font-size: 13.5px; font-weight: 500; text-align: left;
  position: relative; transition: background 0.15s, color 0.15s;
}
.nav-item:hover { background: var(--panel-2); color: var(--text); }
.nav-item.active { background: var(--accent-dim); color: var(--accent); }
.nav-item.active::before {
  content: ""; position: absolute; left: -12px; top: 8px; bottom: 8px;
  width: 3px; border-radius: 0 3px 3px 0; background: var(--accent);
}
.nav-item svg { flex-shrink: 0; }
.side-foot { margin-top: auto; padding: 12px 10px 0; border-top: 1px solid var(--border); }
.side-env { display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 10.5px; color: var(--faint); letter-spacing: 0.05em; }
</style>
