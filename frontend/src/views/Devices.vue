<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listDevices, createDevice, updateDevice, deleteDevice, probeDevice, listCredentials,
  getTopologyGroups,
} from '../api'
import {
  DEVICE_TYPES, typeLabel, statusLabel, statusTag, fmtTime, isValidIp,
} from '../utils/dicts'
import { onWsMessage } from '../stores/ws'

const route = useRoute()
const router = useRouter()

// 页面范围由路由 meta.types 决定（网络设备页 / 服务器设备页复用本组件）
const pageTypes = computed(() => route.meta.types || DEVICE_TYPES.map((t) => t.value))
// 类型筛选/表单下拉只提供当前范围内的类型
const typeOptions = computed(() => DEVICE_TYPES.filter((t) => pageTypes.value.includes(t.value)))

const loading = ref(false)
const devices = ref([])
const credentials = ref([])

// 查询条件（keyword/类型/状态均走后端参数；types 限定页面范围；分组前端过滤）
const query = reactive({ keyword: '', status: '', type: '', group: '' })

// 是否网络设备页（分组是拓扑概念，只在网络设备页提供分组过滤）
const isNetworkPage = computed(() => pageTypes.value.includes('network'))

// 现有分组清单（拓扑分组 API：network/security 设备去重 + 设备数）
const groups = ref([])
async function fetchGroups() {
  try {
    groups.value = await getTopologyGroups()
  } catch {
    // 静默失败，分组下拉为空
  }
}

// 分组过滤在前端做（列表本就全量加载 + 前端分页）
const filteredDevices = computed(() =>
  query.group ? devices.value.filter((d) => d.group_name === query.group) : devices.value
)

// 分页（前端分页，数据量第 1 期可控）
const page = reactive({ current: 1, size: 20 })
const pagedDevices = computed(() => {
  const start = (page.current - 1) * page.size
  return filteredDevices.value.slice(start, start + page.size)
})

async function fetchDevices() {
  loading.value = true
  try {
    devices.value = await listDevices({
      keyword: query.keyword || undefined,
      status: query.status || undefined,
      type: query.type || undefined,
      types: pageTypes.value.join(','),
    })
  } finally {
    loading.value = false
  }
}

async function fetchCredentials() {
  try {
    credentials.value = await listCredentials()
  } catch {
    // 静默失败，下拉为空
  }
}

function handleSearch() {
  page.current = 1
  fetchDevices()
}

// 两个设备页之间切换时组件复用：重置筛选并重新拉取
watch(pageTypes, () => {
  query.keyword = ''
  query.status = ''
  query.type = ''
  query.group = ''
  page.current = 1
  fetchDevices()
})

// ===== 新增/编辑对话框 =====
const dialog = reactive({ visible: false, isEdit: false, id: null })
const formRef = ref()
const saving = ref(false)
// 应用拨测配置（仅 type=application 使用），字段按 probe_kind 动态显示
const emptyProbe = () => ({
  probe_kind: 'http', url: '', expect_status: null, keyword: '',
  domain: '', expect_ip: '', server: '', host: '', port: null, banner: '', password: '', timeout: 5,
})
// 新增表单默认类型取当前页面范围的第一种
const emptyForm = () => ({
  name: '', ip: '', type: pageTypes.value[0], subtype: '', group_name: '', location: '',
  credential_id: null, ssh_credential_id: null, monitor_enabled: true, probe: emptyProbe(),
})
const form = reactive(emptyForm())

const PROBE_KINDS = [
  { value: 'http', label: 'HTTP/HTTPS' },
  { value: 'dns', label: 'DNS 解析' },
  { value: 'tcp', label: 'TCP 端口' },
  { value: 'nginx', label: 'Nginx' },
  { value: 'redis', label: 'Redis' },
]
const isApplication = computed(() => form.type === 'application')
// 主机名校验（application 的 IP 字段语义为目标主机，允许域名）
const HOST_RE = /^[A-Za-z0-9._-]+$/

