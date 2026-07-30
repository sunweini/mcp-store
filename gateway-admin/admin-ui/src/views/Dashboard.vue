<!-- src/views/Dashboard.vue -->
<template>
  <div>
    <!-- ═════ STATUSBOARD ═════ -->
    <div class="statusboard">
      <div class="statusboard-head">
        <div class="statusboard-title">
          <h2>Gateway 流量</h2>
          <span class="live-chip"><StatusLed status="ok" :pulse="true" /> live</span>
        </div>
        <span class="hint mono" style="font-size:10px;color:var(--faint)">last 60 min · {{ scopeName }}</span>
      </div>
      <div class="spark-row">
        <div>
          <div class="spark-big">{{ fmt(summary.requests) }}<span class="unit">req</span></div>
          <div class="spark-label">total · last hour</div>
        </div>
        <div class="sparkline-wrap" v-if="sparkPts.length">
          <svg viewBox="0 0 300 64" preserveAspectRatio="none">
            <defs>
              <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.28"/>
                <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
              </linearGradient>
            </defs>
            <path :d="sparkArea" fill="url(#sparkFill)"/>
            <path :d="sparkLine" fill="none" stroke="var(--accent)" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>
            <circle v-if="sparkEnd" class="spark-dot" :cx="sparkEnd.x" :cy="sparkEnd.y" r="3"/>
          </svg>
        </div>
        <div v-else class="sparkline-wrap" style="display:flex;align-items:center;justify-content:center">
          <span class="mono" style="color:var(--faint);font-size:11px">暂无数据</span>
        </div>
      </div>
    </div>

    <!-- ═════ SERVER FILTER CHIPS ═════ -->
    <div class="chip-row">
      <button class="chip" :class="{active: scope==='all'}" @click="setScope('all')">
        全部 <span class="cnt">{{ servers.length }}</span>
      </button>
      <button v-for="s in servers" :key="s.name" class="chip"
              :class="{active: scope===s.name}" @click="setScope(s.name)">
        <span class="led" :class="srvStatus(s)" style="width:6px;height:6px"></span>{{ s.name }}
      </button>
    </div>

    <!-- ═════ STAT GRID ═════ -->
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-top"><span class="stat-name">总请求</span></div>
        <div class="stat-value">{{ fmt(summary.requests) }}</div>
        <div class="stat-sub">过去 24 小时 · {{ scopeName }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-top"><span class="stat-name">错误数</span></div>
        <div class="stat-value danger">{{ summary.errors }}</div>
        <div class="stat-sub">错误率 {{ summary.error_rate }}%</div>
      </div>
      <div class="stat-card">
        <div class="stat-top"><span class="stat-name">P95 延迟</span></div>
        <div class="stat-value">{{ summary.p95_ms }}ms</div>
        <div class="stat-sub">gateway → server</div>
      </div>
      <div class="stat-card">
        <div class="stat-top"><span class="stat-name">鉴权失败</span></div>
        <div class="stat-value">{{ summary.auth_failures }}</div>
        <div class="stat-sub">invalid / denied</div>
      </div>
    </div>

    <!-- ═════ FAILURE DETAIL (when selected) ═════ -->
    <div v-if="selFail" class="fail-detail">
      <div class="detail-head">
        <div class="d-title">
          <h3>请求轨迹</h3>
          <span class="fail-type" :class="errSev(selFail.error_type)">{{ errLabel(selFail.error_type) }}</span>
          <span class="mono" style="font-size:10px;color:var(--faint)">trace {{ selFail.trace }}</span>
        </div>
        <button class="close-x" @click="selFail=null">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>

      <div class="journey">
        <div v-for="(st, i) in journeySteps" :key="i" class="j-step" :class="st.state">
          <div class="j-node">
            <span v-if="st.state==='fail'">&#10005;</span>
            <span v-else-if="st.state==='ok'">&#10003;</span>
          </div>
          <div class="j-label">{{ st.label }}</div>
          <div class="j-ms">{{ st.ms }}</div>
        </div>
      </div>

      <div class="err-card">
        <div class="e-type"><span class="led err"></span> {{ errLabel(selFail.error_type) }}</div>
        <div class="e-msg">{{ selFail.message }}</div>
      </div>

      <div class="meta-grid">
        <div class="meta-item"><span class="mk">Trace ID</span><span class="mv">{{ selFail.trace }}</span></div>
        <div class="meta-item"><span class="mk">Server</span><span class="mv">{{ selFail.server }}</span></div>
        <div class="meta-item"><span class="mk">Tool</span><span class="mv">{{ selFail.tool }}</span></div>
        <div class="meta-item"><span class="mk">Operation</span><span class="mv">{{ selFail.op }}</span></div>
        <div class="meta-item"><span class="mk">耗时</span><span class="mv">{{ selFail.latency_ms }}ms</span></div>
        <div class="meta-item"><span class="mk">发生时间</span><span class="mv">{{ selFail.time }}</span></div>
      </div>
      <button class="copy-btn" @click="copyTrace(selFail)">&#10729; 复制 Trace ID 到剪贴板</button>
    </div>

    <!-- ═════ PANEL GRID: failure feed + per-server stats ═════ -->
    <div class="panel-grid">
      <!-- failure feed -->
      <div class="panel">
        <div class="panel-head">
          <h3>失败请求 <span class="mono" style="font-size:10px;color:var(--faint);font-weight:400">&middot; 点击查看轨迹</span></h3>
          <span class="hint">{{ displayFailures.length }} FAILED</span>
        </div>
        <div class="fail-feed" v-if="!loading.failures">
          <button v-for="f in displayFailures" :key="f.trace" class="fail-item"
                  :class="{sel: selFail && selFail.trace===f.trace}" @click="selFail=f">
            <span class="led err"></span>
            <div class="fail-main">
              <div class="fail-title">
                <span class="t-srv">{{ f.server }}</span>
                <span style="color:var(--faint)">/</span>
                <span class="t-tool">{{ f.tool }}</span>
                <span class="t-op" :class="f.op">{{ f.op }}</span>
              </div>
              <div class="fail-msg">{{ f.message }}</div>
            </div>
            <span class="fail-type" :class="errSev(f.error_type)">{{ errLabel(f.error_type) }}</span>
            <span class="fail-time">{{ ago(f.time) }}</span>
          </button>
          <div class="fail-item muted" style="font-size:12.5px;padding:12px 4px;justify-content:center;cursor:default"
               v-if="!displayFailures.length && !loading.failures">
            该范围内暂无失败请求 &#10003;
          </div>
        </div>
        <div v-else class="muted" style="font-size:12.5px;padding:12px 4px">加载中…</div>
      </div>

      <!-- per-server stats -->
      <div class="panel">
        <div class="panel-head"><h3>分 Server 统计</h3><span class="hint">24H</span></div>
        <table class="srv-tbl">
          <thead><tr>
            <th>Server</th><th class="num">请求</th><th class="num">错误</th><th class="num">错误率</th><th class="num">P95</th>
          </tr></thead>
          <tbody v-if="!loading.byServer">
            <tr v-for="s in serverStats" :key="s.server"
                :class="{sel: scope===s.server}" @click="setScope(s.server)">
              <td>
                <span class="sname">
                  <span class="led" :class="srvStatus(srvByName(s.server))"></span>
                  {{ s.server }}
                </span>
              </td>
              <td class="num">{{ fmt(s.requests) }}</td>
              <td class="num" :class="{'err-cell': s.errors>0}">{{ s.errors }}</td>
              <td class="num">
                {{ s.error_rate }}%
                <span class="rate-bar"><i :style="{width: Math.min(s.error_rate*8,100)+'%'}"></i></span>
              </td>
              <td class="num">{{ s.p95_ms }}ms</td>
            </tr>
            <tr v-if="!serverStats.length">
              <td colspan="5" style="text-align:center;color:var(--faint);padding:24px">暂无 server 数据</td>
            </tr>
          </tbody>
          <tbody v-else>
            <tr><td colspan="5" style="text-align:center;color:var(--faint);padding:24px">加载中…</td></tr>
          </tbody>
        </table>

        <!-- op split (read/write) -->
        <div style="margin-top:16px">
          <div class="op-split">
            <div class="op-seg">
              <div class="seg-read" :style="{width: readPct+'%'}"></div>
              <div class="seg-write" :style="{width: (100 - readPct)+'%'}"></div>
            </div>
            <div class="op-legend">
              <div class="op-legend-item"><span class="op-dot read"></span><span class="op-name">Read 操作</span><span class="op-count">{{ fmt(summary.read) }}</span><span class="op-pct">{{ readPct }}%</span></div>
              <div class="op-legend-item"><span class="op-dot write"></span><span class="op-name">Write 操作</span><span class="op-count">{{ fmt(summary.write) }}</span><span class="op-pct">{{ 100 - readPct }}%</span></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═════ TIMELINE ═════ -->
    <div class="panel timeline-panel">
      <div class="panel-head"><h3>请求时间线</h3><span class="hint">1H &middot; 1MIN BUCKETS</span></div>
      <svg v-if="tlPts.length" class="timeline" viewBox="0 0 600 120" preserveAspectRatio="none">
        <defs>
          <linearGradient id="tlFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="var(--read)" stop-opacity="0.25"/>
            <stop offset="100%" stop-color="var(--read)" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <line v-for="i in 3" :key="i" :x1="0" :x2="600" :y1="i*30" :y2="i*30" stroke="var(--border)" stroke-width="1" stroke-dasharray="3 5"/>
        <path :d="timelineArea" fill="url(#tlFill)"/>
        <path :d="timelineLine" fill="none" stroke="var(--read)" stroke-width="1.6" stroke-linejoin="round"/>
      </svg>
      <div v-else class="muted" style="font-size:12.5px;padding:12px 4px;height:120px;display:flex;align-items:center;justify-content:center">暂无时间线数据</div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import StatusLed from '../components/StatusLed.vue'
import {
  getMetricsSummary, getMetricsByServer, getMetricsTimeseries,
  getFailures, getServers,
} from '../api/index.js'

/* ── reactive state ── */
const scope = ref('all')
const selFail = ref(null)

const servers = ref([])
const summary = ref({ requests: 0, errors: 0, error_rate: 0, p95_ms: 0, read: 0, write: 0, auth_failures: 0 })
const serverStats = ref([])    // from getMetricsByServer
const timeseries = ref([])     // raw points from getMetricsTimeseries
const failures = ref([])

const loading = reactive({
  summary: true, byServer: true, timeseries: true, failures: true, servers: true,
})

/* ── derived ── */
const scopeName = computed(() => scope.value === 'all' ? '全部 Server' : scope.value)

const srvByName = computed(() => {
  const map = {}
  servers.value.forEach(s => { map[s.name] = s })
  return map
})

function srvStatus(s) {
  if (!s) return 'off'
  if (s.health && !s.health.up) return 'err'
  if (s.health && s.health.latency_ms !== null && s.health.latency_ms > 200) return 'warn'
  if (s.health && s.health.up) return 'ok'
  return 'off'
}

const readPct = computed(() => {
  const total = summary.value.read + summary.value.write
  if (!total) return 50
  return Math.round(summary.value.read / total * 100)
})

const displayFailures = computed(() => {
  if (scope.value === 'all') return failures.value
  return failures.value.filter(f => f.server === scope.value)
})

const journeySteps = computed(() => {
  const f = selFail.value
  if (!f) return []
  const journey = f.journey || []

  // If journey items are objects with {label, state, ms}, use them directly
  if (journey.length && typeof journey[0] === 'object') {
    return journey.map(j => ({
      label: j.label || '',
      state: j.state || 'skip',
      ms: j.ms != null ? (j.ms + 'ms') : '—',
    }))
  }

  // Fallback: journey is array of strings (labels), compute from failAt/ms
  const steps = journey.length ? journey : ['client', 'gateway', 'auth', 'route', f.server]
  const failAt = f.journey_fail_at != null ? f.journey_fail_at : (typeof f.failAt !== 'undefined' ? f.failAt : steps.length)
  const ms = f.journey_ms || f.ms || steps.map(() => 0)
  return steps.map((label, i) => ({
    label: typeof label === 'string' ? label : '',
    state: i < failAt ? 'ok' : (i === failAt ? 'fail' : 'skip'),
    ms: i <= failAt && ms[i] != null ? ms[i] + 'ms' : '—',
  }))
})

/* ── sparkline ── */
const sparkPts = computed(() => {
  const pts = timeseries.value
  if (!pts.length) return []
  const w = 300, h = 64
  const max = Math.max(...pts, 0.001)
  const min = Math.min(...pts)
  const range = (max - min) || 1
  return pts.map((v, i) => ({
    x: (i / (pts.length - 1)) * w,
    y: h - 6 - ((v - min) / range) * (h - 14),
  }))
})

const sparkLine = computed(() => {
  if (!sparkPts.value.length) return ''
  return 'M' + sparkPts.value.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' L')
})

