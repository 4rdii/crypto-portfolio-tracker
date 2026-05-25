/**
 * Tax Reports page.
 *
 * Two states:
 *   1. LOCKED  — show the $9.99 unlock CTA with a chain/token picker
 *                that lets the user choose any supported token + chain.
 *   2. UNLOCKED — show the year/jurisdiction/method picker, run the
 *                 side-by-side method comparison (the killer feature),
 *                 render the summary + disposal detail table, and offer
 *                 PDF / CSV / Form 8949 downloads.
 *
 * The calculator runs on the backend — this file is pure UI + fetches.
 */
import { useEffect, useMemo, useState } from 'react'
import {
  FileText,
  Lock,
  Download,
  RefreshCw,
  AlertCircle,
  Check,
  TrendingDown,
  TrendingUp,
  Loader2,
} from 'lucide-react'
import {
  api,
  ApiError,
  TaxStatus,
  TaxCompareResponse,
  TaxReportResponse,
  TaxLotSnapshot,
  TaxLotSnapshotsResponse,
  TaxOverridesResponse,
  PayTokenConfig,
} from '../lib/api'
import {
  loadPayTokens,
  ensureEthersLoaded,
  signImportPayment,
} from '../lib/payments'
import { formatUSD } from '../lib/format'

const CURRENT_YEAR = new Date().getFullYear()
// Offer the current tax year + 4 prior years (typical back-filing window).
const YEAR_OPTIONS = [
  CURRENT_YEAR,
  CURRENT_YEAR - 1,
  CURRENT_YEAR - 2,
  CURRENT_YEAR - 3,
  CURRENT_YEAR - 4,
]

