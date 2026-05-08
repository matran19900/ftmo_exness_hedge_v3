import {
  CandlestickSeries,
  ColorType,
  createChart,
  CrosshairMode,
  LineStyle,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'
import { getOhlc, type Candle, type Timeframe, type WsCandleMessage } from '../../api/client'
import { useWebSocket } from '../../hooks/useWebSocket'
import { useAppStore } from '../../store'
import { ChartContextMenu } from './ChartContextMenu'
import { SearchSymbolPicker } from './SearchSymbolPicker'
import { TimeframeSelector } from './TimeframeSelector'

interface TrackedCandle {
  time: number // unix seconds
  open: number
  high: number
  low: number
  close: number
}

export function HedgeChart() {
  const selectedSymbol = useAppStore((s) => s.selectedSymbol)
  const setSelectedSymbol = useAppStore((s) => s.setSelectedSymbol)
  const selectedTimeframe = useAppStore((s) => s.selectedTimeframe) as Timeframe
  const setSelectedTimeframe = useAppStore((s) => s.setSelectedTimeframe)
  const latestTick = useAppStore((s) => s.latestTick)
  const symbolDigits = useAppStore((s) => s.symbolDigits)
  const setSymbolDigits = useAppStore((s) => s.setSymbolDigits)
  const entryPrice = useAppStore((s) => s.entryPrice)
  const slPrice = useAppStore((s) => s.slPrice)
  const tpPrice = useAppStore((s) => s.tpPrice)

  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const bidLineRef = useRef<IPriceLine | null>(null)
  const askLineRef = useRef<IPriceLine | null>(null)
  const entryLineRef = useRef<IPriceLine | null>(null)
  const slLineRef = useRef<IPriceLine | null>(null)
  const tpLineRef = useRef<IPriceLine | null>(null)
  // Local mirror of the chart's last bar (unix seconds + OHLC). Lets every
  // tick patch close/high/low without re-fetching from the chart instance,
  // and gives us a baseline so the first tick after load doesn't reset HL.
  const lastCandleRef = useRef<TrackedCandle | null>(null)

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; price: number } | null>(
    null
  )

  const { registerCandleHandler } = useWebSocket()

  // Create the chart instance once on mount; updates flow through setData() so
  // the chart never has to be torn down on symbol/timeframe changes.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return undefined

    const chart = createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight,
      layout: {
        background: { type: ColorType.Solid, color: '#ffffff' },
        textColor: '#374151',
      },
      grid: {
        vertLines: { color: '#f3f4f6' },
        horzLines: { color: '#f3f4f6' },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: '#e5e7eb',
      },
      rightPriceScale: { borderColor: '#e5e7eb' },
      // Free cursor (no snap to candle close) — needed in step 2.9 for picking
      // exact price levels via right-click set Entry/SL/TP.
      crosshair: { mode: CrosshairMode.Normal },
    })

    // lightweight-charts v5 uses ``addSeries(SeriesDef, options)`` instead of
    // v4's ``addCandlestickSeries(options)``.
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#10b981',
      downColor: '#ef4444',
      borderUpColor: '#10b981',
      borderDownColor: '#ef4444',
      wickUpColor: '#10b981',
      wickDownColor: '#ef4444',
      // Hide the v5 default last-close axis label + horizontal price line so
      // the only horizontals on the chart are the explicit bid/ask we draw.
      lastValueVisible: false,
      priceLineVisible: false,
    })

    chartRef.current = chart
    seriesRef.current = series

    const handleResize = () => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({
          width: containerRef.current.clientWidth,
          height: containerRef.current.clientHeight,
        })
      }
    }
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      bidLineRef.current = null
      askLineRef.current = null
      entryLineRef.current = null
      slLineRef.current = null
      tpLineRef.current = null
    }
  }, [])

  // Load OHLC whenever the active symbol or timeframe changes.
  useEffect(() => {
    if (!selectedSymbol) {
      seriesRef.current?.setData([])
      lastCandleRef.current = null
      return undefined
    }

    let cancelled = false
    // Clear the tracked candle eagerly so any tick that arrives during the
    // ~200ms fetch window for the new symbol is ignored (the tick effect
    // bails when the ref is null) — prevents the previous symbol's bar from
    // being patched with the new symbol's bid.
    lastCandleRef.current = null

    async function load() {
      setLoading(true)
      setError(null)
      try {
        const res = await getOhlc(selectedSymbol!, selectedTimeframe, 200)
        if (cancelled || !seriesRef.current) return
        // Apply per-symbol price format BEFORE setData so the very first paint
        // uses the right precision (no flash of "1.08" → "1.08432" on EURUSD).
        // minMove = 10^-digits: e.g. digits=5 → 0.00001, digits=3 → 0.001.
        seriesRef.current.applyOptions({
          priceFormat: {
            type: 'price',
            precision: res.digits,
            minMove: Math.pow(10, -res.digits),
          },
        })
        setSymbolDigits(res.digits)
        const data: CandlestickData[] = res.candles.map((c: Candle) => ({
          // Lightweight Charts accepts unix seconds at runtime but its Time
          // type is a tagged union — narrow with a single cast at the boundary.
          time: c.time as Time,
          open: c.open,
          high: c.high,
          low: c.low,
          close: c.close,
        }))
        seriesRef.current.setData(data)
        chartRef.current?.timeScale().fitContent()
        // Seed the live-tick effect with the last historical bar as baseline.
        if (data.length > 0) {
          const lastBar = data[data.length - 1]!
          lastCandleRef.current = {
            time: lastBar.time as number,
            open: lastBar.open,
            high: lastBar.high,
            low: lastBar.low,
            close: lastBar.close,
          }
        } else {
          lastCandleRef.current = null
        }
      } catch (err) {
        if (cancelled) return
        const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail
        const message = detail ?? (err as Error)?.message ?? 'Failed to load chart data'
        setError(message)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()

    return () => {
      cancelled = true
    }
  }, [selectedSymbol, selectedTimeframe, setSymbolDigits])

  // Subscribe to live candle updates from the shared WS hook.
  //
  // Two paths:
  //   1. In-bar update (msg.time === tracked.time): the tick stream is the
  //      single source of truth for in-bar redraws. Calling series.update
  //      here would race with the tick effect and visibly flicker (server's
  //      mid-bar snapshot close vs the fresher tick-derived close). We still
  //      sync the server's authoritative open and any new high/low into the
  //      ref via Math.max / Math.min so the NEXT tick effect builds from a
  //      correct baseline — server's close is dropped because it's stale.
  //   2. Bar boundary or first candle (different time, or no tracked bar):
  //      server is source of truth — paint via series.update and reset ref
  //      so subsequent ticks build on the new bar.
  useEffect(() => {
    function handleCandle(msg: WsCandleMessage) {
      if (!seriesRef.current) return

      const tracked = lastCandleRef.current
      if (tracked && msg.data.time === tracked.time) {
        lastCandleRef.current = {
          time: tracked.time,
          open: msg.data.open,
          high: Math.max(tracked.high, msg.data.high),
          low: Math.min(tracked.low, msg.data.low),
          close: tracked.close,
        }
        return
      }

      seriesRef.current.update({
        time: msg.data.time as Time,
        open: msg.data.open,
        high: msg.data.high,
        low: msg.data.low,
        close: msg.data.close,
      })
      lastCandleRef.current = {
        time: msg.data.time,
        open: msg.data.open,
        high: msg.data.high,
        low: msg.data.low,
        close: msg.data.close,
      }
    }
    registerCandleHandler(handleCandle)
    return () => registerCandleHandler(null)
  }, [registerCandleHandler])

  // Live bid/ask price lines. Update price via applyOptions when the line
  // already exists; create with createPriceLine on first tick; remove when
  // tick clears (e.g. WS disconnect or symbol change).
  useEffect(() => {
    const series = seriesRef.current
    if (!series) return

    if (!latestTick || !selectedSymbol) {
      if (bidLineRef.current) {
        series.removePriceLine(bidLineRef.current)
        bidLineRef.current = null
      }
      if (askLineRef.current) {
        series.removePriceLine(askLineRef.current)
        askLineRef.current = null
      }
      return
    }

    if (latestTick.bid !== null) {
      if (bidLineRef.current) {
        bidLineRef.current.applyOptions({ price: latestTick.bid })
      } else {
        bidLineRef.current = series.createPriceLine({
          price: latestTick.bid,
          color: '#ef4444',
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: 'Bid',
        })
      }
    } else if (bidLineRef.current) {
      series.removePriceLine(bidLineRef.current)
      bidLineRef.current = null
    }

    if (latestTick.ask !== null) {
      if (askLineRef.current) {
        askLineRef.current.applyOptions({ price: latestTick.ask })
      } else {
        askLineRef.current = series.createPriceLine({
          price: latestTick.ask,
          color: '#10b981',
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: 'Ask',
        })
      }
    } else if (askLineRef.current) {
      series.removePriceLine(askLineRef.current)
      askLineRef.current = null
    }
  }, [latestTick, selectedSymbol])

  // Setup lines (Entry blue, SL red, TP green) reactive to form state. Each
  // line is upserted via createPriceLine + applyOptions; null/0 prices remove
  // the line. Lines persist across symbol switches per CEO directive — the
  // form keeps Entry/SL/TP until the user clears them or reloads.
  useEffect(() => {
    const series = seriesRef.current
    if (!series) return

    function upsert(
      ref: React.MutableRefObject<IPriceLine | null>,
      price: number | null,
      color: string,
      title: string
    ) {
      if (!series) return
      if (price === null || price <= 0) {
        if (ref.current) {
          series.removePriceLine(ref.current)
          ref.current = null
        }
        return
      }
      if (ref.current) {
        ref.current.applyOptions({ price })
      } else {
        ref.current = series.createPriceLine({
          price,
          color,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title,
        })
      }
    }

    upsert(entryLineRef, entryPrice, '#3b82f6', 'Entry')
    upsert(slLineRef, slPrice, '#dc2626', 'SL')
    upsert(tpLineRef, tpPrice, '#16a34a', 'TP')
  }, [entryPrice, slPrice, tpPrice])

  // Right-click on the chart container → open context menu with the price
  // under the cursor. Uses the v5 ISeriesApi.coordinateToPrice(y) — y is in
  // chart-container coordinates, derived from clientY minus the container's
  // bounding rect top.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    function handleContextMenu(e: MouseEvent) {
      const series = seriesRef.current
      if (!series || !container) return
      e.preventDefault()
      const rect = container.getBoundingClientRect()
      const yInContainer = e.clientY - rect.top
      const price = series.coordinateToPrice(yInContainer)
      if (price === null) return
      setContextMenu({ x: e.clientX, y: e.clientY, price: price as number })
    }

    container.addEventListener('contextmenu', handleContextMenu)
    return () => container.removeEventListener('contextmenu', handleContextMenu)
  }, [])

  // Close the context menu on any document click. The 0ms setTimeout defers
  // the listener registration past the same right-click event tick that just
  // opened the menu, so the opening click doesn't immediately close it.
  useEffect(() => {
    if (!contextMenu) return
    const handleClick = () => setContextMenu(null)
    const t = window.setTimeout(() => {
      document.addEventListener('click', handleClick)
    }, 0)
    return () => {
      window.clearTimeout(t)
      document.removeEventListener('click', handleClick)
    }
  }, [contextMenu])

  // Live candle update from ticks. Use `bid` as the running close (industry
  // convention — matches TradingView / MetaTrader). High/low are monotonic
  // within a bar: high only grows, low only shrinks. We always patch the bar
  // at `tracked.time` (current bar) so series.update never tries to write a
  // time earlier than the last bar — that would either no-op or throw.
  useEffect(() => {
    const series = seriesRef.current
    const tracked = lastCandleRef.current
    if (!series || !tracked || !latestTick || latestTick.bid === null) return

    const newClose = latestTick.bid
    const newHigh = Math.max(tracked.high, newClose)
    const newLow = Math.min(tracked.low, newClose)

    series.update({
      time: tracked.time as Time,
      open: tracked.open,
      high: newHigh,
      low: newLow,
      close: newClose,
    })

    lastCandleRef.current = {
      time: tracked.time,
      open: tracked.open,
      high: newHigh,
      low: newLow,
      close: newClose,
    }
  }, [latestTick])

  return (
    <div className="h-full bg-white border border-gray-200 rounded flex flex-col">
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-200 flex-shrink-0">
        <div className="flex items-center gap-3">
          <SearchSymbolPicker selected={selectedSymbol} onSelect={setSelectedSymbol} />
          <TimeframeSelector selected={selectedTimeframe} onSelect={setSelectedTimeframe} />
        </div>
        <div className="flex items-center gap-3">
          {latestTick && latestTick.bid !== null && latestTick.ask !== null && (
            <span className="text-xs font-mono">
              <span className="text-red-600">{latestTick.bid.toFixed(symbolDigits)}</span>
              <span className="text-gray-400">{' / '}</span>
              <span className="text-green-600">{latestTick.ask.toFixed(symbolDigits)}</span>
            </span>
          )}
          {loading && <span className="text-xs text-gray-500">Loading...</span>}
        </div>
      </div>

      <div className="relative flex-1 min-h-0">
        {!selectedSymbol && (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-gray-400">
            Select a symbol to view chart
          </div>
        )}
        {error && (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-red-600 p-4 text-center">
            {error}
          </div>
        )}
        <div ref={containerRef} className="w-full h-full" />
      </div>

      {contextMenu && (
        <ChartContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          price={contextMenu.price}
          digits={symbolDigits}
          onClose={() => setContextMenu(null)}
        />
      )}
    </div>
  )
}
