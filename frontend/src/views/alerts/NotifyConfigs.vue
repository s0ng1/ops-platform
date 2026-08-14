<script setup>
// 通知渠道 tab：smtp / 钉钉 / 企业微信，config 不回显，编辑留空表示不改
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listNotifyConfigs, createNotifyConfig, updateNotifyConfig, deleteNotifyConfig,
} from '../../api'

const KINDS = [
  { value: 'smtp', label: '邮件 SMTP' },
  { value: 'dingtalk', label: '钉钉机器人' },
  { value: 'wecom', label: '企业微信机器人' },
]
const kindLabel = (v) => KINDS.find((k) => k.value === v)?.label || v || '-'

const loading = ref(false)
const rows = ref([])

async function fetchList() {
  loading.value = true
  try {
    rows.value = await listNotifyConfigs()
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
  kind: 'smtp',
  enabled: true,
  // smtp 字段
  host: '',
  port: null,
  username: '',
  password: '',
  from_addr: '',
  to_addrs: [''],
  use_tls: true,
  // webhook 字段（dingtalk / wecom）
  webhook_url: '',
})
const form = reactive(emptyForm())

const rules = {
  name: [{ required: true, message: '请输入渠道名称', trigger: 'blur' }],
  kind: [{ required: true, message: '请选择渠道类型', trigger: 'change' }],
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
  // config 不回显：全部留空表示不修改
  Object.assign(form, { ...emptyForm(), name: row.name, kind: row.kind, enabled: row.enabled })
}

// 收件人动态行
function addToAddr() {
  form.to_addrs.push('')
}
function removeToAddr(i) {
  form.to_addrs.splice(i, 1)
  if (!form.to_addrs.length) form.to_addrs.push('')
}

// 组装 config：只提交非空字段；编辑时全空则提交 {}（不修改配置）
function buildConfig() {
  const c = {}
  const put = (k, v) => {
    if (v !== '' && v !== null && v !== undefined) c[k] = v
  }
  if (form.kind === 'smtp') {
    put('host', form.host)
    put('port', form.port)
    put('username', form.username)
    put('password', form.password)
    put('from_addr', form.from_addr)
    const addrs = form.to_addrs.map((a) => a.trim()).filter(Boolean)
    if (addrs.length) c.to_addrs = addrs
    // 布尔值仅在新建或已有其他字段时提交，避免编辑时误覆盖
    if (!dialog.isEdit || Object.keys(c).length) c.use_tls = form.use_tls
  } else {
    put('webhook_url', form.webhook_url)
  }
  return c
}

async function submit() {
  await formRef.value.validate()
  // 新建时的必填校验（编辑留空=不改）
  if (!dialog.isEdit) {
    if (form.kind === 'smtp') {
      if (!form.host || !form.from_addr || !form.to_addrs.some((a) => a.trim())) {
        ElMessage.warning('请填写 SMTP 服务器、发件地址和至少一个收件人')
        return
      }
    } else if (!form.webhook_url) {
      ElMessage.warning('请填写 Webhook 地址')
      return
    }
  }
  saving.value = true
  try {
    const data = { name: form.name, kind: form.kind, config: buildConfig(), enabled: form.enabled }
    if (dialog.isEdit) {
      await updateNotifyConfig(dialog.id, data)
      ElMessage.success('渠道已更新')
    } else {
      await createNotifyConfig(data)
      ElMessage.success('渠道已创建')
    }
    dialog.visible = false
    fetchList()
  } finally {
    saving.value = false
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除通知渠道「${row.name}」吗？`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await deleteNotifyConfig(row.id)
  ElMessage.success('已删除')
  fetchList()
}

onMounted(fetchList)
</script>

<template>
  <div>
    <div class="toolbar">
      <el-button type="success" :icon="'Plus'" @click="openCreate">新增渠道</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column prop="name" label="名称" min-width="160" />
      <el-table-column label="类型" width="160">
        <template #default="{ row }">
          <el-tag effect="plain">{{ kindLabel(row.kind) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="90">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
            {{ row.enabled ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="handleDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新增/编辑对话框 -->
    <el-dialog
      v-model="dialog.visible"
      :title="dialog.isEdit ? '编辑渠道' : '新增渠道'"
      width="560px"
      destroy-on-close
    >
      <el-alert
        v-if="dialog.isEdit"
        type="info"
        :closable="false"
        title="配置字段不回显，留空表示不修改"
        class="edit-tip"
      />
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="如：运维值班邮箱" />
        </el-form-item>
        <el-form-item label="类型" prop="kind">
          <el-select v-model="form.kind" style="width: 100%" :disabled="dialog.isEdit">
            <el-option v-for="k in KINDS" :key="k.value" :label="k.label" :value="k.value" />
          </el-select>
        </el-form-item>

        <!-- SMTP -->
        <template v-if="form.kind === 'smtp'">
          <el-form-item label="服务器" prop="host">
            <el-input v-model="form.host" placeholder="如 smtp.163.com" />
          </el-form-item>
          <el-form-item label="端口" prop="port">
            <el-input-number v-model="form.port" :min="1" :max="65535" placeholder="默认 465/25" />
          </el-form-item>
          <el-form-item label="用户名" prop="username">
            <el-input v-model="form.username" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input v-model="form.password" type="password" show-password />
          </el-form-item>
          <el-form-item label="发件地址" prop="from_addr">
            <el-input v-model="form.from_addr" placeholder="如 nms@example.com" />
          </el-form-item>
          <el-form-item label="收件人">
            <div class="addr-list">
              <div v-for="(a, i) in form.to_addrs" :key="i" class="addr-row">
                <el-input v-model="form.to_addrs[i]" placeholder="收件邮箱" style="width: 260px" />
                <el-button link type="danger" :icon="'Delete'" @click="removeToAddr(i)" />
              </div>
              <el-button link type="primary" :icon="'Plus'" @click="addToAddr">添加收件人</el-button>
            </div>
          </el-form-item>
          <el-form-item label="TLS">
            <el-switch v-model="form.use_tls" />
          </el-form-item>
        </template>

        <!-- 钉钉 / 企业微信 -->
        <el-form-item v-else label="Webhook" prop="webhook_url">
          <el-input
            v-model="form.webhook_url"
            type="password"
            show-password
            placeholder="机器人 Webhook 地址"
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

.edit-tip {
  margin-bottom: 16px;
}

.addr-list {
  width: 100%;
}

.addr-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
</style>
