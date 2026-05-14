import type { SpecDivergenceResponse } from '../../api/types/symbolMapping'

interface Props {
  divergences: SpecDivergenceResponse[]
}

export function SpecDivergenceBanner({ divergences }: Props) {
  if (divergences.length === 0) return null
  const blocking = divergences.filter((d) => d.severity === 'BLOCK')
  const warnings = divergences.filter((d) => d.severity === 'WARN')
  return (
    <div
      data-testid="spec-divergence-banner"
      className="m-4 p-3 border border-red-300 bg-red-50 rounded text-sm"
    >
      <div className="font-semibold text-red-900 mb-2">
        {blocking.length} blocking divergence{blocking.length === 1 ? '' : 's'}
        {warnings.length > 0
          ? `, ${warnings.length} warning${warnings.length === 1 ? '' : 's'}`
          : ''}
      </div>
      <ul className="space-y-1 font-mono text-xs text-red-800">
        {divergences.map((d, i) => (
          <li key={`${d.symbol}-${d.field}-${i}`}>
            <span className="font-semibold">[{d.severity}]</span> {d.symbol}.
            {d.field}: cached={String(d.cached_value)} raw={String(d.raw_value)}
            {d.delta_percent !== null
              ? ` (Δ${d.delta_percent.toFixed(1)}%)`
              : ''}
          </li>
        ))}
      </ul>
    </div>
  )
}
