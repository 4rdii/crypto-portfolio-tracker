import { useEffect, useMemo, useState } from 'react'
import { Filter, Plus, Search, X, Trash2, RefreshCw } from 'lucide-react'
import { api, TransactionRow, WalletRow, ApiError } from '../lib/api'
import { formatUSD, formatNumber, formatDate } from '../lib/format'

const TYPES = ['buy', 'sell', 'deposit', 'withdraw', 'swap'] as const
const PAGE_SIZE = 100

export function Transactions() {
  const [txs, setTxs] = useState<TransactionRow[]>([])
  const [wallets, setWallets] = useState<WalletRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [showFilters, setShowFilters] = useState(false)
  const [selectedTypes, setSelectedTypes] = useState<string[]>([])
  const [selectedWallets, setSelectedWallets] = useState<number[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const [symbolFilter, setSymbolFilter] = useState('')
  const [minUsd, setMinUsd] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [page, setPage] = useState(1)

  const [showAdd, setShowAdd] = useState(false)
  const [addForm, setAddForm] = useState({
    ts: new Date().toISOString().slice(0, 10),
    tx_type: 'buy',
    symbol: '',
    amount: '',
    price_usd: '',
    notes: '',
    wallet_id: '',
  })
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)

  // Reprice state — single button triggers both top-coin cache warm-up
  // and the per-tx reprice sweep. We collapse them into one UX action
  // because the user mental model is "fix my prices" and they should
  // not need to know we're warming two caches.
  const [repricing, setRepricing] = useState(false)
  const [repriceMessage, setRepriceMessage] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const [txList, walletList] = await Promise.all([
        api.get<TransactionRow[]>('/api/transactions'),
        api.get<WalletRow[]>('/api/wallets'),
      ])
      setTxs(txList)
      setWallets(walletList)
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'Failed to load transactions')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const walletLabel = (id: number | null | undefined): string => {
    if (id == null) return 'Manual'
    return wallets.find((w) => w.id === id)?.label || `Wallet #${id}`
  }

  const filtered = useMemo(() => {
    const minVal = parseFloat(minUsd)
    const hasMin = !isNaN(minVal) && minVal > 0
    const fromTs = dateFrom ? new Date(dateFrom).getTime() : null
    const toTs = dateTo ? new Date(dateTo).getTime() + 86400000 : null
    const sym = symbolFilter.trim().toUpperCase()
    const q = searchQuery.trim().toLowerCase()

    return txs.filter((tx) => {
      if (selectedTypes.length > 0 && !selectedTypes.includes((tx.tx_type || '').toLowerCase()))
        return false
      if (selectedWallets.length > 0) {
        if (tx.wallet_id == null) return false
        if (!selectedWallets.includes(tx.wallet_id)) return false
      }
      if (sym && !(tx.symbol || '').toUpperCase().includes(sym)) return false
      if (q) {
        const hay = `${tx.symbol || ''} ${tx.notes || ''}`.toLowerCase()
        if (!hay.includes(q)) return false
      }
      const value = Number(tx.amount || 0) * Number(tx.price_usd || 0)
      if (hasMin && Math.abs(value) < minVal) return false
      if (fromTs != null || toTs != null) {
        const t = new Date(tx.ts).getTime()
        if (fromTs != null && t < fromTs) return false
        if (toTs != null && t >= toTs) return false
      }
      return true
    })
  }, [txs, selectedTypes, selectedWallets, searchQuery, symbolFilter, minUsd, dateFrom, dateTo])

  // Reset pagination when filters change.
  useEffect(() => {
    setPage(1)
  }, [selectedTypes, selectedWallets, searchQuery, symbolFilter, minUsd, dateFrom, dateTo])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const pageSlice = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  const toggleType = (t: string) =>
    setSelectedTypes((prev) => (prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t]))
  const toggleWallet = (id: number) =>
    setSelectedWallets((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    )
  const clearAll = () => {
    setSelectedTypes([])
    setSelectedWallets([])
    setSearchQuery('')
    setSymbolFilter('')
    setMinUsd('')
    setDateFrom('')
    setDateTo('')
  }

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault()
    setAdding(true)
    setAddError(null)
    try {
      const body: any = {
        ts: addForm.ts,
        tx_type: addForm.tx_type,
        symbol: addForm.symbol.trim().toUpperCase(),
        amount: parseFloat(addForm.amount),
        price_usd: addForm.price_usd ? parseFloat(addForm.price_usd) : 0,
        notes: addForm.notes.trim() || null,
      }
      if (addForm.wallet_id) body.wallet_id = parseInt(addForm.wallet_id, 10)
      if (!body.symbol || !isFinite(body.amount)) {
        throw new Error('Symbol and amount are required')
      }
      await api.post('/api/transactions', body)
      setShowAdd(false)
      setAddForm({
        ts: new Date().toISOString().slice(0, 10),
        tx_type: 'buy',
        symbol: '',
        amount: '',
        price_usd: '',
        notes: '',
        wallet_id: '',
      })
      await load()
    } catch (err: any) {
      setAddError(err instanceof ApiError ? err.detail : err.message || 'Failed to add')
    } finally {
      setAdding(false)
    }
  }

  const handleDelete = async (id: number) => {
    if (!confirm('Delete this transaction?')) return
    try {
      await api.delete(`/api/transactions/${id}`)
      setTxs((prev) => prev.filter((t) => t.id !== id))
    } catch (err: any) {
      setError(err instanceof ApiError ? err.detail : err.message || 'Delete failed')
    }
  }

  // Refresh every tx's USD price using Binance 5m candles.
  //
  // Only re-enriches the user's own transaction rows — the underlying
  // price cache is kept fresh by a server-side daily background task,
  // so we don't need to (and no longer can) trigger a market-wide
  // price backfill from the client. This button is purely about
  // correcting historical prices on rows the user owns.
  const handleRepriceAll = async () => {
    if (repricing) return
    if (!confirm('Recalculate every transaction price from Binance 5m candles? This will overwrite existing prices.')) {
      return
    }
    setRepricing(true)
    setRepriceMessage(null)
    setError(null)
    try {
      const res = await api.post<{
        updated: number
        total_txs: number
        groups: number
        skipped: number
      }>('/api/transactions/reprice-all', {})
      setRepriceMessage(
        `Repriced ${res.updated} of ${res.total_txs} transactions across ${res.groups} (symbol, day) groups. ${res.skipped} skipped (stables / long-tail).`,
      )
      await load()
    } catch (err: any) {
      setError(err instanceof ApiError ? err.detail : err.message || 'Reprice failed')
    } finally {
      setRepricing(false)
    }
  }

  if (loading && txs.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#0a0e14] text-[#8b949e]">
        Loading transactions…
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-auto bg-[#0a0e14]">
      <div className="p-8 max-w-[1800px]">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h2 className="text-2xl font-semibold text-[#e6edf3]">Transactions</h2>
            <p className="text-[#8b949e] text-sm mt-1">
              {filtered.length.toLocaleString()} of {txs.length.toLocaleString()} transactions
            </p>
          </div>
          <div className="flex gap-3">
            <button
              onClick={handleRepriceAll}
              disabled={repricing}
              title="Re-fetch every tx price from Binance 5-minute candles"
              className="flex items-center gap-2 px-4 py-2 rounded-lg transition-colors border bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-50 disabled:cursor-not-allowed text-[#e6edf3] border-[#30363d]"
            >
              <RefreshCw size={16} className={repricing ? 'animate-spin' : ''} />
              <span>{repricing ? 'Repricing…' : 'Reprice all'}</span>
            </button>
            <button
              onClick={() => setShowFilters(!showFilters)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg transition-colors border ${
                showFilters
                  ? 'bg-[#00d4ff]/10 text-[#00d4ff] border-[#00d4ff]/40'
                  : 'bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] border-[#30363d]'
              }`}
            >
              <Filter size={16} />
              <span>Filter</span>
            </button>
            <button
              onClick={() => setShowAdd(true)}
              className="flex items-center gap-2 px-4 py-2 bg-[#00d4ff] hover:bg-[#00d4ff]/90 text-[#0a0e14] rounded-lg transition-colors font-medium"
            >
              <Plus size={16} />
              <span>Add Transaction</span>
            </button>
          </div>
        </div>

        {error && (
          <div className="bg-[#ff4757]/10 border border-[#ff4757]/30 text-[#ff4757] text-sm rounded-lg p-3 mb-6">
            {error}
          </div>
        )}

        {repriceMessage && (
          <div className="bg-[#00ff88]/10 border border-[#00ff88]/30 text-[#00ff88] text-sm rounded-lg p-3 mb-6 flex items-center justify-between">
            <span>{repriceMessage}</span>
            <button
              onClick={() => setRepriceMessage(null)}
              className="text-[#00ff88]/60 hover:text-[#00ff88]"
            >
              <X size={14} />
            </button>
          </div>
        )}

        {showFilters && (
          <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-6 mb-6 space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="relative">
                <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#8b949e]" />
                <input
                  type="text"
                  placeholder="Search token or notes…"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="w-full bg-[#1c2128] border border-[#30363d] rounded-lg pl-10 pr-4 py-2.5 text-[#e6edf3] placeholder:text-[#8b949e] focus:outline-none focus:border-[#00d4ff]"
                />
              </div>
              <input
                type="text"
                placeholder="Symbol contains…"
                value={symbolFilter}
                onChange={(e) => setSymbolFilter(e.target.value)}
                className="w-full bg-[#1c2128] border border-[#30363d] rounded-lg px-4 py-2.5 text-[#e6edf3] placeholder:text-[#8b949e] focus:outline-none focus:border-[#00d4ff]"
              />
            </div>
            <div className="grid grid-cols-3 gap-4">
              <div>
                <label className="text-[#8b949e] text-xs uppercase tracking-wider mb-1 block">
                  From
                </label>
                <input
                  type="date"
                  value={dateFrom}
                  onChange={(e) => setDateFrom(e.target.value)}
                  className="w-full bg-[#1c2128] border border-[#30363d] rounded-lg px-3 py-2 text-[#e6edf3] focus:outline-none focus:border-[#00d4ff]"
                />
              </div>
              <div>
                <label className="text-[#8b949e] text-xs uppercase tracking-wider mb-1 block">
                  To
                </label>
                <input
                  type="date"
                  value={dateTo}
                  onChange={(e) => setDateTo(e.target.value)}
                  className="w-full bg-[#1c2128] border border-[#30363d] rounded-lg px-3 py-2 text-[#e6edf3] focus:outline-none focus:border-[#00d4ff]"
                />
              </div>
              <div>
                <label className="text-[#8b949e] text-xs uppercase tracking-wider mb-1 block">
                  Min USD
                </label>
                <input
                  type="number"
                  placeholder="0"
                  value={minUsd}
                  onChange={(e) => setMinUsd(e.target.value)}
                  className="w-full bg-[#1c2128] border border-[#30363d] rounded-lg px-3 py-2 text-[#e6edf3] placeholder:text-[#8b949e] focus:outline-none focus:border-[#00d4ff]"
                />
              </div>
            </div>
            <div>
              <div className="text-[#8b949e] text-xs uppercase tracking-wider mb-2">
                Transaction Type
              </div>
              <div className="flex flex-wrap gap-2">
                {TYPES.map((t) => (
                  <button
                    key={t}
                    onClick={() => toggleType(t)}
                    className={`px-3 py-1.5 rounded-lg text-sm transition-colors capitalize ${
                      selectedTypes.includes(t)
                        ? 'bg-[#00d4ff]/10 text-[#00d4ff] border border-[#00d4ff]'
                        : 'bg-[#1c2128] text-[#8b949e] border border-[#30363d] hover:text-[#e6edf3]'
                    }`}
                  >
                    {t}
                  </button>
                ))}
              </div>
            </div>
            {wallets.length > 0 && (
              <div>
                <div className="text-[#8b949e] text-xs uppercase tracking-wider mb-2">Wallet</div>
                <div className="flex flex-wrap gap-2">
                  {wallets.map((w) => (
                    <button
                      key={w.id}
                      onClick={() => toggleWallet(w.id)}
                      className={`px-3 py-1.5 rounded-lg text-sm transition-colors ${
                        selectedWallets.includes(w.id)
                          ? 'bg-[#00d4ff]/10 text-[#00d4ff] border border-[#00d4ff]'
                          : 'bg-[#1c2128] text-[#8b949e] border border-[#30363d] hover:text-[#e6edf3]'
                      }`}
                    >
                      {w.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <div className="flex justify-end">
              <button
                onClick={clearAll}
                className="text-[#8b949e] hover:text-[#e6edf3] text-sm"
              >
                Clear all filters
              </button>
            </div>
          </div>
        )}

        <div className="bg-[#0d1117] border border-[#30363d] rounded-lg overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-[#30363d]">
                  <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Date
                  </th>
                  <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Type
                  </th>
                  <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Token
                  </th>
                  <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Amount
                  </th>
                  <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Price
                  </th>
                  <th className="text-right text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Value
                  </th>
                  <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Wallet
                  </th>
                  <th className="text-left text-[#8b949e] text-xs font-medium uppercase tracking-wider px-6 py-4">
                    Notes
                  </th>
                  <th className="px-6 py-4"></th>
                </tr>
              </thead>
              <tbody>
                {pageSlice.map((tx) => {
                  const type = (tx.tx_type || '').toLowerCase()
                  const value = Number(tx.amount || 0) * Number(tx.price_usd || 0)
                  return (
                    <tr
                      key={tx.id}
                      className="border-b border-[#30363d] last:border-b-0 hover:bg-[#161b22] transition-colors"
                    >
                      <td className="px-6 py-4">
                        <span className="text-[#e6edf3] font-mono text-xs">
                          {formatDate(tx.ts, true)}
                        </span>
                      </td>
                      <td className="px-6 py-4">
                        <span
                          className={`px-2 py-1 rounded text-xs font-medium capitalize ${
                            type === 'buy' || type === 'deposit'
                              ? 'bg-[#00ff88]/10 text-[#00ff88]'
                              : type === 'sell' || type === 'withdraw'
                              ? 'bg-[#ff4757]/10 text-[#ff4757]'
                              : 'bg-[#00d4ff]/10 text-[#00d4ff]'
                          }`}
                        >
                          {type}
                        </span>
                      </td>
                      <td className="px-6 py-4">
                        <div className="flex items-center gap-2">
                          <div className="w-6 h-6 rounded-full bg-[#1c2128] flex items-center justify-center text-[#e6edf3] text-xs font-semibold flex-shrink-0">
                            {(tx.symbol || '?').slice(0, 2)}
                          </div>
                          <span className="text-[#e6edf3] font-medium">{tx.symbol}</span>
                        </div>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <span className="text-[#e6edf3] font-mono text-sm">
                          {formatNumber(tx.amount)}
                        </span>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <span className="text-[#e6edf3] font-mono text-sm">
                          {tx.price_usd > 0 ? formatUSD(tx.price_usd) : '—'}
                        </span>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <span className="text-[#e6edf3] font-mono text-sm">
                          {formatUSD(value)}
                        </span>
                      </td>
                      <td className="px-6 py-4">
                        <span className="text-[#8b949e] text-sm">{walletLabel(tx.wallet_id)}</span>
                      </td>
                      <td className="px-6 py-4 max-w-xs">
                        <span className="text-[#8b949e] text-xs font-mono truncate block">
                          {tx.notes || ''}
                        </span>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <button
                          onClick={() => handleDelete(tx.id)}
                          className="text-[#8b949e] hover:text-[#ff4757] transition-colors"
                          title="Delete"
                        >
                          <Trash2 size={14} />
                        </button>
                      </td>
                    </tr>
                  )
                })}
                {pageSlice.length === 0 && (
                  <tr>
                    <td colSpan={9} className="px-6 py-12 text-center text-[#8b949e] text-sm">
                      No transactions match the current filter.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="px-6 py-4 border-t border-[#30363d] flex items-center justify-between">
            <span className="text-[#8b949e] text-sm">
              Page {page} of {totalPages}
            </span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage(page - 1)}
                className="px-3 py-1.5 rounded-lg text-sm bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-30 disabled:cursor-not-allowed text-[#e6edf3] border border-[#30363d]"
              >
                Prev
              </button>
              <button
                disabled={page >= totalPages}
                onClick={() => setPage(page + 1)}
                className="px-3 py-1.5 rounded-lg text-sm bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-30 disabled:cursor-not-allowed text-[#e6edf3] border border-[#30363d]"
              >
                Next
              </button>
            </div>
          </div>
        </div>
      </div>

      {showAdd && (
        <>
          <div className="fixed inset-0 bg-black/60 z-40" onClick={() => setShowAdd(false)} />
          <div className="fixed inset-0 z-50 flex items-center justify-center p-6">
            <form
              onSubmit={handleAdd}
              className="bg-[#0d1117] border border-[#30363d] rounded-lg p-6 max-w-lg w-full"
            >
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-[#e6edf3] text-xl font-semibold">Add Transaction</h3>
                <button
                  type="button"
                  onClick={() => setShowAdd(false)}
                  className="text-[#8b949e] hover:text-[#e6edf3]"
                >
                  <X size={18} />
                </button>
              </div>
              {addError && (
                <div className="bg-[#ff4757]/10 border border-[#ff4757]/30 text-[#ff4757] text-sm rounded-lg p-3 mb-4">
                  {addError}
                </div>
              )}
              <div className="grid grid-cols-2 gap-3">
                <Field label="Date">
                  <input
                    type="date"
                    required
                    value={addForm.ts}
                    onChange={(e) => setAddForm({ ...addForm, ts: e.target.value })}
                    className="input-dark"
                  />
                </Field>
                <Field label="Type">
                  <select
                    value={addForm.tx_type}
                    onChange={(e) => setAddForm({ ...addForm, tx_type: e.target.value })}
                    className="input-dark"
                  >
                    {TYPES.map((t) => (
                      <option key={t} value={t} className="bg-[#1c2128] capitalize">
                        {t}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Symbol">
                  <input
                    type="text"
                    required
                    placeholder="ETH"
                    value={addForm.symbol}
                    onChange={(e) => setAddForm({ ...addForm, symbol: e.target.value })}
                    className="input-dark"
                  />
                </Field>
                <Field label="Amount">
                  <input
                    type="number"
                    step="any"
                    required
                    placeholder="0"
                    value={addForm.amount}
                    onChange={(e) => setAddForm({ ...addForm, amount: e.target.value })}
                    className="input-dark"
                  />
                </Field>
                <Field label="Price (USD)">
                  <input
                    type="number"
                    step="any"
                    placeholder="0"
                    value={addForm.price_usd}
                    onChange={(e) => setAddForm({ ...addForm, price_usd: e.target.value })}
                    className="input-dark"
                  />
                </Field>
                <Field label="Wallet">
                  <select
                    value={addForm.wallet_id}
                    onChange={(e) => setAddForm({ ...addForm, wallet_id: e.target.value })}
                    className="input-dark"
                  >
                    <option value="" className="bg-[#1c2128]">
                      Manual / none
                    </option>
                    {wallets.map((w) => (
                      <option key={w.id} value={w.id} className="bg-[#1c2128]">
                        {w.label}
                      </option>
                    ))}
                  </select>
                </Field>
                <div className="col-span-2">
                  <Field label="Notes">
                    <input
                      type="text"
                      placeholder="optional"
                      value={addForm.notes}
                      onChange={(e) => setAddForm({ ...addForm, notes: e.target.value })}
                      className="input-dark"
                    />
                  </Field>
                </div>
              </div>
              <div className="flex gap-3 mt-5">
                <button
                  type="button"
                  onClick={() => setShowAdd(false)}
                  className="flex-1 px-4 py-2.5 rounded-lg bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] border border-[#30363d]"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={adding}
                  className="flex-1 px-4 py-2.5 rounded-lg bg-[#00d4ff] hover:bg-[#00d4ff]/90 disabled:opacity-50 text-[#0a0e14] font-semibold"
                >
                  {adding ? 'Adding…' : 'Add'}
                </button>
              </div>
            </form>
          </div>
        </>
      )}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-[#8b949e] text-xs uppercase tracking-wider mb-1 block">{label}</span>
      {children}
    </label>
  )
}
