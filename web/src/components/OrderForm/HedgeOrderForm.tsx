import { useEffect, useRef } from 'react'
import { useAppStore, type OrderSide } from '../../store'
import { PairPicker } from './PairPicker'
import { PriceInput } from './PriceInput'
import { RiskAmountInput } from './RiskAmountInput'
import { SideSelector } from './SideSelector'
import { VolumeCalculator } from './VolumeCalculator'

interface ValidationWarnings {
  sl?: string
  tp?: string
}

// Soft validation: returns inline warnings only. Volume calc still runs even
// when the side direction is violated — server validates SL distance and the
// user may have a deliberate non-stop-loss configuration.
function getValidationWarnings(
  side: OrderSide,
  entry: number | null,
  sl: number | null,
  tp: number | null
): ValidationWarnings {
  const warnings: ValidationWarnings = {}
  if (entry === null) return warnings

  if (side === 'buy') {
    if (sl !== null && sl >= entry) warnings.sl = 'SL must be below Entry for BUY'
    if (tp !== null && tp <= entry) warnings.tp = 'TP must be above Entry for BUY'
  } else {
    if (sl !== null && sl <= entry) warnings.sl = 'SL must be above Entry for SELL'
    if (tp !== null && tp >= entry) warnings.tp = 'TP must be below Entry for SELL'
  }

  return warnings
}

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

  const warnings = getValidationWarnings(side, entryPrice, slPrice, tpPrice)

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
        warning={warnings.sl}
      />
      <PriceInput
        label="Take Profit (optional)"
        value={tpPrice}
        onChange={setTpPrice}
        digits={symbolDigits}
        warning={warnings.tp}
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
