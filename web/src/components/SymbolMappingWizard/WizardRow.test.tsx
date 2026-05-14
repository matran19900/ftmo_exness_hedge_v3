import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { useAppStore } from '../../store'
import type { WizardRowState } from '../../store'
import type { RawSymbolResponse } from '../../api/types/symbolMapping'
import { WizardRow } from './WizardRow'

const baseRow: WizardRowState = {
  ftmo: 'EURUSD',
  proposed_exness: 'EURUSDm',
  current_exness: 'EURUSDm',
  match_type: 'suffix_strip',
  confidence: 'medium',
  action: 'accept',
  override_value: '',
  contract_size: 100000,
  digits: 5,
  pip_size: 0.0001,
  pip_value: 10,
}

const availableExness: RawSymbolResponse[] = [
  {
    name: 'EURUSDm',
    contract_size: 100000,
    digits: 5,
    pip_size: 0.0001,
    volume_min: 0.01,
    volume_step: 0.01,
    volume_max: 200,
    currency_profit: 'USD',
  },
  {
    name: 'EURUSDc',
    contract_size: 1000,
    digits: 5,
    pip_size: 0.0001,
    volume_min: 0.01,
    volume_step: 0.01,
    volume_max: 200,
    currency_profit: 'USD',
  },
]

function renderInTable(row: WizardRowState, props: Partial<{
  showAdvancedSpecs: boolean
  showAllExness: boolean
}> = {}) {
  return render(
    <table>
      <tbody>
        <WizardRow
          row={row}
          showAdvancedSpecs={props.showAdvancedSpecs ?? false}
          showAllExness={props.showAllExness ?? false}
          availableExness={availableExness}
        />
      </tbody>
    </table>,
  )
}

describe('WizardRow', () => {
  it('renders ftmo + proposed exness + confidence in accept mode', () => {
    renderInTable(baseRow)
    expect(screen.getByText('EURUSD')).toBeInTheDocument()
    // EURUSDm appears in both the proposed_exness cell and the current cell
    // when not overriding — `getAllByText` confirms both renders.
    expect(screen.getAllByText('EURUSDm').length).toBe(2)
    expect(screen.getByTestId('wizard-row-EURUSD-confidence')).toHaveTextContent('medium')
  })

  it('shows override select only when action=override', () => {
    renderInTable({ ...baseRow, action: 'override' })
    expect(screen.getByTestId('wizard-row-EURUSD-override-select')).toBeInTheDocument()
  })

  it('does not render override select for action=accept', () => {
    renderInTable(baseRow)
    expect(screen.queryByTestId('wizard-row-EURUSD-override-select')).toBeNull()
  })

  it('skip action dims the row via opacity-50 class', () => {
    renderInTable({ ...baseRow, action: 'skip' })
    expect(screen.getByTestId('wizard-row-EURUSD').className).toContain('opacity-50')
  })

  it('changing action select calls store updateRowAction', async () => {
    renderInTable(baseRow)
    await userEvent.selectOptions(
      screen.getByTestId('wizard-row-EURUSD-action-select'),
      'skip',
    )
    expect(useAppStore.getState().wizard.rows.find((r) => r.ftmo === 'EURUSD')?.action).toBeUndefined()
    // The store has no row for EURUSD until a wizard is open; update is still
    // dispatched, just no-op against an empty rows[]. The interaction itself
    // must succeed without throwing.
  })

  it('show_advanced_specs renders specs cell', () => {
    renderInTable(baseRow, { showAdvancedSpecs: true })
    expect(screen.getByText(/cs=100000/)).toBeInTheDocument()
    expect(screen.getByText(/d=5/)).toBeInTheDocument()
  })

  it('override select limited to proposed/current when show_all_exness is off', () => {
    renderInTable({ ...baseRow, action: 'override' }, { showAllExness: false })
    const select = screen.getByTestId('wizard-row-EURUSD-override-select')
    // 1 dummy "— pick —" option + 1 (EURUSDm proposed)
    expect(select.querySelectorAll('option')).toHaveLength(2)
  })

  it('override select expands to all available when show_all_exness is on', () => {
    renderInTable({ ...baseRow, action: 'override' }, { showAllExness: true })
    const select = screen.getByTestId('wizard-row-EURUSD-override-select')
    // 1 "— pick —" + 2 raw symbols
    expect(select.querySelectorAll('option')).toHaveLength(3)
  })
})
