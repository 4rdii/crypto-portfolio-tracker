import { useEffect, useState, useMemo } from 'react'
import {
  AreaChart,
  Area,
  PieChart,
  Pie,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { TrendingUp, TrendingDown, RefreshCw, ArrowUpRight, ArrowDownRight } from 'lucide-react'
import {
  api,
  DashboardResponse,
  TransactionRow,
  ApiError,
} from '../lib/api'
import { formatUSD, formatPercent, chainColor, hashColor, formatDate } from '../lib/format'

const POSITIVE = '#00ff88'
const NEGATIVE = '#ff4757'
const ACCENT = '#00d4ff'

export function Dashboard() {
  const [data, setData] = useState<DashboardResponse | null>(null)
  const [recent, setRecent] = useState<TransactionRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [allocationView, setAllocationView] = useState<'chain' | 'token'>('chain')

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const [dash, txs] = await Promise.all([
        api.get<DashboardResponse>('/api/dashboard'),
        api
          .get<TransactionRow[]>('/api/transactions')
          .catch(() => [] as TransactionRow[]),
      ])
      setData(dash)
      // Recent activity = the most recent 6 transactions, sorted desc by ts.
      const sorted = [...txs].sort((a, b) => (a.ts < b.ts ? 1 : -1)).slice(0, 6)
      setRecent(sorted)
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'Failed to load dashboard')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const handleScanAll = async () => {
    setScanning(true)
    try {
      await api.post('/api/scan-all')
      await load()
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'Scan failed')
    } finally {
      setScanning(false)
    }
  }

  // ----- Derived values -----
  const allocationData = useMemo(() => {
    if (!data) return [] as Array<{ name: string; value: number; color: string }>
    if (allocationView === 'chain') {
      return data.chain_split
        .filter((s) => s.value > 0)
        .sort((a, b) => b.value - a.value)
        .map((s) => ({ name: s.chain, value: s.value, color: chainColor(s.chain) }))
    }
    return data.token_split
      .filter((s) => s.value > 0)
      .map((s) => ({
        name: s.symbol,
        value: s.value,
        color: hashColor(s.symbol),
      }))
  }, [data, allocationView])

  const portfolioSeries = useMemo(() => {
    if (!data) return [] as Array<{ date: string; value: number }>
    return data.snapshots.map((snap) => {
      const d = new Date(snap.ts)
      const label = isNaN(d.getTime())
        ? snap.ts
        : d.toLocaleDateString('en-US', { month: 'short', year: '2-digit' })
      return {
        date: label,
        value: Number(snap.total_usd || 0),
      }
    })
  }, [data])

  const topHoldings = useMemo(() => {
    if (!data) return [] as Array<{ token: string; value: number; percentage: number }>
    const total = data.summary.total_value || 1
    return data.allocation.slice(0, 8).map((a) => ({
      token: a.symbol,
      value: a.value,
      percentage: (a.value / total) * 100,
    }))
  }, [data])

  if (loading && !data) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#0a0e14] text-[#8b949e]">
        Loading dashboard…
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#0a0e14] p-8">
        <div className="max-w-md text-center">
          <p className="text-[#ff4757] mb-4">{error}</p>
          <button
            onClick={load}
            className="px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] rounded-lg border border-[#30363d]"
          >
            Retry
          </button>
        </div>
      </div>
    )
  }

  if (!data) return null

  const { summary } = data
  const unrealColor = summary.unrealized_pnl >= 0 ? POSITIVE : NEGATIVE
  const realColor = summary.realized_pnl >= 0 ? POSITIVE : NEGATIVE

  return (
    <div className="flex-1 overflow-auto bg-[#0a0e14]">
      <div className="p-8 max-w-[1600px]">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h2 className="text-2xl font-semibold text-[#e6edf3]">Dashboard</h2>
            <p className="text-[#8b949e] text-sm mt-1">
              {data.holdings_count} positions across {data.wallet_count} wallets
            </p>
          </div>
          <button
            onClick={handleScanAll}
            disabled={scanning}
            className="flex items-center gap-2 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-50 text-[#e6edf3] rounded-lg transition-colors border border-[#30363d]"
          >
            <RefreshCw size={16} className={scanning ? 'animate-spin' : ''} />
            <span>{scanning ? 'Scanning…' : 'Scan all wallets'}</span>
          </button>
        </div>

        {error && (
          <div className="bg-[#ff4757]/10 border border-[#ff4757]/30 text-[#ff4757] text-sm rounded-lg p-3 mb-6">
            {error}
          </div>
        )}

        {/* Headline metrics */}
        <div className="grid grid-cols-4 gap-6 mb-8">
          <MetricCard
            label="Total Value"
            value={formatUSD(summary.total_value)}
            subtitle={`${data.holdings_count} positions`}
          />
          <MetricCard
            label="Unrealized P&L"
            value={formatUSD(summary.unrealized_pnl)}
            valueColor={unrealColor}
            subtitle={formatPercent(summary.unrealized_pnl_pct || 0) + ' from cost'}
            subtitleColor={unrealColor}
            icon={summary.unrealized_pnl >= 0 ? 'up' : 'down'}
          />
          <MetricCard
            label="Realized P&L"
            value={formatUSD(summary.realized_pnl)}
            valueColor={realColor}
            subtitle="from closed trades"
          />
          <MetricCard
            label="Total Invested"
            value={formatUSD(summary.total_cost_basis)}
            subtitle="Cost basis"
          />
        </div>

        {/* Portfolio value chart + allocation */}
        <div className="grid grid-cols-2 gap-6 mb-8">
          <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-6">
            <h3 className="text-[#e6edf3] font-medium mb-6">Portfolio Value</h3>
            {portfolioSeries.length > 1 ? (
              <ResponsiveContainer width="100%" height={240}>
                <AreaChart data={portfolioSeries}>
                  <defs>
                    <linearGradient id="colorValue" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={ACCENT} stopOpacity={0.3} />
                      <stop offset="95%" stopColor={ACCENT} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <XAxis
                    dataKey="date"
                    tick={{ fill: '#8b949e', fontSize: 11 }}
                    axisLine={{ stroke: '#30363d' }}
                    tickLine={false}
                    minTickGap={30}
                  />
                  <YAxis
                    tick={{ fill: '#8b949e', fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                    tickFormatter={(v) => formatUSD(v, { compact: true })}
                    width={60}
                  />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: '#161b22',
                      border: '1px solid #30363d',
                      borderRadius: '8px',
                      color: '#e6edf3',
                    }}
                    formatter={(value: number) => [formatUSD(value), 'Value']}
                  />
                  <Area
                    type="monotone"
                    dataKey="value"
                    stroke={ACCENT}
                    strokeWidth={2}
                    fill="url(#colorValue)"
                  />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="h-[240px] flex items-center justify-center text-[#8b949e] text-sm">
                Not enough history yet — scan a wallet to start building the chart.
              </div>
            )}
          </div>

          <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-6">
            <div className="flex items-center justify-between mb-6">
              <h3 className="text-[#e6edf3] font-medium">Allocation</h3>
              <div className="flex gap-2">
                <button
                  onClick={() => setAllocationView('chain')}
                  className={`px-3 py-1 rounded text-sm transition-colors ${
                    allocationView === 'chain'
                      ? 'bg-[#00d4ff]/10 text-[#00d4ff]'
                      : 'text-[#8b949e] hover:text-[#e6edf3]'
                  }`}
                >
                  By Chain
                </button>
                <button
                  onClick={() => setAllocationView('token')}
                  className={`px-3 py-1 rounded text-sm transition-colors ${
                    allocationView === 'token'
                      ? 'bg-[#00d4ff]/10 text-[#00d4ff]'
                      : 'text-[#8b949e] hover:text-[#e6edf3]'
                  }`}
                >
                  By Token
                </button>
              </div>
            </div>
            {allocationData.length === 0 ? (
              <div className="h-[200px] flex items-center justify-center text-[#8b949e] text-sm">
                No positions yet.
              </div>
            ) : (
              <div className="flex items-center gap-6">
                <ResponsiveContainer width="50%" height={200}>
                  <PieChart>
                    <Pie
                      data={allocationData}
                      cx="50%"
                      cy="50%"
                      innerRadius={60}
                      outerRadius={80}
                      paddingAngle={2}
                      dataKey="value"
                    >
                      {allocationData.map((entry, index) => (
                        <Cell key={`cell-${index}`} fill={entry.color} />
                      ))}
                    </Pie>
                    <Tooltip
                      contentStyle={{
                        backgroundColor: '#161b22',
                        border: '1px solid #30363d',
                        borderRadius: '8px',
                        color: '#e6edf3',
                      }}
                      formatter={(value: number) => formatUSD(value)}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="space-y-2 flex-1 max-h-[200px] overflow-y-auto">
                  {allocationData.slice(0, 8).map((item) => (
                    <div
                      key={item.name}
                      className="flex items-center justify-between text-sm"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <div
                          className="w-3 h-3 rounded-full flex-shrink-0"
                          style={{ backgroundColor: item.color }}
                        />
                        <span className="text-[#e6edf3] truncate">{item.name}</span>
                      </div>
                      <span className="text-[#8b949e] font-mono ml-2">
                        {formatUSD(item.value, { compact: true })}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Top holdings + recent activity */}
        <div className="grid grid-cols-2 gap-6">
          <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-6">
            <h3 className="text-[#e6edf3] font-medium mb-4">Top Holdings</h3>
            {topHoldings.length === 0 ? (
              <div className="text-[#8b949e] text-sm">No holdings yet.</div>
            ) : (
              <div className="space-y-3">
                {topHoldings.map((holding) => (
                  <div key={holding.token} className="flex items-center justify-between">
                    <div className="flex items-center gap-3 min-w-0">
                      <div className="w-8 h-8 rounded-full bg-[#1c2128] flex items-center justify-center text-[#e6edf3] text-xs font-semibold flex-shrink-0">
                        {holding.token.slice(0, 2)}
                      </div>
                      <div className="min-w-0">
                        <div className="text-[#e6edf3] font-medium truncate">
                          {holding.token}
                        </div>
                        <div className="text-[#8b949e] text-xs">
                          {holding.percentage.toFixed(1)}% of portfolio
                        </div>
                      </div>
                    </div>
                    <div className="text-right flex-shrink-0 ml-3">
                      <div className="text-[#e6edf3] font-mono">{formatUSD(holding.value)}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-6">
            <h3 className="text-[#e6edf3] font-medium mb-4">Recent Activity</h3>
            {recent.length === 0 ? (
              <div className="text-[#8b949e] text-sm">No transactions yet.</div>
            ) : (
              <div className="space-y-3">
                {recent.map((tx) => {
                  const positive = ['buy', 'deposit', 'swap'].includes(
                    (tx.tx_type || '').toLowerCase(),
                  )
                  const value = Number(tx.amount || 0) * Number(tx.price_usd || 0)
                  return (
                    <div key={tx.id} className="flex items-center justify-between">
                      <div className="flex items-center gap-3 min-w-0">
                        <div
                          className={`w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 ${
                            positive ? 'bg-[#00ff88]/10' : 'bg-[#ff4757]/10'
                          }`}
                        >
                          {positive ? (
                            <ArrowUpRight size={16} className="text-[#00ff88]" />
                          ) : (
                            <ArrowDownRight size={16} className="text-[#ff4757]" />
                          )}
                        </div>
                        <div className="min-w-0">
                          <div className="text-[#e6edf3] font-medium truncate">
                            <span className="capitalize">{tx.tx_type}</span> {tx.symbol}
                          </div>
                          <div className="text-[#8b949e] text-xs">{formatDate(tx.ts, true)}</div>
                        </div>
                      </div>
                      <div className="text-right flex-shrink-0 ml-3">
                        <div className="text-[#e6edf3] font-mono text-sm">
                          {tx.amount > 0 ? '+' : ''}
                          {Number(tx.amount || 0).toLocaleString()}
                        </div>
                        <div className="text-[#8b949e] text-xs font-mono">
                          {formatUSD(value)}
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function MetricCard({
  label,
  value,
  subtitle,
  valueColor,
  subtitleColor,
  icon,
}: {
  label: string
  value: string
  subtitle?: string
  valueColor?: string
  subtitleColor?: string
  icon?: 'up' | 'down'
}) {
  return (
    <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-6">
      <div className="text-[#8b949e] text-sm mb-2">{label}</div>
      <div
        className="text-3xl font-bold mb-2 font-mono"
        style={{ color: valueColor || '#e6edf3' }}
      >
        {value}
      </div>
      {subtitle && (
        <div
          className="flex items-center gap-1 text-sm"
          style={{ color: subtitleColor || '#8b949e' }}
        >
          {icon === 'up' && <TrendingUp size={14} />}
          {icon === 'down' && <TrendingDown size={14} />}
          <span>{subtitle}</span>
        </div>
      )}
    </div>
  )
}