const sparkArea = computed(() => sparkLine.value + ' L300,64 L0,64 Z')

const sparkEnd = computed(() => {
  const pts = sparkPts.value
  return pts.length ? pts[pts.length - 1] : null
})

/* ── timeline ── */
const tlPts = computed(() => {
  const pts = timeseries.value
  if (!pts.length) return []
  const w = 600, h = 120
  const max = Math.max(...pts, 0.001)
  return pts.map((v, i) => ({
    x: (i / (pts.length - 1)) * w,
    y: h - 8 - (v / max) * (h - 22),
  }))
})

const timelineLine = computed(() => {
  if (!tlPts.value.length) return ''
  return 'M' + tlPts.value.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' L')
})

const timelineArea = computed(() => timelineLine.value + ' L600,120 L0,120 Z')

/* ── helpers ── */
const fmt = (n) => (n != null ? Number(n).toLocaleString('en-US') : '0')

function ago(ts) {
  if (!ts) return ''
  try {
    const then = new Date(ts).getTime()  // ISO string or epoch ms
    const diff = (Date.now() - then) / 1000
    if (diff < 60) return Math.floor(diff) + ' 秒前'
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前'
    return Math.floor(diff / 3600) + ' 小时前'
  } catch { return ts }
}

function errSev(t) {
  return (t === 'upstream_timeout' || t === 'permission_denied' || t === 'timeout' || t === 'unauthorized') ? 'warn' : 'err'
}

