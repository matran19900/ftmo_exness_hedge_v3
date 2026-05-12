import { useState } from 'react'
import toast from 'react-hot-toast'
import { formatOrderError, listAccounts, updateAccount } from '../../api/client'
import { useAppStore } from '../../store'

// D-108: money fields are money_digits-scaled int strings; divide at
// the render boundary. Duplicates the helper in AccountStatusBar
// deliberately — the two components have different rendering needs
// (this one wants a "—" fallback rather than the dash-glyph used in
// the header).
function scaleMoney(rawStr: string, moneyDigitsStr: string): string {
  const raw = Number(rawStr)
  if (!Number.isFinite(raw)) return '—'
  const digits = parseInt(moneyDigitsStr, 10) || 2
  return (raw / Math.pow(10, digits)).toFixed(2)
}

const STATUS_DOT: Record<string, string> = {
  online: 'bg-green-500',
  offline: 'bg-red-500',
  disabled: 'bg-gray-400',
}

/**
 * Accounts management tab inside the Settings modal (step 3.13).
 *
 * Phase 3 supports only ``enabled`` toggle — FTMO account create
 * needs an OAuth flow (Phase 5) and Exness account CRUD waits for
 * the exness-client (Phase 4). The toggle PATCH'es
 * ``account_meta.enabled`` server-side; the WS account_status_loop
 * broadcast (every 5 s) will replace the store entirely with a fresh
 * snapshot, but we also refresh manually right after the PATCH to
 * give the operator instant feedback.
 */
export function AccountsTab() {
  const accountStatuses = useAppStore((s) => s.accountStatuses)
  const setAccountStatuses = useAppStore((s) => s.setAccountStatuses)
  const [toggling, setToggling] = useState<string | null>(null)

  async function refresh(): Promise<void> {
    try {
      const data = await listAccounts()
      setAccountStatuses(data.accounts)
    } catch {
      // Silent — the next WS broadcast will repopulate within ~5 s,
      // and a transient 5xx isn't worth a toast that competes with
      // the success toast on a successful toggle.
    }
  }

  async function handleToggle(
    broker: 'ftmo' | 'exness',
    accountId: string,
    currentEnabled: boolean
  ): Promise<void> {
    const key = `${broker}:${accountId}`
    setToggling(key)
    try {
      await updateAccount(broker, accountId, !currentEnabled)
      toast.success(`Account ${accountId} ${!currentEnabled ? 'enabled' : 'disabled'}`)
      await refresh()
    } catch (err) {
      toast.error(formatOrderError(err))
    } finally {
      setToggling(null)
    }
  }

  return (
    <div>
      <div className="text-sm text-gray-600 mb-3">
        {accountStatuses.length} account(s). FTMO account creation requires OAuth flow (see docs).
      </div>

      {accountStatuses.length === 0 ? (
        <div className="text-sm text-gray-400 text-center py-8">No accounts configured</div>
      ) : (
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b text-xs uppercase tracking-wide text-gray-600">
            <tr>
              <th className="px-3 py-2 text-left">Broker</th>
              <th className="px-3 py-2 text-left">Name</th>
              <th className="px-3 py-2 text-left">Account ID</th>
              <th className="px-3 py-2 text-center">Status</th>
              <th className="px-3 py-2 text-right">Balance</th>
              <th className="px-3 py-2 text-center">Enabled</th>
            </tr>
          </thead>
          <tbody>
            {accountStatuses.map((acc) => {
              const key = `${acc.broker}:${acc.account_id}`
              const isToggling = toggling === key
              return (
                <tr key={key} className="border-b hover:bg-gray-50">
                  <td className="px-3 py-2 uppercase text-xs text-gray-600">{acc.broker}</td>
                  <td className="px-3 py-2 font-medium text-gray-700">{acc.name || '—'}</td>
                  <td className="px-3 py-2 font-mono text-xs text-gray-600">{acc.account_id}</td>
                  <td className="px-3 py-2 text-center">
                    <span
                      className={`inline-block w-2 h-2 rounded-full ${STATUS_DOT[acc.status] ?? 'bg-gray-400'}`}
                      aria-hidden="true"
                    />
                    <span className="ml-1 text-xs text-gray-600">{acc.status}</span>
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-gray-700">
                    {acc.currency} {scaleMoney(acc.balance_raw, acc.money_digits)}
                  </td>
                  <td className="px-3 py-2 text-center">
                    <button
                      type="button"
                      onClick={() => handleToggle(acc.broker, acc.account_id, acc.enabled)}
                      disabled={isToggling}
                      className={`px-3 py-1 text-xs rounded font-semibold ${
                        acc.enabled
                          ? 'bg-green-100 text-green-800 hover:bg-green-200'
                          : 'bg-gray-200 text-gray-600 hover:bg-gray-300'
                      } disabled:opacity-50`}
                    >
                      {isToggling ? '...' : acc.enabled ? 'ON' : 'OFF'}
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}
