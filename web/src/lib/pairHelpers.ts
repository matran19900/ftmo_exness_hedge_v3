import type { PairResponse } from '../api/client'

/**
 * Resolve a ``pair_id`` (UUID) to its operator-visible name.
 *
 * Step 3.12a: PositionRow + OrderRow both need to render a pair-name
 * column from the same cached ``pairs`` slice. Centralising the lookup
 * keeps the fallback behaviour consistent — when the pair list hasn't
 * loaded yet (transient at page open), or the order references a pair
 * that's been removed since the cache was built, we show the first 8
 * chars of the UUID + ``...``. That's still enough for an operator to
 * disambiguate two orders visually and to grep for the full id in
 * server logs if needed.
 */
export function lookupPairName(pairs: PairResponse[], pair_id: string): string {
  if (!pair_id) return '—'
  const found = pairs.find((p) => p.pair_id === pair_id)
  if (found) return found.name
  return pair_id.length > 8 ? `${pair_id.slice(0, 8)}...` : pair_id
}
