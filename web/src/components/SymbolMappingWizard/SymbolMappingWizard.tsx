import toast from 'react-hot-toast'
import { useAppStore } from '../../store'
import { BulkActions } from './BulkActions'
import { ModeBanner } from './ModeBanner'
import { SpecDivergenceBanner } from './SpecDivergenceBanner'
import { UnmappedExnessSection } from './UnmappedExnessSection'
import { UnmappedFtmoSection } from './UnmappedFtmoSection'
import { WizardRow } from './WizardRow'

/**
 * Full-screen overlay rendering the symbol-mapping wizard.
 *
 * Lifecycle is store-driven: ``store.openWizard(accountId, mode)`` triggers
 * the REST sequence (raw-symbols + auto-match), which populates ``rows``.
 * The user mutates rows locally; ``store.saveMapping()`` POSTs / PATCHes
 * and closes the overlay on success.
 */
export function SymbolMappingWizard() {
  const wizard = useAppStore((s) => s.wizard)
  const closeWizard = useAppStore((s) => s.closeWizard)
  const saveMapping = useAppStore((s) => s.saveMapping)
  const toggleAdvancedSpecs = useAppStore((s) => s.toggleAdvancedSpecs)
  const toggleShowAllExness = useAppStore((s) => s.toggleShowAllExness)

  if (!wizard.open) return null

  const rows = wizard.rows
  const allSkipped = rows.length > 0 && rows.every((r) => r.action === 'skip')

  async function onSave() {
    const result = await saveMapping()
    if (result.success) {
      toast.success('Mapping saved')
    } else {
      toast.error(`Save failed: ${result.error ?? 'unknown error'}`)
    }
  }

  return (
    <div
      data-testid="wizard-overlay"
      className="fixed inset-0 bg-black/50 z-50 flex items-stretch justify-center p-4"
    >
      <div className="bg-white rounded shadow-xl w-full max-w-6xl flex flex-col">
        <div className="flex items-start justify-between border-b">
          <ModeBanner
            mode={wizard.mode}
            accountId={wizard.account_id}
            fuzzyScore={wizard.fuzzy_score}
            fuzzySource={wizard.fuzzy_source}
            sharedAccountCount={1}
          />
          <button
            type="button"
            data-testid="wizard-close"
            onClick={closeWizard}
            className="px-3 py-1 text-gray-500 hover:text-gray-800"
          >
            ✕
          </button>
        </div>

        {wizard.mode === 'spec_mismatch' && (
          <SpecDivergenceBanner divergences={wizard.divergences} />
        )}

        <BulkActions />

        <div className="flex gap-3 px-4 py-2 text-xs border-b">
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              data-testid="toggle-advanced-specs"
              checked={wizard.show_advanced_specs}
              onChange={toggleAdvancedSpecs}
            />
            Show advanced specs
          </label>
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              data-testid="toggle-show-all-exness"
              checked={wizard.show_all_exness}
              onChange={toggleShowAllExness}
            />
            Show all Exness symbols
          </label>
        </div>

        <div className="flex-1 overflow-auto">
          {wizard.loading ? (
            <div className="p-8 text-center text-gray-500">Loading…</div>
          ) : wizard.load_error ? (
            <div className="p-8 text-center text-red-600">
              Failed to load: {wizard.load_error}
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b text-xs uppercase tracking-wide text-gray-600">
                <tr>
                  <th className="px-3 py-2 text-left">FTMO</th>
                  <th className="px-3 py-2 text-left">Auto-match</th>
                  <th className="px-3 py-2 text-left">Exness Override</th>
                  {wizard.show_advanced_specs && (
                    <th className="px-3 py-2 text-left">Specs</th>
                  )}
                  <th className="px-3 py-2 text-left">Confidence</th>
                  <th className="px-3 py-2 text-left">Action</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <WizardRow
                    key={row.ftmo}
                    row={row}
                    showAdvancedSpecs={wizard.show_advanced_specs}
                    showAllExness={wizard.show_all_exness}
                    availableExness={wizard.available_exness}
                  />
                ))}
              </tbody>
            </table>
          )}

          <UnmappedFtmoSection symbols={wizard.unmapped_ftmo} />
          <UnmappedExnessSection symbols={wizard.unmapped_exness} />
        </div>

        <div className="flex justify-end gap-2 px-4 py-3 border-t bg-gray-50">
          {wizard.save_error && (
            <div className="text-sm text-red-600 mr-auto">{wizard.save_error}</div>
          )}
          <button
            type="button"
            data-testid="wizard-cancel"
            onClick={closeWizard}
            className="px-4 py-1 rounded border bg-white text-sm"
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid="wizard-save"
            onClick={onSave}
            disabled={wizard.saving || allSkipped || rows.length === 0}
            className="px-4 py-1 rounded bg-blue-600 text-white text-sm disabled:opacity-40"
          >
            {wizard.saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}
