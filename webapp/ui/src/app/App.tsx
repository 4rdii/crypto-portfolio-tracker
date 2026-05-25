import { useEffect, useState, useCallback } from 'react'
import { Landing } from './components/landing'
import { Navigation } from './components/navigation'
import { Topbar } from './components/topbar'
import { Dashboard } from './components/dashboard'
import { Holdings } from './components/holdings'
import { Transactions } from './components/transactions'
import { Wallets } from './components/wallets'
import { Billing } from './components/billing'
import { Tax } from './components/tax'
import { Profile } from './components/profile'
import { Fund } from './components/fund'
import { Rebalance } from './components/rebalance'
import { api, MeResponse, ApiError } from './lib/api'

type Screen = 'dashboard' | 'holdings' | 'transactions' | 'wallets' | 'billing' | 'tax' | 'fund' | 'profile' | 'rebalance'

const SCREENS: Screen[] = ['dashboard', 'holdings', 'transactions', 'wallets', 'billing', 'tax', 'fund', 'profile', 'rebalance']

function screenFromPath(): Screen {
  const path = window.location.pathname.replace(/\/$/, '')
  const last = path.split('/').pop() || 'dashboard'
  if ((SCREENS as string[]).includes(last)) return last as Screen
  return 'dashboard'
}

export default function App() {
  const [authState, setAuthState] = useState<'loading' | 'anon' | 'authed'>('loading')
  const [me, setMe] = useState<MeResponse | null>(null)
  const [currentScreen, setCurrentScreen] = useState<Screen>(screenFromPath())

  // Probe /api/auth/me on mount to resolve the session state.
  useEffect(() => {
    let mounted = true
    api
      .get<MeResponse>('/api/auth/me')
      .then((data) => {
        if (!mounted) return
        setMe(data)
        setAuthState('authed')
      })
      .catch((err) => {
        if (!mounted) return
        if (err instanceof ApiError && err.status === 401) {
          setAuthState('anon')
        } else {
          // Surface other errors but still drop to anon so the user can retry
          console.error('auth probe failed', err)
          setAuthState('anon')
        }
      })
    return () => {
      mounted = false
    }
  }, [])

  // Keep the browser URL in sync with the active screen so copy-paste +
  // back/forward work naturally. We push a state per navigation and listen
  // for popstate so the browser's back button actually changes tabs.
  const navigate = useCallback((screen: string) => {
    const next = (SCREENS as string[]).includes(screen) ? (screen as Screen) : 'dashboard'
    setCurrentScreen(next)
    const target = `/${next}`
    if (window.location.pathname !== target) {
      window.history.pushState({ screen: next }, '', target)
    }
  }, [])

  useEffect(() => {
    const onPop = () => setCurrentScreen(screenFromPath())
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const handleSignOut = useCallback(async () => {
    try {
      await api.post('/api/auth/logout')
    } catch (err) {
      console.error('sign-out failed', err)
    }
    setMe(null)
    setAuthState('anon')
    window.history.pushState({}, '', '/')
  }, [])

  const handleConnected = useCallback(() => {
    // wallet-auth.js already redirects to /dashboard on success, but in case
    // we ever re-enter the SPA without a hard navigation, reload the session.
    api
      .get<MeResponse>('/api/auth/me')
      .then((data) => {
        setMe(data)
        setAuthState('authed')
        navigate('dashboard')
      })
      .catch(() => {
        /* keep current state */
      })
  }, [navigate])

  if (authState === 'loading') {
    return (
      <div className="h-screen flex items-center justify-center bg-[#0a0e14]">
        <div className="text-[#8b949e] text-sm">Loading…</div>
      </div>
    )
  }

  if (authState === 'anon' || !me) {
    return <Landing onConnected={handleConnected} />
  }

  return (
    <div className="h-screen flex bg-[#0a0e14]">
      <Navigation currentScreen={currentScreen} onNavigate={navigate} />
      <div className="flex-1 flex flex-col min-w-0">
        <Topbar
          walletAddress={me.user.primary_address}
          onNavigate={navigate}
          onSignOut={handleSignOut}
        />
        {currentScreen === 'dashboard' && <Dashboard />}
        {currentScreen === 'holdings' && <Holdings />}
        {currentScreen === 'transactions' && <Transactions />}
        {currentScreen === 'wallets' && <Wallets />}
        {currentScreen === 'rebalance' && <Rebalance />}
        {currentScreen === 'billing' && <Billing />}
        {currentScreen === 'tax' && <Tax />}
        {currentScreen === 'fund' && <Fund />}
        {currentScreen === 'profile' && (
          <Profile
            me={me}
            onSignOut={handleSignOut}
            onNavigate={navigate}
          />
        )}
      </div>
    </div>
  )
}
