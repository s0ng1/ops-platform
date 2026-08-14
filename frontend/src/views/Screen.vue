<script setup>
// 实时大屏：1920×1080 设计稿 + transform 整体缩放适配，深空蓝科技风
// 数据：30 秒轮询 + WS 联动刷新（告警 / 设备状态）
import { onBeforeUnmount, onMounted, ref } from 'vue'
import {
  getOverview, getAlertSummary, listAlertEvents, getReportTraffic,
  listDevices, getDeviceMetrics, getDeviceMetricsLatest,
} from '../api'
import {
  SEVERITY_ORDER, severityLabel, severityColor, metricLabel, fmtLabels,
} from '../utils/dicts'
import { fmtBps } from '../utils/format'
import { startWs, stopWs, onWsMessage } from '../stores/ws'
import Chart from '../components/Chart.vue'

// ===== 整屏缩放：固定 1920×1080 画布，按视口等比缩放居中 =====
const DESIGN_W = 1920
const DESIGN_H = 1080
const scale = ref(1)
function fit() {
  scale.value = Math.min(window.innerWidth / DESIGN_W, window.innerHeight / DESIGN_H)
}

// ===== 顶部时钟（秒级刷新） =====
const now = ref('')
function tick() {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  const week = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()]
  now.value = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} 星期${week} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function todayStr() {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

// ===== 数据 =====
const overview = ref({ total: 0, online: 0, offline: 0, unknown: 0, by_type: {} })
const summary = ref({ critical: 0, major: 0, warning: 0, info: 0 })
const todayCount = ref(0)
const firing = ref([])
const donutOption = ref(null)
const gaugeOption = ref(null)
const trendOption = ref(null)
const trendTitle = ref('')
const perfOption = ref(null)
const trafficOption = ref(null)
// 流量 Top1 接口（趋势图数据源：{ device_id, device_name, interface }）
const topIface = ref(null)

const firingTotal = () => SEVERITY_ORDER.reduce((s, k) => s + (summary.value[k] || 0), 0)
const onlineRatePct = () => {
  const { total, online } = overview.value
  return total ? (online / total) * 100 : 0
}

// 左上：四个核心指标大数字卡
const statCards = () => [
  { label: '设备总数', value: overview.value.total ?? 0, color: '#00e5ff' },
  { label: '在线设备', value: overview.value.online ?? 0, color: '#37f59a' },
  { label: '离线设备', value: overview.value.offline ?? 0, color: '#ff5c7a' },
  { label: '告警中', value: firingTotal(), color: '#ffb84d' },
]

const AXIS = '#5b7299'
const SPLIT = 'rgba(34, 53, 92, 0.55)'
const TIP = {
  backgroundColor: 'rgba(8, 18, 42, 0.92)',
  borderColor: 'rgba(0, 229, 255, 0.35)',
  textStyle: { color: '#cfe6ff' },
}

async function refreshOverview() {
  overview.value = await getOverview()
  // 中上：在线率大仪表盘
  gaugeOption.value = {
    series: [{
      type: 'gauge',
      center: ['50%', '60%'],
      radius: '96%',
      startAngle: 210,
      endAngle: -30,
      min: 0,
      max: 100,
      progress: {
        show: true,
        width: 16,
        itemStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
            colorStops: [
              { offset: 0, color: '#0066ff' },
              { offset: 0.6, color: '#00e5ff' },
              { offset: 1, color: '#37f59a' },
            ],
          },
          shadowColor: 'rgba(0, 229, 255, 0.7)',
          shadowBlur: 14,
        },
      },
      axisLine: { lineStyle: { width: 16, color: [[1, 'rgba(0, 229, 255, 0.1)']] } },
      pointer: {
        length: '58%',
        width: 4,
        itemStyle: { color: '#00e5ff', shadowColor: 'rgba(0, 229, 255, 0.8)', shadowBlur: 8 },
      },
      anchor: {
        show: true, size: 14,
        itemStyle: { color: '#0a1530', borderColor: '#00e5ff', borderWidth: 2 },
      },
      axisTick: { distance: -26, length: 6, lineStyle: { color: '#3b5b8c', width: 1 } },
      splitLine: { distance: -26, length: 12, lineStyle: { color: '#5b7bab', width: 2 } },
      axisLabel: { distance: -42, color: AXIS, fontSize: 12 },
      detail: {
        valueAnimation: true,
        formatter: (v) => `${v.toFixed(1)}%`,
        color: '#00e5ff',
        fontSize: 44,
        fontWeight: 700,
        fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
        offsetCenter: [0, '42%'],
        textShadow: '0 0 18px rgba(0,229,255,0.8)',
      },
      data: [{ value: Number(onlineRatePct().toFixed(1)) }],
    }],
  }
}

