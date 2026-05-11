import { useState } from 'react'
import toast from 'react-hot-toast'
import { modifyOrder } from '../../api/client'

interface Props {
  orderId: string
  symbol: string
  currentSl: string
  currentTp: string
  onClose: () => void
}

/**
 * Modal for editing SL / TP on a filled order. Each field can be:
 *   - empty   → "keep existing" (sends null to backend).
 *   - "0"     → "remove that side" (sends 0; backend recognizes 0 as
 *               unset and clears the level).
 *   - number  → "set to value" (subject to backend tick-direction
 *               validation; rejection lands as a toast).
 * The submit-button is gated by ``hasAtLeastOneEntry`` so a user
 * can't issue a no-op modify (which the backend also rejects with
 * ``missing_field``).
 */
export function ModifyModal({ orderId, symbol, currentSl, currentTp, onClose }: Props) {
  const [sl, setSl] = useState(currentSl || '')
  const [tp, setTp] = useState(currentTp || '')
  const [submitting, setSubmitting] = useState(false)

  const hasAtLeastOneEntry = sl.trim() !== '' || tp.trim() !== ''

  async function handleSubmit() {
    if (!hasAtLeastOneEntry) {
      toast.error('Provide at least one of SL or TP')
      return
    }
    // Parse — sends null when the field is blank so the server
    // treats it as "unchanged" (vs explicit 0 meaning "remove").
    const slNum = sl.trim() === '' ? null : parseFloat(sl)
    const tpNum = tp.trim() === '' ? null : parseFloat(tp)
    if (slNum !== null && Number.isNaN(slNum)) {
      toast.error('SL must be a number')
      return
    }
    if (tpNum !== null && Number.isNaN(tpNum)) {
      toast.error('TP must be a number')
      return
    }
    setSubmitting(true)
    try {
      await modifyOrder(orderId, slNum, tpNum)
      toast.success(`Modify dispatched for ${symbol}`)
      onClose()
    } catch (err) {
      const msg = extractErrorMessage(err, 'Modify failed')
      toast.error(msg)
    } finally {
      setSubmitting(false)
    }
  }

  function handleBackdropClick(e: React.MouseEvent) {
    if (e.target === e.currentTarget) onClose()
  }

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
      onClick={handleBackdropClick}
      role="dialog"
      aria-modal="true"
      aria-label="Modify SL/TP"
    >
      <div className="bg-white rounded-md p-6 w-96 shadow-lg">
        <h2 className="text-lg font-semibold mb-4">Modify SL/TP — {symbol}</h2>
        <div className="space-y-3">
          <div>
            <label className="block text-sm font-medium mb-1" htmlFor="modify-sl">
              Stop Loss
            </label>
            <input
              id="modify-sl"
              type="number"
              step="any"
              className="w-full border rounded px-3 py-1.5 font-mono"
              value={sl}
              onChange={(e) => setSl(e.target.value)}
              placeholder="0 to remove · blank to keep"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1" htmlFor="modify-tp">
              Take Profit
            </label>
            <input
              id="modify-tp"
              type="number"
              step="any"
              className="w-full border rounded px-3 py-1.5 font-mono"
              value={tp}
              onChange={(e) => setTp(e.target.value)}
              placeholder="0 to remove · blank to keep"
            />
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-6">
          <button
            className="px-4 py-1.5 bg-gray-100 hover:bg-gray-200 rounded text-sm"
            onClick={onClose}
            type="button"
          >
            Cancel
          </button>
          <button
            className="px-4 py-1.5 bg-blue-500 text-white hover:bg-blue-600 rounded disabled:opacity-50 text-sm"
            onClick={handleSubmit}
            disabled={submitting || !hasAtLeastOneEntry}
            type="button"
          >
            {submitting ? 'Sending…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

function extractErrorMessage(err: unknown, fallback: string): string {
  // Axios error shape: ``err.response.data.detail.message`` per step 3.6
  // OrderValidationError → HTTPException mapping.
  if (
    typeof err === 'object' &&
    err !== null &&
    'response' in err &&
    typeof (err as { response?: unknown }).response === 'object'
  ) {
    const resp = (err as { response?: { data?: unknown } }).response
    const data = resp?.data
    if (
      typeof data === 'object' &&
      data !== null &&
      'detail' in data &&
      typeof (data as { detail?: unknown }).detail === 'object'
    ) {
      const detail = (data as { detail?: { message?: unknown } }).detail
      if (detail && typeof detail.message === 'string') {
        return detail.message
      }
    }
  }
  return fallback
}