// 网络/安全设备的细分类型选项（拓扑图标按此区分）
const SUBTYPE_OPTIONS = {
  network: [
    { value: 'switch', label: '交换机' },
    { value: 'router', label: '路由器' },
  ],
  security: [
    { value: 'firewall', label: '防火墙' },
  ],
}
const subtypeOptions = computed(() => SUBTYPE_OPTIONS[form.type] || [])
// 备份凭据下拉只列 SSH 类型凭据（配置备份专用辅槽）
const sshCredentials = computed(() => credentials.value.filter((c) => c.kind === 'ssh'))

const rules = {
  ip: [
    { required: true, message: '请输入 IP 地址', trigger: 'blur' },
    {
      validator: (r, v, cb) => {
        if (isApplication.value) {
          return HOST_RE.test(v || '') ? cb() : cb(new Error('请输入目标主机（IP 或域名）'))
        }
        return isValidIp(v) ? cb() : cb(new Error('IP 格式不正确'))
      },
      trigger: 'blur',
    },
  ],
  type: [{ required: true, message: '请选择设备类型', trigger: 'change' }],
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
    name: row.name || '',
    ip: row.ip,
    type: row.type,
    subtype: row.subtype || '',
    group_name: row.group_name || '',
    location: row.location || '',
    credential_id: row.credential_id ?? null,
    ssh_credential_id: row.ssh_credential_id ?? null,
    monitor_enabled: row.monitor_enabled,
    probe: Object.assign(emptyProbe(), row.probe_config || {}),
  })
}

// 按 probe_kind 挑出有效字段，空值不落库
function buildProbeConfig() {
  const p = form.probe
  const cfg = { probe_kind: p.probe_kind }
  if (p.timeout) cfg.timeout = p.timeout
  if (p.probe_kind === 'http') {
    cfg.url = p.url || ''
    if (p.expect_status) cfg.expect_status = p.expect_status
    if (p.keyword) cfg.keyword = p.keyword
  } else if (p.probe_kind === 'dns') {
    cfg.domain = p.domain || ''
    if (p.expect_ip) cfg.expect_ip = p.expect_ip
    if (p.server) cfg.server = p.server
  } else if (p.probe_kind === 'tcp') {
    cfg.port = p.port
    if (p.banner) cfg.banner = p.banner
  } else if (p.probe_kind === 'nginx') {
    cfg.url = p.url || ''
  } else if (p.probe_kind === 'redis') {
    if (p.host) cfg.host = p.host
    if (p.port) cfg.port = p.port
    if (p.password) cfg.password = p.password
  }
  return cfg
}

// 提交表单：空值字段发空串（后端 schema 是非空 str，发 null 会 422）
async function submit() {
  await formRef.value.validate()
  saving.value = true
  try {
    const payload = {
      name: form.name || '',
      ip: form.ip,
      type: form.type,
      subtype: form.subtype || '',
      group_name: form.group_name || '',
      location: form.location || '',
      credential_id: form.credential_id || null,
      ssh_credential_id: form.ssh_credential_id || null,
      monitor_enabled: form.monitor_enabled,
    }
    if (isApplication.value) payload.probe_config = buildProbeConfig()
    if (dialog.isEdit) {
      await updateDevice(dialog.id, payload)
      ElMessage.success('设备已更新')
    } else {
      await createDevice(payload)
      ElMessage.success('设备已创建')
    }
    dialog.visible = false
    fetchDevices()
  } finally {
    saving.value = false
  }
}

