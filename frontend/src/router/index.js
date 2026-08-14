import { createRouter, createWebHistory } from 'vue-router'
import { ElMessage } from 'element-plus'

const routes = [
  { path: '/login', name: 'login', component: () => import('../views/Login.vue') },
  // 实时大屏：独立全屏页，不带侧边栏，需登录
  { path: '/screen', name: 'screen', component: () => import('../views/Screen.vue'), meta: { title: '实时大屏' } },
  {
    path: '/',
    component: () => import('../layout/MainLayout.vue'),
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', name: 'dashboard', component: () => import('../views/Dashboard.vue'), meta: { title: '仪表盘' } },
      { path: 'topology', name: 'topology', component: () => import('../views/Topology.vue'), meta: { title: '网络拓扑' } },
      { path: 'bus', name: 'bus', component: () => import('../views/Bus.vue'), meta: { title: '总线视图' } },
      { path: 'devices', redirect: '/devices/network' },
      // 设备管理拆为两个页面，复用同一组件，通过 meta.types 限定范围
      { path: 'devices/network', name: 'devices-network', component: () => import('../views/Devices.vue'), meta: { title: '网络设备', types: ['network', 'security'] } },
      { path: 'devices/servers', name: 'devices-servers', component: () => import('../views/Devices.vue'), meta: { title: '服务器设备', types: ['server_windows', 'server_linux', 'database', 'application', 'other'] } },
      { path: 'devices/:id(\\d+)', name: 'device-detail', component: () => import('../views/DeviceDetail.vue'), meta: { title: '设备详情' } },
      { path: 'alerts', name: 'alerts', component: () => import('../views/Alerts.vue'), meta: { title: '告警中心' } },
      { path: 'reports', name: 'reports', component: () => import('../views/Reports.vue'), meta: { title: '报表' } },
      { path: 'discovery', name: 'discovery', component: () => import('../views/Discovery.vue'), meta: { title: '自动发现' } },
      { path: 'ipam', name: 'ipam', component: () => import('../views/Ipam.vue'), meta: { title: 'IP 地址管理' } },
      { path: 'credentials', name: 'credentials', component: () => import('../views/Credentials.vue'), meta: { title: '凭据管理' } },
      { path: 'users', name: 'users', component: () => import('../views/Users.vue'), meta: { title: '用户管理', admin: true } },
      { path: 'audits', name: 'audits', component: () => import('../views/Audits.vue'), meta: { title: '审计日志', admin: true } },
      { path: 'change-password', name: 'change-password', component: () => import('../views/ChangePassword.vue'), meta: { title: '修改密码' } },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 全局守卫：未登录跳登录页；meta.admin 页面仅 admin 可见
router.beforeEach((to) => {
  const token = localStorage.getItem('token')
  if (to.path !== '/login' && !token) {
    return '/login'
  }
  if (to.path === '/login' && token) {
    return '/'
  }
  if (to.meta.admin && localStorage.getItem('role') !== 'admin') {
    ElMessage.warning('仅管理员可访问该页面')
    return '/dashboard'
  }
  document.title = to.meta.title ? `${to.meta.title} - 内网运维管理平台` : '内网运维管理平台'
  return true
})

export default router
