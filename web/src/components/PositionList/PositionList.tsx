import { useState } from 'react'
import { useAppStore } from '../../store'
import { HistoryTab } from './HistoryTab'
import { OpenTab } from './OpenTab'

type Tab = 'open' | 'history'

export function PositionList() {
  const [activeTab, setActiveTab] = useState<Tab>('open')
  const openCount = useAppStore((s) => s.positions.length)
  const historyCount = useAppStore((s) => s.history.length)
  const wsState = useAppStore((s) => s.wsState)

  const connected = wsState === 'connected'
  const indicatorColor = connected
    ? 'bg-green-500'
    : wsState === 'connecting'
      ? 'bg-amber-500'
      : 'bg-red-500'
  const indicatorLabel = connected
    ? 'Live'
    : wsState === 'connecting'
      ? 'Connecting…'
      : 'Reconnecting…'

  return (
    <div className="h-full bg-white border border-gray-200 rounded flex flex-col">
      <div className="flex items-center border-b border-gray-200 flex-shrink-0">
        <div className="flex flex-1">
          <button
            onClick={() => setActiveTab('open')}
            className={`px-6 py-2 text-sm font-medium transition-colors ${
              activeTab === 'open'
                ? 'text-blue-600 border-b-2 border-blue-600 -mb-px'
                : 'text-gray-500 hover:text-gray-700'
            }`}
            aria-selected={activeTab === 'open'}
            role="tab"
          >
            Open ({openCount})
          </button>
          <button
            onClick={() => setActiveTab('history')}
            className={`px-6 py-2 text-sm font-medium transition-colors ${
              activeTab === 'history'
                ? 'text-blue-600 border-b-2 border-blue-600 -mb-px'
                : 'text-gray-500 hover:text-gray-700'
            }`}
            aria-selected={activeTab === 'history'}
            role="tab"
          >
            History ({historyCount})
          </button>
        </div>
        <div
          className="flex items-center gap-2 px-4 text-xs text-gray-500"
          aria-live="polite"
          aria-label={`Live data: ${indicatorLabel}`}
        >
          <span className={`w-2 h-2 rounded-full ${indicatorColor}`} />
          <span>{indicatorLabel}</span>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto" role="tabpanel">
        {activeTab === 'open' ? <OpenTab /> : <HistoryTab />}
      </div>
    </div>
  )
}
