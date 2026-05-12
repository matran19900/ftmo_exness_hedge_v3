import { useState } from 'react'
import toast from 'react-hot-toast'
import { deletePair, formatOrderError, listPairs } from '../../api/client'
import { useAppStore } from '../../store'
import { PairForm } from './PairForm'

type Editing = string | 'new' | null

/**
 * Pairs management tab inside the Settings modal (step 3.13).
 *
 * Reads from the cached ``pairs`` store slice (populated at MainPage
 * mount by step 3.12a); after each mutation re-fetches via REST and
 * writes the result back to the store so PairPicker + PositionRow +
 * OrderRow all see the change without a page reload.
 */
export function PairsTab() {
  const pairs = useAppStore((s) => s.pairs)
  const setPairs = useAppStore((s) => s.setPairs)
  const [editing, setEditing] = useState<Editing>(null)
  const [deleting, setDeleting] = useState<string | null>(null)

  async function refresh(): Promise<void> {
    try {
      const data = await listPairs()
      setPairs(data)
    } catch {
      toast.error('Failed to reload pairs')
    }
  }

  async function handleDelete(pairId: string, pairName: string): Promise<void> {
    if (!window.confirm(`Delete pair "${pairName}"?`)) return
    setDeleting(pairId)
    try {
      await deletePair(pairId)
      toast.success(`Deleted pair "${pairName}"`)
      await refresh()
    } catch (err) {
      // 409 ``pair_in_use`` falls through to ``formatOrderError``'s
      // server-message path so the operator sees the count ("Cannot
      // delete pair: N order(s) reference it. Close them first.").
      toast.error(formatOrderError(err))
    } finally {
      setDeleting(null)
    }
  }

  if (editing !== null) {
    const existing = editing === 'new' ? null : (pairs.find((p) => p.pair_id === editing) ?? null)
    return (
      <PairForm
        pair={existing}
        onCancel={() => setEditing(null)}
        onSuccess={async () => {
          await refresh()
          setEditing(null)
        }}
      />
    )
  }

  return (
    <div>
      <div className="flex justify-between items-center mb-3">
        <span className="text-sm text-gray-600">{pairs.length} pair(s)</span>
        <button
          type="button"
          onClick={() => setEditing('new')}
          className="px-3 py-1.5 bg-blue-500 text-white text-sm rounded hover:bg-blue-600"
        >
          + Create Pair
        </button>
      </div>

      {pairs.length === 0 ? (
        <div className="text-sm text-gray-400 text-center py-8">No pairs configured</div>
      ) : (
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b text-xs uppercase tracking-wide text-gray-600">
            <tr>
              <th className="px-3 py-2 text-left">Name</th>
              <th className="px-3 py-2 text-left">FTMO Account</th>
              <th className="px-3 py-2 text-left">Exness Account</th>
              <th className="px-3 py-2 text-right">Ratio</th>
              <th className="px-3 py-2 text-center">Actions</th>
            </tr>
          </thead>
          <tbody>
            {pairs.map((pair) => (
              <tr key={pair.pair_id} className="border-b hover:bg-gray-50">
                <td className="px-3 py-2 font-medium text-gray-700">{pair.name}</td>
                <td className="px-3 py-2 font-mono text-xs text-gray-600">
                  {pair.ftmo_account_id}
                </td>
                <td className="px-3 py-2 font-mono text-xs text-gray-600">
                  {pair.exness_account_id}
                </td>
                <td className="px-3 py-2 text-right font-mono text-gray-700">{pair.ratio}</td>
                <td className="px-3 py-2 text-center whitespace-nowrap">
                  <button
                    type="button"
                    onClick={() => setEditing(pair.pair_id)}
                    className="px-2 py-1 text-xs bg-gray-100 hover:bg-gray-200 rounded mr-1"
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDelete(pair.pair_id, pair.name)}
                    disabled={deleting === pair.pair_id}
                    className="px-2 py-1 text-xs bg-red-100 hover:bg-red-200 rounded disabled:opacity-50"
                  >
                    {deleting === pair.pair_id ? '...' : 'Delete'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
