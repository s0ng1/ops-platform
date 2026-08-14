<script setup>
// 宿主机-应用总线视图（第 8 期 M5，参照北塔宿主机-数据库总线视图）
// 每台宿主机一张深色卡片：宿主机为总线基座（卡片顶部），同 IP 的 database/application
// 对象挂在总线上；状态环 + 告警角标；30s 轮询 + WS 设备状态联动，离开页面停止
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getBus } from '../api'
import { typeLabel, statusLabel, statusTag, severityColor } from '../utils/dicts'
import { onWsMessage } from '../stores/ws'

const router = useRouter()
const hosts = ref([])

async function refresh() {
  try {
    const data = await getBus()
    hosts.value = data.hosts || []
  } catch {
    // 错误提示已由拦截器统一处理，静默失败不阻塞
  }
}

// 状态环：红=critical/major firing，橙=warning/info firing，绿=无 firing 且在线，灰=离线/未启用
function ringClass(o) {
  if (!o.online) return 'ring-gray'
  if (o.max_severity === 'critical' || o.max_severity === 'major') return 'ring-red'
  if (o.max_severity) return 'ring-orange'
  return 'ring-green'
}

// 告警角标底色按最高 firing 等级
const badgeColor = (o) => severityColor(o.max_severity)

const TYPE_ICONS = {
  server_linux: 'Monitor',
  server_windows: 'Monitor',
  database: 'Coin',
  application: 'Link',
}
const typeIcon = (t) => TYPE_ICONS[t] || 'Monitor'

// 对象提示：名称 + 类型 + 状态 + firing 计数
function objTitle(o) {
  const firing = o.alert_total ? `，firing 告警 ${o.alert_total} 条` : '，无 firing 告警'
  return `${o.name}（${typeLabel(o.type)}）：${o.online ? '在线' : '离线/未启用'}${firing}`
}

const goDetail = (o) => router.push(`/devices/${o.id}`)

let timer = null
// WS 设备状态变化时立即刷新一次（与 30 秒轮询结合）
const offWs = onWsMessage('device_status', () => refresh())

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 30000)
})
onBeforeUnmount(() => {
  clearInterval(timer)
  offWs()
})
</script>

<template>
  <div>
    <!-- 图例 -->
    <div class="legend">
      <span class="legend-item"><i class="dot ring-green" />正常（在线且无告警）</span>
      <span class="legend-item"><i class="dot ring-orange" />有警告/信息告警</span>
      <span class="legend-item"><i class="dot ring-red" />有严重/致命告警</span>
      <span class="legend-item"><i class="dot ring-gray" />离线/未启用</span>
      <span class="legend-item"><i class="dot demo-badge">3</i>角标=firing 告警数</span>
    </div>

    <el-empty v-if="!hosts.length" description="暂无宿主机（server_linux / server_windows 设备）" />

    <div class="bus-grid">
      <div v-for="h in hosts" :key="h.id" class="bus-card">
        <!-- 宿主机基座（卡片顶部：图标 + 名称 + IP + 自身状态） -->
        <div class="host" :title="objTitle(h)" @click="goDetail(h)">
          <div class="ring host-ring" :class="ringClass(h)">
            <el-icon :size="22"><component :is="typeIcon(h.type)" /></el-icon>
            <span
              v-if="h.alert_total"
              class="badge"
              :style="{ backgroundColor: badgeColor(h) }"
            >{{ h.alert_total }}</span>
          </div>
          <div class="host-info">
            <div class="host-name">{{ h.name }}</div>
            <div class="host-ip">{{ h.ip }} · {{ typeLabel(h.type) }}</div>
          </div>
          <el-tag :type="statusTag(h.status)" size="small">{{ statusLabel(h.status) }}</el-tag>
        </div>

        <!-- 总线 -->
        <div class="bus-line" />

        <!-- 挂载对象（同 IP 的 database / application） -->
        <div v-if="h.objects.length" class="objects">
          <div
            v-for="o in h.objects"
            :key="o.id"
            class="obj"
            :title="objTitle(o)"
            @click="goDetail(o)"
          >
            <div class="ring" :class="ringClass(o)">
              <el-icon :size="18"><component :is="typeIcon(o.type)" /></el-icon>
              <span
                v-if="o.alert_total"
                class="badge"
                :style="{ backgroundColor: badgeColor(o) }"
              >{{ o.alert_total }}</span>
            </div>
            <div class="obj-name">{{ o.name }}</div>
            <div class="obj-type">{{ typeLabel(o.type) }}</div>
          </div>
        </div>
        <div v-else class="no-mount">无挂载对象</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 深色卡片风（参照大屏配色），页面底色沿用 MainLayout */
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  margin-bottom: 14px;
  color: var(--op-text-secondary);
  font-size: 13px;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  display: inline-block;
}

