import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, beforeEach } from 'vitest'
import { useAppStore } from '../../store'
import type { WizardRowState } from '../../store'
import { BulkActions } from './BulkActions'

function row(overrides: Partial<WizardRowState>): WizardRowState {
  return {
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
    ...overrides,
  }
}

describe('BulkActions', () => {
  beforeEach(() => {
    useAppStore.setState({
      wizard: {
        ...useAppStore.getState().wizard,
        rows: [
          row({ ftmo: 'EURUSD', confidence: 'high', action: 'override' }),
          row({ ftmo: 'GBPUSD', confidence: 'high', action: 'accept' }),
          row({ ftmo: 'XAUUSD', confidence: 'medium', action: 'override' }),
          row({ ftmo: 'BTCUSD', proposed_exness: '', confidence: 'low', action: 'accept' }),
        ],
      },
    })
  })

  it('counts non-accept high-confidence rows', () => {
    render(<BulkActions />)
    expect(screen.getByTestId('bulk-accept-high')).toHaveTextContent(
      /Accept All High Confidence \(1\)/,
    )
  })

  it('clicking high-confidence button sets action=accept on those rows only', async () => {
    render(<BulkActions />)
    await userEvent.click(screen.getByTestId('bulk-accept-high'))
    const rows = useAppStore.getState().wizard.rows
    const eurusd = rows.find((r) => r.ftmo === 'EURUSD')!
    const xauusd = rows.find((r) => r.ftmo === 'XAUUSD')!
    expect(eurusd.action).toBe('accept')
    expect(xauusd.action).toBe('override') // unchanged (medium confidence)
  })

  it('skip-unmapped only flips rows whose proposed_exness is empty', async () => {
    render(<BulkActions />)
    await userEvent.click(screen.getByTestId('bulk-skip-unmapped'))
    const rows = useAppStore.getState().wizard.rows
    const btcusd = rows.find((r) => r.ftmo === 'BTCUSD')!
    const eurusd = rows.find((r) => r.ftmo === 'EURUSD')!
    expect(btcusd.action).toBe('skip')
    expect(eurusd.action).toBe('override') // unchanged
  })

  it('accept-all-proposed flips every row with a proposed_exness to accept', async () => {
    render(<BulkActions />)
    await userEvent.click(screen.getByTestId('bulk-accept-proposed'))
    const rows = useAppStore.getState().wizard.rows
    expect(rows.find((r) => r.ftmo === 'EURUSD')!.action).toBe('accept')
    expect(rows.find((r) => r.ftmo === 'XAUUSD')!.action).toBe('accept')
    // BTCUSD has no proposed_exness so its action stays 'accept' (already)
    expect(rows.find((r) => r.ftmo === 'BTCUSD')!.action).toBe('accept')
  })

  it('disables buttons when their counts are zero', () => {
    useAppStore.setState({
      wizard: {
        ...useAppStore.getState().wizard,
        rows: [row({ confidence: 'low', action: 'accept' })],
      },
    })
    render(<BulkActions />)
    expect(screen.getByTestId('bulk-accept-high')).toBeDisabled()
  })
})
