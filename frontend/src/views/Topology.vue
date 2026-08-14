<script setup>
// 网络拓扑：G6 v5 深色画布，浏览/编辑/连线三种交互，30 秒周期刷新 + WS 状态联动
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Graph } from '@antv/g6'
import {
  getTopologyGraph, getTopologyTraffic, getTopologyGroups, createTopologyLink, deleteTopologyLink,
  saveTopologyLayout, discoverTopology, getDeviceMetricsCatalog, getDeviceMetrics,
} from '../api'
import { typeLabel, statusLabel } from '../utils/dicts'
import { fmtBps as fmtBpsVal, fmtBpsShort } from '../utils/format'
import { onWsMessage } from '../stores/ws'
import Chart from '../components/Chart.vue'

const router = useRouter()
const containerRef = ref()

let graph = null // G6 实例
const rawGraph = ref(null) // 最近一次后端返回的图数据（ref 响应式，v-show/按钮依赖它）
const nodeMap = new Map() // id -> 已应用的节点业务数据
const edgeMap = new Map() // id -> 已应用的链路业务数据

const loading = ref(false)
const discovering = ref(false)
const layoutLoading = ref(false)
const editMode = ref(false)
const linkMode = ref(false)
const firstPick = ref(null) // 连线模式下第一个选中的节点 id

// ===== 分组子拓扑（空串=全部）=====
const groups = ref([]) // [{name, count}]
const activeGroup = ref('')
const nodeCount = ref(0) // 画布节点数（工具栏按钮可用性用；rawGraph 是普通对象不参与响应式）
const showEdgeLabels = ref(true) // 链路标签显隐开关

// 无坐标节点的确定性布局：以已有坐标节点质心（无则画布中心）为圆心，同心圆环均匀铺开。
// 不用 G6 力导布局——G6 5.1 配了 layout 后会接管全部节点定位，力导对 html 节点失效时
// 整图（含已有坐标的节点）塌缩到 (0,0)，真机 23 节点实测复现；确定性布局秒出且可复现。
function assignFallbackPositions(nodes) {
  const missing = nodes.filter((n) => n.x == null || n.y == null)
  if (!missing.length) return
  const positioned = nodes.filter((n) => n.x != null && n.y != null)
  const cx = positioned.length ? positioned.reduce((s, n) => s + n.x, 0) / positioned.length : 400
  const cy = positioned.length ? positioned.reduce((s, n) => s + n.y, 0) / positioned.length : 300
  const PER_RING = 10
  missing.forEach((n, i) => {
    const ring = Math.floor(i / PER_RING)
    const idx = i % PER_RING
    const count = Math.min(PER_RING, missing.length - ring * PER_RING)
    const angle = (2 * Math.PI * idx) / count + ring * 0.35
    const r = 220 + ring * 160
    n.x = Math.round(cx + r * Math.cos(angle))
    n.y = Math.round(cy + r * Math.sin(angle))
  })
}

async function fetchGroups() {
  try {
    groups.value = await getTopologyGroups()
    // 当前分组被清空（设备全部移出/删除）时退回「全部」
    if (activeGroup.value && !groups.value.some((g) => g.name === activeGroup.value)) {
      activeGroup.value = ''
      switchGroup()
    }
  } catch {
    // 静默失败，分组下拉为空
  }
}

// 切换分组：节点集合与布局都是另一套，整体销毁重建画布
async function switchGroup() {
  if (linkMode.value) toggleLinkMode()
  firstPick.value = null
  clearInterval(trafficTimer) // 流量轮询定时器随画布重建，避免叠加
  trafficTimer = null
  closeTrafficCard()
  resizeObserver?.disconnect()
  resizeObserver = null
  graph?.destroy()
  graph = null
  nodeMap.clear()
  edgeMap.clear()
  rawGraph.value = null
  viewportTouched = false
  await fetchAndApply(true)
  startTrafficTimer()
}

// ===== 样式映射 =====
const STATUS_COLORS = { online: '#52c41a', offline: '#f5222d', unknown: '#8c8c8c' }
// 网络设备细分类型 SVG 图标（交换机/路由器/防火墙），其余类型沿用 emoji
const _SVG_ATTR = 'width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#69c0ff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"'
const SUBTYPE_SVGS = {
  // 交换机：矩形机身 + 上下双向箭头
  switch: `<svg ${_SVG_ATTR}><rect x="2" y="8" width="20" height="8" rx="1.5"/><path d="M6 10.8h9m0 0-2.2-2.2m2.2 2.2-2.2 2.2"/><path d="M18 13.2H9m0 0 2.2-2.2M9 13.2l2.2 2.2"/></svg>`,
  // 路由器：圆形机身 + 内部双向转发箭头
  router: `<svg ${_SVG_ATTR}><circle cx="12" cy="12" r="8.5"/><path d="M7.5 9.8h7m0 0-2-2m2 2-2 2"/><path d="M16.5 14.2h-7m0 0 2-2m-2 2 2 2"/></svg>`,
  // 防火墙：砖墙
  firewall: `<svg ${_SVG_ATTR}><rect x="3" y="5.5" width="18" height="13" rx="1"/><path d="M3 10h18M3 14.5h18M9.5 5.5V10M15 10v4.5M9.5 14.5v4"/></svg>`,
}
const TYPE_ICONS = {
  network: '🌐', security: '🛡️', server_windows: '🖥️',
  server_linux: '🐧', database: '🗄️', other: '📦',
}

