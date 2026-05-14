import type { WizardMode } from '../../store'

interface Props {
  mode: WizardMode
  accountId: string | null
  fuzzyScore: number | null
  fuzzySource: string | null
  sharedAccountCount: number
}

const COLORS: Record<Exclude<WizardMode, null>, string> = {
  create: 'bg-blue-50 border-blue-300 text-blue-900',
  diff: 'bg-cyan-50 border-cyan-300 text-cyan-900',
  spec_mismatch: 'bg-red-50 border-red-300 text-red-900',
  edit: 'bg-gray-50 border-gray-300 text-gray-900',
}

export function ModeBanner({
  mode,
  accountId,
  fuzzyScore,
  fuzzySource,
  sharedAccountCount,
}: Props) {
  if (!mode) return null
  const color = COLORS[mode]
  return (
    <div
      data-testid="wizard-mode-banner"
      data-mode={mode}
      className={`border-b px-4 py-3 ${color}`}
    >
      {mode === 'create' && (
        <>
          <div className="font-semibold">Create Symbol Mapping</div>
          <div className="text-sm">
            Account: {accountId}. Review proposals and click Save.
          </div>
        </>
      )}
      {mode === 'diff' && (
        <>
          <div className="font-semibold">Diff-Aware Mode</div>
          <div className="text-sm">
            Found similar mapping (score:{' '}
            {fuzzyScore !== null ? `${(fuzzyScore * 100).toFixed(0)}%` : '—'}) in{' '}
            {fuzzySource ?? '—'}. Pre-filled from existing cache. Review highlighted
            changes.
          </div>
        </>
      )}
      {mode === 'spec_mismatch' && (
        <>
          <div className="font-semibold">⚠ Spec Mismatch Detected</div>
          <div className="text-sm">
            Account: {accountId}. Cached mapping has divergent contract specs. Cannot
            link to existing cache. Re-create mapping below.
          </div>
        </>
      )}
      {mode === 'edit' && (
        <>
          <div className="font-semibold">Edit Symbol Mapping</div>
          <div className="text-sm">
            Account: {accountId}.{' '}
            <strong>
              This mapping is shared with {sharedAccountCount} account
              {sharedAccountCount === 1 ? '' : 's'} — changes apply to all.
            </strong>
          </div>
        </>
      )}
    </div>
  )
}
