import { Suspense, lazy } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { SignIn, logout, useAuth } from './components/Auth'
import Opportunities from './pages/Opportunities'
import Runs from './pages/Runs'

// recharts is ~150 kB gzipped -- more than everything else put together.
// The home screen is opened from a phone every morning and draws no charts,
// so the pages that do are split out and fetched on demand.
const ModelDetail = lazy(() => import('./pages/ModelDetail'))
const Fx = lazy(() => import('./pages/Fx'))
const Watchlist = lazy(() => import('./pages/Watchlist'))

const TABS = [
  { to: '/opportunities', label: 'Deals' },
  { to: '/models', label: 'Models' },
  { to: '/fx', label: 'FX' },
  { to: '/watchlist', label: 'Watchlist' },
  { to: '/runs', label: 'Health' },
]

export default function App() {
  const user = useAuth()

  if (user === undefined) {
    return <div className="p-8 text-center text-sm text-neutral-500">Checking session…</div>
  }
  if (!user) return <SignIn />

  return (
    <div className="min-h-screen pb-20">
      <header className="sticky top-0 z-20 border-b border-edge bg-ink-900/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
          <h1 className="text-sm font-semibold tracking-tight">nippon-margin</h1>
          <button className="text-xs text-neutral-500 hover:text-neutral-300" onClick={logout}>
            {user.email || 'sign out'}
          </button>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-3 py-4">
        <Suspense fallback={<p className="py-10 text-center text-sm text-neutral-500">Loading…</p>}>
        <Routes>
          <Route path="/" element={<Navigate to="/opportunities" replace />} />
          <Route path="/opportunities" element={<Opportunities />} />
          <Route path="/models" element={<ModelDetail />} />
          <Route path="/models/:key" element={<ModelDetail />} />
          <Route path="/fx" element={<Fx />} />
          <Route path="/watchlist" element={<Watchlist user={user} />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="*" element={<Navigate to="/opportunities" replace />} />
        </Routes>
        </Suspense>
      </main>

      {/* Bottom tab bar: this is checked from a phone every morning. */}
      <nav className="fixed inset-x-0 bottom-0 z-20 border-t border-edge bg-ink-800/95 backdrop-blur
                      pb-[env(safe-area-inset-bottom)]">
        <div className="mx-auto flex max-w-6xl">
          {TABS.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={({ isActive }) =>
                `flex-1 py-3 text-center text-xs ${
                  isActive ? 'text-neutral-100 font-semibold' : 'text-neutral-500'
                }`
              }
            >
              {tab.label}
            </NavLink>
          ))}
        </div>
      </nav>
    </div>
  )
}
