import { LayoutDashboard, Wallet, Receipt, CreditCard, User, FileText, PiggyBank, RefreshCw } from 'lucide-react';

interface NavigationProps {
  currentScreen: string;
  onNavigate: (screen: string) => void;
}

export function Navigation({ currentScreen, onNavigate }: NavigationProps) {
  const links = [
    { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
    { id: 'holdings', label: 'Holdings', icon: Wallet },
    { id: 'transactions', label: 'Transactions', icon: Receipt },
    { id: 'wallets', label: 'Wallets', icon: Wallet },
    { id: 'rebalance', label: 'Rebalance', icon: RefreshCw },
    { id: 'tax', label: 'Tax Reports', icon: FileText },
    { id: 'fund', label: 'Fund', icon: PiggyBank },
    { id: 'billing', label: 'Billing', icon: CreditCard },
  ];

  return (
    <nav className="w-64 h-screen bg-[#0d1117] border-r border-[#30363d] flex flex-col">
      <div className="p-6 border-b border-[#30363d]">
        <h1 className="text-xl font-semibold text-[#e6edf3]">Portfolio</h1>
      </div>

      <div className="flex-1 p-4 space-y-1">
        {links.map((link) => {
          const Icon = link.icon;
          const isActive = currentScreen === link.id;

          return (
            <button
              key={link.id}
              onClick={() => onNavigate(link.id)}
              className={`w-full flex items-center gap-3 px-4 py-2.5 rounded-lg transition-colors ${
                isActive
                  ? 'bg-[#00d4ff]/10 text-[#00d4ff]'
                  : 'text-[#8b949e] hover:text-[#e6edf3] hover:bg-[#1c2128]'
              }`}
            >
              <Icon size={18} />
              <span>{link.label}</span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}
