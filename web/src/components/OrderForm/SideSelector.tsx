import { useAppStore } from '../../store'

export function SideSelector() {
  const side = useAppStore((s) => s.side)
  const setSide = useAppStore((s) => s.setSide)

  return (
    <div>
      <label className="block text-xs font-medium text-gray-600 mb-1">Side</label>
      <div className="grid grid-cols-2 gap-2">
        <button
          type="button"
          onClick={() => setSide('buy')}
          className={`py-2 text-sm font-bold rounded transition-colors ${
            side === 'buy'
              ? 'bg-green-600 text-white shadow-sm'
              : 'bg-green-50 text-green-700 hover:bg-green-100'
          }`}
        >
          BUY
        </button>
        <button
          type="button"
          onClick={() => setSide('sell')}
          className={`py-2 text-sm font-bold rounded transition-colors ${
            side === 'sell'
              ? 'bg-red-600 text-white shadow-sm'
              : 'bg-red-50 text-red-700 hover:bg-red-100'
          }`}
        >
          SELL
        </button>
      </div>
    </div>
  )
}
