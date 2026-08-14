<script setup>
// 设备详情：基本信息 + 最新指标卡片 + 分组指标曲线（系统/接口/磁盘/数据库）
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  listDevices, getDeviceMetrics, getDeviceMetricsLatest, getDeviceMetricsCatalog,
  listConfigBackups, getConfigBackup, getConfigBackupDiff, fetchConfigBackup,
} from '../api'
import {
  typeLabel, statusLabel, statusTag, fmtTime, metricLabel, fmtLabels,
} from '../utils/dicts'
import { onWsMessage } from '../stores/ws'
import { useAuth } from '../stores/auth'
import Chart from '../components/Chart.vue'

const route = useRoute()
const deviceId = Number(route.params.id)

const device = ref(null)
const latest = ref([])
const catalog = ref([])

// ===== 时间范围 =====
const RANGES = [
  { key: '1h', label: '近 1 小时', ms: 3600e3 },
  { key: '6h', label: '近 6 小时', ms: 6 * 3600e3 },
  { key: '24h', label: '近 24 小时', ms: 24 * 3600e3 },
  { key: '7d', label: '近 7 天', ms: 7 * 24 * 3600e3 },
]
const range = ref('1h')

function timeWindow() {
  const end = new Date()
  const start = new Date(end.getTime() - RANGES.find((r) => r.key === range.value).ms)
  return { start: start.toISOString(), end: end.toISOString() }
}

// ===== 最新指标卡片：挑重点无 labels 的指标展示 =====
const LATEST_CARD_METRICS = ['cpu_usage', 'mem_usage', 'ping_latency_ms', 'device_online', 'process_count', 'load1']
const latestCards = computed(() => {
  const cards = []
  for (const m of LATEST_CARD_METRICS) {
    const item = latest.value.find((it) => it.metric === m && (!it.labels || !Object.keys(it.labels).length))
    if (item) {
      let text = item.value
      if (m === 'device_online') text = item.value === 1 ? '在线' : '离线'
      else if (m === 'ping_latency_ms') text = item.value < 0 ? '超时' : `${item.value} ms`
      cards.push({ metric: m, label: metricLabel(m), value: text })
    }
  }
  return cards
})

// ===== 曲线分组（来自 catalog） =====
// 系统指标：每个指标一张图；load1/5/15 合并一张图
const SYSTEM_SOLO = ['cpu_usage', 'mem_usage', 'ping_latency_ms', 'process_count']
const LOAD_METRICS = ['load1', 'load5', 'load15']

const systemMetrics = computed(() =>
  SYSTEM_SOLO.filter((m) => catalog.value.some((c) => c.metric === m))
)
const hasLoad = computed(() => catalog.value.some((c) => LOAD_METRICS.includes(c.metric)))

const interfaceNames = computed(() => {
  const names = new Set()
  catalog.value.forEach((c) => {
    if (c.metric === 'if_in_bps' && c.labels?.if) names.add(c.labels.if)
  })
  return [...names]
})
const hasIfUtil = computed(() => catalog.value.some((c) => c.metric === 'if_in_util'))

// 磁盘：disk_usage 按 disk/mount 标签值分组
const diskKeys = computed(() => {
  const keys = new Map()
  catalog.value.forEach((c) => {
    if (c.metric !== 'disk_usage') return
    const k = c.labels?.disk ?? c.labels?.mount
    if (k) keys.set(k, c.labels)
  })
  return [...keys.entries()].map(([name, labels]) => ({ name, labels }))
})

// 数据库组可选项：无 labels 的指标一项一条；带 labels 的（如 Oracle 表空间）按 labels 拆条
const DB_METRIC_PREFIXES = ['mysql_', 'oracle_', 'mssql_', 'db_']
const dbSeries = computed(() => {
  const items = new Map()
  catalog.value.forEach((c) => {
    if (!DB_METRIC_PREFIXES.some((p) => c.metric.startsWith(p))) return
    const labels = c.labels || {}
    const key = Object.keys(labels).length ? `${c.metric}|${JSON.stringify(labels)}` : c.metric
    if (!items.has(key)) items.set(key, { key, metric: c.metric, labels })
  })
  return [...items.values()]
})

function dbSeriesLabel(s) {
  return Object.keys(s.labels).length
    ? `${metricLabel(s.metric)} ${fmtLabels(s.labels)}`
    : metricLabel(s.metric)
}