async function refreshAlerts() {
  summary.value = await getAlertSummary()
  // firing 事件最新 20 条滚动展示
  firing.value = await listAlertEvents({ status: 'firing', limit: 20 })
  // 左中：告警等级分布环图（中心数字为 HTML 覆盖层）
  donutOption.value = {
    tooltip: { ...TIP, trigger: 'item', formatter: '{b}：{c} 条（{d}%）' },
    series: [{
      type: 'pie',
      radius: ['62%', '82%'],
      center: ['50%', '50%'],
      avoidLabelOverlap: true,
      label: { show: false },
      labelLine: { show: false },
      itemStyle: { borderColor: '#08122a', borderWidth: 3 },
      emphasis: { scaleSize: 6, itemStyle: { shadowBlur: 18 } },
      data: SEVERITY_ORDER.map((s) => ({
        name: severityLabel(s),
        value: summary.value[s] || 0,
        itemStyle: { color: severityColor(s), shadowColor: severityColor(s), shadowBlur: 10 },
      })),
    }],
  }
}

async function refreshTodayCount() {
  // 今日新增告警数：按 fired_at 本地日期过滤
  const events = await listAlertEvents({ limit: 200 })
  const today = todayStr()
  todayCount.value = events.filter((e) => (e.fired_at || '').slice(0, 10) === today).length
}

// 中下：Top1 接口近 1 小时 in/out 双线渐变趋势（报表接口只有周期聚合，无时序，故走 metrics 接口）
async function refreshTrend() {
  if (!topIface.value) {
    trendOption.value = null
    trendTitle.value = ''
    return
  }
  const { device_id: id, interface: iface, device_name: name } = topIface.value
  const start = new Date(Date.now() - 3600 * 1000).toISOString()
  const labels = JSON.stringify({ if: iface })
  const [rin, rout] = await Promise.all([
    getDeviceMetrics(id, { metric: 'if_in_bps', start, labels, limit: 800 }),
    getDeviceMetrics(id, { metric: 'if_out_bps', start, labels, limit: 800 }),
  ])
  const inPts = (rin.points || []).map((p) => [p.time, p.value])
  const outPts = (rout.points || []).map((p) => [p.time, p.value])
  if (!inPts.length && !outPts.length) {
    trendOption.value = null
    trendTitle.value = ''
    return
  }
  trendTitle.value = `${name || ''}/${iface}`
  const line = (name2, color, color2) => ({
    name: name2,
    type: 'line',
    smooth: true,
    symbol: 'none',
    lineStyle: { width: 2, color, shadowColor: color, shadowBlur: 8 },
    areaStyle: {
      color: {
        type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [
          { offset: 0, color: color2 },
          { offset: 1, color: 'rgba(0, 0, 0, 0)' },
        ],
      },
    },
  })
  trendOption.value = {
    tooltip: { ...TIP, trigger: 'axis', valueFormatter: (v) => fmtBps(v) },
    legend: { top: 0, right: 10, textStyle: { color: AXIS }, itemWidth: 16, itemHeight: 8 },
    grid: { left: 12, right: 20, top: 34, bottom: 10, containLabel: true },
    xAxis: {
      type: 'time',
      axisLabel: { color: AXIS },
      axisLine: { lineStyle: { color: '#22355c' } },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: AXIS, formatter: (v) => fmtBps(v) },
      splitLine: { lineStyle: { color: SPLIT } },
    },
    series: [
      { ...line('入向', '#00e5ff', 'rgba(0, 229, 255, 0.28)'), data: inPts },
      { ...line('出向', '#a06bff', 'rgba(160, 107, 255, 0.28)'), data: outPts },
    ],
  }
}

