<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox, ElNotification } from 'element-plus'
import { useAuth } from '../stores/auth'
import { startWs, stopWs, onWsMessage, useWsState } from '../stores/ws'
import { getAlertSummary } from '../api'
import {
  roleLabel, SEVERITY_ORDER, severityLabel, metricLabel,
} from '../utils/dicts'

const route = useRoute()
const router = useRouter()
const { state, logout } = useAuth()

const isAdmin = computed(() => state.role === 'admin')
const activeMenu = computed(() => route.path)
const wsState = useWsState()

// ===== 顶栏告警计数 =====
const summary = ref({ critical: 0, major: 0, warning: 0, info: 0 })

async function fetchSummary() {
  try {
    summary.value = await getAlertSummary()
  } catch {
    // 静默失败
  }
}

// 点击等级 pill 跳告警中心并按等级筛选
function goAlerts(severity) {
  router.push({ path: '/alerts', query: severity ? { severity } : {} })
}

// 退出登录
async function handleLogout() {
  try {
    await ElMessageBox.confirm('确定要退出登录吗？', '提示', { type: 'warning' })
  } catch {
    return
  }
  stopWs()
  logout()
  router.push('/login')
}

// 用户下拉菜单
function handleCommand(cmd) {
  if (cmd === 'change-password') {
    router.push('/change-password')
  } else if (cmd === 'logout') {
    handleLogout()
  }
}

// WS：alert → 刷新计数 + 弹通知；device_status → 各页面自行订阅
const NOTIFY_TYPE = { critical: 'error', major: 'warning', warning: 'warning', info: 'info' }
const offAlert = onWsMessage('alert', (msg) => {
  fetchSummary()
  ElNotification({
    title: `新告警【${severityLabel(msg.severity)}】`,
    message: `${msg.device_name || '设备'}：${metricLabel(msg.metric)} = ${msg.value}（${msg.rule_name}）`,
    type: NOTIFY_TYPE[msg.severity] || 'info',
    duration: 6000,
  })
})

onMounted(() => {
  startWs()
  fetchSummary()
})
onBeforeUnmount(() => {
  offAlert()
  stopWs()
})
</script>

<template>
  <el-container class="layout">
    <!-- 左侧浅色导航 -->
    <el-aside width="224px" class="aside">
      <div class="logo">
        <div class="logo-mark">
          <el-icon :size="17"><Monitor /></el-icon>
        </div>
        <span class="logo-text">内网运维管理平台</span>
      </div>
      <el-menu :default-active="activeMenu" router class="menu">
        <el-menu-item index="/dashboard">
          <el-icon><Odometer /></el-icon>
          <span>仪表盘</span>
        </el-menu-item>
        <el-menu-item index="/topology">
          <el-icon><Share /></el-icon>
          <span>网络拓扑</span>
        </el-menu-item>
        <el-menu-item index="/bus">
          <el-icon><SetUp /></el-icon>
          <span>总线视图</span>
        </el-menu-item>
        <el-menu-item index="/devices/network">
          <el-icon><Connection /></el-icon>
          <span>网络设备</span>
        </el-menu-item>
        <el-menu-item index="/devices/servers">
          <el-icon><Monitor /></el-icon>
          <span>服务器设备</span>
        </el-menu-item>
        <el-menu-item index="/alerts">
          <el-icon><Bell /></el-icon>
          <span>告警中心</span>
        </el-menu-item>
        <el-menu-item index="/reports">
          <el-icon><TrendCharts /></el-icon>
          <span>报表</span>
        </el-menu-item>
        <el-menu-item index="/screen">
          <el-icon><DataBoard /></el-icon>
          <span>大屏</span>
        </el-menu-item>
        <el-menu-item index="/discovery">
          <el-icon><Search /></el-icon>
          <span>自动发现</span>
        </el-menu-item>
        <el-menu-item index="/ipam">
          <el-icon><Grid /></el-icon>
          <span>IP 地址管理</span>
        </el-menu-item>
        <el-menu-item index="/credentials">
          <el-icon><Key /></el-icon>
          <span>凭据管理</span>
        </el-menu-item>
        <el-menu-item v-if="isAdmin" index="/users">
          <el-icon><User /></el-icon>
          <span>用户管理</span>
        </el-menu-item>
        <el-menu-item v-if="isAdmin" index="/audits">
          <el-icon><Document /></el-icon>
          <span>审计日志</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <!-- 顶部状态栏 -->
      <el-header class="header" height="56px">
        <div class="header-left">
          <div class="header-title">{{ route.meta.title || '' }}</div>
          <!-- 告警等级计数（柔和 pill，点击查看） -->
          <div class="alarm-pills">
            <div
              v-for="s in SEVERITY_ORDER"
              :key="s"
              class="alarm-pill"
              :class="s"
              :title="`${severityLabel(s)}告警，点击查看`"
              @click="goAlerts(s)"
            >
              <span class="alarm-dot" />
              <span>{{ severityLabel(s) }}</span>
              <span class="alarm-count">{{ summary[s] ?? 0 }}</span>
            </div>
          </div>
        </div>
        <div class="header-right">
          <span
            class="ws-dot"
            :class="{ on: wsState.connected }"
            :title="wsState.connected ? '实时推送已连接' : '实时推送未连接'"
          />
          <el-dropdown trigger="click" @command="handleCommand">
            <div class="user-entry">
              <div class="avatar">{{ (state.username || '?').slice(0, 1).toUpperCase() }}</div>
              <span class="username">{{ state.username }}</span>
              <el-tag size="small" effect="plain" class="role-tag">{{ roleLabel(state.role) }}</el-tag>
              <el-icon class="caret"><ArrowDown /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="change-password">
                  <el-icon><Lock /></el-icon>修改密码
                </el-dropdown-item>
                <el-dropdown-item command="logout" divided>
                  <el-icon><SwitchButton /></el-icon>退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100%;
}

