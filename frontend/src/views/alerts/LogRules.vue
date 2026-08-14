<script setup>
// 日志规则 tab：Syslog/Trap 匹配规则 CRUD（命中后按 alert_severity 产生告警）
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listLogRules, createLogRule, updateLogRule, deleteLogRule } from '../../api'
import {
  SEVERITY_ORDER, severityLabel, severityColor, syslogSeverityLabel,
} from '../../utils/dicts'

const loading = ref(false)
const rows = ref([])

async function fetchList() {
  loading.value = true
  try {
    rows.value = await listLogRules()
  } finally {
    loading.value = false
  }
}

// ===== 新增/编辑对话框 =====
const dialog = reactive({ visible: false, isEdit: false, id: null })
const formRef = ref()
const saving = ref(false)

const emptyForm = () => ({
  name: '',
  enabled: true,
  source_ip: '',
  keyword: '',
  severity_lte: null,
  alert_severity: 'warning',
})
const form = reactive(emptyForm())

const rules = {
  name: [{ required: true, message: '请输入规则名称', trigger: 'blur' }],
  alert_severity: [{ required: true, message: '请选择告警等级', trigger: 'change' }],
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
    enabled: row.enabled,
    source_ip: row.source_ip || '',
    keyword: row.keyword || '',
    severity_lte: row.severity_lte ?? null,
    alert_severity: row.alert_severity,
  })
}

function buildPayload() {
  return {
    name: form.name,
    enabled: form.enabled,
    source_ip: form.source_ip || null,
    keyword: form.keyword || null,
    severity_lte: form.severity_lte ?? null,
    alert_severity: form.alert_severity,
  }
}

async function submit() {
  await formRef.value.validate()
  saving.value = true
  try {
    if (dialog.isEdit) {
      await updateLogRule(dialog.id, buildPayload())
      ElMessage.success('规则已更新')
    } else {
      await createLogRule(buildPayload())
      ElMessage.success('规则已创建')
    }
    dialog.visible = false
    fetchList()
  } finally {
    saving.value = false
  }
}

// 表格内开关快速启停
async function toggleEnabled(row) {
  await updateLogRule(row.id, {
    name: row.name,
    enabled: row.enabled,
    source_ip: row.source_ip,
    keyword: row.keyword,
    severity_lte: row.severity_lte,
    alert_severity: row.alert_severity,
  })
  ElMessage.success(row.enabled ? '已启用' : '已停用')
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除规则「${row.name}」吗？`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await deleteLogRule(row.id)
  ElMessage.success('已删除')
  fetchList()
}

// 匹配条件展示
function condText(row) {
  const parts = []
  if (row.source_ip) parts.push(`来源=${row.source_ip}`)
  if (row.keyword) parts.push(`包含「${row.keyword}」`)
  if (row.severity_lte !== null && row.severity_lte !== undefined) {
    parts.push(`等级≤${row.severity_lte}`)
  }
  return parts.length ? parts.join('，') : '全部日志'
}

onMounted(fetchList)
</script>

<template>
  <div>
    <div class="toolbar">
      <el-button type="success" :icon="'Plus'" @click="openCreate">新增规则</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column prop="name" label="规则名" min-width="150" show-overflow-tooltip />
      <el-table-column label="匹配条件" min-width="220">
        <template #default="{ row }">{{ condText(row) }}</template>
      </el-table-column>
      <el-table-column label="告警等级" width="100">
        <template #default="{ row }">
          <el-tag :color="severityColor(row.alert_severity)" effect="dark" class="sev-tag">
            {{ severityLabel(row.alert_severity) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="70">
        <template #default="{ row }">
          <el-switch v-model="row.enabled" @change="toggleEnabled(row)" />
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
      :title="dialog.isEdit ? '编辑日志规则' : '新增日志规则'"
      width="520px"
      destroy-on-close
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="110px">
        <el-form-item label="规则名称" prop="name">
          <el-input v-model="form.name" placeholder="如：核心交换机错误日志" />
        </el-form-item>
        <el-form-item label="来源 IP">
          <el-input v-model="form.source_ip" placeholder="留空=任意来源" />
        </el-form-item>
        <el-form-item label="内容关键字">
          <el-input v-model="form.keyword" placeholder="对日志内容做子串匹配，留空=不限" />
        </el-form-item>
        <el-form-item label="Syslog 等级≤">
          <el-select v-model="form.severity_lte" clearable placeholder="不限（仅 Syslog 生效）" style="width: 100%">
            <el-option v-for="n in 8" :key="n - 1" :label="syslogSeverityLabel(n - 1)" :value="n - 1" />
          </el-select>
        </el-form-item>
        <el-form-item label="告警等级" prop="alert_severity">
          <el-select v-model="form.alert_severity" style="width: 100%">
            <el-option v-for="s in SEVERITY_ORDER" :key="s" :label="severityLabel(s)" :value="s" />
          </el-select>
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

.sev-tag {
  border: none;
  color: #fff;
}
</style>
