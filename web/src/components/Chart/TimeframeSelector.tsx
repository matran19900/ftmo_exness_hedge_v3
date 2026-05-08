import { TIMEFRAMES, type Timeframe } from '../../api/client'

interface Props {
  selected: Timeframe
  onSelect: (tf: Timeframe) => void
}

export function TimeframeSelector({ selected, onSelect }: Props) {
  return (
    <div className="flex gap-1">
      {TIMEFRAMES.map((tf) => (
        <button
          key={tf}
          onClick={() => onSelect(tf)}
          className={`px-2.5 py-1 text-xs font-medium rounded transition-colors ${
            tf === selected
              ? 'bg-blue-600 text-white'
              : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
          }`}
          type="button"
        >
          {tf}
        </button>
      ))}
    </div>
  )
}
