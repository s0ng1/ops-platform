import { reactive } from 'vue'

// 轻量全局认证状态（token/用户信息持久化在 localStorage）
const state = reactive({
  token: localStorage.getItem('token') || '',
  username: localStorage.getItem('username') || '',
  role: localStorage.getItem('role') || '',
})

export function useAuth() {
  // 登录成功后写入状态
  const setLogin = ({ token, username, role }) => {
    state.token = token
    state.username = username
    state.role = role
    localStorage.setItem('token', token)
    localStorage.setItem('username', username)
    localStorage.setItem('role', role)
  }

  // 退出登录，清理全部本地状态
  const logout = () => {
    state.token = ''
    state.username = ''
    state.role = ''
    localStorage.removeItem('token')
    localStorage.removeItem('username')
    localStorage.removeItem('role')
  }

  return { state, setLogin, logout }
}
