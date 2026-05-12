import { useEffect, useState, type ChangeEvent } from 'react'
import { calculateVolume, type CalculateVolumeResponse } from '../../api/client'
import { useDebouncedValue } from '../../hooks/useDebouncedValue'
import { validateSideDirection } from '../../lib/orderValidation'
import { useAppStore } from '../../store'

const DEBOUNCE_MS = 300
// Phase 4: read ratio from PairPicker's selected pair (pair.ratio).
const RATIO_DEFAULT = 1.0

// API lifecycle only — side-direction errors are derived from store state
// in render (not stored here). Keeping the side error out of this state
// means a side-direction violation never overwrites a previous successful
// `result`, so manual-mode metrics return instantly when the user fixes
// the direction (no need to wait for the next debounce).
//
// Step 3.12c: the ``refreshing`` variant carries the previous ``result``
// so the result block stays mounted during a recalc. Pre-3.12c every
// throttled-tick auto-driven entry change triggered ``calculating`` →
// "Calculating..." 1-liner → ``ready`` → multi-line table, which
// flickered the whole sidebar at the 1 Hz cadence. Holding the prior
// result keeps the layout stable; a small dot top-right of the block
// signals the inflight recalc.
type ApiState =
  | { status: 'idle' }
  | { status: 'calculating' }
  | { status: 'refreshing'; result: CalculateVolumeResponse }
  | { status: 'ready'; result: CalculateVolumeResponse }
  | { status: 'error'; error: string }

