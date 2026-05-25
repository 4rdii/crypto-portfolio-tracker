import { useState, useEffect } from 'react';
import { RefreshCw, Save, Trash2, Plus, TrendingUp } from 'lucide-react';

// Safe number formatter - returns "0.00" if value is not a valid number
const safeFixed = (value: any, decimals: number = 2): string => {
  const num = typeof value === 'number' ? value : parseFloat(value);
  return (!isNaN(num) && isFinite(num)) ? num.toFixed(decimals) : '0.' + '0'.repeat(decimals);
};

interface Strategy {
  id: number;
  name: string;
  allocations: Record<string, number>;
  is_preset?: boolean;
}

interface Holding {
  symbol: string;
  usd_value: number;
}

interface SwapQuote {
  from_symbol:   string;
  from_chain:    string;
  from_contract: string | null;
  to_symbol:     string;
  to_chain:      string;
  input_amount:  number;
  output_amount: number;
  price_impact:  number;
  fee_usd:       number;
  route_id?:     string;
  error?:        string;
}

// Inline notification banner (replaces alert/confirm dialogs per project convention)
function Banner({
  message,
  type,
  onDismiss,
}: {
  message: string;
  type: 'success' | 'error' | 'warn' | 'info';
  onDismiss: () => void;
}) {
  const colors: Record<string, string> = {
    success: 'bg-[#238636]/10 border-[#238636] text-[#3fb950]',
    error: 'bg-[#f85149]/10 border-[#f85149] text-[#f85149]',
    warn: 'bg-[#d29922]/10 border-[#d29922] text-[#d29922]',
    info: 'bg-[#00d4ff]/10 border-[#00d4ff] text-[#00d4ff]',
  };
  return (
    <div className={`p-3 border rounded-lg text-sm flex items-start justify-between gap-3 ${colors[type]}`}>
      <span>{message}</span>
      <button onClick={onDismiss} className="shrink-0 opacity-60 hover:opacity-100 text-xs">✕</button>
    </div>
  );
}

