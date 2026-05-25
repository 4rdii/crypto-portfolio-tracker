/**
 * Auth helpers — thin wrappers around our backend endpoints.
 *
 * Wallet connection is now handled by Dynamic.xyz SDK (see DynamicProvider
 * in main.tsx). These helpers handle the SIWE/SIWS signing + backend
 * verification once a wallet is already connected via Dynamic.
 */

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function postJson(url: string, body: any) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) {
    let detail = 'Request failed'
    try {
      detail = (await r.json()).detail || detail
    } catch {}
    throw new Error(detail)
  }
  return r.json()
}

function buildSiweMessage({
  domain,
  address,
  uri,
  chainId,
  nonce,
  statement,
}: {
  domain: string
  address: string
  uri: string
  chainId: number
  nonce: string
  statement: string
}) {
  const isoNoMs = (d: Date) => d.toISOString().replace(/\.\d{3}Z$/, 'Z')
  const issuedAt = isoNoMs(new Date())
  const expirationTime = isoNoMs(new Date(Date.now() + 10 * 60 * 1000))
  return [
    `${domain} wants you to sign in with your Ethereum account:`,
    address,
    '',
    statement,
    '',
    `URI: ${uri}`,
    `Version: 1`,
    `Chain ID: ${chainId}`,
    `Nonce: ${nonce}`,
    `Issued At: ${issuedAt}`,
    `Expiration Time: ${expirationTime}`,
  ].join('\n')
}

// ---------------------------------------------------------------------------
// EVM sign-in via Dynamic wallet
// ---------------------------------------------------------------------------

export async function signInEvmWithSigner(signer: any): Promise<void> {
  const address = await signer.getAddress()
  const network = await signer.provider.getNetwork()
  const chainId = Number(network.chainId)

  const { nonce } = await postJson('/api/auth/nonce', {
    address,
    chain_type: 'evm',
  })

  const message = buildSiweMessage({
    domain: window.location.host,
    address,
    uri: window.location.origin,
    chainId,
    nonce,
    statement: 'Sign in to Portfolio Tracker',
  })

  const signature = await signer.signMessage(message)
  await postJson('/api/auth/verify/evm', { message, signature })
}

// ---------------------------------------------------------------------------
// EVM link wallet via Dynamic
// ---------------------------------------------------------------------------

export async function linkEvmWithSigner(signer: any, label: string): Promise<any> {
  const address = await signer.getAddress()
  const network = await signer.provider.getNetwork()
  const chainId = Number(network.chainId)

  const { nonce } = await postJson('/api/auth/nonce', {
    address,
    chain_type: 'evm',
  })

  const message = buildSiweMessage({
    domain: window.location.host,
    address,
    uri: window.location.origin,
    chainId,
    nonce,
    statement: 'Link this wallet to your Portfolio Tracker profile',
  })

  const signature = await signer.signMessage(message)
  return postJson('/api/wallets/link', {
    address,
    label: label || 'EVM Wallet',
    chain_type: 'evm',
    message,
    signature,
  })
}

// ---------------------------------------------------------------------------
// Solana sign-in via Dynamic wallet
// ---------------------------------------------------------------------------

async function bs58() {
  const mod = await import('https://cdn.jsdelivr.net/npm/bs58@6.0.0/+esm' as any)
  return (mod as any).default
}

export async function signInSolanaWithProvider(
  solanaProvider: any,
  address: string
): Promise<void> {
  const { nonce } = await postJson('/api/auth/nonce', {
    address,
    chain_type: 'solana',
  })

  const statement = 'Sign in to Portfolio Tracker'
  const message =
    `${window.location.host} wants you to sign in with your Solana account:\n` +
    `${address}\n\n${statement}\n\nURI: ${window.location.origin}\n` +
    `Nonce: ${nonce}\nIssued At: ${new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')}`

  const encoded = new TextEncoder().encode(message)
  const signed = await solanaProvider.signMessage(encoded, 'utf8')
  const b58 = await bs58()
  const signature = b58.encode(signed.signature || signed)

  await postJson('/api/auth/verify/solana', {
    address,
    message,
    signature,
    nonce,
  })
}

// ---------------------------------------------------------------------------
// Solana link wallet via Dynamic
// ---------------------------------------------------------------------------

export async function linkSolanaWithProvider(
  solanaProvider: any,
  address: string,
  label: string
): Promise<any> {
  const { nonce } = await postJson('/api/auth/nonce', {
    address,
    chain_type: 'solana',
  })

  const statement = 'Link this wallet to your Portfolio Tracker profile'
  const message =
    `${window.location.host} wants you to sign in with your Solana account:\n` +
    `${address}\n\n${statement}\n\nURI: ${window.location.origin}\n` +
    `Nonce: ${nonce}\nIssued At: ${new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')}`

  const encoded = new TextEncoder().encode(message)
  const signed = await solanaProvider.signMessage(encoded, 'utf8')
  const b58 = await bs58()
  const signature = b58.encode(signed.signature || signed)

  return postJson('/api/wallets/link', {
    address,
    label: label || 'Solana Wallet',
    chain_type: 'solana',
    message,
    signature,
    nonce,
  })
}

// Legacy exports for backward compat with wallet-auth.js (no longer used)
export async function signInEvm() {
  throw new Error('Use Dynamic SDK wallet connection instead')
}
export async function signInSolana() {
  throw new Error('Use Dynamic SDK wallet connection instead')
}
export async function linkEvmWallet(_label: string) {
  throw new Error('Use Dynamic SDK wallet connection instead')
}
export async function linkSolanaWallet(_label: string) {
  throw new Error('Use Dynamic SDK wallet connection instead')
}
