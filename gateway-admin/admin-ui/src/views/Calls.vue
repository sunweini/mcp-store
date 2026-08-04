<!-- src/views/Calls.vue - 请求日志页（读 MySQL calls 表，分页 + 过滤）
     字段对齐后端 GET /api/calls 返回：id/time/server/tool/op/token_name/latency_ms/status/error_type/trace -->
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
    <table>
      <thead><tr><th>时间</th><th>Server</th><th>Tool</th><th>Token</th><th>操作</th><th>耗时</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="c in calls" :key="c.id" :class="{ 'row-fail': c.status === 'fail' }">
          <td>{{ c.time }}</td><td>{{ c.server }}</td><td>{{ c.tool }}</td>
          <td>{{ c.token_name }}</td><td>{{ c.op === 'write' ? '写' : '读' }}</td>
          <td>{{ c.latency_ms }}ms</td>
          <td><span v-if="c.status === 'ok'" class="ok">✓</span>
              <span v-else class="fail">✗ {{ c.error_type }}</span></td>
        </tr>
      </tbody>
    </table>
    <div v-if="!calls.length" class="empty">暂无调用记录</div>
    <div class="pager">
      <button :disabled="offset === 0" @click="prev">上一页</button>
      <button :disabled="calls.length < limit" @click="next">下一页</button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getCalls } from '../api'
const servers = ['tavily-mcp', 'brave-mcp', 'serpapi-mcp', 'zabbix-mcp']
const calls = ref([])
const filterServer = ref('')
const filterStatus = ref('')
const limit = 50
const offset = ref(0)
async function reload() { offset.value = 0; await load() }
async function load() { calls.value = (await getCalls({ server: filterServer.value, status: filterStatus.value, limit, offset: offset.value })).data }
function prev() { offset.value = Math.max(0, offset.value - limit); load() }
function next() { offset.value += limit; load() }
onMounted(reload)
</script>

<style scoped>
.filters { margin-bottom: 16px; display: flex; gap: 12px; align-items: center; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }
.row-fail { background: rgba(255,90,90,0.06); }
.ok { color: #3fb950; } .fail { color: #f85149; }
.empty { padding: 32px; text-align: center; color: var(--muted); }
.pager { margin-top: 16px; display: flex; gap: 8px; }
</style>
