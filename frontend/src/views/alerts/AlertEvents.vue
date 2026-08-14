<script setup>
// 告警事件 tab：筛选 + 表格 + 确认/关闭操作，支持顶栏 severity 跳转参数
import { onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listAlertEvents, getAlertEvent, ackAlertEvent, resolveAlertEvent, listAlertRules } from '../../api'
import {
  SEVERITY_ORDER, severityLabel, severityColor, EVENT_STATUS,
  metricLabel, fmtLabels, fmtTime,
} from '../../utils/dicts'
import { fmtBps } from '../../utils/format'
import { onWsMessage } from '../../stores/ws'

const route = useRoute()
const loading = ref(false)
const rows = ref([])
const rulesById = ref({})  // rule_id -> 规则（取 op/threshold 显示阈值；规则被删则只显示当前值）

// 按指标类型格式化数值：用量/利用率类加 %，时延加 ms，速率类用 fmtBps
function fmtMetricValue(metric, v) {
  if (v == null || Number.isNaN(Number(v))) return '-'
  const n = Number(v)
  if (metric.endsWith('_bps')) return fmtBps(n)
  if (metric === 'ping_latency_ms') return `${n.toFixed(1)} ms`
  if (/(_usage|_util|_pct)$/.test(metric)) return `${n.toFixed(1)}%`
  return n.toFixed(1)
}

// 当前值 / 阈值（如 `63.0% / > 85%`）；规则已删除或查不到时只显示当前值
function fmtValueThreshold(row) {
  const val = fmtMetricValue(row.metric, row.value)
  const rule = rulesById.value[row.rule_id]
  if (!rule) return val
  // 动态基线规则：阈值语义是 N 倍标准差
  if (rule.op === 'baseline_dev') return `${val} / 偏离基线 > ${rule.threshold}σ`
  const thr = row.metric.endsWith('_bps')
    ? fmtBps(rule.threshold)
    : row.metric === 'ping_latency_ms'
      ? `${rule.threshold} ms`
      : /(_usage|_util|_pct)$/.test(row.metric)
        ? `${rule.threshold}%`
        : `${rule.threshold}`
  return `${val} / ${rule.op} ${thr}`
}

// ===== 触发时指标快照（行展开时按需拉详情，列表接口不带快照）=====
const details = ref({})  // event_id -> 详情（含 snapshot）；'loading' 表示加载中

async function onExpand(row, expandedRows) {
  if (!expandedRows.includes(row) || details.value[row.id]) return
  details.value[row.id] = 'loading'
  try {
    details.value[row.id] = await getAlertEvent(row.id)
  } catch {
    details.value[row.id] = { snapshot: null }  // 失败按无快照展示，不影响列表
  }
}

// 快照条目列表（空快照不显示表格）
function snapshotItems(row) {
  const d = details.value[row.id]
  if (!d || d === 'loading' || !d.snapshot) return []
  return d.snapshot.items || []
}

// 筛选条件：顶栏色块点击会带 severity 参数
const query = reactive({ status: '', severity: route.query.severity || '' })

// 顶栏重复点击时同步筛选
watch(
  () => route.query.severity,
  (v) => {
    query.severity = v || ''
    fetchList()
  }
)

async function fetchList() {
  loading.value = true
  try {
    rows.value = await listAlertEvents({
      status: query.status || undefined,
      severity: query.severity || undefined,
      limit: 200,
    })
  } finally {
    loading.value = false
  }
}

// 确认告警
async function handleAck(row) {
  await ackAlertEvent(row.id)
  ElMessage.success('已确认')
  fetchList()
}

// 关闭（手动恢复）告警
async function handleResolve(row) {
  try {
    await ElMessageBox.confirm('确定手动关闭该告警吗？', '提示', { type: 'warning' })
  } catch {
    return
  }
  await resolveAlertEvent(row.id)
  ElMessage.success('已关闭')
  fetchList()
}

// 规则表一次性加载，用于「当前值 / 阈值」列；失败不影响列表本身
async function fetchRules() {
  try {
    const list = await listAlertRules()
    rulesById.value = Object.fromEntries(list.map((r) => [r.id, r]))
  } catch {
    rulesById.value = {}
  }
}

// 新告警实时刷新列表
const offWs = onWsMessage('alert', () => fetchList())