// 应用拨测组：app_/nginx_/redis_ 指标按 metric+labels 拆条（labels 只带 probe_kind），每条一张图
const appSeries = computed(() => {
  const items = new Map()
  catalog.value.forEach((c) => {
    if (!/^(app_|nginx_|redis_)/.test(c.metric)) return
    const labels = c.labels || {}
    const key = Object.keys(labels).length ? `${c.metric}|${JSON.stringify(labels)}` : c.metric
    if (!items.has(key)) items.set(key, { key, metric: c.metric, labels })
  })
  return [...items.values()]
})

// ===== 图表 option 状态 =====
const sysOptions = reactive({}) // metric -> option
const loadOption = ref(null)
const ifSelected = ref('')
const ifBpsOption = ref(null)
const ifUtilOption = ref(null)
const diskOptions = reactive({}) // diskKey -> option
const mysqlSelected = ref([])
const mysqlOption = ref(null)
const appOptions = reactive({}) // appSeries.key -> option
const chartLoading = ref(false)

// 拉取单条序列（metric + 可选 labels）并转成 [time, value] 点
async function fetchSeries(metric, labelsObj) {
  const { start, end } = timeWindow()
  const res = await getDeviceMetrics(deviceId, {
    metric,
    start,
    end,
    labels: labelsObj ? JSON.stringify(labelsObj) : undefined,
    limit: 2000,
  })
  return (res.points || []).map((p) => [p.time, p.value])
}

// 组装折线图 option
function lineOption(series) {
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: series.map((s) => s.name), top: 0 },
    grid: { left: 60, right: 20, top: 32, bottom: 28 },
    xAxis: { type: 'time' },
    yAxis: { type: 'value' },
    series: series.map((s) => ({
      name: s.name,
      type: 'line',
      showSymbol: false,
      smooth: true,
      data: s.data,
    })),
  }
}

// 系统区图表
async function loadSystemCharts() {
  for (const m of systemMetrics.value) {
    sysOptions[m] = lineOption([{ name: metricLabel(m), data: await fetchSeries(m) }])
  }
  if (hasLoad.value) {
    const series = []
    for (const m of LOAD_METRICS) {
      if (catalog.value.some((c) => c.metric === m)) {
        series.push({ name: metricLabel(m), data: await fetchSeries(m) })
      }
    }
    loadOption.value = series.length ? lineOption(series) : null
  }
}

// 接口区图表
async function loadInterfaceCharts() {
  const name = ifSelected.value
  if (!name) return
  const labels = { if: name }
  ifBpsOption.value = lineOption([
    { name: '入速率(bps)', data: await fetchSeries('if_in_bps', labels) },
    { name: '出速率(bps)', data: await fetchSeries('if_out_bps', labels) },
  ])
  if (hasIfUtil.value) {
    ifUtilOption.value = lineOption([
      { name: '入利用率(%)', data: await fetchSeries('if_in_util', labels) },
      { name: '出利用率(%)', data: await fetchSeries('if_out_util', labels) },
    ])
  }
}

// 磁盘区图表
async function loadDiskCharts() {
  for (const d of diskKeys.value) {
    diskOptions[d.name] = lineOption([
      { name: `使用率(%)`, data: await fetchSeries('disk_usage', d.labels) },
    ])
  }
}

// 数据库区图表（多选指标合并一张图）
async function loadMysqlChart() {
  if (!mysqlSelected.value.length) {
    mysqlOption.value = null
    return
  }
  const byKey = new Map(dbSeries.value.map((s) => [s.key, s]))
  const series = []
  for (const k of mysqlSelected.value) {
    const s = byKey.get(k)
    if (!s) continue
    series.push({
      name: dbSeriesLabel(s),
      data: await fetchSeries(s.metric, Object.keys(s.labels).length ? s.labels : undefined),
    })
  }
  mysqlOption.value = lineOption(series)
}

// 应用拨测区图表（每条序列一张图）
async function loadAppCharts() {
  for (const s of appSeries.value) {
    appOptions[s.key] = lineOption([{
      name: dbSeriesLabel(s),
      data: await fetchSeries(s.metric, Object.keys(s.labels).length ? s.labels : undefined),
    }])
  }
}

async function loadAllCharts() {
  chartLoading.value = true
  try {
    await Promise.all([loadSystemCharts(), loadDiskCharts(), loadMysqlChart(), loadInterfaceCharts(), loadAppCharts()])
  } finally {
    chartLoading.value = false
  }
}

function onRangeChange() {
  loadAllCharts()
}

// ===== 配置备份 tab =====
const { state: authState } = useAuth()
const canOperate = computed(() => ['admin', 'operator'].includes(authState.role))
const activeTab = ref('metrics')
// 配置备份只针对网络/安全设备（调度器也只对这两类 + SSH 凭据生效）
const showBackupTab = computed(() => ['network', 'security'].includes(device.value?.type))

