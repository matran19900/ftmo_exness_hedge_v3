import { useAppStore } from '../../store'
import { PairPicker } from './PairPicker'
import { PriceInput } from './PriceInput'
import { RiskAmountInput } from './RiskAmountInput'
import { SideSelector } from './SideSelector'

export function HedgeOrderForm() {
  const selectedSymbol = useAppStore((s) => s.selectedSymbol)
  const symbolDigits = useAppStore((s) => s.symbolDigits)
  const entryPrice = useAppStore((s) => s.entryPrice)
  const setEntryPrice = useAppStore((s) => s.setEntryPrice)
  const slPrice = useAppStore((s) => s.slPrice)
  const setSlPrice = useAppStore((s) => s.setSlPrice)
  const tpPrice = useAppStore((s) => s.tpPrice)
  const setTpPrice = useAppStore((s) => s.setTpPrice)

  return (
    <div className="h-full bg-white border border-gray-200 rounded p-4 flex flex-col gap-3 overflow-y-auto">
      <h2 className="text-sm font-semibold text-gray-700">New Order</h2>

      <PairPicker />

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">Symbol</label>
        <div className="px-3 py-1.5 bg-gray-50 border border-gray-200 rounded text-sm font-mono text-gray-700">
          {selectedSymbol ?? '—'}
        </div>
      </div>

      <SideSelector />

      <PriceInput
        label="Entry Price"
        value={entryPrice}
        onChange={setEntryPrice}
        digits={symbolDigits}
      />
      <PriceInput label="Stop Loss" value={slPrice} onChange={setSlPrice} digits={symbolDigits} />
      <PriceInput
        label="Take Profit (optional)"
        value={tpPrice}
        onChange={setTpPrice}
        digits={symbolDigits}
      />

      <RiskAmountInput />

      <div className="border-t border-gray-200 pt-3">
        <div className="text-xs text-gray-500 italic">Volume preview: step 2.9</div>
      </div>

      <button
        type="button"
        disabled
        title="Order placement will be implemented in Phase 3"
        className="w-full py-2 bg-gray-300 text-gray-500 rounded text-sm font-bold cursor-not-allowed"
      >
        Place Hedge Order
      </button>
    </div>
  )
}