function errLabel(t) {
  const map = {
    upstream_timeout: '上游超时', permission_denied: '权限拒绝', invalid_token: 'Token 无效',
    upstream_error: '上游业务错误', connection_error: '连接失败',
    timeout: '超时', unauthorized: '未授权',
  }
  return map[t] || t || '未知错误'
}

function copyTrace(f) {
  navigator.clipboard && navigator.clipboard.writeText(f.trace)
}

/* ── scope change trigger ── */
const setScope = (v) => { scope.value = v; fetchData() }

/* ── data fetching ── */
async function fetchData(retry = 0) {
  const s = scope.value === 'all' ? null : scope.value
  try {
    /* summary + timeseries + failures + serverStats in parallel */
    const all = await Promise.allSettled([
      getMetricsSummary(s),
      getMetricsTimeseries(s, '1h'),
      getFailures(s, 50, 0),
      getMetricsByServer(),
    ])

    const [sumResult, tsResult, failResult, byServerResult] = all

    if (sumResult.status === 'fulfilled') {
      summary.value = {
        requests: sumResult.value.requests ?? 0,
        errors: sumResult.value.errors ?? 0,
        error_rate: sumResult.value.error_rate ?? 0,
        p95_ms: sumResult.value.p95_ms ?? 0,
        read: sumResult.value.read ?? 0,
        write: sumResult.value.write ?? 0,
        auth_failures: sumResult.value.auth_failures ?? 0,
      }
      loading.summary = false
    }

    if (tsResult.status === 'fulfilled') {
      timeseries.value = tsResult.value.points || []
      loading.timeseries = false
    }

    if (failResult.status === 'fulfilled') {
      failures.value = failResult.value || []
      loading.failures = false
    }

    if (byServerResult.status === 'fulfilled') {
      serverStats.value = byServerResult.value || []
      loading.byServer = false
    }
  } catch {
    /* Retry once on total failure (e.g. network down) */
    if (retry < 2) {
      setTimeout(() => fetchData(retry + 1), 2000)
    }
  }
}

