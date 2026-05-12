import { useState } from 'react'
import { AccountsTab } from './AccountsTab'
import { PairsTab } from './PairsTab'

type Tab = 'pairs' | 'accounts'

interface Props {
  onClose: () => void
}

/**
 * Settings modal (step 3.13).
 *
 * Two tabs: Pairs (CRUD via /api/pairs) + Accounts (toggle enabled
 * via /api/accounts/{broker}/{account_id}).
 *
 * Overlay closes on click-outside; inner content stops propagation.
 * Escape-to-close is intentionally deferred — operators report
 * accidental Esc-presses when typing into form fields and a Phase 5
 * confirmation hook is the right place to add it.
 */
export function SettingsModal({ onClose }: Props) {
  const [tab, setTab] = useState<Tab>('pairs')

  return (
    <div
      className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Settings"
    >
      <div
        className="bg-white rounded-md w-full max-w-3xl max-h-[90vh] flex flex-col shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
          <h2 className="text-lg font-semibold text-gray-800">Settings</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-2xl leading-none px-1"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <div className="flex gap-1 px-4 pt-3 border-b border-gray-200">
          <button
            type="button"
            onClick={() => setTab('pairs')}
            className={`px-3 py-1.5 text-sm rounded-t ${
              tab === 'pairs' ? 'bg-blue-500 text-white' : 'bg-gray-100 text-gray-700'
            }`}
          >
            Pairs
          </button>
          <button
            type="button"
            onClick={() => setTab('accounts')}
            className={`px-3 py-1.5 text-sm rounded-t ${
              tab === 'accounts' ? 'bg-blue-500 text-white' : 'bg-gray-100 text-gray-700'
            }`}
          >
            Accounts
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {tab === 'pairs' && <PairsTab />}
          {tab === 'accounts' && <AccountsTab />}
        </div>
      </div>
    </div>
  )
}
