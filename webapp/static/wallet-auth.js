// Wallet connection + sign-in helpers.
//
// This file is vanilla JS (no build step). It exposes a small surface area
// that the landing page and the "Link wallet" button use to drive the SIWE /
// SIWS flows:
//
//   await connectEVM(intent)        → connects an injected EVM wallet and
//                                      returns { address }. `intent` is just
//                                      a label used in UX strings.
//
//   await signInWithEthereum()      → full sign-in flow: connect → nonce →
//                                      sign SIWE message → POST verify →
//                                      redirect to /dashboard on success.
//
//   await signInWithSolana()        → same but for Phantom / Solflare /
//                                      Backpack via window.solana.
//
//   await linkEvmWallet(label)      → after the user is already signed in,
//                                      connects a second EVM wallet, has it
//                                      sign a link challenge, and POSTs to
//                                      /api/wallets/link.
//
//   await linkSolanaWallet(label)   → same for a Solana wallet.
//
// "Every conventional wallet" coverage:
//   - MetaMask, Rabby, Coinbase Wallet, Brave, Frame, Trust Wallet desktop,
//     OKX, Bitget, TokenPocket, Zerion desktop, Rainbow desktop — all of
//     these inject window.ethereum and go through the same EIP-1193 path.
//   - WalletConnect v2 modal requires a Reown project ID (cloud.reown.com).
//     When the WC_PROJECT_ID below is set, the WalletConnect button becomes
//     active and covers another 600+ mobile wallets (Trust, Rainbow, Argent,
//     imToken, SafePal, etc.). Until then we surface a "set project ID" hint.
//   - Phantom, Solflare, Backpack all expose window.solana (and window.phantom
//     / window.solflare / window.backpack). We iterate through the known
//     providers so whichever is installed wins.

const WC_PROJECT_ID = ""; // set to enable WalletConnect mobile wallet support

// ---------------------------------------------------------------------------
// Helpers

