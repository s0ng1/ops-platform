<script setup>
// 规则模板 tab：模板列表（可多选批量生成规则）+ 模板 CRUD；内置模板禁删改
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listAlertTemplates, createAlertTemplate, updateAlertTemplate, deleteAlertTemplate,
  instantiateAlertTemplates, listDevices,
} from '../../api'
import {
  DEVICE_TYPES, SEVERITY_ORDER, severityLabel, severityColor,
  ALERT_OPS, COMMON_METRICS, metricLabel, fmtLabels, typeLabel,
} from '../../utils/dicts'

const loading = ref(false)
const rows = ref([])
const devices = ref([])
const selected = ref([])       // 勾选的模板
const overrideType = ref('')   // 批量生成时覆盖设备类型（空=按模板自带）
const instantiating = ref(false)

async function fetchList() {
  loading.value = true
  try {
    rows.value = await listAlertTemplates()
  } finally {
    loading.value = false
  }
}

// ===== 批量生成规则 =====
async function instantiate() {
  if (!selected.value.length) {
    ElMessage.warning('请先勾选模板')
    return
  }
  instantiating.value = true
  try {
    const r = await instantiateAlertTemplates({
      template_ids: selected.value.map((t) => t.id),
      device_type: overrideType.value || '',
    })
    ElMessage.success(`批量生成完成：新建 ${r.created.length} 条，跳过 ${r.skipped.length} 条（同名规则已存在）`)
  } finally {
    instantiating.value = false
  }
}

// ===== 新增/编辑对话框 =====
const dialog = reactive({ visible: false, isEdit: false, id: null })
const formRef = ref()
const saving = ref(false)

const emptyForm = () => ({
  name: '',
  description: '',
  metric: '',
  op: '>',
  threshold: 80,
  duration_cycles: 1,
  severity: 'warning',
  device_type: '',
  group_name: '',
  device_id: null,
  labelRows: [],  // labels_filter 的 key=value 动态行
})
const form = reactive(emptyForm())

const rules = {
  name: [{ required: true, message: '请输入模板名称', trigger: 'blur' }],
  metric: [{ required: true, message: '请选择或输入指标名', trigger: 'change' }],
  op: [{ required: true, message: '请选择比较符', trigger: 'change' }],
  severity: [{ required: true, message: '请选择等级', trigger: 'change' }],
}

function openCreate() {
  dialog.visible = true
  dialog.isEdit = false
  dialog.id = null
  Object.assign(form, emptyForm())
}

function openEdit(row) {
  dialog.visible = true
  dialog.isEdit = true
  dialog.id = row.id
  Object.assign(form, {
    ...emptyForm(),
    name: row.name,
    description: row.description || '',
    metric: row.metric,
    op: row.op,
    threshold: row.threshold,
    duration_cycles: row.duration_cycles ?? 1,
    severity: row.severity,
    device_type: row.device_type || '',
    group_name: row.group_name || '',
    device_id: row.device_id ?? null,
    labelRows: Object.entries(row.labels_filter || {}).map(([key, value]) => ({ key, value })),
  })
}

function addLabelRow() {
  form.labelRows.push({ key: '', value: '' })
}
function removeLabelRow(i) {
  form.labelRows.splice(i, 1)
}

function buildPayload() {
  const labels_filter = {}
  form.labelRows.forEach(({ key, value }) => {
    if (key) labels_filter[key] = value
  })
  return {
    name: form.name,
    description: form.description,
    metric: form.metric,
    op: form.op,
    threshold: form.threshold,
    duration_cycles: form.duration_cycles,
    severity: form.severity,
    device_type: form.device_type || '',
    group_name: form.group_name || '',
    device_id: form.device_id || null,
    labels_filter: Object.keys(labels_filter).length ? labels_filter : null,
  }
}

