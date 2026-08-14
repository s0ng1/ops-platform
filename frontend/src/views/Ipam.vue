<script setup>
// 轻量 IPAM：上半子网 /24 网格（在线/近7天活跃/更早/未见 四色），下半终端清单
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getIpamSubnets, listIpamInventory, updateIpamInventory } from '../api'
import { fmtTime, IPAM_SOURCES, ipamSourceLabel } from '../utils/dicts'
import { useAuth } from '../stores/auth'

const { state } = useAuth()
// 白名单/备注维护需要 operator 及以上（viewer 调 PUT 会被后端 403）
const canEdit = computed(() => state.role === 'admin' || state.role === 'operator')

// ===== 子网网格 =====
const subnetsLoading = ref(false)
const subnets = ref([])
const totalSubnets = ref(0)

async function fetchSubnets() {
  subnetsLoading.value = true
  try {
    const data = await getIpamSubnets({ prefix_len: 24 })
    subnets.value = data.subnets
    totalSubnets.value = data.total_subnets
  } catch {
    // 静默失败，保留下半表格可用
  } finally {
    subnetsLoading.value = false
  }
}

// 把一个 /24 的库存记录展开成 256 格；库存没有的格为「未见」态
function gridCells(subnet) {
  const base = subnet.subnet.split('/')[0].split('.').slice(0, 3).join('.')
  const byLast = {}
  for (const rec of subnet.ips) {
    byLast[rec.ip.split('.')[3]] = rec
  }
  const cells = []
  for (let i = 0; i < 256; i++) {
    const rec = byLast[String(i)]
    cells.push(rec ? { ...rec, last: i } : { last: i, ip: `${base}.${i}`, status: 'unseen' })
  }
  return cells
}

const STATUS_TEXT = { online: '在线', active7d: '近 7 天活跃', stale: '更早', unseen: '未见' }

// 悬停 tooltip（原生 title，轻量；256 格 × N 子网不适合 el-tooltip）
function cellTitle(cell) {
  if (cell.status === 'unseen') return `${cell.ip}\n未见`
  const lines = [
    cell.ip,
    `状态：${STATUS_TEXT[cell.status]}`,
    cell.mac ? `MAC：${cell.mac}` : '',
    cell.hostname ? `主机名：${cell.hostname}` : '',
    cell.device_name || cell.if_name
      ? `接入：${[cell.device_name, cell.if_name].filter(Boolean).join(' / ')}`
      : '',
    `来源：${ipamSourceLabel(cell.source)}`,
    `最近在线：${fmtTime(cell.last_seen)}`,
  ]
  return lines.filter(Boolean).join('\n')
}

// 点有记录的格 → 下方清单按该 IP 过滤
function clickCell(cell) {
  if (cell.status === 'unseen') return
  query.keyword = cell.ip
  query.subnet = ''
  search()
}

// 点子网标题 → 按子网过滤清单
function clickSubnet(subnet) {
  query.subnet = subnet.subnet
  query.keyword = ''
  search()
}

// ===== 终端清单 =====
const loading = ref(false)
const rows = ref([])
const total = ref(0)

const query = reactive({
  keyword: '',
  mac: '',
  subnet: '',
  source: '',
  whitelisted: '',
  page: 1,
  page_size: 50,
})

