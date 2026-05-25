/**
 * Pay-per-import wallet payment helpers.
 *
 * Both the single-wallet and batch-import modals share the same user
 * journey: pick chain → pick token → switch network → sign ERC-20
 * transfer → wait for confirmation → pass the tx hash back to the
 * backend which verifies it on Moralis and runs the import. This file
 * centralises the ethers calls so the components only own the UI state.
 */

import { api, PayTokenConfig, ImportPayDetails } from './api'

declare global {
  interface Window {
    ethereum?: any
    ethers?: any
  }
}

/**
 * Load ethers UMD script if not already present.
 */
function loadEthersScript(): Promise<void> {
  return new Promise((resolve, reject) => {
    if (window.ethers) return resolve()
    if (document.querySelector('script[data-wa="/static/ethers.umd.min.js"]')) {
      // Script tag exists but ethers not yet on window — poll briefly
      const check = setInterval(() => {
        if (window.ethers) { clearInterval(check); resolve() }
      }, 50)
      setTimeout(() => { clearInterval(check); reject(new Error('ethers load timeout')) }, 5000)
      return
    }
    const s = document.createElement('script')
    s.src = '/static/ethers.umd.min.js'
    s.dataset.wa = '/static/ethers.umd.min.js'
    s.onload = () => resolve()
    s.onerror = () => reject(new Error('Failed to load ethers'))
    document.head.appendChild(s)
  })
}

/**
 * Fetch the per-chain stablecoin config (treasury address + token list).
 * This does NOT wait for ethers UMD — that's deferred until the user
 * actually clicks "Pay" so the modal can render its quote breakdown
 * even on slow/unreliable CDN conditions.
 */
export async function loadPayTokens(): Promise<PayTokenConfig> {
  return api.get<PayTokenConfig>('/api/billing/pay/tokens')
}

/**
 * Ensure ethers UMD is loaded on window. Callers should await this
 * right before they call `signImportPayment` — doing it earlier blocks
 * the whole modal on the CDN fetch.
 */
export async function ensureEthersLoaded(): Promise<void> {
  await loadEthersScript()
}

export interface SignAndSendResult {
  txHash: string
  paid: ImportPayDetails
}

/**
 * Switch the wallet to `chainId`, build an ERC-20 transfer to the
 * treasury for `amountUsd` of `token`, broadcast it, wait for one
 * confirmation, and return a SignAndSendResult the caller can post to
 * the backend.
 *
 * Throws on any error (user rejected, chain switch failed, tx revert,
 * etc) — callers should catch and surface e.reason / e.message.
 */
export async function signImportPayment(opts: {
  chain: string
  chainId: number
  tokenSymbol: string
  tokenContract: string
  tokenDecimals: number
  treasury: string
  amountUsd: number
}): Promise<SignAndSendResult> {
  if (!window.ethereum) {
    throw new Error('No EVM wallet detected. Install MetaMask or Rabby.')
  }
  const ethers = (window as any).ethers
  if (!ethers) {
    throw new Error('ethers library not loaded')
  }

  // Round the amount to the token's decimal precision so parseUnits
  // doesn't throw on a stray float artifact like 5.199999999999999.
  const amountStr = opts.amountUsd.toFixed(
    Math.min(opts.tokenDecimals, 6), // stables rarely need more than 6
  )

  // Switch network first — silently bounces off if already on the right
  // chain. Error 4902 means the chain isn't added to the wallet; we
  // surface that as a human error so the user knows to add it.
  try {
    await window.ethereum.request({
      method: 'wallet_switchEthereumChain',
      params: [{ chainId: '0x' + opts.chainId.toString(16) }],
    })
  } catch (err: any) {
    if (err?.code === 4902) {
      throw new Error(`Please add ${opts.chain} to your wallet and retry.`)
    }
    throw err
  }

  const provider = new ethers.BrowserProvider(window.ethereum)
  const signer = await provider.getSigner()

  const erc20Abi = ['function transfer(address to, uint256 amount) returns (bool)']
  const contract = new ethers.Contract(opts.tokenContract, erc20Abi, signer)
  const units = ethers.parseUnits(amountStr, opts.tokenDecimals)

  const tx = await contract.transfer(opts.treasury, units)
  // Wait for inclusion — the backend's verify loop also requires a
  // minimum confirmation depth, but we want the txHash to at least be
  // on-chain before we hand it back.
  await tx.wait()

  return {
    txHash: tx.hash,
    paid: {
      chain: opts.chain,
      tx_hash: tx.hash,
      expected_token: opts.tokenSymbol,
      expected_usd: parseFloat(amountStr),
    },
  }
}
