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

// ----- Orders (step 3.6 + 3.9) -----

export interface Order {
  order_id: string
  pair_id: string
  ftmo_account_id: string
  exness_account_id: string
  symbol: string
  side: 'buy' | 'sell'
  order_type: 'market' | 'limit' | 'stop'
  // Volume on the order row is stored per leg (p_volume_lots is the
  // authoritative server side from step 3.6 onward). Both fields
  // optional in the type because various step writers populate
  // different subsets — components prefer p_volume_lots when set.
  volume_lots?: string
  p_volume_lots?: string
  sl_price?: string
  tp_price?: string
  entry_price?: string
  status: string
  p_status: string
  s_status?: string
  p_broker_order_id?: string
  p_fill_price?: string
  // Step 3.7 writes p_executed_at on fill (NOT p_fill_time per
  // OrderHash schema in server/app/services/redis_service.py).
  p_executed_at?: string
  p_commission?: string
  p_close_price?: string
  // Step 3.7 writes p_closed_at (NOT p_close_time).
  p_closed_at?: string
  p_realized_pnl?: string
  p_close_reason?: string
  // Step 3.4a + 3.7 SL/TP-attach warning fields.
  p_sl_tp_warning?: string
  p_sl_tp_warning_msg?: string
  // Step 3.7 error reporting.
  p_error_code?: string
  p_error_msg?: string
  // Step 3.5b reconstructed-from-deal-history marker.
  p_reconstructed?: string
  // Step 3.5a extended close-detail fields.
  p_swap?: string
  p_balance_after_close?: string
  p_money_digits?: string
  p_closed_volume?: string
  created_at: string
  updated_at: string
}

export interface OrderListResponse {
  orders: Order[]
  total: number
  limit: number
  offset: number
}

export interface OrderDetailResponse {
  order: Order
}

export interface OrderActionResponse {
  order_id: string
  request_id: string
  status: 'accepted'
  message: string
}

export interface ListOrdersParams {
  status?: string
  symbol?: string
  account_id?: string
  limit?: number
  offset?: number
}

export async function listOrders(params?: ListOrdersParams): Promise<OrderListResponse> {
  const response = await apiClient.get<OrderListResponse>('/orders', { params })
  return response.data
}

export async function getOrder(orderId: string): Promise<OrderDetailResponse> {
  const response = await apiClient.get<OrderDetailResponse>(`/orders/${orderId}`)
  return response.data
}

export async function closeOrder(
  orderId: string,
  volumeLots?: number
): Promise<OrderActionResponse> {
  const body: { volume_lots?: number } = volumeLots !== undefined ? { volume_lots: volumeLots } : {}
  const response = await apiClient.post<OrderActionResponse>(`/orders/${orderId}/close`, body)
  return response.data
}

export async function modifyOrder(
  orderId: string,
  sl?: number | null,
  tp?: number | null
): Promise<OrderActionResponse> {
  const body: { sl?: number | null; tp?: number | null } = {}
  if (sl !== undefined) body.sl = sl
  if (tp !== undefined) body.tp = tp
  const response = await apiClient.post<OrderActionResponse>(`/orders/${orderId}/modify`, body)
  return response.data
}

// ----- Positions (step 3.9) -----

export interface Position {
  order_id: string
  symbol: string
  side: 'buy' | 'sell' | string
  volume_lots: string
  entry_price: string
  current_price: string
  unrealized_pnl: string
  money_digits: string
  is_stale: string
  tick_age_ms: string
  computed_at: string
  // Static overlay from order row.
  sl_price?: string
  tp_price?: string
  p_executed_at?: string
}

export interface PositionListResponse {
  positions: Position[]
  total: number
}

export interface ListPositionsParams {
  account_id?: string
  symbol?: string
}

export async function listPositions(params?: ListPositionsParams): Promise<PositionListResponse> {
  const response = await apiClient.get<PositionListResponse>('/positions', { params })
  return response.data
}

// ----- History (step 3.9) -----

export interface HistoryListResponse {
  history: Order[]
  total: number
}

export interface ListHistoryParams {
  from_ts?: number
  to_ts?: number
  symbol?: string
  account_id?: string
  limit?: number
  offset?: number
}

export async function listHistory(params?: ListHistoryParams): Promise<HistoryListResponse> {
  const response = await apiClient.get<HistoryListResponse>('/history', { params })
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

// Step 3.7/3.8: order_updated + position_event + positions_tick broadcasts
// over the `orders` / `positions` channels. Channel names are literals
// (not prefix-stripped like ticks:/candles:) per BroadcastService's
// docstring.

export interface WsOrderUpdatedMessage {
  channel: 'orders'
  data: {
    type: 'order_updated'
    order_id: string
  } & Partial<Order>
}

export interface WsPositionsTickMessage {
  channel: 'positions'
  data: {
    type: 'positions_tick'
    account_id: string
    ts: number
    positions: {
      order_id: string
      symbol: string
      current_price: string | number
      unrealized_pnl: string
      is_stale: boolean
      tick_age_ms: number
    }[]
  }
}

export interface WsPositionEventMessage {
  channel: 'positions'
  data: {
    type: 'position_event'
    event_type: 'closed' | 'modified' | 'pending_filled' | string
    order_id: string
    [k: string]: unknown
  }
}

export type WsServerMessage =
  | WsTickMessage
  | WsCandleMessage
  | WsPingMessage
  | WsErrorMessage
  | WsOrderUpdatedMessage
  | WsPositionsTickMessage
  | WsPositionEventMessage

export type WsClientMessage =
  | { type: 'subscribe'; channels: string[] }
  | { type: 'unsubscribe'; channels: string[] }
  | { type: 'set_symbol'; symbol: string; timeframe: string }
  | { type: 'pong' }