.demo-badge {
  width: 18px;
  height: 18px;
  background: var(--op-color-danger);
  color: #fff;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
  font-style: normal;
}

.bus-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}

.bus-card {
  width: 320px;
  padding: 16px 16px 20px;
  border-radius: 8px;
  background: linear-gradient(160deg, #0d1b3a 0%, #0a1530 100%);
  border: 1px solid rgba(0, 229, 255, 0.22);
  box-shadow: 0 4px 14px rgba(8, 18, 42, 0.35);
  color: #cfe6ff;
}

/* 状态环 */
.ring {
  position: relative;
  width: 46px;
  height: 46px;
  border-radius: 50%;
  border: 3px solid;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  background: rgba(8, 18, 42, 0.6);
}

.ring-green { border-color: #37f59a; color: #37f59a; box-shadow: 0 0 8px rgba(55, 245, 154, 0.45); }
.ring-orange { border-color: #ffb84d; color: #ffb84d; box-shadow: 0 0 8px rgba(255, 184, 77, 0.45); }
.ring-red { border-color: #ff5c7a; color: #ff5c7a; box-shadow: 0 0 8px rgba(255, 92, 122, 0.5); }
.ring-gray { border-color: #6b7a90; color: #6b7a90; }

/* 告警角标 */
.badge {
  position: absolute;
  top: -8px;
  right: -10px;
  min-width: 18px;
  height: 18px;
  padding: 0 4px;
  border-radius: 9px;
  color: #fff;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
  font-weight: 600;
}

/* 宿主机基座 */
.host {
  display: flex;
  align-items: center;
  gap: 12px;
  cursor: pointer;
}

.host:hover .host-name {
  color: #00e5ff;
}

.host-info {
  flex: 1;
  min-width: 0;
}

.host-name {
  font-size: 15px;
  font-weight: 600;
  color: #e8f4ff;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.host-ip {
  font-size: 12px;
  color: #7d9bc4;
  margin-top: 2px;
}

/* 总线 */
.bus-line {
  height: 2px;
  margin: 14px 8px 0;
  background: linear-gradient(90deg, transparent, rgba(0, 229, 255, 0.55) 15%, rgba(0, 229, 255, 0.55) 85%, transparent);
}

/* 挂载对象 */
.objects {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 10px 18px;
  padding-top: 14px;
}

.obj {
  position: relative;
  width: 76px;
  text-align: center;
  cursor: pointer;
  padding-top: 12px;
}

/* 对象到总线的竖直连接线 */
.obj::before {
  content: '';
  position: absolute;
  top: 0;
  left: 50%;
  width: 2px;
  height: 12px;
  transform: translateX(-50%);
  background: rgba(0, 229, 255, 0.4);
}

.obj .ring {
  margin: 0 auto;
  transition: transform 0.15s;
}

.obj:hover .ring {
  transform: scale(1.08);
}

.obj-name {
  margin-top: 6px;
  font-size: 12px;
  color: #cfe6ff;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.obj-type {
  font-size: 11px;
  color: #7d9bc4;
}

.no-mount {
  padding: 18px 0 4px;
  text-align: center;
  font-size: 12px;
  color: #5b7299;
}
</style>
