import { useEffect, useState, type ChangeEvent } from 'react'
import { calculateVolume, type CalculateVolumeResponse } from '../../api/client'
import { useDebouncedValue } from '../../hooks/useDebouncedValue'
import { useAppStore } from '../../store'

const DEBOUNCE_MS = 300
// Phase 4: read ratio from PairPicker's selected pair (pair.ratio).
const RATIO_DEFAULT = 1.0

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
  const manualVolumePrimary = useAppStore((s) => s.manualVolumePrimary)
  const setManualVolumePrimary = useAppStore((s) => s.setManualVolumePrimary)

  const debouncedEntry = useDebouncedValue(entryPrice, DEBOUNCE_MS)
  const debouncedSl = useDebouncedValue(slPrice, DEBOUNCE_MS)
  const debouncedRisk = useDebouncedValue(riskAmount, DEBOUNCE_MS)

  // Validity gates the API call. When invalid, the render path returns the
  // idle message directly — no setState needed (React 19's
  // react-hooks/set-state-in-effect lint rule forbids that pattern).
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

    async function run(symbol: string, entry: number, sl: number, risk: number) {
      setState({ status: 'calculating' })
      try {
        const result = await calculateVolume(symbol, {
          entry,
          sl,
          risk_amount: risk,
          ratio: RATIO_DEFAULT,
        })
        if (!cancelled) setState({ status: 'ready', result })
      } catch (err: unknown) {
        if (cancelled) return
        const status = (err as { response?: { status?: number } })?.response?.status
        const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail
        const fallback = (err as Error)?.message ?? 'Volume calc failed'
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

  const isManualMode = manualVolumePrimary !== null

  function handleManualChange(e: ChangeEvent<HTMLInputElement>) {
    const raw = e.target.value
    if (raw === '') return // empty in manual mode would unintentionally drop back to auto
    const parsed = parseFloat(raw)
    if (Number.isNaN(parsed) || parsed < 0) return
    setManualVolumePrimary(parsed)
  }

  // ----- Render branches -----

  // Auto mode: no inputs yet → idle hint.
  if (!isManualMode && (!inputsValid || state.status === 'idle')) {
    return (
      <div className="text-xs text-gray-500 italic">Fill Entry, SL, Risk to preview volume.</div>
    )
  }

  if (!isManualMode && state.status === 'calculating') {
    return <div className="text-xs text-gray-500">Calculating...</div>
  }

  // Auto-mode error: still offer manual override so the user isn't blocked
  // by a stale rate cache or an SL distance the server rejected.
  if (!isManualMode && state.status === 'error') {
    return (
      <div className="space-y-2">
        <div className="text-xs text-red-600">{state.error}</div>
        <button
          type="button"
          onClick={() => setManualVolumePrimary(0.01)}
          className="text-xs text-blue-600 hover:underline"
        >
          Override manually
        </button>
      </div>
    )
  }

  // Either auto-ready or manual mode. Vol P comes from manual override or
  // the last successful calc; Vol S = Vol P × ratio.
  const autoResult = state.status === 'ready' ? state.result : null
  const effectiveVolP = isManualMode ? manualVolumePrimary : (autoResult?.volume_primary ?? null)
  const effectiveVolS = effectiveVolP === null ? null : effectiveVolP * RATIO_DEFAULT

  return (
    <div className="space-y-2 text-xs">
      <div className="flex justify-between items-center gap-2">
        <span className="text-gray-600 shrink-0">Vol Primary:</span>
        {isManualMode ? (
          <input
            type="number"
            step="0.01"
            min="0"
            value={manualVolumePrimary ?? 0}
            onChange={handleManualChange}
            className="w-20 px-2 py-0.5 bg-white border border-blue-400 rounded text-xs font-mono text-right focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        ) : (
          <span className="font-mono font-semibold">{effectiveVolP?.toFixed(2)}</span>
        )}
      </div>

      <div className="flex justify-between">
        <span className="text-gray-600">Vol Secondary:</span>
        <span className="font-mono">{effectiveVolS?.toFixed(2)}</span>
      </div>

      {/* SL distance + Est. SL $ are computed from the auto formula; suppress
          them in manual mode where they no longer reflect the typed Vol P. */}
      {!isManualMode && autoResult && (
        <>
          <div className="flex justify-between">
            <span className="text-gray-600">SL distance:</span>
            <span className="font-mono">{autoResult.sl_pips.toFixed(1)} pips</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-600">Est. SL $:</span>
            <span className="font-mono">
              ${(autoResult.sl_usd_per_lot * autoResult.volume_primary).toFixed(2)}
            </span>
          </div>
        </>
      )}

      <div className="border-t border-gray-100 pt-1.5">
        {isManualMode ? (
          <button
            type="button"
            onClick={() => setManualVolumePrimary(null)}
            className="text-xs text-blue-600 hover:underline"
          >
            ↻ Reset to auto
          </button>
        ) : autoResult ? (
          <button
            type="button"
            onClick={() => setManualVolumePrimary(autoResult.volume_primary)}
            className="text-xs text-gray-500 hover:text-blue-600 hover:underline"
          >
            Override manually
          </button>
        ) : null}
      </div>
    </div>
  )
}