async function fetchServers() {
  try {
    const data = await getServers()
    servers.value = data || []
    loading.servers = false
  } catch {
    loading.servers = false
  }
}

/* ── lifecycle ── */
onMounted(() => {
  fetchServers()
  fetchData()
})
</script>

<style scoped>
/* ── Copied from docs/superpowers/mockups/gateway-admin.html ── */

/* statusboard */
.statusboard {
  background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
  padding: 22px 24px 16px; margin-bottom: 18px; position: relative; overflow: hidden;
}
.statusboard-head { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 4px; }
.statusboard-title { display: flex; align-items: center; gap: 10px; }
.statusboard-title h2 { font-family: var(--font-display); font-size: 15px; font-weight: 600; }
.live-chip { display: inline-flex; align-items: center; gap: 6px; font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.1em; color: var(--ok); background: var(--ok-dim); padding: 3px 9px; border-radius: 999px; text-transform: uppercase; }
.spark-row { display: flex; align-items: flex-end; gap: 28px; flex-wrap: wrap; }
.spark-big { font-family: var(--font-display); font-weight: 700; font-size: 44px; letter-spacing: -0.02em; line-height: 1; font-variant-numeric: tabular-nums; }
.spark-big .unit { font-size: 17px; color: var(--muted); font-weight: 600; margin-left: 6px; }
.spark-label { font-family: var(--font-mono); font-size: 10px; color: var(--faint); letter-spacing: 0.1em; text-transform: uppercase; margin-top: 6px; }
.sparkline-wrap { flex: 1; min-width: 240px; height: 64px; align-self: flex-end; }
.sparkline-wrap svg { width: 100%; height: 64px; display: block; }
.spark-dot { fill: var(--accent); filter: drop-shadow(0 0 5px var(--accent)); animation: dotpulse 1.6s ease-in-out infinite; }
@keyframes dotpulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }

