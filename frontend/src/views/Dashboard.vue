<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { getOverview } from '../api'
import { DEVICE_TYPES, typeLabel } from '../utils/dicts'
import { onWsMessage } from '../stores/ws'

// 总览数据，每 15 秒自动刷新
const overview = ref({ total: 0, online: 0, offline: 0, unknown: 0, by_type: {} })

async function refresh() {
  try {
    overview.value = await getOverview()
  } catch {
    // 错误提示已由拦截器统一处理，静默失败不阻塞
  }
}

let timer = null
// WS 设备状态变化时立即刷新一次计数（与 15 秒轮询结合）
const offWs = onWsMessage('device_status', () => refresh())

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 15000)
})
onBeforeUnmount(() => {
  clearInterval(timer)
  offWs()
})

const cards = [
  { key: 'total', label: '设备总数', color: 'var(--op-color-primary)', bg: 'var(--op-color-primary-light-9)', icon: 'Cpu' },
  { key: 'online', label: '在线', color: 'var(--op-color-success)', bg: '#e9f7ee', icon: 'CircleCheck' },
  { key: 'offline', label: '离线', color: 'var(--op-color-danger)', bg: '#fdecec', icon: 'CircleClose' },
  { key: 'unknown', label: '未知', color: 'var(--op-color-info)', bg: '#eef1f6', icon: 'QuestionFilled' },
]

// 类型统计最大值，用于占比条宽度
const typeMax = computed(() => {
  const vals = DEVICE_TYPES.map((t) => overview.value.by_type?.[t.value] ?? 0)
  return Math.max(...vals, 1)
})
</script>

<template>
  <div>
    <!-- 状态计数卡片 -->
    <el-row :gutter="16">
      <el-col v-for="c in cards" :key="c.key" :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-body">
            <div class="stat-icon" :style="{ color: c.color, background: c.bg }">
              <el-icon :size="22"><component :is="c.icon" /></el-icon>
            </div>
            <div>
              <div class="stat-num">{{ overview[c.key] ?? 0 }}</div>
              <div class="stat-label">{{ c.label }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 按类型统计 -->
    <el-card shadow="never" class="type-card">
      <template #header>按类型统计</template>
      <div class="type-list">
        <div v-for="t in DEVICE_TYPES" :key="t.value" class="type-item">
          <div class="type-head">
            <span class="type-name">{{ typeLabel(t.value) }}</span>
            <span class="type-num">{{ overview.by_type?.[t.value] ?? 0 }}</span>
          </div>
          <div class="type-bar">
            <div
              class="type-bar-fill"
              :style="{ width: `${((overview.by_type?.[t.value] ?? 0) / typeMax) * 100}%` }"
            />
          </div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.stat-card {
  margin-bottom: 16px;
}

.stat-body {
  display: flex;
  align-items: center;
  gap: 16px;
}

.stat-icon {
  width: 48px;
  height: 48px;
  border-radius: var(--op-radius-lg);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.stat-num {
  font-size: 28px;
  font-weight: 600;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}

.stat-label {
  color: var(--op-text-tertiary);
  font-size: 13px;
  margin-top: 2px;
}

.type-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
}

.type-item {
  padding: 14px 16px;
  background: var(--op-bg-subtle);
  border-radius: var(--op-radius-md);
}

.type-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 10px;
}

.type-name {
  color: var(--op-text-secondary);
  font-size: 13px;
}

.type-num {
  font-size: 20px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.type-bar {
  height: 6px;
  border-radius: 3px;
  background: var(--op-border-light);
  overflow: hidden;
}

.type-bar-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--op-color-primary);
  transition: width 0.4s ease;
}
</style>
