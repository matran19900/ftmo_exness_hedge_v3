import { useAppStore } from '../../store'
import { AccountStatusBar } from './AccountStatusBar'

export function Header() {
  const logout = useAppStore((s) => s.logout)
  return (
    <header className="bg-white border-b border-gray-200 px-6 py-3 flex justify-between items-center flex-shrink-0">
      <div className="flex items-center gap-6">
        <h1 className="text-lg font-semibold text-gray-800">FTMO Hedge Tool v3</h1>
        <AccountStatusBar />
      </div>
      <button
        onClick={logout}
        className="text-sm text-gray-600 hover:text-red-600 transition-colors"
      >
        Logout
      </button>
    </header>
  )
}
