import { useEffect } from 'react'
import { useAppStore } from '../../store'

export function PairPicker() {
  const selectedPairId = useAppStore((s) => s.selectedPairId)
  const setSelectedPairId = useAppStore((s) => s.setSelectedPairId)
  // Step 3.12a: pairs are owned by the store; MainPage performs the
  // one-shot fetch on mount. PairPicker no longer self-fetches — it
  // just reads the cached list. While the fetch is in flight the
  // dropdown is in its "no pairs configured" placeholder state below.
  const pairs = useAppStore((s) => s.pairs)

  // D-114 / step 3.11a stale-pair validation: when the persisted
  // ``selectedPairId`` from a previous session is no longer in the
  // freshly-fetched ``pairs`` list (pair removed server-side, or
  // first-ever load), auto-select the first pair so the form's
  // ``value`` attribute always matches a visible <option> — otherwise
  // the form silently sends the wrong pair_id and the server returns
  // 404 ``pair_not_found``. Reading via getState() avoids re-firing
  // on every selectedPairId change.
  useEffect(() => {
    if (pairs.length === 0) return
    const currentSelected = useAppStore.getState().selectedPairId
    const isSelectedValid =
      currentSelected !== null && pairs.some((p) => p.pair_id === currentSelected)
    if (!isSelectedValid) {
      const first = pairs[0]
      if (first) setSelectedPairId(first.pair_id)
    }
  }, [pairs, setSelectedPairId])

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
