import { useCallback, useEffect } from 'react'
import toast from 'react-hot-toast'
import { listPositions } from '../../api/client'
import { useAppStore } from '../../store'
import { PositionRow } from './PositionRow'

/**
 * Live positions list.
 *
 * Initial mount: REST ``GET /api/positions`` populates the store.
 * Thereafter ``useWebSocket`` (mounted higher in the tree) merges
 * 1 Hz ``positions_tick`` broadcasts from step 3.8's
 * ``position_tracker_loop`` into the same store slice, so live P&L
 * updates without any extra fetch.
 *
 * ``onActionDone`` is passed to each row so the Close + Modify
 * buttons can trigger a REST refresh after their respective 202
 * dispatches resolve — the broker's eventual fill / amend arrives
 * via the WS broadcast a moment later.
 */
export function OpenTab() {
  const positions = useAppStore((s) => s.positions)
  const setPositions = useAppStore((s) => s.setPositions)

  const refresh = useCallback(async () => {
    try {
      const res = await listPositions()
      setPositions(res.positions)
    } catch {
      toast.error('Failed to load positions')
    }
  }, [setPositions])

  useEffect(() => {
    void refresh()
  }, [refresh])

  if (positions.length === 0) {
    return (
      <div className="p-6 text-center text-gray-400">
        <p className="text-sm font-medium">No open positions</p>
        <p className="text-xs mt-1">Filled hedge orders appear here with live P&amp;L.</p>
      </div>
    )
  }

  return (
    <table className="w-full text-sm">
      <thead className="bg-gray-50 border-b text-xs uppercase tracking-wide text-gray-600 sticky top-0">
        <tr>
          <th className="px-4 py-2 text-left">Symbol</th>
          <th className="px-4 py-2 text-left">Side</th>
          <th className="px-4 py-2 text-right">Volume</th>
          <th className="px-4 py-2 text-right">Entry</th>
          <th className="px-4 py-2 text-right">Current</th>
          <th className="px-4 py-2 text-right">SL / TP</th>
          <th className="px-4 py-2 text-right">P&amp;L</th>
          <th className="px-4 py-2 text-center">Actions</th>
        </tr>
      </thead>
      <tbody>
        {positions.map((p) => (
          <PositionRow key={p.order_id} position={p} onActionDone={refresh} />
        ))}
      </tbody>
    </table>
  )
}
