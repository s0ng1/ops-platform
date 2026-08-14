import { reactive } from 'vue'
import request from '../api/request'

// WebSocket 实时推送（/api/ws）：连接管理 + 指数退避重连 + 按 type 分发
// 消息类型：alert（新告警）/ device_status（设备状态变化）
// 鉴权用一次性短时票据（先经 /api/ws-ticket 换取），避免 JWT 进 URL 查询串
const state = reactive({ connected: false })

const handlers = { alert: new Set(), device_status: new Set() }

let ws = null
let started = false
let retries = 0
let reconnectTimer = null

async function fetchTicket() {
  const data = await request.get('/ws-ticket')
  return data.ticket
}

async function connect() {
  const token = localStorage.getItem('token')
  if (!token || !started) return
  let ticket
  try {
    ticket = await fetchTicket()
  } catch {
    scheduleReconnect()
    return
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/api/ws?ticket=${encodeURIComponent(ticket)}`)

  ws.onopen = () => {
    state.connected = true
    retries = 0
  }

  ws.onmessage = (e) => {
    let msg
    try {
      msg = JSON.parse(e.data)
    } catch {
      return
    }
    const set = handlers[msg?.type]
    if (set) set.forEach((fn) => fn(msg))
  }

  ws.onclose = () => {
    state.connected = false
    scheduleReconnect()
  }

  ws.onerror = () => {
    ws?.close()
  }
}

// 指数退避重连：1s、2s、4s …… 封顶 30s
function scheduleReconnect() {
  if (!started) return
  clearTimeout(reconnectTimer)
  const delay = Math.min(30000, 1000 * 2 ** retries)
  retries += 1
  reconnectTimer = setTimeout(connect, delay)
}

// 启动连接（幂等），登录后由主布局调用
export function startWs() {
  if (started) return
  started = true
  retries = 0
  connect()
}

// 停止连接（退出登录时调用），停止后不再重连
export function stopWs() {
  started = false
  clearTimeout(reconnectTimer)
  if (ws) {
    ws.onclose = null // 阻止触发重连
    ws.close()
    ws = null
  }
  state.connected = false
}

// 订阅消息，返回取消订阅函数（组件卸载时调用）
export function onWsMessage(type, fn) {
  handlers[type]?.add(fn)
  return () => handlers[type]?.delete(fn)
}

export function useWsState() {
  return state
}
