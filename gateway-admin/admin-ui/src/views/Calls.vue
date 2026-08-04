<!-- src/views/Calls.vue - 请求日志页（读 MySQL calls 表，分页 + 过滤）
     字段对齐后端 GET /api/calls 返回：id/time/server/tool/op/token_name/latency_ms/status/error_type/trace
     错误处理/loading/主题色对齐 APIKeys.vue 模式 -->
<template>
  <div>
    <h2>请求日志</h2>
    <div class="filters">
      <select v-model="filterServer" @change="reload">
        <option value="">全部 Server</option>
        <option v-for="s in servers" :key="s" :value="s">{{ s }}</option>
      </select>
      <select v-model="filterStatus" @change="reload">
        <option value="">全部状态</option>
        <option value="ok">成功</option>
        <option value="fail">失败</option>
      </select>
      <button class="btn" @click="reload">刷新</button>
    </div>

    <!-- Error banner（对齐 APIKeys.vue .err-banner） -->
    <div v-if="error" class="err-banner">{{ error }}</div>

    <!-- Loading（对齐 APIKeys.vue） -->
    <div v-if="loading" class="muted" style="padding:24px 0">加载中…</div>

    <!-- Table（用 .tbl 全局样式：圆角/panel/hover） -->
    <table v-else class="tbl">
      <thead><tr><th>时间</th><th>Server</th><th>Tool</th><th>Token</th><th>操作</th><th>耗时</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="c in calls" :key="c.id" :class="{ 'row-fail': c.status === 'fail' }">
          <td class="mono" style="font-size:11.5px">{{ fmtTime(c.time) }}</td><td>{{ c.server }}</td><td>{{ c.tool }}</td>
          <td>{{ c.token_name }}</td><td>{{ c.op === 'write' ? '写' : '读' }}</td>
          <td class="mono" style="font-size:12.5px">{{ c.latency_ms }}ms</td>
          <td><span v-if="c.status === 'ok'" class="ok">✓</span>
              <span v-else class="fail">✗ {{ c.error_type }}</span></td>
        </tr>
        <tr v-if="!calls.length && !loading">
          <td colspan="7" style="text-align:center;color:var(--muted);padding:24px 0">暂无调用记录</td>
        </tr>
      </tbody>
    </table>

    <div class="pager">
      <button class="btn btn-ghost" :disabled="offset === 0" @click="prev">上一页</button>
      <button class="btn btn-ghost" :disabled="calls.length < limit" @click="next">下一页</button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getCalls } from '../api'

/* ── state ── */
const servers = ['tavily-mcp', 'brave-mcp', 'serpapi-mcp', 'zabbix-mcp']
const calls = ref([])
const filterServer = ref('')
const filterStatus = ref('')
const limit = 50
const offset = ref(0)
const loading = ref(false)
const error = ref('')

/* ── helpers ── */
// 截取前 19 字符去掉毫秒部分（MySQL DATETIME(3) -> "YYYY-MM-DD HH:MM:SS"）
function fmtTime(t) {
  if (!t) return '-'
  return t.slice(0, 19)
}

/* ── actions ── */
async function reload() { offset.value = 0; await load() }

async function load() {
  loading.value = true
  error.value = ''
  try {
    calls.value = (await getCalls({ server: filterServer.value, status: filterStatus.value, limit, offset: offset.value })).data
  } catch (e) {
    error.value = '加载调用记录失败: ' + e.message
    calls.value = []
  } finally {
    loading.value = false
  }
}

async function prev() { offset.value = Math.max(0, offset.value - limit); await load() }
async function next() { offset.value += limit; await load() }

/* ── lifecycle ── */
onMounted(reload)
</script>

<style scoped>
.filters { margin-bottom: 16px; display: flex; gap: 12px; align-items: center; }
/* .tbl 全局样式已提供圆角/panel 背景/hover；这里只补充列宽与行高微调 */
.tbl th, .tbl td { padding: 8px 10px; font-size: 13px; }
.row-fail { background: var(--err-dim); }
.row-fail:hover { background: color-mix(in srgb, var(--err) 16%, transparent); }
.ok { color: var(--ok); font-weight: 600; }
.fail { color: var(--err); font-weight: 600; }
.pager { margin-top: 16px; display: flex; gap: 8px; }

/* 错误提示条（对齐 APIKeys.vue .err-banner 风格） */
.err-banner {
  background: var(--err-dim); border: 1px solid color-mix(in srgb, var(--err) 35%, transparent);
  color: var(--err); border-radius: 9px; padding: 9px 12px; font-size: 12.5px; margin-bottom: 16px;
}
</style>
