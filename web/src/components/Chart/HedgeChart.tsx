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
import { SearchSymbolPicker } from './SearchSymbolPicker'
import { TimeframeSelector } from './TimeframeSelector'

export function HedgeChart() {
  const selectedSymbol = useAppStore((s) => s.selectedSymbol)
  const setSelectedSymbol = useAppStore((s) => s.setSelectedSymbol)
  const selectedTimeframe = useAppStore((s) => s.selectedTimeframe) as Timeframe
  const setSelectedTimeframe = useAppStore((s) => s.setSelectedTimeframe)
  const latestTick = useAppStore((s) => s.latestTick)

  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const bidLineRef = useRef<IPriceLine | null>(null)
  const askLineRef = useRef<IPriceLine | null>(null)

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
    }
  }, [])

  // Load OHLC whenever the active symbol or timeframe changes.
  useEffect(() => {
    if (!selectedSymbol) {
      seriesRef.current?.setData([])
      return undefined
    }

    let cancelled = false
    async function load() {
      setLoading(true)
      setError(null)
      try {
        const res = await getOhlc(selectedSymbol!, selectedTimeframe, 200)
        if (cancelled || !seriesRef.current) return
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
  }, [selectedSymbol, selectedTimeframe])

  // Subscribe to live candle updates from the shared WS hook. `series.update`
  // updates the matching bar in place or appends a new one.
  useEffect(() => {
    function handleCandle(msg: WsCandleMessage) {
      if (!seriesRef.current) return
      seriesRef.current.update({
        time: msg.data.time as Time,
        open: msg.data.open,
        high: msg.data.high,
        low: msg.data.low,
        close: msg.data.close,
      })
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
              <span className="text-red-600">{latestTick.bid.toFixed(5)}</span>
              <span className="text-gray-400">{' / '}</span>
              <span className="text-green-600">{latestTick.ask.toFixed(5)}</span>
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
    </div>
  )
}