/* server filter chips */
.chip-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
.chip {
  display: inline-flex; align-items: center; gap: 8px;
  border-radius: 999px; border: 1px solid var(--border);
  background: var(--panel); color: var(--muted);
  font-family: var(--font-mono); font-size: 12px; padding: 6px 14px;
  transition: border-color 0.15s, color 0.15s, background 0.15s, box-shadow 0.15s;
  cursor: pointer;
}
.chip:hover { color: var(--text); border-color: var(--border-strong); }
.chip.active { background: var(--accent-dim); border-color: var(--accent); color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 8%, transparent); }
.chip .cnt { font-size: 10px; opacity: 0.7; }

/* stat grid */
.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 18px; }
@media (max-width: 960px) { .stat-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 560px) { .stat-grid { grid-template-columns: 1fr; } }
.stat-card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; transition: transform 0.18s, border-color 0.18s, box-shadow 0.18s; }
.stat-card:hover { transform: translateY(-2px); border-color: var(--border-strong); box-shadow: var(--shadow); }
.stat-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.stat-name { font-family: var(--font-mono); font-size: 10px; color: var(--faint); letter-spacing: 0.1em; text-transform: uppercase; }
.stat-value { font-family: var(--font-display); font-size: 28px; font-weight: 700; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.stat-value.danger { color: var(--err); }
.stat-sub { font-size: 11.5px; color: var(--muted); margin-top: 3px; }

/* panel grid */
.panel-grid { display: grid; grid-template-columns: 1.35fr 1fr; gap: 14px; margin-bottom: 18px; }
@media (max-width: 900px) { .panel-grid { grid-template-columns: 1fr; } }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; }
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.panel-head h3 { font-family: var(--font-display); font-size: 14px; font-weight: 600; }
.panel-head .hint { font-family: var(--font-mono); font-size: 10px; color: var(--faint); letter-spacing: 0.08em; }

