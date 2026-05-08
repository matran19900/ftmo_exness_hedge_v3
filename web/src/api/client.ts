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

// ----- Pairs -----

export interface PairResponse {
  pair_id: string
  name: string
  ftmo_account_id: string
  exness_account_id: string
  ratio: number
  created_at: number
  updated_at: number
}

export async function listPairs(): Promise<PairResponse[]> {
  const response = await apiClient.get<PairResponse[]>('/pairs/')
  return response.data
}

// ----- Charts -----

export interface Candle {
  time: number // unix seconds (Lightweight Charts convention)
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface OhlcResponse {
  symbol: string
  timeframe: string
  count: number
  // Display precision for the symbol (e.g. EURUSD=5, USDJPY=3, XAUUSD=2).
  // Drives chart Y-axis priceFormat + toolbar bid/ask formatting.
  digits: number
  candles: Candle[]
}

export type Timeframe = 'M1' | 'M5' | 'M15' | 'M30' | 'H1' | 'H4' | 'D1' | 'W1'

export const TIMEFRAMES: readonly Timeframe[] = [
  'M1',
  'M5',
  'M15',
  'M30',
  'H1',
  'H4',
  'D1',
  'W1',
] as const

export async function getOhlc(
  symbol: string,
  timeframe: Timeframe,
  count = 200
): Promise<OhlcResponse> {
  const response = await apiClient.get<OhlcResponse>(`/charts/${symbol}/ohlc`, {
    params: { timeframe, count },
  })
  return response.data
}

// ----- Volume calculation -----

export interface CalculateVolumeRequest {
  entry: number
  sl: number
  risk_amount: number
  ratio: number
}

export interface CalculateVolumeResponse {
  symbol: string
  volume_primary: number
  volume_secondary: number
  sl_pips: number
  pip_value_usd_per_lot: number
  sl_usd_per_lot: number
  quote_ccy: string
  quote_to_usd_rate: number
}

export async function calculateVolume(
  symbol: string,
  req: CalculateVolumeRequest
): Promise<CalculateVolumeResponse> {
  const response = await apiClient.post<CalculateVolumeResponse>(
    `/symbols/${symbol}/calculate-volume`,
    req
  )
  return response.data
}

// ----- WebSocket messages (server protocol from docs/08-server-api.md §9) -----

export interface WsTickMessage {
  channel: string // "ticks:EURUSD"
  data: {
    type: 'tick'
    symbol: string
    bid: number | null
    ask: number | null
    ts: number // unix ms
  }
}

export interface WsCandleMessage {
  channel: string // "candles:EURUSD:M15"
  data: {
    type: 'candle_update'
    time: number // unix seconds (Lightweight Charts convention)
    open: number
    high: number
    low: number
    close: number
  }
}

export interface WsPingMessage {
  type: 'ping'
}

export interface WsErrorMessage {
  type: 'error'
  detail: string
}

export type WsServerMessage = WsTickMessage | WsCandleMessage | WsPingMessage | WsErrorMessage

export type WsClientMessage =
  | { type: 'subscribe'; channels: string[] }
  | { type: 'unsubscribe'; channels: string[] }
  | { type: 'set_symbol'; symbol: string; timeframe: string }
  | { type: 'pong' }
