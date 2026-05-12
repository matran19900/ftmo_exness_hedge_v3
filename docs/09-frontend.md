# 09 — Frontend (React 18 + TypeScript)

## 1. Tech stack

| Mục | Choice | Note |
| --- | --- | --- |
| UI framework | React 18 | |
| Build | Vite | |
| Language | TypeScript strict | |
| State | Zustand + persist middleware | localStorage cho `token`, `selectedSymbol`, `selectedTimeframe`, `riskAmount`, `selectedPairId` |
| Charts | Lightweight Charts v5 | |
| HTTP | Axios | `baseURL: '/api'` + JWT interceptor + 401 reload |
| WS | Native WebSocket | query param `?token=` |
| Styling | Inline CSS-in-JS object styles | |
| Mock backend (dev) | MSW (Mock Service Worker) | Để dev frontend trước khi backend ready |

## 2. Cấu trúc thư mục

```
apps/web/
├── index.html
├── vite.config.ts
├── package.json
└── src/
    ├── main.tsx
    ├── App.tsx
    ├── store/index.ts                ← Zustand single store + persist
    ├── api/
    │   ├── index.ts                  ← axios instance + interceptor
    │   ├── auth.ts
    │   ├── orders.ts                 ← create, close, updateSlTp, list, history
    │   ├── pairs.ts                  ← CRUD pairs
    │   ├── accounts.ts               ← CRUD accounts + status
    │   ├── symbols.ts                ← list, detail, tick, calculateVolume
    │   ├── positions.ts
    │   ├── settings.ts
    │   └── charts.ts
    ├── hooks/
    │   ├── useWebSocket.ts
    │   ├── useChart.ts
    │   └── useChartOrderOverlay.ts
    ├── mocks/
    │   ├── handlers.ts               ← MSW handlers (dev only)
    │   └── browser.ts
    ├── components/
    │   ├── Login.tsx
    │   ├── Settings.tsx              ← Modal: pairs management, accounts management, default ratio
    │   ├── ToastContainer.tsx
    │   ├── Chart/
    │   │   ├── HedgeChart.tsx
    │   │   ├── ChartContextMenu.tsx
    │   │   └── PriceMeasureTool.tsx
    │   ├── OrderForm/
    │   │   ├── HedgeOrderForm.tsx
    │   │   ├── PairPicker.tsx        ← NEW v2: dropdown chọn pair
    │   │   ├── VolumeCalculator.tsx
    │   │   └── ConversionRateInfo.tsx
    │   └── Dashboard/
    │       ├── PositionList.tsx      ← Tabs: Open + History
    │       └── AccountStatus.tsx     ← Bar showing all FTMO + Exness accounts status
    └── types/index.ts
```

## 3. Layout (`App.tsx`)

```
┌──────────────────────────── Top Bar ──────────────────────────────┐
│ AccountStatus (multiple accounts)                  ⚙ Settings     │
├───────────────────────────────────────┬───────────────────────────┤
│                                       │ 5px vertical resizer      │
│ HedgeChart                            │◄─────────────────────────►│
│  - Toolbar TF buttons                 │ HedgeOrderForm            │
│  - Chart (historical + live)          │  - PairPicker dropdown    │
│  - Tick price line (ask/bid)          │  - Symbol picker           │
│  - Setup lines (form draft)           │  - Order type tabs         │
│  - Order overlay lines (open/pending) │  - BUY/SELL                │
│  - Drag SL/TP (open orders)           │  - Entry/SL/TP/Risk        │
│  - Right-click menu (set entry/SL/TP) │  - VolumeCalculator        │
│  - Measure tool                       │  - ConversionRateInfo      │
├──── 6px horizontal resizer ───────────┴───────────────────────────┤
│                                                                   │
│ PositionList                                                      │
│  - Tabs: Open Positions (default) | History                       │
│  - Live P&L per row, action buttons (close, edit SL/TP)           │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

Resize:
- Bottom panel: `bottomFrac` ∈ [0.1, 0.75] của tổng height. Default 0.35.
- Right panel: `rightWidth` ∈ [140, 500] px. Default 320.
- Trạng thái resize không persist (reload về default — đơn giản hóa).

## 4. State (`store/index.ts`)

Zustand single store với persist middleware.

```typescript
interface Store {
  // === PERSISTED (localStorage) ===
  token: string | null
  selectedSymbol: string              // default 'EURUSD'
  selectedTimeframe: Timeframe        // default 'M15'
  riskAmount: number                  // default 100
  selectedPairId: string | null       // default null
  
