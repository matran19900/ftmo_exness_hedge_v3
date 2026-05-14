import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import type { AccountStatusEntry, Order, PairResponse, Position } from '../api/client'
import { symbolMappingApi } from '../api/symbolMapping'
import type {
  AutoMatchResponse,
  Confidence,
  DecisionAction,
  MappingStatus,
  MatchType,
  RawSymbolResponse,
  SaveMappingRequest,
  SpecDivergenceResponse,
} from '../api/types/symbolMapping'

export interface LatestTick {
  bid: number | null
  ask: number | null
  ts: number
}

export type WsState = 'disconnected' | 'connecting' | 'connected'

export type OrderSide = 'buy' | 'sell'

// Step 3.12b: Market = fill-at-bid/ask, Limit = pending below market (BUY) /
// above (SELL), Stop = pending above market (BUY) / below (SELL). The server
// (step 3.6) accepts all three with the same request shape; only the
// ``entry_price`` semantics differ.
export type OrderType = 'market' | 'limit' | 'stop'

// Step 3.12b: 1 Hz snapshot of ``latestTick`` carried separately so the
// VolumeCalculator + market-mode entry-preview don't re-render at the WS
// tick rate (~10 Hz on liquid FX), which would jitter the displayed volume
// number every fraction of a second. ``symbol`` is captured at copy time
// (``latestTick`` itself doesn't carry it; the WS handler filters by symbol
// before writing the store) so a stale-symbol race after a symbol switch
// can be defended against without trusting timestamp ordering.
export interface TickThrottled {
  bid: number
  ask: number
  ts: number
  symbol: string
}

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

  // Step 3.12: server-derived account status snapshot. Initial REST
  // load + 5s WS refresh via ``accounts`` channel. NOT persisted —
  // a stale localStorage snapshot would mislead the operator about
  // whether the FTMO client is actually reachable right now.
  accountStatuses: AccountStatusEntry[]
  setAccountStatuses: (statuses: AccountStatusEntry[]) => void

  // Step 3.12a: configured pairs (ftmo/exness id + ratio + name) hoisted
  // from PairPicker so PositionRow + OrderRow can render the pair name
  // alongside the symbol. Single mount-once fetch in MainPage. NOT
  // persisted — pair config can change server-side and the page reload
  // refetches anyway.
  pairs: PairResponse[]
  setPairs: (pairs: PairResponse[]) => void

  // Step 3.12b: order-type segmented selector. Persisted so the operator's
  // last-used type sticks across reloads — a Limit-mostly trader doesn't
  // want to re-click Limit every session.
  orderType: OrderType
  setOrderType: (orderType: OrderType) => void

  // Step 3.12b: 1 Hz throttle of latestTick (see TickThrottled docstring).
  // NOT persisted — session-only, driven by useTickThrottle at MainPage.
  tickThrottled: TickThrottled | null
  setTickThrottled: (tick: TickThrottled | null) => void

  // ----- Phase 4.A.6: symbol-mapping wizard -----
  //
  // ``mappingStatusByAccount`` mirrors the per-account ``mapping_status``
  // Redis key, populated by the WS ``mapping_status:{exness_account_id}``
  // channel + REST refresh on settings open. NOT persisted — server is
  // the source of truth.
  mappingStatusByAccount: Record<string, MappingStatus>
  setMappingStatusForAccount: (accountId: string, status: MappingStatus) => void
  resetMappingStatuses: () => void

  // Wizard overlay state. ``open=false`` means hidden; opening triggers
  // the REST sequence raw-symbols → auto-match.
  wizard: WizardState
  openWizard: (accountId: string, mode: WizardMode) => Promise<void>
  closeWizard: () => void
  updateRowAction: (ftmo: string, action: DecisionAction) => void
  updateRowOverride: (ftmo: string, exnessValue: string) => void
  bulkAcceptHighConfidence: () => void
  bulkAcceptAllProposed: () => void
  bulkSkipUnmapped: () => void
  toggleAdvancedSpecs: () => void
  toggleShowAllExness: () => void
  saveMapping: () => Promise<{ success: boolean; error?: string }>
  triggerResync: (accountId: string) => Promise<void>
}

// ---------------------------------------------------------------------------
// Wizard sub-types
// ---------------------------------------------------------------------------

export type WizardMode = 'create' | 'diff' | 'spec_mismatch' | 'edit' | null

export interface WizardRowState {
  ftmo: string
  proposed_exness: string
  current_exness: string
  match_type: MatchType
  confidence: Confidence
  action: DecisionAction
  override_value: string
  contract_size: number
  digits: number
  pip_size: number
  pip_value: number
}

export interface WizardState {
  open: boolean
  account_id: string | null
  mode: WizardMode
  signature: string | null
  fuzzy_source: string | null
  fuzzy_score: number | null
  rows: WizardRowState[]
  unmapped_ftmo: string[]
  unmapped_exness: string[]
  available_exness: RawSymbolResponse[]
  show_advanced_specs: boolean
  show_all_exness: boolean
  divergences: SpecDivergenceResponse[]
  loading: boolean
  saving: boolean
  load_error: string | null
  save_error: string | null
}

const INITIAL_WIZARD_STATE: WizardState = {
  open: false,
  account_id: null,
  mode: null,
  signature: null,
  fuzzy_source: null,
  fuzzy_score: null,
  rows: [],
  unmapped_ftmo: [],
  unmapped_exness: [],
  available_exness: [],
  show_advanced_specs: false,
  show_all_exness: false,
  divergences: [],
  loading: false,
  saving: false,
  load_error: null,
  save_error: null,
}

