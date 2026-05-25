import { useEffect, useState } from 'react'
import {
  RefreshCw,
  ExternalLink,
  Receipt,
  Info,
} from 'lucide-react'
import { api, BillingSummary, ApiError } from '../lib/api'
import { formatUSD, formatDate, truncateAddress } from '../lib/format'

// Block explorer hostnames for each supported chain, used to build the
// per-row "view on explorer" link. Keys must match the chain slugs the
// backend writes into credit_transactions.chain.
const EXPLORERS: Record<string, string> = {
  eth: 'https://etherscan.io',
  arbitrum: 'https://arbiscan.io',
  optimism: 'https://optimistic.etherscan.io',
  base: 'https://basescan.org',
  polygon: 'https://polygonscan.com',
  bsc: 'https://bscscan.com',
}

function explorerTxUrl(chain: string | null, txHash: string | null): string | null {
  if (!chain || !txHash) return null
  const base = EXPLORERS[chain.toLowerCase()]
  if (!base) return null
  return `${base}/tx/${txHash}`
}

export function Billing() {
  const [summary, setSummary] = useState<BillingSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.get<BillingSummary>('/api/billing/summary')
      setSummary(res)
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'Failed to load billing')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  if (loading && !summary) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#0a0e14] text-[#8b949e]">
        Loading billing…
      </div>
    )
  }

  if (error && !summary) {
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

  if (!summary) return null

  const { treasury_address, accepted_tokens, pricing, transactions } = summary

  const totalSpent = transactions.reduce(
    (acc, tx) => acc + Math.abs(Number(tx.delta_usd || 0)),
    0,
  )

  return (
    <div className="flex-1 overflow-auto bg-[#0a0e14]">
      <div className="p-8 max-w-[1400px]">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h2 className="text-2xl font-semibold text-[#e6edf3]">Billing</h2>
            <p className="text-[#8b949e] text-sm mt-1">
              Pay-per-import · no prepaid balance, one on-chain tx per history import
            </p>
          </div>
          <div className="flex gap-3">
            <button
              onClick={load}
              disabled={loading}
              className="flex items-center gap-2 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-50 text-[#e6edf3] rounded-lg transition-colors border border-[#30363d]"
            >
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
              <span>Refresh</span>
            </button>
          </div>
        </div>

        {/* How it works banner */}
        <div className="bg-[#00d4ff]/5 border border-[#00d4ff]/20 rounded-xl p-5 mb-6 flex gap-3">
          <Info size={18} className="text-[#00d4ff] flex-shrink-0 mt-0.5" />
          <div className="text-sm text-[#8b949e] leading-relaxed">
            <span className="text-[#e6edf3] font-medium">How billing works: </span>
            balance scans are free. When you import a wallet's full history,
            you'll see a quote and pay directly from a linked wallet in a single
            on-chain transaction. Want to refresh every wallet at once? Use{' '}
            <span className="text-[#e6edf3] font-medium">Batch import</span> on
            the Wallets page to pay for all imports with one tx.
          </div>
        </div>

        {/* Headline spend card */}
        <div className="grid grid-cols-3 gap-6 mb-6">
          <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-6">
            <div className="text-[#8b949e] text-xs uppercase tracking-wider mb-2">
              Lifetime spent
            </div>
            <div className="text-3xl font-bold font-mono text-[#e6edf3]">
              {formatUSD(totalSpent)}
            </div>
            <div className="text-[#8b949e] text-xs mt-1">
              across {transactions.length} payment{transactions.length === 1 ? '' : 's'}
            </div>
          </div>
          <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-6">
            <div className="text-[#8b949e] text-xs uppercase tracking-wider mb-2">
              Per transaction
            </div>
            <div className="text-3xl font-bold font-mono text-[#e6edf3]">
              ${pricing.per_tx.toFixed(4)}
            </div>
            <div className="text-[#8b949e] text-xs mt-1">
              ${pricing.min_charge.toFixed(2)} minimum per import · {pricing.delta_multiplier}× on delta re-imports
            </div>
          </div>
          <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-6">
            <div className="text-[#8b949e] text-xs uppercase tracking-wider mb-2">
              Free txs remaining
            </div>
            <div className="text-3xl font-bold font-mono text-[#e6edf3]">
              {pricing.free_allowance_remaining}
              <span className="text-[#8b949e] text-lg font-normal"> / {pricing.free_allowance}</span>
            </div>
            <div className="text-[#8b949e] text-xs mt-1">
              shared across all your wallets (lifetime)
            </div>
          </div>
        </div>

        {/* Payment history */}
        <div className="bg-[#0d1117] border border-[#30363d] rounded-lg overflow-hidden">
          <div className="px-6 py-4 border-b border-[#30363d]">
            <h3 className="text-[#e6edf3] font-medium flex items-center gap-2">
              <Receipt size={16} />
              Payment history
            </h3>
          </div>
          {transactions.length === 0 ? (
            <div className="p-12 text-center text-[#8b949e] text-sm">
              No payments yet. Import a wallet's history from the Wallets page to get started.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-[#30363d]">
                    <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-3">
                      Date
                    </th>
                    <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-3">
                      Description
                    </th>
                    <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-3">
                      Paid with
                    </th>
                    <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-3">
                      Chain
                    </th>
                    <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-3">
                      Cost
                    </th>
                    <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-3">
                      Tx
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {transactions.map((tx) => {
                    const cost = Math.abs(Number(tx.delta_usd || 0))
                    const explorer = explorerTxUrl(tx.chain, tx.tx_hash)
                    const tokenAmt = tx.token_amount != null
                      ? Number(tx.token_amount).toFixed(2)
                      : null
                    return (
                      <tr
                        key={tx.id}
                        className="border-b border-[#30363d] last:border-b-0 hover:bg-[#161b22] transition-colors"
                      >
                        <td className="px-6 py-4">
                          <span className="text-[#e6edf3] text-sm">
                            {formatDate(tx.created_at, true)}
                          </span>
                        </td>
                        <td className="px-6 py-4">
                          <span className="text-[#e6edf3] text-sm">
                            {tx.notes || 'Import payment'}
                          </span>
                        </td>
                        <td className="px-6 py-4">
                          <span className="text-[#e6edf3] text-sm font-mono">
                            {tokenAmt ? `${tokenAmt} ${tx.token_symbol || ''}` : '—'}
                          </span>
                        </td>
                        <td className="px-6 py-4">
                          <span className="text-[#8b949e] text-xs uppercase tracking-wider">
                            {tx.chain || '—'}
                          </span>
                        </td>
                        <td className="px-6 py-4 text-right">
                          <span className="font-mono text-sm text-[#e6edf3]">
                            {formatUSD(cost)}
                          </span>
                        </td>
                        <td className="px-6 py-4 text-right">
                          {explorer ? (
                            <a
                              href={explorer}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex items-center gap-1 text-[#00d4ff] hover:text-[#00d4ff]/80 text-xs font-mono"
                              title={tx.tx_hash || ''}
                            >
                              {tx.tx_hash ? truncateAddress(tx.tx_hash, 6, 4) : '—'}
                              <ExternalLink size={11} />
                            </a>
                          ) : (
                            <span className="text-[#8b949e] text-xs">—</span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="mt-6 text-[#8b949e] text-xs">
          Treasury address:{' '}
          <span className="font-mono text-[#e6edf3]">{truncateAddress(treasury_address, 10, 8)}</span>
          {' · '}
          Accepted tokens: {accepted_tokens.join(', ')}
        </div>
      </div>
    </div>
  )
}