// Inline confirm dialog (replaces browser confirm() per project convention)
function ConfirmDialog({
  message,
  onConfirm,
  onCancel,
}: {
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-6 max-w-sm w-full mx-4 space-y-4">
        <p className="text-[#e6edf3]">{message}</p>
        <div className="flex gap-3 justify-end">
          <button
            onClick={onCancel}
            className="px-4 py-2 bg-[#21262d] text-[#8b949e] rounded-lg hover:bg-[#30363d]"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="px-4 py-2 bg-[#f85149] text-white rounded-lg hover:bg-[#da3633]"
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}

// Inline symbol input (replaces browser prompt() per project convention)
function AddAssetDialog({
  onAdd,
  onCancel,
}: {
  onAdd: (symbol: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState('');
  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-6 max-w-sm w-full mx-4 space-y-4">
        <p className="text-[#e6edf3] font-semibold">Add Asset</p>
        <input
          type="text"
          placeholder="Symbol, e.g. BTC, USDC, NVDAx"
          value={value}
          onChange={(e) => setValue(e.target.value.toUpperCase())}
          onKeyDown={(e) => e.key === 'Enter' && value.trim() && onAdd(value.trim())}
          className="w-full px-4 py-2 bg-[#0d1117] border border-[#30363d] rounded-lg text-[#e6edf3]"
          autoFocus
          maxLength={32}
        />
        <div className="flex gap-3 justify-end">
          <button
            onClick={onCancel}
            className="px-4 py-2 bg-[#21262d] text-[#8b949e] rounded-lg hover:bg-[#30363d]"
          >
            Cancel
          </button>
          <button
            onClick={() => value.trim() && onAdd(value.trim())}
            className="px-4 py-2 bg-[#238636] text-white rounded-lg hover:bg-[#2ea043]"
          >
            Add
          </button>
        </div>
      </div>
    </div>
  );
}

export function Rebalance() {
  const [strategies, setStrategies] = useState<{ presets: Strategy[]; custom: Strategy[] }>({
    presets: [],
    custom: [],
  });
  const [selectedStrategy, setSelectedStrategy] = useState<Strategy | null>(null);
  const [targetAllocations, setTargetAllocations] = useState<Record<string, number>>({});
  const [currentHoldings, setCurrentHoldings] = useState<Holding[]>([]);
  const [totalValue, setTotalValue] = useState(0);
  const [swapsNeeded, setSwapsNeeded] = useState<any[]>([]);
  const [swapPairs, setSwapPairs] = useState<any[]>([]);
  const [execSteps, setExecSteps] = useState<{label: string; status: 'pending'|'active'|'done'|'error'}[]>([]);
  const [quotes, setQuotes] = useState<SwapQuote[]>([]);
  const [loading, setLoading] = useState(false);
  const [calcError, setCalcError] = useState<string | null>(null);
  const [showCustomEditor, setShowCustomEditor] = useState(false);
  const [customName, setCustomName] = useState('');
  // Modal state — replaces browser alert/confirm/prompt
  const [banner, setBanner] = useState<{ message: string; type: 'success' | 'error' | 'warn' | 'info' } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);
  const [showAddAsset, setShowAddAsset] = useState(false);

  const notify = (message: string, type: 'success' | 'error' | 'warn' | 'info' = 'info') => {
    setBanner({ message, type });
  };

  // Fetch strategies on mount
  useEffect(() => {
    fetchStrategies();
    fetchHoldings();
  }, []);

  const fetchStrategies = async () => {
    try {
      const res = await fetch('/api/rebalance/strategies', { credentials: 'include' });
      if (!res.ok) {
        console.error('Failed to fetch strategies:', res.status);
        return;
      }
      const data = await res.json();
      setStrategies(data);
    } catch (err) {
      console.error('Failed to fetch strategies:', err);
    }
  };

  const fetchHoldings = async () => {
    try {
      const res = await fetch('/api/holdings', { credentials: 'include' });
      if (!res.ok) {
        console.error('Failed to fetch holdings:', res.status);
        return;
      }
      const data = await res.json();

      const holdingsMap: Record<string, number> = {};
      let total = 0;

      data.holdings?.forEach((h: any) => {
        // API returns usd_value; guard against legacy current_value key
        const value = parseFloat(h.usd_value ?? h.current_value) || 0;
        holdingsMap[h.symbol] = (holdingsMap[h.symbol] || 0) + value;
        total += value;
      });

      setCurrentHoldings(
        Object.entries(holdingsMap).map(([symbol, usd_value]) => ({ symbol, usd_value }))
      );
      setTotalValue(total);
    } catch (err) {
      console.error('Failed to fetch holdings:', err);
    }
  };

  const selectStrategy = (strategy: Strategy) => {
    setSelectedStrategy(strategy);
    setTargetAllocations(JSON.parse(JSON.stringify(strategy.allocations)));
    setSwapsNeeded([]);
    setSwapPairs([]);
    setQuotes([]);
    setExecSteps([]);
    setCalcError(null);
  };

  const calculateRebalance = async () => {
    if (!targetAllocations || Object.keys(targetAllocations).length === 0) {
      setCalcError('Please select or define a strategy first');
      return;
    }

    setLoading(true);
    setCalcError(null);
    try {
      const res = await fetch('/api/rebalance/calculate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          target_allocations: targetAllocations,
          total_value_usd: totalValue,
        }),
      });

      const data = await res.json();

      if (!res.ok) {
        setCalcError(data.detail || `API error: ${res.status}`);
        return;
      }

      const swaps = data.swaps_needed || [];
      setSwapsNeeded(swaps);
      setSwapPairs(data.swap_pairs || []);
      if (swaps.length === 0) {
        setCalcError('Portfolio is already balanced — no swaps needed.');
      }
    } catch (err) {
      console.error('Failed to calculate rebalance:', err);
      setCalcError('Network error — could not reach the server.');
    } finally {
      setLoading(false);
    }
  };

  const getQuotes = async () => {
    if (swapPairs.length === 0) {
      notify('Calculate rebalance first', 'warn');
      return;
    }

    setLoading(true);
    try {
      // Use server-generated swap pairs (chain + contract from actual holdings)
      const swaps = swapPairs.map((p) => ({
        from_symbol:   p.from_symbol,
        from_chain:    p.from_chain    || 'SOLANA',
        from_contract: p.from_contract || null,
        to_symbol:     p.to_symbol,
        to_chain:      p.to_chain      || p.from_chain || 'SOLANA',
        amount:        p.from_amount,   // actual token amount, NOT USD
      }));

      const res = await fetch('/api/rebalance/quote', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          swaps,
          slippage: 1.0,
        }),
      });

      const data = await res.json();

      if (!res.ok) {
        notify(data.detail || `Failed to get quotes (${res.status})`, 'error');
        return;
      }

      setQuotes(data.quotes || []);
    } catch (err) {
      console.error('Failed to get quotes:', err);
      notify('Failed to get swap quotes — network error', 'error');
    } finally {
      setLoading(false);
    }
  };

  const saveCustomStrategy = async () => {
    if (!customName.trim() || Object.keys(targetAllocations).length === 0) {
      notify('Please provide a name and at least one allocation', 'warn');
      return;
    }

    try {
      const res = await fetch('/api/rebalance/strategies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          name: customName.trim(),
          allocations: targetAllocations,
        }),
      });

      if (res.ok) {
        notify('Strategy saved!', 'success');
        setShowCustomEditor(false);
        setCustomName('');
        fetchStrategies();
      } else {
        const error = await res.json();
        notify(`Failed to save: ${error.detail || 'Unknown error'}`, 'error');
      }
    } catch (err) {
      console.error('Failed to save strategy:', err);
      notify('Failed to save strategy — network error', 'error');
    }
  };

  const deleteStrategy = async (id: number) => {
    // confirmDelete state triggers the ConfirmDialog; actual delete runs here
    try {
      const res = await fetch(`/api/rebalance/strategies/${id}`, {
        method: 'DELETE',
        credentials: 'include',
      });

      if (res.ok) {
        fetchStrategies();
        if (selectedStrategy?.id === id) {
          setSelectedStrategy(null);
          setTargetAllocations({});
        }
      } else {
        notify('Failed to delete strategy', 'error');
      }
    } catch (err) {
      console.error('Failed to delete strategy:', err);
      notify('Failed to delete strategy — network error', 'error');
    }
  };

  const updateAllocation = (symbol: string, value: number) => {
    setTargetAllocations((prev) => ({
      ...prev,
      [symbol]: value,
    }));
  };

  const addNewAsset = () => {
    setShowAddAsset(true);
  };

  const handleAddAsset = (symbol: string) => {
    setShowAddAsset(false);
    const clean = symbol.toUpperCase().trim().replace(/[^A-Z0-9]/g, '').slice(0, 32);
    if (!clean) return;
    setTargetAllocations((prev) => ({
      ...prev,
      [clean]: prev[clean] ?? 0,
    }));
  };

  const removeAsset = (symbol: string) => {
    setTargetAllocations((prev) => {
      const copy = { ...prev };
      delete copy[symbol];
      return copy;
    });
  };

  const totalAllocation = Object.values(targetAllocations).reduce((sum, val) => sum + val, 0);

  const executeRebalance = async () => {
    if (quotes.length === 0 || quotes.some(q => q.error)) {
      notify('Please get valid quotes first', 'warn');
      return;
    }

    setLoading(true);
    try {
      // Build transactions via backend
      const res = await fetch('/api/rebalance/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          quotes: quotes,
          strategy_id: selectedStrategy?.id || null,
        }),
      });

      const data = await res.json();

      if (!res.ok) {
        notify(data.detail || 'Failed to build transactions', 'error');
        return;
      }

      const EVM_CHAINS = new Set(['ETH', 'BSC', 'MATIC', 'AVAX', 'ARB', 'OP', 'FTM', 'BASE']);
      const isEvm = (chain: string) => EVM_CHAINS.has((chain || '').toUpperCase());

      // ── helpers ────────────────────────────────────────────────────────────
      const waitForTx = async (provider: any, txHash: string): Promise<void> => {
        for (let i = 0; i < 80; i++) {
          const receipt = await provider.request({
            method: 'eth_getTransactionReceipt', params: [txHash],
          });
          if (receipt) {
            if (receipt.status === '0x0') throw new Error('Transaction reverted on-chain');
            return;
          }
          await new Promise(r => setTimeout(r, 3000));
        }
        throw new Error('Confirmation timeout (4 min)');
      };

      const getERC20Allowance = async (provider: any, token: string, owner: string, spender: string): Promise<bigint> => {
        const data = '0xdd62ed3e'
          + owner.slice(2).toLowerCase().padStart(64, '0')
          + spender.slice(2).toLowerCase().padStart(64, '0');
        const res = await provider.request({ method: 'eth_call', params: [{ to: token, data }, 'latest'] });
        return BigInt(res || '0x0');
      };
      // ──────────────────────────────────────────────────────────────────────

      // Build step labels for the progress display
      const steps: {label: string; status: 'pending'|'active'|'done'|'error'}[] = [];
      for (const txData of data.transactions) {
        const label = `${txData.from_symbol} → ${txData.to_symbol}`;
        if (isEvm(txData.from_chain) && txData.approve_to && txData.approve_data) {
          steps.push({ label: `Approve ${txData.from_symbol}`, status: 'pending' });
        }
        steps.push({ label: `Swap ${label}`, status: 'pending' });
      }
      setExecSteps(steps);

      const setStep = (idx: number, status: 'pending'|'active'|'done'|'error') =>
        setExecSteps(prev => prev.map((s, i) => i === idx ? { ...s, status } : s));

      // Sign and send each transaction
      const txHashes: string[] = [];
      let allSuccess = true;
      let stepIdx = 0;

      for (const txData of data.transactions) {
        try {
          let txHash: string;

          if (isEvm(txData.from_chain)) {
            // ── EVM chain ──────────────────────────────────────────────────
            if (!(window as any).ethereum) {
              notify('EVM wallet not detected. Please install MetaMask or Rabby.', 'warn');
              allSuccess = false; break;
            }
            const evmProvider = (window as any).ethereum;
            const accounts: string[] = await evmProvider.request({ method: 'eth_accounts' });
            const from = accounts[0];
            if (!from) { notify('EVM wallet not connected.', 'warn'); allSuccess = false; break; }

            // 1. ERC-20 approval (if token needs it)
            if (txData.approve_to && txData.approve_data && txData.spender) {
              const allowance = await getERC20Allowance(evmProvider, txData.approve_to, from, txData.spender);
              const needed = BigInt(txData.from_amount_raw || '0');
              if (allowance < needed) {
                setStep(stepIdx, 'active');
                const approveTxHash = await evmProvider.request({
                  method: 'eth_sendTransaction',
                  params: [{ from, to: txData.approve_to, data: txData.approve_data }],
                });
                await waitForTx(evmProvider, approveTxHash);
                setStep(stepIdx, 'done');
              } else {
                // Already approved — skip
                setStep(stepIdx, 'done');
              }
              stepIdx++;
            }

            // 2. Swap tx
            setStep(stepIdx, 'active');
            const evmTx = txData.evm_tx || {};
            txHash = await evmProvider.request({
              method: 'eth_sendTransaction',
              params: [{
                from,
                to:                   evmTx.to,
                data:                 evmTx.data,
                value:                evmTx.value || '0x0',
                gas:                  evmTx.gas   || undefined,
                maxFeePerGas:         evmTx.maxFeePerGas         ? '0x' + Number(evmTx.maxFeePerGas).toString(16)         : undefined,
                maxPriorityFeePerGas: evmTx.maxPriorityFeePerGas ? '0x' + Number(evmTx.maxPriorityFeePerGas).toString(16) : undefined,
              }],
            });
            await waitForTx(evmProvider, txHash);

          } else {
            // ── Solana ─────────────────────────────────────────────────────
            if (!(window as any).solana) {
              notify('Solana wallet not detected. Please install Phantom or Solflare.', 'warn');
              allSuccess = false; break;
            }
            setStep(stepIdx, 'active');
            const solWallet = (window as any).solana;
            const bytes = Uint8Array.from(atob(txData.serialized_tx), c => c.charCodeAt(0));
            const result = await solWallet.signAndSendTransaction({ message: bytes });
            txHash = result.signature || result.txid || result;
          }

          setStep(stepIdx, 'done');
          stepIdx++;
          txHashes.push(txHash);
          console.log(`Swap ${txData.from_symbol} → ${txData.to_symbol} confirmed:`, txHash);
        } catch (err) {
          setStep(stepIdx, 'error');
          console.error(`Failed to execute swap ${txData.from_symbol} → ${txData.to_symbol}:`, err);
          allSuccess = false;
          break;
        }
      }

      // Confirm execution on backend
      const status = allSuccess ? 'success' : (txHashes.length > 0 ? 'partial' : 'failed');

      await fetch('/api/rebalance/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          rebalance_id: data.rebalance_id,
          tx_hashes: txHashes,
          status: status,
        }),
      });

      if (allSuccess) {
        notify(`Rebalance executed successfully! ${txHashes.length} swap${txHashes.length !== 1 ? 's' : ''} completed.`, 'success');
        fetchHoldings();
      } else if (txHashes.length > 0) {
        notify(`Partial success: ${txHashes.length} out of ${data.transactions.length} swaps completed.`, 'warn');
      } else {
        notify('Rebalance failed. Please try again.', 'error');
      }
    } catch (err) {
      console.error('Failed to execute rebalance:', err);
      notify(`Execution failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex-1 p-8 space-y-6 overflow-y-auto bg-[#0d1117]">
      {/* Modals */}
      {confirmDelete !== null && (
        <ConfirmDialog
          message="Delete this strategy? This cannot be undone."
          onConfirm={() => {
            const id = confirmDelete;
            setConfirmDelete(null);
            deleteStrategy(id);
          }}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
      {showAddAsset && (
        <AddAssetDialog
          onAdd={handleAddAsset}
          onCancel={() => setShowAddAsset(false)}
        />
      )}

      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-[#e6edf3]">Portfolio Rebalancer</h2>
          <p className="text-sm text-[#8b949e] mt-1">
            Define target allocations and execute multi-asset swaps atomically
          </p>
        </div>
        <button
          onClick={() => setShowCustomEditor(!showCustomEditor)}
          className="flex items-center gap-2 px-4 py-2 bg-[#238636] text-white rounded-lg hover:bg-[#2ea043]"
        >
          <Plus size={16} />
          New Strategy
        </button>
      </div>

      {/* Notification banner */}
      {banner && (
        <Banner
          message={banner.message}
          type={banner.type}
          onDismiss={() => setBanner(null)}
        />
      )}

      {/* Current Portfolio Overview */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-6">
        <h3 className="text-lg font-semibold text-[#e6edf3] mb-4">Current Holdings</h3>
        <div className="grid grid-cols-3 gap-4">
          <div>
            <div className="text-sm text-[#8b949e]">Total Value</div>
            <div className="text-2xl font-bold text-[#e6edf3]">${safeFixed(totalValue, 2)}</div>
          </div>
          <div>
            <div className="text-sm text-[#8b949e]">Assets</div>
            <div className="text-2xl font-bold text-[#e6edf3]">{currentHoldings.length}</div>
          </div>
          <div>
            <div className="text-sm text-[#8b949e]">Largest Position</div>
            <div className="text-lg font-bold text-[#e6edf3]">
              {currentHoldings[0]?.symbol || 'N/A'}
            </div>
          </div>
        </div>
      </div>

      {/* Strategy Selector */}
      <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-6">
        <h3 className="text-lg font-semibold text-[#e6edf3] mb-4">Select Strategy</h3>

        <div className="space-y-4">
          <div>
            <div className="text-sm text-[#8b949e] mb-2">Preset Strategies</div>
            <div className="grid grid-cols-3 gap-3">
              {strategies.presets.map((strategy) => (
                <button
                  key={strategy.id}
                  onClick={() => selectStrategy(strategy)}
                  className={`p-4 rounded-lg border transition-all ${
                    selectedStrategy?.id === strategy.id
                      ? 'border-[#00d4ff] bg-[#00d4ff]/10'
                      : 'border-[#30363d] bg-[#0d1117] hover:border-[#8b949e]'
                  }`}
                >
                  <div className="text-[#e6edf3] font-medium">{strategy.name}</div>
                  <div className="text-xs text-[#8b949e] mt-1">
                    {Object.keys(strategy.allocations).length} assets
                  </div>
                </button>
              ))}
            </div>
          </div>

          {strategies.custom.length > 0 && (
            <div>
              <div className="text-sm text-[#8b949e] mb-2">Your Custom Strategies</div>
              <div className="grid grid-cols-3 gap-3">
                {strategies.custom.map((strategy) => (
                  <div key={strategy.id} className="relative">
                    <button
                      onClick={() => selectStrategy(strategy)}
                      className={`w-full p-4 rounded-lg border transition-all ${
                        selectedStrategy?.id === strategy.id
                          ? 'border-[#00d4ff] bg-[#00d4ff]/10'
                          : 'border-[#30363d] bg-[#0d1117] hover:border-[#8b949e]'
                      }`}
                    >
                      <div className="text-[#e6edf3] font-medium">{strategy.name}</div>
                      <div className="text-xs text-[#8b949e] mt-1">
                        {Object.keys(strategy.allocations).length} assets
                      </div>
                    </button>
                    <button
                      onClick={() => setConfirmDelete(strategy.id)}
                      className="absolute top-2 right-2 p-1 text-[#f85149] hover:bg-[#f85149]/10 rounded"
                      title="Delete strategy"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Custom Strategy Editor */}
      {showCustomEditor && (
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-6">
          <h3 className="text-lg font-semibold text-[#e6edf3] mb-4">Create Custom Strategy</h3>
          <input
            type="text"
            placeholder="Strategy name..."
            value={customName}
            onChange={(e) => setCustomName(e.target.value)}
            className="w-full px-4 py-2 bg-[#0d1117] border border-[#30363d] rounded-lg text-[#e6edf3] mb-4"
            maxLength={100}
          />
          <div className="flex gap-3">
            <button
              onClick={addNewAsset}
              className="px-4 py-2 bg-[#21262d] text-[#00d4ff] rounded-lg hover:bg-[#30363d] border border-[#30363d]"
            >
              + Add Asset
            </button>
            <button
              onClick={saveCustomStrategy}
              className="px-4 py-2 bg-[#238636] text-white rounded-lg hover:bg-[#2ea043]"
            >
              <Save size={16} className="inline mr-2" />
              Save Strategy
            </button>
          </div>
        </div>
      )}

      {/* Target Allocation Editor */}
      {Object.keys(targetAllocations).length > 0 && (
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-[#e6edf3]">Target Allocation</h3>
            <div className="flex items-center gap-4">
              <div className={`text-sm ${totalAllocation === 100 ? 'text-[#238636]' : 'text-[#f85149]'}`}>
                Total: {safeFixed(totalAllocation, 1)}%
              </div>
              <button
                onClick={addNewAsset}
                className="text-sm text-[#00d4ff] hover:underline"
              >
                + Add Asset
              </button>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            {Object.entries(targetAllocations).map(([symbol, percentage]) => (
              <div key={symbol} className="flex items-center gap-3 p-3 bg-[#0d1117] rounded-lg">
                <div className="flex-1">
                  <div className="text-[#e6edf3] font-medium">{symbol}</div>
                  <input
                    type="number"
                    value={percentage}
                    onChange={(e) => updateAllocation(symbol, parseFloat(e.target.value) || 0)}
                    className="w-full mt-1 px-2 py-1 bg-[#161b22] border border-[#30363d] rounded text-[#e6edf3] text-sm"
                    step="0.1"
                    min="0"
                    max="100"
                  />
                </div>
                <button
                  onClick={() => removeAsset(symbol)}
                  className="text-[#f85149] hover:bg-[#f85149]/10 p-2 rounded"
                  title={`Remove ${symbol}`}
                >
                  <Trash2 size={16} />
                </button>
              </div>
            ))}
          </div>

          {calcError && (
            <div className="mt-3 p-3 bg-[#f85149]/10 border border-[#f85149] rounded-lg text-[#f85149] text-sm">
              {calcError}
            </div>
          )}

          {totalAllocation < 99 || totalAllocation > 101 ? (
            <div className="mt-4 p-3 bg-[#d29922]/10 border border-[#d29922] rounded-lg text-[#d29922] text-sm">
              Allocations must sum to 100% before calculating. Currently at {safeFixed(totalAllocation, 1)}%.
            </div>
          ) : null}

          <button
            onClick={calculateRebalance}
            disabled={loading || totalAllocation < 99 || totalAllocation > 101}
            className="mt-4 w-full px-4 py-3 bg-[#00d4ff] text-black font-semibold rounded-lg hover:bg-[#00b8e6] disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <RefreshCw size={16} className="inline mr-2" />
            {loading ? 'Calculating...' : 'Calculate Required Swaps'}
          </button>
        </div>
      )}

      {/* Swap Preview */}
      {swapsNeeded.length > 0 && (
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-6">
          <h3 className="text-lg font-semibold text-[#e6edf3] mb-4">Required Swaps</h3>

          <div className="space-y-3">
            {swapsNeeded.map((swap, idx) => (
              <div key={idx} className="p-4 bg-[#0d1117] rounded-lg flex items-center justify-between">
                <div>
                  <div className="text-[#e6edf3] font-medium">
                    {swap.action === 'buy' ? '🟢' : '🔴'} {swap.action.toUpperCase()} {swap.symbol}
                  </div>
                  <div className="text-sm text-[#8b949e]">${safeFixed(swap.amount_usd, 2)}</div>
                </div>
                <TrendingUp size={18} className="text-[#00d4ff]" />
              </div>
            ))}
          </div>

          <button
            onClick={getQuotes}
            disabled={loading}
            className="mt-4 w-full px-4 py-3 bg-[#238636] text-white font-semibold rounded-lg hover:bg-[#2ea043] disabled:opacity-50"
          >
            Get Swap Quotes (Rango)
          </button>
        </div>
      )}

      {/* Quotes */}
      {quotes.length > 0 && (
        <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-6">
          <h3 className="text-lg font-semibold text-[#e6edf3] mb-4">Swap Quotes</h3>

          <div className="space-y-3">
            {quotes.map((quote, idx) => (
              <div key={idx} className="p-4 bg-[#0d1117] rounded-lg">
                {quote.error ? (
                  <div className="text-[#f85149]">Error: {quote.error}</div>
                ) : (
                  <div className="grid grid-cols-4 gap-4 text-sm">
                    <div>
                      <div className="text-[#8b949e]">From</div>
                      <div className="text-[#e6edf3] font-medium">{quote.from_symbol}</div>
                      {quote.from_chain && (
                        <div className="text-xs text-[#58a6ff] mt-0.5">{quote.from_chain}</div>
                      )}
                    </div>
                    <div>
                      <div className="text-[#8b949e]">To</div>
                      <div className="text-[#e6edf3] font-medium">{quote.to_symbol}</div>
                      {quote.to_chain && (
                        <div className="text-xs text-[#58a6ff] mt-0.5">{quote.to_chain}</div>
                      )}
                    </div>
                    <div>
                      <div className="text-[#8b949e]">Output</div>
                      <div className="text-[#e6edf3]">{safeFixed(quote.output_amount, 4)}</div>
                    </div>
                    <div>
                      <div className="text-[#8b949e]">Fee</div>
                      <div className="text-[#e6edf3]">${safeFixed(quote.fee_usd, 2)}</div>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>

          <button
            onClick={executeRebalance}
            disabled={loading}
            className="mt-4 w-full px-4 py-3 bg-[#238636] text-white font-semibold rounded-lg hover:bg-[#2ea043] disabled:opacity-50 flex items-center justify-center gap-2"
          >
            <TrendingUp size={18} />
            Execute Rebalance
          </button>

          {execSteps.length > 0 && (
            <div className="mt-4 p-4 bg-[#0d1117] border border-[#30363d] rounded-lg">
              <div className="text-sm font-medium text-[#e6edf3] mb-3">Execution Progress</div>
              <div className="space-y-2">
                {execSteps.map((step, i) => (
                  <div key={i} className="flex items-center gap-3 text-sm">
                    <span className="w-5 text-center">
                      {step.status === 'done'    && <span className="text-[#3fb950]">✓</span>}
                      {step.status === 'active'  && <span className="text-[#f0883e] animate-pulse">⏳</span>}
                      {step.status === 'error'   && <span className="text-[#f85149]">✗</span>}
                      {step.status === 'pending' && <span className="text-[#484f58]">○</span>}
                    </span>
                    <span className={
                      step.status === 'done'    ? 'text-[#3fb950]' :
                      step.status === 'active'  ? 'text-[#f0883e]' :
                      step.status === 'error'   ? 'text-[#f85149]' :
                      'text-[#484f58]'
                    }>{step.label}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="mt-3 p-4 bg-[#238636]/10 border border-[#238636] rounded-lg">
            <div className="text-xs text-[#8b949e]">
              Your connected wallet will be prompted to sign each transaction. EVM swaps (ETH, BSC…) use MetaMask/Rabby; Solana swaps use Phantom/Solflare.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
