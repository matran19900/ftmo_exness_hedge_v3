import { useEffect, useState } from 'react'
import { calculateVolume, type CalculateVolumeResponse } from '../../api/client'
import { useDebouncedValue } from '../../hooks/useDebouncedValue'
import { useAppStore } from '../../store'

const DEBOUNCE_MS = 300

type State =
  | { status: 'idle' }
  | { status: 'calculating' }
  | { status: 'ready'; result: CalculateVolumeResponse }
  | { status: 'error'; error: string }

export function VolumeCalculator() {
  const selectedSymbol = useAppStore((s) => s.selectedSymbol)
  const entryPrice = useAppStore((s) => s.entryPrice)
  const slPrice = useAppStore((s) => s.slPrice)
  const riskAmount = useAppStore((s) => s.riskAmount)

  // Debounce every input that triggers the API call so a typing burst
  // collapses into a single request after 300ms of quiet.
  const debouncedEntry = useDebouncedValue(entryPrice, DEBOUNCE_MS)
  const debouncedSl = useDebouncedValue(slPrice, DEBOUNCE_MS)
  const debouncedRisk = useDebouncedValue(riskAmount, DEBOUNCE_MS)

  // Derive validity from debounced inputs. When invalid, the effect doesn't
  // fire and the render path short-circuits to "idle" — keeps setState out
  // of the effect's synchronous path (React 19 forbids that pattern).
  const inputsValid =
    !!selectedSymbol &&
    debouncedEntry !== null &&
    debouncedSl !== null &&
    debouncedRisk > 0 &&
    debouncedEntry !== debouncedSl

  const [state, setState] = useState<State>({ status: 'idle' })

  useEffect(() => {
    if (!inputsValid || !selectedSymbol || debouncedEntry === null || debouncedSl === null) {
      return
    }

    let cancelled = false

    // setState must run inside an async wrapper, not synchronously in the
    // effect body — React 19's react-hooks/set-state-in-effect lint rule.
    async function run(symbol: string, entry: number, sl: number, risk: number) {
      setState({ status: 'calculating' })
      try {
        const result = await calculateVolume(symbol, {
          entry,
          sl,
          risk_amount: risk,
          // Phase 2: ratio is hardcoded 1.0; Phase 4 will read it from the
          // selected pair (PairPicker exposes pair.ratio).
          ratio: 1.0,
        })
        if (!cancelled) setState({ status: 'ready', result })
      } catch (err: unknown) {
        if (cancelled) return
        const status = (err as { response?: { status?: number } })?.response?.status
        const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail
        const fallback = (err as Error)?.message ?? 'Volume calc failed'
        // 503 = rate cache miss; the server starts a subscribe-on-miss
        // pipeline so the next debounce window typically succeeds.
        const friendly =
          status === 503 ? 'Conversion rate not ready, retry...' : (detail ?? fallback)
        setState({ status: 'error', error: friendly })
      }
    }

    void run(selectedSymbol, debouncedEntry, debouncedSl, debouncedRisk)

    return () => {
      cancelled = true
    }
  }, [inputsValid, selectedSymbol, debouncedEntry, debouncedSl, debouncedRisk])

  if (!inputsValid || state.status === 'idle') {
    return (
      <div className="text-xs text-gray-500 italic">Fill Entry, SL, Risk to preview volume.</div>
    )
  }

  if (state.status === 'calculating') {
    return <div className="text-xs text-gray-500">Calculating...</div>
  }

  if (state.status === 'error') {
    return <div className="text-xs text-red-600">{state.error}</div>
  }

  const r = state.result
  const estSlUsd = r.sl_usd_per_lot * r.volume_primary
  return (
    <div className="space-y-1 text-xs">
      <div className="flex justify-between">
        <span className="text-gray-600">Vol Primary:</span>
        <span className="font-mono font-semibold">{r.volume_primary.toFixed(2)}</span>
      </div>
      <div className="flex justify-between">
        <span className="text-gray-600">Vol Secondary:</span>
        <span className="font-mono">{r.volume_secondary.toFixed(2)}</span>
      </div>
      <div className="flex justify-between">
        <span className="text-gray-600">SL distance:</span>
        <span className="font-mono">{r.sl_pips.toFixed(1)} pips</span>
      </div>
      <div className="flex justify-between">
        <span className="text-gray-600">Est. SL $:</span>
        <span className="font-mono">${estSlUsd.toFixed(2)}</span>
      </div>
    </div>
  )
}