// 立即探测（行级 loading）
const probingIds = ref(new Set())
async function handleProbe(row) {
  probingIds.value = new Set([...probingIds.value, row.id])
  try {
    const updated = await probeDevice(row.id)
    ElMessage.success(`探测完成：${statusLabel(updated.status)}`)
    // 用返回的最新设备数据局部更新
    const idx = devices.value.findIndex((d) => d.id === row.id)
    if (idx !== -1) devices.value[idx] = updated
  } finally {
    const next = new Set(probingIds.value)
    next.delete(row.id)
    probingIds.value = next
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除设备「${row.name || row.ip}」吗？`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await deleteDevice(row.id)
  ElMessage.success('已删除')
  fetchDevices()
}

onMounted(() => {
  fetchDevices()
  fetchCredentials()
  fetchGroups()
})

// WS：设备状态变化时局部更新对应行，不整表刷新
const offWs = onWsMessage('device_status', (msg) => {
  const d = devices.value.find((x) => x.id === msg.device_id)
  if (d) {
    d.status = msg.status
    if (msg.name) d.name = msg.name
  }
})
onBeforeUnmount(offWs)
</script>

<template>
  <div>
    <!-- 搜索与筛选 -->
    <el-card shadow="never" class="toolbar">
      <el-form inline @submit.prevent="handleSearch">
        <el-form-item>
          <el-input
            v-model="query.keyword"
            placeholder="名称 / IP 关键字"
            clearable
            style="width: 220px"
            @clear="handleSearch"
            @keyup.enter="handleSearch"
          />
        </el-form-item>
        <el-form-item>
          <el-select v-model="query.type" placeholder="设备类型" clearable style="width: 150px">
            <el-option v-for="t in typeOptions" :key="t.value" :label="t.label" :value="t.value" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-select v-model="query.status" placeholder="状态" clearable style="width: 120px">
            <el-option label="在线" value="online" />
            <el-option label="离线" value="offline" />
            <el-option label="未知" value="unknown" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="isNetworkPage">
          <el-select
            v-model="query.group"
            placeholder="分组"
            clearable
            filterable
            style="width: 150px"
            @change="page.current = 1"
          >
            <el-option v-for="g in groups" :key="g.name" :label="`${g.name}（${g.count}）`" :value="g.name" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :icon="'Search'" @click="handleSearch">查询</el-button>
          <el-button :icon="'Plus'" @click="openCreate">新增设备</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 设备表格 -->
    <el-card shadow="never">
      <el-table :data="pagedDevices" v-loading="loading" stripe>
        <el-table-column prop="name" label="名称" min-width="120">
          <template #default="{ row }">{{ row.name || '-' }}</template>
        </el-table-column>
        <el-table-column prop="ip" label="IP 地址" min-width="130" />
        <el-table-column label="类型" width="120">
          <template #default="{ row }">{{ typeLabel(row.type) }}</template>
        </el-table-column>
        <el-table-column label="分组" min-width="100">
          <template #default="{ row }">{{ row.group_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status)">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时延" width="90">
          <template #default="{ row }">
            {{ row.last_latency_ms != null ? row.last_latency_ms + ' ms' : '-' }}
          </template>
        </el-table-column>
        <el-table-column label="最后在线时间" width="165">
          <template #default="{ row }">{{ fmtTime(row.last_seen) }}</template>
        </el-table-column>
        <el-table-column label="凭据" min-width="110">
          <template #default="{ row }">{{ row.credential_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="监控" width="70">
          <template #default="{ row }">
            <el-tag :type="row.monitor_enabled ? 'success' : 'info'" size="small">
              {{ row.monitor_enabled ? '开启' : '关闭' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push(`/devices/${row.id}`)">详情</el-button>
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button
              link
              type="primary"
              :loading="probingIds.has(row.id)"
              @click="handleProbe(row)"
            >
              立即探测
            </el-button>
            <el-button link type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-model:current-page="page.current"
        v-model:page-size="page.size"
        :total="filteredDevices.length"
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next"
        class="pager"
      />
    </el-card>

    <!-- 新增/编辑对话框 -->
    <el-dialog
      v-model="dialog.visible"
      :title="dialog.isEdit ? '编辑设备' : '新增设备'"
      width="520px"
      destroy-on-close
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="选填，默认同 IP" />
        </el-form-item>
        <el-form-item :label="isApplication ? '目标主机' : 'IP 地址'" prop="ip">
          <el-input v-model="form.ip" :placeholder="isApplication ? 'IP 或域名，如 web.internal' : '如 192.168.1.1'" />
        </el-form-item>
        <el-form-item label="设备类型" prop="type">
          <el-select v-model="form.type" style="width: 100%">
            <el-option v-for="t in typeOptions" :key="t.value" :label="t.label" :value="t.value" />
          </el-select>
        </el-form-item>
        <template v-if="isApplication">
          <el-form-item label="拨测类型">
            <el-select v-model="form.probe.probe_kind" style="width: 100%">
              <el-option v-for="k in PROBE_KINDS" :key="k.value" :label="k.label" :value="k.value" />
            </el-select>
          </el-form-item>
          <template v-if="form.probe.probe_kind === 'http'">
            <el-form-item label="URL" required>
              <el-input v-model="form.probe.url" placeholder="如 http://web.internal:8080/health" />
            </el-form-item>
            <el-form-item label="期望状态码">
              <el-input-number v-model="form.probe.expect_status" :min="100" :max="599" placeholder="默认 200~399" style="width: 100%" />
            </el-form-item>
            <el-form-item label="响应关键字">
              <el-input v-model="form.probe.keyword" placeholder="选填，命中响应体才算可用" />
            </el-form-item>
          </template>
          <template v-else-if="form.probe.probe_kind === 'dns'">
            <el-form-item label="域名" required>
              <el-input v-model="form.probe.domain" placeholder="如 www.example.com" />
            </el-form-item>
            <el-form-item label="期望 IP">
              <el-input v-model="form.probe.expect_ip" placeholder="选填，解析结果须包含该 IP" />
            </el-form-item>
            <el-form-item label="DNS 服务器">
              <el-input v-model="form.probe.server" placeholder="选填，默认系统解析" />
            </el-form-item>
          </template>
          <template v-else-if="form.probe.probe_kind === 'tcp'">
            <el-form-item label="端口" required>
              <el-input-number v-model="form.probe.port" :min="1" :max="65535" style="width: 100%" />
            </el-form-item>
            <el-form-item label="Banner 匹配">
              <el-input v-model="form.probe.banner" placeholder="选填，如 SSH-2.0" />
            </el-form-item>
          </template>
          <template v-else-if="form.probe.probe_kind === 'nginx'">
            <el-form-item label="stub_status URL" required>
              <el-input v-model="form.probe.url" placeholder="如 http://host/nginx_status" />
            </el-form-item>
          </template>
          <template v-else-if="form.probe.probe_kind === 'redis'">
            <el-form-item label="主机">
              <el-input v-model="form.probe.host" placeholder="选填，默认取目标主机" />
            </el-form-item>
            <el-form-item label="端口">
              <el-input-number v-model="form.probe.port" :min="1" :max="65535" placeholder="默认 6379" style="width: 100%" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="form.probe.password" type="password" show-password placeholder="选填，无认证留空" />
            </el-form-item>
          </template>
          <el-form-item label="超时(秒)">
            <el-input-number v-model="form.probe.timeout" :min="1" :max="60" style="width: 100%" />
          </el-form-item>
        </template>
        <el-form-item v-if="subtypeOptions.length" label="细分类型" prop="subtype">
          <el-select v-model="form.subtype" placeholder="默认按设备类型" style="width: 100%">
            <el-option v-for="s in subtypeOptions" :key="s.value" :label="s.label" :value="s.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="分组" prop="group_name">
          <el-select
            v-model="form.group_name"
            placeholder="选填，可选择已有分组或输入新分组"
            clearable
            filterable
            allow-create
            style="width: 100%"
          >
            <el-option v-for="g in groups" :key="g.name" :label="g.name" :value="g.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="位置" prop="location">
          <el-input v-model="form.location" placeholder="选填，如机房/机柜" />
        </el-form-item>
        <el-form-item label="凭据" prop="credential_id">
          <el-select v-model="form.credential_id" clearable placeholder="选填" style="width: 100%">
            <el-option v-for="c in credentials" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="subtypeOptions.length" label="备份凭据(SSH)" prop="ssh_credential_id">
          <el-select v-model="form.ssh_credential_id" clearable placeholder="选填，用于配置备份" style="width: 100%">
            <el-option v-for="c in sshCredentials" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="监控开关" prop="monitor_enabled">
          <el-switch v-model="form.monitor_enabled" active-text="开启" inactive-text="关闭" />
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

.toolbar :deep(.el-form-item) {
  margin-bottom: 0;
}

.pager {
  margin-top: 16px;
  justify-content: flex-end;
}
</style>
