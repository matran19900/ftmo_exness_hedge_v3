import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import type { Order, Position } from '../api/client'

export interface LatestTick {
  bid: number | null
  ask: number | null
  ts: number
}

export type WsState = 'disconnected' | 'connecting' | 'connected'

export type OrderSide = 'buy' | 'sell'

// Cap the history list so a long-running session doesn't grow the
// in-memory array unboundedly. 200 closed orders covers a typical
// week; the History tab can paginate beyond that via REST refresh.
const HISTORY_MAX = 200

export interface AppState {
  token: string | null
  setToken: (token: string | null) => void
  logout: () => void

  selectedSymbol: string | null
  setSelectedSymbol: (symbol: string | null) => void

  selectedTimeframe: string
  setSelectedTimeframe: (tf: string) => void

  selectedPairId: string | null
  setSelectedPairId: (id: string | null) => void

  riskAmount: number
  setRiskAmount: (amount: number) => void

  // Runtime-only: latest tick for currently selected symbol (not persisted).
  latestTick: LatestTick | null
  setLatestTick: (tick: LatestTick | null) => void

  // Runtime-only: WS connection status for header indicator (not persisted).
  wsState: WsState
  setWsState: (state: WsState) => void

  // Runtime-only: display precision for the active symbol (refreshed on each
  // OHLC load). Default 5 = FX. Not persisted — server is the source of truth.
  symbolDigits: number
  setSymbolDigits: (digits: number) => void

  // Order form draft state. Per-trade, NOT persisted: each session starts with
  // side='buy' and empty Entry/SL/TP so a stale draft can't bleed into a new
  // session. (riskAmount + selectedPairId are user prefs and stay persisted.)
  side: OrderSide
  setSide: (side: OrderSide) => void
  entryPrice: number | null
  setEntryPrice: (price: number | null) => void
  slPrice: number | null
  setSlPrice: (price: number | null) => void
  tpPrice: number | null
  setTpPrice: (price: number | null) => void

  // Manual volume override for VolumeCalculator. null = auto (use server's
  // calc); number = user-typed Vol Primary (Vol Secondary derived from
  // ratio). Reset on symbol switch + per session — NOT persisted.
  manualVolumePrimary: number | null
  setManualVolumePrimary: (v: number | null) => void

  // True when the order has a positive effective volume (auto calc ready
  // OR manual override set) AND no side-direction error blocks it. Phase 3
  // submit handler reads this to enable/disable the submit button. NOT
  // persisted (runtime).
  volumeReady: boolean
  setVolumeReady: (ready: boolean) => void

  // Step 3.11: the effective per-leg volume (in lots) that the submit
  // handler ships to POST /api/orders. ``VolumeCalculator`` is the
  // sole writer — it pushes either the server's auto-computed
  // ``volume_primary`` or the user's manual override here whenever
  // a positive value is available. ``null`` means "no usable volume
  // yet"; the form's Place button stays disabled in that state.
  // NOT persisted (derived from per-session inputs).
  effectiveVolumeLots: number | null
  setEffectiveVolumeLots: (v: number | null) => void

  // ----- Step 3.10: server-derived order/position state -----
  //
  // These slices are populated from initial REST loads (PositionList
  // mount) and kept in sync by `useWebSocket` consuming the
  // `positions` channel. Not persisted — Zustand's
  // ``partialize`` whitelist deliberately excludes them so a stale
  // localStorage snapshot can't shadow live server state on reload.

  positions: Position[]
  setPositions: (positions: Position[]) => void
  upsertPositionTick: (update: Partial<Position> & { order_id: string }) => void
  removePosition: (orderId: string) => void

  orders: Order[]
  setOrders: (orders: Order[]) => void
  upsertOrder: (update: Partial<Order> & { order_id: string }) => void

  history: Order[]
  setHistory: (history: Order[]) => void
  prependHistory: (order: Order) => void
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      token: null,
      setToken: (token) => set({ token }),
      logout: () => set({ token: null }),

      selectedSymbol: null,
      setSelectedSymbol: (selectedSymbol) => set({ selectedSymbol }),