async function refreshTraffic() {
  const today = todayStr()
  const res = await getReportTraffic({ start: today, end: today, granularity: 'day', top: 10 })
  const rows = res.rows || []
  // Top1 接口作为趋势图数据源
  const top = rows[0]
  topIface.value = top
    ? { device_id: top.device_id, device_name: top.device_name, interface: top.interface }
    : null
  // 右下：接口流量 Top10 渐变横条
  trafficOption.value = {
    tooltip: {
      ...TIP,
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (ps) => `${ps[0].name}<br/>均值速率：${fmtBps(ps[0].value)}`,
    },
    grid: { left: 8, right: 78, top: 8, bottom: 8, containLabel: true },
    xAxis: {
      type: 'value',
      axisLabel: { color: AXIS, formatter: (v) => fmtBps(v) },
      splitLine: { lineStyle: { color: SPLIT } },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: rows.map((r) => `${r.device_name || r.ip}/${r.interface}`),
      axisLabel: { color: '#9fb3d8', fontSize: 12 },
      axisLine: { lineStyle: { color: '#22355c' } },
      axisTick: { show: false },
    },
    series: [{
      type: 'bar',
      barMaxWidth: 12,
      itemStyle: {
        color: {
          type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
          colorStops: [
            { offset: 0, color: '#0055cc' },
            { offset: 1, color: '#00e5ff' },
          ],
        },
        borderRadius: [0, 6, 6, 0],
        shadowColor: 'rgba(0, 229, 255, 0.4)',
        shadowBlur: 6,
      },
      label: {
        show: true,
        position: 'right',
        color: '#00e5ff',
        fontSize: 12,
        fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
        formatter: (p) => fmtBps(p.value),
      },
      data: rows.map((r) => ((r.in_avg || 0) + (r.out_avg || 0)) / 2),
    }],
  }
}

// 右上：CPU/内存 Top5（overview 无此数据，逐设备取 metrics/latest，上限 40 台防刷屏）
async function refreshPerf() {
  const devices = await listDevices()
  const targets = devices.slice(0, 40)
  const results = await Promise.allSettled(targets.map((d) => getDeviceMetricsLatest(d.id)))
  const rows = []
  results.forEach((r, i) => {
    if (r.status !== 'fulfilled') return
    let cpu = null
    let mem = null
    for (const it of r.value.items || []) {
      if (it.metric === 'cpu_usage') cpu = it.value
      else if (it.metric === 'mem_usage') mem = it.value
    }
    if (cpu == null && mem == null) return
    rows.push({ name: targets[i].name || targets[i].ip, cpu: cpu ?? 0, mem: mem ?? 0 })
  })
  rows.sort((a, b) => b.cpu + b.mem - (a.cpu + a.mem))
  const top = rows.slice(0, 5)
  if (!top.length) {
    perfOption.value = null
    return
  }
  const grad = (c1, c2) => ({
    type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
    colorStops: [{ offset: 0, color: c1 }, { offset: 1, color: c2 }],
  })
  perfOption.value = {
    tooltip: { ...TIP, trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v) => `${Number(v).toFixed(1)}%` },
    legend: { top: 0, right: 10, textStyle: { color: AXIS }, itemWidth: 14, itemHeight: 8 },
    grid: { left: 8, right: 46, top: 32, bottom: 8, containLabel: true },
    xAxis: {
      type: 'value',
      max: 100,
      axisLabel: { color: AXIS, formatter: '{value}%' },
      splitLine: { lineStyle: { color: SPLIT } },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: top.map((r) => r.name),
      axisLabel: { color: '#9fb3d8', fontSize: 12 },
      axisLine: { lineStyle: { color: '#22355c' } },
      axisTick: { show: false },
    },
    series: [
      {
        name: 'CPU',
        type: 'bar',
        barMaxWidth: 10,
        itemStyle: { color: grad('#0055cc', '#00e5ff'), borderRadius: [0, 5, 5, 0] },
        label: { show: true, position: 'right', color: '#00e5ff', fontSize: 11, formatter: (p) => `${Number(p.value).toFixed(0)}%` },
        data: top.map((r) => r.cpu),
      },
      {
        name: '内存',
        type: 'bar',
        barMaxWidth: 10,
        itemStyle: { color: grad('#4b2fa0', '#c86bff'), borderRadius: [0, 5, 5, 0] },
        label: { show: true, position: 'right', color: '#c86bff', fontSize: 11, formatter: (p) => `${Number(p.value).toFixed(0)}%` },
        data: top.map((r) => r.mem),
      },
    ],
  }
}

async function refreshAll() {
  // 单个面板失败不影响其他面板；错误提示由拦截器统一处理
  await Promise.allSettled([
    refreshOverview(), refreshAlerts(), refreshTodayCount(),
    refreshTraffic(), refreshPerf(),
  ])
  // 趋势图依赖 Top1 接口，须在流量报表之后
  await refreshTrend().catch(() => {})
}

