import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { useAppStore } from '../../store'
import { SymbolMappingWizard } from './SymbolMappingWizard'

describe('SymbolMappingWizard overlay', () => {
  beforeEach(() => {
    useAppStore.getState().closeWizard()
  })

  afterEach(() => {
    useAppStore.getState().closeWizard()
  })

  it('renders nothing when wizard.open is false', () => {
    const { container } = render(<SymbolMappingWizard />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows loading indicator immediately after openWizard before REST resolves', async () => {
    const promise = useAppStore.getState().openWizard('exness_001', 'create')
    render(<SymbolMappingWizard />)
    expect(screen.getByText(/Loading…/)).toBeInTheDocument()
    await promise
  })

  it('renders rows table after REST resolves', async () => {
    await useAppStore.getState().openWizard('exness_001', 'create')
    render(<SymbolMappingWizard />)
    await waitFor(() => {
      expect(screen.getByTestId('wizard-row-EURUSD')).toBeInTheDocument()
    })
  })

  it('cancel button closes the wizard', async () => {
    await useAppStore.getState().openWizard('exness_001', 'create')
    render(<SymbolMappingWizard />)
    await userEvent.click(screen.getByTestId('wizard-cancel'))
    expect(useAppStore.getState().wizard.open).toBe(false)
  })

  it('save button triggers POST and closes overlay', async () => {
    await useAppStore.getState().openWizard('exness_001', 'create')
    render(<SymbolMappingWizard />)
    await userEvent.click(screen.getByTestId('wizard-save'))
    await waitFor(() => {
      expect(useAppStore.getState().wizard.open).toBe(false)
    })
  })

  it('save button is disabled when every row is action=skip', async () => {
    await useAppStore.getState().openWizard('exness_001', 'create')
    // Force every row to skip via store action.
    const rows = useAppStore.getState().wizard.rows
    for (const r of rows) useAppStore.getState().updateRowAction(r.ftmo, 'skip')
    render(<SymbolMappingWizard />)
    expect(screen.getByTestId('wizard-save')).toBeDisabled()
  })

  it('toggle "Show advanced specs" reveals the specs column', async () => {
    await useAppStore.getState().openWizard('exness_001', 'create')
    render(<SymbolMappingWizard />)
    await userEvent.click(screen.getByTestId('toggle-advanced-specs'))
    expect(useAppStore.getState().wizard.show_advanced_specs).toBe(true)
    // The cs= prefix appears once per row when advanced specs is on.
    expect(screen.getAllByText(/cs=/).length).toBeGreaterThan(0)
  })

  it('mode banner reflects the wizard mode', async () => {
    await useAppStore.getState().openWizard('exness_001', 'edit')
    render(<SymbolMappingWizard />)
    expect(screen.getByTestId('wizard-mode-banner').dataset.mode).toBe('edit')
  })
})
