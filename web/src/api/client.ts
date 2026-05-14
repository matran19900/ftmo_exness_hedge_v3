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

// ----- Pair CRUD (step 3.13) -----

export interface PairCreateRequest {
  name: string
  ftmo_account_id: string
  exness_account_id: string
  ratio: number
}

export interface PairUpdateRequest {
  name?: string
  ftmo_account_id?: string
  exness_account_id?: string
  ratio?: number
}

export async function createPair(req: PairCreateRequest): Promise<PairResponse> {
  const response = await apiClient.post<PairResponse>('/pairs/', req)
  return response.data
}

export async function updatePair(
  pairId: string,
  req: PairUpdateRequest
): Promise<PairResponse> {
  const response = await apiClient.patch<PairResponse>(`/pairs/${pairId}`, req)
  return response.data
}

export async function deletePair(pairId: string): Promise<void> {
  await apiClient.delete(`/pairs/${pairId}`)
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
  // Phase 4.A.5 server breaking change: pair_id is required so the
  // calculator can resolve the per-Exness-account mapping (real broker
  // contract size when wizard has been run, 1:1 synthetic for Phase 3
  // single-leg pairs). See server/app/api/symbols.py.
  pair_id: string
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

// ----- Order create (step 3.6 / 3.11) -----

export interface OrderCreateRequest {
  pair_id: string
  symbol: string
  side: 'buy' | 'sell'
  order_type: 'market' | 'limit' | 'stop'
  volume_lots: number
  // ``0`` means "no SL / TP" (matches the cmd_stream protocol in
  // docs/05 §4.2). Frontend translates blank inputs to 0 before
  // posting.
  sl: number
  tp: number
  // ``0`` for market orders (server uses bid/ask for direction
  // validation in that case). Required > 0 for limit / stop —
  // step 3.6 service rejects with ``missing_entry_price`` otherwise.
  entry_price: number
}

export interface OrderCreateResponse {
  order_id: string
  request_id: string
  status: 'accepted'
  message: string
}

export async function createOrder(req: OrderCreateRequest): Promise<OrderCreateResponse> {
  const response = await apiClient.post<OrderCreateResponse>('/orders', req)
  return response.data
}

// Step 3.11: maps step-3.6 ``OrderValidationError.error_code`` strings
// to Vietnamese operator-facing messages. Fallback to the server's raw
// ``message`` if the code isn't in the map (covers new codes added by
// future steps).
export const ORDER_ERROR_MESSAGES: Record<string, string> = {
  pair_not_found: 'Không tìm thấy pair',
  pair_disabled: 'Pair đã bị vô hiệu hóa',
  account_not_found: 'Không tìm thấy account FTMO',
  account_disabled: 'Account FTMO đã bị vô hiệu hóa',
  client_offline: 'FTMO client đang offline',
  symbol_inactive: 'Symbol chưa được kích hoạt',
  symbol_not_synced: 'Symbol chưa được đồng bộ từ broker',
  invalid_volume: 'Volume không hợp lệ (vượt min/max/step)',
  missing_entry_price: 'Thiếu entry price cho lệnh limit/stop',
  invalid_sl_direction: 'SL sai hướng so với side',
  invalid_tp_direction: 'TP sai hướng so với side',
  no_tick_data: 'Chưa có dữ liệu giá hiện tại cho symbol',
  validation_error: 'Dữ liệu không hợp lệ',
  order_corrupt: 'Order data bị lỗi (báo admin)',
}

/**
 * Extract a user-facing error message from a failed ``createOrder``
 * (or any of the step-3.9 mutation endpoints).
 *
 * The server's ``HTTPException(detail={"error_code", "message"})``
 * shape lives at ``err.response.data.detail``. If ``error_code``
 * matches our map we use the Vietnamese translation; otherwise we
 * fall through to the raw ``message`` (already user-friendly per
 * step 3.6's ``OrderValidationError`` constructor).
 */
export function formatOrderError(err: unknown): string {
  if (
    typeof err === 'object' &&
    err !== null &&
    'response' in err &&
    typeof (err as { response?: unknown }).response === 'object'
  ) {
    const resp = (err as { response?: { data?: unknown } }).response
    const data = resp?.data
    if (
      typeof data === 'object' &&
      data !== null &&
      'detail' in data &&
      typeof (data as { detail?: unknown }).detail === 'object'
    ) {
      const detail = (data as { detail: { error_code?: unknown; message?: unknown } }).detail
      const code = typeof detail.error_code === 'string' ? detail.error_code : undefined
      const message = typeof detail.message === 'string' ? detail.message : undefined
      if (code && code in ORDER_ERROR_MESSAGES) {
        return ORDER_ERROR_MESSAGES[code]!
      }
      if (message) return message
    }
    // 422 (Pydantic) shape: detail is an array of {loc, msg, type}.
    if (
      typeof data === 'object' &&
      data !== null &&
      Array.isArray((data as { detail?: unknown }).detail)
    ) {
      return 'Dữ liệu form không hợp lệ'
    }
  }
  if (err instanceof Error && err.message) return err.message
  return 'Lỗi kết nối server'
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

// ----- Accounts (step 3.12) -----

export interface AccountStatusEntry {
  broker: 'ftmo' | 'exness'
  account_id: string
  name: string
  enabled: boolean
  status: 'online' | 'offline' | 'disabled'
  // Money fields are ``money_digits``-scaled int strings (D-108) — divide
  // by ``10**money_digits`` at the render boundary, never in this layer
  // so REST + WS payloads stay shape-identical.
  balance_raw: string
  equity_raw: string
  margin_raw: string
  free_margin_raw: string
  currency: string
  money_digits: string
}

export interface AccountListResponse {
  accounts: AccountStatusEntry[]
  total: number
}

export async function listAccounts(): Promise<AccountListResponse> {
  const response = await apiClient.get<AccountListResponse>('/accounts')
  return response.data
}

// Step 3.13: toggle the enabled flag for one account. Returns the
// updated row (same shape as one entry from listAccounts) so the
// caller can splice it into its cached list without a follow-up
// list fetch (though the WS account_status_loop broadcast will
// overwrite within 5 s anyway).
export async function updateAccount(
  broker: 'ftmo' | 'exness',
  accountId: string,
  enabled: boolean
): Promise<AccountStatusEntry> {
  const response = await apiClient.patch<AccountStatusEntry>(
    `/accounts/${broker}/${accountId}`,
    { enabled }
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
      // Step 3.11c server payload enrichment — static metadata sourced
      // from the order HASH so the WS handler (step 3.11d) can forward
      // enough data into ``upsertPositionTick`` to render a brand-new
      // row without a REST refresh. Optional so the TS compiler tolerates
      // older server versions (pre-3.11c) that don't ship these keys.
      side?: string
      volume_lots?: string
      entry_price?: string
      money_digits?: string
      sl_price?: string
      tp_price?: string
      p_executed_at?: string
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

// Step 3.12: account_status snapshot broadcast on the ``accounts`` channel
// every 5 s by the server's ``account_status_loop``. Drives the
// AccountStatusBar in the page header.
export interface WsAccountStatusMessage {
  channel: 'accounts'
  data: {
    type: 'account_status'
    ts: number
    accounts: AccountStatusEntry[]
  }
}

// Phase 4.A.6: per-Exness-account mapping status broadcast — see
// ``MappingCacheService.set_mapping_status`` in
// ``server/app/services/mapping_cache_service.py``. Channel pattern is
// ``mapping_status:{exness_account_id}``.
export interface WsMappingStatusMessage {
  channel: string
  data: {
    type: 'status_changed'
    account_id: string
    status: 'pending_mapping' | 'active' | 'spec_mismatch' | 'disconnected'
    signature: string | null
    cache_filename: string | null
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
  | WsAccountStatusMessage
  | WsMappingStatusMessage

export type WsClientMessage =
  | { type: 'subscribe'; channels: string[] }
  | { type: 'unsubscribe'; channels: string[] }
  | { type: 'set_symbol'; symbol: string; timeframe: string }
  | { type: 'pong' }