async function fetchList() {
  loading.value = true
  try {
    const params = {
      keyword: query.keyword || undefined,
      mac: query.mac || undefined,
      subnet: query.subnet || undefined,
      source: query.source || undefined,
      whitelisted: query.whitelisted === '' ? undefined : query.whitelisted,
      page: query.page,
      page_size: query.page_size,
    }
    const data = await listIpamInventory(params)
    rows.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function search() {
  query.page = 1
  fetchList()
}

function reset() {
  Object.assign(query, { keyword: '', mac: '', subnet: '', source: '', whitelisted: '', page: 1 })
  fetchList()
}

// 白名单开关
async function toggleWhitelist(row) {
  try {
    await updateIpamInventory(row.id, { whitelisted: row.whitelisted })
    ElMessage.success(row.whitelisted ? `已把 ${row.ip} 加入白名单` : `已把 ${row.ip} 移出白名单`)
  } catch {
    row.whitelisted = !row.whitelisted // 失败回滚开关状态
  }
}

// hostname 备注编辑
const editVisible = ref(false)
const editRow = ref(null)
const editHostname = ref('')

function openEdit(row) {
  editRow.value = row
  editHostname.value = row.hostname || ''
  editVisible.value = true
}

async function saveEdit() {
  try {
    await updateIpamInventory(editRow.value.id, { hostname: editHostname.value })
    ElMessage.success('备注已保存')
    editVisible.value = false
    fetchList()
  } catch {
    // 拦截器已提示
  }
}

onMounted(() => {
  fetchSubnets()
  fetchList()
})
</script>

<template>
  <div>
    <!-- 子网网格 -->
    <el-card v-loading="subnetsLoading" shadow="never" class="subnet-card">
      <template #header>
        <div class="card-header">
          <span>子网使用情况（/24）</span>
          <span class="legend">
            <i class="dot online" />在线
            <i class="dot active7d" />近 7 天活跃
            <i class="dot stale" />更早
            <i class="dot unseen" />未见
          </span>
        </div>
      </template>
      <el-empty v-if="!subnets.length && !subnetsLoading" description="暂无台账数据，等待 IPAM 采集或扫描回写" />
      <div class="subnets">
        <div v-for="s in subnets" :key="s.subnet" class="subnet">
          <div class="subnet-title" :title="`点击按 ${s.subnet} 过滤清单`" @click="clickSubnet(s)">
            <span class="subnet-name">{{ s.subnet }}</span>
            <span class="subnet-stats">共 {{ s.total }} · 在线 {{ s.online }} · 7天 {{ s.active7d }}</span>
          </div>
          <div class="grid">
            <div
              v-for="cell in gridCells(s)"
              :key="cell.last"
              class="cell"
              :class="cell.status"
              :title="cellTitle(cell)"
              @click="clickCell(cell)"
            />
          </div>
        </div>
      </div>
      <div v-if="totalSubnets > subnets.length" class="truncated">
        共 {{ totalSubnets }} 个子网，仅显示前 {{ subnets.length }} 个
      </div>
    </el-card>

    <!-- 终端清单 -->
    <el-card shadow="never" class="list-card">
      <template #header><span>终端清单</span></template>
      <div class="toolbar">
        <el-input v-model="query.keyword" placeholder="IP 关键字" clearable style="width: 150px" @change="search" />
        <el-input v-model="query.mac" placeholder="MAC 关键字" clearable style="width: 150px" @change="search" />
        <el-input v-model="query.subnet" placeholder="子网，如 203.0.113.0/24" clearable style="width: 180px" @change="search" />
        <el-select v-model="query.source" placeholder="来源" clearable style="width: 120px" @change="search">
          <el-option v-for="s in IPAM_SOURCES" :key="s.value" :label="s.label" :value="s.value" />
        </el-select>
        <el-select v-model="query.whitelisted" placeholder="白名单" clearable style="width: 110px" @change="search">
          <el-option label="白名单" value="true" />
          <el-option label="非白名单" value="false" />
        </el-select>
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="reset">重置</el-button>
      </div>

      <el-table :data="rows" v-loading="loading" stripe>
        <el-table-column prop="ip" label="IP" width="130" />
        <el-table-column label="MAC" width="150">
          <template #default="{ row }">{{ row.mac || '-' }}</template>
        </el-table-column>
        <el-table-column label="主机名/备注" min-width="130">
          <template #default="{ row }">
            <span>{{ row.hostname || '-' }}</span>
            <el-button v-if="canEdit" link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.source === 'arp' ? 'success' : 'info'" effect="plain">
              {{ ipamSourceLabel(row.source) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="接入设备" min-width="140">
          <template #default="{ row }">{{ row.device_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="接入端口" width="170">
          <template #default="{ row }">{{ row.if_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="首次发现" width="165">
          <template #default="{ row }">{{ fmtTime(row.first_seen) }}</template>
        </el-table-column>
        <el-table-column label="最近在线" width="165">
          <template #default="{ row }">{{ fmtTime(row.last_seen) }}</template>
        </el-table-column>
        <el-table-column label="白名单" width="80" align="center">
          <template #default="{ row }">
            <el-switch
              v-model="row.whitelisted"
              :disabled="!canEdit"
              @change="toggleWhitelist(row)"
            />
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="query.page"
        v-model:page-size="query.page_size"
        :total="total"
        :page-sizes="[20, 50, 100, 200]"
        layout="total, sizes, prev, pager, next"
        class="pager"
        @current-change="fetchList"
        @size-change="search"
      />
    </el-card>

    <!-- hostname 备注编辑 -->
    <el-dialog v-model="editVisible" :title="`备注 - ${editRow?.ip || ''}`" width="420px">
      <el-input v-model="editHostname" maxlength="128" placeholder="主机名或备注（留空清除）" />
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.subnet-card {
  margin-bottom: 16px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.legend {
  font-size: 12px;
  color: var(--op-text-tertiary);
  display: flex;
  align-items: center;
  gap: 6px;
}

.dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  margin-left: 12px;
}

.dot.online { background-color: var(--op-color-success); }
.dot.active7d { background-color: var(--op-color-warning); }
.dot.stale { background-color: var(--op-text-tertiary); }
.dot.unseen { background-color: var(--op-border); }

.subnets {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}

.subnet-title {
  cursor: pointer;
  margin-bottom: 6px;
  display: flex;
  gap: 10px;
  align-items: baseline;
}

.subnet-title:hover .subnet-name {
  color: var(--op-color-primary);
}

.subnet-name {
  font-weight: 600;
  font-size: 13px;
}

.subnet-stats {
  font-size: 12px;
  color: var(--op-text-tertiary);
}

.grid {
  display: grid;
  grid-template-columns: repeat(16, 14px);
  gap: 2px;
}

.cell {
  width: 14px;
  height: 14px;
  border-radius: 2px;
  cursor: pointer;
}

.cell.online { background-color: var(--op-color-success); }
.cell.active7d { background-color: var(--op-color-warning); }
.cell.stale { background-color: var(--op-text-tertiary); }
.cell.unseen { background-color: var(--op-border); cursor: default; }

.truncated {
  margin-top: 10px;
  font-size: 12px;
  color: var(--op-text-tertiary);
}

.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.pager {
  margin-top: 16px;
  justify-content: flex-end;
}
</style>
