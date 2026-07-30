<!-- src/views/Tokens.vue -->
<!-- All CSS extracted from docs/superpowers/mockups/gateway-admin.html and placed in src/styles/base.css (global). -->
<template>
  <div>
    <!-- Top action bar -->
    <div class="table-actions">
      <input class="search-input" placeholder="搜索 token…" v-model="tokenQuery" />
      <button class="btn btn-primary" @click="openCreateModal()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
        创建 Token
      </button>
    </div>

    <!-- Error banner -->
    <div v-if="error" class="err-banner">{{ error }}</div>

    <!-- Loading -->
    <div v-if="loading" class="muted" style="padding:24px 0">加载中…</div>

    <!-- Token table -->
    <table v-else class="tbl">
      <thead>
        <tr>
          <th style="width:22%">Name</th>
          <th style="width:42%">Permissions</th>
          <th style="width:18%">Created</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="t in filteredTokens" :key="t.id">
          <td class="cell-name">{{ t.name }}</td>
          <td>
            <div class="perm-chips">
              <span class="perm-chip" v-for="(perm, srv) in (t.permissions || {})" :key="srv">
                {{ srv }}<span v-if="perm.read" class="r">R</span><span v-if="perm.write" class="w">W</span>
              </span>
            </div>
          </td>
          <td class="mono" style="font-size:11.5px;color:var(--muted)">{{ fmtDate(t.created_at) }}</td>
          <td>
            <div class="row-actions">
              <button class="mini-btn danger" @click="doDelete(t)">删除</button>
            </div>
          </td>
        </tr>
        <tr v-if="!tokens.length && !loading">
          <td colspan="4" style="text-align:center;color:var(--muted);padding:24px 0">暂无 Token · 点击"创建 Token"生成第一个</td>
        </tr>
      </tbody>
    </table>

    <!-- ═════ TOKEN CREATE MODAL ═════ -->
    <Modal :show="!!tokenModal" title="创建 Token" @close="closeTokenModal">
      <div class="field"><label>Token 名称</label><input v-model="tokenModal.name" placeholder="zabbix-readonly" /></div>
      <label style="display:block;font-size:12px;font-weight:600;color:var(--muted);margin:4px 0 10px">权限配置</label>
      <div class="server-perm-block" v-for="s in servers" :key="s.name">
        <div class="server-perm-name">
          <span class="led" :class="healthLed(s.health)"></span> {{ s.name }}
        </div>
        <div class="perm-toggles">
          <label class="perm-toggle read">
            <input type="checkbox" v-model="tokenModal.perms[s.name].read" />
            <span class="switch"></span>
            <span class="plabel">Read</span>
          </label>
          <label class="perm-toggle write">
            <input type="checkbox" v-model="tokenModal.perms[s.name].write" />
            <span class="switch"></span>
            <span class="plabel">Write</span>
          </label>
        </div>
      </div>
      <div v-if="!servers.length" class="muted" style="font-size:12px;padding:8px 0">暂无已注册 Server，请先添加 Server</div>

      <!-- Token reveal (shown after creation) -->
      <div v-if="tokenModal.revealed" class="token-reveal">
        <div style="font-size:10px;color:var(--muted);margin-bottom:4px">以下 Token 仅显示一次，请立即复制保存：</div>
        {{ tokenModal.revealed }}
      </div>

      <template #footer>
        <button class="btn btn-ghost" @click="closeTokenModal">关闭</button>
        <button v-if="!tokenModal.revealed" class="btn btn-primary" :disabled="creating" @click="createTokenAction">
          {{ creating ? '创建中…' : '创建' }}
        </button>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { getTokens, createToken, deleteToken, getServers } from '../api/index.js'
import Modal from '../components/Modal.vue'

/* ── state ── */
const tokens = ref([])
const servers = ref([])
const loading = ref(false)
const error = ref('')
const creating = ref(false)
const tokenQuery = ref('')
const tokenModal = ref(null)

/* ── computed ── */
const filteredTokens = computed(() =>
  tokens.value.filter(t => t.name.includes(tokenQuery.value))
)

/* ── helpers ── */
function healthLed(h) {
  if (!h) return 'off'
  return h.up ? 'ok' : 'err'
}

function fmtDate(iso) {
  if (!iso) return '—'
  return iso.slice(0, 10) // YYYY-MM-DD
}

/* ── actions ── */
async function loadTokens() {
  loading.value = true
  error.value = ''
  try {
    tokens.value = await getTokens()
  } catch (e) {
    error.value = '加载 Token 列表失败: ' + e.message
  } finally {
    loading.value = false
  }
}

async function loadServers() {
  try {
    servers.value = await getServers()
  } catch {
    // non-critical; just means no server references
  }
}

function openCreateModal() {
  const perms = {}
  servers.value.forEach(s => { perms[s.name] = { read: false, write: false } })
  tokenModal.value = reactive({ name: '', perms, revealed: null })
}

function closeTokenModal() {
  tokenModal.value = null
}

async function createTokenAction() {
  const m = tokenModal.value
  if (!m || !m.name) return
  // Build permissions object — only include servers with at least one toggle on
  const active = {}
  for (const [srv, p] of Object.entries(m.perms)) {
    if (p.read || p.write) active[srv] = { read: p.read, write: p.write }
  }
  if (!Object.keys(active).length) {
    error.value = '请至少为一个 Server 开启权限'
    return
  }
  creating.value = true
  try {
    const result = await createToken({ name: m.name, permissions: active })
    m.revealed = result.token
    await loadTokens()
  } catch (e) {
    error.value = '创建 Token 失败: ' + e.message
  } finally {
    creating.value = false
  }
}

async function doDelete(t) {
  if (!confirm(`确定删除 Token "${t.name}"? 此操作不可撤销。`)) return
  try {
    await deleteToken(t.id)
    await loadTokens()
  } catch (e) {
    error.value = '删除失败: ' + e.message
  }
}

/* ── lifecycle ── */
onMounted(async () => {
  await Promise.all([loadTokens(), loadServers()])
})
</script>
