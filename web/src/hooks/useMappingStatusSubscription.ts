// Phase 4.A.6: subscribe to ``mapping_status:{exness_account_id}`` channels
// for a list of accounts. Caller is the SettingsModal — it knows which
// Exness accounts are visible in the Accounts tab and only subscribes
// while that tab is open.
//
// Pattern mirrors the WS subscribe/unsubscribe flow used by the chart
// (set_symbol) — emit explicit subscribe + unsubscribe messages around
// the lifecycle, dispatch comes through ``useWebSocket``'s onmessage.

import { useEffect } from 'react'
import { symbolMappingApi } from '../api/symbolMapping'
import { useAppStore } from '../store'
import { sendWsMessage } from './useWebSocket'

/**
 * Subscribe to mapping_status channels for ``exnessAccountIds`` while the
 * containing component is mounted. Also seeds the store via REST so the
 * initial render shows the correct status without waiting for the next WS
 * broadcast (which is event-driven, not periodic).
 *
 * Uses the module-level ``sendWsMessage`` helper from ``useWebSocket`` so
 * we don't open a second WebSocket connection (the hook is intentionally
 * called only once at MainPage).
 */
export function useMappingStatusSubscription(exnessAccountIds: string[]) {
  const setStatus = useAppStore((s) => s.setMappingStatusForAccount)

  useEffect(() => {
    if (exnessAccountIds.length === 0) return
    const channels = exnessAccountIds.map((id) => `mapping_status:${id}`)
    sendWsMessage({ type: 'subscribe', channels })

    // REST seed — populates the store before the next event-driven push.
    let cancelled = false
    void Promise.all(
      exnessAccountIds.map(async (id) => {
        try {
          const resp = await symbolMappingApi.getMappingStatus(id)
          if (!cancelled) setStatus(id, resp.status)
        } catch {
          // 404 = no status yet → leave at default ('pending_mapping' on read).
        }
      }),
    )

    return () => {
      cancelled = true
      sendWsMessage({ type: 'unsubscribe', channels })
    }
    // ``setStatus`` is a stable ref; depending only on the ID list keeps
    // re-subscribes scoped to actual changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exnessAccountIds.join(',')])
}
