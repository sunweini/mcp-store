<!-- src/views/APIKeys.vue — 搜索 MCP API key 管理页（tavily / brave / serpapi key 池）
     结构对齐 Tokens.vue：.tbl 表格 + Modal + .err-banner 错误提示 + mini-btn 行操作 -->
<template>
  <div>
    <!-- Top action bar: 源 tab + 添加按钮 -->
    <div class="table-actions">
      <div class="tabs">
        <button
          v-for="p in providers"
          :key="p.id"
          class="tab"
          :class="{ active: active === p.id }"
          @click="switchTab(p.id)"
        >
          {{ p.label }}
          <span v-if="p.warnCount" class="badge" :title="`${p.warnCount} 个 key 低配额/失效`">{{ p.warnCount }}</span>
        </button>
      </div>
      <button class="btn btn-primary" @click="openAdd">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
        添加 Key
      </button>
    </div>

    <!-- Notice / error banners -->
    <div v-if="notice" class="ok-banner">{{ notice }}</div>
    <div v-if="error" class="err-banner">{{ error }}</div>

    <!-- Loading -->
    <div v-if="loading" class="muted" style="padding:24px 0">加载中…</div>

    <!-- Key table -->
    <table v-else class="tbl">
      <thead>
        <tr>
          <th style="width:30%">Key</th>
          <th style="width:12%">状态</th>
          <th style="width:20%">剩余配额</th>
          <th style="width:10%">本月用量</th>
          <th style="width:14%">最后使用</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="k in currentKeys"
          :key="k.key_id"
          :class="{ 'row-warn': k.status === 'low_quota_warning', 'row-critical': k.status === 'low_quota' }"
        >
          <td class="mono" style="font-size:12.5px">{{ k.key_masked }}</td>
          <td><span class="status-chip" :class="statusClass(k)">{{ statusLabel(k) }}</span></td>
          <td class="mono" style="font-size:12.5px">{{ quotaText(k) }}</td>
          <td>{{ k.month_usage }}</td>
          <td class="mono" style="font-size:11.5px;color:var(--muted)">{{ fmtDate(k.last_used_at) }}</td>
          <td>
            <div class="row-actions">
              <button class="mini-btn" @click="toggleKey(k)">{{ k.enabled ? '停用' : '启用' }}</button>
              <button class="mini-btn danger" @click="removeKey(k)">删除</button>
            </div>
          </td>
        </tr>
        <tr v-if="!currentKeys.length && !loading">
          <td colspan="6" style="text-align:center;color:var(--muted);padding:24px 0">暂无 Key · 点击"添加 Key"录入第一个</td>
        </tr>
      </tbody>
    </table>

    <!-- ═════ ADD KEY MODAL ═════ -->
    <Modal :show="!!addModal" title="添加 Key" @close="closeAdd">
      <div class="field">
        <label>Key 明文</label>
        <input class="mono" v-model="addModal.key" placeholder="tvly-… / BSA… / serp-…" style="font-size:12.5px" />
      </div>
      <div class="field">
        <label>每月配额（可选，留空使用源默认：tavily 1000 / brave 2000 / serpapi 100）</label>
        <input type="number" min="1" v-model="addModal.quota" placeholder="留空使用默认" />
      </div>
      <div class="muted" style="font-size:12px">保存时将自动探活验证 key 有效性（最长约 10s，消耗 1 次官方配额）。</div>

      <template #footer>
        <button class="btn btn-ghost" @click="closeAdd">取消</button>
        <button class="btn btn-primary" :disabled="saving || !addModal.key.trim()" @click="submitAdd">
          {{ saving ? '探活验证中…' : '添加' }}
        </button>
      </template>
    </Modal>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { getSearchKeys, addSearchKey, updateSearchKey, deleteSearchKey, getSearchKeyUsage } from '../api/index.js'
import Modal from '../components/Modal.vue'

