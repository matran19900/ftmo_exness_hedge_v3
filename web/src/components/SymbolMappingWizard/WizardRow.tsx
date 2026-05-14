import { useAppStore } from '../../store'
import type { WizardRowState } from '../../store'
import type { Confidence, RawSymbolResponse } from '../../api/types/symbolMapping'

interface Props {
  row: WizardRowState
  showAdvancedSpecs: boolean
  showAllExness: boolean
  availableExness: RawSymbolResponse[]
}

const CONFIDENCE_BADGE: Record<Confidence, string> = {
  high: 'bg-green-100 text-green-800',
  medium: 'bg-amber-100 text-amber-800',
  low: 'bg-orange-100 text-orange-800',
}

export function WizardRow({
  row,
  showAdvancedSpecs,
  showAllExness,
  availableExness,
}: Props) {
  const updateAction = useAppStore((s) => s.updateRowAction)
  const updateOverride = useAppStore((s) => s.updateRowOverride)

  const filteredExness = showAllExness
    ? availableExness
    : availableExness.filter((s) => s.name === row.proposed_exness || s.name === row.override_value)

  const isOverride = row.action === 'override'
  const isSkip = row.action === 'skip'
  return (
    <tr
      data-testid={`wizard-row-${row.ftmo}`}
      data-action={row.action}
      className={`border-b ${isSkip ? 'opacity-50' : ''}`}
    >
      <td className="px-3 py-2 font-mono text-sm">{row.ftmo}</td>
      <td className="px-3 py-2 font-mono text-sm">
        {row.proposed_exness || <span className="text-gray-400">—</span>}
      </td>
      <td className="px-3 py-2">
        {isOverride ? (
          <select
            data-testid={`wizard-row-${row.ftmo}-override-select`}
            value={row.override_value}
            onChange={(e) => updateOverride(row.ftmo, e.target.value)}
            className="border rounded px-2 py-1 text-sm"
          >
            <option value="">— pick —</option>
            {filteredExness.map((s) => (
              <option key={s.name} value={s.name}>
                {s.name}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-gray-500 text-sm">{row.current_exness || '—'}</span>
        )}
      </td>
      {showAdvancedSpecs && (
        <td className="px-3 py-2 text-xs font-mono text-gray-600">
          cs={row.contract_size} d={row.digits} pip={row.pip_size}
        </td>
      )}
      <td className="px-3 py-2">
        <span
          data-testid={`wizard-row-${row.ftmo}-confidence`}
          className={`text-xs px-2 py-0.5 rounded ${CONFIDENCE_BADGE[row.confidence]}`}
        >
          {row.confidence}
        </span>
      </td>
      <td className="px-3 py-2">
        <select
          data-testid={`wizard-row-${row.ftmo}-action-select`}
          value={row.action}
          onChange={(e) =>
            updateAction(row.ftmo, e.target.value as 'accept' | 'override' | 'skip')
          }
          className="border rounded px-2 py-1 text-sm"
        >
          <option value="accept">Accept</option>
          <option value="override">Override</option>
          <option value="skip">Skip</option>
        </select>
      </td>
    </tr>
  )
}
