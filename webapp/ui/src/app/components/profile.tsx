import { useEffect, useState } from 'react'
import { User, LogOut, Copy, Check, ChevronRight } from 'lucide-react'
import { api, MeResponse, WalletRow, ApiError } from '../lib/api'
import { truncateAddress, formatDate, chainColor } from '../lib/format'

interface ProfileProps {
  me: MeResponse
  onSignOut: () => void
  onNavigate: (screen: string) => void
}

export function Profile({ me, onSignOut, onNavigate }: ProfileProps) {
  const [wallets, setWallets] = useState<WalletRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    ;(async () => {
      setLoading(true)
      try {
        const res = await api.get<WalletRow[]>('/api/wallets')
        setWallets(res || [])
      } catch (e: any) {
        setError(e instanceof ApiError ? e.detail : e.message || 'Failed to load')
      } finally {
        setLoading(false)
      }
    })()
  }, [])

  const copyPrimary = async () => {
    try {
      await navigator.clipboard.writeText(me.user.primary_address)
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    } catch {
      /* ignore */
    }
  }

  const displayName =
    me.user.display_name ||
    `${me.user.chain_type.toUpperCase()} · ${truncateAddress(me.user.primary_address)}`

  return (
    <div className="flex-1 overflow-auto bg-[#0a0e14]">
      <div className="p-8 max-w-[900px]">
        <h2 className="text-2xl font-semibold text-[#e6edf3] mb-8">Profile</h2>

        {/* Identity card */}
        <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-6 mb-6">
          <div className="flex items-start gap-4">
            <div className="w-16 h-16 rounded-2xl bg-[#00d4ff]/10 text-[#00d4ff] flex items-center justify-center flex-shrink-0">
              <User size={28} />
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-[#e6edf3] text-xl font-semibold mb-1">{displayName}</div>
              <button
                onClick={copyPrimary}
                className="inline-flex items-center gap-1.5 text-[#8b949e] hover:text-[#e6edf3] text-sm font-mono"
              >
                <span>{me.user.primary_address}</span>
                {copied ? <Check size={14} /> : <Copy size={14} />}
              </button>
              <div className="mt-3 flex gap-6 text-xs text-[#8b949e]">
                <div>
                  <span className="text-[#8b949e]">Member since</span>{' '}
                  <span className="text-[#e6edf3]">{formatDate(me.user.created_at)}</span>
                </div>
                <div>
                  <span className="text-[#8b949e]">Last sign-in</span>{' '}
                  <span className="text-[#e6edf3]">{formatDate(me.user.last_login_at, true)}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Linked wallets */}
        <div className="bg-[#0d1117] border border-[#30363d] rounded-xl overflow-hidden mb-6">
          <div className="px-6 py-4 border-b border-[#30363d] flex items-center justify-between">
            <div>
              <h3 className="text-[#e6edf3] font-medium">Linked wallets</h3>
              <p className="text-[#8b949e] text-xs mt-0.5">
                {wallets.length} wallet{wallets.length === 1 ? '' : 's'} connected
              </p>
            </div>
            <button
              onClick={() => onNavigate('wallets')}
              className="flex items-center gap-1 text-[#00d4ff] hover:text-[#00d4ff]/80 text-sm"
            >
              Manage
              <ChevronRight size={16} />
            </button>
          </div>

          {loading ? (
            <div className="p-6 text-center text-[#8b949e] text-sm">Loading…</div>
          ) : error ? (
            <div className="p-6 text-center text-[#ff4757] text-sm">{error}</div>
          ) : wallets.length === 0 ? (
            <div className="p-6 text-center text-[#8b949e] text-sm">
              No wallets linked yet.
            </div>
          ) : (
            <div>
              {wallets.map((w) => (
                <div
                  key={w.id}
                  className="flex items-center gap-3 px-6 py-3 border-b border-[#30363d] last:border-b-0"
                >
                  <div
                    className="w-2 h-2 rounded-full flex-shrink-0"
                    style={{ backgroundColor: chainColor(w.chain) }}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-[#e6edf3] text-sm font-medium truncate">
                        {w.label}
                      </span>
                      <span className="text-[10px] px-1.5 py-0.5 bg-[#1c2128] text-[#8b949e] rounded uppercase">
                        {w.chain}
                      </span>
                      {w.verified && (
                        <span className="text-[10px] px-1.5 py-0.5 bg-[#00ff88]/10 text-[#00ff88] rounded">
                          Verified
                        </span>
                      )}
                    </div>
                    <div className="text-[#8b949e] text-xs font-mono">
                      {truncateAddress(w.address, 10, 6)}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Actions */}
        <div className="bg-[#0d1117] border border-[#30363d] rounded-xl p-6">
          <h3 className="text-[#e6edf3] font-medium mb-4">Account</h3>
          <button
            onClick={onSignOut}
            className="w-full flex items-center justify-between px-4 py-3 bg-[#1c2128] hover:bg-[#ff4757]/10 hover:text-[#ff4757] text-[#e6edf3] rounded-lg transition-colors border border-[#30363d]"
          >
            <span className="flex items-center gap-3">
              <LogOut size={16} />
              <span>Sign out</span>
            </span>
            <ChevronRight size={16} />
          </button>
        </div>
      </div>
    </div>
  )
}
