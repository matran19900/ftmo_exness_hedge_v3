import { useState } from 'react'
import toast from 'react-hot-toast'
import { closeOrder, type Position } from '../../api/client'
import { ModifyModal } from './ModifyModal'

/**
 * Scale a raw cTrader money-digits int back to a display value.
 *
 * Server stores P&L / commission / balance as raw int64 with an
 * exponent in ``money_digits`` (per D-053). For display we divide
 * by 10^money_digits. Missing / non-numeric raw → ``—``.
 */
function scaleMoney(rawStr: string, moneyDigitsStr: string): string {
  const raw = parseInt(rawStr, 10)
  if (Number.isNaN(raw)) return '—'
  const digits = parseInt(moneyDigitsStr, 10)
  const d = Number.isNaN(digits) ? 2 : digits
  return (raw / Math.pow(10, d)).toFixed(2)
}

function extractErrorMessage(err: unknown, fallback: string): string {
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

interface Props {
  position: Position
  onActionDone: () => void
}

/**
 * One open position row in the Open tab.
 *
 * Displays:
 *   - Symbol / side / volume / entry / current / SL / TP / P&L.
 *   - "Modify" button → opens ``ModifyModal``.
 *   - "Close" button → POST /api/orders/{id}/close after a JS
 *     confirm. The endpoint is 202-async: the row stays here until
 *     the WS broadcast (or next REST refresh) reflects the closed
 *     state.
 *
 * ``is_stale === "true"`` (set by step 3.8 position_tracker when
 * the underlying tick is older than 5 s) renders the whole row at
 * 60 % opacity and tags the current-price cell with a ⚠ icon.
 */
export function PositionRow({ position, onActionDone }: Props) {
  const [closing, setClosing] = useState(false)
  const [modifyOpen, setModifyOpen] = useState(false)

  const pnlDisplay = scaleMoney(position.unrealized_pnl, position.money_digits)
  const pnlRaw = parseInt(position.unrealized_pnl, 10) || 0
  const pnlColor = pnlRaw > 0 ? 'text-green-600' : pnlRaw < 0 ? 'text-red-600' : 'text-gray-600'
  const isStale = position.is_stale === 'true'

  async function handleClose() {
    if (
      !window.confirm(
        `Close ${position.symbol} ${position.side.toUpperCase()} ${position.volume_lots}?`
      )
    ) {
      return
    }
    setClosing(true)
    try {
      await closeOrder(position.order_id)
      toast.success(`Close dispatched for ${position.symbol}`)
    } catch (err) {
      const msg = extractErrorMessage(err, 'Close failed')
      toast.error(msg)
    } finally {
      setClosing(false)
    }
  }

  return (
    <>
      <tr className={`border-b hover:bg-gray-50 ${isStale ? 'opacity-60' : ''}`}>
        <td className="px-4 py-2 font-mono">{position.symbol}</td>
        <td className="px-4 py-2">
          <span className={position.side === 'buy' ? 'text-green-700' : 'text-red-700'}>
            {position.side.toUpperCase()}
          </span>
        </td>
        <td className="px-4 py-2 text-right font-mono">{position.volume_lots}</td>
        <td className="px-4 py-2 text-right font-mono">{position.entry_price || '—'}</td>
        <td className="px-4 py-2 text-right font-mono">
          {position.current_price || '—'}
          {isStale && (
            <span
              className="ml-1 text-xs text-amber-600"
              title={`Tick ${position.tick_age_ms || '?'}ms old`}
            >
              ⚠
            </span>
          )}
        </td>
        <td className="px-4 py-2 text-right font-mono text-xs">
          <div>{position.sl_price || '—'}</div>
          <div>{position.tp_price || '—'}</div>
        </td>
        <td className={`px-4 py-2 text-right font-mono font-semibold ${pnlColor}`}>
          ${pnlDisplay}
        </td>
        <td className="px-4 py-2 text-center whitespace-nowrap">
          <button
            type="button"
            className="px-2 py-1 text-xs bg-amber-100 hover:bg-amber-200 rounded mr-1"
            onClick={() => setModifyOpen(true)}
          >
            Modify
          </button>
          <button
            type="button"
            className="px-2 py-1 text-xs bg-red-100 hover:bg-red-200 rounded disabled:opacity-50"
            disabled={closing}
            onClick={handleClose}
          >
            {closing ? '…' : 'Close'}
          </button>
        </td>
      </tr>
      {modifyOpen && (
        <ModifyModal
          orderId={position.order_id}
          symbol={position.symbol}
          currentSl={position.sl_price ?? ''}
          currentTp={position.tp_price ?? ''}
          onClose={() => {
            setModifyOpen(false)
            onActionDone()
          }}
        />
      )}
    </>
  )
}