const backupTableRef = ref(null)
const backupInited = ref(false)
const backupsLoading = ref(false)
const backups = ref([])
const backupsTotal = ref(0)
const backupsPage = ref(1)
const hasSshCredential = ref(true)
const selectedBackups = ref([])
const diffText = ref('')
const diffLoading = ref(false)
const fetching = ref(false)
const viewVisible = ref(false)
const viewContent = ref('')

function fmtSize(n) {
  if (n == null) return '-'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

async function loadBackups() {
  backupsLoading.value = true
  try {
    const res = await listConfigBackups(deviceId, { page: backupsPage.value, page_size: 20 })
    backups.value = res.items || []
    backupsTotal.value = res.total || 0
  } finally {
    backupsLoading.value = false
  }
}

async function initBackupTab() {
  // 备份走辅槽 SSH 凭据（设备接口直接带 ssh_credential_id，服务端已保证是 SSH 类型）
  hasSshCredential.value = !!device.value?.ssh_credential_id
  if (hasSshCredential.value) loadBackups()
}

function onTabChange(name) {
  if (name === 'backup' && !backupInited.value) {
    backupInited.value = true
    initBackupTab()
  }
}

function onSelectionChange(rows) {
  if (rows.length > 2) {
    // 最多选两个版本：保留最近选中的两个
    const keep = rows.slice(-2)
    backupTableRef.value.clearSelection()
    keep.forEach((r) => backupTableRef.value.toggleRowSelection(r, true))
    return
  }
  selectedBackups.value = rows
}

const diffLines = computed(() => (diffText.value ? diffText.value.split('\n') : []))

function diffLineClass(line) {
  if (line.startsWith('+') && !line.startsWith('+++')) return 'diff-add'
  if (line.startsWith('-') && !line.startsWith('---')) return 'diff-del'
  return ''
}

async function showDiff() {
  if (selectedBackups.value.length !== 2) return
  diffLoading.value = true
  try {
    const [older, newer] = [...selectedBackups.value].sort((a, b) => a.id - b.id)
    const text = await getConfigBackupDiff(deviceId, older.id, newer.id)
    diffText.value = text || '两个版本内容一致'
  } finally {
    diffLoading.value = false
  }
}

async function viewBackup(row) {
  const res = await getConfigBackup(deviceId, row.id)
  viewContent.value = res.content
  viewVisible.value = true
}

async function fetchNow() {
  fetching.value = true
  try {
    const r = await fetchConfigBackup(deviceId)
    const messages = {
      baseline: '已建立首个配置基线',
      changed: '检测到配置变更，已保存新版本',
      same: '配置无变化',
    }
    if (r.status === 'failed') ElMessage.error('配置拉取失败，请检查 SSH 连通性与凭据')
    else ElMessage.success(messages[r.status] || '备份完成')
    backupsPage.value = 1
    loadBackups()
  } finally {
    fetching.value = false
  }
}

// ===== 初始化 =====
const offWs = onWsMessage('device_status', (msg) => {
  // 当前设备状态变化时局部更新
  if (device.value && msg.device_id === device.value.id) {
    device.value.status = msg.status
    if (msg.name) device.value.name = msg.name
  }
})

onMounted(async () => {
  // 后端无单设备查询接口，从列表中按 id 找
  const devices = await listDevices({})
  device.value = devices.find((d) => d.id === deviceId) || null
  try {
    const [latestRes, catalogRes] = await Promise.all([
      getDeviceMetricsLatest(deviceId),
      getDeviceMetricsCatalog(deviceId),
    ])
    latest.value = latestRes.items || []
    catalog.value = catalogRes.catalog || []
  } catch {
    // 无采集数据时静默，页面显示空态
  }
  // 默认选中第一个接口 / 全部 MySQL 指标
  ifSelected.value = interfaceNames.value[0] || ''
  mysqlSelected.value = dbSeries.value.map((s) => s.key)
  if (catalog.value.length) loadAllCharts()
})

onBeforeUnmount(offWs)
</script>

<template>
  <div v-loading="!device">
    <template v-if="device">
      <!-- 基本信息 -->
      <el-card shadow="never" class="card">
        <template #header>
          <div class="header-row">
            <span>{{ device.name || device.ip }} 基本信息</span>
            <el-button size="small" :icon="'Back'" @click="$router.push(['network', 'security'].includes(device.type) ? '/devices/network' : '/devices/servers')">返回列表</el-button>
          </div>
        </template>
        <el-descriptions :column="4" border size="small">
          <el-descriptions-item label="IP 地址">{{ device.ip }}</el-descriptions-item>
          <el-descriptions-item label="类型">{{ typeLabel(device.type) }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusTag(device.status)" size="small">{{ statusLabel(device.status) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="时延">
            {{ device.last_latency_ms != null ? device.last_latency_ms + ' ms' : '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="分组">{{ device.group_name || '-' }}</el-descriptions-item>
          <el-descriptions-item label="位置">{{ device.location || '-' }}</el-descriptions-item>
          <el-descriptions-item label="凭据">{{ device.credential_name || '-' }}</el-descriptions-item>
          <el-descriptions-item label="最后在线">{{ fmtTime(device.last_seen) }}</el-descriptions-item>
          <el-descriptions-item label="系统描述" :span="4">
            {{ device.sys_descr || '-' }}
          </el-descriptions-item>
        </el-descriptions>

        <!-- 最新指标卡片 -->
        <div v-if="latestCards.length" class="latest-cards">
          <div v-for="c in latestCards" :key="c.metric" class="latest-item">
            <div class="latest-value">{{ c.value }}</div>
            <div class="latest-label">{{ c.label }}</div>
          </div>
        </div>
      </el-card>

      <el-tabs v-model="activeTab" class="card" @tab-change="onTabChange">
        <el-tab-pane label="监控指标" name="metrics">
      <!-- 时间范围 -->
      <el-card v-if="catalog.length" shadow="never" class="card">
        <div class="range-row">
          <span class="range-label">指标曲线</span>
          <el-radio-group v-model="range" @change="onRangeChange">
            <el-radio-button v-for="r in RANGES" :key="r.key" :value="r.key">{{ r.label }}</el-radio-button>
          </el-radio-group>
        </div>
      </el-card>

      <div v-loading="chartLoading">
        <!-- 系统 -->
        <el-card v-if="systemMetrics.length || loadOption" shadow="never" class="card">
          <template #header>系统</template>
          <el-row :gutter="16">
            <el-col v-for="m in systemMetrics" :key="m" :span="12">
              <div class="chart-title">{{ metricLabel(m) }}</div>
              <Chart v-if="sysOptions[m]" :option="sysOptions[m]" height="260px" />
            </el-col>
            <el-col v-if="loadOption" :span="12">
              <div class="chart-title">系统负载</div>
              <Chart :option="loadOption" height="260px" />
            </el-col>
          </el-row>
        </el-card>

        <!-- 接口 -->
        <el-card v-if="interfaceNames.length" shadow="never" class="card">
          <template #header>
            <div class="header-row">
              <span>接口</span>
              <el-select
                v-model="ifSelected"
                filterable
                placeholder="选择接口"
                style="width: 240px"
                @change="loadInterfaceCharts"
              >
                <el-option v-for="n in interfaceNames" :key="n" :label="n" :value="n" />
              </el-select>
            </div>
          </template>
          <el-row :gutter="16">
            <el-col :span="hasIfUtil ? 12 : 24">
              <div class="chart-title">{{ ifSelected }} 速率</div>
              <Chart v-if="ifBpsOption" :option="ifBpsOption" height="280px" />
            </el-col>
            <el-col v-if="hasIfUtil" :span="12">
              <div class="chart-title">{{ ifSelected }} 利用率</div>
              <Chart v-if="ifUtilOption" :option="ifUtilOption" height="280px" />
            </el-col>
          </el-row>
        </el-card>

        <!-- 磁盘 -->
        <el-card v-if="diskKeys.length" shadow="never" class="card">
          <template #header>磁盘</template>
          <el-row :gutter="16">
            <el-col v-for="d in diskKeys" :key="d.name" :span="12">
              <div class="chart-title">{{ d.name }}</div>
              <Chart v-if="diskOptions[d.name]" :option="diskOptions[d.name]" height="240px" />
            </el-col>
          </el-row>
        </el-card>

        <!-- 数据库 -->
        <el-card v-if="dbSeries.length" shadow="never" class="card">
          <template #header>
            <div class="header-row">
              <span>数据库</span>
              <el-select
                v-model="mysqlSelected"
                multiple
                collapse-tags
                placeholder="选择指标"
                style="width: 420px"
                @change="loadMysqlChart"
              >
                <el-option v-for="s in dbSeries" :key="s.key" :label="dbSeriesLabel(s)" :value="s.key" />
              </el-select>
            </div>
          </template>
          <Chart v-if="mysqlOption" :option="mysqlOption" height="320px" />
          <el-empty v-else description="请选择要展示的指标" :image-size="60" />
        </el-card>

        <!-- 应用拨测 -->
        <el-card v-if="appSeries.length" shadow="never" class="card">
          <template #header>应用拨测</template>
          <el-row :gutter="16">
            <el-col v-for="s in appSeries" :key="s.key" :span="12">
              <div class="chart-title">{{ dbSeriesLabel(s) }}</div>
              <Chart v-if="appOptions[s.key]" :option="appOptions[s.key]" height="240px" />
            </el-col>
          </el-row>
        </el-card>
      </div>

      <!-- 无采集数据空态 -->
      <el-card v-if="!catalog.length" shadow="never" class="card">
        <el-empty description="暂无指标采集数据" />
      </el-card>
        </el-tab-pane>

        <!-- 配置备份（仅网络/安全设备） -->
        <el-tab-pane v-if="showBackupTab" label="配置备份" name="backup">
          <el-card shadow="never" class="card">
            <template #header>
              <div class="header-row">
                <span>配置版本</span>
                <div v-if="hasSshCredential">
                  <el-button
                    v-if="canOperate"
                    size="small"
                    type="primary"
                    :disabled="selectedBackups.length !== 2"
                    :loading="diffLoading"
                    @click="showDiff"
                  >对比选中版本</el-button>
                  <el-button v-if="canOperate" size="small" :loading="fetching" @click="fetchNow">立即备份</el-button>
                </div>
              </div>
            </template>
            <el-empty
              v-if="!hasSshCredential"
              description="该设备未配置备份凭据，无法备份配置（请在设备编辑中绑定「备份凭据(SSH)」）"
            />
            <template v-else>
              <el-table
                ref="backupTableRef"
                v-loading="backupsLoading"
                :data="backups"
                size="small"
                @selection-change="onSelectionChange"
              >
                <el-table-column v-if="canOperate" type="selection" width="40" />
                <el-table-column label="备份时间">
                  <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
                </el-table-column>
                <el-table-column label="内容 Hash" width="120">
                  <template #default="{ row }">{{ row.content_hash.slice(0, 8) }}</template>
                </el-table-column>
                <el-table-column label="大小" width="100">
                  <template #default="{ row }">{{ fmtSize(row.size) }}</template>
                </el-table-column>
                <el-table-column label="操作" width="80">
                  <template #default="{ row }">
                    <el-button v-if="canOperate" link type="primary" size="small" @click="viewBackup(row)">查看</el-button>
                  </template>
                </el-table-column>
                <template #empty>
                  <el-empty description="暂无配置备份版本，可点右上角「立即备份」拉取" :image-size="60" />
                </template>
              </el-table>
              <el-pagination
                v-if="backupsTotal > 20"
                class="pager"
                layout="total, prev, pager, next"
                :total="backupsTotal"
                :page-size="20"
                :current-page="backupsPage"
                @current-change="(p) => { backupsPage = p; loadBackups() }"
              />
              <pre v-if="diffText" class="diff-view"><div
                v-for="(line, i) in diffLines"
                :key="i"
                :class="diffLineClass(line)"
              >{{ line }}</div></pre>
            </template>
          </el-card>

          <el-dialog v-model="viewVisible" title="配置内容" width="80%" top="5vh">
            <pre class="config-view">{{ viewContent }}</pre>
          </el-dialog>
        </el-tab-pane>
      </el-tabs>
    </template>
  </div>
</template>

<style scoped>
.card {
  margin-bottom: 16px;
}

.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.latest-cards {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-top: 16px;
}

.latest-item {
  padding: 12px 24px;
  background: var(--op-bg-subtle);
  border-radius: var(--op-radius-md);
  text-align: center;
  min-width: 110px;
}

.latest-value {
  font-size: 20px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.latest-label {
  color: var(--op-text-tertiary);
  font-size: 12px;
  margin-top: 4px;
}

.range-row {
  display: flex;
  align-items: center;
  gap: 16px;
}

.range-label {
  font-weight: 600;
}

.chart-title {
  color: var(--op-text-secondary);
  font-size: 13px;
  margin-bottom: 4px;
}

.pager {
  margin-top: 12px;
  justify-content: flex-end;
}

.diff-view,
.config-view {
  font-family: 'Courier New', Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  background: var(--op-bg-subtle);
  border-radius: var(--op-radius-sm);
  padding: 12px;
  margin: 12px 0 0;
  max-height: 60vh;
  overflow: auto;
  white-space: pre;
}

.diff-view .diff-add {
  color: var(--op-color-success);
  background: #e9f7ee;
}

.diff-view .diff-del {
  color: var(--op-color-danger);
  background: #fdecec;
}
</style>