export function VolumeCalculator() {
  const selectedSymbol = useAppStore((s) => s.selectedSymbol)
  const side = useAppStore((s) => s.side)
  const entryPrice = useAppStore((s) => s.entryPrice)
  const slPrice = useAppStore((s) => s.slPrice)
  const tpPrice = useAppStore((s) => s.tpPrice)
  const riskAmount = useAppStore((s) => s.riskAmount)
  const manualVolumePrimary = useAppStore((s) => s.manualVolumePrimary)
  const setManualVolumePrimary = useAppStore((s) => s.setManualVolumePrimary)
  const setVolumeReady = useAppStore((s) => s.setVolumeReady)
  const setEffectiveVolumeLots = useAppStore((s) => s.setEffectiveVolumeLots)

  const debouncedEntry = useDebouncedValue(entryPrice, DEBOUNCE_MS)
  const debouncedSl = useDebouncedValue(slPrice, DEBOUNCE_MS)
  const debouncedRisk = useDebouncedValue(riskAmount, DEBOUNCE_MS)

  // Side validation runs synchronously every render — cheap, no debounce.
  // entrySlError is a HARD block (no API call); tpWarning is informational.
  const { entrySlError } = validateSideDirection(side, entryPrice, slPrice, tpPrice)

  const inputsValid =
    !!selectedSymbol &&
    debouncedEntry !== null &&
    debouncedSl !== null &&
    debouncedRisk > 0 &&
    debouncedEntry !== debouncedSl

  const [apiState, setApiState] = useState<ApiState>({ status: 'idle' })

  useEffect(() => {
    // Hard block: skip the API entirely on side-direction violation. We do
    // NOT setApiState here so any prior `ready.result` is preserved — when
    // the user fixes the direction, metrics return without a fetch.
    if (entrySlError) return
    if (!inputsValid || !selectedSymbol || debouncedEntry === null || debouncedSl === null) {
      return
    }

    let cancelled = false

    async function run(symbol: string, entry: number, sl: number, risk: number) {
      // Step 3.12c: hold the previous result during a recalc so the
      // operator never sees the layout swap between "Calculating..."
      // and the result block on each 5 s tick. Only the first-ever
      // calc (no prior ``ready`` / ``refreshing``) takes the bare
      // ``calculating`` path.
      setApiState((prev) => {
        if (prev.status === 'ready') {
          return { status: 'refreshing', result: prev.result }
        }
        if (prev.status === 'refreshing') {
          return prev
        }
        return { status: 'calculating' }
      })
      try {
        const result = await calculateVolume(symbol, {
          entry,
          sl,
          risk_amount: risk,
          ratio: RATIO_DEFAULT,
        })
        if (!cancelled) setApiState({ status: 'ready', result })
      } catch (err: unknown) {
        if (cancelled) return
        const status = (err as { response?: { status?: number } })?.response?.status
        const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail
        const fallback = (err as Error)?.message ?? 'Volume calc failed'
        const friendly =
          status === 503 ? 'Conversion rate not ready, retry...' : (detail ?? fallback)
        // API error: drop result so manual mode doesn't show stale metrics
        // for the (now invalid) entry/sl/risk combination.
        setApiState({ status: 'error', error: friendly })
      }
    }

    void run(selectedSymbol, debouncedEntry, debouncedSl, debouncedRisk)

    return () => {
      cancelled = true
    }
  }, [entrySlError, inputsValid, selectedSymbol, debouncedEntry, debouncedSl, debouncedRisk])

  const isManualMode = manualVolumePrimary !== null

  // Phase-3 submit gate: a usable volume exists when no side error blocks us
  // AND either auto succeeded or manual override is a positive number.
  // Step 3.11 piggy-backs ``effectiveVolumeLots`` here so the form's submit
  // handler can read the exact number to ship to POST /api/orders without
  // re-running ``calculateVolume`` itself.
  useEffect(() => {
    if (entrySlError) {
      setVolumeReady(false)
      setEffectiveVolumeLots(null)
      return
    }
    if (isManualMode && manualVolumePrimary !== null && manualVolumePrimary > 0) {
      setVolumeReady(true)
      setEffectiveVolumeLots(manualVolumePrimary)
      return
    }
    // Step 3.12c: ``refreshing`` carries the prior result and is still
    // a valid submit-time volume — drop-through to the same gate.
    if (apiState.status === 'ready' || apiState.status === 'refreshing') {
      setVolumeReady(true)
      setEffectiveVolumeLots(apiState.result.volume_primary)
      return
    }
    setVolumeReady(false)
    setEffectiveVolumeLots(null)
  }, [
    entrySlError,
    isManualMode,
    manualVolumePrimary,
    apiState,
    setVolumeReady,
    setEffectiveVolumeLots,
  ])

  function handleManualChange(e: ChangeEvent<HTMLInputElement>) {
    const raw = e.target.value
    if (raw === '') return // empty in manual mode would unintentionally drop back to auto
    const parsed = parseFloat(raw)
    if (Number.isNaN(parsed) || parsed < 0) return
    setManualVolumePrimary(parsed)
  }

  // ----- Render branches -----

  // Auto-mode side-direction violation: show the error + manual-override
  // unblock. Mirrors the auto-mode API error block to keep parity.
  if (!isManualMode && entrySlError) {
    return (
      <div className="space-y-2">
        <div className="text-xs text-red-600">{entrySlError}</div>
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

  // Auto-mode idle hint.
  if (!isManualMode && (!inputsValid || apiState.status === 'idle')) {
    return (
      <div className="text-xs text-gray-500 italic">Fill Entry, SL, Risk to preview volume.</div>
    )
  }

  if (!isManualMode && apiState.status === 'calculating') {
    return <div className="text-xs text-gray-500">Calculating...</div>
  }

  // Auto-mode API error: surface the message + offer manual override.
  if (!isManualMode && apiState.status === 'error') {
    return (
      <div className="space-y-2">
        <div className="text-xs text-red-600">{apiState.error}</div>
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

  // Auto-ready / refreshing / manual mode. Result preserved across
  // side-direction violations and across in-flight recalcs (step
  // 3.12c); cleared by API errors so we never show stale metrics for
  // an entry/sl that the server actively rejected.
  const result =
    apiState.status === 'ready' || apiState.status === 'refreshing' ? apiState.result : null
  const isRefreshing = apiState.status === 'refreshing'
  const effectiveVolP = isManualMode ? manualVolumePrimary : (result?.volume_primary ?? null)
  const effectiveVolS = effectiveVolP === null ? null : effectiveVolP * RATIO_DEFAULT

  return (
    <div className="space-y-2 text-xs relative">
      {/* Step 3.12c: subtle inflight-recalc indicator. Invisible
          99 % of the time (calculateVolume typically returns in
          < 300 ms); blip only shows on slow networks or during
          the brief window between debounce-fire and API resolve. */}
      {isRefreshing && (
        <div
          className="absolute -top-1 -right-1 w-1.5 h-1.5 rounded-full bg-blue-400 opacity-60"
          aria-hidden="true"
          title="Đang cập nhật..."
        />
      )}
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

      {/* SL pips + Est. SL $ are price-derived (sl_pips depends only on
          entry/sl/symbol; sl_usd_per_lot doesn't depend on volume), so they
          are valid in both auto and manual modes once we have a `result`.
          Substituting effectiveVolP into the $ calc gives the manual-mode
          number too. Hidden during a side-direction violation so we don't
          imply the order is placeable while it's blocked. */}
      {result && effectiveVolP !== null && !entrySlError && (
        <>
          <div className="flex justify-between">
            <span className="text-gray-600">SL distance:</span>
            <span className="font-mono">{result.sl_pips.toFixed(1)} pips</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-600">Est. SL $:</span>
            <span className="font-mono">${(result.sl_usd_per_lot * effectiveVolP).toFixed(2)}</span>
          </div>
        </>
      )}

      {/* Manual-mode side error: inline message kept next to the editable
          Vol P so the user sees why submit will be blocked. */}
      {isManualMode && entrySlError && <div className="text-xs text-red-600">{entrySlError}</div>}

      <div className="border-t border-gray-100 pt-1.5">
        {isManualMode ? (
          <button
            type="button"
            onClick={() => setManualVolumePrimary(null)}
            className="text-xs text-blue-600 hover:underline"
          >
            ↻ Reset to auto
          </button>
        ) : result ? (
          <button
            type="button"
            onClick={() => setManualVolumePrimary(result.volume_primary)}
            className="text-xs text-gray-500 hover:text-blue-600 hover:underline"
          >
            Override manually
          </button>
        ) : null}
      </div>
    </div>
  )
}
