<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listCredentials, createCredential, updateCredential, deleteCredential,
} from '../api'
import { CREDENTIAL_KINDS, kindLabel, fmtTime } from '../utils/dicts'

const loading = ref(false)
const rows = ref([])

async function fetchList() {
  loading.value = true
  try {
    rows.value = await listCredentials()
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
  kind: 'snmp_v2c',
  // payload 各字段（按 kind 动态展示；编辑时留空表示不修改）
  community: '',
  port: null,
  username: '',
  auth_protocol: 'SHA',
  auth_key: '',
  priv_protocol: 'AES',
  priv_key: '',
  password: '',
  db_type: 'mysql',
  database: '',
})
const form = reactive(emptyForm())

const rules = {
  name: [{ required: true, message: '请输入凭据名称', trigger: 'blur' }],
  kind: [{ required: true, message: '请选择凭据类型', trigger: 'change' }],
}

// 编辑模式下敏感字段留空 = 不修改，因此都不必填
const payloadRules = computed(() => {
  if (dialog.isEdit) return {}
  const r = {}
  if (form.kind === 'snmp_v2c') {
    r.community = [{ required: true, message: '请输入团体名', trigger: 'blur' }]
  } else if (form.kind === 'snmp_v3') {
    r.username = [{ required: true, message: '请输入用户名', trigger: 'blur' }]
    if (form.auth_protocol !== 'none') {
      r.auth_key = [{ required: true, message: '请输入认证密钥', trigger: 'blur' }]
    }
    if (form.priv_protocol !== 'none') {
      r.priv_key = [{ required: true, message: '请输入加密密钥', trigger: 'blur' }]
    }
  } else if (form.kind === 'ssh') {
    r.username = [{ required: true, message: '请输入用户名', trigger: 'blur' }]
    r.password = [{ required: true, message: '请输入密码', trigger: 'blur' }]
  } else if (form.kind === 'database') {
    r.db_type = [{ required: true, message: '请选择数据库类型', trigger: 'change' }]
    r.username = [{ required: true, message: '请输入用户名', trigger: 'blur' }]
    r.password = [{ required: true, message: '请输入密码', trigger: 'blur' }]
  }
  return r
})

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
  // 编辑时 payload 全部留空，表示不修改密钥
  Object.assign(form, { ...emptyForm(), name: row.name, kind: row.kind })
}

// 组装 payload：只提交非空字段；编辑时全空则提交 {}（后端视为不改密钥）
function buildPayload() {
  const p = {}
  const put = (k, v) => {
    if (v !== '' && v !== null && v !== undefined) p[k] = v
  }
  if (form.kind === 'snmp_v2c') {
    put('community', form.community)
    put('port', form.port)
  } else if (form.kind === 'snmp_v3') {
    put('username', form.username)
    p.auth_protocol = form.auth_protocol
    put('auth_key', form.auth_key)
    p.priv_protocol = form.priv_protocol
    put('priv_key', form.priv_key)
    put('port', form.port)
  } else if (form.kind === 'ssh') {
    put('username', form.username)
    put('password', form.password)
    put('port', form.port)
  } else if (form.kind === 'database') {
    p.db_type = form.db_type
    put('username', form.username)
    put('password', form.password)
    put('port', form.port)
    put('database', form.database)
  }
  return p
}

