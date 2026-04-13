import { createContext, useContext, useState, useEffect, useCallback, ReactNode } from 'react'
import { api, setAccessToken } from '../api/axios'

interface User {
  id: string
  email: string
  has_api_keys: boolean
  bot_active: boolean
  is_admin?: boolean
}

interface AuthCtx {
  user: User | null
  isAuthenticated: boolean
  isLoading: boolean
  login: (email: string, password: string) => Promise<User>
  signup: (email: string, password: string) => Promise<User>
  logout: () => void
  refreshUser: () => Promise<void>
}

const AuthContext = createContext<AuthCtx | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  const refreshUser = useCallback(async () => {
    try {
      const res = await api.get('/api/auth/me')
      setUser(res.data)
    } catch {
      // ignore
    }
  }, [])

  useEffect(() => {
    api.post('/api/auth/refresh')
      .then(res => {
        setAccessToken(res.data.access_token)
        setUser(res.data.user)
      })
      .catch(() => setUser(null))
      .finally(() => setIsLoading(false))
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    const res = await api.post('/api/auth/login', { email, password })
    setAccessToken(res.data.access_token)
    setUser(res.data.user)
    return res.data.user
  }, [])

  const signup = useCallback(async (email: string, password: string) => {
    const res = await api.post('/api/auth/signup', { email, password })
    setAccessToken(res.data.access_token)
    setUser(res.data.user)
    return res.data.user
  }, [])

  const logout = useCallback(async () => {
    await api.post('/api/auth/logout').catch(() => {})
    setAccessToken(null)
    setUser(null)
  }, [])

  return (
    <AuthContext.Provider value={{ user, isAuthenticated: !!user, isLoading, login, signup, logout, refreshUser }}>
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be inside AuthProvider')
  return ctx
}
