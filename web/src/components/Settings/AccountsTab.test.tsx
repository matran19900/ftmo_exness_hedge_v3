/**
 * AccountsTab integration tests (Phase 4.A.6).
 *
 * Stubs `useMappingStatusSubscription` to a no-op so the test doesn't
 * try to send WebSocket messages — the relevant behaviours
 * (status-aware buttons, status dot colour) come from the
 * ``mappingStatusByAccount`` store mirror.
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAppStore } from '../../store'

vi.mock('../../hooks/useMappingStatusSubscription', () => ({
  useMappingStatusSubscription: () => undefined,
}))

const { AccountsTab } = await import('./AccountsTab')

const SEED_ACCOUNTS = [
  {
    broker: 'exness' as const,
    account_id: 'exness_001',
    name: 'My Exness',
    status: 'online' as const,
    enabled: true,
    balance_raw: '1000000',
    equity_raw: '1000000',
    margin_raw: '0',
    free_margin_raw: '1000000',
    currency: 'USD',
    money_digits: '2',
  },
]

describe('AccountsTab — Phase 4.A.6 status-aware buttons', () => {
  beforeEach(() => {
    useAppStore.setState({
      accountStatuses: SEED_ACCOUNTS,
      mappingStatusByAccount: {},
    })
  })

  afterEach(() => {
    useAppStore.setState({ accountStatuses: [], mappingStatusByAccount: {} })
  })

  it('renders Map Symbols button when status is pending_mapping (default)', () => {
    render(<AccountsTab />)
    expect(screen.getByTestId('map-symbols-exness_001')).toBeInTheDocument()
  })

  it('renders Edit Mapping + Re-sync buttons when status is active', () => {
    useAppStore.getState().setMappingStatusForAccount('exness_001', 'active')
    render(<AccountsTab />)
    expect(screen.getByTestId('edit-mapping-exness_001')).toBeInTheDocument()
    expect(screen.getByTestId('resync-exness_001')).toBeInTheDocument()
  })

  it('renders Resolve Mismatch when status is spec_mismatch', () => {
    useAppStore.getState().setMappingStatusForAccount('exness_001', 'spec_mismatch')
    render(<AccountsTab />)
    expect(screen.getByTestId('resolve-mismatch-exness_001')).toBeInTheDocument()
  })

  it('renders disabled Map Symbols when status is disconnected', () => {
    useAppStore.getState().setMappingStatusForAccount('exness_001', 'disconnected')
    render(<AccountsTab />)
    const btn = screen.getByText('Map Symbols')
    expect(btn).toBeDisabled()
  })

  it('mapping dot data-status attribute reflects current status', () => {
    useAppStore.getState().setMappingStatusForAccount('exness_001', 'active')
    render(<AccountsTab />)
    const dot = screen.getByTestId('mapping-dot-exness_001')
    expect(dot.dataset.status).toBe('active')
  })

  it('clicking Map Symbols opens wizard in create mode', async () => {
    render(<AccountsTab />)
    await userEvent.click(screen.getByTestId('map-symbols-exness_001'))
    expect(useAppStore.getState().wizard.open).toBe(true)
    expect(useAppStore.getState().wizard.mode).toBe('create')
    expect(useAppStore.getState().wizard.account_id).toBe('exness_001')
  })

  it('clicking Edit Mapping opens wizard in edit mode', async () => {
    useAppStore.getState().setMappingStatusForAccount('exness_001', 'active')
    render(<AccountsTab />)
    await userEvent.click(screen.getByTestId('edit-mapping-exness_001'))
    expect(useAppStore.getState().wizard.mode).toBe('edit')
  })
})
