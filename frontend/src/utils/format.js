// 通用格式化 / 下载工具

// bps 自适应格式化：bps / Kbps / Mbps / Gbps
export function fmtBps(v) {
  if (v == null || Number.isNaN(Number(v))) return '-'
  const n = Number(v)
  const abs = Math.abs(n)
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)} Gbps`
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)} Mbps`
  if (abs >= 1e3) return `${(n / 1e3).toFixed(2)} Kbps`
  return `${n.toFixed(0)} bps`
}

// bps 短格式（5.6M / 2.1G），适合拓扑图链路标签等小空间
export function fmtBpsShort(v) {
  if (v == null || Number.isNaN(Number(v))) return '-'
  const n = Number(v)
  const abs = Math.abs(n)
  if (abs >= 1e9) return `${(n / 1e9).toFixed(1)}G`
  if (abs >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return `${n.toFixed(0)}`
}

// 触发浏览器下载 blob 响应；文件名优先解析 Content-Disposition（RFC 5987 filename*=UTF-8''...）
export function downloadBlob(res, fallback = 'export.xlsx') {
  const cd = res.headers?.['content-disposition'] || ''
  let filename = fallback
  const star = cd.match(/filename\*=UTF-8''([^;]+)/i)
  const plain = cd.match(/filename="?([^";]+)"?/i)
  if (star) {
    try {
      filename = decodeURIComponent(star[1].trim())
    } catch {
      filename = star[1].trim()
    }
  } else if (plain) {
    filename = plain[1].trim()
  }
  const url = URL.createObjectURL(res.data)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
