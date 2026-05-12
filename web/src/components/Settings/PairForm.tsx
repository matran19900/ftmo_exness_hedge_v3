import { useState } from 'react'
import toast from 'react-hot-toast'
import {
  createPair,
  formatOrderError,
  updatePair,
  type PairResponse,
} from '../../api/client'
import { useAppStore } from '../../store'

interface Props {
  pair: PairResponse | null // null = create, else edit
  onCancel: () => void
  onSuccess: () => Promise<void>
}

/**
 * Create / edit pair form inside the Settings modal (step 3.13).
 *
 * FTMO account is a dropdown sourced from the cached ``accountStatuses``
 * slice (step 3.12) — only FTMO-side accounts are listed because
 * Exness CRUD is Phase 4. Exness account stays a free-text input
 * (Phase 4 will switch it to a dropdown once exness-client ships and
 * the accounts list grows an exness section).
 */
export function PairForm({ pair, onCancel, onSuccess }: Props) {
  const accountStatuses = useAppStore((s) => s.accountStatuses)
  const ftmoAccounts = accountStatuses.filter((a) => a.broker === 'ftmo')

  const [name, setName] = useState(pair?.name ?? '')
  const [ftmoAccountId, setFtmoAccountId] = useState(
    pair?.ftmo_account_id ?? ftmoAccounts[0]?.account_id ?? ''
  )
  const [exnessAccountId, setExnessAccountId] = useState(pair?.exness_account_id ?? '')
  const [ratio, setRatio] = useState<number>(pair?.ratio ?? 1.0)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(): Promise<void> {
    if (!name.trim()) {
      toast.error('Tên pair không được trống')
      return
    }
    if (!ftmoAccountId) {
      toast.error('Chọn FTMO account')
      return
    }
    if (!exnessAccountId.trim()) {
      toast.error('Nhập Exness account ID')
      return
    }
    if (!Number.isFinite(ratio) || ratio <= 0) {
      toast.error('Ratio phải > 0')
      return
    }

    setSubmitting(true)
    try {
      if (pair) {
        await updatePair(pair.pair_id, {
          name,
          ftmo_account_id: ftmoAccountId,
          exness_account_id: exnessAccountId,
          ratio,
        })
        toast.success(`Updated pair "${name}"`)
      } else {
        await createPair({
          name,
          ftmo_account_id: ftmoAccountId,
          exness_account_id: exnessAccountId,
          ratio,
        })
        toast.success(`Created pair "${name}"`)
      }
      await onSuccess()
    } catch (err) {
      toast.error(formatOrderError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="space-y-3">
      <h3 className="font-semibold text-gray-800">
        {pair ? `Edit pair: ${pair.name}` : 'Create new pair'}
      </h3>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">Name *</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="w-full border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">FTMO Account *</label>
        <select
          value={ftmoAccountId}
          onChange={(e) => setFtmoAccountId(e.target.value)}
          className="w-full border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          {ftmoAccounts.length === 0 && <option value="">No FTMO accounts</option>}
          {ftmoAccounts.map((acc) => (
            <option key={acc.account_id} value={acc.account_id}>
              {acc.name || acc.account_id} ({acc.account_id})
            </option>
          ))}
        </select>
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">Exness Account ID *</label>
        <input
          type="text"
          value={exnessAccountId}
          onChange={(e) => setExnessAccountId(e.target.value)}
          placeholder="e.g. exness_001 (Phase 4 will populate)"
          className="w-full border border-gray-300 rounded px-2 py-1 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">Ratio *</label>
        <input
          type="number"
          step="0.01"
          min="0"
          value={ratio}
          onChange={(e) => setRatio(parseFloat(e.target.value) || 0)}
          className="w-full border border-gray-300 rounded px-2 py-1 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <div className="flex justify-end gap-2 pt-2">
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className="px-3 py-1.5 bg-gray-100 hover:bg-gray-200 rounded text-sm text-gray-700 disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={submitting}
          className="px-3 py-1.5 bg-blue-500 text-white hover:bg-blue-600 rounded text-sm disabled:opacity-50"
        >
          {submitting ? 'Saving...' : pair ? 'Update' : 'Create'}
        </button>
      </div>
    </div>
  )
}
