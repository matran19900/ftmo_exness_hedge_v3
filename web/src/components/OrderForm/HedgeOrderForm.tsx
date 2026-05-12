import { useEffect, useRef, useState } from 'react'
import toast from 'react-hot-toast'
import { createOrder, formatOrderError } from '../../api/client'
import { validateSideDirection } from '../../lib/orderValidation'
import { useAppStore } from '../../store'
import { PairPicker } from './PairPicker'
import { PriceInput } from './PriceInput'
import { RiskAmountInput } from './RiskAmountInput'
import { SideSelector } from './SideSelector'
import { VolumeCalculator } from './VolumeCalculator'

/**
 * Hedge order form.
 *
 * Phase 3 / step 3.11: submit posts to ``POST /api/orders`` (FTMO
 * leg only — Phase 4 cascades the Exness leg). Order type is
 * fixed to ``"market"`` in the wire payload: the Entry input drives
 * volume-calculator math (SL distance + risk-based sizing) but
 * the broker is asked to fill at market, so ``entry_price`` ships
 * as ``0`` and the server uses bid/ask for direction validation.
 *
 * After a successful 202 the server's ``response_handler`` (step
 * 3.7) consumes the eventual cTrader fill on resp_stream and
 * broadcasts an ``order_updated`` message on the ``orders`` WS
 * channel (step 3.10a unblocked the whitelist). The
 * ``useWebSocket`` hook calls ``upsertOrder`` and the Open tab
 * picks the row up reactively — no manual refresh needed in the
 * form.
 */
