import axios from 'axios'
import { ElMessage } from 'element-plus'

// axios 实例：统一 baseURL、鉴权头与错误提示
const request = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// 请求拦截器：自动带上 token
request.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截器：401 跳登录页，其余错误统一弹出后端 detail
// blob（文件下载）请求保留完整响应，便于读取 Content-Disposition 文件名
request.interceptors.response.use(
  (res) => (res.config.responseType === 'blob' ? res : res.data),
  async (err) => {
    const resp = err.response
    if (resp?.status === 401) {
      // 登录失效：清理本地状态并回到登录页
      localStorage.removeItem('token')
      localStorage.removeItem('username')
      localStorage.removeItem('role')
      // 动态引入避免与 router 形成循环依赖
      const { default: router } = await import('../router')
      if (router.currentRoute.value.path !== '/login') {
        ElMessage.error(resp.data?.detail || '登录已失效，请重新登录')
        router.push('/login')
      } else {
        ElMessage.error(resp.data?.detail || '用户名或密码错误')
      }
    } else {
      const detail = resp?.data?.detail
      ElMessage.error(typeof detail === 'string' ? detail : '请求失败，请稍后重试')
    }
    return Promise.reject(err)
  }
)

export default request
