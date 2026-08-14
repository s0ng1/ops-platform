// 设备类型枚举
export const DEVICE_TYPES = [
  { value: 'network', label: '网络设备' },
  { value: 'security', label: '安全设备' },
  { value: 'server_windows', label: 'Windows主机' },
  { value: 'server_linux', label: 'Linux主机' },
  { value: 'database', label: '数据库' },
  { value: 'application', label: '应用' },
  { value: 'other', label: '其他' },
]

export const typeLabel = (v) => DEVICE_TYPES.find((t) => t.value === v)?.label || v || '-'

// 设备在线状态
export const DEVICE_STATUS = {
  online: { label: '在线', tag: 'success' },
  offline: { label: '离线', tag: 'danger' },
  unknown: { label: '未知', tag: 'info' },
}

export const statusLabel = (v) => DEVICE_STATUS[v]?.label || v || '-'
export const statusTag = (v) => DEVICE_STATUS[v]?.tag || 'info'

// 凭据类型
export const CREDENTIAL_KINDS = [
  { value: 'snmp_v2c', label: 'SNMP v2c' },
  { value: 'snmp_v3', label: 'SNMP v3' },
  { value: 'ssh', label: 'SSH' },
  { value: 'database', label: '数据库' },
]

export const kindLabel = (v) => CREDENTIAL_KINDS.find((k) => k.value === v)?.label || v || '-'

// 用户角色
export const ROLES = [
  { value: 'admin', label: '管理员' },
  { value: 'operator', label: '操作员' },
  { value: 'viewer', label: '只读用户' },
]

export const roleLabel = (v) => ROLES.find((r) => r.value === v)?.label || v || '-'

// ISO 时间格式化为本地可读字符串
export function fmtTime(s) {
  if (!s) return '-'
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

// IP 格式校验
export const IP_RE = /^(\d{1,3}\.){3}\d{1,3}$/
export const isValidIp = (ip) =>
  IP_RE.test(ip) && ip.split('.').every((n) => Number(n) >= 0 && Number(n) <= 255)

// ===== 第 2 期：告警 / 指标 =====

// 告警等级（顶栏 pill 与事件 tag 共用颜色，与 styles/tokens.css 的 --op-severity-* 对齐）
export const SEVERITIES = {
  critical: { label: '致命', color: '#dc2626' },
  major: { label: '严重', color: '#ea580c' },
  warning: { label: '警告', color: '#d97706' },
  info: { label: '信息', color: '#64748b' },
}
export const SEVERITY_ORDER = ['critical', 'major', 'warning', 'info']
export const severityLabel = (v) => SEVERITIES[v]?.label || v || '-'
export const severityColor = (v) => SEVERITIES[v]?.color || '#64748b'

// 告警事件状态
export const EVENT_STATUS = {
  firing: { label: '告警中', tag: 'danger' },
  resolved: { label: '已恢复', tag: 'success' },
}

// 比较操作符
export const ALERT_OPS = ['>', '>=', '<', '<=', '==', '!=']

// 指标中文名（未知名称原样展示）
export const METRIC_NAMES = {
  ping_latency_ms: 'Ping 时延(ms)',
  device_online: '在线状态',
  if_in_bps: '接口入速率(bps)',
  if_out_bps: '接口出速率(bps)',
  if_in_util: '接口入利用率(%)',
  if_out_util: '接口出利用率(%)',
  if_status: '接口状态',
  cpu_usage: 'CPU 使用率(%)',
  mem_usage: '内存使用率(%)',
  disk_usage: '磁盘使用率(%)',
  process_count: '进程数',
  load1: '负载 1 分钟',
  load5: '负载 5 分钟',
  load15: '负载 15 分钟',
  mysql_threads_connected: 'MySQL 已连接线程',
  mysql_threads_running: 'MySQL 运行中线程',
  mysql_max_connections: 'MySQL 最大连接数',
  mysql_qps: 'MySQL QPS',
  mysql_tps: 'MySQL TPS',
  mysql_slow_qps: 'MySQL 慢查询 QPS',
  mysql_replication_delay: 'MySQL 主从延迟(s)',
  db_available: '数据库可用性',
  db_latency: '数据库查询耗时(ms)',
  oracle_sessions: 'Oracle 会话数',
  oracle_sessions_usage_pct: 'Oracle 会话使用率(%)',
  oracle_active_sessions: 'Oracle 活动会话数',
  oracle_tablespace_usage_pct: 'Oracle 表空间使用率(%)',
  mssql_connections: 'SQLServer 连接数',
  mssql_buffer_cache_hit_pct: 'SQLServer 缓存命中率(%)',
  mssql_batch_per_sec: 'SQLServer 批请求/秒',
  config_changed: '配置变更',
  log_event: '日志事件',
  new_terminal: '新终端接入',
  app_available: '应用可用性',
  app_latency: '应用响应时间(ms)',
  app_status_code: 'HTTP 状态码',
  nginx_active: 'Nginx 活跃连接',
  nginx_reading: 'Nginx 读连接',
  nginx_writing: 'Nginx 写连接',
  nginx_waiting: 'Nginx 等待连接',
  nginx_accepts_per_sec: 'Nginx 接受连接/秒',
  nginx_handled_per_sec: 'Nginx 处理连接/秒',
  nginx_requests_per_sec: 'Nginx 请求/秒',
  redis_connected_clients: 'Redis 连接客户端数',
  redis_used_memory: 'Redis 已用内存(B)',
  redis_used_memory_rss: 'Redis RSS 内存(B)',
  redis_mem_usage_pct: 'Redis 内存使用率(%)',
  redis_hit_rate: 'Redis 命中率(%)',
  redis_ops_per_sec: 'Redis 每秒命令数',
}
export const metricLabel = (m) => METRIC_NAMES[m] || m || '-'

// IPAM 终端来源
export const IPAM_SOURCES = [
  { value: 'ping', label: '扫描发现' },
  { value: 'arp', label: 'ARP 表' },
  { value: 'mac_table', label: 'MAC 表' },
]
export const ipamSourceLabel = (v) => IPAM_SOURCES.find((s) => s.value === v)?.label || v || '-'

// Syslog 等级（RFC3164，数值越小越严重）
export const SYSLOG_SEVERITIES = [
  { value: 0, label: '0 紧急' },
  { value: 1, label: '1 警报' },
  { value: 2, label: '2 严重' },
  { value: 3, label: '3 错误' },
  { value: 4, label: '4 警告' },
  { value: 5, label: '5 通知' },
  { value: 6, label: '6 信息' },
  { value: 7, label: '7 调试' },
]
export const syslogSeverityLabel = (v) =>
  v === null || v === undefined ? '-' : SYSLOG_SEVERITIES[v]?.label || String(v)

// 告警规则 metric 下拉的常见预填项（允许自定义输入）
export const COMMON_METRICS = Object.keys(METRIC_NAMES)

// labels 对象转可读字符串，如 {if:"GE0/0/1"} → if=GE0/0/1
export function fmtLabels(labels) {
  if (!labels || typeof labels !== 'object') return '-'
  const parts = Object.entries(labels).map(([k, v]) => `${k}=${v}`)
  return parts.length ? parts.join(', ') : '-'
}
