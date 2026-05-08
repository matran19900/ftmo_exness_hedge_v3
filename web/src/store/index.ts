import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'

export interface LatestTick {
  bid: number | null
  ask: number | null
  ts: number
}

export type WsState = 'disconnected' | 'connecting' | 'connected'

export type OrderSide = 'buy' | 'sell'

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
