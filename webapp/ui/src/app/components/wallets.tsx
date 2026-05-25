import { useEffect, useState } from 'react'
import {
  Plus,
  RefreshCw,
  Trash2,
  Download,
  Copy,
  Check,
  X,
  ExternalLink,
  Layers,
} from 'lucide-react'
import {
  api,
  WalletRow,
  ApiError,
  ImportQuote,
  BatchImportQuote,
  PayTokenConfig,
} from '../lib/api'
import { truncateAddress, formatDate, chainColor, formatUSD } from '../lib/format'
import { linkEvmWithSigner, linkSolanaWithProvider } from '../lib/auth'
import { useDynamicContext } from '@dynamic-labs/sdk-react-core'
import { getSigner } from '@dynamic-labs/ethers-v6'
import { loadPayTokens, signImportPayment, ensureEthersLoaded } from '../lib/payments'

type Busy = Record<number, 'scan' | 'delete' | 'import' | null>

type PayStatus =
  | 'idle'        // awaiting user click
  | 'loading'     // fetching quote or pay tokens
  | 'signing'     // prompting wallet signature
  | 'confirming'  // tx broadcast, waiting for on-chain confirmation
  | 'verifying'   // backend verifying + running import
  | 'done'
  | 'error'

export function Wallets() {
  const [wallets, setWallets] = useState<WalletRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<Busy>({})
  const [showAdd, setShowAdd] = useState(false)
  const [addKind, setAddKind] = useState<'evm' | 'solana'>('evm')
  const [addMode, setAddMode] = useState<'sign' | 'watch'>('sign')
  const [addLabel, setAddLabel] = useState('')
  const [addAddress, setAddAddress] = useState('')
  const [addBusy, setAddBusy] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [copied, setCopied] = useState<number | null>(null)
  const { setShowAuthFlow, primaryWallet } = useDynamicContext()

  // Single-wallet import modal
  const [importWallet, setImportWallet] = useState<WalletRow | null>(null)
  // Batch import modal
  const [showBatch, setShowBatch] = useState(false)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.get<WalletRow[]>('/api/wallets')
      setWallets(res || [])
    } catch (e: any) {
      setError(e instanceof ApiError ? e.detail : e.message || 'Failed to load wallets')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const setWalletBusy = (id: number, kind: 'scan' | 'delete' | 'import' | null) => {
    setBusy((prev) => ({ ...prev, [id]: kind }))
  }

  const handleScan = async (w: WalletRow) => {
    setWalletBusy(w.id, 'scan')
    try {
      await api.post(`/api/wallets/${w.id}/scan`)
      await load()
    } catch (e: any) {
      alert(e instanceof ApiError ? e.detail : e.message || 'Scan failed')
    } finally {
      setWalletBusy(w.id, null)
    }
  }

  const handleDelete = async (w: WalletRow) => {
    if (!confirm(`Delete wallet "${w.label}"? This removes its holdings and transactions.`)) return
    setWalletBusy(w.id, 'delete')
    try {
      await api.delete(`/api/wallets/${w.id}`)
      await load()
    } catch (e: any) {
      alert(e instanceof ApiError ? e.detail : e.message || 'Delete failed')
    } finally {
      setWalletBusy(w.id, null)
    }
  }

  const handleAdd = async () => {
    setAddBusy(true)
    setAddError(null)
    try {
      if (addMode === 'watch') {
        // Watch-only: no signature, just save address
        const addr = addAddress.trim()
        if (!addr) throw new Error('Please enter an address')
        await api.post('/api/wallets/watch', {
          address: addr,
          label: addLabel || 'Wallet',
          chain_type: addKind,
        })
        setShowAdd(false)
        setAddLabel('')
        setAddAddress('')
        await load()
        return
      }

      // Signature-based (verified) path
      if (!primaryWallet) {
        setShowAuthFlow?.(true)
        setAddBusy(false)
        return
      }
      const connector = primaryWallet.connector
      const chain = connector?.connectedChain
      if (addKind === 'solana' || chain === 'SOL' || primaryWallet.chain === 'SOL') {
        // Only use Dynamic's connector signer when the primary wallet is actually Solana.
        // If the primary wallet is EVM, connector.getSigner() returns a viem WalletClient
        // whose signMessage() expects { account, message } — not a Uint8Array — and will
        // crash with "Cannot read properties of undefined (reading 'raw')".
        const isSolPrimary = primaryWallet.chain === 'SOL' || chain === 'SOL'
        let solProvider: any
        let address: string | undefined

        if (isSolPrimary) {
          solProvider = await primaryWallet.connector?.getSigner()
          address = primaryWallet.address
        } else {
          // Primary wallet is EVM but user wants to add a Solana wallet — use window.solana
          solProvider = (window as any).solana
          if (!solProvider) throw new Error('No Solana wallet detected. Please install Phantom (or another Solana wallet) and refresh.')
          if (!solProvider.isConnected) await solProvider.connect()
          address = solProvider.publicKey?.toString()
        }

        if (!address) throw new Error('No Solana address')
        await linkSolanaWithProvider(solProvider, address, addLabel || 'Wallet')
      } else {
        let signer: any
        try {
          signer = await getSigner(primaryWallet)
        } catch {
          const walletClient = await primaryWallet.connector?.getSigner()
          if (walletClient) {
            const { BrowserProvider } = await import('ethers')
            const provider = new BrowserProvider(walletClient as any)
            signer = await provider.getSigner()
          } else {
            throw new Error('Could not get wallet signer — please try again')
          }
        }
        await linkEvmWithSigner(signer, addLabel || 'Wallet')
      }
      setShowAdd(false)
      setAddLabel('')
      setAddAddress('')
      await load()
    } catch (e: any) {
      setAddError(e?.message || 'Link failed')
    } finally {
      setAddBusy(false)
    }
  }

  const copyAddress = async (w: WalletRow) => {
    try {
      await navigator.clipboard.writeText(w.address)
      setCopied(w.id)
      setTimeout(() => setCopied(null), 1200)
    } catch {
      /* ignore */
    }
  }

  const hasEvm = wallets.some((w) => w.chain === 'evm')

  if (loading && wallets.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#0a0e14] text-[#8b949e]">
        Loading wallets…
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-auto bg-[#0a0e14]">
      <div className="p-8 max-w-[1400px]">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h2 className="text-2xl font-semibold text-[#e6edf3]">Wallets</h2>
            <p className="text-[#8b949e] text-sm mt-1">
              {wallets.length} connected · scan balances or import full history
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
            {hasEvm && (
              <button
                onClick={() => setShowBatch(true)}
                className="flex items-center gap-2 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] rounded-lg transition-colors border border-[#30363d]"
                title="Refresh history for every EVM wallet in a single payment"
              >
                <Layers size={16} />
                <span>Batch import history</span>
              </button>
            )}
            <button
              onClick={() => setShowAdd(true)}
              className="flex items-center gap-2 px-4 py-2 bg-[#00d4ff] hover:bg-[#00d4ff]/90 text-[#0a0e14] font-semibold rounded-lg transition-colors"
            >
              <Plus size={16} />
              <span>Add Wallet</span>
            </button>
          </div>
        </div>

        {error && (
          <div className="bg-[#ff4757]/10 border border-[#ff4757]/30 text-[#ff4757] text-sm rounded-lg p-3 mb-6">
            {error}
          </div>
        )}

        {wallets.length === 0 ? (
          <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-12 text-center">
            <p className="text-[#e6edf3] mb-2">No wallets yet</p>
            <p className="text-[#8b949e] text-sm mb-6">
              Connect an EVM or Solana wallet to start tracking.
            </p>
            <button
              onClick={() => setShowAdd(true)}
              className="inline-flex items-center gap-2 px-4 py-2 bg-[#00d4ff] hover:bg-[#00d4ff]/90 text-[#0a0e14] font-semibold rounded-lg transition-colors"
            >
              <Plus size={16} />
              <span>Add your first wallet</span>
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {wallets.map((w) => {
              const b = busy[w.id]
              return (
                <div
                  key={w.id}
                  className="bg-[#0d1117] border border-[#30363d] rounded-lg p-5 hover:border-[#484f58] transition-colors"
                >
                  <div className="flex items-start justify-between mb-4">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 mb-1">
                        <div
                          className="w-2 h-2 rounded-full flex-shrink-0"
                          style={{ backgroundColor: chainColor(w.chain) }}
                        />
                        <h3 className="text-[#e6edf3] font-medium truncate">{w.label}</h3>
                        <span className="text-xs px-2 py-0.5 bg-[#1c2128] text-[#8b949e] rounded uppercase">
                          {w.chain}
                        </span>
                        {w.verified && (
                          <span className="text-xs px-2 py-0.5 bg-[#00ff88]/10 text-[#00ff88] rounded">
                            Verified
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => copyAddress(w)}
                          className="flex items-center gap-1.5 text-[#8b949e] hover:text-[#e6edf3] text-sm font-mono"
                          title="Copy address"
                        >
                          <span>{truncateAddress(w.address, 8, 6)}</span>
                          {copied === w.id ? <Check size={14} /> : <Copy size={14} />}
                        </button>
                      </div>
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-4 mb-4 text-xs">
                    <div>
                      <div className="text-[#8b949e] mb-0.5">Added</div>
                      <div className="text-[#e6edf3]">{formatDate(w.created_at)}</div>
                    </div>
                    <div>
                      <div className="text-[#8b949e] mb-0.5">Last scanned</div>
                      <div className="text-[#e6edf3]">
                        {w.last_scanned_at ? formatDate(w.last_scanned_at, true) : '—'}
                      </div>
                    </div>
                  </div>

                  {w.chain === 'evm' && (
                    <ChainToggles wallet={w} onUpdate={load} />
                  )}

                  <div className="flex gap-2">
                    <button
                      onClick={() => handleScan(w)}
                      disabled={!!b}
                      className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-50 text-[#e6edf3] rounded-lg text-sm transition-colors border border-[#30363d]"
                    >
                      <RefreshCw size={14} className={b === 'scan' ? 'animate-spin' : ''} />
                      <span>{b === 'scan' ? 'Scanning…' : 'Scan'}</span>
                    </button>
                    {(w.chain === 'evm' || w.chain === 'solana') && (
                      <button
                        onClick={() => setImportWallet(w)}
                        disabled={!!b}
                        className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-50 text-[#e6edf3] rounded-lg text-sm transition-colors border border-[#30363d]"
                      >
                        <Download size={14} />
                        <span>Import history</span>
                      </button>
                    )}
                    <a
                      href={
                        w.chain === 'solana'
                          ? `https://solscan.io/account/${w.address}`
                          : `https://etherscan.io/address/${w.address}`
                      }
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center justify-center px-3 py-2 bg-[#1c2128] hover:bg-[#21262d] text-[#e6edf3] rounded-lg text-sm transition-colors border border-[#30363d]"
                      title="View on explorer"
                    >
                      <ExternalLink size={14} />
                    </a>
                    <button
                      onClick={() => handleDelete(w)}
                      disabled={!!b}
                      className="flex items-center justify-center px-3 py-2 bg-[#1c2128] hover:bg-[#ff4757]/10 hover:text-[#ff4757] disabled:opacity-50 text-[#8b949e] rounded-lg text-sm transition-colors border border-[#30363d]"
                      title="Delete wallet"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {showAdd && (
        <AddWalletModal
          addKind={addKind}
          setAddKind={(k) => { setAddKind(k); setAddAddress(''); setAddError(null) }}
          addMode={addMode}
          setAddMode={(m) => { setAddMode(m); setAddAddress(''); setAddError(null) }}
          addLabel={addLabel}
          setAddLabel={setAddLabel}
          addAddress={addAddress}
          setAddAddress={setAddAddress}
          addBusy={addBusy}
          addError={addError}
          onClose={() => { if (!addBusy) { setShowAdd(false); setAddMode('sign'); setAddAddress('') } }}
          onAdd={handleAdd}
        />
      )}

      {importWallet && (
        <ImportHistoryModal
          wallet={importWallet}
          onClose={() => setImportWallet(null)}
          onDone={() => {
            setImportWallet(null)
            load()
          }}
        />
      )}

      {showBatch && (
        <BatchImportModal
          onClose={() => setShowBatch(false)}
          onDone={() => {
            setShowBatch(false)
            load()
          }}
        />
      )}
    </div>
  )
}

// ---------- Add wallet modal (unchanged logic) ----------

function AddWalletModal({
  addKind,
  setAddKind,
  addMode,
  setAddMode,
  addLabel,
  setAddLabel,
  addAddress,
  setAddAddress,
  addBusy,
  addError,
  onClose,
  onAdd,
}: {
  addKind: 'evm' | 'solana'
  setAddKind: (k: 'evm' | 'solana') => void
  addMode: 'sign' | 'watch'
  setAddMode: (m: 'sign' | 'watch') => void
  addLabel: string
  setAddLabel: (l: string) => void
  addAddress: string
  setAddAddress: (a: string) => void
  addBusy: boolean
  addError: string | null
  onClose: () => void
  onAdd: () => void
}) {
  return (
    <Modal onClose={onClose}>
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-[#e6edf3]">Add wallet</h3>
        <button onClick={onClose} className="text-[#8b949e] hover:text-[#e6edf3]">
          <X size={20} />
        </button>
      </div>

      {/* Chain selector */}
      <div className="mb-4">
        <div className="text-[#8b949e] text-xs mb-2 uppercase tracking-wider">Chain</div>
        <div className="grid grid-cols-2 gap-2">
          {(['evm', 'solana'] as const).map((k) => (
            <button
              key={k}
              onClick={() => setAddKind(k)}
              className={`px-3 py-2 rounded-lg text-sm transition-colors border ${
                addKind === k
                  ? 'bg-[#00d4ff]/10 text-[#00d4ff] border-[#00d4ff]'
                  : 'bg-[#1c2128] text-[#8b949e] border-[#30363d]'
              }`}
            >
              {k === 'evm' ? 'EVM' : 'Solana'}
            </button>
          ))}
        </div>
      </div>

      {/* Mode toggle */}
      <div className="mb-4">
        <div className="text-[#8b949e] text-xs mb-2 uppercase tracking-wider">Method</div>
        <div className="grid grid-cols-2 gap-2">
          <button
            onClick={() => setAddMode('sign')}
            className={`px-3 py-2 rounded-lg text-sm transition-colors border ${
              addMode === 'sign'
                ? 'bg-[#00d4ff]/10 text-[#00d4ff] border-[#00d4ff]'
                : 'bg-[#1c2128] text-[#8b949e] border-[#30363d]'
            }`}
          >
            Sign to verify
          </button>
          <button
            onClick={() => setAddMode('watch')}
            className={`px-3 py-2 rounded-lg text-sm transition-colors border ${
              addMode === 'watch'
                ? 'bg-[#00d4ff]/10 text-[#00d4ff] border-[#00d4ff]'
                : 'bg-[#1c2128] text-[#8b949e] border-[#30363d]'
            }`}
          >
            Watch address
          </button>
        </div>
      </div>

      {/* Label */}
      <div className="mb-4">
        <div className="text-[#8b949e] text-xs mb-2 uppercase tracking-wider">Label</div>
        <input
          type="text"
          value={addLabel}
          onChange={(e) => setAddLabel(e.target.value)}
          placeholder="e.g. Main, Cold storage"
          className="w-full px-3 py-2 bg-[#0a0e14] border border-[#30363d] rounded-lg text-[#e6edf3] text-sm focus:outline-none focus:border-[#00d4ff]"
        />
      </div>

      {/* Address input (watch mode only) */}
      {addMode === 'watch' && (
        <div className="mb-4">
          <div className="text-[#8b949e] text-xs mb-2 uppercase tracking-wider">Address</div>
          <input
            type="text"
            value={addAddress}
            onChange={(e) => setAddAddress(e.target.value)}
            placeholder={addKind === 'solana' ? 'Solana public key (base58)' : '0x… EVM address'}
            className="w-full px-3 py-2 bg-[#0a0e14] border border-[#30363d] rounded-lg text-[#e6edf3] text-sm font-mono focus:outline-none focus:border-[#00d4ff]"
          />
        </div>
      )}

      <p className="text-[#8b949e] text-xs mb-4">
        {addMode === 'sign'
          ? "You'll be asked to sign a message to prove ownership. No transactions are sent."
          : 'Watch-only: balances and history will be tracked but the address is unverified.'}
      </p>

      {addError && (
        <div className="bg-[#ff4757]/10 border border-[#ff4757]/30 text-[#ff4757] text-xs rounded-lg p-3 mb-4">
          {addError}
        </div>
      )}

      <div className="flex gap-2">
        <button
          onClick={onClose}
          disabled={addBusy}
          className="flex-1 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-50 text-[#e6edf3] rounded-lg text-sm border border-[#30363d]"
        >
          Cancel
        </button>
        <button
          onClick={onAdd}
          disabled={addBusy}
          className="flex-1 px-4 py-2 bg-[#00d4ff] hover:bg-[#00d4ff]/90 disabled:opacity-50 text-[#0a0e14] font-semibold rounded-lg text-sm"
        >
          {addBusy
            ? (addMode === 'watch' ? 'Adding…' : 'Waiting for signature…')
            : (addMode === 'watch' ? 'Add (watch only)' : 'Connect')}
        </button>
      </div>
    </Modal>
  )
}

// ---------- Single-wallet import modal ----------

function ImportHistoryModal({
  wallet,
  onClose,
  onDone,
}: {
  wallet: WalletRow
  onClose: () => void
  onDone: () => void
}) {
  const [status, setStatus] = useState<PayStatus>('loading')
  const [quote, setQuote] = useState<ImportQuote | null>(null)
  const [payConfig, setPayConfig] = useState<PayTokenConfig | null>(null)
  const [chainIdx, setChainIdx] = useState(0)
  const [tokenIdx, setTokenIdx] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<{ inserted: number; found: number } | null>(null)

  useEffect(() => {
    ;(async () => {
      setStatus('loading')
      setError(null)
      try {
        const [q, cfg] = await Promise.all([
          api.get<ImportQuote>(`/api/wallets/${wallet.id}/estimate-import`),
          loadPayTokens(),
        ])
        setQuote(q)
        setPayConfig(cfg)
        setStatus('idle')
      } catch (e: any) {
        setError(e instanceof ApiError ? e.detail : e.message || 'Failed to prepare import')
        setStatus('error')
      }
    })()
  }, [wallet.id])

  const chain = payConfig?.chains[chainIdx]
  const token = chain?.tokens[tokenIdx]
  const cost = quote?.cost_usd || 0
  const isFree = cost <= 0

  const run = async () => {
    if (!quote) return
    setError(null)
    try {
      // Free import — skip the whole payment dance and hit the endpoint
      // directly. The backend accepts `pay: null` when cost_usd <= 0.
      if (isFree) {
        setStatus('verifying')
        const res = await api.post<any>(
          `/api/wallets/${wallet.id}/import-history`,
          {
            max_cost_usd: Math.max(cost, 0) + 0.01,
            fetch_prices: true,
          },
        )
        setResult({ inserted: res.inserted || 0, found: res.found || 0 })
        setStatus('done')
        return
      }

      if (!payConfig || !chain || !token) {
        throw new Error('No payment option selected')
      }

      setStatus('signing')
      await ensureEthersLoaded()
      const { paid } = await signImportPayment({
        chain: chain.chain,
        chainId: chain.chain_id,
        tokenSymbol: token.symbol,
        tokenContract: token.contract,
        tokenDecimals: token.decimals,
        treasury: payConfig.treasury_address,
        amountUsd: cost,
      })

      setStatus('verifying')
      // Backend verifies + runs import. May return 425 pending → retry
      // a few times while confirmations land.
      const res = await postImportWithRetry(
        `/api/wallets/${wallet.id}/import-history`,
        {
          max_cost_usd: cost + 0.01,
          fetch_prices: true,
          pay: paid,
        },
      )
      setResult({ inserted: res.inserted || 0, found: res.found || 0 })
      setStatus('done')
    } catch (e: any) {
      const msg = e instanceof ApiError
        ? e.detail
        : e?.reason || e?.shortMessage || e?.message || 'Import failed'
      setError(msg)
      setStatus('error')
    }
  }

  const busy = status === 'signing' || status === 'confirming' || status === 'verifying'

  return (
    <Modal onClose={() => !busy && onClose()}>
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-[#e6edf3]">Import history</h3>
        <button
          onClick={() => !busy && onClose()}
          className="text-[#8b949e] hover:text-[#e6edf3]"
        >
          <X size={20} />
        </button>
      </div>

      <p className="text-[#8b949e] text-sm mb-4">
        Fetches the full onchain history for{' '}
        <span className="text-[#e6edf3]">{wallet.label}</span> and adds every tx to your ledger.
      </p>

      {status === 'loading' && (
        <div className="bg-[#0a0e14] border border-[#30363d] rounded-lg p-4 text-center text-[#8b949e] text-sm">
          {wallet.chain === 'solana' ? 'Preparing import…' : 'Estimating cost…'}
        </div>
      )}

      {status === 'done' && result && (
        <div className="bg-[#00ff88]/5 border border-[#00ff88]/30 rounded-lg p-6 mb-4 text-center">
          <div className="w-12 h-12 rounded-full bg-[#00ff88]/10 text-[#00ff88] flex items-center justify-center mx-auto mb-3">
            <Check size={24} />
          </div>
          <div className="text-[#e6edf3] font-semibold mb-1">Import complete</div>
          <div className="text-[#8b949e] text-sm">
            {result.inserted} new tx{result.inserted === 1 ? '' : 's'} added
            {result.found !== result.inserted && ` (${result.found} scanned)`}
          </div>
        </div>
      )}

      {status !== 'loading' && status !== 'done' && quote && (
        <div className="space-y-4 mb-4">
          <div className="bg-[#0a0e14] border border-[#30363d] rounded-lg p-4 space-y-2 text-sm">
            {wallet.chain === 'solana' ? (
              <>
                {quote.already_imported > 0 && (
                  <Row label="Already imported" value={String(quote.already_imported)} />
                )}
                <Row label="Cost" value="Free" valueColor="#00ff88" />
                <p className="text-[#8b949e] text-xs pt-1">
                  Solana history is fetched via Helius — always free. Tx count shown after import.
                </p>
              </>
            ) : (
              <>
                <Row label="Transactions found" value={String(quote.total_tx)} />
                {quote.is_delta && (
                  <Row label="Already imported" value={String(quote.already_imported)} />
                )}
                {quote.free_tx_applied > 0 && (
                  <Row
                    label="Free txs applied"
                    value={`-${quote.free_tx_applied}`}
                    valueColor="#00ff88"
                  />
                )}
                <Row label="Chargeable" value={String(quote.chargeable_tx)} />
                <Row
                  label="Cost"
                  value={isFree ? 'Free' : formatUSD(cost)}
                  valueColor={isFree ? '#00ff88' : '#e6edf3'}
                />
              </>
            )}
          </div>

          {!isFree && payConfig && payConfig.chains.length > 0 && (
            <PayPicker
              config={payConfig}
              chainIdx={chainIdx}
              tokenIdx={tokenIdx}
              setChainIdx={setChainIdx}
              setTokenIdx={setTokenIdx}
              disabled={busy}
            />
          )}
        </div>
      )}

      {error && (
        <div className="bg-[#ff4757]/10 border border-[#ff4757]/30 text-[#ff4757] text-xs rounded-lg p-3 mb-4">
          {error}
        </div>
      )}

      <PayStatusBanner status={status} />

      <div className="flex gap-2">
        {status === 'done' ? (
          <button
            onClick={onDone}
            className="flex-1 px-4 py-2 bg-[#00d4ff] text-[#0a0e14] font-semibold rounded-lg text-sm"
          >
            Done
          </button>
        ) : (
          <>
            <button
              onClick={onClose}
              disabled={busy}
              className="flex-1 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-50 text-[#e6edf3] rounded-lg text-sm border border-[#30363d]"
            >
              Cancel
            </button>
            <button
              onClick={run}
              disabled={busy || status === 'loading' || !quote}
              className="flex-1 px-4 py-2 bg-[#00d4ff] hover:bg-[#00d4ff]/90 disabled:opacity-50 text-[#0a0e14] font-semibold rounded-lg text-sm"
            >
              {busy
                ? 'Processing…'
                : isFree
                  ? 'Import free'
                  : `Pay ${formatUSD(cost)} & import`}
            </button>
          </>
        )}
      </div>
    </Modal>
  )
}

// ---------- Batch import modal ----------

function BatchImportModal({
  onClose,
  onDone,
}: {
  onClose: () => void
  onDone: () => void
}) {
  const [status, setStatus] = useState<PayStatus>('loading')
  const [quote, setQuote] = useState<BatchImportQuote | null>(null)
  const [payConfig, setPayConfig] = useState<PayTokenConfig | null>(null)
  const [chainIdx, setChainIdx] = useState(0)
  const [tokenIdx, setTokenIdx] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<any | null>(null)

  useEffect(() => {
    ;(async () => {
      setStatus('loading')
      setError(null)
      try {
        const [q, cfg] = await Promise.all([
          api.get<BatchImportQuote>('/api/wallets/estimate-import-all'),
          loadPayTokens(),
        ])
        setQuote(q)
        setPayConfig(cfg)
        setStatus('idle')
      } catch (e: any) {
        setError(e instanceof ApiError ? e.detail : e.message || 'Failed to prepare batch')
        setStatus('error')
      }
    })()
  }, [])

  const chain = payConfig?.chains[chainIdx]
  const token = chain?.tokens[tokenIdx]
  const cost = quote?.cost_usd || 0
  const isFree = cost <= 0

  const run = async () => {
    if (!quote) return
    setError(null)
    try {
      if (isFree) {
        setStatus('verifying')
        const res = await api.post<any>('/api/wallets/import-history-all', {
          max_cost_usd: Math.max(cost, 0) + 0.01,
          fetch_prices: true,
        })
        setResult(res)
        setStatus('done')
        return
      }

      if (!payConfig || !chain || !token) {
        throw new Error('No payment option selected')
      }

      setStatus('signing')
      await ensureEthersLoaded()
      const { paid } = await signImportPayment({
        chain: chain.chain,
        chainId: chain.chain_id,
        tokenSymbol: token.symbol,
        tokenContract: token.contract,
        tokenDecimals: token.decimals,
        treasury: payConfig.treasury_address,
        amountUsd: cost,
      })

      setStatus('verifying')
      const res = await postImportWithRetry('/api/wallets/import-history-all', {
        max_cost_usd: cost + 0.01,
        fetch_prices: true,
        pay: paid,
      })
      setResult(res)
      setStatus('done')
    } catch (e: any) {
      const msg = e instanceof ApiError
        ? e.detail
        : e?.reason || e?.shortMessage || e?.message || 'Import failed'
      setError(msg)
      setStatus('error')
    }
  }

  const busy = status === 'signing' || status === 'confirming' || status === 'verifying'

  return (
    <Modal onClose={() => !busy && onClose()} wide>
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-[#e6edf3]">Batch import history</h3>
        <button
          onClick={() => !busy && onClose()}
          className="text-[#8b949e] hover:text-[#e6edf3]"
        >
          <X size={20} />
        </button>
      </div>

      <p className="text-[#8b949e] text-sm mb-4">
        Refresh the full onchain history for every EVM wallet you've linked,
        paying for all imports in a single transaction.
      </p>

      {status === 'loading' && (
        <div className="bg-[#0a0e14] border border-[#30363d] rounded-lg p-4 text-center text-[#8b949e] text-sm">
          Quoting every wallet…
        </div>
      )}

      {status === 'done' && result && (
        <div className="bg-[#00ff88]/5 border border-[#00ff88]/30 rounded-lg p-6 mb-4">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-full bg-[#00ff88]/10 text-[#00ff88] flex items-center justify-center">
              <Check size={20} />
            </div>
            <div>
              <div className="text-[#e6edf3] font-semibold">Batch import complete</div>
              <div className="text-[#8b949e] text-xs">
                {result.wallet_count} wallet{result.wallet_count === 1 ? '' : 's'} · paid {formatUSD(result.total_cost_usd || 0)}
              </div>
            </div>
          </div>
          <div className="space-y-1 max-h-60 overflow-auto">
            {(result.results || []).map((r: any, i: number) => (
              <div
                key={i}
                className="flex items-center justify-between text-xs px-3 py-2 bg-[#0a0e14] rounded border border-[#30363d]"
              >
                <span className="text-[#e6edf3] truncate">{r.label}</span>
                {r.error ? (
                  <span className="text-[#ff4757]">{r.error}</span>
                ) : r.skipped ? (
                  <span className="text-[#8b949e]">skipped: {r.reason}</span>
                ) : (
                  <span className="text-[#8b949e]">
                    {r.inserted} new / {r.found} scanned
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {status !== 'loading' && status !== 'done' && quote && (
        <div className="space-y-4 mb-4">
          {/* Per-wallet quote breakdown */}
          <div className="bg-[#0a0e14] border border-[#30363d] rounded-lg overflow-hidden">
            <div className="px-4 py-2 border-b border-[#30363d] flex items-center justify-between">
              <span className="text-[#8b949e] text-xs uppercase tracking-wider">
                Breakdown
              </span>
              {quote.free_allowance_remaining_before != null && quote.free_allowance_remaining_before > 0 && (
                <span className="text-[#00ff88] text-xs">
                  {quote.free_allowance_remaining_before} free tx{quote.free_allowance_remaining_before === 1 ? '' : 's'} available
                </span>
              )}
            </div>
            <div className="max-h-48 overflow-auto">
              {quote.wallets.map((w) => (
                <div
                  key={w.wallet_id}
                  className="flex items-center justify-between px-4 py-2 border-b border-[#30363d] last:border-b-0 text-sm"
                >
                  <div className="min-w-0 flex-1">
                    <div className="text-[#e6edf3] truncate">{w.label}</div>
                    <div className="text-[#8b949e] text-xs">
                      {w.is_delta
                        ? w.chargeable_tx > 0
                          ? `${w.total_tx} onchain · ${w.chargeable_tx} new (delta)`
                          : `${w.total_tx} onchain · no new txs`
                        : `${w.total_tx} txs · first import`}
                    </div>
                  </div>
                  <div className="font-mono text-[#e6edf3]">
                    {w.cost_usd > 0 ? formatUSD(w.cost_usd) : <span className="text-[#00ff88]">Free</span>}
                  </div>
                </div>
              ))}
            </div>
            <div className="px-4 py-3 border-t border-[#30363d] flex items-center justify-between">
              <span className="text-[#e6edf3] font-medium">Total</span>
              <span className="font-mono text-[#e6edf3] font-semibold">
                {isFree ? 'Free' : formatUSD(cost)}
              </span>
            </div>
          </div>

          {!isFree && payConfig && payConfig.chains.length > 0 && (
            <PayPicker
              config={payConfig}
              chainIdx={chainIdx}
              tokenIdx={tokenIdx}
              setChainIdx={setChainIdx}
              setTokenIdx={setTokenIdx}
              disabled={busy}
            />
          )}
        </div>
      )}

      {error && (
        <div className="bg-[#ff4757]/10 border border-[#ff4757]/30 text-[#ff4757] text-xs rounded-lg p-3 mb-4">
          {error}
        </div>
      )}

      <PayStatusBanner status={status} />

      <div className="flex gap-2">
        {status === 'done' ? (
          <button
            onClick={onDone}
            className="flex-1 px-4 py-2 bg-[#00d4ff] text-[#0a0e14] font-semibold rounded-lg text-sm"
          >
            Done
          </button>
        ) : (
          <>
            <button
              onClick={onClose}
              disabled={busy}
              className="flex-1 px-4 py-2 bg-[#1c2128] hover:bg-[#21262d] disabled:opacity-50 text-[#e6edf3] rounded-lg text-sm border border-[#30363d]"
            >
              Cancel
            </button>
            <button
              onClick={run}
              disabled={busy || status === 'loading' || !quote || quote.wallet_count === 0}
              className="flex-1 px-4 py-2 bg-[#00d4ff] hover:bg-[#00d4ff]/90 disabled:opacity-50 text-[#0a0e14] font-semibold rounded-lg text-sm"
            >
              {busy
                ? 'Processing…'
                : isFree
                  ? `Import ${quote?.wallet_count || 0} wallets`
                  : `Pay ${formatUSD(cost)} & import all`}
            </button>
          </>
        )}
      </div>
    </Modal>
  )
}

// ---------- Shared subcomponents ----------

function PayPicker({
  config,
  chainIdx,
  tokenIdx,
  setChainIdx,
  setTokenIdx,
  disabled,
}: {
  config: PayTokenConfig
  chainIdx: number
  tokenIdx: number
  setChainIdx: (i: number) => void
  setTokenIdx: (i: number) => void
  disabled: boolean
}) {
  const chain = config.chains[chainIdx]
  return (
    <div className="bg-[#0a0e14] border border-[#30363d] rounded-lg p-4 space-y-3">
      <div>
        <div className="text-[#8b949e] text-xs mb-2 uppercase tracking-wider">Pay with chain</div>
        <div className="grid grid-cols-3 gap-2">
          {config.chains.map((c, i) => (
            <button
              key={c.chain}
              onClick={() => {
                setChainIdx(i)
                setTokenIdx(0)
              }}
              disabled={disabled}
              className={`px-3 py-2 rounded-lg text-xs transition-colors border disabled:opacity-50 ${
                chainIdx === i
                  ? 'bg-[#00d4ff]/10 text-[#00d4ff] border-[#00d4ff]'
                  : 'bg-[#1c2128] text-[#8b949e] border-[#30363d]'
              }`}
            >
              {c.display}
            </button>
          ))}
        </div>
      </div>
      <div>
        <div className="text-[#8b949e] text-xs mb-2 uppercase tracking-wider">Token</div>
        <div className="flex flex-wrap gap-2">
          {chain?.tokens.map((t, i) => (
            <button
              key={t.symbol}
              onClick={() => setTokenIdx(i)}
              disabled={disabled}
              className={`px-3 py-1.5 rounded-lg text-sm transition-colors border disabled:opacity-50 ${
                tokenIdx === i
                  ? 'bg-[#00d4ff]/10 text-[#00d4ff] border-[#00d4ff]'
                  : 'bg-[#1c2128] text-[#8b949e] border-[#30363d]'
              }`}
            >
              {t.symbol}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function PayStatusBanner({ status }: { status: PayStatus }) {
  if (status === 'signing') {
    return (
      <div className="bg-[#00d4ff]/10 border border-[#00d4ff]/30 text-[#00d4ff] text-xs rounded-lg p-3 mb-4 flex items-center gap-2">
        <RefreshCw size={14} className="animate-spin" />
        <span>Waiting for signature in your wallet…</span>
      </div>
    )
  }
  if (status === 'confirming') {
    return (
      <div className="bg-[#00d4ff]/10 border border-[#00d4ff]/30 text-[#00d4ff] text-xs rounded-lg p-3 mb-4 flex items-center gap-2">
        <RefreshCw size={14} className="animate-spin" />
        <span>Waiting for on-chain confirmation…</span>
      </div>
    )
  }
  if (status === 'verifying') {
    return (
      <div className="bg-[#00d4ff]/10 border border-[#00d4ff]/30 text-[#00d4ff] text-xs rounded-lg p-3 mb-4 flex items-center gap-2">
        <RefreshCw size={14} className="animate-spin" />
        <span>Verifying payment & importing history… (up to ~3 min)</span>
      </div>
    )
  }
  return null
}

/**
 * POST an import request, retrying on HTTP 202 (payment pending
 * confirmation on our side) with a short backoff. The backend's
 * MIN_AGE_SECONDS gate means a freshly-broadcast tx may not be
 * acceptable for ~30s even after the wallet reports it mined.
 */
async function postImportWithRetry(path: string, body: any) {
  const maxAttempts = 20
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      return await api.post<any>(path, body)
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 425 && attempt < maxAttempts - 1) {
        await new Promise((r) => setTimeout(r, 10000))
        continue
      }
      throw e
    }
  }
  throw new Error('Payment did not confirm in time')
}

function Modal({
  children,
  onClose,
  wide,
}: {
  children: React.ReactNode
  onClose: () => void
  wide?: boolean
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div
        className={`relative bg-[#0d1117] border border-[#30363d] rounded-xl p-6 w-full shadow-2xl ${
          wide ? 'max-w-xl' : 'max-w-md'
        }`}
      >
        {children}
      </div>
    </div>
  )
}

function Row({ label, value, valueColor }: { label: string; value: string; valueColor?: string }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[#8b949e]">{label}</span>
      <span className="font-mono" style={{ color: valueColor || '#e6edf3' }}>
        {value}
      </span>
    </div>
  )
}

interface ChainInfo {
  slug: string
  name: string
  chain_id: number
  core: boolean
}

const CHAIN_COLORS: Record<string, string> = {
  eth: '#627eea', polygon: '#8247e5', bsc: '#f0b90b', arbitrum: '#28a0f0',
  optimism: '#ff0420', base: '#0052ff', avalanche: '#e84142', fantom: '#1969ff',
  cronos: '#002d74', gnosis: '#04795b', linea: '#61dfff', moonbeam: '#53cbc9',
  moonriver: '#f2b705', pulsechain: '#00ff00', ronin: '#1273ea', lisk: '#0d47a1',
  flow: '#00ef8b', chiliz: '#cd0124',
}

function ChainToggles({ wallet, onUpdate }: { wallet: WalletRow; onUpdate: () => void }) {
  const enabled: string[] = wallet.enabled_chains || ['eth', 'polygon', 'bsc', 'arbitrum', 'optimism', 'base']
  const [saving, setSaving] = useState(false)
  const [allChains, setAllChains] = useState<ChainInfo[]>([])
  const [showAdd, setShowAdd] = useState(false)

  useEffect(() => {
    api.get<ChainInfo[]>('/api/chains').then(setAllChains).catch(() => {})
  }, [])

  const toggle = async (slug: string) => {
    const isEnabled = enabled.includes(slug)
    let next: string[]
    if (isEnabled) {
      next = enabled.filter((c) => c !== slug)
      if (next.length === 0) return
    } else {
      next = [...enabled, slug]
    }
    setSaving(true)
    try {
      await api.put(`/api/wallets/${wallet.id}/chains`, { chains: next })
      onUpdate()
    } catch {
      // silently fail
    } finally {
      setSaving(false)
    }
  }

  const addChain = async (slug: string) => {
    if (enabled.includes(slug)) return
    const next = [...enabled, slug]
    setSaving(true)
    setShowAdd(false)
    try {
      await api.put(`/api/wallets/${wallet.id}/chains`, { chains: next })
      onUpdate()
    } catch {
      // silently fail
    } finally {
      setSaving(false)
    }
  }

  const availableToAdd = allChains.filter((c) => !enabled.includes(c.slug))

  return (
    <div className="mb-3">
      <div className="text-[#8b949e] text-xs mb-1.5">Active chains</div>
      <div className="flex flex-wrap gap-1.5 items-center">
        {enabled.map((slug) => {
          const info = allChains.find((c) => c.slug === slug)
          const label = info?.name || slug
          const color = CHAIN_COLORS[slug] || '#8b949e'
          return (
            <button
              key={slug}
              onClick={() => toggle(slug)}
              disabled={saving}
              className="text-xs px-2 py-1 rounded-md border border-[#30363d] bg-[#1c2128] text-[#e6edf3] hover:border-[#ff4757] hover:bg-[#ff4757]/10 disabled:opacity-50 transition-all group"
              title={`Remove ${label}`}
            >
              <span
                className="inline-block w-1.5 h-1.5 rounded-full mr-1"
                style={{ backgroundColor: color }}
              />
              {label}
              <span className="ml-1 text-[#484f58] group-hover:text-[#ff4757]">&times;</span>
            </button>
          )
        })}
        {availableToAdd.length > 0 && (
          <div className="relative">
            <button
              onClick={() => setShowAdd(!showAdd)}
              disabled={saving}
              className="text-xs px-2 py-1 rounded-md border border-dashed border-[#30363d] text-[#484f58] hover:border-[#00d4ff] hover:text-[#00d4ff] disabled:opacity-50 transition-all"
              title="Add chain"
            >
              + Add
            </button>
            {showAdd && (
              <div className="absolute top-full left-0 mt-1 z-50 bg-[#161b22] border border-[#30363d] rounded-lg shadow-xl py-1 w-44 max-h-48 overflow-y-auto">
                {availableToAdd.map((c) => (
                  <button
                    key={c.slug}
                    onClick={() => addChain(c.slug)}
                    className="w-full text-left px-3 py-1.5 text-xs text-[#e6edf3] hover:bg-[#1c2128] flex items-center gap-2"
                  >
                    <span
                      className="inline-block w-2 h-2 rounded-full flex-shrink-0"
                      style={{ backgroundColor: CHAIN_COLORS[c.slug] || '#8b949e' }}
                    />
                    {c.name}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
