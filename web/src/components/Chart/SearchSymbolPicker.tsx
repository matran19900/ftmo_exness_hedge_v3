import { useEffect, useMemo, useRef, useState } from 'react'
import { getSymbols } from '../../api/client'

interface Props {
  selected: string | null
  onSelect: (symbol: string) => void
}

const RESULT_LIMIT = 50

export function SearchSymbolPicker({ selected, onSelect }: Props) {
  const [allSymbols, setAllSymbols] = useState<string[]>([])
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      try {
        const res = await getSymbols()
        if (cancelled) return
        setAllSymbols(res.symbols)
        setError(null)
      } catch (err) {
        if (cancelled) return
        const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail
        setError(detail ?? 'Failed to load symbols')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [])

  const filtered = useMemo(() => {
    const q = query.trim().toUpperCase()
    if (!q) return allSymbols.slice(0, RESULT_LIMIT)
    return allSymbols.filter((s) => s.toUpperCase().includes(q)).slice(0, RESULT_LIMIT)
  }, [query, allSymbols])

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  function handleSelect(symbol: string) {
    onSelect(symbol)
    setOpen(false)
    setQuery('')
  }

  return (
    <div ref={containerRef} className="relative w-48">
      <button
        onClick={() => setOpen((prev) => !prev)}
        className="w-full text-left px-3 py-1.5 bg-white border border-gray-300 rounded text-sm font-medium hover:border-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500"
        type="button"
      >
        {selected ?? 'Select symbol...'}
        <span className="float-right text-gray-400">▾</span>
      </button>

      {open && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-white border border-gray-200 rounded shadow-lg z-10">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search..."
            autoFocus
            className="w-full px-3 py-2 border-b border-gray-200 text-sm focus:outline-none"
          />

          {loading && <div className="px-3 py-2 text-sm text-gray-500">Loading...</div>}
          {error && <div className="px-3 py-2 text-sm text-red-600">{error}</div>}

          {!loading && !error && (
            <div className="max-h-64 overflow-y-auto">
              {filtered.length === 0 ? (
                <div className="px-3 py-2 text-sm text-gray-500">No symbols match</div>
              ) : (
                filtered.map((sym) => (
                  <button
                    key={sym}
                    onClick={() => handleSelect(sym)}
                    className={`w-full text-left px-3 py-1.5 text-sm hover:bg-blue-50 ${
                      sym === selected ? 'bg-blue-100 font-semibold' : ''
                    }`}
                    type="button"
                  >
                    {sym}
                  </button>
                ))
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