/* per-server table */
.srv-tbl { width: 100%; border-collapse: collapse; }
.srv-tbl th {
  font-family: var(--font-mono); font-size: 9.5px; text-transform: uppercase;
  letter-spacing: 0.1em; color: var(--faint); text-align: left;
  padding: 6px 8px; border-bottom: 1px solid var(--border); font-weight: 500;
}
.srv-tbl th.num, .srv-tbl td.num { text-align: right; }
.srv-tbl td { padding: 10px 8px; border-bottom: 1px solid var(--border); font-size: 12.5px; vertical-align: middle; }
.srv-tbl tr:last-child td { border-bottom: none; }
.srv-tbl tbody tr { cursor: pointer; transition: background 0.15s; }
.srv-tbl tbody tr:hover { background: var(--panel-2); }
.srv-tbl tbody tr.sel { background: var(--accent-dim); }
.srv-tbl .sname { display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-weight: 500; }
.srv-tbl .num { font-family: var(--font-mono); font-variant-numeric: tabular-nums; color: var(--muted); }
.srv-tbl .num.err-cell { color: var(--err); font-weight: 500; }
.rate-bar { display: inline-block; width: 46px; height: 4px; border-radius: 99px; background: var(--panel-2); overflow: hidden; vertical-align: middle; margin-left: 8px; }
.rate-bar i { display: block; height: 100%; background: var(--err); border-radius: 99px; }

/* failure feed */
.fail-feed { display: flex; flex-direction: column; gap: 8px; }
.fail-item {
  display: flex; align-items: center; gap: 12px;
  border: 1px solid var(--border); border-radius: 10px;
  padding: 11px 14px; background: var(--panel-2);
  cursor: pointer; text-align: left; width: 100%;
  transition: border-color 0.15s, transform 0.15s, background 0.15s;
  font-family: inherit; font-size: inherit; color: inherit;
}
.fail-item:hover { border-color: var(--border-strong); transform: translateX(3px); }
.fail-item.sel { border-color: var(--err); background: color-mix(in srgb, var(--err) 5%, var(--panel-2)); }
.fail-main { flex: 1; min-width: 0; }
.fail-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fail-title .t-srv { font-family: var(--font-mono); font-size: 11px; color: var(--accent); }
.fail-title .t-tool { font-family: var(--font-mono); font-size: 12px; font-weight: 500; }
.fail-title .t-op { font-family: var(--font-mono); font-size: 9px; letter-spacing: 0.08em; padding: 1px 6px; border-radius: 4px; text-transform: uppercase; }
.t-op.read { color: var(--read); background: color-mix(in srgb, var(--read) 12%, transparent); }
.t-op.write { color: var(--warn); background: var(--warn-dim); }
.fail-msg { font-size: 11.5px; color: var(--muted); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fail-type {
  font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.05em;
  padding: 3px 9px; border-radius: 6px; flex-shrink: 0; white-space: nowrap;
}
.fail-type.err { color: var(--err); background: var(--err-dim); }
.fail-type.warn { color: var(--warn); background: var(--warn-dim); }
.fail-time { font-family: var(--font-mono); font-size: 10px; color: var(--faint); flex-shrink: 0; width: 58px; text-align: right; }

/* failure detail (journey) */
.fail-detail { background: var(--panel); border: 1px solid var(--border); border-left: 3px solid var(--err); border-radius: 12px; padding: 20px 22px; margin-bottom: 18px; animation: detail-in 0.3s cubic-bezier(0.22,1,0.36,1); }
@keyframes detail-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
.detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 6px; }
.detail-head .d-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.detail-head h3 { font-family: var(--font-display); font-size: 15px; font-weight: 600; }
.close-x { width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; border-radius: 7px; border: 1px solid var(--border); background: transparent; color: var(--muted); transition: color .15s, border-color .15s; flex-shrink: 0; cursor: pointer; }
.close-x:hover { color: var(--text); border-color: var(--border-strong); }

