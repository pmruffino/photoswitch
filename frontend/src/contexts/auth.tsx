import { createContext, useContext, useEffect, useState, ReactNode } from 'react'
import { api, setSessionExpiredHandler, User } from '../api'

export interface AuthCtx {
  user: User | null
  loading: boolean
  setUser: (u: User | null) => void
  // True right after the backend rejected a request with 401 while the SPA still
  // thought the user was logged in (session expired / cookie invalidated mid-visit),
  // as opposed to simply never having logged in. Login.tsx uses this to show a
  // "please sign in again" prompt instead of a bare sign-in form.
  sessionExpired: boolean
  dismissSessionExpired: () => void
}

export const AuthContext = createContext<AuthCtx>({
  user: null,
  loading: true,
  setUser: () => {},
  sessionExpired: false,
  dismissSessionExpired: () => {},
})

export function useAuth() {
  return useContext(AuthContext)
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)
  const [sessionExpired, setSessionExpired] = useState(false)

  useEffect(() => {
    api.auth.me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    setSessionExpiredHandler(() => {
      setUser(prev => {
        // Only surface the prompt if we actually thought we were logged in — avoids
        // flagging the routine 401 a logged-out visitor gets from other API calls.
        if (prev) setSessionExpired(true)
        return null
      })
    })
    return () => setSessionExpiredHandler(null)
  }, [])

  return (
    <AuthContext.Provider value={{ user, loading, setUser, sessionExpired, dismissSessionExpired: () => setSessionExpired(false) }}>
      {children}
    </AuthContext.Provider>
  )
}