  // === RUNTIME (not persisted) ===
  isAuthenticated: boolean
  symbols: Symbol[]
  pairs: Pair[]                       // user pre-configured pairs
  accounts: { ftmo: Account[], exness: Account[] }
  positions: Record<string, PositionItem>     // keyed by order_id
  ticks: Record<string, { bid: number, ask: number, ts: number }>
  liveCandles: Record<string, Candle>          // key = "{sym}:{tf}"
  agentStatuses: Record<string, AgentStatus>  // key = "{broker}:{account_id}"
  settings: AppSettings
  toasts: Toast[]
  
  // === BRIDGES (chart ↔ form) ===
  formEntry: number | null
  formSl: number | null
  formTp: number | null
  chartLines: { entry: number | null, sl: number | null, tp: number | null, side: 'buy' | 'sell' }
  
  // === ACTIONS ===
  setAuthenticated(b: boolean): void
  setSelectedSymbol(s: string): void
  setSelectedTimeframe(tf: Timeframe): void
  setRiskAmount(n: number): void
  setSelectedPairId(id: string | null): void
  setSymbols(arr: Symbol[]): void
  setPairs(arr: Pair[]): void
  setAccounts(obj: { ftmo: Account[], exness: Account[] }): void
  setPositions(obj: Record<string, PositionItem>): void
  updatePosition(patch: { order_id: string } & Partial<PositionItem>): void
  removePosition(order_id: string): void
  setTick(symbol: string, tick: { bid, ask, ts }): void
  setLiveCandle(key: string, candle: Candle): void
  setAgentStatus(key: string, status: AgentStatus): void
  setSettings(s: AppSettings): void
  addToast(t: Omit<Toast, 'id'>): void
  removeToast(id: string): void
  setFormEntry(n: number | null): void
  setFormSl(n: number | null): void
  setFormTp(n: number | null): void
  setChartLines(l: ChartLines): void
}

