import { useEffect, useState } from 'react'
import { useAppStore } from '../store'
import { apiClient } from '../api/client'

export function MainPage() {
  const logout = useAppStore((s) => s.logout)
  const [symbolCount, setSymbolCount] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    apiClient
      .get<{ symbols: string[] }>('/symbols/')
      .then((res) => setSymbolCount(res.data.symbols.length))
      .catch((err) => {
        const msg = (err as Error)?.message ?? 'Failed to load symbols'
        setError(msg)
      })
  }, [])

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col">
      <header className="bg-white border-b border-gray-200 px-6 py-3 flex justify-between items-center">
        <h1 className="text-lg font-semibold text-gray-800">FTMO Hedge Tool v3</h1>
        <button
          onClick={logout}
          className="text-sm text-gray-600 hover:text-red-600 transition-colors"
        >
          Logout
        </button>
      </header>

      <main className="flex-1 flex items-center justify-center p-4">
        <div className="bg-white rounded-lg shadow-md p-8 max-w-lg w-full">
          <p className="text-sm text-gray-500 mb-4">Authenticated session ready</p>
          <div className="space-y-2 text-sm">
            <div>
              <span className="font-semibold">Symbols loaded:</span>{' '}
              <span className="text-gray-700">
                {error ? `✗ ${error}` : symbolCount === null ? 'loading...' : `✓ ${symbolCount}`}
              </span>
            </div>
          </div>
          <div className="mt-6 pt-4 border-t border-gray-200 text-xs text-gray-400">
            3-panel layout: step 1.7
          </div>
        </div>
      </main>
    </div>
  )
}
