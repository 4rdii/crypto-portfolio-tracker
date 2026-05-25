import { useEffect, useState, useMemo } from 'react'
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from 'recharts'
import {
  Users,
  DollarSign,
  TrendingUp,
  Plus,
  ArrowDownRight,
  ArrowUpRight,
  Settings,
  X,
  ChevronDown,
} from 'lucide-react'
import { api, ApiError } from '../lib/api'
import { formatUSD, hashColor, formatDate } from '../lib/format'

// ---------- Types ----------

interface Shareholder {
  name: string
  shares: number
  pool_pct: number
  pool_value: number
  personal: number
  total: number
}

interface FundStatus {
  pooled_value: number
  total_shares: number
  nav_per_share: number
  total_aum: number
  shareholders: Shareholder[]
}

interface FundTransaction {
  created_at: string
  name: string
  type: string
  usd_amount: number
  shares_delta: number
  nav_per_share: number
  note: string
}

interface WalletInfo {
  id: number
  label: string
  address: string
  chain: string
  value: number
  ownership_type: string | null
  owner_name: string | null
}

// ---------- Component ----------

export function Fund() {
  const [status, setStatus] = useState<FundStatus | null>(null)
  const [history, setHistory] = useState<FundTransaction[]>([])
  const [wallets, setWallets] = useState<WalletInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [initialized, setInitialized] = useState<boolean | null>(null)

  // UI state
  const [showDepositForm, setShowDepositForm] = useState(false)
  const [showWithdrawForm, setShowWithdrawForm] = useState(false)
  const [showAddPerson, setShowAddPerson] = useState(false)
  const [showSetup, setShowSetup] = useState(false)
  const [historyFilter, setHistoryFilter] = useState('')
  const [actionLoading, setActionLoading] = useState(false)

  // Form state
  const [txPerson, setTxPerson] = useState('')
  const [txAmount, setTxAmount] = useState('')
  const [txNote, setTxNote] = useState('')
  const [newPersonName, setNewPersonName] = useState('')
  const [newPersonShares, setNewPersonShares] = useState('')

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const [s, h, w] = await Promise.all([
        api.get<FundStatus>('/api/fund/v2/status'),
        api.get<FundTransaction[]>('/api/fund/v2/history'),
        api.get<WalletInfo[]>('/api/fund/v2/wallets'),
      ])
      setStatus(s)
      setHistory(h)
      setWallets(w)
      setInitialized(s.shareholders.length > 0)
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 401) {
        setError('Not authenticated. Please sign in first.')
      } else {
        setError(e instanceof ApiError ? e.detail : e.message || 'Failed to load fund data')
      }
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const filteredHistory = useMemo(() => {
    if (!historyFilter) return history
    return history.filter((tx) => tx.name.toLowerCase() === historyFilter.toLowerCase())
  }, [history, historyFilter])

  const allocationData = useMemo(() => {
    if (!status) return []
    return status.shareholders
      .filter((s) => s.pool_value > 0)
      .sort((a, b) => b.pool_value - a.pool_value)
      .map((s) => ({ name: s.name, value: s.pool_value, color: hashColor(s.name) }))
  }, [status])

  // --- Actions ---

  const handleDeposit = async () => {
    if (!txPerson || !txAmount) return
    setActionLoading(true)
    try {
      await api.post('/api/fund/v2/deposit', {
        person: txPerson,
        amount: parseFloat(txAmount),
        note: txNote,
      })
      setShowDepositForm(false)
      setTxPerson('')
      setTxAmount('')
      setTxNote('')
      await load()
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message)
    } finally {
      setActionLoading(false)
    }
  }

  const handleWithdraw = async () => {
    if (!txPerson || !txAmount) return
    setActionLoading(true)
    try {
      await api.post('/api/fund/v2/withdrawal', {
        person: txPerson,
        amount: parseFloat(txAmount),
        note: txNote,
      })
      setShowWithdrawForm(false)
      setTxPerson('')
      setTxAmount('')
      setTxNote('')
      await load()
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message)
    } finally {
      setActionLoading(false)
    }
  }

  const handleAddPerson = async () => {
    if (!newPersonName) return
    setActionLoading(true)
    try {
      await api.post('/api/fund/v2/add-person', {
        name: newPersonName,
        shares: parseFloat(newPersonShares) || 0,
      })
      setShowAddPerson(false)
      setNewPersonName('')
      setNewPersonShares('')
      await load()
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message)
    } finally {
      setActionLoading(false)
    }
  }

  // --- Loading / Error states ---

  if (loading && !status) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#0a0e14] text-[#8b949e]">
        Loading fund data...
      </div>
    )
  }

  if (error && !status) {
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

  // --- Setup wizard (first-time) ---

  if (initialized === false || showSetup) {
    return (
      <SetupWizard
        wallets={wallets}
        onComplete={() => {
          setShowSetup(false)
          load()
        }}
        onCancel={() => setShowSetup(false)}
      />
    )
  }

  if (!status) return null

  return (
    <div className="flex-1 overflow-auto bg-[#0a0e14]">
      <div className="p-8 max-w-[1600px]">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div>
            <h2 className="text-2xl font-semibold text-[#e6edf3]">Fund Management</h2>
            <p className="text-[#8b949e] text-sm mt-1">
              {status.shareholders.length} shareholders - NAV/Share: {formatUSD(status.nav_per_share)}
            </p>
          </div>
          <div className="flex gap-3">
            <button
              onClick={() => {
                setShowDepositForm(true)
                setShowWithdrawForm(false)
              }}
              className="flex items-center gap-2 px-4 py-2 bg-[#00ff88]/10 hover:bg-[#00ff88]/20 text-[#00ff88] rounded-lg transition-colors border border-[#00ff88]/30"
            >
              <ArrowDownRight size={16} />
              Deposit
            </button>
            <button
              onClick={() => {
                setShowWithdrawForm(true)
                setShowDepositForm(false)
              }}
              className="flex items-center gap-2 px-4 py-2 bg-[#ff4757]/10 hover:bg-[#ff4757]/20 text-[#ff4757] rounded-lg transition-colors border border-[#ff4757]/30"
            >
              <ArrowUpRight size={16} />
              Withdraw
            </button>
            <button
              onClick={() => setShowAddPerson(true)}
              className="flex items-center gap-2 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] rounded-lg transition-colors border border-[#30363d]"
            >
              <Plus size={16} />
              Add Person
            </button>
            <button
              onClick={() => setShowSetup(true)}
              className="flex items-center gap-2 px-3 py-2 bg-[#1c2128] hover:bg-[#21262d] text-[#8b949e] rounded-lg transition-colors border border-[#30363d]"
              title="Fund settings"
            >
              <Settings size={16} />
            </button>
          </div>
        </div>

        {error && (
          <div className="bg-[#ff4757]/10 border border-[#ff4757]/30 text-[#ff4757] text-sm rounded-lg p-3 mb-6">
            {error}
            <button onClick={() => setError(null)} className="ml-3 underline">
              dismiss
            </button>
          </div>
        )}

        {/* Deposit/Withdraw form */}
        {(showDepositForm || showWithdrawForm) && (
          <TransactionForm
            type={showDepositForm ? 'deposit' : 'withdrawal'}
            shareholders={status.shareholders}
            person={txPerson}
            amount={txAmount}
            note={txNote}
            loading={actionLoading}
            onPersonChange={setTxPerson}
            onAmountChange={setTxAmount}
            onNoteChange={setTxNote}
            onSubmit={showDepositForm ? handleDeposit : handleWithdraw}
            onCancel={() => {
              setShowDepositForm(false)
              setShowWithdrawForm(false)
              setTxPerson('')
              setTxAmount('')
              setTxNote('')
            }}
          />
        )}

        {/* Add Person modal */}
        {showAddPerson && (
          <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-6 mb-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-[#e6edf3] font-medium">Add Shareholder</h3>
              <button onClick={() => setShowAddPerson(false)} className="text-[#8b949e] hover:text-[#e6edf3]">
                <X size={18} />
              </button>
            </div>
            <div className="flex gap-4">
              <input
                type="text"
                placeholder="Name"
                value={newPersonName}
                onChange={(e) => setNewPersonName(e.target.value)}
                className="flex-1 bg-[#0a0e14] border border-[#30363d] text-[#e6edf3] rounded-lg px-4 py-2 focus:outline-none focus:border-[#00d4ff]"
              />
              <input
                type="number"
                placeholder="Initial shares (0)"
                value={newPersonShares}
                onChange={(e) => setNewPersonShares(e.target.value)}
                className="w-40 bg-[#0a0e14] border border-[#30363d] text-[#e6edf3] rounded-lg px-4 py-2 focus:outline-none focus:border-[#00d4ff]"
              />
              <button
                onClick={handleAddPerson}
                disabled={actionLoading || !newPersonName}
                className="px-6 py-2 bg-[#00d4ff]/10 hover:bg-[#00d4ff]/20 text-[#00d4ff] rounded-lg border border-[#00d4ff]/30 disabled:opacity-50"
              >
                {actionLoading ? 'Adding...' : 'Add'}
              </button>
            </div>
          </div>
        )}

        {/* Metric cards */}
        <div className="grid grid-cols-4 gap-6 mb-8">
          <MetricCard
            label="Pooled Value"
            value={formatUSD(status.pooled_value)}
            icon={<DollarSign size={18} className="text-[#00d4ff]" />}
          />
          <MetricCard
            label="Total AUM"
            value={formatUSD(status.total_aum)}
            icon={<TrendingUp size={18} className="text-[#00ff88]" />}
          />
          <MetricCard
            label="NAV / Share"
            value={formatUSD(status.nav_per_share)}
            subtitle={`${status.total_shares.toFixed(2)} total shares`}
          />
          <MetricCard
            label="Shareholders"
            value={String(status.shareholders.length)}
            icon={<Users size={18} className="text-[#a371f7]" />}
          />
        </div>

        {/* Shareholders table + allocation chart */}
        <div className="grid grid-cols-3 gap-6 mb-8">
          {/* Table */}
          <div className="col-span-2 bg-[#0d1117] border border-[#30363d] rounded-lg">
            <div className="p-6 pb-4">
              <h3 className="text-[#e6edf3] font-medium">Shareholders</h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-t border-[#30363d] text-left text-[#8b949e] text-xs uppercase tracking-wider">
                    <th className="px-6 py-3">Name</th>
                    <th className="px-6 py-3 text-right">Shares</th>
                    <th className="px-6 py-3 text-right">Pool %</th>
                    <th className="px-6 py-3 text-right">Pool Value</th>
                    <th className="px-6 py-3 text-right">Personal</th>
                    <th className="px-6 py-3 text-right">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {status.shareholders.map((sh) => (
                    <tr
                      key={sh.name}
                      className="border-t border-[#21262d] hover:bg-[#161b22] transition-colors"
                    >
                      <td className="px-6 py-3 text-[#e6edf3] font-medium">
                        <div className="flex items-center gap-2">
                          <div
                            className="w-2 h-2 rounded-full"
                            style={{ backgroundColor: hashColor(sh.name) }}
                          />
                          {sh.name}
                        </div>
                      </td>
                      <td className="px-6 py-3 text-right text-[#e6edf3] font-mono text-sm">
                        {sh.shares.toFixed(2)}
                      </td>
                      <td className="px-6 py-3 text-right text-[#8b949e] text-sm">
                        {sh.pool_pct.toFixed(1)}%
                      </td>
                      <td className="px-6 py-3 text-right text-[#e6edf3] font-mono text-sm">
                        {formatUSD(sh.pool_value)}
                      </td>
                      <td className="px-6 py-3 text-right text-[#8b949e] font-mono text-sm">
                        {sh.personal > 0 ? formatUSD(sh.personal) : '\u2014'}
                      </td>
                      <td className="px-6 py-3 text-right text-[#e6edf3] font-mono text-sm font-semibold">
                        {formatUSD(sh.total)}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-[#30363d] font-semibold">
                    <td className="px-6 py-3 text-[#e6edf3]">Total</td>
                    <td className="px-6 py-3 text-right text-[#e6edf3] font-mono text-sm">
                      {status.total_shares.toFixed(2)}
                    </td>
                    <td className="px-6 py-3 text-right text-[#8b949e] text-sm">100.0%</td>
                    <td className="px-6 py-3 text-right text-[#e6edf3] font-mono text-sm">
                      {formatUSD(status.pooled_value)}
                    </td>
                    <td className="px-6 py-3" />
                    <td className="px-6 py-3 text-right text-[#e6edf3] font-mono text-sm">
                      {formatUSD(status.total_aum)}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </div>

          {/* Allocation chart */}
          <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-6">
            <h3 className="text-[#e6edf3] font-medium mb-4">Allocation</h3>
            {allocationData.length > 0 ? (
              <>
                <ResponsiveContainer width="100%" height={220}>
                  <PieChart>
                    <Pie
                      data={allocationData}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="50%"
                      innerRadius={50}
                      outerRadius={85}
                      paddingAngle={2}
                      stroke="none"
                    >
                      {allocationData.map((entry, i) => (
                        <Cell key={i} fill={entry.color} />
                      ))}
                    </Pie>
                    <Tooltip
                      formatter={(value: number) => formatUSD(value)}
                      contentStyle={{
                        background: '#161b22',
                        border: '1px solid #30363d',
                        borderRadius: 8,
                        color: '#e6edf3',
                        fontSize: 13,
                      }}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="space-y-2 mt-4">
                  {allocationData.map((d) => (
                    <div key={d.name} className="flex items-center justify-between text-sm">
                      <div className="flex items-center gap-2">
                        <div
                          className="w-2.5 h-2.5 rounded-full"
                          style={{ backgroundColor: d.color }}
                        />
                        <span className="text-[#e6edf3]">{d.name}</span>
                      </div>
                      <span className="text-[#8b949e] font-mono">{formatUSD(d.value)}</span>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <div className="text-[#8b949e] text-sm text-center py-12">
                No allocation data yet
              </div>
            )}
          </div>
        </div>

        {/* Transaction history */}
        <div className="bg-[#0d1117] border border-[#30363d] rounded-lg">
          <div className="p-6 pb-4 flex items-center justify-between">
            <h3 className="text-[#e6edf3] font-medium">Transaction History</h3>
            <div className="relative">
              <select
                value={historyFilter}
                onChange={(e) => setHistoryFilter(e.target.value)}
                className="appearance-none bg-[#0a0e14] border border-[#30363d] text-[#e6edf3] rounded-lg pl-3 pr-8 py-1.5 text-sm focus:outline-none focus:border-[#00d4ff]"
              >
                <option value="">All people</option>
                {status.shareholders.map((sh) => (
                  <option key={sh.name} value={sh.name}>
                    {sh.name}
                  </option>
                ))}
              </select>
              <ChevronDown
                size={14}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[#8b949e] pointer-events-none"
              />
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-t border-[#30363d] text-left text-[#8b949e] text-xs uppercase tracking-wider">
                  <th className="px-6 py-3">Date</th>
                  <th className="px-6 py-3">Person</th>
                  <th className="px-6 py-3">Type</th>
                  <th className="px-6 py-3 text-right">Amount</th>
                  <th className="px-6 py-3 text-right">Shares</th>
                  <th className="px-6 py-3 text-right">NAV</th>
                  <th className="px-6 py-3">Note</th>
                </tr>
              </thead>
              <tbody>
                {filteredHistory.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="px-6 py-8 text-center text-[#8b949e] text-sm">
                      No transactions yet
                    </td>
                  </tr>
                ) : (
                  filteredHistory.map((tx, i) => (
                    <tr
                      key={i}
                      className="border-t border-[#21262d] hover:bg-[#161b22] transition-colors"
                    >
                      <td className="px-6 py-3 text-[#8b949e] text-sm">
                        {formatDate(tx.created_at, true)}
                      </td>
                      <td className="px-6 py-3 text-[#e6edf3] text-sm">{tx.name}</td>
                      <td className="px-6 py-3">
                        <span
                          className={`inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full ${
                            tx.type === 'deposit'
                              ? 'bg-[#00ff88]/10 text-[#00ff88]'
                              : tx.type === 'withdrawal'
                              ? 'bg-[#ff4757]/10 text-[#ff4757]'
                              : 'bg-[#00d4ff]/10 text-[#00d4ff]'
                          }`}
                        >
                          {tx.type}
                        </span>
                      </td>
                      <td className="px-6 py-3 text-right text-[#e6edf3] font-mono text-sm">
                        {formatUSD(tx.usd_amount)}
                      </td>
                      <td
                        className={`px-6 py-3 text-right font-mono text-sm ${
                          tx.shares_delta >= 0 ? 'text-[#00ff88]' : 'text-[#ff4757]'
                        }`}
                      >
                        {tx.shares_delta >= 0 ? '+' : ''}
                        {tx.shares_delta.toFixed(4)}
                      </td>
                      <td className="px-6 py-3 text-right text-[#8b949e] font-mono text-sm">
                        {formatUSD(tx.nav_per_share)}
                      </td>
                      <td className="px-6 py-3 text-[#8b949e] text-sm max-w-[200px] truncate">
                        {tx.note || '\u2014'}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  )
}

// ---------- Sub-components ----------

function MetricCard({
  label,
  value,
  subtitle,
  icon,
}: {
  label: string
  value: string
  subtitle?: string
  icon?: React.ReactNode
}) {
  return (
    <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-6">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[#8b949e] text-sm">{label}</span>
        {icon}
      </div>
      <div className="text-3xl font-bold text-[#e6edf3] font-mono">{value}</div>
      {subtitle && <div className="text-[#8b949e] text-sm mt-1">{subtitle}</div>}
    </div>
  )
}

function TransactionForm({
  type,
  shareholders,
  person,
  amount,
  note,
  loading,
  onPersonChange,
  onAmountChange,
  onNoteChange,
  onSubmit,
  onCancel,
}: {
  type: 'deposit' | 'withdrawal'
  shareholders: Shareholder[]
  person: string
  amount: string
  note: string
  loading: boolean
  onPersonChange: (v: string) => void
  onAmountChange: (v: string) => void
  onNoteChange: (v: string) => void
  onSubmit: () => void
  onCancel: () => void
}) {
  const isDeposit = type === 'deposit'
  const color = isDeposit ? '#00ff88' : '#ff4757'

  return (
    <div className="bg-[#0d1117] border rounded-lg p-6 mb-6" style={{ borderColor: `${color}40` }}>
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-medium" style={{ color }}>
          {isDeposit ? 'Record Deposit' : 'Record Withdrawal'}
        </h3>
        <button onClick={onCancel} className="text-[#8b949e] hover:text-[#e6edf3]">
          <X size={18} />
        </button>
      </div>
      <div className="flex gap-4">
        <div className="relative flex-1">
          <select
            value={person}
            onChange={(e) => onPersonChange(e.target.value)}
            className="w-full appearance-none bg-[#0a0e14] border border-[#30363d] text-[#e6edf3] rounded-lg pl-3 pr-8 py-2 focus:outline-none focus:border-[#00d4ff]"
          >
            <option value="">Select person...</option>
            {shareholders.map((sh) => (
              <option key={sh.name} value={sh.name}>
                {sh.name}
              </option>
            ))}
          </select>
          <ChevronDown
            size={14}
            className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[#8b949e] pointer-events-none"
          />
        </div>
        <div className="relative">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-[#8b949e]">$</span>
          <input
            type="number"
            placeholder="Amount"
            value={amount}
            onChange={(e) => onAmountChange(e.target.value)}
            className="w-40 bg-[#0a0e14] border border-[#30363d] text-[#e6edf3] rounded-lg pl-7 pr-4 py-2 focus:outline-none focus:border-[#00d4ff]"
          />
        </div>
        <input
          type="text"
          placeholder="Note (optional)"
          value={note}
          onChange={(e) => onNoteChange(e.target.value)}
          className="flex-1 bg-[#0a0e14] border border-[#30363d] text-[#e6edf3] rounded-lg px-4 py-2 focus:outline-none focus:border-[#00d4ff]"
        />
        <button
          onClick={onSubmit}
          disabled={loading || !person || !amount}
          className="px-6 py-2 rounded-lg border disabled:opacity-50 font-medium"
          style={{
            backgroundColor: `${color}15`,
            borderColor: `${color}40`,
            color,
          }}
        >
          {loading ? 'Processing...' : isDeposit ? 'Deposit' : 'Withdraw'}
        </button>
      </div>
    </div>
  )
}

// ---------- Setup Wizard ----------

function SetupWizard({
  wallets,
  onComplete,
  onCancel,
}: {
  wallets: WalletInfo[]
  onComplete: () => void
  onCancel: () => void
}) {
  const [step, setStep] = useState<'wallets' | 'shareholders'>(
    wallets.some((w) => w.ownership_type !== null) ? 'shareholders' : 'wallets'
  )
  const [walletTypes, setWalletTypes] = useState<
    Record<number, { type: string; owner: string }>
  >(() => {
    const m: Record<number, { type: string; owner: string }> = {}
    for (const w of wallets) {
      m[w.id] = {
        type: w.ownership_type || 'pooled',
        owner: w.owner_name || '',
      }
    }
    return m
  })
  const [shareholders, setShareholders] = useState<Array<{ name: string; shares: string }>>([
    { name: '', shares: '' },
  ])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleAddRow = () => {
    setShareholders([...shareholders, { name: '', shares: '' }])
  }

  const handleRemoveRow = (i: number) => {
    setShareholders(shareholders.filter((_, idx) => idx !== i))
  }

  const handleSubmit = async () => {
    setLoading(true)
    setError(null)
    try {
      const walletSpecs = Object.entries(walletTypes).map(([id, spec]) => ({
        wallet_id: parseInt(id),
        ownership_type: spec.type,
        owner_name: spec.type === 'personal' ? spec.owner || null : null,
      }))
      const shareholderSpecs = shareholders
        .filter((s) => s.name.trim())
        .map((s) => ({
          name: s.name.trim(),
          shares: parseFloat(s.shares) || 0,
        }))

      if (shareholderSpecs.length === 0) {
        setError('Add at least one shareholder')
        setLoading(false)
        return
      }

      await api.post('/api/fund/v2/init', {
        wallets: walletSpecs,
        shareholders: shareholderSpecs,
      })
      onComplete()
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex-1 overflow-auto bg-[#0a0e14]">
      <div className="p-8 max-w-[900px] mx-auto">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h2 className="text-2xl font-semibold text-[#e6edf3]">Fund Setup</h2>
            <p className="text-[#8b949e] text-sm mt-1">
              {step === 'wallets'
                ? 'Step 1: Classify your wallets as pooled or personal'
                : 'Step 2: Add shareholders and their share allocation'}
            </p>
          </div>
          <button
            onClick={onCancel}
            className="text-[#8b949e] hover:text-[#e6edf3] text-sm"
          >
            Cancel
          </button>
        </div>

        {error && (
          <div className="bg-[#ff4757]/10 border border-[#ff4757]/30 text-[#ff4757] text-sm rounded-lg p-3 mb-6">
            {error}
          </div>
        )}

        {/* Step indicator */}
        <div className="flex gap-2 mb-8">
          <div
            className={`flex-1 h-1 rounded-full ${
              step === 'wallets' ? 'bg-[#00d4ff]' : 'bg-[#00d4ff]/40'
            }`}
          />
          <div
            className={`flex-1 h-1 rounded-full ${
              step === 'shareholders' ? 'bg-[#00d4ff]' : 'bg-[#30363d]'
            }`}
          />
        </div>

        {step === 'wallets' && (
          <div>
            <div className="bg-[#0d1117] border border-[#30363d] rounded-lg">
              <div className="p-6 pb-2">
                <h3 className="text-[#e6edf3] font-medium mb-1">Wallet Classification</h3>
                <p className="text-[#8b949e] text-xs">
                  Pooled wallets are shared among shareholders. Personal wallets are fully owned by
                  one person.
                </p>
              </div>
              <div className="p-6 pt-4 space-y-3">
                {wallets.map((w) => {
                  const spec = walletTypes[w.id] || { type: 'pooled', owner: '' }
                  return (
                    <div
                      key={w.id}
                      className="flex items-center gap-4 bg-[#0a0e14] border border-[#21262d] rounded-lg p-4"
                    >
                      <div className="flex-1 min-w-0">
                        <div className="text-[#e6edf3] text-sm font-medium truncate">
                          {w.label}
                        </div>
                        <div className="text-[#8b949e] text-xs font-mono truncate">
                          {w.address}
                        </div>
                      </div>
                      <div className="text-[#e6edf3] font-mono text-sm">{formatUSD(w.value)}</div>
                      <div className="relative">
                        <select
                          value={spec.type}
                          onChange={(e) =>
                            setWalletTypes({
                              ...walletTypes,
                              [w.id]: { ...spec, type: e.target.value },
                            })
                          }
                          className="appearance-none bg-[#161b22] border border-[#30363d] text-[#e6edf3] rounded-lg pl-3 pr-8 py-1.5 text-sm focus:outline-none focus:border-[#00d4ff]"
                        >
                          <option value="pooled">Pooled</option>
                          <option value="personal">Personal</option>
                        </select>
                        <ChevronDown
                          size={12}
                          className="absolute right-2 top-1/2 -translate-y-1/2 text-[#8b949e] pointer-events-none"
                        />
                      </div>
                      {spec.type === 'personal' && (
                        <input
                          type="text"
                          placeholder="Owner name"
                          value={spec.owner}
                          onChange={(e) =>
                            setWalletTypes({
                              ...walletTypes,
                              [w.id]: { ...spec, owner: e.target.value },
                            })
                          }
                          className="w-32 bg-[#161b22] border border-[#30363d] text-[#e6edf3] rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:border-[#00d4ff]"
                        />
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
            <div className="flex justify-end mt-6">
              <button
                onClick={() => setStep('shareholders')}
                className="px-6 py-2.5 bg-[#00d4ff]/10 hover:bg-[#00d4ff]/20 text-[#00d4ff] rounded-lg border border-[#00d4ff]/30 font-medium"
              >
                Next: Add Shareholders
              </button>
            </div>
          </div>
        )}

        {step === 'shareholders' && (
          <div>
            <div className="bg-[#0d1117] border border-[#30363d] rounded-lg">
              <div className="p-6 pb-2">
                <h3 className="text-[#e6edf3] font-medium mb-1">Shareholders</h3>
                <p className="text-[#8b949e] text-xs">
                  Set each person's share allocation. Shares represent ownership percentage of the
                  pooled wallets.
                </p>
              </div>
              <div className="p-6 pt-4 space-y-3">
                {shareholders.map((sh, i) => (
                  <div key={i} className="flex items-center gap-3">
                    <input
                      type="text"
                      placeholder="Name"
                      value={sh.name}
                      onChange={(e) => {
                        const next = [...shareholders]
                        next[i] = { ...next[i], name: e.target.value }
                        setShareholders(next)
                      }}
                      className="flex-1 bg-[#0a0e14] border border-[#30363d] text-[#e6edf3] rounded-lg px-4 py-2 focus:outline-none focus:border-[#00d4ff]"
                    />
                    <input
                      type="number"
                      placeholder="Shares"
                      value={sh.shares}
                      onChange={(e) => {
                        const next = [...shareholders]
                        next[i] = { ...next[i], shares: e.target.value }
                        setShareholders(next)
                      }}
                      className="w-32 bg-[#0a0e14] border border-[#30363d] text-[#e6edf3] rounded-lg px-4 py-2 focus:outline-none focus:border-[#00d4ff]"
                    />
                    <button
                      onClick={() => handleRemoveRow(i)}
                      className="text-[#8b949e] hover:text-[#ff4757] p-1"
                      title="Remove"
                    >
                      <X size={16} />
                    </button>
                  </div>
                ))}
                <button
                  onClick={handleAddRow}
                  className="flex items-center gap-2 text-[#00d4ff] text-sm hover:text-[#00d4ff]/80"
                >
                  <Plus size={14} /> Add another person
                </button>
              </div>
            </div>
            <div className="flex justify-between mt-6">
              <button
                onClick={() => setStep('wallets')}
                className="px-6 py-2.5 bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] rounded-lg border border-[#30363d]"
              >
                Back
              </button>
              <button
                onClick={handleSubmit}
                disabled={loading}
                className="px-6 py-2.5 bg-[#00d4ff]/10 hover:bg-[#00d4ff]/20 text-[#00d4ff] rounded-lg border border-[#00d4ff]/30 font-medium disabled:opacity-50"
              >
                {loading ? 'Initializing...' : 'Initialize Fund'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
