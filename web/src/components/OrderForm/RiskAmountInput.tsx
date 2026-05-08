import type { ChangeEvent } from 'react'
import { useAppStore } from '../../store'

export function RiskAmountInput() {
  const riskAmount = useAppStore((s) => s.riskAmount)
  const setRiskAmount = useAppStore((s) => s.setRiskAmount)

  function handleChange(e: ChangeEvent<HTMLInputElement>) {
    const raw = e.target.value
    // Empty / NaN / negative → ignore (we keep the last valid value rather
    // than allowing an undefined risk).
    if (raw === '') return
    const parsed = parseFloat(raw)
    if (Number.isNaN(parsed) || parsed < 0) return
    setRiskAmount(parsed)
  }

  return (
    <div>
      <label htmlFor="risk-amount" className="block text-xs font-medium text-gray-600 mb-1">
        Risk Amount (USD)
      </label>
      <input
        id="risk-amount"
        type="number"
        step="1"
        min="0"
        value={riskAmount}
        onChange={handleChange}
        className="w-full px-3 py-1.5 bg-white border border-gray-300 rounded text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
    </div>
  )
}
