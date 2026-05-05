export function AccountStatusBar() {
  // TODO Phase 4: subscribe to WS heartbeat events, display real account status
  return (
    <div className="flex items-center gap-4 text-xs">
      <div className="flex items-center gap-1.5">
        <span className="w-2 h-2 rounded-full bg-gray-400" aria-hidden="true" />
        <span className="text-gray-600">FTMO: not configured</span>
      </div>
      <div className="flex items-center gap-1.5">
        <span className="w-2 h-2 rounded-full bg-gray-400" aria-hidden="true" />
        <span className="text-gray-600">Exness: not configured</span>
      </div>
    </div>
  )
}
