<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  startScan, getScanJob, listScanJobs, importDevices, listCredentials,
} from '../api'
import { DEVICE_TYPES, fmtTime } from '../utils/dicts'

const credentials = ref([])

// ===== 扫描发起 =====
const scanForm = reactive({ ranges: '', credential_id: null })
const scanning = ref(false)
const parseErrors = ref([])
const job = ref(null) // 当前任务详情
let pollTimer = null

// 进度
const progress = computed(() => {
  if (!job.value || !job.value.total) return 0
  return Math.round((job.value.done / job.value.total) * 100)
})

async function handleScan() {
  if (!scanForm.ranges.trim()) {
    ElMessage.warning('请输入要扫描的 IP 段')
    return
  }
  scanning.value = true
  parseErrors.value = []
  job.value = null
  stopPoll()
  try {
    const res = await startScan({
      ranges: scanForm.ranges,
      credential_id: scanForm.credential_id || undefined,
    })
    parseErrors.value = res.parse_errors || []
    // 立即查一次任务详情，然后每秒轮询直到结束
    await pollOnce(res.job_id)
    pollTimer = setInterval(() => pollOnce(res.job_id), 1000)
  } catch {
    scanning.value = false
  }
}

async function pollOnce(jobId) {
  try {
    job.value = await getScanJob(jobId)
    if (job.value.status !== 'running') {
      stopPoll()
      scanning.value = false
      fetchJobs()
      if (job.value.status === 'failed') {
        ElMessage.error(`扫描失败：${job.value.error || '未知错误'}`)
      } else {
        ElMessage.success('扫描完成')
      }
    }
  } catch {
    // 单次轮询失败不阻塞
  }
}

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// ===== 批量入库 =====
const resultsRef = ref()
const selected = ref([])
const importDialog = reactive({ visible: false })
const importForm = reactive({ type: 'network', group_name: '', location: '', credential_id: null })
const importing = ref(false)

// 仅在线且未入库的行可勾选
const selectable = (row) => row.online && !row.already_added

function onSelectionChange(rows) {
  selected.value = rows
}

function openImport() {
  if (!selected.value.length) {
    ElMessage.warning('请先勾选要入库的在线 IP')
    return
  }
  importDialog.visible = true
}

async function submitImport() {
  importing.value = true
  try {
    const res = await importDevices({
      ips: selected.value.map((r) => r.ip),
      type: importForm.type,
      group_name: importForm.group_name || undefined,
      location: importForm.location || undefined,
      credential_id: importForm.credential_id || undefined,
    })
    importDialog.visible = false
    ElMessageBox.alert(
      `成功入库 ${res.created} 台，跳过（已存在）${res.skipped} 台`,
      '导入结果',
      { confirmButtonText: '知道了' }
    )
    // 刷新任务详情以更新「已入库」标记，并清空勾选
    resultsRef.value?.clearSelection()
    if (job.value) job.value = await getScanJob(job.value.id)
    fetchJobs()
  } finally {
    importing.value = false
  }
}

// ===== 历史任务 =====
const jobs = ref([])
const jobsLoading = ref(false)

async function fetchJobs() {
  jobsLoading.value = true
  try {
    jobs.value = await listScanJobs()
  } finally {
    jobsLoading.value = false
  }
}

