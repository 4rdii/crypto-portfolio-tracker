/**
 * Number and date formatters shared across screens.
 *
 * Crypto P&L spans from sub-cent dust to six-figure positions, so we use
 * Intl with dynamic fraction digits — tiny numbers still show meaningful
 * precision and big numbers stay readable.
 */

export function formatUSD(value: number | null | undefined, opts?: { compact?: boolean }): string {
  const v = Number(value || 0)
  if (!isFinite(v)) return '$0'
  const abs = Math.abs(v)
  // Compact mode for dashboards: $1.2K, $45.6M
  if (opts?.compact && abs >= 10000) {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(v)
  }
  // Sub-dollar dust needs more precision
  let max = 2
  if (abs > 0 && abs < 0.01) max = 6
  else if (abs < 1) max = 4
  else if (abs >= 1000) max = 0
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 0,
    maximumFractionDigits: max,
  }).format(v)
}

export function formatUSDSigned(value: number | null | undefined): string {
  const v = Number(value || 0)
  const sign = v > 0 ? '+' : ''
  return sign + formatUSD(v)
}

export function formatPercent(value: number | null | undefined, digits = 2): string {
  const v = Number(value || 0)
  const sign = v >= 0 ? '+' : ''
  return `${sign}${v.toFixed(digits)}%`
}

export function formatNumber(value: number | null | undefined): string {
  const v = Number(value || 0)
  if (!isFinite(v)) return '0'
  const abs = Math.abs(v)
  let max = 2
  if (abs > 0 && abs < 0.0001) max = 8
  else if (abs < 0.01) max = 6
  else if (abs < 1) max = 4
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: max,
  }).format(v)
}

export function truncateAddress(addr: string | null | undefined, left = 6, right = 4): string {
  if (!addr) return ''
  if (addr.length <= left + right + 3) return addr
  return `${addr.slice(0, left)}…${addr.slice(-right)}`
}

export function formatDate(iso: string | null | undefined, withTime = false): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  if (withTime) {
    return d.toLocaleString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  }
  return d.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

// Deterministic color for a chain or symbol — same input always hashes to the
// same HSL, so the pie chart and badges stay stable across re-renders.
export function hashColor(key: string): string {
  let h = 0
  for (let i = 0; i < key.length; i++) {
    h = (h << 5) - h + key.charCodeAt(i)
    h |= 0
  }
  const hue = Math.abs(h) % 360
  return `hsl(${hue}, 65%, 58%)`
}

// Well-known chain colors overlaid on hashColor for the chains we actually
// surface — makes the pie chart match established brand palettes without
// hard-coding every minor L2.
const CHAIN_COLORS: Record<string, string> = {
  eth: '#627eea',
  ethereum: '#627eea',
  base: '#0052ff',
  arbitrum: '#28a0f0',
  optimism: '#ff0420',
  polygon: '#8247e5',
  bsc: '#f0b90b',
  avalanche: '#e84142',
  solana: '#9945ff',
  bitcoin: '#f7931a',
  btc: '#f7931a',
}

export function chainColor(chain: string): string {
  const key = (chain || '').toLowerCase()
  return CHAIN_COLORS[key] || hashColor(key || 'chain')
}
