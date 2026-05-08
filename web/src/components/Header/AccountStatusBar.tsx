import { useAppStore } from '../../store'

const WS_DOT_COLOR = {
  connected: 'bg-green-500',
  connecting: 'bg-yellow-500',
  disconnected: 'bg-gray-400',
} as const

const WS_LABEL = {
  connected: 'WS: connected',
  connecting: 'WS: connecting...',
  disconnected: 'WS: disconnected',
} as const

export function AccountStatusBar() {
  const wsState = useAppStore((s) => s.wsState)

  return (
    <div className="flex items-center gap-4 text-xs">
      <div className="flex items-center gap-1.5">
        <span className={`w-2 h-2 rounded-full ${WS_DOT_COLOR[wsState]}`} aria-hidden="true" />
        <span className="text-gray-600">{WS_LABEL[wsState]}</span>
      </div>
      {/* FTMO + Exness account status indicators added in Phase 4. */}
    </div>
  )
}