export function Tax() {
  const [status, setStatus] = useState<TaxStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Picker state — populated from status once loaded.
  const [year, setYear] = useState<number>(CURRENT_YEAR - 1)
  const [jurisdiction, setJurisdiction] = useState<string>('US')
  const [method, setMethod] = useState<string>('FIFO')

  const [compare, setCompare] = useState<TaxCompareResponse | null>(null)
  const [compareLoading, setCompareLoading] = useState(false)
  const [report, setReport] = useState<TaxReportResponse | null>(null)
  const [reportLoading, setReportLoading] = useState(false)

  // Spec-ID picker state — only populated when method === 'SPEC'
  const [lotSnapshots, setLotSnapshots] = useState<TaxLotSnapshot[]>([])
  const [lotSnapshotsLoading, setLotSnapshotsLoading] = useState(false)
  // Overrides: { sellTxId → { lotId → amount } }, both as strings because
  // JSON object keys are strings and we want no accidental number→string
  // conversions when looking up picks.
  const [overrides, setOverrides] = useState<
    Record<string, Record<string, number>>
  >({})
  // Which sell accordion is expanded (null = none).
  const [expandedSell, setExpandedSell] = useState<number | null>(null)
  // Per-sell save state (so the picker can show a little saving spinner).
  const [savingOverride, setSavingOverride] = useState<string | null>(null)

  // Unlock (payment) state
  const [unlocking, setUnlocking] = useState(false)
  const [unlockMsg, setUnlockMsg] = useState<string | null>(null)
  const [unlockErr, setUnlockErr] = useState<string | null>(null)
  const [payTokens, setPayTokens] = useState<PayTokenConfig | null>(null)
  const [selectedChain, setSelectedChain] = useState<string>('')
  const [selectedToken, setSelectedToken] = useState<string>('')
  const [showPayPicker, setShowPayPicker] = useState(false)

  const loadStatus = async () => {
    try {
      const s = await api.get<TaxStatus>('/api/tax/status')
      setStatus(s)
      if (s.jurisdictions.length > 0) {
        const us = s.jurisdictions.find((j) => j.code === 'US') || s.jurisdictions[0]
        setJurisdiction(us.code)
        setMethod(us.methods[0] || 'FIFO')
      }
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'Failed to load tax status')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadStatus()
  }, [])

  // Whenever the jurisdiction changes, snap method to the first allowed
  // one for that country (so the user isn't left with an invalid picker).
  const currentJuris = useMemo(
    () => status?.jurisdictions.find((j) => j.code === jurisdiction) || null,
    [status, jurisdiction],
  )
  useEffect(() => {
    if (currentJuris && !currentJuris.methods.includes(method)) {
      setMethod(currentJuris.methods[0])
    }
  }, [currentJuris, method])

  // ---------- Unlock flow (pay $9.99 with chain/token choice) ----------

  const handleShowPayPicker = async () => {
    setUnlockErr(null)
    try {
      const tokens = await loadPayTokens()
      setPayTokens(tokens)
      // Default to Base USDC
      const defaultChain =
        tokens.chains.find((c) => c.chain === 'base') ||
        tokens.chains.find((c) => c.chain === 'arbitrum') ||
        tokens.chains[0]
      if (defaultChain) {
        setSelectedChain(defaultChain.chain)
        const defaultToken =
          defaultChain.tokens.find((t) => t.symbol === 'USDC') ||
          defaultChain.tokens[0]
        if (defaultToken) setSelectedToken(defaultToken.symbol)
      }
      setShowPayPicker(true)
    } catch (e: any) {
      setUnlockErr(e?.message || 'Failed to load payment options')
    }
  }

  const handleUnlock = async () => {
    if (!status || !payTokens) return
    const chainConfig = payTokens.chains.find((c) => c.chain === selectedChain)
    if (!chainConfig) return
    const tokenConfig = chainConfig.tokens.find((t) => t.symbol === selectedToken)
    if (!tokenConfig) return

    setUnlocking(true)
    setShowPayPicker(false)
    setUnlockMsg(null)
    setUnlockErr(null)
    try {
      await ensureEthersLoaded()
      setUnlockMsg(`Signing $${status.price_usd.toFixed(2)} ${tokenConfig.symbol} on ${chainConfig.display}…`)
      const signed = await signImportPayment({
        chain: chainConfig.chain,
        chainId: chainConfig.chain_id,
        tokenSymbol: tokenConfig.symbol,
        tokenContract: tokenConfig.contract,
        tokenDecimals: tokenConfig.decimals,
        treasury: payTokens.treasury_address,
        amountUsd: status.price_usd,
      })
      setUnlockMsg(`Payment broadcast (${signed.txHash.slice(0, 10)}…). Verifying on-chain…`)

      let ok = false
      for (let i = 0; i < 24; i++) {
        const resp = await api.post<any>('/api/tax/unlock', {
          chain: signed.paid.chain,
          tx_hash: signed.paid.tx_hash,
          expected_token: signed.paid.expected_token,
        })
        if (resp.ok) {
          ok = true
          setUnlockMsg(resp.message || 'Tax reports unlocked!')
          break
        }
        if (resp.status === 'rejected') {
          throw new Error(resp.reason || 'Payment rejected')
        }
        setUnlockMsg(`${resp.reason || 'Waiting for confirmations'}…`)
        await new Promise((r) => setTimeout(r, 5000))
      }
      if (!ok) throw new Error('Verification timed out. Retry in a few minutes.')

      await loadStatus()
    } catch (e: any) {
      setUnlockErr(e?.message || 'Unlock failed')
      setUnlockMsg(null)
    } finally {
      setUnlocking(false)
    }
  }

  // ---------- Report generation ----------

  const loadCompare = async () => {
    setCompareLoading(true)
    setReport(null)
    try {
      const q = `year=${year}&jurisdiction=${jurisdiction}`
      const r = await api.get<TaxCompareResponse>(`/api/tax/compare?${q}`)
      setCompare(r)
      // If the current picker method is in the results, auto-load its report.
      if (r.results.length > 0 && !r.results.find((x) => x.method === method)) {
        setMethod(r.results[0].method)
      }
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'compare failed')
    } finally {
      setCompareLoading(false)
    }
  }

  const loadReport = async () => {
    setReportLoading(true)
    try {
      const q = `year=${year}&jurisdiction=${jurisdiction}&method=${method}`
      const r = await api.get<TaxReportResponse>(`/api/tax/report?${q}`)
      setReport(r)
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'report failed')
    } finally {
      setReportLoading(false)
    }
  }

  // ---------- Spec-ID lot picker loaders ----------
  //
  // When the user flips the method picker to SPEC (or changes year /
  // jurisdiction while on SPEC) we fetch two things in parallel:
  //   • the open-lot snapshots for every sell in that year
  //   • any previously-saved overrides the user has in the DB
  // The picker panel reads from both.

  const loadLotSnapshots = async () => {
    setLotSnapshotsLoading(true)
    try {
      const q = `year=${year}&jurisdiction=${jurisdiction}`
      const [snaps, ovr] = await Promise.all([
        api.get<TaxLotSnapshotsResponse>(`/api/tax/lot-snapshots?${q}`),
        api.get<TaxOverridesResponse>('/api/tax/overrides'),
      ])
      setLotSnapshots(snaps.snapshots)
      setOverrides(ovr.overrides || {})
      // If only one sell exists, auto-expand it so the user doesn't have
      // to click twice.
      if (snaps.snapshots.length === 1) {
        setExpandedSell(snaps.snapshots[0].sell_tx_id)
      }
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'lot snapshots failed')
    } finally {
      setLotSnapshotsLoading(false)
    }
  }

  useEffect(() => {
    if (status?.unlocked && method === 'SPEC') {
      loadLotSnapshots()
    } else {
      // Clear picker state when leaving SPEC so stale lots don't flash
      // back if the user returns.
      setLotSnapshots([])
      setExpandedSell(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.unlocked, method, year, jurisdiction])

  const saveOverride = async (
    sellTxId: number,
    symbol: string,
    lotId: number,
    amount: number,
  ) => {
    const key = `${sellTxId}:${lotId}`
    setSavingOverride(key)
    try {
      await api.post('/api/tax/overrides', {
        sell_tx_id: sellTxId,
        symbol,
        lot_id: lotId,
        amount,
      })
      // Optimistically update local state so the UI doesn't wait for a
      // full reload (and total-picked math stays snappy as the user
      // types).
      setOverrides((prev) => {
        const next = { ...prev }
        const inner = { ...(next[String(sellTxId)] || {}) }
        if (amount > 0) {
          inner[String(lotId)] = amount
        } else {
          delete inner[String(lotId)]
        }
        if (Object.keys(inner).length === 0) {
          delete next[String(sellTxId)]
        } else {
          next[String(sellTxId)] = inner
        }
        return next
      })
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'save failed')
    } finally {
      setSavingOverride(null)
    }
  }

  const clearOverridesForSell = async (sellTxId: number) => {
    try {
      await api.delete(`/api/tax/overrides/${sellTxId}`)
      setOverrides((prev) => {
        const next = { ...prev }
        delete next[String(sellTxId)]
        return next
      })
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'clear failed')
    }
  }

  const downloadFile = (path: string, filename: string) => {
    // Open in a new tab so the browser handles the binary stream.
    // We append year/jurisdiction/method as query args.
    const q = `year=${year}&jurisdiction=${jurisdiction}&method=${method}`
    const url = `${path}?${q}`
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
  }

  // ---------- Render ----------

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#0a0e14] text-[#8b949e]">
        Loading tax status…
      </div>
    )
  }

  if (error && !status) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#0a0e14] p-8">
        <div className="max-w-md text-center">
          <p className="text-[#ff4757] mb-4">{error}</p>
          <button
            onClick={loadStatus}
            className="px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] rounded-lg border border-[#30363d]"
          >
            Retry
          </button>
        </div>
      </div>
    )
  }

  if (!status) return null

  // ---------- Locked state: show unlock CTA ----------
  if (!status.unlocked) {
    return (
      <div className="flex-1 overflow-auto bg-[#0a0e14]">
        <div className="p-8 max-w-[900px] mx-auto">
          <div className="flex items-center gap-3 mb-2">
            <FileText className="text-[#00d4ff]" size={24} />
            <h2 className="text-2xl font-semibold text-[#e6edf3]">Tax Reports</h2>
          </div>
          <p className="text-[#8b949e] text-sm mb-8">
            Generate country-specific capital gains reports with your choice
            of cost basis method. One-time unlock — no subscription.
          </p>

          <div className="bg-gradient-to-br from-[#00d4ff]/10 to-[#0088cc]/5 border border-[#00d4ff]/30 rounded-2xl p-8 mb-6">
            <div className="flex items-start gap-4 mb-6">
              <div className="p-3 rounded-xl bg-[#00d4ff]/20">
                <Lock className="text-[#00d4ff]" size={24} />
              </div>
              <div>
                <h3 className="text-xl text-[#e6edf3] font-semibold mb-1">
                  Unlock tax reports — ${status.price_usd.toFixed(2)} once
                </h3>
                <p className="text-[#8b949e] text-sm">
                  Pay once with your choice of token and chain. Unlocks every
                  year, jurisdiction, and cost basis method — forever.
                </p>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3 mb-6">
              <div className="flex items-center gap-2 text-sm text-[#e6edf3]">
                <Check size={16} className="text-[#00d4ff] flex-shrink-0" />
                <span>8 jurisdictions (US, UK, Singapore, Germany, Canada, Australia, Japan, India)</span>
              </div>
              <div className="flex items-center gap-2 text-sm text-[#e6edf3]">
                <Check size={16} className="text-[#00d4ff] flex-shrink-0" />
                <span>5 cost basis methods (FIFO, LIFO, HIFO, Average, Spec-ID)</span>
              </div>
              <div className="flex items-center gap-2 text-sm text-[#e6edf3]">
                <Check size={16} className="text-[#00d4ff] flex-shrink-0" />
                <span>Side-by-side method comparison</span>
              </div>
              <div className="flex items-center gap-2 text-sm text-[#e6edf3]">
                <Check size={16} className="text-[#00d4ff] flex-shrink-0" />
                <span>PDF + CSV + Form 8949 exports</span>
              </div>
              <div className="flex items-center gap-2 text-sm text-[#e6edf3]">
                <Check size={16} className="text-[#00d4ff] flex-shrink-0" />
                <span>All past &amp; future tax years</span>
              </div>
              <div className="flex items-center gap-2 text-sm text-[#e6edf3]">
                <Check size={16} className="text-[#00d4ff] flex-shrink-0" />
                <span>No subscription, no limits</span>
              </div>
            </div>

            {!showPayPicker ? (
              <button
                onClick={handleShowPayPicker}
                disabled={unlocking}
                className="w-full flex items-center justify-center gap-2 px-6 py-3 bg-[#00d4ff] hover:bg-[#00b8e0] disabled:opacity-50 disabled:cursor-not-allowed text-[#0a0e14] font-semibold rounded-xl transition-colors"
              >
                {unlocking ? (
                  <>
                    <Loader2 size={18} className="animate-spin" />
                    <span>Processing…</span>
                  </>
                ) : (
                  <>
                    <Lock size={18} />
                    <span>Pay ${status.price_usd.toFixed(2)} to unlock</span>
                  </>
                )}
              </button>
            ) : payTokens ? (
              <div className="space-y-3 bg-[#1c2128] rounded-xl p-4">
                <div>
                  <label className="text-xs text-[#8b949e] mb-1 block">Chain</label>
                  <select
                    value={selectedChain}
                    onChange={(e) => {
                      setSelectedChain(e.target.value)
                      const chain = payTokens.chains.find((c) => c.chain === e.target.value)
                      if (chain) {
                        const usdc = chain.tokens.find((t) => t.symbol === 'USDC')
                        setSelectedToken(usdc ? usdc.symbol : chain.tokens[0]?.symbol || '')
                      }
                    }}
                    className="w-full bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-2 text-[#e6edf3] text-sm"
                  >
                    {payTokens.chains.map((c) => (
                      <option key={c.chain} value={c.chain}>{c.display}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-[#8b949e] mb-1 block">Token</label>
                  <select
                    value={selectedToken}
                    onChange={(e) => setSelectedToken(e.target.value)}
                    className="w-full bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-2 text-[#e6edf3] text-sm"
                  >
                    {payTokens.chains
                      .find((c) => c.chain === selectedChain)
                      ?.tokens.map((t) => (
                        <option key={t.symbol} value={t.symbol}>
                          {t.symbol}{(t as any).stable ? '' : ' (market price)'}
                        </option>
                      ))}
                  </select>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => setShowPayPicker(false)}
                    className="flex-1 px-4 py-2 bg-[#0d1117] border border-[#30363d] text-[#8b949e] rounded-lg text-sm hover:bg-[#21262d] transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleUnlock}
                    className="flex-1 px-4 py-2 bg-[#00d4ff] hover:bg-[#00b8e0] text-[#0a0e14] font-semibold rounded-lg text-sm transition-colors"
                  >
                    Pay ${status.price_usd.toFixed(2)} {selectedToken}
                  </button>
                </div>
              </div>
            ) : null}

            {unlockMsg && (
              <div className="mt-4 px-4 py-3 rounded-lg bg-[#1c2128] text-[#8b949e] text-sm flex items-center gap-2">
                <Loader2 size={14} className="animate-spin flex-shrink-0" />
                <span>{unlockMsg}</span>
              </div>
            )}
            {unlockErr && (
              <div className="mt-4 px-4 py-3 rounded-lg bg-[#ff4757]/10 border border-[#ff4757]/30 text-[#ff4757] text-sm flex items-start gap-2">
                <AlertCircle size={14} className="flex-shrink-0 mt-0.5" />
                <span>{unlockErr}</span>
              </div>
            )}
          </div>

          {/* Preview of what's supported */}
          <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-6">
            <h3 className="text-[#e6edf3] font-semibold mb-4">
              Supported jurisdictions
            </h3>
            <div className="grid grid-cols-2 gap-3">
              {status.jurisdictions.map((j) => (
                <div
                  key={j.code}
                  className="flex items-center justify-between px-4 py-3 rounded-lg bg-[#1c2128] border border-[#30363d]"
                >
                  <div>
                    <div className="text-[#e6edf3] text-sm font-medium">{j.name}</div>
                    <div className="text-[#8b949e] text-xs mt-0.5">
                      Year-end: {j.tax_year_end}
                    </div>
                  </div>
                  <div className="text-[#8b949e] text-xs">
                    {j.methods.join(' · ')}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    )
  }

  // ---------- Unlocked state: report generator ----------
  return (
    <div className="flex-1 overflow-auto bg-[#0a0e14]">
      <div className="p-8 max-w-[1400px]">
        <div className="flex items-center justify-between mb-8">
          <div>
            <div className="flex items-center gap-3">
              <FileText className="text-[#00d4ff]" size={24} />
              <h2 className="text-2xl font-semibold text-[#e6edf3]">Tax Reports</h2>
              <span className="px-2 py-0.5 rounded bg-[#00d4ff]/10 text-[#00d4ff] text-xs font-medium">
                UNLOCKED
              </span>
            </div>
            <p className="text-[#8b949e] text-sm mt-1">
              Generate capital gains reports for any supported jurisdiction.
            </p>
          </div>
        </div>

        {/* Picker bar */}
        <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-5 mb-6">
          <div className="grid grid-cols-4 gap-4">
            <div>
              <label className="block text-xs text-[#8b949e] uppercase mb-2 tracking-wide">
                Tax year
              </label>
              <select
                value={year}
                onChange={(e) => setYear(Number(e.target.value))}
                className="w-full px-3 py-2 bg-[#1c2128] border border-[#30363d] text-[#e6edf3] rounded-lg"
              >
                {YEAR_OPTIONS.map((y) => (
                  <option key={y} value={y}>{y}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs text-[#8b949e] uppercase mb-2 tracking-wide">
                Jurisdiction
              </label>
              <select
                value={jurisdiction}
                onChange={(e) => setJurisdiction(e.target.value)}
                className="w-full px-3 py-2 bg-[#1c2128] border border-[#30363d] text-[#e6edf3] rounded-lg"
              >
                {status.jurisdictions.map((j) => (
                  <option key={j.code} value={j.code}>
                    {j.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs text-[#8b949e] uppercase mb-2 tracking-wide">
                Cost basis method
              </label>
              <select
                value={method}
                onChange={(e) => setMethod(e.target.value)}
                className="w-full px-3 py-2 bg-[#1c2128] border border-[#30363d] text-[#e6edf3] rounded-lg"
              >
                {(currentJuris?.methods || []).map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </div>
            <div className="flex items-end gap-2">
              <button
                onClick={loadCompare}
                disabled={compareLoading}
                className="flex-1 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-50 text-[#e6edf3] border border-[#30363d] rounded-lg transition-colors flex items-center justify-center gap-2"
              >
                {compareLoading ? <Loader2 size={16} className="animate-spin" /> : <RefreshCw size={16} />}
                <span>Compare</span>
              </button>
              <button
                onClick={loadReport}
                disabled={reportLoading}
                className="flex-1 px-4 py-2 bg-[#00d4ff] hover:bg-[#00b8e0] disabled:opacity-50 text-[#0a0e14] font-semibold rounded-lg transition-colors flex items-center justify-center gap-2"
              >
                {reportLoading ? <Loader2 size={16} className="animate-spin" /> : <FileText size={16} />}
                <span>Generate</span>
              </button>
            </div>
          </div>
        </div>

        {/* Method comparison table */}
        {compare && compare.results.length > 0 && (
          <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-5 mb-6">
            <h3 className="text-[#e6edf3] font-semibold mb-3 flex items-center gap-2">
              <TrendingDown size={16} className="text-[#00d4ff]" />
              Side-by-side comparison ({compare.year} · {compare.jurisdiction})
            </h3>
            <p className="text-[#8b949e] text-xs mb-4">
              Cheapest taxable method highlighted — that's the winner for
              this year. Click a row to load the full report.
            </p>
            <table className="w-full text-sm">
              <thead className="text-xs text-[#8b949e] uppercase border-b border-[#30363d]">
                <tr>
                  <th className="text-left py-2">Method</th>
                  <th className="text-right py-2">Short-term</th>
                  <th className="text-right py-2">Long-term</th>
                  <th className="text-right py-2">Total gain</th>
                  <th className="text-right py-2">Taxable</th>
                  <th className="text-right py-2">Disposals</th>
                </tr>
              </thead>
              <tbody>
                {compare.results.map((r, i) => {
                  const isBest = i === 0
                  const isSelected = r.method === method
                  return (
                    <tr
                      key={r.method}
                      onClick={() => setMethod(r.method)}
                      className={`border-b border-[#21262d] cursor-pointer transition-colors ${
                        isSelected
                          ? 'bg-[#00d4ff]/10'
                          : 'hover:bg-[#1c2128]'
                      }`}
                    >
                      <td className="py-2 text-[#e6edf3] font-medium">
                        <div className="flex items-center gap-2">
                          {r.method}
                          {isBest && (
                            <span className="px-1.5 py-0.5 rounded bg-[#00d4ff]/10 text-[#00d4ff] text-[10px] font-semibold">
                              BEST
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="py-2 text-right text-[#8b949e]">
                        {formatUSD(r.short_term_gain ?? 0)}
                      </td>
                      <td className="py-2 text-right text-[#8b949e]">
                        {formatUSD(r.long_term_gain ?? 0)}
                      </td>
                      <td className={`py-2 text-right ${
                        (r.total_gain ?? 0) < 0 ? 'text-[#ff4757]' : 'text-[#e6edf3]'
                      }`}>
                        {formatUSD(r.total_gain ?? 0)}
                      </td>
                      <td className={`py-2 text-right font-semibold ${
                        isBest ? 'text-[#00d4ff]' : 'text-[#e6edf3]'
                      }`}>
                        {formatUSD(r.taxable_gain ?? 0)}
                      </td>
                      <td className="py-2 text-right text-[#8b949e]">
                        {r.disposal_count ?? 0}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {compare.results.length > 1 && (
              <div className="mt-3 text-xs text-[#8b949e]">
                Savings vs worst: <span className="text-[#00d4ff] font-semibold">
                  {formatUSD(
                    (compare.results[compare.results.length - 1].taxable_gain ?? 0) -
                    (compare.results[0].taxable_gain ?? 0),
                  )}
                </span>
              </div>
            )}
          </div>
        )}

        {/* Spec-ID lot picker panel — only shown when SPEC is selected */}
        {method === 'SPEC' && (
          <SpecIdPicker
            loading={lotSnapshotsLoading}
            snapshots={lotSnapshots}
            overrides={overrides}
            expandedSell={expandedSell}
            setExpandedSell={setExpandedSell}
            savingOverride={savingOverride}
            onSave={saveOverride}
            onClear={clearOverridesForSell}
            onRefresh={loadLotSnapshots}
          />
        )}

        {/* Full report view */}
        {report && (
          <>
            {/* Summary card grid */}
            <div className="grid grid-cols-4 gap-4 mb-6">
              <SummaryStat
                label="Short-term gain"
                usd={report.short_term_gain}
                local={report.short_term_gain_local}
                currency={report.currency}
              />
              <SummaryStat
                label="Long-term gain"
                usd={report.long_term_gain}
                local={report.long_term_gain_local}
                currency={report.currency}
              />
              <SummaryStat
                label="Net gain / loss"
                usd={report.total_gain}
                local={report.total_gain_local}
                currency={report.currency}
              />
              <SummaryStat
                label="Taxable (after discount)"
                usd={report.taxable_gain}
                local={report.taxable_gain_local}
                currency={report.currency}
                highlight
              />
            </div>

            {/* Download bar */}
            <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-5 mb-6 flex items-center justify-between">
              <div>
                <div className="text-[#e6edf3] font-medium">
                  {report.jurisdiction_name} · Tax Year {report.tax_year} · {report.method}
                </div>
                <div className="text-[#8b949e] text-xs mt-1">
                  {report.disposal_count} disposals · {report.asset_count} assets ·
                  Generated {report.generated_at.replace('T', ' ').slice(0, 19)} UTC
                </div>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => downloadFile('/api/tax/report/pdf', `tax_${report.tax_year}_${report.method}.pdf`)}
                  className="flex items-center gap-2 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] rounded-lg border border-[#30363d] transition-colors"
                >
                  <Download size={14} />
                  <span>PDF</span>
                </button>
                <button
                  onClick={() => downloadFile('/api/tax/report/csv', `tax_${report.tax_year}_${report.method}.csv`)}
                  className="flex items-center gap-2 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] rounded-lg border border-[#30363d] transition-colors"
                >
                  <Download size={14} />
                  <span>CSV</span>
                </button>
                {report.jurisdiction_code === 'US' && (
                  <button
                    onClick={() => downloadFile('/api/tax/report/8949', `form_8949_${report.tax_year}.csv`)}
                    className="flex items-center gap-2 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] rounded-lg border border-[#30363d] transition-colors"
                  >
                    <Download size={14} />
                    <span>Form 8949</span>
                  </button>
                )}
              </div>
            </div>

            {/* Caveats */}
            {report.caveats.length > 0 && (
              <div className="bg-[#e6b800]/5 border border-[#e6b800]/20 rounded-xl p-5 mb-6">
                <div className="flex items-start gap-3 mb-3">
                  <AlertCircle size={16} className="text-[#e6b800] flex-shrink-0 mt-0.5" />
                  <h3 className="text-[#e6edf3] font-semibold text-sm">
                    Jurisdiction notes — {report.jurisdiction_name}
                  </h3>
                </div>
                <ul className="space-y-2 text-[#8b949e] text-sm pl-7">
                  {report.caveats.map((c, i) => (
                    <li key={i} className="list-disc">{c}</li>
                  ))}
                </ul>
              </div>
            )}

            {/* Disposal detail table */}
            {report.disposals.length > 0 ? (
              <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-5">
                <h3 className="text-[#e6edf3] font-semibold mb-3">
                  Disposal detail ({report.disposal_count})
                </h3>
                <div className="overflow-auto max-h-[600px]">
                  <table className="w-full text-sm">
                    <thead className="text-xs text-[#8b949e] uppercase border-b border-[#30363d] sticky top-0 bg-[#0d1117]">
                      <tr>
                        <th className="text-left py-2">Asset</th>
                        <th className="text-right py-2">Amount</th>
                        <th className="text-left py-2">Acquired</th>
                        <th className="text-left py-2">Disposed</th>
                        <th className="text-right py-2">Held</th>
                        <th className="text-left py-2">Term</th>
                        <th className="text-right py-2">Cost basis</th>
                        <th className="text-right py-2">Proceeds</th>
                        <th className="text-right py-2">Gain / loss</th>
                      </tr>
                    </thead>
                    <tbody>
                      {report.disposals.map((d, i) => (
                        <tr key={i} className="border-b border-[#21262d] hover:bg-[#1c2128]">
                          <td className="py-2 text-[#e6edf3] font-medium">{d.symbol}</td>
                          <td className="py-2 text-right text-[#8b949e] font-mono">
                            {d.amount.toFixed(6).replace(/\.?0+$/, '')}
                          </td>
                          <td className="py-2 text-[#8b949e]">{d.acquired_ts.slice(0, 10)}</td>
                          <td className="py-2 text-[#8b949e]">{d.disposed_ts.slice(0, 10)}</td>
                          <td className="py-2 text-right text-[#8b949e]">{d.holding_days}d</td>
                          <td className="py-2">
                            <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                              d.long_term
                                ? 'bg-[#00d4ff]/10 text-[#00d4ff]'
                                : 'bg-[#e6b800]/10 text-[#e6b800]'
                            }`}>
                              {d.long_term ? 'LT' : 'ST'}
                            </span>
                          </td>
                          <td className="py-2 text-right text-[#8b949e]">{formatUSD(d.cost_basis)}</td>
                          <td className="py-2 text-right text-[#8b949e]">{formatUSD(d.proceeds)}</td>
                          <td className={`py-2 text-right font-medium ${
                            d.gain < 0 ? 'text-[#ff4757]' : 'text-[#00d4ff]'
                          }`}>
                            {formatUSD(d.gain)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : (
              <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-8 text-center">
                <p className="text-[#8b949e]">
                  No disposals in this tax year. Try a different year or
                  jurisdiction.
                </p>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

// Map ISO code → Intl locale hint so Intl.NumberFormat can render
// "£1,234.56" / "€1.234,56" / "¥123,456" etc. natively. Unknown codes
// fall back to en-US which just shows the 3-letter code.
const LOCALE_BY_CCY: Record<string, string> = {
  USD: 'en-US', GBP: 'en-GB', EUR: 'de-DE', JPY: 'ja-JP',
  CAD: 'en-CA', AUD: 'en-AU', SGD: 'en-SG', INR: 'en-IN',
}

function fmtLocal(amount: number, currency: string): string {
  try {
    return new Intl.NumberFormat(LOCALE_BY_CCY[currency] || 'en-US', {
      style: 'currency',
      currency,
      maximumFractionDigits: 2,
    }).format(amount)
  } catch {
    return `${currency} ${amount.toFixed(2)}`
  }
}

// ---------------------------------------------------------------------------
// Spec-ID lot picker
// ---------------------------------------------------------------------------
// One accordion per sell. Each expanded row shows the open lots at that
// moment with a number input per lot. A header strip shows how much of
// the sale has been "accounted for" vs the sale amount — turns green
// when the picks balance, amber when they don't.
//
// Picks auto-save on blur (not every keystroke — the rate limiter would
// cry). The Reset button fires a DELETE that wipes all picks for the
// sale so the engine falls back to FIFO for that one.

function SpecIdPicker({
  loading,
  snapshots,
  overrides,
  expandedSell,
  setExpandedSell,
  savingOverride,
  onSave,
  onClear,
  onRefresh,
}: {
  loading: boolean
  snapshots: TaxLotSnapshot[]
  overrides: Record<string, Record<string, number>>
  expandedSell: number | null
  setExpandedSell: (v: number | null) => void
  savingOverride: string | null
  onSave: (sellTxId: number, symbol: string, lotId: number, amount: number) => void
  onClear: (sellTxId: number) => void
  onRefresh: () => void
}) {
  if (loading) {
    return (
      <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-8 mb-6 flex items-center justify-center gap-3 text-[#8b949e]">
        <Loader2 size={16} className="animate-spin" />
        <span>Loading lot snapshots…</span>
      </div>
    )
  }
  if (snapshots.length === 0) {
    return (
      <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-8 mb-6 text-center text-[#8b949e]">
        <p className="mb-2">No sells in this tax year to pick lots for.</p>
        <p className="text-xs">
          Spec-ID only matters when you have disposals. Try a different year.
        </p>
      </div>
    )
  }
  return (
    <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-5 mb-6">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-[#e6edf3] font-semibold flex items-center gap-2">
          <FileText size={16} className="text-[#00d4ff]" />
          Spec-ID lot picker
          <span className="text-[#8b949e] text-xs font-normal">
            ({snapshots.length} {snapshots.length === 1 ? 'sale' : 'sales'})
          </span>
        </h3>
        <button
          onClick={onRefresh}
          className="flex items-center gap-2 px-3 py-1.5 text-xs bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] rounded-lg border border-[#30363d]"
        >
          <RefreshCw size={12} />
          <span>Refresh</span>
        </button>
      </div>
      <p className="text-[#8b949e] text-xs mb-4">
        Pick exactly which lots each sale consumes. Click a sale to expand,
        type how much of each lot to spend (in units of the asset), and the
        numbers auto-save. Any unpicked amount falls back to FIFO when you
        hit Generate.
      </p>
      <div className="space-y-2">
        {snapshots.map((s) => {
          const picks = overrides[String(s.sell_tx_id)] || {}
          const totalPicked = Object.values(picks).reduce((a, b) => a + b, 0)
          const remaining = s.amount - totalPicked
          const balanced = Math.abs(remaining) < 1e-8
          const expanded = expandedSell === s.sell_tx_id
          return (
            <div
              key={s.sell_tx_id}
              className="border border-[#30363d] rounded-lg bg-[#0a0e14]"
            >
              <button
                onClick={() => setExpandedSell(expanded ? null : s.sell_tx_id)}
                className="w-full px-4 py-3 flex items-center justify-between hover:bg-[#1c2128] transition-colors rounded-lg"
              >
                <div className="flex items-center gap-4">
                  <span className="text-[#e6edf3] font-medium">
                    {s.symbol}
                  </span>
                  <span className="text-[#8b949e] text-xs font-mono">
                    {s.amount.toFixed(6).replace(/\.?0+$/, '')} units
                  </span>
                  <span className="text-[#8b949e] text-xs">
                    {s.disposed_ts.slice(0, 10)}
                  </span>
                  <span className="text-[#8b949e] text-xs">
                    · proceeds {formatUSD(s.proceeds_usd)}
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <span
                    className={`px-2 py-0.5 rounded text-[10px] font-semibold ${
                      totalPicked === 0
                        ? 'bg-[#8b949e]/10 text-[#8b949e]'
                        : balanced
                        ? 'bg-[#00d4ff]/10 text-[#00d4ff]'
                        : 'bg-[#e6b800]/10 text-[#e6b800]'
                    }`}
                  >
                    {totalPicked === 0
                      ? 'FIFO FALLBACK'
                      : balanced
                      ? 'BALANCED'
                      : `${remaining.toFixed(6).replace(/\.?0+$/, '')} LEFT`}
                  </span>
                  <span className="text-[#8b949e] text-xs">
                    {expanded ? '▾' : '▸'}
                  </span>
                </div>
              </button>
              {expanded && (
                <div className="border-t border-[#21262d] p-4">
                  {s.open_lots.length === 0 ? (
                    <p className="text-[#8b949e] text-sm text-center py-2">
                      No open lots at the time of this sale.
                    </p>
                  ) : (
                    <>
                      <table className="w-full text-sm">
                        <thead className="text-[10px] text-[#8b949e] uppercase">
                          <tr>
                            <th className="text-left py-1">Lot #</th>
                            <th className="text-left py-1">Acquired</th>
                            <th className="text-right py-1">Available</th>
                            <th className="text-right py-1">Cost/unit</th>
                            <th className="text-right py-1 w-32">Pick (units)</th>
                          </tr>
                        </thead>
                        <tbody>
                          {s.open_lots.map((lot) => {
                            const currentPick =
                              picks[String(lot.lot_id)] ?? 0
                            const key = `${s.sell_tx_id}:${lot.lot_id}`
                            const saving = savingOverride === key
                            return (
                              <tr
                                key={lot.lot_id}
                                className="border-t border-[#21262d]"
                              >
                                <td className="py-2 text-[#e6edf3] font-mono">
                                  #{lot.lot_id}
                                </td>
                                <td className="py-2 text-[#8b949e]">
                                  {lot.acquired_ts.slice(0, 10)}
                                </td>
                                <td className="py-2 text-right text-[#8b949e] font-mono">
                                  {lot.remaining.toFixed(6).replace(/\.?0+$/, '')}
                                </td>
                                <td className="py-2 text-right text-[#8b949e]">
                                  {formatUSD(lot.cost_per_unit)}
                                </td>
                                <td className="py-2 text-right">
                                  <div className="flex items-center justify-end gap-2">
                                    {saving && (
                                      <Loader2
                                        size={12}
                                        className="animate-spin text-[#00d4ff]"
                                      />
                                    )}
                                    <input
                                      type="number"
                                      min="0"
                                      max={lot.remaining}
                                      step="any"
                                      defaultValue={currentPick || ''}
                                      placeholder="0"
                                      onBlur={(e) => {
                                        const v = parseFloat(e.target.value)
                                        const amt =
                                          Number.isFinite(v) && v > 0 ? v : 0
                                        if (amt !== currentPick) {
                                          onSave(
                                            s.sell_tx_id,
                                            s.symbol,
                                            lot.lot_id,
                                            amt,
                                          )
                                        }
                                      }}
                                      className="w-28 px-2 py-1 bg-[#1c2128] border border-[#30363d] text-[#e6edf3] rounded text-right font-mono text-xs focus:outline-none focus:border-[#00d4ff]"
                                    />
                                  </div>
                                </td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                      <div className="flex items-center justify-between mt-3 pt-3 border-t border-[#21262d]">
                        <div className="text-xs text-[#8b949e]">
                          Picked: <span className="text-[#e6edf3] font-mono">
                            {totalPicked.toFixed(6).replace(/\.?0+$/, '')}
                          </span>{' '}
                          of{' '}
                          <span className="text-[#e6edf3] font-mono">
                            {s.amount.toFixed(6).replace(/\.?0+$/, '')}
                          </span>
                        </div>
                        <button
                          onClick={() => onClear(s.sell_tx_id)}
                          disabled={Object.keys(picks).length === 0}
                          className="px-3 py-1 text-xs bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-30 disabled:cursor-not-allowed text-[#e6edf3] rounded border border-[#30363d]"
                        >
                          Reset to FIFO
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function SummaryStat({
  label,
  usd,
  local,
  currency,
  highlight = false,
}: {
  label: string
  usd: number
  local: number
  currency: string
  highlight?: boolean
}) {
  const isNeg = usd < 0
  const showLocal = currency && currency !== 'USD'
  // When the filing currency isn't USD the local amount is the
  // headline (the number that goes on the actual form) and USD drops
  // to a secondary line.
  const headline = showLocal ? fmtLocal(local, currency) : formatUSD(usd)
  const sub = showLocal ? formatUSD(usd) : null
  return (
    <div
      className={`rounded-xl p-5 border ${
        highlight
          ? 'bg-[#00d4ff]/5 border-[#00d4ff]/30'
          : 'bg-[#0d1117] border-[#30363d]'
      }`}
    >
      <div className="text-xs text-[#8b949e] uppercase tracking-wide mb-2">
        {label}
      </div>
      <div className="flex items-center gap-2">
        <div className={`text-2xl font-semibold ${
          highlight ? 'text-[#00d4ff]' : isNeg ? 'text-[#ff4757]' : 'text-[#e6edf3]'
        }`}>
          {headline}
        </div>
        {isNeg ? (
          <TrendingDown size={16} className="text-[#ff4757]" />
        ) : usd > 0 ? (
          <TrendingUp size={16} className="text-[#00d4ff]" />
        ) : null}
      </div>
      {sub && (
        <div className="text-xs text-[#8b949e] mt-1 font-mono">{sub}</div>
      )}
    </div>
  )
}
