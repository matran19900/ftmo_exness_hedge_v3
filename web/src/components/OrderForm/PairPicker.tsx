import { useEffect, useState } from 'react'
import { listPairs, type PairResponse } from '../../api/client'
import { useAppStore } from '../../store'

export function PairPicker() {
  const selectedPairId = useAppStore((s) => s.selectedPairId)
  const setSelectedPairId = useAppStore((s) => s.setSelectedPairId)

  const [pairs, setPairs] = useState<PairResponse[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false

    async function load() {
      setLoading(true)
      setError(null)
      try {
        const data = await listPairs()
        if (cancelled) return
        setPairs(data)
        // Auto-select the first pair when nothing is selected yet OR when
        // the persisted ``selectedPairId`` from a previous session no longer
        // exists in the freshly-fetched list. Reading via getState() keeps
        // this effect a mount-once load (avoids reloading on every store
        // update). The membership check (step 3.11a) prevents a stale
        // localStorage UUID from sitting silently as the form's value
        // attribute while the <option> list shows different IDs — that
        // mismatch was sending the wrong pair_id to POST /api/orders and
        // surfacing as a 404 ``pair_not_found``.
        const currentSelected = useAppStore.getState().selectedPairId
        const isSelectedValid =
          currentSelected !== null && data.some((p) => p.pair_id === currentSelected)
        if (data.length > 0 && !isSelectedValid) {
          const first = data[0]
          if (first) setSelectedPairId(first.pair_id)
        }
      } catch (err) {
        if (cancelled) return
        const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail
        setError(detail ?? (err as Error)?.message ?? 'Failed to load pairs')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void load()
    return () => {
      cancelled = true
    }
  }, [setSelectedPairId])

  if (loading) {
    return <div className="text-xs text-gray-500">Loading pairs...</div>
  }

  if (error) {
    return <div className="text-xs text-red-600">{error}</div>
  }

  if (pairs.length === 0) {
    return (
      <div className="text-xs text-gray-500 italic">
        No pairs configured. Create one via API or Settings (Phase 4).
      </div>
    )
  }

  return (
    <div>
      <label htmlFor="pair-select" className="block text-xs font-medium text-gray-600 mb-1">
        Pair
      </label>
      <select
        id="pair-select"
        value={selectedPairId ?? ''}
        onChange={(e) => setSelectedPairId(e.target.value || null)}
        className="w-full px-3 py-1.5 bg-white border border-gray-300 rounded text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
      >
        {pairs.map((pair) => (
          <option key={pair.pair_id} value={pair.pair_id}>
            {pair.name} (ratio {pair.ratio})
          </option>
        ))}
      </select>
    </div>
  )
}