      selectedTimeframe: 'M15',
      setSelectedTimeframe: (selectedTimeframe) => set({ selectedTimeframe }),

      selectedPairId: null,
      setSelectedPairId: (selectedPairId) => set({ selectedPairId }),

      riskAmount: 100,
      setRiskAmount: (riskAmount) => set({ riskAmount }),

      latestTick: null,
      setLatestTick: (latestTick) => set({ latestTick }),

      wsState: 'disconnected',
      setWsState: (wsState) => set({ wsState }),

      symbolDigits: 5,
      setSymbolDigits: (symbolDigits) => set({ symbolDigits }),

      side: 'buy',
      setSide: (side) => set({ side }),
      entryPrice: null,
      setEntryPrice: (entryPrice) => set({ entryPrice }),
      slPrice: null,
      setSlPrice: (slPrice) => set({ slPrice }),
      tpPrice: null,
      setTpPrice: (tpPrice) => set({ tpPrice }),

      manualVolumePrimary: null,
      setManualVolumePrimary: (manualVolumePrimary) => set({ manualVolumePrimary }),

      volumeReady: false,
      setVolumeReady: (volumeReady) => set({ volumeReady }),

      effectiveVolumeLots: null,
      setEffectiveVolumeLots: (effectiveVolumeLots) => set({ effectiveVolumeLots }),

      // ----- Step 3.10 server-derived slices -----

      positions: [],
      setPositions: (positions) => set({ positions }),
      // Upsert a tick-level update (step 3.11c). If the order_id
      // matches an existing entry, merge fields over it. If the
      // order_id is unknown, INSERT as a new entry at the top of the
      // list (newest-first, matching the REST sort by
      // ``p_executed_at DESC``).
      //
      // Pre-3.11c this dropped unknown order_ids on the floor, relying
      // on the next REST refresh to pull them in — but Phase 3 has no
      // automatic refresh trigger after a positions_tick lands, so
      // newly-filled orders never surfaced until the user manually
      // refreshed. The 3.11c server change enriches the broadcast
      // payload with the static order metadata (side, volume_lots,
      // entry_price, money_digits, sl_price, tp_price, p_executed_at)
      // so an insert here carries enough to render the row from
      // scratch. Fields missing from ``update`` read as ``undefined``;
      // the Position type's strings render as empty cells until the
      // next REST refresh or positions_tick fills them.
      upsertPositionTick: (update) =>
        set((state) => {
          const idx = state.positions.findIndex((p) => p.order_id === update.order_id)
          if (idx === -1) {
            return { positions: [{ ...update } as Position, ...state.positions] }
          }
          const merged = { ...state.positions[idx], ...update } as Position
          const next = state.positions.slice()
          next[idx] = merged
          return { positions: next }
        }),
      removePosition: (orderId) =>
        set((state) => ({
          positions: state.positions.filter((p) => p.order_id !== orderId),
        })),

      orders: [],
      setOrders: (orders) => set({ orders }),
      upsertOrder: (update) =>
        set((state) => {
          const idx = state.orders.findIndex((o) => o.order_id === update.order_id)
          if (idx === -1) {
            // Unknown order_id (e.g. a fill arriving for an order the
            // current session never created). Prepend; missing
            // required fields will read as `undefined` until the next
            // REST refresh fills them.
            const placeholder = {
              ...update,
              status: update.status ?? '',
              p_status: update.p_status ?? '',
              created_at: update.created_at ?? '',
              updated_at: update.updated_at ?? '',
            } as Order
            return { orders: [placeholder, ...state.orders] }
          }
          const merged = { ...state.orders[idx], ...update } as Order
          const next = state.orders.slice()
          next[idx] = merged
          return { orders: next }
        }),

      history: [],
      setHistory: (history) => set({ history }),
      prependHistory: (order) =>
        set((state) => ({
          history: [order, ...state.history].slice(0, HISTORY_MAX),
        })),
    }),
    {
      name: 'ftmo-hedge-store',
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        token: state.token,
        selectedSymbol: state.selectedSymbol,
        selectedTimeframe: state.selectedTimeframe,
        selectedPairId: state.selectedPairId,
        riskAmount: state.riskAmount,
      }),
    }
  )
)
