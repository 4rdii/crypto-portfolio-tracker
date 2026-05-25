import { useState, useEffect } from 'react'
import { Shield } from 'lucide-react'
import { DynamicWidget, useDynamicContext } from '@dynamic-labs/sdk-react-core'
import { getSigner } from '@dynamic-labs/ethers-v6'
import { signInEvmWithSigner, signInSolanaWithProvider } from '../lib/auth'

interface LandingProps {
  onConnected: () => void
}

export function Landing({ onConnected }: LandingProps) {
  const [error, setError] = useState<string | null>(null)
  const [signingIn, setSigningIn] = useState(false)
  const { primaryWallet } = useDynamicContext()

  // When Dynamic connects a wallet, do SIWE/SIWS sign-in with our backend
  useEffect(() => {
    if (!primaryWallet || signingIn) return

    const doSignIn = async () => {
      setSigningIn(true)
      setError(null)
      try {
        const connector = primaryWallet.connector
        const chain = connector?.connectedChain

        if (chain === 'SOL' || primaryWallet.chain === 'SOL') {
          // Solana wallet
          const solProvider = await primaryWallet.connector?.getSigner()
          const address = primaryWallet.address
          if (!address) throw new Error('No Solana address')
          await signInSolanaWithProvider(solProvider || (window as any).solana, address)
        } else {
          // EVM wallet — get ethers signer via Dynamic, with fallback
          let signer: any
          try {
            signer = await getSigner(primaryWallet)
          } catch {
            // Fallback: get the raw provider from the connector and wrap with ethers
            const walletClient = await primaryWallet.connector?.getSigner()
            if (walletClient) {
              const { BrowserProvider } = await import('ethers')
              const provider = new BrowserProvider(walletClient as any)
              signer = await provider.getSigner()
            } else {
              throw new Error('Could not get wallet signer — please try again')
            }
          }
          await signInEvmWithSigner(signer)
        }
        onConnected()
      } catch (e: any) {
        setError(e?.message || 'Sign-in failed')
        setSigningIn(false)
      }
    }

    // Small delay to let Dynamic SDK fully initialize the wallet connection
    const timer = setTimeout(doSignIn, 500)
    return () => clearTimeout(timer)
  }, [primaryWallet])

  return (
    <div className="min-h-screen bg-[#0a0e14] flex items-center justify-center p-6">
      <div className="max-w-md w-full">
        <div className="text-center mb-12">
          <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-[#00d4ff]/10 text-[#00d4ff] text-2xl font-bold mb-4">
            ◆
          </div>
          <h1 className="text-4xl font-bold text-[#e6edf3] mb-4">Portfolio Tracker</h1>
          <p className="text-[#8b949e] text-lg">
            Track your crypto holdings across multiple chains
          </p>
        </div>

        <div className="flex justify-center mb-6">
          {signingIn ? (
            <div className="text-[#00d4ff] text-sm">Verifying wallet ownership...</div>
          ) : (
            <DynamicWidget />
          )}
        </div>

        {error && (
          <div className="bg-[#ff4757]/10 border border-[#ff4757]/30 text-[#ff4757] text-sm rounded-lg p-3 mb-6">
            {error}
          </div>
        )}

        <div className="bg-[#0d1117] border border-[#30363d] rounded-lg p-4 flex gap-3">
          <Shield size={20} className="text-[#00ff88] flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-[#e6edf3] text-sm font-medium mb-1">Read-only access</p>
            <p className="text-[#8b949e] text-sm">
              We only request a signature to verify wallet ownership. No token approvals or transfers required.
            </p>
          </div>
        </div>

        <p className="text-center text-[#8b949e] text-xs mt-8">
          Supports MetaMask, Rabby, Coinbase, Rainbow, Phantom, Solflare, WalletConnect, and more
        </p>
      </div>
    </div>
  )
}
