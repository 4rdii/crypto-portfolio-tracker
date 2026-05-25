/**
 * Tiny fetch wrapper for the FastAPI backend.
 *
 * All calls go out with `credentials: 'include'` so the signed
 * portfolio_session cookie rides along, and we surface backend error
 * bodies as Error messages so the UI can show something useful instead
 * of generic "failed" strings.
 */

export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
    this.detail = detail
    this.name = 'ApiError'
  }
}

async function request<T = any>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(path, {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(init.headers || {}),
    },
    ...init,
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body && typeof body.detail === 'string') detail = body.detail
    } catch {
      /* not json, keep default */
    }
    throw new ApiError(res.status, detail)
  }
  // 204 No Content
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const api = {
  get: <T = any>(path: string) => request<T>(path, { method: 'GET' }),
  post: <T = any>(path: string, body?: any) =>
    request<T>(path, {
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  put: <T = any>(path: string, body?: any) =>
    request<T>(path, {
      method: 'PUT',
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  delete: <T = any>(path: string) => request<T>(path, { method: 'DELETE' }),
}

// ---------- Types (loose — backend returns snake_case, we pass through) ----

export interface MeResponse {
  user: {
    id: number
    primary_address: string
    chain_type: string
    display_name: string | null
    created_at: string
    last_login_at: string
  }
  wallet_count: number
}

export interface WalletRow {
  id: number
  label: string
  address: string
  chain: string
  created_at?: string
  last_scanned_at?: string | null
  verified?: boolean
  [k: string]: any
}

// Backend row from /api/holdings and inside /api/dashboard. Field names match
// the Python payload exactly — don't rename them here without also updating
// the FastAPI side.
export interface HoldingWalletBreakdown {
  wallet_id: number
  label: string
  address: string
  chain: string
  balance: number
  value: number
  protocol: string
}

export interface HoldingRow {
  symbol: string
  name?: string
  chains?: string | null
  protocols?: string | null
  wallet_breakdown?: HoldingWalletBreakdown[]
  balance: number
  usd_price: number
  current_value: number
  avg_cost: number
  cost_basis_value: number
  unrealized_pnl: number
  unrealized_pnl_pct: number
  realized_pnl: number
  price_24h_change?: number
  has_cost_basis?: boolean
  closed?: boolean
  [k: string]: any
}

export interface PortfolioSummary {
  total_value: number
  total_cost_basis: number
  unrealized_pnl: number
  unrealized_pnl_pct: number
  realized_pnl: number
  total_pnl?: number
}

export interface DashboardResponse {
  summary: PortfolioSummary
  allocation: Array<{ symbol: string; value: number; protocols?: string }>
  chain_split: Array<{ chain: string; value: number }>
  token_split: Array<{ symbol: string; value: number }>
  snapshots: Array<{ ts: string; total_usd: number }>
  history_source: string
  wallet_count: number
  holdings_count: number
}

export interface HoldingsResponse {
  holdings: HoldingRow[]
  summary: PortfolioSummary
  wallets: Array<{ id: number; label: string; address: string; chain: string }>
  filter_active: boolean
}

export interface TransactionRow {
  id: number
  ts: string
  tx_type: string
  symbol: string
  amount: number
  price_usd: number
  wallet_id: number | null
  wallet_label?: string | null
  notes: string | null
  [k: string]: any
}

// Row shape from /api/billing/summary. The pay-per-import model logs each
// import payment to credit_transactions with kind='import_payment' and a
// negative delta_usd — no balance tracking. `created_at` / `delta_usd` are
// snake_case because the backend passes through row_to_dict without
// camelCasing.
export interface ImportPaymentRow {
  id: number
  kind: string
  delta_usd: number
  chain: string | null
  tx_hash: string | null
  from_address: string | null
  token_symbol: string | null
  token_amount: number | null
  notes: string | null
  created_at: string
  [k: string]: any
}

export interface BillingSummary {
  treasury_address: string
  accepted_tokens: string[]
  pricing: {
    per_tx: number
    min_charge: number
    free_allowance: number
    free_allowance_remaining: number
    delta_multiplier: number
  }
  transactions: ImportPaymentRow[]
}

// /api/wallets/{id}/estimate-import — per-wallet quote (no balance).
export interface ImportQuote {
  address: string
  chains: Array<{ chain: string; tx_count: number }>
  total_tx: number
  already_imported: number
  is_delta: boolean
  free_tx_applied: number
  chargeable_tx: number
  cost_usd: number
  price_per_tx: number
  min_charge: number
  free_allowance: number
  [k: string]: any
}

// /api/wallets/estimate-import-all — aggregate quote across all EVM wallets.
export interface BatchImportQuote {
  wallets: Array<{
    wallet_id: number
    label: string
    address: string
    total_tx: number
    chargeable_tx: number
    cost_usd: number
    is_delta: boolean
  }>
  wallet_count: number
  total_tx: number
  cost_usd: number
  price_per_tx: number
  min_charge: number
  free_allowance: number
  free_allowance_remaining_before?: number
  free_allowance_remaining_after?: number
}

// Payload shared by single and batch import bodies — carries the signed
// on-chain tx proving the user paid for this specific import.
export interface ImportPayDetails {
  chain: string
  tx_hash: string
  expected_token: string
  expected_usd: number
}

export interface PayTokenConfig {
  treasury_address: string
  chains: Array<{
    chain: string
    chain_id: number
    display: string
    tokens: Array<{ symbol: string; contract: string; decimals: number }>
  }>
}

// ---------- Tax report types ----------

export interface TaxJurisdictionInfo {
  code: string
  name: string
  methods: string[]
  tax_year_end: string
}

export interface TaxStatus {
  unlocked: boolean
  unlocked_at: string | null
  price_usd: number
  jurisdictions: TaxJurisdictionInfo[]
}

export interface TaxCompareRow {
  method: string
  short_term_gain?: number
  long_term_gain?: number
  total_gain?: number
  taxable_gain?: number
  disposal_count?: number
  error?: string
}

export interface TaxCompareResponse {
  year: number
  jurisdiction: string
  tx_count: number
  results: TaxCompareRow[]
}

export interface TaxDisposal {
  symbol: string
  acquired_ts: string
  disposed_ts: string
  amount: number
  cost_basis: number
  proceeds: number
  gain: number
  holding_days: number
  long_term: boolean
  source_tx_id: number | null
  lot_id: number | null
}

export interface TaxLotSnapshotLot {
  lot_id: number
  acquired_ts: string
  remaining: number
  cost_per_unit: number
  original_amount: number
  source_tx_id: number | null
}

export interface TaxLotSnapshot {
  sell_tx_id: number
  symbol: string
  disposed_ts: string
  amount: number
  proceeds_usd: number
  open_lots: TaxLotSnapshotLot[]
}

export interface TaxLotSnapshotsResponse {
  tax_year: number
  jurisdiction: string
  snapshots: TaxLotSnapshot[]
}

// { "12345": { "7": 0.5 } } — outer key is sell_tx_id, inner is lot_id
export interface TaxOverridesResponse {
  overrides: Record<string, Record<string, number>>
}

export interface TaxReportResponse {
  jurisdiction_code: string
  jurisdiction_name: string
  method: string
  tax_year: number
  currency: string
  short_term_proceeds: number
  short_term_cost_basis: number
  short_term_gain: number
  long_term_proceeds: number
  long_term_cost_basis: number
  long_term_gain: number
  total_proceeds: number
  total_cost_basis: number
  total_gain: number
  discount_applied_pct: number
  taxable_gain: number
  fx_rate_to_local: number
  fx_is_fresh: boolean
  short_term_gain_local: number
  long_term_gain_local: number
  total_gain_local: number
  taxable_gain_local: number
  disposal_count: number
  asset_count: number
  disposals: TaxDisposal[]
  caveats: string[]
  generated_at: string
}