async function submit() {
  await formRef.value.validate()
  saving.value = true
  try {
    const data = { name: form.name, kind: form.kind, payload: buildPayload() }
    if (dialog.isEdit) {
      await updateCredential(dialog.id, data)
      ElMessage.success('凭据已更新')
    } else {
      await createCredential(data)
      ElMessage.success('凭据已创建')
    }
    dialog.visible = false
    fetchList()
  } finally {
    saving.value = false
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除凭据「${row.name}」吗？`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await deleteCredential(row.id)
  ElMessage.success('已删除')
  fetchList()
}

onMounted(fetchList)
</script>

<template>
  <el-card shadow="never">
    <div class="toolbar">
      <el-button type="success" :icon="'Plus'" @click="openCreate">新增凭据</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column prop="name" label="名称" min-width="160" />
      <el-table-column label="类型" width="140">
        <template #default="{ row }">
          <el-tag effect="plain">{{ kindLabel(row.kind) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
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
      :title="dialog.isEdit ? '编辑凭据' : '新增凭据'"
      width="520px"
      destroy-on-close
    >
      <el-alert
        v-if="dialog.isEdit"
        type="info"
        :closable="false"
        title="密钥字段留空表示不修改"
        class="edit-tip"
      />
      <el-form
        ref="formRef"
        :model="form"
        :rules="{ ...rules, ...payloadRules }"
        label-width="100px"
      >
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="如：核心交换机 SNMP" />
        </el-form-item>
        <el-form-item label="类型" prop="kind">
          <el-select v-model="form.kind" style="width: 100%" :disabled="dialog.isEdit">
            <el-option v-for="k in CREDENTIAL_KINDS" :key="k.value" :label="k.label" :value="k.value" />
          </el-select>
        </el-form-item>

        <!-- SNMP v2c -->
        <template v-if="form.kind === 'snmp_v2c'">
          <el-form-item label="团体名" prop="community">
            <el-input v-model="form.community" type="password" show-password placeholder="如 public" />
          </el-form-item>
          <el-form-item label="端口" prop="port">
            <el-input-number v-model="form.port" :min="1" :max="65535" placeholder="默认 161" />
          </el-form-item>
        </template>

        <!-- SNMP v3 -->
        <template v-else-if="form.kind === 'snmp_v3'">
          <el-form-item label="用户名" prop="username">
            <el-input v-model="form.username" />
          </el-form-item>
          <el-form-item label="认证协议" prop="auth_protocol">
            <el-select v-model="form.auth_protocol" style="width: 100%">
              <el-option label="SHA" value="SHA" />
              <el-option label="MD5" value="MD5" />
              <el-option label="无认证" value="none" />
            </el-select>
          </el-form-item>
          <el-form-item label="认证密钥" prop="auth_key">
            <el-input v-model="form.auth_key" type="password" show-password />
          </el-form-item>
          <el-form-item label="加密协议" prop="priv_protocol">
            <el-select v-model="form.priv_protocol" style="width: 100%">
              <el-option label="AES" value="AES" />
              <el-option label="DES" value="DES" />
              <el-option label="无加密" value="none" />
            </el-select>
          </el-form-item>
          <el-form-item label="加密密钥" prop="priv_key">
            <el-input v-model="form.priv_key" type="password" show-password />
          </el-form-item>
          <el-form-item label="端口" prop="port">
            <el-input-number v-model="form.port" :min="1" :max="65535" placeholder="默认 161" />
          </el-form-item>
        </template>

        <!-- SSH -->
        <template v-else-if="form.kind === 'ssh'">
          <el-form-item label="用户名" prop="username">
            <el-input v-model="form.username" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input v-model="form.password" type="password" show-password />
          </el-form-item>
          <el-form-item label="端口" prop="port">
            <el-input-number v-model="form.port" :min="1" :max="65535" placeholder="默认 22" />
          </el-form-item>
        </template>

        <!-- 数据库 -->
        <template v-else-if="form.kind === 'database'">
          <el-form-item label="数据库类型" prop="db_type">
            <el-select v-model="form.db_type" style="width: 100%">
              <el-option label="MySQL" value="mysql" />
              <el-option label="Oracle" value="oracle" />
              <el-option label="SQL Server" value="sqlserver" />
              <el-option label="PostgreSQL" value="postgresql" />
            </el-select>
          </el-form-item>
          <el-form-item label="用户名" prop="username">
            <el-input v-model="form.username" placeholder="只读监控账号" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input v-model="form.password" type="password" show-password />
          </el-form-item>
          <el-form-item label="端口" prop="port">
            <el-input-number v-model="form.port" :min="1" :max="65535" placeholder="选填" />
          </el-form-item>
          <el-form-item :label="form.db_type === 'oracle' ? '服务名' : '库名'" prop="database">
            <el-input
              v-model="form.database"
              :placeholder="form.db_type === 'oracle' ? 'Oracle service_name，如 ORCLPDB1' : '选填'"
            />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.toolbar {
  margin-bottom: 16px;
}

.edit-tip {
  margin-bottom: 16px;
}
</style>
