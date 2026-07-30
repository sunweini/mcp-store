<!-- src/views/Servers.vue -->
<!-- All CSS extracted from docs/superpowers/mockups/gateway-admin.html and placed in src/styles/base.css (global). -->
<template>
  <div>
    <!-- Top action bar -->
    <div class="table-actions">
      <input class="search-input" placeholder="搜索 server…" v-model="serverQuery" />
      <button class="btn btn-primary" @click="openCreateModal()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
        添加 Server
      </button>
    </div>

    <!-- Error banner -->
    <div v-if="error" class="err-banner">{{ error }}</div>

    <!-- Loading -->
    <div v-if="loading" class="muted" style="padding:24px 0">加载中…</div>

    <!-- Server list -->
    <div v-else class="srv-list">
      <div v-for="s in filteredServers" :key="s.name" class="srv-card" :class="{ open: expanded === s.name }">
        <!-- Card head — click to expand -->
        <div class="srv-card-head" @click="expanded = expanded === s.name ? null : s.name">
          <div class="srv-id">
            <span class="led" :class="healthLed(s.health)"></span>
            <div style="min-width:0">
              <div class="cell-name">{{ s.name }}</div>
              <div class="srv-desc">{{ s.description }}</div>
            </div>
          </div>
          <div class="srv-health">
            <span class="status-chip" :class="s.health && s.health.up ? 'ok' : 'err'">
              <span class="led" :class="s.health && s.health.up ? 'ok pulse' : 'err'"></span>
              {{ s.health && s.health.up ? '健康' : '不可达' }}
            </span>
            <span class="mono" style="font-size:10px;color:var(--faint)">{{ healthLatency(s) }}</span>
          </div>
          <div class="srv-meta">
            <span class="mono" style="font-size:11px;color:var(--muted)">{{ (s.tools || []).length }} tools</span>
            <span class="mono" style="font-size:10px;color:var(--faint)">{{ countMode(s, 'read') }}R · {{ countMode(s, 'write') }}W</span>
          </div>
          <button class="expand-caret" :class="{ open: expanded === s.name }">▾</button>
        </div>

        <!-- Expanded detail -->
        <div v-if="expanded === s.name" class="srv-detail">
          <div class="srv-detail-meta">
            <div><span class="mk">MCP URL</span><span class="mv mono">{{ s.url }}</span></div>
            <div><span class="mk">最近探活</span><span class="mv mono">{{ s.health && s.health.last_check || '从未' }}</span></div>
            <div><span class="mk">探活延迟</span><span class="mv mono">{{ healthLatency(s) }}</span></div>
            <div><span class="mk">探活方式</span><span class="mv mono">MCP ping</span></div>
          </div>

          <!-- Tools list -->
          <div class="tools-head">
            <span>Tools</span>
            <span class="hint">{{ (s.tools || []).length }} 个 · {{ countMode(s, 'read') }} 读 / {{ countMode(s, 'write') }} 写</span>
          </div>
          <div v-if="!s.tools || !s.tools.length" class="muted" style="font-size:12.5px;padding:6px 4px">暂无工具（点击"刷新 Tools"获取）</div>
          <div class="tool-row" v-for="t in (s.tools || [])" :key="t.name">
            <span class="mode-badge" :class="t.mode" :title="t.mode === 'read' ? '读操作 readOnlyHint' : '写操作 destructiveHint'">
              {{ t.mode === 'read' ? 'R' : 'W' }}
            </span>
            <div class="tool-info">
              <div class="tool-name mono">{{ t.name }}</div>
              <div class="tool-desc">{{ t.desc || t.description || '' }}</div>
            </div>
          </div>

          <!-- Action buttons -->
          <div class="srv-actions">
            <button class="mini-btn" @click="doPing(s)">立即探活</button>
            <button class="mini-btn" @click="doRefreshTools(s)">刷新 Tools</button>
            <button class="mini-btn" @click="openEditModal(s)">编辑</button>
            <button class="mini-btn danger" @click="doDelete(s)">删除</button>
          </div>
        </div>
      </div>

      <!-- Empty state -->
      <div v-if="!servers.length && !loading" class="muted" style="padding:24px 4px;text-align:center">
        暂无 Server · 点击"添加 Server"注册第一个 MCP 后端
      </div>
    </div>

    <!-- ═════ SERVER MODAL (create / edit) ═════ -->
    <Modal :show="!!serverModal" :title="serverModal && serverModal.isNew ? '添加 Server' : '编辑 Server'" @close="serverModal = null">
      <div class="field"><label>Name</label><input v-model="serverModal.name" placeholder="zabbix" :disabled="!serverModal.isNew" /></div>
      <div class="field"><label>MCP URL</label><input class="mono" v-model="serverModal.url" placeholder="http://localhost:8000/mcp" style="font-size:12.5px" /></div>
      <div class="field"><label>描述</label><input v-model="serverModal.desc" placeholder="Zabbix 监控 MCP" /></div>
      <template #footer>
        <button class="btn btn-ghost" @click="serverModal = null">取消</button>
        <button class="btn btn-primary" :disabled="saving" @click="saveServer">{{ saving ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { getServers, createServer, deleteServer, updateServer, pingServer, refreshTools } from '../api/index.js'
import Modal from '../components/Modal.vue'

/* ── state ── */
const servers = ref([])
const loading = ref(false)
const error = ref('')
const saving = ref(false)
const serverQuery = ref('')
const expanded = ref(null)
const serverModal = ref(null)

/* ── computed ── */
const filteredServers = computed(() =>
  servers.value.filter(s => s.name.includes(serverQuery.value))
)

/* ── helpers ── */
function countMode(s, mode) {
  return (s.tools || []).filter(t => t.mode === mode).length
}

function healthLed(h) {
  if (!h) return 'off'
  return h.up ? 'ok' : 'err'
}

function healthLatency(s) {
  const h = s.health
  if (!h) return '—'
  if (h.up && h.latency_ms != null) return h.latency_ms + 'ms'
  if (!h.up) return '超时'
  return '—'
}

/* ── actions ── */
async function load() {
  loading.value = true
  error.value = ''
  try {
    servers.value = await getServers()
  } catch (e) {
    error.value = '加载 Server 列表失败: ' + e.message
  } finally {
    loading.value = false
  }
}

function openCreateModal() {
  serverModal.value = { isNew: true, name: '', url: '', desc: '' }
}

function openEditModal(s) {
  serverModal.value = { isNew: false, name: s.name, url: s.url, desc: s.description || '' }
}

async function saveServer() {
  const m = serverModal.value
  if (!m || !m.name || !m.url) return
  saving.value = true
  try {
    if (m.isNew) {
      await createServer({ name: m.name, url: m.url, description: m.desc })
    } else {
      await updateServer(m.name, { url: m.url, description: m.desc })
    }
    serverModal.value = null
    await load()
  } catch (e) {
    error.value = '保存失败: ' + e.message
  } finally {
    saving.value = false
  }
}

async function doDelete(s) {
  if (!confirm(`确定删除 Server "${s.name}"? 此操作不可撤销。`)) return
  try {
    await deleteServer(s.name)
    expanded.value = null
    await load()
  } catch (e) {
    error.value = '删除失败: ' + e.message
  }
}

async function doPing(s) {
  try {
    const result = await pingServer(s.name)
    // Update the server entry in-place
    const idx = servers.value.findIndex(x => x.name === s.name)
    if (idx > -1) {
      servers.value[idx] = {
        ...servers.value[idx],
        health: { up: result.up, latency_ms: result.latency_ms, last_check: result.checked },
      }
    }
  } catch (e) {
    error.value = '探活失败: ' + e.message
  }
}

async function doRefreshTools(s) {
  try {
    const result = await refreshTools(s.name)
    const idx = servers.value.findIndex(x => x.name === s.name)
    if (idx > -1) {
      servers.value[idx] = { ...servers.value[idx], tools: result.tools }
    }
  } catch (e) {
    error.value = '刷新 Tools 失败: ' + e.message
  }
}

/* ── lifecycle ── */
onMounted(load)
</script>
