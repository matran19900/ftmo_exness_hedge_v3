import { useAppStore } from '../../store'

interface Props {
  x: number // viewport-relative pixel (clientX)
  y: number // viewport-relative pixel (clientY)
  price: number
  digits: number
  onClose: () => void
}

export function ChartContextMenu({ x, y, price, digits, onClose }: Props) {
  const setEntryPrice = useAppStore((s) => s.setEntryPrice)
  const setSlPrice = useAppStore((s) => s.setSlPrice)
  const setTpPrice = useAppStore((s) => s.setTpPrice)

  const formatted = price.toFixed(digits)

  function handleSet(setter: (p: number) => void) {
    setter(price)
    onClose()
  }

  return (
    <div
      className="fixed bg-white border border-gray-200 rounded shadow-lg py-1 z-50 text-sm min-w-[180px]"
      style={{ left: x, top: y }}
      onMouseLeave={onClose}
    >
      <button
        type="button"
        onClick={() => handleSet(setEntryPrice)}
        className="w-full text-left px-3 py-1.5 hover:bg-blue-50 flex justify-between gap-3"
      >
        <span>Set as Entry</span>
        <span className="font-mono text-blue-600">{formatted}</span>
      </button>
      <button
        type="button"
        onClick={() => handleSet(setSlPrice)}
        className="w-full text-left px-3 py-1.5 hover:bg-red-50 flex justify-between gap-3"
      >
        <span>Set as SL</span>
        <span className="font-mono text-red-600">{formatted}</span>
      </button>
      <button
        type="button"
        onClick={() => handleSet(setTpPrice)}
        className="w-full text-left px-3 py-1.5 hover:bg-green-50 flex justify-between gap-3"
      >
        <span>Set as TP</span>
        <span className="font-mono text-green-600">{formatted}</span>
      </button>
    </div>
  )
}
