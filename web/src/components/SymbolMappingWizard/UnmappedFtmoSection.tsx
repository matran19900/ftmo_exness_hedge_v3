import { useState } from 'react'

interface Props {
  symbols: string[]
}

export function UnmappedFtmoSection({ symbols }: Props) {
  const [expanded, setExpanded] = useState(false)
  if (symbols.length === 0) return null
  return (
    <section
      data-testid="unmapped-ftmo-section"
      className="border-t bg-gray-50 px-4 py-2"
    >
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="text-sm text-gray-700 font-semibold hover:underline"
      >
        {expanded ? '▼' : '▶'} {symbols.length} FTMO symbol
        {symbols.length === 1 ? '' : 's'} without Exness match
      </button>
      {expanded && (
        <ul className="mt-2 ml-4 text-xs font-mono text-gray-600 space-y-0.5">
          {symbols.map((s) => (
            <li key={s}>
              {s} — cannot be traded on this Exness account.
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
