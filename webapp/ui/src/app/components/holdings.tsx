import { useEffect, useMemo, useState } from 'react'
import { Filter, RefreshCw } from 'lucide-react'
import { api, HoldingsResponse, HoldingWalletBreakdown, ApiError } from '../lib/api'
import { formatUSD, formatPercent, formatNumber } from '../lib/format'

// Tooltip state for the wallet-breakdown popover. Lives at the Holdings
// component level so a single <div> is rendered outside the table's
// overflow container — otherwise overflow-x:auto clips the popover on
// Y as well (CSS: any non-visible axis forces the other to auto).
interface WalletTooltipState {
  x: number
  y: number
  breakdown: HoldingWalletBreakdown[]
}

export function Holdings() {
  const [data, setData] = useState<HoldingsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showFilters, setShowFilters] = useState(false)
  const [selectedWallets, setSelectedWallets] = useState<number[]>([])
  const [walletTip, setWalletTip] = useState<WalletTooltipState | null>(null)

  const load = async (walletIds: number[] = selectedWallets) => {
    setLoading(true)
    setError(null)
    try {
      const qs = walletIds.length > 0 ? `?wallets=${walletIds.join(',')}` : ''
      const res = await api.get<HoldingsResponse>('/api/holdings' + qs)
      setData(res)
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'Failed to load holdings')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load([])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const toggleWallet = (id: number) => {
    const next = selectedWallets.includes(id)
      ? selectedWallets.filter((w) => w !== id)
      : [...selectedWallets, id]
    setSelectedWallets(next)
    load(next)
  }

  const clearFilter = () => {
    setSelectedWallets([])
    load([])
  }

  // Sort holdings: active first, then by value desc, closed last.
  const sortedHoldings = useMemo(() => {
    if (!data) return []
    return [...data.holdings].sort((a, b) => {
      const aClosed = !!a.closed
      const bClosed = !!b.closed
      if (aClosed !== bClosed) return aClosed ? 1 : -1
      return (b.current_value || 0) - (a.current_value || 0)
    })
  }, [data])

  if (loading && !data) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#0a0e14] text-[#8b949e]">
        Loading holdings…
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#0a0e14] p-8">
        <div className="max-w-md text-center">
          <p className="text-[#ff4757] mb-4">{error}</p>
          <button
            onClick={() => load([])}
            className="px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] rounded-lg border border-[#30363d]"
          >
            Retry
          </button>
        </div>
      </div>
    )
  }

  if (!data) return null

  const { summary, wallets } = data

  return (
    <div className="flex-1 overflow-auto bg-[#0a0e14]">
      <div className="p-8 max-w-[1800px]">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h2 className="text-2xl font-semibold text-[#e6edf3]">Holdings</h2>
            <p className="text-[#8b949e] text-sm mt-1">
              {sortedHoldings.length} positions
              {data.filter_active ? ` · filtered to ${selectedWallets.length} wallet(s)` : ''}
            </p>
          </div>
          <div className="flex gap-3">
            <button
              onClick={() => load()}
              disabled={loading}
              className="flex items-center gap-2 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-50 text-[#e6edf3] rounded-lg transition-colors border border-[#30363d]"
            >
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
              <span>Refresh</span>
            </button>
            <button
              onClick={() => setShowFilters(!showFilters)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg transition-colors border ${
                showFilters || selectedWallets.length > 0
                  ? 'bg-[#00d4ff]/10 text-[#00d4ff] border-[#00d4ff]/40'
                  : 'bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] border-[#30363d]'
              }`}
            >
              <Filter size={16} />
              <span>Filter{selectedWallets.length > 0 ? ` (${selectedWallets.length})` : ''}</span>
            </button>
          </div>
        </div>

        {showFilters && (
          <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-4 mb-6">
            <div className="flex items-center justify-between mb-3">
              <span className="text-[#e6edf3] font-medium">Filter by Wallet</span>
              {selectedWallets.length > 0 && (
                <button
                  onClick={clearFilter}
                  className="text-[#8b949e] hover:text-[#e6edf3] text-sm"
                >
                  Clear all
                </button>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              {wallets.map((wallet) => (
                <button
                  key={wallet.id}
                  onClick={() => toggleWallet(wallet.id)}
                  className={`px-3 py-1.5 rounded-lg text-sm transition-colors ${
                    selectedWallets.includes(wallet.id)
                      ? 'bg-[#00d4ff]/10 text-[#00d4ff] border border-[#00d4ff]'
                      : 'bg-[#1c2128] text-[#8b949e] border border-[#30363d] hover:text-[#e6edf3]'
                  }`}
                >
                  {wallet.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Summary row */}
        <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-6 mb-6">
          <div className="grid grid-cols-4 gap-8">
            <SummaryCell label="Total Value" value={formatUSD(summary.total_value)} />
            <SummaryCell label="Total Invested" value={formatUSD(summary.total_cost_basis)} />
            <SummaryCell
              label="Unrealized P&L"
              value={formatUSD(summary.unrealized_pnl)}
              valueColor={summary.unrealized_pnl >= 0 ? '#00ff88' : '#ff4757'}
              sub={formatPercent(summary.unrealized_pnl_pct || 0)}
            />
            <SummaryCell
              label="Realized P&L"
              value={formatUSD(summary.realized_pnl)}
              valueColor={summary.realized_pnl >= 0 ? '#00ff88' : '#ff4757'}
            />
          </div>
        </div>

        <div className="bg-[#0d1117] border border-[#30363d] rounded-lg overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-[#30363d]">
                  <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Token
                  </th>
                  <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Chain
                  </th>
                  <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Wallet
                  </th>
                  <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Amount
                  </th>
                  <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Avg Buy
                  </th>
                  <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Market
                  </th>
                  <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Invested
                  </th>
                  <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Value
                  </th>
                  <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Unrealized
                  </th>
                  <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Realized
                  </th>
                </tr>
              </thead>
              <tbody>
                {sortedHoldings.map((h) => {
                  const closed = !!h.closed
                  const isDeFi = !!(h.protocols && h.protocols !== 'wallet')
                  const chain = h.chains || ''
                  // Aggregate holding row may span multiple wallets — pull
                  // unique labels from the breakdown so we can show e.g.
                  // "EVM Wallet, Aave wallet" in the Wallet column.
                  const walletLabels = Array.from(
                    new Set((h.wallet_breakdown || []).map((wb) => wb.label)),
                  )
                  const walletDisplay =
                    walletLabels.length === 0
                      ? '—'
                      : walletLabels.length === 1
                      ? walletLabels[0]
                      : `${walletLabels[0]} +${walletLabels.length - 1}`
                  return (
                    <tr
                      key={`${h.symbol}-${chain}-${h.protocols || ''}`}
                      className="border-b border-[#30363d] last:border-b-0 hover:bg-[#161b22] transition-colors"
                    >
                      <td className="px-6 py-4">
                        <div className="flex items-center gap-3">
                          <div className="w-8 h-8 rounded-full bg-[#1c2128] flex items-center justify-center text-[#e6edf3] text-xs font-semibold flex-shrink-0">
                            {h.symbol.slice(0, 2)}
                          </div>
                          <div className="min-w-0">
                            <div className="text-[#e6edf3] font-medium flex items-center gap-2 flex-wrap">
                              {h.symbol}
                              {closed && (
                                <span className="text-xs px-2 py-0.5 bg-[#1c2128] text-[#8b949e] rounded">
                                  Closed
                                </span>
                              )}
                              {isDeFi && (
                                <span className="text-xs px-2 py-0.5 bg-[#00d4ff]/10 text-[#00d4ff] rounded">
                                  {h.protocols}
                                </span>
                              )}
                            </div>
                          </div>
                        </div>
                      </td>
                      <td className="px-6 py-4">
                        <span className="text-[#e6edf3] text-sm">{chain || '—'}</span>
                      </td>
                      <td className="px-6 py-4">
                        {walletLabels.length <= 1 ? (
                          <span className="text-[#8b949e] text-sm">{walletDisplay}</span>
                        ) : (
                          <span
                            className="text-[#8b949e] text-sm border-b border-dotted border-[#484f58] cursor-help"
                            onMouseEnter={(e) => {
                              const r = (e.currentTarget as HTMLElement).getBoundingClientRect()
                              setWalletTip({
                                x: r.left,
                                y: r.bottom + 6,
                                breakdown: h.wallet_breakdown || [],
                              })
                            }}
                            onMouseLeave={() => setWalletTip(null)}
                          >
                            {walletDisplay}
                          </span>
                        )}
                      </td>
                      <td className="px-6 py-4 text-right">
                        <span className="text-[#e6edf3] font-mono text-sm">
                          {formatNumber(h.balance)}
                        </span>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <span className="text-[#e6edf3] font-mono text-sm">
                          {h.avg_cost > 0 ? formatUSD(h.avg_cost) : '—'}
                        </span>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <span className="text-[#e6edf3] font-mono text-sm">
                          {formatUSD(h.usd_price)}
                        </span>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <span className="text-[#e6edf3] font-mono text-sm">
                          {formatUSD(h.cost_basis_value)}
                        </span>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <span className="text-[#e6edf3] font-mono text-sm">
                          {formatUSD(h.current_value)}
                        </span>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <div className="flex flex-col items-end">
                          <span
                            className="font-mono text-sm"
                            style={{ color: h.unrealized_pnl >= 0 ? '#00ff88' : '#ff4757' }}
                          >
                            {formatUSD(h.unrealized_pnl)}
                          </span>
                          <span
                            className="text-xs font-mono"
                            style={{ color: h.unrealized_pnl_pct >= 0 ? '#00ff88' : '#ff4757' }}
                          >
                            {formatPercent(h.unrealized_pnl_pct || 0)}
                          </span>
                        </div>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <span
                          className="font-mono text-sm"
                          style={{
                            color:
                              h.realized_pnl === 0
                                ? '#8b949e'
                                : h.realized_pnl >= 0
                                ? '#00ff88'
                                : '#ff4757',
                          }}
                        >
                          {h.realized_pnl === 0 ? '—' : formatUSD(h.realized_pnl)}
                        </span>
                      </td>
                    </tr>
                  )
                })}
                {sortedHoldings.length === 0 && (
                  <tr>
                    <td colSpan={10} className="px-6 py-12 text-center text-[#8b949e] text-sm">
                      No holdings to show. Scan a wallet from the Wallets page to populate this table.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
      {walletTip && <WalletTooltip state={walletTip} />}
    </div>
  )
}

/**
 * Wallet-breakdown tooltip. Rendered once at the Holdings root in fixed
 * coordinates so it escapes the table's overflow container. Shows
 * per-wallet label, chain, protocol, balance, and USD value for each
 * slice that contributes to the aggregate row.
 */
function WalletTooltip({ state }: { state: WalletTooltipState }) {
  return (
    <div
      className="fixed z-50 pointer-events-none min-w-[280px] bg-[#161b22] border border-[#30363d] rounded-lg shadow-2xl p-3"
      style={{ left: state.x, top: state.y }}
    >
      <div className="text-[#8b949e] text-xs uppercase tracking-wider mb-2">
        {state.breakdown.length} wallets
      </div>
      <div className="space-y-1.5">
        {state.breakdown.map((wb, i) => (
          <div
            key={`${wb.wallet_id}-${wb.chain}-${wb.protocol}-${i}`}
            className="flex items-center justify-between gap-4 text-xs"
          >
            <div className="min-w-0 flex-1">
              <div className="text-[#e6edf3] truncate">
                {wb.label}
                {wb.protocol && wb.protocol !== 'wallet' && (
                  <span className="ml-1.5 text-[#00d4ff]">· {wb.protocol}</span>
                )}
              </div>
              <div className="text-[#8b949e] font-mono">{wb.chain}</div>
            </div>
            <div className="text-right flex-shrink-0">
              <div className="text-[#e6edf3] font-mono">{formatNumber(wb.balance)}</div>
              <div className="text-[#8b949e] font-mono">{formatUSD(wb.value)}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function SummaryCell({
  label,
  value,
  valueColor,
  sub,
}: {
  label: string
  value: string
  valueColor?: string
  sub?: string
}) {
  return (
    <div>
      <div className="text-[#8b949e] text-sm mb-1">{label}</div>
      <div
        className="text-2xl font-bold font-mono"
        style={{ color: valueColor || '#e6edf3' }}
      >
        {value}
      </div>
      {sub && (
        <div className="text-xs font-mono mt-1" style={{ color: valueColor || '#8b949e' }}>
          {sub}
        </div>
      )}
    </div>
  )
}