async function submit() {
  await formRef.value.validate()
  saving.value = true
  try {
    if (dialog.isEdit) {
      await updateAlertTemplate(dialog.id, buildPayload())
      ElMessage.success('模板已更新')
    } else {
      await createAlertTemplate(buildPayload())
      ElMessage.success('模板已创建')
    }
    dialog.visible = false
    fetchList()
  } finally {
    saving.value = false
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除模板「${row.name}」吗？`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await deleteAlertTemplate(row.id)
  ElMessage.success('已删除')
  fetchList()
}

// 适用范围展示（同规则 tab 口径）
function scopeText(row) {
  const parts = []
  if (row.device_id) {
    const d = devices.value.find((x) => x.id === row.device_id)
    parts.push(`设备：${d ? d.name || d.ip : row.device_id}`)
  }
  if (row.device_type) parts.push(`类型：${typeLabel(row.device_type)}`)
  if (row.group_name) parts.push(`分组：${row.group_name}`)
  return parts.length ? parts.join(' / ') : '全部设备'
}

onMounted(async () => {
  fetchList()
  try {
    devices.value = await listDevices({})
  } catch {
    // 静默失败，设备下拉为空
  }
})
</script>

<template>
  <div>
    <div class="toolbar">
      <el-button type="success" :icon="'Plus'" @click="openCreate">新增模板</el-button>
      <el-divider direction="vertical" />
      <el-button
        type="primary"
        :icon="'MagicStick'"
        :disabled="!selected.length"
        :loading="instantiating"
        @click="instantiate"
      >批量生成规则（已选 {{ selected.length }}）</el-button>
      <el-select v-model="overrideType" clearable placeholder="覆盖设备类型（选填）" style="width: 180px">
        <el-option v-for="t in DEVICE_TYPES" :key="t.value" :label="t.label" :value="t.value" />
      </el-select>
      <span class="hint">同名规则已存在则跳过，可重复执行</span>
    </div>

    <el-table :data="rows" v-loading="loading" stripe @selection-change="(v) => (selected = v)">
      <el-table-column type="selection" width="45" />
      <el-table-column prop="name" label="模板名" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.name }}
          <el-tag v-if="row.builtin" size="small" type="info" effect="plain">内置</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="指标" min-width="150">
        <template #default="{ row }">
          {{ metricLabel(row.metric) }}
          <div v-if="fmtLabels(row.labels_filter) !== '-'" class="sub">
            {{ fmtLabels(row.labels_filter) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="条件" width="150">
        <template #default="{ row }">
          <span v-if="row.op === 'baseline_dev'">偏离基线 &gt; {{ row.threshold }}σ</span>
          <span v-else>{{ row.op }} {{ row.threshold }}</span>
          ，持续 {{ row.duration_cycles }} 周期
        </template>
      </el-table-column>
      <el-table-column label="等级" width="90">
        <template #default="{ row }">
          <el-tag :color="severityColor(row.severity)" effect="dark" class="sev-tag">
            {{ severityLabel(row.severity) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="适用范围" min-width="140">
        <template #default="{ row }">{{ scopeText(row) }}</template>
      </el-table-column>
      <el-table-column prop="description" label="说明" min-width="160" show-overflow-tooltip />
      <el-table-column label="操作" width="130" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="!!row.builtin" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" :disabled="!!row.builtin" @click="handleDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新增/编辑对话框 -->
    <el-dialog
      v-model="dialog.visible"
      :title="dialog.isEdit ? '编辑模板' : '新增模板'"
      width="560px"
      destroy-on-close
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="模板名称" prop="name">
          <el-input v-model="form.name" placeholder="如：CPU 使用率过高" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" placeholder="选填" />
        </el-form-item>
        <el-form-item label="指标" prop="metric">
          <el-select v-model="form.metric" filterable allow-create default-first-option style="width: 100%">
            <el-option v-for="m in COMMON_METRICS" :key="m" :label="`${metricLabel(m)}（${m}）`" :value="m" />
          </el-select>
        </el-form-item>
        <el-form-item label="触发条件" required>
          <div class="cond-block">
            <div class="cond-row">
              <el-select v-model="form.op" style="width: 130px">
                <el-option v-for="op in ALERT_OPS" :key="op" :label="op" :value="op" />
                <el-option label="偏离基线 Nσ" value="baseline_dev" />
              </el-select>
              <el-input-number
                v-model="form.threshold"
                :step="form.op === 'baseline_dev' ? 0.5 : 1"
                :min="form.op === 'baseline_dev' ? 0.5 : undefined"
                style="width: 140px"
              />
              <span>持续</span>
              <el-input-number v-model="form.duration_cycles" :min="1" :max="100" style="width: 110px" />
              <span>周期</span>
            </div>
            <div v-if="form.op === 'baseline_dev'" class="hint">
              与近 7 天同时段均值比较，偏离超 N 倍标准差触发，建议 N=3；新设备样本不足 7 天内不触发
            </div>
          </div>
        </el-form-item>
        <el-form-item label="等级" prop="severity">
          <el-select v-model="form.severity" style="width: 100%">
            <el-option v-for="s in SEVERITY_ORDER" :key="s" :label="severityLabel(s)" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item label="适用范围">
          <div class="scope-row">
            <el-select v-model="form.device_type" clearable placeholder="设备类型（选填）" style="width: 160px">
              <el-option v-for="t in DEVICE_TYPES" :key="t.value" :label="t.label" :value="t.value" />
            </el-select>
            <el-input v-model="form.group_name" placeholder="分组（选填）" style="width: 130px" />
            <el-select v-model="form.device_id" clearable filterable placeholder="指定设备（选填）" style="width: 160px">
              <el-option v-for="d in devices" :key="d.id" :label="d.name || d.ip" :value="d.id" />
            </el-select>
          </div>
        </el-form-item>
        <el-form-item label="标签过滤">
          <div v-for="(lr, i) in form.labelRows" :key="i" class="label-row">
            <el-input v-model="lr.key" placeholder="键，如 if" style="width: 140px" />
            <span>=</span>
            <el-input v-model="lr.value" placeholder="值" style="width: 180px" />
            <el-button link type="danger" :icon="'Delete'" @click="removeLabelRow(i)" />
          </div>
          <el-button link type="primary" :icon="'Plus'" @click="addLabelRow">添加标签条件</el-button>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.sev-tag {
  border: none;
  color: #fff;
}

.sub {
  color: var(--op-text-tertiary);
  font-size: 12px;
}

.hint {
  margin-left: 8px;
  color: var(--op-text-tertiary);
  font-size: 12px;
}

.cond-row,
.scope-row,
.label-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.cond-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.label-row {
  margin-bottom: 8px;
}
</style>
