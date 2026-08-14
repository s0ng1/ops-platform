<script setup>
// 审计日志（仅 admin）：筛选 + 分页表格
import { onMounted, reactive, ref } from 'vue'
import { listAudits } from '../api'
import { fmtTime } from '../utils/dicts'

const loading = ref(false)
const rows = ref([])
const total = ref(0)

// 筛选与分页
const query = reactive({ username: '', action: '', range: [] })
const page = ref(1)
const pageSize = ref(20)

async function fetchList() {
  loading.value = true
  try {
    const data = await listAudits({
      username: query.username || undefined,
      action: query.action || undefined,
      start: query.range?.[0] || undefined,
      end: query.range?.[1] || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    rows.value = data.items || []
    total.value = data.total || 0
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  page.value = 1
  fetchList()
}

function handleReset() {
  query.username = ''
  query.action = ''
  query.range = []
  page.value = 1
  fetchList()
}

onMounted(fetchList)
</script>

<template>
  <el-card shadow="never">
    <el-form inline class="filters">
      <el-form-item label="用户名">
        <el-input v-model="query.username" clearable placeholder="按用户名筛选" style="width: 150px" @keyup.enter="handleSearch" />
      </el-form-item>
      <el-form-item label="动作">
        <el-input v-model="query.action" clearable placeholder="如 login / device_create" style="width: 180px" @keyup.enter="handleSearch" />
      </el-form-item>
      <el-form-item label="时间范围">
        <el-date-picker
          v-model="query.range"
          type="datetimerange"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          value-format="YYYY-MM-DDTHH:mm:ss"
          style="width: 340px"
        />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :icon="'Search'" @click="handleSearch">查询</el-button>
        <el-button :icon="'RefreshLeft'" @click="handleReset">重置</el-button>
      </el-form-item>
    </el-form>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column label="时间" width="165">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column prop="username" label="用户" width="120" show-overflow-tooltip />
      <el-table-column prop="action" label="动作" width="150" show-overflow-tooltip />
      <el-table-column prop="target" label="对象" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ row.target || '-' }}</template>
      </el-table-column>
      <el-table-column prop="detail" label="详情" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.detail || '-' }}</template>
      </el-table-column>
      <el-table-column prop="ip" label="IP" width="130">
        <template #default="{ row }">{{ row.ip || '-' }}</template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        background
        @current-change="fetchList"
        @size-change="handleSearch"
      />
    </div>
  </el-card>
</template>

<style scoped>
.filters {
  margin-bottom: 4px;
}

.pager {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
