import { useEffect } from 'react'
import { listPairs } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'
import { useAppStore } from '../store'
import { HedgeChart } from './Chart/HedgeChart'
import { Header } from './Header/Header'
import { HedgeOrderForm } from './OrderForm/HedgeOrderForm'
import { PositionList } from './PositionList/PositionList'

export function MainPage() {
  // Step 3.10: own the single shared WebSocket at the layout level.
  // Subscriptions (ticks/candles for the chart, positions for live
  // P&L, orders for state changes) all flow through this one
  // connection. ``registerCandleHandler`` is passed down to
  // ``HedgeChart`` so the chart can wire its handle-candle reducer
  // into the central onmessage dispatcher.
  const { registerCandleHandler } = useWebSocket()

  // Step 3.12a: hoist the pairs fetch up here so PairPicker (order
  // form) AND PositionRow + OrderRow (positions / history tables) all
  // share one cache. Single mount-once REST call; a page reload
  // refetches naturally. Errors are swallowed silently — a 401 has
  // already triggered global logout in ``apiClient``, and a transient
  // 5xx just leaves the dropdown empty + the row pair-name as the
  // truncated UUID fallback, which is the safe default.
  const setPairs = useAppStore((s) => s.setPairs)
  useEffect(() => {
    let cancelled = false
    listPairs()
      .then((data) => {
        if (!cancelled) setPairs(data)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [setPairs])

  return (
    <div className="h-screen flex flex-col bg-gray-50 min-w-[1280px]">
      <Header />

      <div className="flex-1 flex min-h-0 overflow-hidden">
        {/* Left 70%: chart on top, position list below. */}
        <div className="w-[70%] flex flex-col gap-2 p-2 pr-1 min-h-0 overflow-hidden">
          <div className="flex-1 min-h-0">
            <HedgeChart registerCandleHandler={registerCandleHandler} />
          </div>
          <div className="h-[35%] min-h-0">
            <PositionList />
          </div>
        </div>

        {/* Right 30%: order form full-height column. */}
        <div className="w-[30%] p-2 pl-1 min-h-0 overflow-hidden">
          <HedgeOrderForm />
        </div>
      </div>
    </div>
  )
}
