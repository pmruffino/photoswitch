import { FormEvent, useEffect, useState } from 'react'
import { Link, Navigate, useNavigate, useLocation } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../contexts/auth'
import { useTheme } from '../contexts/theme'

function SunIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="5" />
      <line x1="12" y1="1" x2="12" y2="3" /><line x1="12" y1="21" x2="12" y2="23" />
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" /><line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
      <line x1="1" y1="12" x2="3" y2="12" /><line x1="21" y1="12" x2="23" y2="12" />
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" /><line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
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

export default function Login() {
  const { user, loading, sessionExpired, dismissSessionExpired } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [rememberMe, setRememberMe] = useState(false)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [signupPolicy, setSignupPolicy] = useState<'open' | 'approval' | 'closed' | null>(null)

  useEffect(() => {
    api.auth.signupPolicy().then(r => setSignupPolicy(r.signup_policy)).catch(() => setSignupPolicy('open'))
  }, [])
  const { setUser } = useAuth()
  const { dark, toggle } = useTheme()
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: { pathname?: string } })?.from?.pathname ?? '/'

  if (!loading && user) return <Navigate to={from} replace />

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      const user = await api.auth.login(username, password, rememberMe)
      setUser(user)
      dismissSessionExpired()
      navigate(from, { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-zinc-950 flex items-center justify-center">
      <div className="absolute top-4 right-4">
        <button
          onClick={toggle}
          title={dark ? 'Switch to light mode' : 'Switch to dark mode'}
          className="text-slate-400 hover:text-slate-700 dark:text-zinc-500 dark:hover:text-zinc-200"
        >
          {dark ? <SunIcon /> : <MoonIcon />}
        </button>
      </div>
      <div className="w-full max-w-sm bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded-lg p-8 shadow-sm">
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-zinc-100 mb-1">Sign in</h1>
        <p className="text-sm text-slate-500 dark:text-zinc-400 mb-6">Photoswitch</p>

        {sessionExpired && (
          <p className="mb-4 text-sm bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-400 border border-amber-200 dark:border-amber-800 rounded-md px-3 py-2">
            Your session expired. Please sign in again.
          </p>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Username</label>
            <input
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              required
              autoFocus
              className="w-full border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 placeholder-slate-400 dark:placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-red-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 dark:text-zinc-300 mb-1">Password</label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
              className="w-full border border-slate-300 dark:border-zinc-600 rounded-md px-3 py-2 text-sm bg-white dark:bg-zinc-800 text-slate-900 dark:text-zinc-100 placeholder-slate-400 dark:placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-red-500"
            />
          </div>
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={rememberMe}
              onChange={e => setRememberMe(e.target.checked)}
              className="rounded accent-red-600"
            />
            <span className="text-sm text-slate-600 dark:text-zinc-400">Remember me for 30 days</span>
          </label>
          {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
          <button
            type="submit"
            disabled={submitting}
            className="w-full bg-red-600 hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-600 text-white rounded-md py-2 text-sm font-medium disabled:opacity-50"
          >
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        {signupPolicy !== 'closed' && signupPolicy !== null && (
          <p className="mt-4 text-sm text-slate-500 dark:text-zinc-400 text-center">
            No account?{' '}
            <Link to="/register" className="text-red-600 dark:text-red-400 hover:underline">
              Register
            </Link>
          </p>
        )}
      </div>
    </div>
  )
}
