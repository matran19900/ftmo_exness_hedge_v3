import { useState } from 'react'

interface Props {
  symbols: string[]
}

export function UnmappedExnessSection({ symbols }: Props) {
  const [expanded, setExpanded] = useState(false)
  if (symbols.length === 0) return null
  return (
    <section
      data-testid="unmapped-exness-section"
      className="border-t bg-gray-50 px-4 py-2"
    >
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="text-sm text-gray-700 font-semibold hover:underline"
      >
        {expanded ? '▼' : '▶'} {symbols.length} Exness symbol
        {symbols.length === 1 ? '' : 's'} not claimed by any FTMO mapping
      </button>
      {expanded && (
        <ul className="mt-2 ml-4 text-xs font-mono text-gray-600 space-y-0.5">
          {symbols.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
    </section>
  )
}