const useStore = create<Store>()(
  persist(
    (set, get) => ({ /* ... */ }),
    {
      name: 'ftmo-hedge-store',
      partialize: (state) => ({
        token: state.token,
        selectedSymbol: state.selectedSymbol,
        selectedTimeframe: state.selectedTimeframe,
        riskAmount: state.riskAmount,
        selectedPairId: state.selectedPairId,
      }),
    }
  )
)
```

## 5. WebSocket (`hooks/useWebSocket.ts`)

Single shared connection. Reconnect except code 1000 (logout).

```typescript
function useWebSocket(token: string | null) {
  const wsRef = useRef<WebSocket | null>(null)
  const channelsRef = useRef<Set<string>>(new Set())
  
  useEffect(() => {
    if (!token) return
    connect()
    return () => wsRef.current?.close(1000, 'logout')
  }, [token])
  
  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws?token=${token}`)
    
    ws.onopen = () => {
      console.log('ws open')
      // Re-subscribe all channels
      if (channelsRef.current.size > 0) {
        ws.send(JSON.stringify({ type: 'subscribe', channels: Array.from(channelsRef.current) }))
      }
    }
    
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong' }))
        return
      }
      handleMessage(msg)
    }
    
    ws.onclose = (ev) => {
      if (ev.code === 1000) return
      setTimeout(connect, 3000)
    }
    
    wsRef.current = ws
  }
  
  function setChannels(next: string[]) {
    const prev = channelsRef.current
    const nextSet = new Set(next)
    const unsub = [...prev].filter(c => !nextSet.has(c))
    const sub = [...nextSet].filter(c => !prev.has(c))
    
    if (unsub.length) wsRef.current?.send(JSON.stringify({ type: 'unsubscribe', channels: unsub }))
    if (sub.length) wsRef.current?.send(JSON.stringify({ type: 'subscribe', channels: sub }))
    
    channelsRef.current = nextSet
  }
  
  function setSymbol(symbol: string, timeframe: string) {
    wsRef.current?.send(JSON.stringify({ type: 'set_symbol', symbol, timeframe }))
  }
  
  return { setChannels, setSymbol }
}
```

## 6. Chart (`hooks/useChart.ts`)

Tạo Lightweight Charts 1 lần khi mount. Mọi update qua `series.update()` / `applyOptions()`. KHÔNG recreate khi `digits` change → tránh flash trắng.

### 6.1 Base hook

```typescript
function useChart(containerRef, options) {
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const askLineRef = useRef<IPriceLine | null>(null)
  const bidLineRef = useRef<IPriceLine | null>(null)
  
  useEffect(() => {
    if (!containerRef.current || chartRef.current) return
    chartRef.current = createChart(containerRef.current, options)
    seriesRef.current = chartRef.current.addCandlestickSeries({})
    return () => chartRef.current?.remove()
  }, [])
  
  // Update digits via applyOptions, NOT recreate
  useEffect(() => {
    if (!seriesRef.current) return
    seriesRef.current.applyOptions({
      priceFormat: { type: 'price', minMove: options.minMove, precision: options.digits }
    })
  }, [options.digits, options.minMove])
  
  return { chart: chartRef, series: seriesRef, askLine: askLineRef, bidLine: bidLineRef }
}
```

### 6.2 Order overlay (`hooks/useChartOrderOverlay.ts`)

Vẽ horizontal lines cho Entry / SL / TP của các order liên quan symbol+pair đang xem.

**Quy tắc filter (R31)**: chỉ vẽ order thỏa CẢ 2 điều kiện:
- `order.symbol === store.selectedSymbol` (FTMO symbol đang xem trên chart).
- `order.pair_id === store.selectedPairId`.

**Quy tắc leg (R32)**: chỉ vẽ leg **primary (FTMO)**. Secondary (Exness) KHÔNG vẽ — vì:
- Symbol Exness có thể khác (vd `XAUUSD` ↔ `GOLD`).
- Giá quote 2 broker khác nhau, vẽ lên cùng chart cTrader gây hiểu lầm.
- Secondary không có SL/TP ở broker side (R3).

**Quy tắc số lượng (R33)**: không giới hạn cứng số order vẽ. Trong thực tế ≤ 3 lệnh/symbol/pair tại 1 thời điểm. Nếu vượt → vẽ hết, không lọc.

### 6.3 Ba trạng thái line

| Trạng thái | Khi nào hiện | Style | Drag |
|---|---|---|---|
| **Setup** | User đang nhập Entry/SL/TP trong form (chưa submit) | Dashed, màu nhạt | ✅ Có (drag → update form) |
| **Pending** | Order đã tạo, status `pending` (limit/stop chưa fill) | Dashed, màu trung bình | ❌ Không |
| **Open** | Primary đã fill, status `open` | Solid, màu đậm | ✅ Có (drag SL/TP → PATCH) |

Color convention:
- Entry: blue
- SL: red
- TP: green
- BUY side: arrow up indicator
- SELL side: arrow down indicator

### 6.4 Quản lý price line theo order_id

Tránh full redraw mỗi lần state đổi → flicker. Dùng map keyed:

```typescript
type OrderLineSet = {
  entry: IPriceLine | null
  sl: IPriceLine | null
  tp: IPriceLine | null
}

const orderLinesRef = useRef<Map<string /* order_id */, OrderLineSet>>(new Map())
```

Mỗi WS event → diff với map → chỉ create/update/remove cái đổi:

| Event | Action |
|---|---|
| `hedge_open` | Refetch /positions → tạo `OrderLineSet` mới cho order này |
| `sl_tp_updated` | Tìm order_id trong map → update SL/TP line position (`applyOptions({ price })`) |
| `hedge_closed` | Tìm order_id trong map → `series.removePriceLine(line)` cho cả 3 line → delete map entry |
| Đổi symbol/pair | Clear toàn bộ map → render lại theo filter mới |

### 6.5 Setup lines (form draft)

Lines tách biệt với order overlay, chỉ vẽ khi form đang được fill:

```typescript
const setupLinesRef = useRef<{ entry: IPriceLine | null, sl: IPriceLine | null, tp: IPriceLine | null }>({...})
```

Reactive với `store.chartLines`. Khi user clear form / submit thành công → remove setup lines.

### 6.6 Right-click context menu (`Chart/ChartContextMenu.tsx`)

Right-click trên chart tại vị trí giá Y → menu:

```
┌─────────────────────────┐
│ Set as Entry  (1.08412) │
│ Set as SL     (1.08412) │
│ Set as TP     (1.08412) │
└─────────────────────────┘
```

Click → cập nhật field tương ứng trong `store.chartLines` → form input đồng bộ + setup line vẽ ngay.

Logic lấy giá từ Y coordinate: `chart.timeScale().coordinateToPrice(y)` (Lightweight Charts API).

### 6.7 Drag SL/TP (chỉ open orders)

Lightweight Charts không support native drag price line. Implementation:
- Mouse down trên price line area + Y trùng với SL/TP của open order → bắt đầu drag.
- Mouse move → update line position visually qua `applyOptions({ price: newPrice })`.
- Mouse up → PATCH `/orders/{id}/sl-tp` với giá mới.
- Server reject (G6) → revert position cũ + toast.

Setup line drag tương tự nhưng không gọi API — chỉ update `store.chartLines`.

## 7. PairPicker (`OrderForm/PairPicker.tsx`)

Dropdown bắt buộc trong order form. Hiển thị:

```
┌─────────────────────────────────┐
│ Pair: [Main Hedge        ▼]     │
│       ●ftmo_acc_001 ●exness_acc │  ← status dots
└─────────────────────────────────┘
```

- ●xanh = online, ●đỏ = offline.
- Disabled options nếu pair offline (cả 2 leg).
- localStorage persist `selectedPairId`.
- Default: pair đầu tiên trong list khi user chưa chọn gì.

## 8. HedgeOrderForm

Tabs Market/Limit/Stop, BUY/SELL. Validation:
- SL direction (R16): BUY → sl<entry; SELL → sl>entry.
- MIN_SL_PIPS = 5 (R17).
- TP=0 → skip TP (R18).

Submit body:
```typescript
{
  pair_id: store.selectedPairId,
  symbol: store.selectedSymbol,
  side: 'buy' | 'sell',
  risk_amount: store.riskAmount,
  sl_price: number,
  tp_price: number,  // 0 = skip
  order_type: 'market' | 'limit' | 'stop',
  entry_price: number,  // 0 cho market
  secondary_ratio: null,  // dùng default từ pair
}
```

Sau submit:
- Toast "order pending"
- Refetch /positions
- Form đóng / clear

## 9. PositionList — Open + History tabs

Single component, 2 tab:

```typescript
function PositionList() {
  const [activeTab, setActiveTab] = useState<'open' | 'history'>('open')
  const positions = useStore(s => s.positions)
  const [history, setHistory] = useState<Order[]>([])
  
  useEffect(() => {
    if (activeTab === 'history') fetchHistory()
  }, [activeTab])
  
  async function fetchHistory() {
    const data = await api.orders.list({ status: 'closed', limit: 50 })
    setHistory(data)
  }
  
  return (
    <div>
      <Tabs>
        <Tab onClick={() => setActiveTab('open')} active={activeTab === 'open'}>
          Open ({Object.keys(positions).length})
        </Tab>
        <Tab onClick={() => setActiveTab('history')} active={activeTab === 'history'}>
          History
        </Tab>
      </Tabs>
      
      {activeTab === 'open' ? (
        <OpenList positions={positions} />
      ) : (
        <HistoryList orders={history} />
      )}
    </div>
  )
}
```

### OpenList row
```
| Symbol | Pair      | Side | Volume | P&L (USD)  | Total | SL/TP    | Actions     |
| EURUSD | Main Hedge| BUY  | 0.45   | +5.20/-5.10| +0.10 | edit ✎   | × close     |
```

### HistoryList row
```
| Symbol | Pair      | Side | Final P&L | Reason | Closed At   | [v expand] |
```

Expand row → show 2 legs detail (entry, close, pnl, reason, commission).

## 10. AccountStatus bar

Top bar shows ALL FTMO + Exness accounts:

```
[●ftmo_acc_001: 50,012.34 / 50,000.00]  [●ftmo_acc_002: 100,200/100,000]  
[●exness_acc_001: 4,988.66 / 5,000.00]  [●exness_acc_002: 9,750/10,000]  ⚙
```

Color dots theo heartbeat. Polling `/accounts` 30s + WS `agent_status` event.

## 11. Settings modal

Tabs:
1. **Pairs** — list + add + edit + remove
2. **Accounts** — list + add + edit + remove (FTMO + Exness)
3. **General** — default_secondary_ratio, primary_fill_timeout

## 12. Toast container

- 4 type: info / success / warning / error
- Auto-dismiss 5s
- Stack góc bottom-right
- Trigger từ store.addToast()

Ví dụ: order pending → toast info; primary_filled → success; secondary_failed → error.

## 13. MSW (dev only)

```typescript
// src/mocks/handlers.ts
export const handlers = [
  rest.post('/api/auth/login', (req, res, ctx) =>
    res(ctx.json({ access_token: 'mock-jwt', token_type: 'Bearer', expires_in: 86400 }))
  ),
  rest.get('/api/symbols', (req, res, ctx) =>
    res(ctx.json({ symbols: [/* fake 5 symbols */] }))
  ),
  // ... etc
]
```

Enable trong dev mode:
```typescript
// src/main.tsx
if (import.meta.env.DEV && import.meta.env.VITE_MOCK === 'true') {
  const { worker } = await import('./mocks/browser')
  await worker.start()
}
```

## 14. Lessons learned từ v1

- ❌ Recreate Lightweight Chart khi digits change → flash trắng. **V2: applyOptions only.**
- ❌ WS broadcast full state mỗi tick → lag. **V2: chỉ delta, frontend refetch khi cần.**
- ❌ Position state thay vì keyed → re-render toàn bộ list. **V2: keyed by order_id.**
- ❌ WS resubscribe full mỗi lần đổi symbol. **V2: diff subscribe.**
- ❌ Inline CSS huge → hard to maintain. **V2: vẫn inline (đơn giản hóa) nhưng tách styles vào const objects.**

---

## 15. Phase 3 additions

> Phase 3 implement spec từ §1-§14. Mục này ghi nhận **deltas thực tế** trong Phase 3 — chi tiết quyết định xem `DECISIONS.md` D-046 → D-149. Web stack vẫn React + TS + Vite + Tailwind nhưng v2 Phase 3 dùng **Tailwind CSS** (D-029 Phase 1 lock) thay vì inline CSS-in-JS object styles như §1 mention.

### 15.1 Actual layout

Code thực tế tại `web/src/` (không `apps/web/` như spec §2). Structure đã evolve:
- `components/Chart/HedgeChart.tsx` — chart canvas + price line + setup lines.
- `components/Header/Header.tsx` — title + WS pill + `<AccountStatusBar />` + Settings gear + Logout.
- `components/Header/AccountStatusBar.tsx` — per-account dot + balance + equity (D-130 extends Phase 1 placeholder).
- `components/OrderForm/` — full form (xem §15.3).
- `components/PositionList/` — Open + History tabs với rows (xem §15.4).
- `components/Settings/` — modal Pair CRUD + Account toggle (xem §15.5).
- `components/MainPage.tsx` — layout shell hosting useWebSocket + useTickThrottle hooks (D-105, D-131 hoist patterns).
- `hooks/` — useWebSocket, useDebouncedValue, useTickThrottle.
- `lib/` — orderValidation.ts (Phase 2), pairHelpers.ts (Phase 3 D-132).
- `store/index.ts` — Zustand single store + persist middleware (D-106 single file convention).
- `api/client.ts` — Axios + JWT interceptor + all endpoint helpers (D-106 single file).

### 15.2 useWebSocket hoisted to MainPage (D-105)

Phase 2 placeholder had `useWebSocket` in HedgeChart. Phase 3 hoists to **MainPage** as the single WS connection per session. Chart accepts a `candleHandler` prop để register cho `candles:{symbol}:{tf}` channel; positions/orders/accounts dispatched directly to Zustand store.

Subscribe pattern on (re)connect (D-127):
```typescript
ws.send(JSON.stringify({
  type: 'subscribe',
  channels: [
    `ticks:${selectedSymbol}`,
    `candles:${selectedSymbol}:${selectedTimeframe}`,
    'positions',
    'orders',
    'accounts',
  ]
}))
```

### 15.3 OrderForm components Phase 3

`web/src/components/OrderForm/`:

| Component | Purpose | Phase 3 deltas |
|---|---|---|
| `HedgeOrderForm.tsx` | Main form, submit handler, preflight, reset on success | D-110 market-only submit, D-112 2-layer preflight, D-113 partial reset, D-129 submit gating với hasOnlineFtmoAccount, D-148 3-tier ftmoBlockMessage |
| `PairPicker.tsx` | Pair selector dropdown | D-114 stale validate selectedPairId membership |
| `SideSelector.tsx` | BUY/SELL toggle | Phase 2 |
| `OrderTypeSelector.tsx` | **NEW** Market/Limit/Stop segmented | D-134 (default Market, persisted) |
| `VolumeCalculator.tsx` | Risk-based + manual override | D-110 manual mode, D-140 ApiState.refreshing variant, D-141 decorative dot |
| `PriceInput.tsx` | Entry/SL/TP numeric input | Phase 2 |
| `RiskAmountInput.tsx` | Risk USD input | Phase 2 |

**Submit gating** (D-129, D-148): button disabled khi `hasOnlineFtmoAccount === false`. Tooltip + inline red banner 3-tier priority:
1. No FTMO accounts → "Chưa có FTMO account được cấu hình".
2. All FTMO disabled (operator-toggled) → "FTMO account đã bị vô hiệu hóa (mở Settings → Accounts để bật lại)".
3. Heartbeat-dead → "FTMO client offline (heartbeat đã expired)".

**Market mode entry auto-drive** (D-135): Entry input hidden; `entryPrice` auto-drives từ `tickThrottled` (ask cho BUY, bid cho SELL). Throttle 5s (D-139) tránh volume number jitter. On orderType change to limit/stop, entry reset to null (D-137).

**Direction validation preflight** (D-136): Limit BUY: `entry < ask`, SELL: `entry > bid`. Stop BUY: `entry > ask`, SELL: `entry < bid`. Client preflight + server validate authoritative.

### 15.4 PositionList components Phase 3

`web/src/components/PositionList/`:

| Component | Purpose | Phase 3 deltas |
|---|---|---|
| `PositionList.tsx` | Tab switcher Open/History | Phase 3 new |
| `OpenTab.tsx` | Live positions table | Phase 3 new |
| `HistoryTab.tsx` | Closed orders table với time-range | Phase 3 new |
| `PositionRow.tsx` | Live position row | D-133 joins via orders slice for pair_id, D-122 current_price str unified |
| `OrderRow.tsx` | Closed order row | Direct pair_id |
| `ModifyModal.tsx` | SL/TP edit dialog | Phase 3 new, D-101 None/0/positive semantics |

Columns Open tab: Pair (D-132 lookupPairName), Symbol, Side, Volume, Entry, Current Price, P&L, SL, TP, Actions (Close + Modify).
Columns History tab: Pair, Symbol, Side, Volume, Entry, Close, P&L, Close Reason, Closed at.

Money display via `scaleMoney(raw, money_digits)` helper (D-108).

### 15.5 Settings modal Phase 3 (D-144)

`web/src/components/Settings/` (NEW directory):

| Component | Purpose |
|---|---|
| `SettingsModal.tsx` | Overlay với click-outside close + × button (Escape Phase 5 defer) |
| `PairsTab.tsx` | List + create/edit/delete actions; D-142 409 pair_in_use server-message display |
| `PairForm.tsx` | Create/edit pair với FTMO account dropdown từ accountStatuses slice (D-145 Exness account text-input free-form Phase 3) |
| `AccountsTab.tsx` | List với enabled toggle (D-143 PATCH endpoint); D-146 FTMO create UI defer Phase 5 OAuth |

Access via Header gear icon button. Window.confirm cho delete confirm (Phase 5 custom modal backlog).

### 15.6 Hooks Phase 3

`web/src/hooks/`:
- `useWebSocket.ts` — single connection, subscribe channels (D-105 hoisted). Reconnect logic exponential backoff, ping/pong, auto-resend set_symbol. Dispatcher dispatch positions_tick / order_updated / account_status / tick / candle_update.
- `useDebouncedValue.ts` — generic debounce (Phase 2).
- `useTickThrottle.ts` — **NEW** 5s throttle latestTick → tickThrottled (D-138 symbol-at-copy-time, D-139 5s interval).

### 15.7 Zustand store slices Phase 3

`web/src/store/index.ts` single file (D-106). Phase 3 added slices (xem `06-data-models.md` §15.7 cho TypedDict):

**Persisted** (partialize whitelist):
- `token`, `selectedSymbol`, `selectedTimeframe`, `selectedPairId` (Phase 1+2).
- `riskAmount` (Phase 2).
- `orderType` (Phase 3, D-134 default "market").

**Runtime-only** (NOT persisted):
- `side, entryPrice, slPrice, tpPrice, manualVolumePrimary, volumeReady, effectiveVolumeLots` (form draft — reset per session).
- `latestTick, tickThrottled, wsState, symbolDigits` (server-derived).
- `orders, positions, history, accountStatuses, pairs` (Phase 3 server-derived REST + WS).

**Setters**: simple Zustand action methods, e.g., `setOrderType(orderType: OrderType) => set({ orderType })`. `upsertPositionTick(update)` insert prepend khi findIndex===-1 (D-121 true upsert).

### 15.8 Helper libraries `web/src/lib/`

- `orderValidation.ts` — Phase 2 `validateSideDirection`; Phase 3.11a extension cho server-side normalize precision sync (D-115).
- `pairHelpers.ts` — **NEW** `lookupPairName(pairs, pair_id)` với fallback hierarchy (D-132): pair found → `pair.name`; pair_id missing → "—"; pair not in cache → truncated UUID `xxxxxxxx...`.

### 15.9 API client `web/src/api/client.ts` (D-106 single file)

Phase 3 thêm endpoint helpers:

```typescript
// Orders (Phase 3)
export async function createOrder(req: OrderCreateRequest): Promise<OrderCreateResponse>
export async function listOrders(params?: ListOrdersParams): Promise<OrderListResponse>
export async function getOrder(orderId: string): Promise<OrderDetailResponse>
export async function closeOrder(orderId: string): Promise<OrderActionResponse>
export async function modifyOrder(orderId: string, body: ModifyBody): Promise<OrderActionResponse>

// Positions (Phase 3)
export async function listPositions(params?: ListPositionsParams): Promise<PositionListResponse>

// History (Phase 3)
export async function listHistory(params?: ListHistoryParams): Promise<OrderListResponse>

// Accounts (Phase 3)
export async function listAccounts(): Promise<AccountListResponse>
export async function updateAccount(broker: string, accountId: string, enabled: boolean): Promise<AccountStatusEntry>

// Volume calc (Phase 2) — Phase 3 unchanged
export async function calculateVolume(req: CalcVolumeRequest): Promise<CalcVolumeResponse>

// Error mapping helper (D-111)
export function formatOrderError(err: unknown): string
export const ORDER_ERROR_MESSAGES: Record<string, string> = { ... }
```

### 15.10 D-13 / WS message handler routing Phase 3

```typescript
// useWebSocket dispatcher pattern
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data)
  if (msg.channel?.startsWith('ticks:')) {
    setLatestTick(msg.data)
  } else if (msg.channel?.startsWith('candles:')) {
    candleHandler?.(msg)
  } else if (msg.channel === 'positions') {
    if (msg.data.type === 'positions_tick') {
      msg.data.positions.forEach((p) => upsertPositionTick({...p, ...coercions}))
    } else if (msg.data.type === 'position_event') {
      ...
    }
  } else if (msg.channel === 'orders') {
    if (msg.data.type === 'order_updated') upsertOrder(msg.data)
  } else if (msg.channel === 'accounts') {
    if (msg.data.type === 'account_status') setAccountStatuses(msg.data.accounts)
  }
}
```

Property-merge order trong positions_tick handler (D-124): spread first (`...p`), then post-spread coercions for 3 wire-type fields (`current_price: String(p.current_price), is_stale: p.is_stale ? 'true' : 'false', tick_age_ms: String(p.tick_age_ms)`) override.

### 15.11 Lessons learned Phase 3 mở rộng

V2 Phase 3 reuse lessons từ §14, plus thêm:
- ❌ Drop unknown order_id on positions_tick. **V2 Phase 3**: `upsertPositionTick` true upsert prepend khi findIndex===-1 (D-121). Server payload includes static metadata (D-120) → row renders complete trên insert.
- ❌ Trust string "false" as falsy in JS. **V2 Phase 3**: server WS payload routes qua typed Pydantic helper `row_to_entry` (D-147) → real bool/Literal types arrive trên wire.
- ❌ Recompute volume every 1s during market mode auto-entry. **V2 Phase 3**: useTickThrottle 5s (D-139) + VolumeCalculator `refreshing` variant holds prev result (D-140) → smooth UX.
- ❌ Lose pair name in PositionRow + OrderRow rows. **V2 Phase 3**: pairs cache hoist MainPage (D-131) + `lookupPairName` helper (D-132) với truncated UUID fallback.
