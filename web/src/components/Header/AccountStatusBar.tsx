import { useEffect } from 'react'
import { listAccounts, type AccountStatusEntry } from '../../api/client'
import { useAppStore } from '../../store'

const WS_DOT_COLOR = {
  connected: 'bg-green-500',
  connecting: 'bg-yellow-500',
  disconnected: 'bg-gray-400',
} as const

const WS_LABEL = {
  connected: 'WS: connected',
  connecting: 'WS: connecting...',
  disconnected: 'WS: disconnected',
} as const

const ACCOUNT_DOT_COLOR: Record<AccountStatusEntry['status'], string> = {
  online: 'bg-green-500',
  offline: 'bg-red-500',
  disabled: 'bg-gray-400',
}

// D-108: server ships money fields as ``money_digits``-scaled int strings;
// the UI divides at the render boundary so REST + WS payloads share an
// identical shape and the wire stays integer-precise.
function scaleMoney(rawStr: string, moneyDigitsStr: string): string {
  const raw = Number(rawStr)
  if (!Number.isFinite(raw)) return '—'
  const digits = parseInt(moneyDigitsStr, 10) || 2
  return (raw / Math.pow(10, digits)).toFixed(2)
}

function AccountCard({ acc }: { acc: AccountStatusEntry }) {
  const balance = scaleMoney(acc.balance_raw, acc.money_digits)
  const equity = scaleMoney(acc.equity_raw, acc.money_digits)
  return (
    <div
      className="flex items-center gap-2 px-2 py-1 bg-white border border-gray-200 rounded text-xs"
      title={`${acc.broker.toUpperCase()} ${acc.account_id} — ${acc.status}`}
      data-testid={`account-card-${acc.broker}-${acc.account_id}`}
    >
      <span
        className={`inline-block w-2 h-2 rounded-full ${ACCOUNT_DOT_COLOR[acc.status]}`}
        aria-hidden="true"
      />
      <span className="font-semibold text-gray-700">{acc.name || acc.account_id}</span>
      <span className="text-gray-300">|</span>
      <span className="font-mono text-gray-600">
        {acc.currency} {balance}
      </span>
      <span className="text-gray-300">/</span>
      <span className="font-mono text-gray-800">{equity}</span>
    </div>
  )
}

export function AccountStatusBar() {
  const wsState = useAppStore((s) => s.wsState)
  const accountStatuses = useAppStore((s) => s.accountStatuses)
  const setAccountStatuses = useAppStore((s) => s.setAccountStatuses)

  // Step 3.12: initial REST load so the bar populates before the first
  // WS broadcast (which fires every 5 s). After that the
  // ``account_status_loop`` snapshot replaces this state on each tick.
  // Errors are swallowed silently — a 401 will already have triggered
  // the global logout flow in ``apiClient``, and a transient 5xx just
  // means the bar shows the WS broadcast once it arrives.
  useEffect(() => {
    let cancelled = false
    listAccounts()
      .then((res) => {
        if (!cancelled) setAccountStatuses(res.accounts)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [setAccountStatuses])

  return (
    <div className="flex items-center gap-3 text-xs">
      <div className="flex items-center gap-1.5">
        <span className={`w-2 h-2 rounded-full ${WS_DOT_COLOR[wsState]}`} aria-hidden="true" />
        <span className="text-gray-600">{WS_LABEL[wsState]}</span>
      </div>
      {accountStatuses.map((acc) => (
        <AccountCard key={`${acc.broker}:${acc.account_id}`} acc={acc} />
      ))}
    </div>
  )
}
