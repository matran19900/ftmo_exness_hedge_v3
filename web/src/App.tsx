import { useEffect, useState } from 'react'
import { useAppStore } from './store'
import { apiClient } from './api/client'

function App() {
  const token = useAppStore((s) => s.token)
  const [healthStatus, setHealthStatus] = useState<string>('checking...')

  useEffect(() => {
    apiClient
      .get('/health')
      .then((res) => setHealthStatus(`✓ ${res.data.service} v${res.data.version}`))
      .catch((err) => setHealthStatus(`✗ error: ${err.message}`))
  }, [])

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-lg shadow-md p-8 max-w-lg w-full">
        <h1 className="text-2xl font-bold text-gray-800 mb-2">FTMO Hedge Tool v3</h1>
        <p className="text-sm text-gray-500 mb-4">Frontend scaffold ready</p>

        <div className="space-y-2 text-sm">
          <div>
            <span className="font-semibold">Backend health:</span>{' '}
            <span className="text-gray-700">{healthStatus}</span>
          </div>
          <div>
            <span className="font-semibold">Auth state:</span>{' '}
            <span className="text-gray-700">
              {token ? 'logged in (token in localStorage)' : 'not logged in'}
            </span>
          </div>
        </div>

        <div className="mt-6 pt-4 border-t border-gray-200 text-xs text-gray-400">
          Login page: step 1.6 · Layout: step 1.7
        </div>
      </div>
    </div>
  )
}

export default App