function typeIcon(d) {
  // subtype 为空时按 type 取默认：network→交换机，security→防火墙
  const st = d.subtype || (d.type === 'network' ? 'switch' : d.type === 'security' ? 'firewall' : '')
  return SUBTYPE_SVGS[st] || TYPE_ICONS[d.type] || TYPE_ICONS.other
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]))
}

// 节点 HTML：类型图标 + 名称 + IP，边框按状态着色，连线选中时加高亮
function nodeHtml(d) {
  const color = STATUS_COLORS[d.status] || STATUS_COLORS.unknown
  return `<div class="topo-node${d.picked ? ' topo-picked' : ''}" style="border-color:${color}">`
    + `<div class="topo-icon">${typeIcon(d)}</div>`
    + `<div class="topo-text">`
    + `<div class="topo-name" title="${escapeHtml(d.name || '')}">${escapeHtml(d.name || d.ip)}</div>`
    + `<div class="topo-ip">${escapeHtml(d.ip || '')}</div>`
    + `</div></div>`
}

// 取链路两侧 in/out 中最大的速率
function maxTraffic(l) {
  const vals = []
  for (const t of [l.src_traffic, l.dst_traffic]) {
    if (!t) continue
    if (t.in_bps != null) vals.push(t.in_bps)
    if (t.out_bps != null) vals.push(t.out_bps)
  }
  return vals.length ? Math.max(...vals) : null
}

// 取链路最大利用率，用于着色
function maxUtil(l) {
  const vals = []
  for (const t of [l.src_traffic, l.dst_traffic]) {
    if (!t) continue
    if (t.in_util != null) vals.push(t.in_util)
    if (t.out_util != null) vals.push(t.out_util)
  }
  return vals.length ? Math.max(...vals) : null
}

function edgeStroke(l) {
  const u = maxUtil(l)
  if (u == null) return '#3d5a80'
  if (u > 80) return '#f5222d'
  if (u > 50) return '#faad14'
  return '#52c41a'
}

// 链路两侧 in/out 各自取最大，用于链路标签的 ↓/↑ 短格式
function maxInOut(l) {
  let inMax = null
  let outMax = null
  for (const t of [l.src_traffic, l.dst_traffic]) {
    if (!t) continue
    if (t.in_bps != null) inMax = inMax == null ? t.in_bps : Math.max(inMax, t.in_bps)
    if (t.out_bps != null) outMax = outMax == null ? t.out_bps : Math.max(outMax, t.out_bps)
  }
  return { inMax, outMax }
}

// 边标签：端口对 + ↓入向/↑出向短速率；都为空则不显示
function edgeLabelText(l) {
  const lines = []
  const ports = [l.src_port, l.dst_port].filter(Boolean).join(' ↔ ')
  if (ports) lines.push(ports)
  const { inMax, outMax } = maxInOut(l)
  const traffic = [
    inMax != null ? `↓ ${fmtBpsShort(inMax)}` : '',
    outMax != null ? `↑ ${fmtBpsShort(outMax)}` : '',
  ].filter(Boolean).join(' ')
  if (traffic) lines.push(traffic)
  return lines.join('\n')
}

function edgeStyle(l) {
  const label = edgeLabelText(l)
  return {
    stroke: edgeStroke(l),
    lineWidth: 2,
    // 手工链路实线，LLDP/CDP 发现链路虚线
    lineDash: l.source === 'manual' ? undefined : [6, 4],
    label: showEdgeLabels.value && !!label,
    labelText: label,
    labelFill: '#c9d6e8',
    labelFontSize: 10,
    labelBackground: true,
    labelBackgroundFill: '#0d1b2a',
    labelBackgroundOpacity: 0.7,
    labelPadding: [2, 4],
  }
}

// ===== 数据转换 =====
function toNodeDatum(n) {
  return {
    id: String(n.id),
    data: { ...n, picked: firstPick.value === String(n.id) },
  }
}

function toEdgeDatum(l) {
  // 边 id 加 e 前缀：G6 的 elementMap 节点与边同命名空间，裸数字 id 会和节点 id 撞车
  // （撞了后 getElement 取到边对象，渲染报 getPorts is not a function 整图挂掉）
  return {
    id: `e${l.id}`,
    source: String(l.src_device_id),
    target: String(l.dst_device_id),
    data: l,
  }
}

// ===== G6 初始化 =====
let lastDragEnd = 0 // 用于区分拖拽与点击
let initializing = false // 防止 nextTick 等待期间被定时器重复触发初始化
let resizeObserver = null
let viewportTouched = false // 用户手动缩放/平移后，resize 不再自动居中视口

