import type { ChangeEvent, MouseEvent } from 'react'

interface Props {
  label: string
  value: number | null
  onChange: (value: number | null) => void
  digits: number
  placeholder?: string
  warning?: string
}

export function PriceInput({ label, value, onChange, digits, placeholder, warning }: Props) {
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

  function handleClear(e: MouseEvent<HTMLButtonElement>) {
    e.preventDefault()
    onChange(null)
  }

  // Display the raw stored number — formatting on each keystroke would fight
  // the user mid-typing (e.g. "1.0" → "1.00000" while they intend "1.085").
  const displayValue = value === null ? '' : value.toString()
  const showClear = value !== null

  return (
    <div>
      <label className="block text-xs font-medium text-gray-600 mb-1">{label}</label>
      <div className="relative">
        <input
          type="number"
          step={Math.pow(10, -digits)}
          value={displayValue}
          onChange={handleChange}
          placeholder={placeholder ?? '—'}
          className={`w-full px-3 py-1.5 ${showClear ? 'pr-7' : ''} bg-white border border-gray-300 rounded text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500`}
        />
        {showClear && (
          <button
            type="button"
            onClick={handleClear}
            aria-label={`Clear ${label}`}
            title={`Clear ${label}`}
            className="absolute right-1 top-1/2 -translate-y-1/2 w-5 h-5 flex items-center justify-center text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded text-xs leading-none"
          >
            ×
          </button>
        )}
      </div>
      {warning && <p className="text-[11px] text-red-600 mt-0.5">{warning}</p>}
    </div>
  )
}
