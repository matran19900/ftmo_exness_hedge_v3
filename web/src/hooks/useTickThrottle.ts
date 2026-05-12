import { useEffect } from 'react'
import { useAppStore } from '../store'

const THROTTLE_INTERVAL_MS = 5000

/**
 * Drive the ``tickThrottled`` store slice from ``latestTick`` on a
 * 5 s cadence (step 3.12c — relaxed from 1 s in step 3.12b).
 *
 * WHY: the WS publishes ticks at ~5–10 Hz on liquid FX. If the
 * VolumeCalculator + market-mode entry preview reacted to every
 * tick, the displayed volume number would jitter visibly and the
 * ``calculateVolume`` debounce (300 ms) would keep firing API calls
 * back-to-back. Throttling at the store boundary keeps the UI calm
 * without losing the "live" feel.
 *
 * 3.12b started at 1 s. Operator feedback during active markets:
 * the 1 Hz layout swap between "Calculating..." and the result block
 * was distracting and the submit button's volume label flickered.
 * 3.12c bumps to 5 s AND VolumeCalculator now holds the previous
 * result during recalc (see ``ApiState.refreshing``) — together they
 * make the form feel stable during market-mode sizing. The server
 * fills at the actual tick on submit, so the on-screen preview
 * lagging the real market by up to ~5 s is acceptable for the
 * sizing-feedback purpose.
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
    // to wait 5 s before market-mode entry populates.
    snapshot()

    const interval = setInterval(snapshot, THROTTLE_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [setTickThrottled])
}