// 容器 v-show 隐藏时尺寸为 0，必须等 DOM 更新并完成一帧布局后再初始化
async function initGraph() {
  if (initializing || graph) return
  initializing = true
  try {
    await nextTick()
    await new Promise((r) => requestAnimationFrame(r))
    const el = containerRef.value
    if (!el) return

    const nodes = rawGraph.value.nodes.map(toNodeDatum)
    const edges = rawGraph.value.links.map(toEdgeDatum)
    nodes.forEach((n) => nodeMap.set(n.id, n.data))
    edges.forEach((e) => edgeMap.set(e.id, e.data))

    // 坐标统一来自数据（缺失的已由 assignFallbackPositions 铺好），不启用 G6 布局——
    // 配了 layout 它会接管全部节点定位，力导失效时连已有坐标也一起塌缩
    graph = new Graph({
        container: el,
        // 显式传容器实测尺寸，避免量到 0 时退回默认 640x480
        width: el.clientWidth || 800,
        height: el.clientHeight || 500,
        autoResize: true,
        background: '#0d1b2a',
      data: { nodes, edges },
      node: {
        type: 'html',
        style: (d) => {
          const s = { innerHTML: nodeHtml(d.data) }
          if (d.data.x != null && d.data.y != null) {
            s.x = d.data.x
            s.y = d.data.y
          }
          return s
        },
      },
      edge: {
        type: 'line',
        style: (d) => edgeStyle(d.data),
      },
      behaviors: [
        // 用户手动缩放/平移后，窗口 resize 时不再自动居中（避免打断用户视角）
        { type: 'zoom-canvas', onFinish: () => { viewportTouched = true } },
        { type: 'drag-canvas', onFinish: () => { viewportTouched = true } },
        // 仅编辑模式可拖拽节点，结束后保存坐标
        {
          type: 'drag-element',
          enable: () => editMode.value && !linkMode.value,
          onFinish: () => {
            lastDragEnd = Date.now()
            persistPositions()
          },
        },
      ],
      plugins: [
        {
          type: 'tooltip',
          trigger: 'hover',
          getContent: (e, items) => {
            const d = items[0]
            if (!d) return ''
            if (e.targetType === 'node') {
              const n = d.data
              return `<div style="font-size:12px;line-height:1.8">
                <b>${escapeHtml(n.name || n.ip)}</b><br/>
                IP：${escapeHtml(n.ip || '-')}<br/>
                类型：${typeLabel(n.type)}<br/>
                状态：${statusLabel(n.status)}</div>`
            }
            if (e.targetType === 'edge') {
              const l = d.data
              const srcName = nodeMap.get(String(l.src_device_id))?.name || l.src_device_id
              const dstName = nodeMap.get(String(l.dst_device_id))?.name || l.dst_device_id
              const bps = maxTraffic(l)
              return `<div style="font-size:12px;line-height:1.8">
                ${escapeHtml(String(srcName))} ↔ ${escapeHtml(String(dstName))}<br/>
                端口：${escapeHtml(l.src_port || '-')} ↔ ${escapeHtml(l.dst_port || '-')}<br/>
                来源：${l.source === 'manual' ? '手工' : l.source.toUpperCase()}<br/>
                峰值速率：${bps != null ? fmtBpsVal(bps) : '-'}</div>`
            }
            return ''
          },
        },
      ],
    })

    bindEvents()
    window.__topoGraph = graph // 调试句柄（render 前就挂上，render 挂了也能 inspect）
    await graph.render()
    lastSize = { w: el.clientWidth || 800, h: el.clientHeight || 500 }
    // 多节点首次渲染缩放到全图可见（节点少时 fitView 会把单节点放大撑满，退回只居中）
    if (nodes.length > 2) await graph.fitView()
    else await graph.fitCenter()
    observeResize()
  } catch (err) {
    // 先打日志再往上抛（fetchAndApply 会静默吞掉，这里是唯一能看到错误的地方）
    console.error('[topo] initGraph 失败:', err)
    throw err
  } finally {
    initializing = false
  }
}

// 自带 ResizeObserver 兜底（不依赖 autoResize）：容器尺寸变化时同步画布尺寸。
// 注意回调在 observe() 时会立即触发一次，尺寸没变要跳过——否则会把 initGraph 刚做的
// fitView 用 fitCenter 顶掉（全图缩放被打回 100% 居中）
let lastSize = { w: 0, h: 0 }
function observeResize() {
  const el = containerRef.value
  if (!el) return
  resizeObserver?.disconnect() // 分组切换重建画布时避免旧观察者叠加
  resizeObserver = new ResizeObserver(() => {
    if (!graph) return
    const w = el.clientWidth
    const h = el.clientHeight
    if (!w || !h) return
    if (w === lastSize.w && h === lastSize.h) return
    lastSize = { w, h }
    graph.resize(w, h)
    // 用户未手动操作视口时跟随容器重新居中（不缩放，避免单节点被放大撑满屏幕）
    if (!viewportTouched) graph.fitCenter()
  })
  resizeObserver.observe(el)
}

