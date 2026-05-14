import { useMemo, useState } from 'react'
import toast from 'react-hot-toast'
import { formatOrderError, listAccounts, updateAccount } from '../../api/client'
import { useMappingStatusSubscription } from '../../hooks/useMappingStatusSubscription'
import { useAppStore } from '../../store'
import type { MappingStatus } from '../../api/types/symbolMapping'

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

// Phase 4.A.6: per-account mapping_status colour-coding.
const MAPPING_DOT: Record<MappingStatus, string> = {
  active: 'bg-green-500',
  pending_mapping: 'bg-yellow-500',
  spec_mismatch: 'bg-red-500 animate-pulse',
  disconnected: 'bg-gray-400',
}

/**
 * Accounts management tab inside the Settings modal (step 3.13 + Phase 4.A.6).
 *
 * Phase 3 supports only ``enabled`` toggle. Phase 4.A.6 adds the
 * per-Exness-account symbol-mapping wizard buttons (Map / Edit / Re-sync /
 * Resolve), each gated by the live ``mapping_status`` of that account.
 */
export function AccountsTab() {
  const accountStatuses = useAppStore((s) => s.accountStatuses)
  const setAccountStatuses = useAppStore((s) => s.setAccountStatuses)
  const mappingStatusByAccount = useAppStore((s) => s.mappingStatusByAccount)
  const openWizard = useAppStore((s) => s.openWizard)
  const triggerResync = useAppStore((s) => s.triggerResync)
  const [toggling, setToggling] = useState<string | null>(null)

  // Subscribe to mapping_status:{id} channels for every Exness account
  // currently in the table. ``join`` keeps the dependency stable across
  // identical-arrays renders.
  const exnessIds = useMemo(
    () =>
      accountStatuses
        .filter((a) => a.broker === 'exness')
        .map((a) => a.account_id),
    [accountStatuses],
  )
  useMappingStatusSubscription(exnessIds)

  async function refresh(): Promise<void> {
    try {
      const data = await listAccounts()
      setAccountStatuses(data.accounts)
    } catch {
      // Silent — the next WS broadcast will repopulate within ~5 s.
    }
  }

  async function handleToggle(
    broker: 'ftmo' | 'exness',
    accountId: string,
    currentEnabled: boolean,
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

  async function handleResync(accountId: string): Promise<void> {
    try {
      await triggerResync(accountId)
      toast.success(`Re-sync requested for ${accountId}`)
    } catch (err) {
      toast.error(formatOrderError(err))
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
              <th className="px-3 py-2 text-center">Mapping</th>
              <th className="px-3 py-2 text-right">Balance</th>
              <th className="px-3 py-2 text-center">Enabled</th>
              <th className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {accountStatuses.map((acc) => {
              const key = `${acc.broker}:${acc.account_id}`
              const isToggling = toggling === key
              const mappingStatus: MappingStatus | undefined =
                acc.broker === 'exness'
                  ? (mappingStatusByAccount[acc.account_id] ?? 'pending_mapping')
                  : undefined
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
                  <td className="px-3 py-2 text-center">
                    {mappingStatus ? (
                      <>
                        <span
                          data-testid={`mapping-dot-${acc.account_id}`}
                          data-status={mappingStatus}
                          className={`inline-block w-2 h-2 rounded-full ${MAPPING_DOT[mappingStatus]}`}
                          aria-hidden="true"
                        />
                        <span className="ml-1 text-xs text-gray-600">{mappingStatus}</span>
                      </>
                    ) : (
                      <span className="text-xs text-gray-400">—</span>
                    )}
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
                  <td className="px-3 py-2 text-right">
                    {mappingStatus === 'pending_mapping' && (
                      <button
                        type="button"
                        data-testid={`map-symbols-${acc.account_id}`}
                        onClick={() => openWizard(acc.account_id, 'create')}
                        className="text-xs px-3 py-1 rounded bg-blue-600 text-white hover:bg-blue-700"
                      >
                        Map Symbols
                      </button>
                    )}
                    {mappingStatus === 'active' && (
                      <div className="flex gap-1 justify-end">
                        <button
                          type="button"
                          data-testid={`edit-mapping-${acc.account_id}`}
                          onClick={() => openWizard(acc.account_id, 'edit')}
                          className="text-xs px-3 py-1 rounded bg-gray-600 text-white hover:bg-gray-700"
                        >
                          Edit Mapping
                        </button>
                        <button
                          type="button"
                          data-testid={`resync-${acc.account_id}`}
                          onClick={() => handleResync(acc.account_id)}
                          className="text-xs px-3 py-1 rounded bg-blue-100 text-blue-700 hover:bg-blue-200"
                        >
                          Re-sync
                        </button>
                      </div>
                    )}
                    {mappingStatus === 'spec_mismatch' && (
                      <button
                        type="button"
                        data-testid={`resolve-mismatch-${acc.account_id}`}
                        onClick={() => openWizard(acc.account_id, 'spec_mismatch')}
                        className="text-xs px-3 py-1 rounded bg-red-600 text-white hover:bg-red-700"
                      >
                        Resolve Mismatch
                      </button>
                    )}
                    {mappingStatus === 'disconnected' && (
                      <button
                        type="button"
                        disabled
                        title="Exness client offline"
                        className="text-xs px-3 py-1 rounded bg-gray-200 text-gray-400 cursor-not-allowed"
                      >
                        Map Symbols
                      </button>
                    )}
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