/* ---- 侧栏 ---- */
.aside {
  background: var(--op-bg-card);
  border-right: 1px solid var(--op-border-light);
  display: flex;
  flex-direction: column;
}

.logo {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 56px;
  padding: 0 16px;
  border-bottom: 1px solid var(--op-border-light);
  flex-shrink: 0;
}

.logo-mark {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: var(--op-color-primary);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.logo-text {
  font-size: 15px;
  font-weight: 600;
  color: var(--op-text-primary);
  white-space: nowrap;
  letter-spacing: 0.5px;
}

.menu {
  border-right: none;
  padding: 8px;
  flex: 1;
  overflow-y: auto;
}

.menu :deep(.el-menu-item) {
  height: 40px;
  line-height: 40px;
  margin: 2px 0;
  border-radius: var(--op-radius-md);
  position: relative;
  font-size: 13.5px;
}

.menu :deep(.el-menu-item:hover) {
  background: var(--op-bg-subtle);
  color: var(--op-text-primary);
}

.menu :deep(.el-menu-item.is-active) {
  background: var(--op-color-primary-light-9);
  color: var(--op-color-primary);
  font-weight: 500;
}

.menu :deep(.el-menu-item.is-active)::before {
  content: '';
  position: absolute;
  left: -8px;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 2px;
  background: var(--op-color-primary);
}

/* ---- 顶栏 ---- */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(255, 255, 255, 0.85);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--op-border-light);
  padding: 0 20px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 24px;
  min-width: 0;
}

.header-title {
  font-size: 17px;
  font-weight: 600;
  white-space: nowrap;
}

/* 告警等级 pill */
.alarm-pills {
  display: flex;
  gap: 8px;
}

.alarm-pill {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 12.5px;
  cursor: pointer;
  user-select: none;
  transition: filter 0.15s ease;
}

.alarm-pill:hover {
  filter: brightness(0.96);
}

.alarm-pill .alarm-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.alarm-pill .alarm-count {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.alarm-pill.critical {
  color: var(--op-severity-critical);
  background: var(--op-severity-critical-bg);
}
.alarm-pill.major {
  color: var(--op-severity-major);
  background: var(--op-severity-major-bg);
}
.alarm-pill.warning {
  color: var(--op-severity-warning);
  background: var(--op-severity-warning-bg);
}
.alarm-pill.info {
  color: var(--op-severity-info);
  background: var(--op-severity-info-bg);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 14px;
}

/* WS 连接状态点（连接态呼吸动画） */
.ws-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #c3c9d4;
}

.ws-dot.on {
  background: var(--op-color-success);
  animation: ws-pulse 2s ease-in-out infinite;
}

@keyframes ws-pulse {
  0%, 100% {
    box-shadow: 0 0 0 0 rgba(22, 163, 74, 0.35);
  }
  50% {
    box-shadow: 0 0 0 5px rgba(22, 163, 74, 0);
  }
}

/* 用户入口 */
.user-entry {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: var(--op-radius-md);
  outline: none;
}

.user-entry:hover {
  background: var(--op-bg-subtle);
}

.avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--op-color-primary-light-9);
  color: var(--op-color-primary);
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}

.username {
  color: var(--op-text-primary);
  font-size: 13.5px;
}

.role-tag {
  border-radius: 4px;
}

.caret {
  color: var(--op-text-tertiary);
  font-size: 12px;
}

.main {
  background: var(--op-bg-page);
  padding: 20px;
}
</style>
