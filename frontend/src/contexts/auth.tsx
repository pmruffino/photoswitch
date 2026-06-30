import { createContext, useContext, useEffect, useState, ReactNode } from 'react'
import { api, User } from '../api'

export interface AuthCtx {
  user: User | null
  loading: boolean
  setUser: (u: User | null) => void
}

export const AuthContext = createContext<AuthCtx>({
  user: null,
  loading: true,
  setUser: () => {},
})

export function useAuth() {
  return useContext(AuthContext)
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.auth.me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setLoading(false))
  }, [])

  return (
    <AuthContext.Provider value={{ user, loading, setUser }}>
      {children}
    </AuthContext.Provider>
  )
}
