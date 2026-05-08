import type { ChangeEvent } from 'react'

interface Props {
  label: string
  value: number | null
  onChange: (value: number | null) => void
  digits: number
  placeholder?: string
}

export function PriceInput({ label, value, onChange, digits, placeholder }: Props) {
  function handleChange(e: ChangeEvent<HTMLInputElement>) {
    const raw = e.target.value
    if (raw === '') {
      onChange(null)
      return
    }
    const parsed = parseFloat(raw)
    if (Number.isNaN(parsed)) return
    onChange(parsed)
  }

  // Display the raw stored number — formatting on each keystroke would fight
  // the user mid-typing (e.g. "1.0" → "1.00000" while they intend "1.085").
  const displayValue = value === null ? '' : value.toString()

  return (
    <div>
      <label className="block text-xs font-medium text-gray-600 mb-1">{label}</label>
      <input
        type="number"
        step={Math.pow(10, -digits)}
        value={displayValue}
        onChange={handleChange}
        placeholder={placeholder ?? '—'}
        className="w-full px-3 py-1.5 bg-white border border-gray-300 rounded text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
    </div>
  )
}