// ===== 告警列表无缝滚动：定时把第一条移到最后，悬停暂停 =====
const scrollPaused = ref(false)
let scrollTimer = null

// ===== 轮询 + WS =====
let pollTimer = null
let clockTimer = null
// 收到新告警时刷新告警相关面板（等级环图 / 今日新增 / 滚动列表 / 告警数指标卡）
const offAlert = onWsMessage('alert', () => {
  refreshAlerts()
  refreshTodayCount()
})
// 设备状态变化时刷新设备面板（指标卡 + 在线率仪表盘）
const offDevice = onWsMessage('device_status', () => refreshOverview())

onMounted(() => {
  startWs()
  fit()
  window.addEventListener('resize', fit)
  tick()
  refreshAll()
  clockTimer = setInterval(tick, 1000)
  pollTimer = setInterval(refreshAll, 30000)
  scrollTimer = setInterval(() => {
    if (!scrollPaused.value && firing.value.length > 1) {
      firing.value.push(firing.value.shift())
    }
  }, 3000)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', fit)
  clearInterval(clockTimer)
  clearInterval(pollTimer)
  clearInterval(scrollTimer)
  offAlert()
  offDevice()
  stopWs()
})
</script>

<template>
  <div class="screen">
    <div class="screen-canvas" :style="{ transform: `translate(-50%, -50%) scale(${scale})` }">
      <!-- 顶部标题栏 -->
      <header class="hd">
        <div class="hd-side hd-side-l" />
        <h1 class="hd-title">内网运维管理平台</h1>
        <div class="hd-side hd-side-r" />
        <div class="hd-clock">{{ now }}</div>
      </header>

      <main class="bd">
        <!-- 左列：指标卡 / 告警等级环图 / 告警滚动墙 -->
        <section class="col col-l">
          <div class="panel stat-panel">
            <div class="stat-grid">
              <div v-for="c in statCards()" :key="c.label" class="stat-card">
                <div
                  class="stat-value"
                  :style="{ color: c.color, textShadow: `0 0 16px ${c.color}, 0 0 40px ${c.color}55` }"
                >
                  <span :key="c.value" class="num-pop">{{ c.value }}</span>
                </div>
                <div class="stat-label">{{ c.label }}</div>
              </div>
            </div>
          </div>

          <div class="panel donut-panel">
            <div class="panel-title">告警等级分布</div>
            <div class="donut-wrap">
              <Chart v-if="donutOption" :option="donutOption" height="100%" class="chart" />
              <div class="donut-center">
                <div class="donut-num">{{ firingTotal() }}</div>
                <div class="donut-sub">告警中</div>
              </div>
            </div>
          </div>

          <div
            class="panel alert-panel"
            @mouseenter="scrollPaused = true"
            @mouseleave="scrollPaused = false"
          >
            <div class="panel-title">
              实时告警
              <span class="panel-title-note">告警中 {{ firing.length }} · 今日新增 {{ todayCount }}</span>
            </div>
            <div v-if="!firing.length" class="panel-empty">当前无告警中事件</div>
            <TransitionGroup v-else name="roll" tag="div" class="alert-list">
              <div
                v-for="e in firing"
                :key="e.id"
                class="alert-item"
                :style="{
                  borderLeftColor: severityColor(e.severity),
                  background: `linear-gradient(90deg, ${severityColor(e.severity)}1f, transparent 70%)`,
                }"
              >
                <span class="alert-sev" :style="{ color: severityColor(e.severity) }">
                  {{ severityLabel(e.severity) }}
                </span>
                <span class="alert-main">
                  <span class="alert-device">{{ e.device_name || e.device_ip || '-' }}</span>
                  <span class="alert-metric">
                    {{ metricLabel(e.metric) }}
                    <template v-if="fmtLabels(e.labels) !== '-'">（{{ fmtLabels(e.labels) }}）</template>
                    = {{ e.value }}
                  </span>
                </span>
                <span class="alert-time">{{ (e.fired_at || '').slice(11, 19) }}</span>
              </div>
            </TransitionGroup>
          </div>
        </section>

        <!-- 中列：在线率仪表盘 / 流量趋势 -->
        <section class="col col-c">
          <div class="panel gauge-panel">
            <div class="panel-title">设备在线率</div>
            <Chart v-if="gaugeOption" :option="gaugeOption" height="100%" class="chart" />
            <div class="gauge-sub">
              在线 <b class="ok">{{ overview.online }}</b> / 离线
              <b class="bad">{{ overview.offline }}</b> / 未知 <b>{{ overview.unknown }}</b>
            </div>
          </div>

          <div class="panel trend-panel">
            <div class="panel-title">
              接口流量趋势（近 1 小时）
              <span v-if="trendTitle" class="panel-title-note">{{ trendTitle }}</span>
            </div>
            <Chart v-if="trendOption" :option="trendOption" height="100%" class="chart" />
            <div v-else class="panel-empty">暂无流量数据</div>
          </div>
        </section>

        <!-- 右列：CPU/内存 Top5 / 流量 Top10 -->
        <section class="col col-r">
          <div class="panel chart-panel">
            <div class="panel-title">CPU / 内存负载 Top5</div>
            <Chart v-if="perfOption" :option="perfOption" height="100%" class="chart" />
            <div v-else class="panel-empty">暂无 CPU / 内存指标数据</div>
          </div>

          <div class="panel chart-panel">
            <div class="panel-title">今日接口流量 Top10</div>
            <Chart v-if="trafficOption" :option="trafficOption" height="100%" class="chart" />
          </div>
        </section>
      </main>
    </div>
  </div>
