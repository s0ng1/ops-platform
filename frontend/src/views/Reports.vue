<script setup>
// 报表页：可用率报表 / 流量报表 两个 tab，支持查询与 xlsx 导出
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  getReportAvailability, getReportTraffic, exportReportAvailability, exportReportTraffic,
} from '../api'
import { DEVICE_TYPES, typeLabel } from '../utils/dicts'
import { fmtBps, downloadBlob } from '../utils/format'

const tab = ref('availability')

// 默认近 7 天
function defaultRange() {
  const end = new Date()
  const start = new Date(end.getTime() - 6 * 24 * 3600e3)
  const p = (n) => String(n).padStart(2, '0')
  const fmt = (d) => `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
  return [fmt(start), fmt(end)]
}

// ===== 可用率报表 =====
const availQuery = reactive({ range: defaultRange(), device_type: '' })
const availRows = ref([])
const availLoading = ref(false)
const availExporting = ref(false)

function availParams() {
  const [start, end] = availQuery.range || []
  return { start, end, device_type: availQuery.device_type || undefined }
}

async function fetchAvail() {
  if (!availQuery.range?.length) {
    ElMessage.warning('请选择日期范围')
    return
  }
  availLoading.value = true
  try {
    const res = await getReportAvailability(availParams())
    availRows.value = res.rows || []
  } finally {
    availLoading.value = false
  }
}

async function exportAvail() {
  if (!availQuery.range?.length) {
    ElMessage.warning('请选择日期范围')
    return
  }
  availExporting.value = true
  try {
    const res = await exportReportAvailability(availParams())
    downloadBlob(res, '可用率报表.xlsx')
  } finally {
    availExporting.value = false
  }
}

// 可用率（0~1）格式化与着色
function fmtAvail(v) {
  return v == null ? '-' : `${(v * 100).toFixed(2)}%`
}
function availColor(v) {
  if (v == null) return 'var(--op-text-tertiary)'
  if (v >= 1) return 'var(--op-color-success)'
  if (v < 0.999) return 'var(--op-color-danger)'
  return 'var(--op-color-warning)'
}

// ===== 流量报表 =====
const trafficQuery = reactive({ range: defaultRange(), granularity: 'day', top: null })
const trafficRows = ref([])
const trafficLoading = ref(false)
const trafficExporting = ref(false)

function trafficParams() {
  const [start, end] = trafficQuery.range || []
  return {
    start,
    end,
    granularity: trafficQuery.granularity,
    top: trafficQuery.top || undefined,
  }
}

async function fetchTraffic() {
  if (!trafficQuery.range?.length) {
    ElMessage.warning('请选择日期范围')
    return
  }
  trafficLoading.value = true
  try {
    const res = await getReportTraffic(trafficParams())
    trafficRows.value = res.rows || []
  } finally {
    trafficLoading.value = false
  }
}

async function exportTraffic() {
  if (!trafficQuery.range?.length) {
    ElMessage.warning('请选择日期范围')
    return
  }
  trafficExporting.value = true
  try {
    const res = await exportReportTraffic(trafficParams())
    downloadBlob(res, '流量报表.xlsx')
  } finally {
    trafficExporting.value = false
  }
}
</script>

<template>
  <el-card shadow="never">
    <el-tabs v-model="tab">
      <!-- 可用率报表 -->
      <el-tab-pane label="可用率报表" name="availability">
        <el-form inline class="filters">
          <el-form-item label="日期范围">
            <el-date-picker
              v-model="availQuery.range"
              type="daterange"
              value-format="YYYY-MM-DD"
              range-separator="至"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              :clearable="false"
            />
          </el-form-item>
          <el-form-item label="设备类型">
            <el-select v-model="availQuery.device_type" clearable placeholder="全部" style="width: 150px">
              <el-option v-for="t in DEVICE_TYPES" :key="t.value" :label="t.label" :value="t.value" />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :icon="'Search'" :loading="availLoading" @click="fetchAvail">查询</el-button>
            <el-button :icon="'Download'" :loading="availExporting" @click="exportAvail">导出 Excel</el-button>
          </el-form-item>
        </el-form>

        <el-table :data="availRows" v-loading="availLoading" stripe>
          <el-table-column prop="device_name" label="设备名称" min-width="150" show-overflow-tooltip />
          <el-table-column prop="ip" label="IP" min-width="120" />
          <el-table-column label="类型" width="120">
            <template #default="{ row }">{{ typeLabel(row.type) }}</template>
          </el-table-column>
          <el-table-column prop="day" label="日期" width="110" />
          <el-table-column prop="total_points" label="采集点数" width="100" align="right" />
          <el-table-column prop="online_points" label="在线点数" width="100" align="right" />
          <el-table-column
            prop="availability"
            label="可用率"
            width="110"
            align="right"
            sortable
            :sort-method="(a, b) => (a.availability ?? 0) - (b.availability ?? 0)"
          >
            <template #default="{ row }">
              <span :style="{ color: availColor(row.availability), fontWeight: 600 }">
                {{ fmtAvail(row.availability) }}
              </span>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="暂无数据，请选择条件后查询" :image-size="60" />
          </template>
        </el-table>
      </el-tab-pane>

      <!-- 流量报表 -->
      <el-tab-pane label="流量报表" name="traffic">
        <el-form inline class="filters">
          <el-form-item label="日期范围">
            <el-date-picker
              v-model="trafficQuery.range"
              type="daterange"
              value-format="YYYY-MM-DD"
              range-separator="至"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              :clearable="false"
            />
          </el-form-item>
          <el-form-item label="粒度">
            <el-radio-group v-model="trafficQuery.granularity">
              <el-radio-button value="day">按日</el-radio-button>
              <el-radio-button value="month">按月</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="Top N">
            <el-input-number v-model="trafficQuery.top" :min="1" :max="100" placeholder="全部" style="width: 120px" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :icon="'Search'" :loading="trafficLoading" @click="fetchTraffic">查询</el-button>
            <el-button :icon="'Download'" :loading="trafficExporting" @click="exportTraffic">导出 Excel</el-button>
          </el-form-item>
        </el-form>

        <el-table :data="trafficRows" v-loading="trafficLoading" stripe>
          <el-table-column prop="device_name" label="设备" min-width="150" show-overflow-tooltip />
          <el-table-column prop="ip" label="IP" min-width="120" />
          <el-table-column prop="interface" label="接口" min-width="120" show-overflow-tooltip />
          <el-table-column prop="period" label="周期" width="110" />
          <el-table-column label="入均值" width="110" align="right">
            <template #default="{ row }">{{ fmtBps(row.in_avg) }}</template>
          </el-table-column>
          <el-table-column label="入峰值" width="110" align="right">
            <template #default="{ row }">{{ fmtBps(row.in_max) }}</template>
          </el-table-column>
          <el-table-column label="出均值" width="110" align="right">
            <template #default="{ row }">{{ fmtBps(row.out_avg) }}</template>
          </el-table-column>
          <el-table-column label="出峰值" width="110" align="right">
            <template #default="{ row }">{{ fmtBps(row.out_max) }}</template>
          </el-table-column>
          <el-table-column label="P95" width="110" align="right">
            <template #default="{ row }">{{ fmtBps(row.p95) }}</template>
          </el-table-column>
          <template #empty>
            <el-empty description="暂无数据，请选择条件后查询" :image-size="60" />
          </template>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<style scoped>
.filters {
  margin-bottom: 4px;
}
</style>