function _mergeProposalIntoRows(
  proposal: AutoMatchResponse,
  rawSymbols: RawSymbolResponse[],
): WizardRowState[] {
  const rawByName: Record<string, RawSymbolResponse> = {}
  for (const r of rawSymbols) rawByName[r.name] = r
  return proposal.proposals.map((p) => {
    const raw = rawByName[p.exness]
    return {
      ftmo: p.ftmo,
      proposed_exness: p.exness,
      current_exness: p.exness,
      match_type: p.match_type,
      confidence: p.confidence,
      action: 'accept' as DecisionAction,
      override_value: '',
      contract_size: raw?.contract_size ?? 0,
      digits: raw?.digits ?? 0,
      pip_size: raw?.pip_size ?? 0,
      pip_value: raw ? raw.contract_size * raw.pip_size : 0,
    }
  })
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

      accountStatuses: [],
      setAccountStatuses: (accountStatuses) => set({ accountStatuses }),

      pairs: [],
      setPairs: (pairs) => set({ pairs }),

      orderType: 'market',
      setOrderType: (orderType) => set({ orderType }),

      tickThrottled: null,
      setTickThrottled: (tickThrottled) => set({ tickThrottled }),

      // ----- Phase 4.A.6: symbol-mapping wizard -----
      mappingStatusByAccount: {},
      setMappingStatusForAccount: (accountId, status) =>
        set((state) => ({
          mappingStatusByAccount: {
            ...state.mappingStatusByAccount,
            [accountId]: status,
          },
        })),
      resetMappingStatuses: () => set({ mappingStatusByAccount: {} }),

      wizard: { ...INITIAL_WIZARD_STATE },

      openWizard: async (accountId, mode) => {
        set((state) => ({
          wizard: {
            ...INITIAL_WIZARD_STATE,
            open: true,
            account_id: accountId,
            mode,
            loading: true,
          },
          mappingStatusByAccount: state.mappingStatusByAccount,
        }))
        try {
          const [rawResp, autoMatch] = await Promise.all([
            symbolMappingApi.getRawSymbols(accountId),
            symbolMappingApi.runAutoMatch(accountId),
          ])
          set((state) => ({
            wizard: {
              ...state.wizard,
              loading: false,
              signature: autoMatch.signature,
              fuzzy_source: autoMatch.fuzzy_match_source,
              fuzzy_score: autoMatch.fuzzy_match_score,
              rows: _mergeProposalIntoRows(autoMatch, rawResp.symbols),
              unmapped_ftmo: autoMatch.unmapped_ftmo,
              unmapped_exness: autoMatch.unmapped_exness,
              available_exness: rawResp.symbols,
            },
          }))
        } catch (err) {
          set((state) => ({
            wizard: {
              ...state.wizard,
              loading: false,
              load_error: err instanceof Error ? err.message : String(err),
            },
          }))
        }
      },

      closeWizard: () => set({ wizard: { ...INITIAL_WIZARD_STATE } }),

      updateRowAction: (ftmo, action) =>
        set((state) => ({
          wizard: {
            ...state.wizard,
            rows: state.wizard.rows.map((r) =>
              r.ftmo === ftmo ? { ...r, action } : r,
            ),
          },
        })),

      updateRowOverride: (ftmo, exnessValue) =>
        set((state) => ({
          wizard: {
            ...state.wizard,
            rows: state.wizard.rows.map((r) =>
              r.ftmo === ftmo
                ? {
                    ...r,
                    override_value: exnessValue,
                    current_exness: exnessValue || r.proposed_exness,
                  }
                : r,
            ),
          },
        })),

      bulkAcceptHighConfidence: () =>
        set((state) => ({
          wizard: {
            ...state.wizard,
            rows: state.wizard.rows.map((r) =>
              r.confidence === 'high' ? { ...r, action: 'accept' } : r,
            ),
          },
        })),

      bulkAcceptAllProposed: () =>
        set((state) => ({
          wizard: {
            ...state.wizard,
            rows: state.wizard.rows.map((r) =>
              r.proposed_exness ? { ...r, action: 'accept' } : r,
            ),
          },
        })),

      bulkSkipUnmapped: () =>
        set((state) => ({
          wizard: {
            ...state.wizard,
            rows: state.wizard.rows.map((r) =>
              r.proposed_exness ? r : { ...r, action: 'skip' },
            ),
          },
        })),

      toggleAdvancedSpecs: () =>
        set((state) => ({
          wizard: {
            ...state.wizard,
            show_advanced_specs: !state.wizard.show_advanced_specs,
          },
        })),

      toggleShowAllExness: () =>
        set((state) => ({
          wizard: {
            ...state.wizard,
            show_all_exness: !state.wizard.show_all_exness,
          },
        })),

      saveMapping: async () => {
        const { wizard } = useAppStore.getState()
        if (!wizard.account_id) return { success: false, error: 'no account' }
        set((state) => ({ wizard: { ...state.wizard, saving: true, save_error: null } }))
        const body: SaveMappingRequest = {
          decisions: wizard.rows.map((r) => ({
            ftmo: r.ftmo,
            action: r.action,
            exness_override:
              r.action === 'override'
                ? r.override_value || null
                : r.action === 'accept'
                  ? r.current_exness
                  : null,
          })),
        }
        try {
          if (wizard.mode === 'edit') {
            await symbolMappingApi.editMapping(wizard.account_id, body)
          } else {
            await symbolMappingApi.saveMapping(wizard.account_id, body)
          }
          set((state) => ({
            wizard: { ...state.wizard, saving: false, open: false },
          }))
          return { success: true }
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err)
          set((state) => ({
            wizard: { ...state.wizard, saving: false, save_error: msg },
          }))
          return { success: false, error: msg }
        }
      },

      triggerResync: async (accountId) => {
        await symbolMappingApi.triggerResync(accountId)
      },
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
        orderType: state.orderType,
      }),
    }
  )
)