/* ── 状态映射（集中管理，页面其它地方不散落状态字符串）──
   active 正常 / low_quota_warning 低配额(<10%) 橙 / low_quota 即将耗尽(<5%) 深红
   invalid 失效 / exhausted 欠费 → 灰；cooldown 冷却中 → 黄 */
const STATUS_CLASS = {
  active: 'ok',
  low_quota_warning: 'orange',
  low_quota: 'critical',
  invalid: 'off',
  exhausted: 'off',
  cooldown: 'cooldown',
}
const STATUS_LABEL = {
  active: '正常',
  low_quota_warning: '低配额',
  low_quota: '即将耗尽',
  invalid: '失效',
  exhausted: '欠费',
  cooldown: '冷却中',
}
// tab 角标统计的状态集合
const WARN_STATUSES = ['low_quota_warning', 'low_quota', 'invalid', 'exhausted']

/* ── state ── */
const active = ref('tavily')
const providers = reactive([
  { id: 'tavily', label: 'Tavily', warnCount: 0, keys: [] },
  { id: 'brave', label: 'Brave', warnCount: 0, keys: [] },
  { id: 'serpapi', label: 'SerpAPI', warnCount: 0, keys: [] },
])
// 每源 usage 索引：key_id → { remaining, month_quota, ratio }
const usageMap = ref({ tavily: {}, brave: {}, serpapi: {} })
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const addModal = ref(null)

/* ── computed ── */
const currentKeys = computed(() => providers.find(p => p.id === active.value).keys)

/* ── helpers ── */
function statusClass(k) {
  if (!k.enabled) return 'off'  // 已停用的 key 统一灰态，状态语义让位于启停开关
  return STATUS_CLASS[k.status] || 'off'
}
function statusLabel(k) {
  if (!k.enabled) return '已停用'
  return STATUS_LABEL[k.status] || k.status
}
function quotaText(k) {
  const u = usageMap.value[active.value][k.key_id]
  if (!u || u.remaining == null) return '—'
  const pct = Math.round((u.remaining / u.month_quota) * 100)
  return `${pct}%（${u.remaining}/${u.month_quota}）`
}
function fmtDate(iso) {
  if (!iso) return '—'
  return iso.slice(0, 10) // YYYY-MM-DD
}
function countWarn(list) {
  return list.filter(k => k.enabled && WARN_STATUSES.includes(k.status)).length
}

/* ── actions ── */
async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    // 并行拉 3 源 list + usage
    const [lists, usages] = await Promise.all([
      Promise.all(providers.map(p => getSearchKeys(p.id))),
      Promise.all(providers.map(p => getSearchKeyUsage(p.id))),
    ])
    providers.forEach((p, i) => {
      p.keys = lists[i]
      p.warnCount = countWarn(lists[i])
      usageMap.value[p.id] = Object.fromEntries((usages[i].keys || []).map(u => [u.key_id, u]))
    })
  } catch (e) {
    error.value = '加载 Key 列表失败: ' + e.message
  } finally {
    loading.value = false
  }
}

// 单源刷新（增删改后只需重拉当前 tab，比全量快）
async function loadCurrent() {
  const p = active.value
  try {
    const [list, usage] = await Promise.all([getSearchKeys(p), getSearchKeyUsage(p)])
    const prov = providers.find(x => x.id === p)
    prov.keys = list
    prov.warnCount = countWarn(list)
    usageMap.value[p] = Object.fromEntries((usage.keys || []).map(u => [u.key_id, u]))
  } catch (e) {
    error.value = '刷新失败: ' + e.message
  }
}

function switchTab(id) {
  active.value = id
  notice.value = ''
}

function openAdd() {
  addModal.value = { key: '', quota: '' }
}

function closeAdd() {
  if (saving.value) return  // 探活进行中禁止关闭，防止误操作半途中断
  addModal.value = null
}

