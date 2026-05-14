import { useState } from 'react'
import { useAppStore } from '../../store'
import { SettingsModal } from '../Settings/SettingsModal'
import { SymbolMappingWizard } from '../SymbolMappingWizard/SymbolMappingWizard'
import { AccountStatusBar } from './AccountStatusBar'

export function Header() {
  const logout = useAppStore((s) => s.logout)
  const [settingsOpen, setSettingsOpen] = useState(false)

  return (
    <>
      <header className="bg-white border-b border-gray-200 px-6 py-3 flex justify-between items-center flex-shrink-0">
        <div className="flex items-center gap-6">
          <h1 className="text-lg font-semibold text-gray-800">FTMO Hedge Tool v3</h1>
          <AccountStatusBar />
        </div>
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={() => setSettingsOpen(true)}
            className="text-sm text-gray-600 hover:text-blue-600 transition-colors"
            aria-label="Settings"
          >
            ⚙ Settings
          </button>
          <button
            onClick={logout}
            className="text-sm text-gray-600 hover:text-red-600 transition-colors"
          >
            Logout
          </button>
        </div>
      </header>
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
      <SymbolMappingWizard />
    </>
  )
}
