<!-- src/views/Dashboard.vue -->
<template>
  <div>
    <!-- statusboard -->
    <div class="statusboard">
      <div class="statusboard-head">
        <div class="statusboard-title">
          <h2>Gateway 流量</h2>
          <span class="live-chip"><StatusLed status="ok" :pulse="true" /> live</span>
        </div>
        <span class="hint mono" style="font-size:10px;color:var(--faint)">last 60 min · 全部 Server</span>
      </div>
      <div class="spark-row">
        <div>
          <div class="spark-big">—<span class="unit">req</span></div>
          <div class="spark-label">total · last hour</div>
        </div>
        <div class="sparkline-wrap">
          <svg viewBox="0 0 300 64" preserveAspectRatio="none">
            <defs>
              <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.28"/>
                <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
              </linearGradient>
            </defs>
          </svg>
        </div>
      </div>
    </div>

    <!-- server filter chips -->
    <div class="chip-row">
      <button class="chip active">全部</button>
    </div>

    <!-- stat grid -->
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-top"><span class="stat-name">总请求</span></div>
        <div class="stat-value">—</div>
        <div class="stat-sub">过去 24 小时</div>
      </div>
      <div class="stat-card">
        <div class="stat-top"><span class="stat-name">错误数</span></div>
        <div class="stat-value danger">—</div>
        <div class="stat-sub">数据加载中</div>
      </div>
      <div class="stat-card">
        <div class="stat-top"><span class="stat-name">P95 延迟</span></div>
        <div class="stat-value">—</div>
        <div class="stat-sub">gateway → server</div>
      </div>
      <div class="stat-card">
        <div class="stat-top"><span class="stat-name">鉴权失败</span></div>
        <div class="stat-value">—</div>
        <div class="stat-sub">invalid / denied</div>
      </div>
    </div>

    <!-- panel grid: failure feed + per-server stats -->
    <div class="panel-grid">
      <div class="panel">
        <div class="panel-head">
          <h3>失败请求 <span class="mono" style="font-size:10px;color:var(--faint);font-weight:400">· 点击查看轨迹</span></h3>
          <span class="hint">0 FAILED</span>
        </div>
        <div class="fail-feed">
          <div class="muted" style="font-size:12.5px;padding:12px 4px">暂无失败请求 ✓</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><h3>分 Server 统计</h3><span class="hint">24H</span></div>
        <table class="srv-tbl">
          <thead><tr>
            <th>Server</th><th class="num">请求</th><th class="num">错误</th><th class="num">错误率</th><th class="num">P95</th>
          </tr></thead>
          <tbody>
            <tr><td colspan="5" style="text-align:center;color:var(--faint);padding:24px">加载中…</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- timeline -->
    <div class="panel timeline-panel">
      <div class="panel-head"><h3>请求时间线</h3><span class="hint">1H · 1MIN BUCKETS</span></div>
      <svg class="timeline" viewBox="0 0 600 120" preserveAspectRatio="none"></svg>
    </div>
  </div>
</template>

<script setup>
import StatusLed from '../components/StatusLed.vue'
// Dashboard shell — real data comes in Task 4
</script>

<style scoped>
/* ── Extracted from docs/superpowers/mockups/gateway-admin.html ── */
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

/* filter chips */
.chip-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
.chip {
  display: inline-flex; align-items: center; gap: 8px;
  border-radius: 999px; border: 1px solid var(--border);
  background: var(--panel); color: var(--muted);
  font-family: var(--font-mono); font-size: 12px; padding: 6px 14px;
  transition: border-color 0.15s, color 0.15s, background 0.15s, box-shadow 0.15s;
}
.chip:hover { color: var(--text); border-color: var(--border-strong); }
.chip.active { background: var(--accent-dim); border-color: var(--accent); color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 8%, transparent); }

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

/* failure feed */
.fail-feed { display: flex; flex-direction: column; gap: 8px; }

/* timeline */
.timeline-panel { margin-bottom: 18px; }
.timeline { width: 100%; height: 120px; display: block; }
</style>
