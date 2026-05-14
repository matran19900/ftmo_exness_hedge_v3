import { useAppStore } from '../../store'

export function BulkActions() {
  const rows = useAppStore((s) => s.wizard.rows)
  const bulkAcceptHighConfidence = useAppStore((s) => s.bulkAcceptHighConfidence)
  const bulkAcceptAllProposed = useAppStore((s) => s.bulkAcceptAllProposed)
  const bulkSkipUnmapped = useAppStore((s) => s.bulkSkipUnmapped)

  const highConfidenceCount = rows.filter(
    (r) => r.confidence === 'high' && r.action !== 'accept',
  ).length
  const allProposedCount = rows.filter(
    (r) => r.proposed_exness && r.action !== 'accept',
  ).length
  const unmappedCount = rows.filter(
    (r) => !r.proposed_exness && r.action !== 'skip',
  ).length

  return (
    <div className="flex gap-2 items-center px-4 py-2 border-b bg-gray-50">
      <button
        type="button"
        data-testid="bulk-accept-high"
        onClick={bulkAcceptHighConfidence}
        disabled={highConfidenceCount === 0}
        className="text-xs px-3 py-1 rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-40"
      >
        Accept All High Confidence ({highConfidenceCount})
      </button>
      <button
        type="button"
        data-testid="bulk-accept-proposed"
        onClick={bulkAcceptAllProposed}
        disabled={allProposedCount === 0}
        className="text-xs px-3 py-1 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40"
      >
        Accept All Proposed ({allProposedCount})
      </button>
      <button
        type="button"
        data-testid="bulk-skip-unmapped"
        onClick={bulkSkipUnmapped}
        disabled={unmappedCount === 0}
        className="text-xs px-3 py-1 rounded bg-gray-500 text-white hover:bg-gray-600 disabled:opacity-40"
      >
        Skip All Unmapped ({unmappedCount})
      </button>
    </div>
  )
}
