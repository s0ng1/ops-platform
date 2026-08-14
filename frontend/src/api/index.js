import request from './request'

// 认证
export const login = (data) => request.post('/auth/login', data)
export const getMe = () => request.get('/auth/me')
export const changePassword = (data) => request.post('/auth/change-password', data)

// 用户管理（仅 admin）
export const listUsers = () => request.get('/users')
export const createUser = (data) => request.post('/users', data)
export const updateUser = (id, data) => request.put(`/users/${id}`, data)
export const deleteUser = (id) => request.delete(`/users/${id}`)

// 凭据管理
export const listCredentials = () => request.get('/credentials')
export const createCredential = (data) => request.post('/credentials', data)
export const updateCredential = (id, data) => request.put(`/credentials/${id}`, data)
export const deleteCredential = (id) => request.delete(`/credentials/${id}`)

// 设备管理
export const listDevices = (params) => request.get('/devices', { params })
export const createDevice = (data) => request.post('/devices', data)
export const updateDevice = (id, data) => request.put(`/devices/${id}`, data)
export const deleteDevice = (id) => request.delete(`/devices/${id}`)
export const probeDevice = (id) => request.post(`/devices/${id}/probe`)

// 监控总览
export const getOverview = () => request.get('/monitor/overview')

// 设备指标
export const getDeviceMetrics = (id, params) => request.get(`/devices/${id}/metrics`, { params })
export const getDeviceMetricsLatest = (id) => request.get(`/devices/${id}/metrics/latest`)
export const getDeviceMetricsCatalog = (id) => request.get(`/devices/${id}/metrics/catalog`)

// 配置备份（diff 返回纯文本）
export const listConfigBackups = (id, params) => request.get(`/devices/${id}/config-backups`, { params })
export const getConfigBackup = (id, backupId) => request.get(`/devices/${id}/config-backups/${backupId}`)
export const getConfigBackupDiff = (id, from, to) =>
  request.get(`/devices/${id}/config-backups/diff`, { params: { from, to } })
export const fetchConfigBackup = (id) => request.post(`/devices/${id}/config-backups/fetch`)

// 告警规则
export const listAlertRules = () => request.get('/alert/rules')
export const createAlertRule = (data) => request.post('/alert/rules', data)
export const updateAlertRule = (id, data) => request.put(`/alert/rules/${id}`, data)
export const deleteAlertRule = (id) => request.delete(`/alert/rules/${id}`)

// 告警规则模板（批量套用生成规则）
export const listAlertTemplates = () => request.get('/alert/templates')
export const createAlertTemplate = (data) => request.post('/alert/templates', data)
export const updateAlertTemplate = (id, data) => request.put(`/alert/templates/${id}`, data)
export const deleteAlertTemplate = (id) => request.delete(`/alert/templates/${id}`)
export const instantiateAlertTemplates = (data) => request.post('/alert/templates/instantiate', data)

// 告警事件
export const listAlertEvents = (params) => request.get('/alert/events', { params })
export const getAlertEvent = (id) => request.get(`/alert/events/${id}`)
export const ackAlertEvent = (id) => request.post(`/alert/events/${id}/ack`)
export const resolveAlertEvent = (id) => request.post(`/alert/events/${id}/resolve`)
export const getAlertSummary = () => request.get('/alert/summary')

// 静默窗口
export const listAlertSilences = () => request.get('/alert/silences')
export const createAlertSilence = (data) => request.post('/alert/silences', data)
export const updateAlertSilence = (id, data) => request.put(`/alert/silences/${id}`, data)
export const deleteAlertSilence = (id) => request.delete(`/alert/silences/${id}`)

// 审计日志（仅 admin）
export const listAudits = (params) => request.get('/audits', { params })

// 日志事件（Syslog / SNMP Trap）
export const listLogEvents = (params) => request.get('/logs/events', { params })

// 日志规则
export const listLogRules = () => request.get('/logs/rules')
export const createLogRule = (data) => request.post('/logs/rules', data)
export const updateLogRule = (id, data) => request.put(`/logs/rules/${id}`, data)
export const deleteLogRule = (id) => request.delete(`/logs/rules/${id}`)

// 通知渠道
export const listNotifyConfigs = () => request.get('/notify/configs')
export const createNotifyConfig = (data) => request.post('/notify/configs', data)
export const updateNotifyConfig = (id, data) => request.put(`/notify/configs/${id}`, data)
export const deleteNotifyConfig = (id) => request.delete(`/notify/configs/${id}`)

// 宿主机-应用总线视图
export const getBus = () => request.get('/bus')

// 网络拓扑
export const getTopologyGroups = () => request.get('/topology/groups')
export const getTopologyGraph = (group) =>
  request.get('/topology/graph', { params: group ? { group } : {} })
export const getTopologyTraffic = (group) =>
  request.get('/topology/traffic', { params: group ? { group } : {} })
export const createTopologyLink = (data) => request.post('/topology/links', data)
export const deleteTopologyLink = (id) => request.delete(`/topology/links/${id}`)
export const saveTopologyLayout = (positions, group) =>
  request.put('/topology/layout', { positions, group: group || '' })
// 自动发现耗时较长，单独放宽超时
export const discoverTopology = () => request.post('/topology/discover', null, { timeout: 120000 })

// 报表（format=xlsx 时为文件下载，返回完整响应）
export const getReportAvailability = (params) => request.get('/reports/availability', { params })
export const getReportTraffic = (params) => request.get('/reports/traffic', { params })
export const exportReportAvailability = (params) =>
  request.get('/reports/availability', { params: { ...params, format: 'xlsx' }, responseType: 'blob' })
export const exportReportTraffic = (params) =>
  request.get('/reports/traffic', { params: { ...params, format: 'xlsx' }, responseType: 'blob' })

// 自动发现
export const startScan = (data) => request.post('/discovery/scan', data)
export const listScanJobs = () => request.get('/discovery/jobs')
export const getScanJob = (id) => request.get(`/discovery/jobs/${id}`)
export const importDevices = (data) => request.post('/discovery/import', data)

// IPAM（轻量 IP 地址台账）
export const listIpamInventory = (params) => request.get('/ipam/inventory', { params })
export const updateIpamInventory = (id, data) => request.put(`/ipam/inventory/${id}`, data)
export const getIpamSubnets = (params) => request.get('/ipam/subnets', { params })