</template>

<style scoped>
/* ===== 深空蓝背景：径向渐变 + CSS 网格纹理 + 扫描线 ===== */
.screen {
  position: fixed;
  inset: 0;
  overflow: hidden;
  background: radial-gradient(ellipse at 50% 35%, #0a1530 0%, #050b1e 72%);
  color: #c9d6ec;
}

/* 网格线纹理 */
.screen::before {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    repeating-linear-gradient(0deg, rgba(0, 229, 255, 0.035) 0 1px, transparent 1px 56px),
    repeating-linear-gradient(90deg, rgba(0, 229, 255, 0.035) 0 1px, transparent 1px 56px);
}

/* 扫描线动画 */
.screen::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  top: -140px;
  height: 140px;
  pointer-events: none;
  background: linear-gradient(180deg, transparent, rgba(0, 229, 255, 0.05), transparent);
  animation: scan 9s linear infinite;
}

@keyframes scan {
  to { transform: translateY(calc(100vh + 280px)); }
}

/* ===== 1920×1080 画布，等比缩放居中 ===== */
.screen-canvas {
  position: absolute;
  left: 50%;
  top: 50%;
  width: 1920px;
  height: 1080px;
  transform-origin: center center;
  display: flex;
  flex-direction: column;
  padding: 0 20px 18px;
  box-sizing: border-box;
}

/* ===== 标题栏 ===== */
.hd {
  position: relative;
  height: 92px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 36px;
}

.hd-title {
  margin: 0;
  font-size: 36px;
  font-weight: 700;
  letter-spacing: 12px;
  text-indent: 12px;
  color: #eaf6ff;
  text-shadow: 0 0 14px rgba(0, 229, 255, 0.9), 0 0 46px rgba(0, 229, 255, 0.4);
  white-space: nowrap;
}

/* 标题两侧装饰横线 + 菱形 */
.hd-side {
  flex: 0 0 300px;
  height: 2px;
  position: relative;
}

.hd-side-l { background: linear-gradient(90deg, transparent, rgba(0, 229, 255, 0.8)); }
.hd-side-r { background: linear-gradient(90deg, rgba(0, 229, 255, 0.8), transparent); }

.hd-side::after {
  content: '';
  position: absolute;
  top: -4px;
  width: 10px;
  height: 10px;
  background: #00e5ff;
  transform: rotate(45deg);
  box-shadow: 0 0 10px rgba(0, 229, 255, 0.9);
}

.hd-side-l::after { right: -4px; }
.hd-side-r::after { left: -4px; }

.hd-clock {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 20px;
  color: #7fd8f0;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  letter-spacing: 1px;
  text-shadow: 0 0 10px rgba(0, 229, 255, 0.4);
  white-space: nowrap;
}

/* ===== 三列布局 ===== */
.bd {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 460px 1fr 460px;
  gap: 16px;
}

.col {
  display: grid;
  gap: 16px;
  min-height: 0;
}

.col-l { grid-template-rows: 216px 320px 1fr; }
.col-c { grid-template-rows: 380px 1fr; }
.col-r { grid-template-rows: 1fr 1fr; }

/* ===== 面板：深色半透明底 + 四角发光角标 ===== */
.panel {
  position: relative;
  min-height: 0;
  background: rgba(10, 22, 48, 0.55);
  border: 1px solid rgba(0, 229, 255, 0.14);
  box-shadow: inset 0 0 26px rgba(0, 120, 200, 0.08);
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
}

