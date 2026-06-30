import { ReactNode } from 'react'
import { Link, useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../contexts/auth'
import { useTheme } from '../contexts/theme'
import { api } from '../api'

function SunIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="5" />
      <line x1="12" y1="1" x2="12" y2="3" />
      <line x1="12" y1="21" x2="12" y2="23" />
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
      <line x1="1" y1="12" x2="3" y2="12" />
      <line x1="21" y1="12" x2="23" y2="12" />
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
    </svg>
  )
}

function MoonIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  )
}

export default function Layout({ children }: { children: ReactNode }) {
  const { user, setUser } = useAuth()
  const { dark, toggle } = useTheme()
  const navigate = useNavigate()
  const location = useLocation()

  async function handleLogout() {
    await api.auth.logout().catch(() => {})
    setUser(null)
    navigate('/login')
  }

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-zinc-950">
      <nav className="bg-white border-b border-slate-200 dark:bg-zinc-900 dark:border-zinc-700">
        <div className="max-w-6xl mx-auto px-4 h-14 flex items-center justify-between">
          <div className="flex items-center gap-6">
            <Link to="/" className="flex items-center gap-2">
              <img src="/icon.png" alt="" className="w-7 h-7 object-contain" />
              <span className="font-semibold text-slate-900 dark:text-zinc-100 text-lg tracking-tight">Photoswitch</span>
            </Link>
            <Link
              to="/"
              className={`text-sm font-medium ${location.pathname === '/' ? 'text-red-600 dark:text-red-400' : 'text-slate-500 hover:text-slate-900 dark:text-zinc-400 dark:hover:text-zinc-100'}`}
            >
              Dashboard
            </Link>
            {user?.role === 'admin' && (
              <Link
                to="/admin"
                className={`text-sm font-medium ${location.pathname === '/admin' ? 'text-red-600 dark:text-red-400' : 'text-slate-500 hover:text-slate-900 dark:text-zinc-400 dark:hover:text-zinc-100'}`}
              >
                Admin
              </Link>
            )}
          </div>
          <div className="flex items-center gap-4">
            <Link to="/profile" className="text-sm text-slate-600 dark:text-zinc-300 hover:text-slate-900 dark:hover:text-zinc-100">
              {user?.username}
            </Link>
            {user?.role === 'admin' && (
              <span className="text-xs bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400 px-2 py-0.5 rounded-full font-medium">
                admin
              </span>
            )}
            <button
              onClick={toggle}
              title={dark ? 'Switch to light mode' : 'Switch to dark mode'}
              className="text-slate-400 hover:text-slate-700 dark:text-zinc-500 dark:hover:text-zinc-200"
            >
              {dark ? <SunIcon /> : <MoonIcon />}
            </button>
            <button
              onClick={handleLogout}
              className="text-sm text-slate-500 hover:text-slate-900 dark:text-zinc-400 dark:hover:text-zinc-100"
            >
              Sign out
            </button>
          </div>
        </div>
      </nav>
      <main className="max-w-6xl mx-auto px-4 py-8">{children}</main>
    </div>
  )
}
