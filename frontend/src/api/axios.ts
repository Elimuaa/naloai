import axios from 'axios'

export const api = axios.create({
  baseURL: '/',
  withCredentials: true,
})

let accessToken: string | null = null

export const setAccessToken = (token: string | null) => { accessToken = token }
export const getAccessToken = () => accessToken

api.interceptors.request.use(config => {
  if (accessToken) {
    config.headers.Authorization = `Bearer ${accessToken}`
  }
  return config
})

api.interceptors.response.use(
  res => res,
  async error => {
    const original = error.config
    if (error.response?.status === 401 && !original._retry && !original.url?.includes('/api/auth/refresh')) {
      original._retry = true
      try {
        const res = await api.post('/api/auth/refresh')
        setAccessToken(res.data.access_token)
        return api(original)
      } catch {
        setAccessToken(null)
        window.location.href = '/login'
      }
    }
    return Promise.reject(error)
  }
)
