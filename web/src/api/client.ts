import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import toast from 'react-hot-toast'
import { useAppStore } from '../store'

export const apiClient: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 10000,
})

apiClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = useAppStore.getState().token
  if (token) {
    config.headers = config.headers ?? {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (error.response?.status === 401) {
      const hadToken = useAppStore.getState().token !== null
      useAppStore.getState().logout()
      if (hadToken) {
        toast.error('Session expired. Please login again.')
      }
    }
    return Promise.reject(error)
  }
)

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
}

export async function login(req: LoginRequest): Promise<LoginResponse> {
  const response = await apiClient.post<LoginResponse>('/auth/login', req)
  return response.data
}

export interface SymbolMapping {
  ftmo: string
  exness: string
  match_type: 'exact' | 'manual' | 'suffix_strip'
  ftmo_units_per_lot: number
  exness_trade_contract_size: number
  ftmo_pip_size: number
  exness_pip_size: number
  ftmo_pip_value: number
  exness_pip_value: number
  quote_ccy: string
}

export async function getSymbols(): Promise<{ symbols: string[] }> {
  const response = await apiClient.get<{ symbols: string[] }>('/symbols/')
  return response.data
}

export async function getSymbolMapping(ftmoSymbol: string): Promise<SymbolMapping> {
  const response = await apiClient.get<SymbolMapping>(`/symbols/${ftmoSymbol}`)
  return response.data
}
