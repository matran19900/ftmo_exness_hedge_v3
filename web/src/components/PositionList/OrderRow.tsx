import type { Order } from '../../api/client'

function scaleMoney(rawStr: string | undefined, moneyDigitsStr?: string): string {
  if (!rawStr) return '—'
  const raw = parseInt(rawStr, 10)
  if (Number.isNaN(raw)) return '—'
  const digits = moneyDigitsStr ? parseInt(moneyDigitsStr, 10) : NaN
  const d = Number.isNaN(digits) ? 2 : digits
  return (raw / Math.pow(10, d)).toFixed(2)
}

function formatTime(epochMs: string | undefined): string {
  if (!epochMs) return '—'
  const n = parseInt(epochMs, 10)
  if (Number.isNaN(n)) return '—'
  return new Date(n).toLocaleString()
}

/**
 * One closed-order row in the History tab.
 *
 * Renders the broker's recorded close info: close price, realized
 * P&L (scaled by ``p_money_digits`` per D-053), the structured
 * close reason from step 3.5a (``sl`` / ``tp`` / ``manual`` /
 * ``unknown``), and the ↺ marker for orders that were reconstructed
 * from cTrader deal history during a step-3.5b reconcile (vs caught
 * live).
 */
export function OrderRow({ order }: { order: Order }) {
  const pnlDisplay = scaleMoney(order.p_realized_pnl, order.p_money_digits)
  const pnlRaw = parseInt(order.p_realized_pnl ?? '0', 10) || 0
  const pnlColor = pnlRaw > 0 ? 'text-green-600' : pnlRaw < 0 ? 'text-red-600' : 'text-gray-600'
  const volume = order.p_volume_lots ?? order.volume_lots ?? '—'

  return (
    <tr className="border-b hover:bg-gray-50">
      <td className="px-4 py-2 font-mono">{order.symbol}</td>
      <td className="px-4 py-2">
        <span className={order.side === 'buy' ? 'text-green-700' : 'text-red-700'}>
          {order.side.toUpperCase()}
        </span>
      </td>
      <td className="px-4 py-2 text-right font-mono">{volume}</td>
      <td className="px-4 py-2 text-right font-mono">{order.p_fill_price || '—'}</td>
      <td className="px-4 py-2 text-right font-mono">{order.p_close_price || '—'}</td>
      <td className={`px-4 py-2 text-right font-mono font-semibold ${pnlColor}`}>${pnlDisplay}</td>
      <td className="px-4 py-2 text-xs whitespace-nowrap">
        <span className="inline-block px-2 py-0.5 bg-gray-100 rounded">
          {order.p_close_reason || 'unknown'}
        </span>
        {order.p_reconstructed === 'true' && (
          <span
            className="ml-1 text-xs text-blue-600"
            title="Reconstructed from cTrader deal history (offline close)"
          >
            ↺
          </span>
        )}
      </td>
      <td className="px-4 py-2 text-xs whitespace-nowrap">{formatTime(order.p_closed_at)}</td>
    </tr>
  )
}
