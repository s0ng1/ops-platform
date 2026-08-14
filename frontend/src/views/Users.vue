<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listUsers, createUser, updateUser, deleteUser } from '../api'
import { ROLES, roleLabel, fmtTime } from '../utils/dicts'
import { useAuth } from '../stores/auth'

const { state } = useAuth()

const loading = ref(false)
const rows = ref([])

// 当前登录用户自己的行：禁用「禁用/降级」操作（后端同样校验，这里只做提示）
const isSelf = (row) => row.username === state.username

async function fetchList() {
  loading.value = true
  try {
    rows.value = await listUsers()
  } finally {
    loading.value = false
  }
}

// ===== 新增用户对话框 =====
const dialog = reactive({ visible: false })
const formRef = ref()
const saving = ref(false)
const form = reactive({ username: '', password: '', role: 'viewer' })

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
  role: [{ required: true, message: '请选择角色', trigger: 'change' }],
}

function openCreate() {
  dialog.visible = true
  Object.assign(form, { username: '', password: '', role: 'viewer' })
}

async function submit() {
  await formRef.value.validate()
  saving.value = true
  try {
    await createUser(form)
    ElMessage.success('用户已创建')
    dialog.visible = false
    fetchList()
  } finally {
    saving.value = false
  }
}

async function handleRoleChange(row, role) {
  try {
    await updateUser(row.id, { role })
    ElMessage.success(`「${row.username}」角色已改为${roleLabel(role)}`)
  } catch {
    // 失败（如最后一个管理员不可降级）：刷新列表还原下拉显示
    fetchList()
    return
  }
  fetchList()
}

async function handleToggleDisabled(row) {
  const disabling = !row.disabled
  try {
    await ElMessageBox.confirm(
      `确定${disabling ? '禁用' : '启用'}用户「${row.username}」吗？` +
        (disabling ? '禁用后该用户立即无法登录，已签发的登录态同时失效。' : ''),
      '确认',
      { type: 'warning', confirmButtonText: disabling ? '禁用' : '启用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await updateUser(row.id, { disabled: disabling })
  ElMessage.success(disabling ? '已禁用' : '已启用')
  fetchList()
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除用户「${row.username}」吗？`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await deleteUser(row.id)
  ElMessage.success('已删除')
  fetchList()
}

onMounted(fetchList)
</script>

<template>
  <el-card shadow="never">
    <div class="toolbar">
      <el-button type="success" :icon="'Plus'" @click="openCreate">新增用户</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column prop="username" label="用户名" min-width="140" />
      <el-table-column label="角色" width="160">
        <template #default="{ row }">
          <el-tooltip
            content="不能修改自己的角色"
            placement="top"
            :disabled="!isSelf(row)"
          >
            <span>
              <el-select
                :model-value="row.role"
                :disabled="isSelf(row)"
                style="width: 130px"
                @change="(v) => handleRoleChange(row, v)"
              >
                <el-option v-for="r in ROLES" :key="r.value" :label="r.label" :value="r.value" />
              </el-select>
            </span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.disabled ? 'info' : 'success'">
            {{ row.disabled ? '已禁用' : '启用中' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="170" fixed="right">
        <template #default="{ row }">
          <el-tooltip
            content="不能禁用或启用自己"
            placement="top"
            :disabled="!isSelf(row)"
          >
            <span>
              <el-button
                link
                :type="row.disabled ? 'success' : 'warning'"
                :disabled="isSelf(row)"
                @click="handleToggleDisabled(row)"
              >
                {{ row.disabled ? '启用' : '禁用' }}
              </el-button>
            </span>
          </el-tooltip>
          <el-button
            link
            type="danger"
            :disabled="row.username === state.username"
            @click="handleDelete(row)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新增用户对话框 -->
    <el-dialog v-model="dialog.visible" title="新增用户" width="440px" destroy-on-close>
      <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="角色" prop="role">
          <el-select v-model="form.role" style="width: 100%">
            <el-option v-for="r in ROLES" :key="r.value" :label="r.label" :value="r.value" />
          </el-select>
        </el-form-item>
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
</style>
