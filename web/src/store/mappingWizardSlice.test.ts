import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { useAppStore } from '.'

describe('mappingWizardSlice', () => {
  beforeEach(() => {
    useAppStore.getState().closeWizard()
    useAppStore.getState().resetMappingStatuses()
  })

  afterEach(() => {
    useAppStore.getState().closeWizard()
  })

  describe('openWizard + REST sequence', () => {
    it('hits raw-symbols + auto-match and merges into rows', async () => {
      await useAppStore.getState().openWizard('exness_001', 'create')
      const w = useAppStore.getState().wizard
      expect(w.open).toBe(true)
      expect(w.account_id).toBe('exness_001')
      expect(w.mode).toBe('create')
      expect(w.rows.length).toBeGreaterThan(0)
      // Mock fixture has EURUSD → EURUSDm with medium confidence.
      const eurusd = w.rows.find((r) => r.ftmo === 'EURUSD')!
      expect(eurusd.proposed_exness).toBe('EURUSDm')
      expect(eurusd.confidence).toBe('medium')
    })

    it('populates unmapped lists from auto-match response', async () => {
      await useAppStore.getState().openWizard('exness_001', 'create')
      const w = useAppStore.getState().wizard
      expect(w.unmapped_ftmo).toContain('SPX500')
      expect(w.unmapped_exness).toContain('BTCUSDm')
    })

    it('records signature from auto-match response', async () => {
      await useAppStore.getState().openWizard('exness_001', 'create')
      expect(useAppStore.getState().wizard.signature).toBe('abc123def456')
    })
  })

  describe('updateRowAction', () => {
    beforeEach(async () => {
      await useAppStore.getState().openWizard('exness_001', 'create')
    })

    it('flips one row only', () => {
      useAppStore.getState().updateRowAction('EURUSD', 'skip')
      const eurusd = useAppStore.getState().wizard.rows.find((r) => r.ftmo === 'EURUSD')!
      expect(eurusd.action).toBe('skip')
      const gbpusd = useAppStore.getState().wizard.rows.find((r) => r.ftmo === 'GBPUSD')!
      expect(gbpusd.action).toBe('accept')
    })

    it('updateRowOverride sets current_exness when value provided', () => {
      useAppStore.getState().updateRowOverride('EURUSD', 'EURUSDc')
      const eurusd = useAppStore.getState().wizard.rows.find((r) => r.ftmo === 'EURUSD')!
      expect(eurusd.override_value).toBe('EURUSDc')
      expect(eurusd.current_exness).toBe('EURUSDc')
    })
  })

  describe('toggles', () => {
    it('toggleAdvancedSpecs flips the boolean', async () => {
      await useAppStore.getState().openWizard('exness_001', 'create')
      const before = useAppStore.getState().wizard.show_advanced_specs
      useAppStore.getState().toggleAdvancedSpecs()
      expect(useAppStore.getState().wizard.show_advanced_specs).toBe(!before)
    })

    it('toggleShowAllExness flips the boolean', async () => {
      await useAppStore.getState().openWizard('exness_001', 'create')
      const before = useAppStore.getState().wizard.show_all_exness
      useAppStore.getState().toggleShowAllExness()
      expect(useAppStore.getState().wizard.show_all_exness).toBe(!before)
    })
  })

  describe('saveMapping', () => {
    it('POSTs save in non-edit mode and closes the overlay on 201', async () => {
      await useAppStore.getState().openWizard('exness_001', 'create')
      const result = await useAppStore.getState().saveMapping()
      expect(result.success).toBe(true)
      expect(useAppStore.getState().wizard.open).toBe(false)
    })

    it('PATCHes edit endpoint when mode=edit', async () => {
      await useAppStore.getState().openWizard('exness_001', 'edit')
      const result = await useAppStore.getState().saveMapping()
      expect(result.success).toBe(true)
    })
  })

  describe('mapping status mirror', () => {
    it('setMappingStatusForAccount updates the per-account record', () => {
      useAppStore.getState().setMappingStatusForAccount('exness_001', 'active')
      expect(useAppStore.getState().mappingStatusByAccount['exness_001']).toBe('active')
    })

    it('resetMappingStatuses clears all entries', () => {
      useAppStore.getState().setMappingStatusForAccount('exness_001', 'active')
      useAppStore.getState().resetMappingStatuses()
      expect(Object.keys(useAppStore.getState().mappingStatusByAccount)).toHaveLength(0)
    })
  })

  describe('triggerResync', () => {
    it('does not throw when MSW returns 202', async () => {
      await expect(
        useAppStore.getState().triggerResync('exness_001'),
      ).resolves.toBeUndefined()
    })
  })

  describe('closeWizard', () => {
    it('resets the wizard state to closed/empty', async () => {
      await useAppStore.getState().openWizard('exness_001', 'create')
      useAppStore.getState().closeWizard()
      const w = useAppStore.getState().wizard
      expect(w.open).toBe(false)
      expect(w.rows).toHaveLength(0)
      expect(w.account_id).toBeNull()
    })
  })
})
