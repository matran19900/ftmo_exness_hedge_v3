import {
  CandlestickSeries,
  ColorType,
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'
import { getOhlc, type Candle, type Timeframe } from '../../api/client'
import { useAppStore } from '../../store'
import { SearchSymbolPicker } from './SearchSymbolPicker'
import { TimeframeSelector } from './TimeframeSelector'

export function HedgeChart() {
  const selectedSymbol = useAppStore((s) => s.selectedSymbol)
  const setSelectedSymbol = useAppStore((s) => s.setSelectedSymbol)
  const selectedTimeframe = useAppStore((s) => s.selectedTimeframe) as Timeframe
  const setSelectedTimeframe = useAppStore((s) => s.setSelectedTimeframe)

  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

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

  return (
    <div className="h-full bg-white border border-gray-200 rounded flex flex-col">
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-200 flex-shrink-0">
        <div className="flex items-center gap-3">
          <SearchSymbolPicker selected={selectedSymbol} onSelect={setSelectedSymbol} />
          <TimeframeSelector selected={selectedTimeframe} onSelect={setSelectedTimeframe} />
        </div>
        {loading && <span className="text-xs text-gray-500">Loading...</span>}
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
