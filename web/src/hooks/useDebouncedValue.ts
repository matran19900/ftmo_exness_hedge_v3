import { useEffect, useState } from 'react'

/**
 * Returns a debounced version of `value` that updates only after `delayMs` of
 * no further changes. Use to gate side effects (API calls, expensive work)
 * on stable input — the debounced value is safe to put in a useEffect dep
 * without firing on every keystroke.
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)

  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(value), delayMs)
    return () => window.clearTimeout(handle)
  }, [value, delayMs])

  return debounced
}
