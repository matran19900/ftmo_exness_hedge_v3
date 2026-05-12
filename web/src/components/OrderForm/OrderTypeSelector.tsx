import { useAppStore, type OrderType } from '../../store'

const TYPES: { value: OrderType; label: string }[] = [
  { value: 'market', label: 'Market' },
  { value: 'limit', label: 'Limit' },
  { value: 'stop', label: 'Stop' },
]

/**
 * Segmented 3-button order-type selector (step 3.12b).
 *
 * Reads ``orderType`` from the persisted store slice — the operator's
 * last-used choice survives page reloads.
 */
export function OrderTypeSelector() {
  const orderType = useAppStore((s) => s.orderType)
  const setOrderType = useAppStore((s) => s.setOrderType)

  return (
    <div>
      <label className="block text-xs font-medium text-gray-600 mb-1">Order Type</label>
      <div className="flex gap-1" role="radiogroup" aria-label="Order type">
        {TYPES.map((t) => {
          const active = orderType === t.value
          return (
            <button
              key={t.value}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => setOrderType(t.value)}
              className={`flex-1 px-3 py-1.5 text-sm rounded border transition-colors ${
                active
                  ? 'bg-blue-500 text-white border-blue-500'
                  : 'bg-white text-gray-700 border-gray-300 hover:bg-gray-50'
              }`}
            >
              {t.label}
            </button>
          )
        })}
      </div>
    </div>
  )
}