// 查看历史任务详情（回填到结果区）
async function viewJob(row) {
  job.value = await getScanJob(row.id)
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

const jobStatusTag = (s) => ({ running: 'warning', done: 'success', failed: 'danger' }[s] || 'info')
const jobStatusLabel = (s) => ({ running: '扫描中', done: '已完成', failed: '失败' }[s] || s)

onMounted(async () => {
  fetchJobs()
  try {
    credentials.value = await listCredentials()
  } catch {
    // 静默失败
  }
})
onBeforeUnmount(stopPoll)
</script>

<template>
  <div>
    <!-- 扫描发起区 -->
    <el-card shadow="never" class="card">
      <template #header>发起扫描</template>
      <el-input
        v-model="scanForm.ranges"
        type="textarea"
        :rows="3"
        placeholder='支持网段（192.168.1.0/24）、范围（192.168.1.1-192.168.1.254）、单 IP，可用逗号、分号或换行分隔'
      />
      <div class="scan-actions">
        <el-select v-model="scanForm.credential_id" clearable placeholder="SNMP 凭据（选填）" style="width: 220px">
          <el-option v-for="c in credentials" :key="c.id" :label="c.name" :value="c.id" />
        </el-select>
        <el-button type="primary" :loading="scanning" :icon="'Search'" @click="handleScan">
          {{ scanning ? '扫描中…' : '开始扫描' }}
        </el-button>
      </div>
      <!-- 解析错误提示 -->
      <el-alert
        v-if="parseErrors.length"
        type="warning"
        :closable="false"
        class="parse-errors"
        title="以下输入无法解析，已忽略："
      >
        <div v-for="(e, i) in parseErrors" :key="i">{{ e }}</div>
      </el-alert>
      <!-- 扫描进度 -->
      <div v-if="job" class="progress">
        <el-progress :percentage="progress" :status="job.status === 'failed' ? 'exception' : undefined" />
        <span class="progress-text">
          {{ job.done }} / {{ job.total }}
          <template v-if="job.status === 'running'">（扫描中）</template>
        </span>
      </div>
    </el-card>

    <!-- 扫描结果 -->
    <el-card v-if="job && job.results && job.results.length" shadow="never" class="card">
      <template #header>
        <div class="result-header">
          <span>扫描结果（任务 #{{ job.id }}）</span>
          <el-button type="success" size="small" :disabled="!selected.length" @click="openImport">
            批量入库（已选 {{ selected.length }} 台）
          </el-button>
        </div>
      </template>
      <el-table
        ref="resultsRef"
        :data="job.results"
        stripe
        max-height="420"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="45" :selectable="selectable" />
        <el-table-column prop="ip" label="IP 地址" width="140" />
        <el-table-column label="在线" width="80">
          <template #default="{ row }">
            <el-tag :type="row.online ? 'success' : 'info'" size="small">
              {{ row.online ? '在线' : '离线' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时延" width="90">
          <template #default="{ row }">
            {{ row.latency_ms != null ? row.latency_ms + ' ms' : '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="sys_name" label="系统名称" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.sys_name || '-' }}</template>
        </el-table-column>
        <el-table-column prop="sys_descr" label="系统描述" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.sys_descr || '-' }}</template>
        </el-table-column>
        <el-table-column label="已入库" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.already_added" type="warning" size="small">已入库</el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 历史任务 -->
    <el-card shadow="never" class="card">
      <template #header>
        <div class="result-header">
          <span>历史扫描任务（最近 20 条）</span>
          <el-button size="small" :icon="'Refresh'" @click="fetchJobs">刷新</el-button>
        </div>
      </template>
      <el-table :data="jobs" v-loading="jobsLoading" stripe>
        <el-table-column prop="id" label="任务 ID" width="80" />
        <el-table-column prop="ranges" label="扫描范围" min-width="220" show-overflow-tooltip />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="jobStatusTag(row.status)" size="small">{{ jobStatusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="100">
          <template #default="{ row }">{{ row.done }} / {{ row.total }}</template>
        </el-table-column>
        <el-table-column label="发起时间" width="165">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="完成时间" width="165">
          <template #default="{ row }">{{ fmtTime(row.finished_at) }}</template>
        </el-table-column>
        <el-table-column prop="error" label="错误信息" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.error || '-' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="viewJob(row)">查看结果</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 批量入库对话框 -->
    <el-dialog v-model="importDialog.visible" title="批量入库" width="480px" destroy-on-close>
      <div class="import-tip">将 {{ selected.length }} 台在线设备入库：</div>
      <el-form :model="importForm" label-width="90px">
        <el-form-item label="设备类型" required>
          <el-select v-model="importForm.type" style="width: 100%">
            <el-option v-for="t in DEVICE_TYPES" :key="t.value" :label="t.label" :value="t.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="分组">
          <el-input v-model="importForm.group_name" placeholder="选填" />
        </el-form-item>
        <el-form-item label="位置">
          <el-input v-model="importForm.location" placeholder="选填" />
        </el-form-item>
        <el-form-item label="凭据">
          <el-select v-model="importForm.credential_id" clearable placeholder="选填" style="width: 100%">
            <el-option v-for="c in credentials" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="importDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="importing" @click="submitImport">确认入库</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.card {
  margin-bottom: 16px;
}

.scan-actions {
  display: flex;
  gap: 12px;
  margin-top: 12px;
}

.parse-errors {
  margin-top: 12px;
}

.progress {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}

.progress :deep(.el-progress) {
  flex: 1;
}

.progress-text {
  color: var(--op-text-secondary);
  white-space: nowrap;
}

.result-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.import-tip {
  margin-bottom: 12px;
  color: var(--op-text-secondary);
}
</style>
