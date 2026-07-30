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
      <div class="field"><label>用户名</label><input v-model="username" placeholder="admin" autocomplete="username" @keyup.enter="doLogin" /></div>
      <div class="field"><label>密码</label><input v-model="password" type="password" placeholder="········" autocomplete="current-password" @keyup.enter="doLogin" /></div>
      <button class="btn btn-primary btn-block" @click="doLogin" :disabled="loading">{{ loading ? '登录中…' : '登 录' }}</button>
      <div class="login-foot">v2026-07-28 ・ stateless ・ fastmcp 4.0</div>
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
/* ── Extracted from docs/superpowers/mockups/gateway-admin.html ── */
.login-wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; }
.login-card {
  width: 100%; max-width: 380px; background: var(--panel);
  border: 1px solid var(--border); border-radius: 14px;
  padding: 34px 32px 28px; box-shadow: var(--shadow);
  animation: rise 0.5s cubic-bezier(0.22, 1, 0.36, 1);
}
@keyframes rise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: translateY(0); } }
.brand { display: flex; align-items: center; gap: 12px; margin-bottom: 26px; }
.brand-glyph {
  width: 38px; height: 38px; border-radius: 9px; flex-shrink: 0;
  background: linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent) 55%, var(--read)));
  display: flex; align-items: center; justify-content: center; color: var(--accent-ink);
}
.brand-name { font-family: var(--font-display); font-weight: 700; font-size: 18px; letter-spacing: -0.01em; }
.brand-sub { font-family: var(--font-mono); font-size: 10.5px; color: var(--faint); letter-spacing: 0.08em; text-transform: uppercase; }
.field { margin-bottom: 16px; }
.field label { display: block; font-size: 12px; font-weight: 600; color: var(--muted); margin-bottom: 7px; }
.field input {
  width: 100%; background: var(--panel-2); border: 1px solid var(--border);
  border-radius: 9px; padding: 10px 12px; color: var(--text); font-size: 14px;
  transition: border-color 0.18s, box-shadow 0.18s;
}
.field input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  border-radius: 9px; border: 1px solid transparent;
  font-weight: 600; font-size: 13.5px; padding: 10px 16px;
  transition: transform 0.15s, background 0.18s, border-color 0.18s, box-shadow 0.18s, filter 0.18s;
}
.btn:active { transform: translateY(1px); }
.btn-primary { background: var(--accent); color: var(--accent-ink); }
.btn-primary:hover { filter: brightness(1.08); box-shadow: 0 4px 16px var(--accent-dim); }
.btn-block { width: 100%; }
.btn:disabled { opacity: 0.55; cursor: not-allowed; filter: none; }
.login-foot { margin-top: 18px; text-align: center; font-family: var(--font-mono); font-size: 10.5px; color: var(--faint); letter-spacing: 0.05em; }
.err-banner {
  background: var(--err-dim); border: 1px solid color-mix(in srgb, var(--err) 35%, transparent);
  color: var(--err); border-radius: 9px; padding: 9px 12px; font-size: 12.5px; margin-bottom: 16px;
}
</style>
