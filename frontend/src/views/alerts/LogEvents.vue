<script setup>
// 日志事件 tab：Syslog / SNMP Trap 事件过滤查询 + 分页
import { onMounted, reactive, ref } from 'vue'
import { listLogEvents } from '../../api'
import { fmtTime, syslogSeverityLabel } from '../../utils/dicts'

const loading = ref(false)
const rows = ref([])
const total = ref(0)

const query = reactive({
  source_ip: '',
  kind: '',
  severity: null,
  keyword: '',
  range: [], // [开始时间, 结束时间]
  page: 1,
  page_size: 50,
})

async function fetchList() {
  loading.value = true
  try {
    const params = {
      source_ip: query.source_ip || undefined,
      kind: query.kind || undefined,
      severity: query.severity ?? undefined,
      keyword: query.keyword || undefined,
      page: query.page,
      page_size: query.page_size,
    }
    if (query.range && query.range.length === 2) {
      params.start = query.range[0]
      params.end = query.range[1]
    }
    const data = await listLogEvents(params)
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
  Object.assign(query, { source_ip: '', kind: '', severity: null, keyword: '', range: [], page: 1 })
  fetchList()
}

onMounted(fetchList)
</script>

<template>
  <div>
    <div class="toolbar">
      <el-input v-model="query.source_ip" placeholder="来源 IP" clearable style="width: 150px" @change="search" />
      <el-select v-model="query.kind" placeholder="类型" clearable style="width: 110px" @change="search">
        <el-option label="Syslog" value="syslog" />
        <el-option label="Trap" value="trap" />
      </el-select>
      <el-select v-model="query.severity" placeholder="等级" clearable style="width: 110px" @change="search">
        <el-option v-for="n in 8" :key="n - 1" :label="syslogSeverityLabel(n - 1)" :value="n - 1" />
      </el-select>
      <el-input v-model="query.keyword" placeholder="内容关键字" clearable style="width: 180px" @change="search" />
      <el-date-picker
        v-model="query.range"
        type="datetimerange"
        range-separator="至"
        start-placeholder="开始时间"
        end-placeholder="结束时间"
        value-format="YYYY-MM-DDTHH:mm:ss"
        style="width: 340px"
        @change="search"
      />
      <el-button type="primary" @click="search">查询</el-button>
      <el-button @click="reset">重置</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" stripe>
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column prop="source_ip" label="来源 IP" width="130" />
      <el-table-column label="类型" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.kind === 'trap' ? 'warning' : 'info'" effect="plain">
            {{ row.kind === 'trap' ? 'Trap' : 'Syslog' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="等级" width="90">
        <template #default="{ row }">{{ syslogSeverityLabel(row.severity) }}</template>
      </el-table-column>
      <el-table-column prop="message" label="内容" min-width="320" show-overflow-tooltip />
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
  </div>
</template>

<style scoped>
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