export function HedgeOrderForm() {
  const selectedPairId = useAppStore((s) => s.selectedPairId)
  const selectedSymbol = useAppStore((s) => s.selectedSymbol)
  const symbolDigits = useAppStore((s) => s.symbolDigits)
  const side = useAppStore((s) => s.side)
  const entryPrice = useAppStore((s) => s.entryPrice)
  const setEntryPrice = useAppStore((s) => s.setEntryPrice)
  const slPrice = useAppStore((s) => s.slPrice)
  const setSlPrice = useAppStore((s) => s.setSlPrice)
  const tpPrice = useAppStore((s) => s.tpPrice)
  const setTpPrice = useAppStore((s) => s.setTpPrice)
  const setManualVolumePrimary = useAppStore((s) => s.setManualVolumePrimary)
  const volumeReady = useAppStore((s) => s.volumeReady)
  const effectiveVolumeLots = useAppStore((s) => s.effectiveVolumeLots)
  const latestTick = useAppStore((s) => s.latestTick)
  const accountStatuses = useAppStore((s) => s.accountStatuses)

  // Step 3.12: block submit while no FTMO account is online. We can't
  // see Exness here (Phase 4) so we only gate on the FTMO side.
  // ``accountStatuses === []`` (initial load) is treated as offline —
  // the disabled state lasts at most one REST roundtrip + 5 s WS tick.
  const hasOnlineFtmoAccount = accountStatuses.some(
    (acc) => acc.broker === 'ftmo' && acc.status === 'online'
  )
  const submitBlockedReason = !hasOnlineFtmoAccount
    ? 'FTMO client offline — không thể gửi lệnh'
    : ''

  const [submitting, setSubmitting] = useState(false)

  // Reset draft prices + volume override on actual symbol transitions. The
  // prevSymbolRef guard is critical: on initial mount the store may already
  // hold a persisted selectedSymbol, and we must NOT wipe Entry/SL/TP on
  // first render (the user might be returning to a fresh form anyway, but
  // this also avoids transient null state races during persist hydration).
  const prevSymbolRef = useRef<string | null>(selectedSymbol)
  useEffect(() => {
    if (prevSymbolRef.current !== null && prevSymbolRef.current !== selectedSymbol) {
      setEntryPrice(null)
      setSlPrice(null)
      setTpPrice(null)
      setManualVolumePrimary(null)
    }
    prevSymbolRef.current = selectedSymbol
  }, [selectedSymbol, setEntryPrice, setSlPrice, setTpPrice, setManualVolumePrimary])

  // entrySlError is also enforced as a hard block inside VolumeCalculator;
  // this displays the same message inline under the SL input. tpWarning
  // stays soft (TP is optional and a wrong-direction TP doesn't prevent a
  // valid order from being placed).
  const { entrySlError, tpWarning } = validateSideDirection(side, entryPrice, slPrice, tpPrice)

  /**
   * Final client-side gate before POST. Mirror server's step-3.6
   * checks so the operator gets fast feedback without a round-trip
   * for obvious mistakes. The server re-validates authoritatively;
   * this never trusts the client.
   *
   * Returns ``null`` if OK, else a Vietnamese toast string.
   */
  function preflight(): string | null {
    if (!selectedPairId) return 'Vui lòng chọn pair'
    if (!selectedSymbol) return 'Vui lòng chọn symbol'
    if (entrySlError) return entrySlError
    if (!volumeReady || effectiveVolumeLots === null || effectiveVolumeLots <= 0) {
      return 'Volume chưa sẵn sàng (kiểm tra Entry / SL / Risk)'
    }
    // Market-direction check against latest tick (BUY: SL < bid, TP > ask;
    // SELL: SL > ask, TP < bid). Skipped silently when no tick available
    // — server will reject with ``no_tick_data`` if it can't fetch one
    // server-side either.
    if (latestTick && latestTick.bid !== null && latestTick.ask !== null) {
      const sl = slPrice ?? 0
      const tp = tpPrice ?? 0
      if (side === 'buy') {
        if (sl > 0 && sl >= latestTick.bid) {
          return `SL ${sl} phải < bid hiện tại ${latestTick.bid} cho BUY`
        }
        if (tp > 0 && tp <= latestTick.ask) {
          return `TP ${tp} phải > ask hiện tại ${latestTick.ask} cho BUY`
        }
      } else {
        if (sl > 0 && sl <= latestTick.ask) {
          return `SL ${sl} phải > ask hiện tại ${latestTick.ask} cho SELL`
        }
        if (tp > 0 && tp >= latestTick.bid) {
          return `TP ${tp} phải < bid hiện tại ${latestTick.bid} cho SELL`
        }
      }
    }
    return null
  }

  async function handleSubmit() {
    const blocker = preflight()
    if (blocker) {
      toast.error(blocker)
      return
    }
    // preflight already gated these so the asserts here are just for
    // TypeScript narrowing.
    if (!selectedPairId || !selectedSymbol || effectiveVolumeLots === null) return

    setSubmitting(true)
    try {
      const res = await createOrder({
        pair_id: selectedPairId,
        symbol: selectedSymbol,
        side,
        // Phase 3 / step 3.11 ships market orders only — the Entry
        // input drives volume sizing but the broker fills at
        // market. See module docstring for the rationale.
        order_type: 'market',
        volume_lots: effectiveVolumeLots,
        sl: slPrice ?? 0,
        tp: tpPrice ?? 0,
        entry_price: 0,
      })
      toast.success(`Order tạo: ${res.order_id}`)
      // Partial reset: clear Entry/SL/TP so the next order starts
      // fresh, but keep pair / symbol / side / risk so a fast
      // re-issue (e.g. add another leg on the same symbol) doesn't
      // need re-typing everything.
      setEntryPrice(null)
      setSlPrice(null)
      setTpPrice(null)
    } catch (err) {
      toast.error(formatOrderError(err))
      console.error('createOrder failed', err)
    } finally {
      setSubmitting(false)
    }
  }

  // Submit button stays disabled until every gating condition is met.
  // Reading them up here keeps the JSX terse + makes the disable contract
  // discoverable (mirrors ``preflight`` minus the latestTick check, which
  // we still want to surface as a toast if it fails post-click).
  const placeDisabled =
    submitting ||
    !selectedPairId ||
    !selectedSymbol ||
    !volumeReady ||
    effectiveVolumeLots === null ||
    !hasOnlineFtmoAccount

  const sideClasses =
    side === 'buy'
      ? 'bg-green-500 hover:bg-green-600 disabled:bg-green-300'
      : 'bg-red-500 hover:bg-red-600 disabled:bg-red-300'

  return (
    <div className="h-full bg-white border border-gray-200 rounded p-4 flex flex-col gap-3 overflow-y-auto">
      <h2 className="text-sm font-semibold text-gray-700">New Order</h2>

      <PairPicker />

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">Symbol</label>
        <div className="px-3 py-1.5 bg-gray-50 border border-gray-200 rounded text-sm font-mono text-gray-700">
          {selectedSymbol ?? '—'}
        </div>
      </div>

      <SideSelector />

      <PriceInput
        label="Entry Price"
        value={entryPrice}
        onChange={setEntryPrice}
        digits={symbolDigits}
      />
      <PriceInput
        label="Stop Loss"
        value={slPrice}
        onChange={setSlPrice}
        digits={symbolDigits}
        warning={entrySlError ?? undefined}
      />
      <PriceInput
        label="Take Profit (optional)"
        value={tpPrice}
        onChange={setTpPrice}
        digits={symbolDigits}
        warning={tpWarning ?? undefined}
      />

      <RiskAmountInput />

      <div className="border-t border-gray-200 pt-3">
        <div className="text-xs font-medium text-gray-600 mb-2">Volume Preview</div>
        <VolumeCalculator />
      </div>

      <button
        type="button"
        onClick={handleSubmit}
        disabled={placeDisabled}
        title={submitBlockedReason}
        className={`w-full py-2 rounded text-sm font-bold text-white transition-colors disabled:cursor-not-allowed ${sideClasses}`}
      >
        {submitting
          ? 'Đang gửi...'
          : `${side === 'buy' ? 'BUY' : 'SELL'} ${selectedSymbol ?? ''} ${effectiveVolumeLots ?? ''}`.trim()}
      </button>
      {submitBlockedReason && (
        <div className="text-xs text-red-600 text-center -mt-1">{submitBlockedReason}</div>
      )}
    </div>
  )
}