.journey { display: flex; align-items: flex-start; padding: 20px 4px 6px; }
.j-step { flex: 1; display: flex; flex-direction: column; align-items: center; position: relative; min-width: 0; }
.j-step::after { content: ""; position: absolute; top: 9px; left: 50%; width: 100%; height: 0; z-index: 0; }
.j-step:last-child::after { display: none; }
.j-step.ok::after { border-top: 2px solid var(--border-strong); }
.j-step.fail::after { border-top: 2px dashed var(--err); opacity: 0.55; }
.j-step.skip::after { border-top: 2px dashed var(--border-strong); opacity: 0.35; }
.j-node {
  width: 20px; height: 20px; border-radius: 50%; position: relative; z-index: 1;
  background: var(--panel); border: 2px solid var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-size: 9px; font-weight: 700; color: var(--accent);
}
.j-step.fail .j-node { border-color: var(--err); background: var(--err-dim); color: var(--err); box-shadow: 0 0 12px var(--err-dim); animation: failpulse 1.4s ease-in-out infinite; }
@keyframes failpulse { 0%,100% { box-shadow: 0 0 4px var(--err-dim); } 50% { box-shadow: 0 0 14px var(--err); } }
.j-step.skip .j-node { border-color: var(--border-strong); opacity: 0.4; }
.j-label { margin-top: 9px; font-family: var(--font-mono); font-size: 9.5px; color: var(--muted); letter-spacing: 0.07em; text-transform: uppercase; text-align: center; }
.j-step.fail .j-label { color: var(--err); font-weight: 600; }
.j-step.skip .j-label { opacity: 0.35; }
.j-ms { font-family: var(--font-mono); font-size: 9.5px; color: var(--faint); margin-top: 2px; }
.j-step.fail .j-ms { color: var(--err); }
.j-step.skip .j-ms { opacity: 0.35; }

.err-card { background: var(--panel-2); border: 1px solid color-mix(in srgb, var(--err) 25%, transparent); border-radius: 10px; padding: 14px 16px; margin-top: 14px; }
.err-card .e-type { display: inline-flex; align-items: center; gap: 7px; font-family: var(--font-mono); font-size: 10.5px; color: var(--err); letter-spacing: 0.06em; margin-bottom: 7px; text-transform: uppercase; }
.err-card .e-msg { font-size: 13px; color: var(--text); line-height: 1.55; word-break: break-word; }
.meta-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px 18px; margin-top: 14px; }
.meta-item .mk { font-family: var(--font-mono); font-size: 9.5px; color: var(--faint); letter-spacing: 0.1em; text-transform: uppercase; display: block; margin-bottom: 2px; }
.meta-item .mv { font-family: var(--font-mono); font-size: 11.5px; color: var(--muted); word-break: break-all; }
.copy-btn { display: inline-flex; align-items: center; gap: 5px; margin-top: 12px; border-radius: 7px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-family: var(--font-mono); font-size: 10.5px; padding: 5px 11px; transition: color .15s, border-color .15s; cursor: pointer; }
.copy-btn:hover { color: var(--accent); border-color: var(--accent); }

/* bars / op split / timeline */
.op-split { display: flex; flex-direction: column; gap: 16px; }
.op-seg { display: flex; height: 12px; border-radius: 999px; overflow: hidden; background: var(--panel-2); }
.op-seg .seg-read { background: var(--read); transition: width 0.7s cubic-bezier(0.22,1,0.36,1); }
.op-seg .seg-write { background: var(--warn); transition: width 0.7s cubic-bezier(0.22,1,0.36,1); }
.op-legend { display: flex; flex-direction: column; gap: 10px; }
.op-legend-item { display: flex; align-items: center; gap: 10px; }
.op-dot { width: 9px; height: 9px; border-radius: 3px; }
.op-dot.read { background: var(--read); } .op-dot.write { background: var(--warn); }
.op-legend-item .op-name { font-size: 13px; color: var(--muted); flex: 1; }
.op-legend-item .op-count { font-family: var(--font-mono); font-size: 13px; font-weight: 500; font-variant-numeric: tabular-nums; }
.op-legend-item .op-pct { font-family: var(--font-mono); font-size: 11px; color: var(--faint); width: 42px; text-align: right; }
.timeline-panel { margin-bottom: 18px; }
.timeline { width: 100%; height: 120px; display: block; }

/* Hint utility (matching global) */
.hint { font-family: var(--font-mono); font-size: 10px; color: var(--faint); letter-spacing: 0.08em; }
</style>