async function submitAdd() {
  const m = addModal.value
  if (!m || !m.key.trim()) return
  const data = { key: m.key.trim() }
  if (m.quota) data.monthly_quota = Number(m.quota)
  saving.value = true
  error.value = ''
  try {
    // 后端自动探活（真实外网请求，最长 ~10s），期间按钮 loading 防重复提交
    const rec = await addSearchKey(active.value, data)
    closeAdd()
    await loadCurrent()
    notice.value = rec.status === 'invalid'
      ? `添加成功 · 探活已消耗 1 次配额（key 未通过探活，状态为"失效"，可删除后重试）`
      : `添加成功 · 探活已消耗 1 次配额`
  } catch (e) {
    error.value = '添加失败: ' + e.message
  } finally {
    saving.value = false
  }
}

async function toggleKey(k) {
  try {
    await updateSearchKey(active.value, k.key_id, { enabled: !k.enabled })
    await loadCurrent()
  } catch (e) {
    error.value = '更新失败: ' + e.message
  }
}

async function removeKey(k) {
  if (!confirm(`确定删除 Key "${k.key_masked}"? 此操作不可撤销。`)) return
  try {
    await deleteSearchKey(active.value, k.key_id)
    await loadCurrent()
  } catch (e) {
    error.value = '删除失败: ' + e.message
  }
}

/* ── lifecycle ── */
onMounted(loadAll)
</script>

<style scoped>
/* ── 源 tab ── */
.tabs { display: flex; gap: 6px; flex-wrap: wrap; }
.tab {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 8px 14px; border-radius: 9px; border: 1px solid var(--border);
  background: transparent; color: var(--muted); font-size: 13px; font-weight: 600;
  transition: color 0.15s, background 0.15s, border-color 0.15s;
}
.tab:hover { color: var(--text); border-color: var(--border-strong); }
.tab.active { background: var(--accent-dim); color: var(--accent); border-color: transparent; }
.badge {
  min-width: 17px; height: 17px; padding: 0 5px; border-radius: 999px;
  background: var(--err); color: #fff; font-size: 10px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center;
}

/* ── 状态 chip 扩展色（ok/warn/err 在 base.css 全局）──
   橙色由 warn+err 混出（色板无独立橙变量，不新造色系） */
.status-chip.orange {
  color: color-mix(in srgb, var(--warn) 55%, var(--err) 45%);
  background: color-mix(in srgb, color-mix(in srgb, var(--warn) 55%, var(--err) 45%) 14%, transparent);
  border-color: color-mix(in srgb, color-mix(in srgb, var(--warn) 55%, var(--err) 45%) 30%, transparent);
}
.status-chip.critical {
  color: var(--err);
  background: color-mix(in srgb, var(--err) 18%, transparent);
  border-color: color-mix(in srgb, var(--err) 40%, transparent);
}
.status-chip.off { color: var(--faint); background: color-mix(in srgb, var(--faint) 10%, transparent); }
.status-chip.cooldown { color: var(--warn); background: var(--warn-dim); }

/* ── 行样式：低配额橙边 / 即将耗尽深红底（hover 保持行色，不被全局 hover 覆盖）── */
.tbl tbody tr.row-warn {
  background: color-mix(in srgb, var(--warn) 7%, transparent);
  box-shadow: inset 3px 0 0 color-mix(in srgb, var(--warn) 55%, var(--err) 45%);
}
.tbl tbody tr.row-warn:hover { background: color-mix(in srgb, var(--warn) 11%, transparent); }
.tbl tbody tr.row-critical {
  background: color-mix(in srgb, var(--err) 12%, transparent);
  box-shadow: inset 3px 0 0 var(--err);
}
.tbl tbody tr.row-critical:hover { background: color-mix(in srgb, var(--err) 17%, transparent); }

/* ── 成功提示条（对齐 .err-banner 风格）── */
.ok-banner {
  background: var(--ok-dim); border: 1px solid color-mix(in srgb, var(--ok) 35%, transparent);
  color: var(--ok); border-radius: 9px; padding: 9px 12px; font-size: 12.5px; margin-bottom: 16px;
}
</style>