.panel::before {
  content: '';
  position: absolute;
  inset: -1px;
  pointer-events: none;
  background:
    linear-gradient(#00e5ff, #00e5ff) left top / 14px 2px,
    linear-gradient(#00e5ff, #00e5ff) left top / 2px 14px,
    linear-gradient(#00e5ff, #00e5ff) right top / 14px 2px,
    linear-gradient(#00e5ff, #00e5ff) right top / 2px 14px,
    linear-gradient(#00e5ff, #00e5ff) left bottom / 14px 2px,
    linear-gradient(#00e5ff, #00e5ff) left bottom / 2px 14px,
    linear-gradient(#00e5ff, #00e5ff) right bottom / 14px 2px,
    linear-gradient(#00e5ff, #00e5ff) right bottom / 2px 14px;
  background-repeat: no-repeat;
  opacity: 0.85;
  filter: drop-shadow(0 0 4px rgba(0, 229, 255, 0.8));
}

.panel-title {
  flex-shrink: 0;
  font-size: 15px;
  font-weight: 600;
  color: #a9d6ee;
  letter-spacing: 2px;
  margin-bottom: 8px;
  padding-left: 12px;
  position: relative;
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.panel-title::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 4px;
  height: 14px;
  background: linear-gradient(180deg, #00e5ff, rgba(0, 229, 255, 0.1));
  box-shadow: 0 0 8px rgba(0, 229, 255, 0.8);
}

.panel-title-note {
  font-size: 12px;
  font-weight: 400;
  color: #5b7299;
  letter-spacing: 0;
}

.panel-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #44557a;
  font-size: 14px;
  letter-spacing: 2px;
}

.chart {
  flex: 1;
  min-height: 0;
}

/* ===== 左上：大数字指标卡（2×2） ===== */
.stat-grid {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: 1fr 1fr;
  gap: 10px;
}

.stat-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: rgba(0, 229, 255, 0.04);
  border: 1px solid rgba(0, 229, 255, 0.1);
}

.stat-value {
  font-size: 42px;
  font-weight: 700;
  line-height: 1.15;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  transition: color 0.4s ease;
}

/* 数字变化时重新挂载触发弹出动画 */
.num-pop {
  display: inline-block;
  animation: num-pop 0.5s ease;
}

@keyframes num-pop {
  0% { opacity: 0.2; transform: scale(1.18); }
  100% { opacity: 1; transform: scale(1); }
}

.stat-label {
  margin-top: 4px;
  font-size: 13px;
  letter-spacing: 3px;
  color: #6e84ab;
}

/* ===== 左中：环图 + 中心总数 ===== */
.donut-wrap {
  position: relative;
  flex: 1;
  min-height: 0;
}

.donut-center {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}

.donut-num {
  font-size: 38px;
  font-weight: 700;
  color: #eaf6ff;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  text-shadow: 0 0 16px rgba(0, 229, 255, 0.7);
}

.donut-sub {
  font-size: 13px;
  letter-spacing: 4px;
  color: #6e84ab;
}

/* ===== 左下：告警滚动墙 ===== */
.alert-panel {
  overflow: hidden;
}

.alert-list {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.alert-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  margin-bottom: 6px;
  border-left: 3px solid transparent;
}

/* 重排过渡：首条移到末尾时平滑滚动 */
.roll-move {
  transition: transform 0.6s ease;
}

.alert-sev {
  flex-shrink: 0;
  width: 40px;
  font-size: 12px;
  font-weight: 600;
  text-align: center;
  border: 1px solid currentColor;
  padding: 1px 0;
  opacity: 0.95;
}

.alert-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.alert-device {
  font-size: 13px;
  color: #d8e4f5;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.alert-metric {
  font-size: 12px;
  color: #6e84ab;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.alert-time {
  flex-shrink: 0;
  font-size: 12px;
  color: #4a5d84;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-variant-numeric: tabular-nums;
}

/* ===== 中上：仪表盘副标 ===== */
.gauge-sub {
  flex-shrink: 0;
  text-align: center;
  font-size: 13px;
  color: #6e84ab;
  letter-spacing: 1px;
  margin-top: -6px;
}

.gauge-sub b {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  color: #cfe6ff;
}

.gauge-sub .ok { color: #37f59a; }
.gauge-sub .bad { color: #ff5c7a; }
</style>