onMounted(() => {
  fetchRules()
  fetchList()
})
onBeforeUnmount(offWs)
</script>

<template>
  <div>
    <el-form inline class="filters">
      <el-form-item label="状态">
        <el-select v-model="query.status" clearable placeholder="全部" style="width: 120px" @change="fetchList">
          <el-option label="告警中" value="firing" />
          <el-option label="已恢复" value="resolved" />
        </el-select>
      </el-form-item>
      <el-form-item label="等级">
        <el-select v-model="query.severity" clearable placeholder="全部" style="width: 120px" @change="fetchList">
          <el-option v-for="s in SEVERITY_ORDER" :key="s" :label="severityLabel(s)" :value="s" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button :icon="'Refresh'" @click="fetchList">刷新</el-button>
      </el-form-item>
    </el-form>

    <el-table :data="rows" v-loading="loading" stripe @expand-change="onExpand">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="snapshot-box">
            <template v-if="details[row.id] === 'loading'">快照加载中…</template>
            <el-table v-else-if="snapshotItems(row).length" :data="snapshotItems(row)" size="small" max-height="320">
              <el-table-column label="指标" min-width="180">
                <template #default="{ row: item }">{{ metricLabel(item.metric) }}</template>
              </el-table-column>
              <el-table-column label="触发时取值" width="140">
                <template #default="{ row: item }">{{ fmtMetricValue(item.metric, item.value) }}</template>
              </el-table-column>
              <el-table-column label="标签" min-width="160">
                <template #default="{ row: item }">{{ fmtLabels(item.labels) }}</template>
              </el-table-column>
              <el-table-column label="采集时间" width="165">
                <template #default="{ row: item }">{{ fmtTime(item.time) }}</template>
              </el-table-column>
            </el-table>
            <span v-else class="no-snapshot">该事件无触发时指标快照</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="等级" width="90">
        <template #default="{ row }">
          <el-tag :color="severityColor(row.severity)" effect="dark" class="sev-tag">
            {{ severityLabel(row.severity) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="rule_name" label="规则" min-width="140" show-overflow-tooltip />
      <el-table-column label="设备" min-width="150">
        <template #default="{ row }">
          {{ row.device_name || '-' }}
          <span class="ip">{{ row.device_ip }}</span>
        </template>
      </el-table-column>
      <el-table-column label="指标" min-width="180">
        <template #default="{ row }">
          {{ metricLabel(row.metric) }}
          <span v-if="fmtLabels(row.labels) !== '-'" class="labels">（{{ fmtLabels(row.labels) }}）</span>
        </template>
      </el-table-column>
      <el-table-column label="当前值 / 阈值" width="160">
        <template #default="{ row }">{{ fmtValueThreshold(row) }}</template>
      </el-table-column>
      <el-table-column label="触发时间" width="165">
        <template #default="{ row }">{{ fmtTime(row.fired_at) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="130">
        <template #default="{ row }">
          <el-tag :type="EVENT_STATUS[row.status]?.tag || 'info'" size="small">
            {{ EVENT_STATUS[row.status]?.label || row.status }}
          </el-tag>
          <el-tag v-if="row.silenced" type="info" size="small" effect="plain" class="silenced-tag">
            已静默
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="确认人" width="90">
        <template #default="{ row }">{{ row.ack_by || '-' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="130" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.status === 'firing' && !row.ack_by"
            link
            type="primary"
            @click="handleAck(row)"
          >
            确认
          </el-button>
          <el-button v-if="row.status === 'firing'" link type="danger" @click="handleResolve(row)">
            关闭
          </el-button>
          <span v-if="row.status !== 'firing'" class="resolved-at">
            {{ fmtTime(row.resolved_at) }}
          </span>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.filters {
  margin-bottom: 4px;
}

.sev-tag {
  border: none;
  color: #fff;
}

.ip {
  color: var(--op-text-tertiary);
  font-size: 12px;
}

.labels {
  color: var(--op-text-tertiary);
  font-size: 12px;
}

.resolved-at {
  color: var(--op-text-tertiary);
  font-size: 12px;
}

.silenced-tag {
  margin-left: 4px;
}

.snapshot-box {
  padding: 8px 24px;
  color: var(--op-text-tertiary);
  font-size: 13px;
}

.no-snapshot {
  color: var(--op-text-tertiary);
  font-size: 13px;
}
</style>
