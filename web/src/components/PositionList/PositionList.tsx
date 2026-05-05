import { useState } from 'react'
import { OpenTab } from './OpenTab'
import { HistoryTab } from './HistoryTab'

type Tab = 'open' | 'history'

export function PositionList() {
  const [activeTab, setActiveTab] = useState<Tab>('open')

  return (
    <div className="h-full bg-white border border-gray-200 rounded flex flex-col">
      <div className="flex border-b border-gray-200 flex-shrink-0">
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
          Open
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
          History
        </button>
      </div>

      <div className="flex-1 overflow-y-auto" role="tabpanel">
        {activeTab === 'open' ? <OpenTab /> : <HistoryTab />}
      </div>
    </div>
  )
}