async function postJson(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = "Request failed";
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

function buildSiweMessage({ domain, address, uri, chainId, nonce, statement }) {
  // EIP-4361 canonical format. Line order + spacing matters — any deviation
  // breaks the siwe-py parser on the server.
  //
  // CAREFUL: siwe-py does NOT accept millisecond precision on Issued At /
  // Expiration Time. `new Date().toISOString()` returns "...T14:55:33.123Z"
  // which fails to parse. Strip the milliseconds — second precision is enough.
  const isoNoMs = (d) => d.toISOString().replace(/\.\d{3}Z$/, "Z");
  const issuedAt = isoNoMs(new Date());
  const expirationTime = isoNoMs(new Date(Date.now() + 10 * 60 * 1000));
  return [
    `${domain} wants you to sign in with your Ethereum account:`,
    address,
    "",
    statement,
    "",
    `URI: ${uri}`,
    `Version: 1`,
    `Chain ID: ${chainId}`,
    `Nonce: ${nonce}`,
    `Issued At: ${issuedAt}`,
    `Expiration Time: ${expirationTime}`,
  ].join("\n");
}

function toChecksum(address) {
  try { return ethers.getAddress(address); } catch { return address; }
}

// ---------------------------------------------------------------------------
// EVM — injected provider (MetaMask / Rabby / Coinbase Wallet / Brave / ...)

async function getInjectedProvider() {
  if (!window.ethereum) {
    throw new Error(
      "No Ethereum wallet detected. Install MetaMask, Rabby, Coinbase Wallet, " +
      "Rainbow, Brave Wallet, or any compatible browser extension."
    );
  }
  // EIP-6963 multi-provider discovery — if multiple wallets are installed,
  // prefer the one the user most recently used. For simplicity we use whichever
  // `window.ethereum` resolves to.
  return new ethers.BrowserProvider(window.ethereum);
}

async function connectEVM() {
  const provider = await getInjectedProvider();
  // Request account access
  const accounts = await provider.send("eth_requestAccounts", []);
  if (!accounts || accounts.length === 0) throw new Error("No account returned");
  const signer = await provider.getSigner();
  const address = await signer.getAddress();
  const network = await provider.getNetwork();
  return { provider, signer, address: toChecksum(address), chainId: Number(network.chainId) };
}

async function signInWithEthereum() {
  const { signer, address, chainId } = await connectEVM();
  const { nonce } = await postJson("/api/auth/nonce", {
    address, chain_type: "evm",
  });
  const message = buildSiweMessage({
    domain: window.location.host,
    address,
    uri: window.location.origin,
    chainId,
    nonce,
    statement: "Sign in to Portfolio Tracker",
  });
  const signature = await signer.signMessage(message);
  await postJson("/api/auth/verify/evm", { message, signature });
  window.location = "/dashboard";
}

async function linkEvmWallet(label) {
  const { signer, address, chainId } = await connectEVM();
  const { nonce } = await postJson("/api/auth/nonce", {
    address, chain_type: "evm",
  });
  const message = buildSiweMessage({
    domain: window.location.host,
    address,
    uri: window.location.origin,
    chainId,
    nonce,
    statement: "Link this wallet to your Portfolio Tracker profile",
  });
  const signature = await signer.signMessage(message);
  return postJson("/api/wallets/link", {
    address,
    label: label || "EVM Wallet",
    chain_type: "evm",
    message,
    signature,
  });
}

// ---------------------------------------------------------------------------
// Solana — Phantom / Solflare / Backpack via window.solana

function getSolanaProvider() {
  // Phantom puts itself at window.solana AND window.phantom.solana.
  // Solflare exposes window.solflare. Backpack exposes window.backpack.
  if (window.phantom?.solana?.isPhantom) return window.phantom.solana;
  if (window.solflare?.isSolflare) return window.solflare;
  if (window.backpack?.isBackpack) return window.backpack;
  if (window.solana) return window.solana;
  throw new Error(
    "No Solana wallet detected. Install Phantom, Solflare, or Backpack."
  );
}

async function connectSolana() {
  const provider = getSolanaProvider();
  const res = await provider.connect();
  const address = res.publicKey.toString();
  return { provider, address };
}

// bs58 helpers via the esm module — we lazy-import to avoid blocking page load
async function bs58() {
  const mod = await import("https://cdn.jsdelivr.net/npm/bs58@6.0.0/+esm");
  return mod.default;
}

async function signInWithSolana() {
  const { provider, address } = await connectSolana();
  const { nonce } = await postJson("/api/auth/nonce", {
    address, chain_type: "solana",
  });
  const statement = "Sign in to Portfolio Tracker";
  const message =
    `${window.location.host} wants you to sign in with your Solana account:\n` +
    `${address}\n\n${statement}\n\nURI: ${window.location.origin}\n` +
    `Nonce: ${nonce}\nIssued At: ${new Date().toISOString().replace(/\.\d{3}Z$/, "Z")}`;
  const encoded = new TextEncoder().encode(message);
  const signed = await provider.signMessage(encoded, "utf8");
  const b58 = await bs58();
  const signature = b58.encode(signed.signature || signed);
  await postJson("/api/auth/verify/solana", {
    address, message, signature, nonce,
  });
  window.location = "/dashboard";
}

async function linkSolanaWallet(label) {
  const { provider, address } = await connectSolana();
  const { nonce } = await postJson("/api/auth/nonce", {
    address, chain_type: "solana",
  });
  const statement = "Link this wallet to your Portfolio Tracker profile";
  const message =
    `${window.location.host} wants you to sign in with your Solana account:\n` +
    `${address}\n\n${statement}\n\nURI: ${window.location.origin}\n` +
    `Nonce: ${nonce}\nIssued At: ${new Date().toISOString().replace(/\.\d{3}Z$/, "Z")}`;
  const encoded = new TextEncoder().encode(message);
  const signed = await provider.signMessage(encoded, "utf8");
  const b58 = await bs58();
  const signature = b58.encode(signed.signature || signed);
  return postJson("/api/wallets/link", {
    address,
    label: label || "Solana Wallet",
    chain_type: "solana",
    message,
    signature,
    nonce,
  });
}

// Expose to window for inline onclick handlers in templates
window.signInWithEthereum = signInWithEthereum;
window.signInWithSolana = signInWithSolana;
window.linkEvmWallet = linkEvmWallet;
window.linkSolanaWallet = linkSolanaWallet;
