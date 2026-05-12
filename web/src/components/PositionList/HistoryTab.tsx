import { useCallback, useEffect } from 'react'
import toast from 'react-hot-toast'
import { listHistory } from '../../api/client'
import { useAppStore } from '../../store'
import { OrderRow } from './OrderRow'

const SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
const HISTORY_FETCH_LIMIT = 200

/**
 * Closed-order history (trailing 7-day window).
 *
 * Step 3.10 only fetches on mount + tab change; future steps could
 * subscribe to a ``history``-channel broadcast when new closes
 * land, but for Phase 3 the WS-driven ``upsertOrder`` already
 * removes filled rows from the Open tab; on close the order moves
 * to the History list via the next REST refresh (or the operator
 * can switch tabs to re-fetch).
 */
export function HistoryTab() {
  const history = useAppStore((s) => s.history)
  const setHistory = useAppStore((s) => s.setHistory)

  const refresh = useCallback(async () => {
    try {
      const now = Date.now()
      const res = await listHistory({
        from_ts: now - SEVEN_DAYS_MS,
        to_ts: now,
        limit: HISTORY_FETCH_LIMIT,
      })
      setHistory(res.history)
    } catch {
      toast.error('Failed to load history')
    }
  }, [setHistory])

  useEffect(() => {
    void refresh()
  }, [refresh])

  if (history.length === 0) {
    return (
      <div className="p-6 text-center text-gray-400">
        <p className="text-sm font-medium">No closed positions yet</p>
        <p className="text-xs mt-1">Trailing 7-day close history will populate here.</p>
      </div>
    )
  }

  return (
    <table className="w-full text-sm">
      <thead className="bg-gray-50 border-b text-xs uppercase tracking-wide text-gray-600 sticky top-0">
        <tr>
          <th className="px-4 py-2 text-left">Pair</th>
          <th className="px-4 py-2 text-left">Symbol</th>
          <th className="px-4 py-2 text-left">Side</th>
          <th className="px-4 py-2 text-right">Volume</th>
          <th className="px-4 py-2 text-right">Entry</th>
          <th className="px-4 py-2 text-right">Close</th>
          <th className="px-4 py-2 text-right">P&amp;L</th>
          <th className="px-4 py-2 text-left">Reason</th>
          <th className="px-4 py-2 text-left">Closed at</th>
        </tr>
      </thead>
      <tbody>
        {history.map((o) => (
          <OrderRow key={o.order_id} order={o} />
        ))}
      </tbody>
    </table>
  )
}
