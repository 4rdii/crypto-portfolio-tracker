import { ChevronDown, LogOut } from 'lucide-react';
import { useState } from 'react';

interface TopbarProps {
  walletAddress: string;
  onNavigate: (screen: string) => void;
  onSignOut: () => void;
}

export function Topbar({ walletAddress, onNavigate, onSignOut }: TopbarProps) {
  const [showMenu, setShowMenu] = useState(false);

  const truncateAddress = (addr: string) => {
    return `${addr.slice(0, 6)}...${addr.slice(-4)}`;
  };

  return (
    <div className="h-16 bg-[#0d1117] border-b border-[#30363d] flex items-center justify-end px-6">
      <div className="relative">
        <button
          onClick={() => setShowMenu(!showMenu)}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-[#1c2128] hover:bg-[#21262d] transition-colors"
        >
          <div className="w-2 h-2 rounded-full bg-[#00ff88]" />
          <span className="text-[#e6edf3] font-mono text-sm">{truncateAddress(walletAddress)}</span>
          <ChevronDown size={16} className="text-[#8b949e]" />
        </button>

        {showMenu && (
          <>
            <div className="fixed inset-0" onClick={() => setShowMenu(false)} />
            <div className="absolute right-0 mt-2 w-48 bg-[#161b22] border border-[#30363d] rounded-lg shadow-xl overflow-hidden z-50">
              <button
                onClick={() => {
                  setShowMenu(false);
                  onNavigate('profile');
                }}
                className="w-full px-4 py-2.5 text-left text-[#e6edf3] hover:bg-[#1c2128] transition-colors flex items-center gap-2"
              >
                <LogOut size={16} />
                <span>Profile</span>
              </button>
              <button
                onClick={() => {
                  setShowMenu(false);
                  onSignOut();
                }}
                className="w-full px-4 py-2.5 text-left text-[#ff4757] hover:bg-[#1c2128] transition-colors flex items-center gap-2"
              >
                <LogOut size={16} />
                <span>Sign out</span>
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
