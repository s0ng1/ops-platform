<script setup>
// 静默窗口 tab：窗口列表 + 新增/编辑/删除，生效中状态由前端按当前时间计算
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listAlertSilences, createAlertSilence, updateAlertSilence, deleteAlertSilence, listDevices,
} from '../../api'
import { DEVICE_TYPES, typeLabel, fmtTime } from '../../utils/dicts'

const loading = ref(false)
const rows = ref([])
const devices = ref([])

async function fetchList() {
  loading.value = true
  try {
    rows.value = await listAlertSilences()
  } finally {
    loading.value = false
  }
}

// 当前时间（每 30s 刷新一次，用于计算“生效中”）
const now = ref(Date.now())
const timer = setInterval(() => {
  now.value = Date.now()
}, 30000)
onBeforeUnmount(() => clearInterval(timer))

function isActive(row) {
  if (!row.enabled) return false
  const start = new Date(row.start_at).getTime()
  const end = new Date(row.end_at).getTime()
  return !Number.isNaN(start) && !Number.isNaN(end) && start <= now.value && now.value <= end
}

// 选择器描述：device_type/group_name/device_id 与关系，全空=全部设备
function selectorText(row) {
  const parts = []
  if (row.device_id) {
    const d = devices.value.find((x) => x.id === row.device_id)
    parts.push(`设备：${d ? d.name || d.ip : row.device_id}`)
  }
  if (row.device_type) parts.push(`类型：${typeLabel(row.device_type)}`)
  if (row.group_name) parts.push(`分组：${row.group_name}`)
  return parts.length ? parts.join(' / ') : '全部设备'
}

// ===== 新增/编辑对话框 =====
const dialog = reactive({ visible: false, isEdit: false, id: null })
const formRef = ref()
const saving = ref(false)

const emptyForm = () => ({
  name: '',
  device_type: '',
  group_name: '',
  device_id: null,
  range: [],
  enabled: true,
})
const form = reactive(emptyForm())

const rules = {
  name: [{ required: true, message: '请输入窗口名称', trigger: 'blur' }],
  range: [
    { required: true, type: 'array', len: 2, message: '请选择起止时间', trigger: 'change' },
    {
      validator: (r, v, cb) => {
        if (v && v[0] && v[1] && new Date(v[1]) <= new Date(v[0])) {
          cb(new Error('结束时间必须晚于开始时间'))
        } else {
          cb()
        }
      },
      trigger: 'change',
    },
  ],
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
    device_type: row.device_type || '',
    group_name: row.group_name || '',
    device_id: row.device_id ?? null,
    range: [row.start_at, row.end_at],
    enabled: row.enabled,
  })
}

function buildPayload() {
  return {
    name: form.name,
    device_type: form.device_type || null,
    group_name: form.group_name || null,
    device_id: form.device_id || null,
    start_at: form.range[0],
    end_at: form.range[1],
    enabled: form.enabled,
  }
}

async function submit() {
  await formRef.value.validate()
  saving.value = true
  try {
    if (dialog.isEdit) {
      await updateAlertSilence(dialog.id, buildPayload())
      ElMessage.success('静默窗口已更新')
    } else {
      await createAlertSilence(buildPayload())
      ElMessage.success('静默窗口已创建')
    }
    dialog.visible = false
    fetchList()
  } finally {
    saving.value = false
  }
}

// 表格内开关快速启停
async function toggleEnabled(row) {
  const { id, created_at, ...rest } = row
  await updateAlertSilence(id, { ...rest, enabled: row.enabled })
  ElMessage.success(row.enabled ? '已启用' : '已停用')
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除静默窗口「${row.name}」吗？`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await deleteAlertSilence(row.id)
  ElMessage.success('已删除')
  fetchList()
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
      <el-button type="success" :icon="'Plus'" @click="openCreate">新增静默窗口</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
      <el-table-column label="选择器" min-width="180">
        <template #default="{ row }">{{ selectorText(row) }}</template>
      </el-table-column>
      <el-table-column label="开始时间" width="165">
        <template #default="{ row }">{{ fmtTime(row.start_at) }}</template>
      </el-table-column>
      <el-table-column label="结束时间" width="165">
        <template #default="{ row }">{{ fmtTime(row.end_at) }}</template>
      </el-table-column>
      <el-table-column label="启用" width="70">
        <template #default="{ row }">
          <el-switch v-model="row.enabled" @change="toggleEnabled(row)" />
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag v-if="isActive(row)" type="success" size="small" effect="dark">生效中</el-tag>
          <el-tag v-else type="info" size="small" effect="plain">未生效</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="130" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="handleDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新增/编辑对话框 -->
    <el-dialog
      v-model="dialog.visible"
      :title="dialog.isEdit ? '编辑静默窗口' : '新增静默窗口'"
      width="560px"
      destroy-on-close
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="窗口名称" prop="name">
          <el-input v-model="form.name" placeholder="如：周末维护静默" />
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
        <el-form-item label="起止时间" prop="range">
          <el-date-picker
            v-model="form.range"
            type="datetimerange"
            range-separator="至"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            value-format="YYYY-MM-DDTHH:mm:ss"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
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
}

.scope-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