function bindEvents() {
  // 拖拽与点击区分：拖拽结束后短时间内的 click 忽略
  graph.on('node:dragend', () => {
    lastDragEnd = Date.now()
  })

  graph.on('node:click', (e) => {
    if (Date.now() - lastDragEnd < 300) return
    const id = e.target.id
    if (linkMode.value) {
      handleLinkPick(id)
      return
    }
    if (!editMode.value) {
      router.push(`/devices/${id}`)
    }
  })

  // 非编辑模式单击链路弹出实时流量卡（历史曲线入口在卡片内）；编辑模式点击不弹窗
  graph.on('edge:click', (e) => {
    if (Date.now() - lastDragEnd < 300) return
    if (editMode.value) return
    openTrafficCard(e.target.id)
  })

  // 点画布空白处关闭流量卡
  graph.on('canvas:click', () => {
    if (Date.now() - lastDragEnd < 300) return
    closeTrafficCard()
  })

  // 编辑模式下右键链路删除
  graph.on('edge:contextmenu', async (e) => {
    if (!editMode.value) return
    const id = e.target.id
    const l = edgeMap.get(id)
    const ports = l ? [l.src_port, l.dst_port].filter(Boolean).join(' ↔ ') : ''
    try {
      await ElMessageBox.confirm(
        `确定删除链路${ports ? `（${ports}）` : ''}吗？`,
        '删除链路',
        { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
      )
    } catch {
      return
    }
    await deleteTopologyLink(Number(String(id).replace(/^e/, '')))
    ElMessage.success('链路已删除')
    graph.removeEdgeData([id])
    edgeMap.delete(id)
    await graph.draw()
  })
}

// ===== 坐标保存 =====
async function persistPositions() {
  if (!graph) return
  const positions = graph.getNodeData().map((n) => {
    const [x, y] = graph.getElementPosition(n.id)
    // 写回本地数据，保证后续增量刷新/布局判断一致
    n.data.x = Math.round(x)
    n.data.y = Math.round(y)
    nodeMap.set(n.id, n.data)
    return { device_id: Number(n.id), x: n.data.x, y: n.data.y }
  })
  if (!positions.length) return
  try {
    await saveTopologyLayout(positions, activeGroup.value)
  } catch {
    // 提示由拦截器处理，静默失败
  }
}

// ===== 刷新（全量首次 / 增量周期） =====
async function fetchAndApply(manual = false) {
  if (manual) loading.value = true
  try {
    const g = await getTopologyGraph(activeGroup.value || undefined)
    rawGraph.value = g
    nodeCount.value = g.nodes.length
    assignFallbackPositions(g.nodes) // 无坐标节点先铺到确定位置（幂等，同输入同输出不跳动）
    fetchGroups() // 分组清单/设备数随设备变更，随刷新同步（静默失败）
    if (!graph) {
      // initGraph 内部有 nextTick/rAF 等待与重入保护
      if (g.nodes.length) await initGraph()
      return
    }
    // 节点增量：新增/变更/删除
    const newIds = new Set()
    const adds = []
    const updates = []
    for (const n of g.nodes) {
      const id = String(n.id)
      newIds.add(id)
      const old = nodeMap.get(id)
      if (!old) {
        adds.push(toNodeDatum(n))
        nodeMap.set(id, { ...n, picked: false })
      } else if (
        old.status !== n.status || old.name !== n.name
        || old.ip !== n.ip || old.type !== n.type
      ) {
        const data = { ...old, ...n }
        nodeMap.set(id, data)
        updates.push({ id, data, style: { innerHTML: nodeHtml(data) } })
      }
    }
    const nodeRemoves = [...nodeMap.keys()].filter((id) => !newIds.has(id))
    nodeRemoves.forEach((id) => nodeMap.delete(id))

    // 链路增量：流量每周期都变，直接全量更新 + 增删差集
    const newEdgeIds = new Set()
    const edgeAdds = []
    const edgeUpdates = []
    for (const l of g.links) {
      const id = String(l.id)
      newEdgeIds.add(id)
      if (!edgeMap.has(id)) {
        edgeAdds.push(toEdgeDatum(l))
        edgeMap.set(id, l)
      } else {
        edgeMap.set(id, l)
        edgeUpdates.push({ id, data: l, style: edgeStyle(l) })
      }
    }
    const edgeRemoves = [...edgeMap.keys()].filter((id) => !newEdgeIds.has(id))
    edgeRemoves.forEach((id) => edgeMap.delete(id))

    if (adds.length) graph.addNodeData(adds)
    if (updates.length) graph.updateNodeData(updates)
    if (nodeRemoves.length) graph.removeNodeData(nodeRemoves)
    if (edgeAdds.length) graph.addEdgeData(edgeAdds)
    if (edgeUpdates.length) graph.updateEdgeData(edgeUpdates)
    if (edgeRemoves.length) graph.removeEdgeData(edgeRemoves)
    await graph.draw()
  } catch {
    // 提示由拦截器处理
  } finally {
    loading.value = false
  }
}

// ===== 链路流量 5 秒轮询（轻量接口，只更新每条边的流量字段） =====
let trafficTimer = null
const trafficTick = ref(0) // edgeMap 是普通 Map 不参与响应式，流量卡靠它触发重算

function startTrafficTimer() {
  clearInterval(trafficTimer) // 切分组重建时防叠加
  trafficTimer = setInterval(fetchTraffic, 5000)
}

async function fetchTraffic() {
  if (!graph) return // 图未初始化完成（如切分组重建中）跳过本轮
  try {
    const res = await getTopologyTraffic(activeGroup.value || undefined)
    const updates = []
    for (const l of res.links || []) {
      const eid = `e${l.id}` // G6 边 id 带 e 前缀（见 toEdgeDatum）
      const key = edgeMap.has(eid) ? eid : String(l.id)
      const old = edgeMap.get(key)
      if (!old) continue
      const data = { ...old, src_traffic: l.src_traffic, dst_traffic: l.dst_traffic }
      edgeMap.set(key, data)
      updates.push({ id: eid, data, style: edgeStyle(data) })
    }
    if (updates.length) {
      graph.updateEdgeData(updates)
      await graph.draw()
      trafficTick.value++
    }
  } catch {
    // 静默失败，下轮再试
  }
}

// ===== 链路标签显隐 =====
function applyEdgeLabelVisibility() {
  if (!graph) return
  const updates = graph.getEdgeData().map((e) => {
    const data = edgeMap.get(e.id) || e.data
    return { id: e.id, data, style: edgeStyle(data) }
  })
  if (updates.length) {
    graph.updateEdgeData(updates)
    graph.draw()
  }
}

// ===== 链路实时流量卡（非编辑模式单击边弹出，随 5s 轮询自动刷新） =====
const trafficCard = reactive({ visible: false, linkId: null })

const cardLink = computed(() => {
  trafficTick.value // 建立依赖：轮询更新 edgeMap 后重算
  if (!trafficCard.linkId) return null
  return edgeMap.get(trafficCard.linkId)
    || edgeMap.get(String(trafficCard.linkId).replace(/^e/, ''))
    || null
})

const cardTitle = computed(() => {
  const l = cardLink.value
  if (!l) return ''
  const src = nodeMap.get(String(l.src_device_id))?.name || l.src_device_id
  const dst = nodeMap.get(String(l.dst_device_id))?.name || l.dst_device_id
  return `${src} ↔ ${dst}`
})

const cardSides = computed(() => {
  const l = cardLink.value
  if (!l) return []
  const side = (deviceId, port, t) => ({
    name: nodeMap.get(String(deviceId))?.name || deviceId,
    port,
    in_bps: t?.in_bps ?? null,
    out_bps: t?.out_bps ?? null,
    in_util: t?.in_util ?? null,
    out_util: t?.out_util ?? null,
  })
  return [
    side(l.src_device_id, l.src_port, l.src_traffic),
    side(l.dst_device_id, l.dst_port, l.dst_traffic),
  ]
})

const cardHasTraffic = computed(() =>
  cardSides.value.some((s) => s.in_bps != null || s.out_bps != null || s.in_util != null || s.out_util != null)
)

function utilColor(u) {
  if (u == null) return '#8fa8c8'
  if (u > 80) return '#f5222d'
  if (u > 50) return '#faad14'
  return '#52c41a'
}

function fmtUtil(u) {
  return u == null ? '-' : `${Number(u).toFixed(1)}%`
}

function openTrafficCard(edgeId) {
  trafficCard.linkId = edgeId
  trafficCard.visible = true
}

function closeTrafficCard() {
  trafficCard.visible = false
  trafficCard.linkId = null
}

// 卡片底部「历史曲线」：打开流量历史抽屉（原单击边开抽屉行为迁移至此）
function openHistoryFromCard() {
  const l = cardLink.value
  if (l) openTrafficDrawer(l)
}

// ===== 工具栏动作 =====
async function handleDiscover() {
  discovering.value = true
  try {
    const res = await discoverTopology()
    const unmatchedTip = res.unmatched?.length
      ? `；未匹配 ${res.unmatched.length} 条（如 ${res.unmatched.slice(0, 3).map((u) => u.remote_name || u.remote_ip).join('、')}${res.unmatched.length > 3 ? ' 等' : ''}）`
      : ''
    ElMessage.success(
      `自动发现完成：扫描 ${res.scanned} 台，发现邻居 ${res.neighbors} 条，新建链路 ${res.created} 条，跳过 ${res.skipped} 条${unmatchedTip}`
    )
    fetchAndApply()
  } finally {
    discovering.value = false
  }
}

// 自动布局：BFS 分层布局。从度数最大的节点（并列取 id 最小）向外分层：L0=根居中，
// L1 第一环(半径300)、L2 第二环(半径500)、L3+ 每层 +180；不与根连通的孤立点放最外环。
// 确定性布局，不启用 G6 力导（原因见 assignFallbackPositions 注释），仅作用于本按钮。
async function runAutoLayout() {
  if (!graph) return
  layoutLoading.value = true
  try {
    const nodes = graph.getNodeData()
    // 用 edgeMap 里的链路建邻接表
    const adj = new Map(nodes.map((n) => [n.id, []]))
    for (const l of edgeMap.values()) {
      const s = String(l.src_device_id)
      const t = String(l.dst_device_id)
      if (!adj.has(s) || !adj.has(t) || s === t) continue
      adj.get(s).push(t)
      adj.get(t).push(s)
    }
    // 根：度数最大，并列取 id 最小
    let root = nodes[0]
    for (const n of nodes) {
      const d = adj.get(n.id).length
      const rd = adj.get(root.id).length
      if (d > rd || (d === rd && Number(n.id) < Number(root.id))) root = n
    }
    // BFS 分层：L0=根，L1=根的邻居，L2=下一层……
    const level = new Map([[root.id, 0]])
    const queue = [root.id]
    while (queue.length) {
      const cur = queue.shift()
      for (const nb of adj.get(cur)) {
        if (!level.has(nb)) {
          level.set(nb, level.get(cur) + 1)
          queue.push(nb)
        }
      }
    }
    const isolated = nodes.filter((n) => !level.has(n.id)).map((n) => n.id)
    const maxLevel = Math.max(0, ...level.values())
    const layers = []
    for (let i = 0; i <= maxLevel; i++) layers.push([])
    for (const [id, lv] of level) layers[lv].push(id)
    if (isolated.length) layers.push(isolated) // 最外层

    // 中心：优先取当前已有坐标节点的质心（全图已有布局时不乱跳），无则用画布中心
    const positioned = nodes.filter((n) => n.data?.x != null && n.data?.y != null)
    let cx
    let cy
    if (positioned.length) {
      cx = positioned.reduce((s, n) => s + n.data.x, 0) / positioned.length
      cy = positioned.reduce((s, n) => s + n.data.y, 0) / positioned.length
    } else {
      const el = containerRef.value
      cx = (el?.clientWidth || 800) / 2
      cy = (el?.clientHeight || 500) / 2
    }

    const ringRadius = (lv) => (lv <= 1 ? 300 : lv === 2 ? 500 : 500 + (lv - 2) * 180)
    const updates = []
    layers.forEach((ids, li) => {
      const isIsolatedRing = isolated.length > 0 && li === layers.length - 1 && li > maxLevel
      const r = li === 0 ? 0 : isIsolatedRing ? ringRadius(maxLevel) + 180 : ringRadius(li)
      ids.forEach((id, i) => {
        // 环内按角度均匀分布，每层起始角度错开 0.3 弧度减少连线交叉
        const angle = (2 * Math.PI * i) / ids.length + li * 0.3
        const x = Math.round(cx + r * Math.cos(angle))
        const y = Math.round(cy + r * Math.sin(angle))
        const data = { ...(nodeMap.get(id) || {}), x, y }
        nodeMap.set(id, data)
        updates.push({ id, data, style: { x, y } })
      })
    })
    graph.updateNodeData(updates)
    await graph.draw()
    await persistPositions()
    if (nodes.length > 2) await graph.fitView()
    else await graph.fitCenter()
  } finally {
    layoutLoading.value = false
  }
}

// 适应画布：手动触发 fitView（缩放至全图可见）
function handleFitView() {
  graph?.fitView()
}

// 实际大小：恢复 100% 缩放并居中
async function handleActualSize() {
  if (!graph) return
  await graph.zoomTo(1)
  await graph.fitCenter()
}

function toggleLinkMode() {
  linkMode.value = !linkMode.value
  if (!linkMode.value && firstPick.value) {
    markPicked(firstPick.value, false)
    firstPick.value = null
  }
}

// ===== 连线模式 =====
const linkDialog = reactive({ visible: false })
const linkForm = reactive({ srcId: null, dstId: null, srcPort: '', dstPort: '' })
const linkPorts = reactive({ src: [], dst: [] })
const linkSaving = ref(false)

// 标记/取消节点选中高亮
function markPicked(id, picked) {
  const data = nodeMap.get(id)
  if (!data) return
  data.picked = picked
  graph.updateNodeData([{ id, data, style: { innerHTML: nodeHtml(data) } }])
  graph.draw()
}

function handleLinkPick(id) {
  if (!firstPick.value) {
    firstPick.value = id
    markPicked(id, true)
    ElMessage.info('已选择第一个节点，请点击第二个节点')
    return
  }
  if (firstPick.value === id) {
    markPicked(id, false)
    firstPick.value = null
    return
  }
  openLinkDialog(firstPick.value, id)
}

// 取设备已采集接口名作为端口下拉建议
async function loadPortOptions(deviceId) {
  try {
    const res = await getDeviceMetricsCatalog(deviceId)
    const names = new Set()
    ;(res.catalog || []).forEach((c) => {
      if (c.metric.startsWith('if_') && c.labels?.if) names.add(c.labels.if)
    })
    return [...names]
  } catch {
    return []
  }
}

async function openLinkDialog(srcId, dstId) {
  linkForm.srcId = srcId
  linkForm.dstId = dstId
  linkForm.srcPort = ''
  linkForm.dstPort = ''
  linkDialog.visible = true
  const [src, dst] = await Promise.all([loadPortOptions(Number(srcId)), loadPortOptions(Number(dstId))])
  linkPorts.src = src
  linkPorts.dst = dst
}

function closeLinkDialog() {
  linkDialog.visible = false
  if (firstPick.value) {
    markPicked(firstPick.value, false)
    firstPick.value = null
  }
}

async function submitLink() {
  linkSaving.value = true
  try {
    await createTopologyLink({
      src_device_id: Number(linkForm.srcId),
      src_port: linkForm.srcPort || null,
      dst_device_id: Number(linkForm.dstId),
      dst_port: linkForm.dstPort || null,
    })
    ElMessage.success('链路已创建')
    closeLinkDialog()
    fetchAndApply()
  } finally {
    linkSaving.value = false
  }
}

// ===== 链路流量历史曲线（非编辑模式点击链路弹出） =====
const TRAFFIC_RANGES = [
  { key: '1h', label: '近 1 小时', ms: 3600e3 },
  { key: '6h', label: '近 6 小时', ms: 6 * 3600e3 },
  { key: '24h', label: '近 24 小时', ms: 24 * 3600e3 },
  { key: '7d', label: '近 7 天', ms: 7 * 24 * 3600e3 },
]
const trafficDrawer = reactive({ visible: false })
const trafficLink = ref(null)
const trafficRange = ref('1h')
const trafficOption = ref(null)
const trafficHasData = ref(false)
const trafficLoading = ref(false)

// 抽屉标题：设备A/端口A ↔ 设备B/端口B
const trafficTitle = computed(() => {
  const l = trafficLink.value
  if (!l) return ''
  const side = (deviceId, port) =>
    `${nodeMap.get(String(deviceId))?.name || deviceId}/${port || '-'}`
  return `${side(l.src_device_id, l.src_port)} ↔ ${side(l.dst_device_id, l.dst_port)}`
})

function openTrafficDrawer(l) {
  trafficLink.value = l
  trafficDrawer.visible = true
  loadTrafficChart()
}

// 拉取某设备某接口单条流量序列（复用设备指标曲线接口）
async function fetchTrafficSeries(deviceId, port, metric) {
  const end = new Date()
  const start = new Date(end.getTime() - TRAFFIC_RANGES.find((r) => r.key === trafficRange.value).ms)
  const res = await getDeviceMetrics(deviceId, {
    metric,
    start: start.toISOString(),
    end: end.toISOString(),
    labels: JSON.stringify({ if: port }),
    limit: 10000,
  })
  return (res.points || []).map((p) => [p.time, p.value])
}

async function loadTrafficChart() {
  const l = trafficLink.value
  if (!l) return
  trafficLoading.value = true
  try {
    const sides = [
      { name: nodeMap.get(String(l.src_device_id))?.name || l.src_device_id, deviceId: l.src_device_id, port: l.src_port },
      { name: nodeMap.get(String(l.dst_device_id))?.name || l.dst_device_id, deviceId: l.dst_device_id, port: l.dst_port },
    ]
    const series = []
    for (const s of sides) {
      if (!s.port) continue // 无端口的链路（如手工连线未填端口）跳过该侧
      const label = `${s.name}/${s.port}`
      const [inData, outData] = await Promise.all([
        fetchTrafficSeries(s.deviceId, s.port, 'if_in_bps'),
        fetchTrafficSeries(s.deviceId, s.port, 'if_out_bps'),
      ])
      series.push({ name: `${label} 入向`, data: inData })
      series.push({ name: `${label} 出向`, data: outData })
    }
    trafficHasData.value = series.some((s) => s.data.length)
    trafficOption.value = trafficHasData.value
      ? {
          tooltip: { trigger: 'axis', valueFormatter: (v) => (v == null ? '-' : fmtBpsVal(v)) },
          legend: { data: series.map((s) => s.name), top: 0 },
          grid: { left: 76, right: 20, top: 36, bottom: 28 },
          xAxis: { type: 'time' },
          yAxis: { type: 'value', axisLabel: { formatter: (v) => fmtBpsVal(v) } },
          series: series.map((s) => ({
            name: s.name,
            type: 'line',
            showSymbol: false,
            smooth: true,
            data: s.data,
          })),
        }
      : null
  } finally {
    trafficLoading.value = false
  }
}

// ===== WS：设备状态变化局部更新节点颜色 =====
const offWs = onWsMessage('device_status', (msg) => {
  const id = String(msg.device_id)
  const data = nodeMap.get(id)
  if (!data || !graph) return
  data.status = msg.status
  if (msg.name) data.name = msg.name
  graph.updateNodeData([{ id, data, style: { innerHTML: nodeHtml(data) } }])
  graph.draw()
})

// ===== 生命周期 =====
let timer = null
onMounted(() => {
  fetchGroups()
  fetchAndApply()
  // 30 秒全量刷新（节点状态/增删）；链路流量走 5 秒轻量轮询
  timer = setInterval(() => fetchAndApply(), 30000)
  startTrafficTimer()
})

onBeforeUnmount(() => {
  clearInterval(timer)
  clearInterval(trafficTimer)
  offWs()
  resizeObserver?.disconnect()
  resizeObserver = null
  graph?.destroy()
  graph = null
})
</script>

<template>
  <div class="topology-page">
    <!-- 顶部工具栏 -->
    <div class="toolbar">
      <el-select
        v-model="activeGroup"
        class="group-select"
        @change="switchGroup"
      >
        <el-option label="全部" value="" />
        <el-option
          v-for="g in groups"
          :key="g.name"
          :label="`${g.name}（${g.count}）`"
          :value="g.name"
        />
      </el-select>
      <el-divider direction="vertical" />
      <el-button :icon="'Refresh'" :loading="loading" @click="fetchAndApply(true)">刷新</el-button>
      <el-button type="primary" :icon="'Search'" :loading="discovering" @click="handleDiscover">
        自动发现
      </el-button>
      <el-button :icon="'Grid'" :loading="layoutLoading" :disabled="!nodeCount" @click="runAutoLayout">
        自动布局
      </el-button>
      <el-button :icon="'FullScreen'" :disabled="!nodeCount" @click="handleFitView">
        适应画布
      </el-button>
      <el-button :icon="'Aim'" :disabled="!nodeCount" @click="handleActualSize">
        实际大小
      </el-button>
      <el-divider direction="vertical" />
      <span class="switch-label">链路标签</span>
      <el-switch v-model="showEdgeLabels" @change="applyEdgeLabelVisibility" />
      <el-divider direction="vertical" />
      <span class="switch-label">编辑模式</span>
      <el-switch v-model="editMode" @change="!$event && linkMode && toggleLinkMode()" />
      <el-button
        v-if="editMode"
        :type="linkMode ? 'warning' : 'default'"
        :icon="'Link'"
        @click="toggleLinkMode"
      >
        {{ linkMode ? '退出连线' : '连线' }}
      </el-button>
      <span v-if="linkMode" class="hint">连线模式：依次点击两个节点创建链路</span>
      <span v-else-if="editMode" class="hint">编辑模式：可拖拽节点调整位置，右键链路可删除</span>
      <div class="legend">
        <span><i class="lg solid" />手工链路</span>
        <span><i class="lg dash" />自动发现</span>
        <span><i class="lg" style="background:#52c41a" />利用率正常</span>
        <span><i class="lg" style="background:#faad14" />&gt;50%</span>
        <span><i class="lg" style="background:#f5222d" />&gt;80%</span>
      </div>
    </div>

    <!-- 画布（relative 容器，右上角叠链路实时流量卡） -->
    <div v-show="rawGraph?.nodes?.length" class="canvas-wrap">
      <div ref="containerRef" class="canvas" @contextmenu.prevent />
      <div v-if="trafficCard.visible && cardLink" class="traffic-card">
        <div class="tc-head">
          <span class="tc-title" :title="cardTitle">{{ cardTitle }}</span>
          <span class="tc-close" @click="closeTrafficCard">×</span>
        </div>
        <template v-if="cardHasTraffic">
          <div v-for="s in cardSides" :key="s.name" class="tc-row">
            <div class="tc-port">{{ s.name }}<span v-if="s.port"> / {{ s.port }}</span></div>
            <div class="tc-metrics">
              <span>↓ {{ s.in_bps != null ? fmtBpsVal(s.in_bps) : '-' }}</span>
              <span>↑ {{ s.out_bps != null ? fmtBpsVal(s.out_bps) : '-' }}</span>
              <span :style="{ color: utilColor(s.in_util) }">入 {{ fmtUtil(s.in_util) }}</span>
              <span :style="{ color: utilColor(s.out_util) }">出 {{ fmtUtil(s.out_util) }}</span>
            </div>
          </div>
        </template>
        <div v-else class="tc-empty">暂无流量数据</div>
        <div class="tc-foot">
          <el-button size="small" type="primary" plain @click="openHistoryFromCard">历史曲线</el-button>
        </div>
      </div>
    </div>

    <!-- 空状态引导 -->
    <el-empty
      v-if="rawGraph && !rawGraph.nodes.length"
      class="empty"
      :description="activeGroup
        ? `分组「${activeGroup}」下暂无网络/安全设备`
        : '拓扑图仅展示网络/安全设备，暂无数据，请先到「网络设备」或「自动发现」添加'"
    >
      <el-button v-if="!activeGroup" type="primary" @click="router.push('/devices/network')">去添加网络设备</el-button>
    </el-empty>

    <!-- 链路流量历史曲线抽屉 -->
    <el-drawer v-model="trafficDrawer.visible" :title="trafficTitle" size="680px">
      <div class="traffic-bar">
        <el-radio-group v-model="trafficRange" @change="loadTrafficChart">
          <el-radio-button v-for="r in TRAFFIC_RANGES" :key="r.key" :value="r.key">{{ r.label }}</el-radio-button>
        </el-radio-group>
      </div>
      <div v-loading="trafficLoading" class="traffic-body">
        <Chart v-if="trafficOption" :option="trafficOption" height="360px" />
        <el-empty v-else-if="!trafficLoading" description="暂无流量数据" />
      </div>
    </el-drawer>

    <!-- 连线对话框 -->
    <el-dialog v-model="linkDialog.visible" title="创建链路" width="480px" @close="closeLinkDialog">
      <div class="link-tip">
        {{ nodeMap.get(linkForm.srcId)?.name || linkForm.srcId }} ↔ {{ nodeMap.get(linkForm.dstId)?.name || linkForm.dstId }}
      </div>
      <el-form label-width="90px">
        <el-form-item label="源端口">
          <el-select v-model="linkForm.srcPort" filterable allow-create clearable placeholder="选择或输入接口名" style="width: 100%">
            <el-option v-for="p in linkPorts.src" :key="p" :label="p" :value="p" />
          </el-select>
        </el-form-item>
        <el-form-item label="对端端口">
          <el-select v-model="linkForm.dstPort" filterable allow-create clearable placeholder="选择或输入接口名" style="width: 100%">
            <el-option v-for="p in linkPorts.dst" :key="p" :label="p" :value="p" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="closeLinkDialog">取消</el-button>
        <el-button type="primary" :loading="linkSaving" @click="submitLink">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<!-- topo-node 渲染在 G6 HTML 节点容器内，scoped 样式不生效，故单独用非 scoped 块 -->
<style>
.topo-node {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 10px 16px;
  min-width: 140px;
  background: #16283d;
  border: 2px solid #8c8c8c;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}

.topo-picked {
  box-shadow: 0 0 0 3px #e6a23c;
}

.topo-icon {
  font-size: 30px;
  line-height: 1;
}

.topo-icon svg {
  display: block;
  width: 38px;
  height: 38px;
  stroke-width: 1.8;
}

.topo-text {
  text-align: center;
}

.topo-name {
  color: #e8f0fa;
  font-size: 13px;
  font-weight: 600;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.topo-ip {
  color: #8fa8c8;
  font-size: 11px;
}
</style>

<style scoped>
.topology-page {
  height: calc(100vh - 60px - 40px);
  display: flex;
  flex-direction: column;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  background: #fff;
  border-radius: 4px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.switch-label {
  color: #606266;
  font-size: 13px;
}

.group-select {
  width: 180px;
}

.hint {
  color: #e6a23c;
  font-size: 13px;
}

.legend {
  margin-left: auto;
  display: flex;
  gap: 14px;
  color: #909399;
  font-size: 12px;
  align-items: center;
}

.legend .lg {
  display: inline-block;
  width: 18px;
  height: 3px;
  margin-right: 4px;
  vertical-align: middle;
  background: #3d5a80;
}

.legend .lg.solid {
  background: #3d5a80;
}

.legend .lg.dash {
  background: repeating-linear-gradient(90deg, #3d5a80 0 4px, transparent 4px 8px);
}

.canvas-wrap {
  flex: 1;
  min-height: 400px;
  position: relative;
  display: flex;
  flex-direction: column;
}

.canvas {
  flex: 1;
  min-height: 400px;
  border-radius: 4px;
  overflow: hidden;
}

/* 链路实时流量卡：画布右上角浮卡，深色与大屏一致 */
.traffic-card {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 280px;
  background: rgba(13, 27, 42, 0.92);
  border: 1px solid #3d5a80;
  border-radius: 6px;
  padding: 10px 12px;
  z-index: 10;
  color: #e8f0fa;
  font-size: 12px;
}

.tc-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.tc-title {
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tc-close {
  cursor: pointer;
  color: #8fa8c8;
  font-size: 16px;
  line-height: 1;
  padding: 0 2px;
}

.tc-close:hover {
  color: #fff;
}

.tc-row {
  padding: 6px 0;
  border-top: 1px solid rgba(61, 90, 128, 0.4);
}

.tc-port {
  color: #69c0ff;
  margin-bottom: 4px;
}

.tc-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  color: #c9d6e8;
}

.tc-empty {
  padding: 14px 0;
  text-align: center;
  color: #8fa8c8;
}

.tc-foot {
  margin-top: 8px;
  text-align: right;
}

.empty {
  flex: 1;
  background: #fff;
  border-radius: 4px;
}

.link-tip {
  margin-bottom: 12px;
  color: #606266;
  font-weight: 600;
}

.traffic-bar {
  margin-bottom: 16px;
}

.traffic-body {
  min-height: 360px;
}
</style>
