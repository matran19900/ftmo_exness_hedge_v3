import { useEffect, useRef } from 'react'
import { validateSideDirection } from '../../lib/orderValidation'
import { useAppStore } from '../../store'
import { PairPicker } from './PairPicker'
import { PriceInput } from './PriceInput'
import { RiskAmountInput } from './RiskAmountInput'
import { SideSelector } from './SideSelector'
import { VolumeCalculator } from './VolumeCalculator'

export function HedgeOrderForm() {
  const selectedSymbol = useAppStore((s) => s.selectedSymbol)
  const symbolDigits = useAppStore((s) => s.symbolDigits)
  const side = useAppStore((s) => s.side)
  const entryPrice = useAppStore((s) => s.entryPrice)
  const setEntryPrice = useAppStore((s) => s.setEntryPrice)
  const slPrice = useAppStore((s) => s.slPrice)
  const setSlPrice = useAppStore((s) => s.setSlPrice)
  const tpPrice = useAppStore((s) => s.tpPrice)
  const setTpPrice = useAppStore((s) => s.setTpPrice)
  const setManualVolumePrimary = useAppStore((s) => s.setManualVolumePrimary)

  // Reset draft prices + volume override on actual symbol transitions. The
  // prevSymbolRef guard is critical: on initial mount the store may already
  // hold a persisted selectedSymbol, and we must NOT wipe Entry/SL/TP on
  // first render (the user might be returning to a fresh form anyway, but
  // this also avoids transient null state races during persist hydration).
  const prevSymbolRef = useRef<string | null>(selectedSymbol)
  useEffect(() => {
    if (prevSymbolRef.current !== null && prevSymbolRef.current !== selectedSymbol) {
      setEntryPrice(null)
      setSlPrice(null)
      setTpPrice(null)
      setManualVolumePrimary(null)
    }
    prevSymbolRef.current = selectedSymbol
  }, [selectedSymbol, setEntryPrice, setSlPrice, setTpPrice, setManualVolumePrimary])

  // entrySlError is also enforced as a hard block inside VolumeCalculator;
  // this displays the same message inline under the SL input. tpWarning
  // stays soft (TP is optional and a wrong-direction TP doesn't prevent a
  // valid order from being placed).
  const { entrySlError, tpWarning } = validateSideDirection(side, entryPrice, slPrice, tpPrice)

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
      <PriceInput
        label="Stop Loss"
        value={slPrice}
        onChange={setSlPrice}
        digits={symbolDigits}
        warning={entrySlError ?? undefined}
      />
      <PriceInput
        label="Take Profit (optional)"
        value={tpPrice}
        onChange={setTpPrice}
        digits={symbolDigits}
        warning={tpWarning ?? undefined}
      />

      <RiskAmountInput />

      <div className="border-t border-gray-200 pt-3">
        <div className="text-xs font-medium text-gray-600 mb-2">Volume Preview</div>
        <VolumeCalculator />
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
