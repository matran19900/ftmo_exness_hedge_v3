import { useEffect } from 'react'
import { useAppStore } from '../store'

const THROTTLE_INTERVAL_MS = 1000

/**
 * Drive the ``tickThrottled`` store slice from ``latestTick`` on a
 * 1 Hz cadence (step 3.12b).
 *
 * WHY: the WS publishes ticks at ~5–10 Hz on liquid FX. If the
 * VolumeCalculator + market-mode entry preview reacted to every
 * tick, the displayed volume number would jitter visibly and the
 * ``calculateVolume`` debounce (300 ms) would keep firing API calls
 * back-to-back. Throttling to 1 Hz at the store boundary keeps the
 * UI calm without losing the "live" feel.
 *
 * Symbol snapshot: ``latestTick`` itself doesn't carry the symbol
 * (the WS handler filters by symbol before storing it), but the
 * downstream auto-driver in HedgeOrderForm wants a defensive check
 * for the race window between a symbol change and the next throttle
 * tick. So we attach the current ``selectedSymbol`` at copy time.
 *
 * Null handling: if ``latestTick`` is null OR either side is null
 * (cTrader delta-tick coalesced into a half-tick was already filtered
 * by step 3.11b, but defensive in case the bug ever returns) we
 * write ``null`` to the throttled slice. Consumers treat null as
 * "no entry preview available yet" and disable the affected UI.
 *
 * Call once from MainPage; idempotent across re-renders (the
 * setInterval is owned by a single useEffect with stable deps).
 */
export function useTickThrottle(): void {
  const setTickThrottled = useAppStore((s) => s.setTickThrottled)

  useEffect(() => {
    function snapshot(): void {
      const fresh = useAppStore.getState().latestTick
      const sym = useAppStore.getState().selectedSymbol
      if (fresh && sym && fresh.bid !== null && fresh.ask !== null) {
        setTickThrottled({ bid: fresh.bid, ask: fresh.ask, ts: fresh.ts, symbol: sym })
      } else {
        setTickThrottled(null)
      }
    }

    // Seed immediately so the first render after mount doesn't have
    // to wait a full second before market-mode entry populates.
    snapshot()

    const interval = setInterval(snapshot, THROTTLE_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [setTickThrottled])
}
