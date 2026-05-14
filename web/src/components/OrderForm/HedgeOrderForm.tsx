import { useEffect, useMemo, useRef, useState } from 'react'
import toast from 'react-hot-toast'
import { createOrder, formatOrderError } from '../../api/client'
import { symbolMappingApi } from '../../api/symbolMapping'
import { validateSideDirection } from '../../lib/orderValidation'
import { useAppStore } from '../../store'
import { OrderTypeSelector } from './OrderTypeSelector'
import { PairPicker } from './PairPicker'
import { PriceInput } from './PriceInput'
import { RiskAmountInput } from './RiskAmountInput'
import { SideSelector } from './SideSelector'
import { VolumeCalculator } from './VolumeCalculator'

// Phase 4.A.7: 4 failure-reason → human-readable toast strings. Mirrors
// MappingService.is_pair_symbol_tradeable's TradeableReason literal.
function checkSymbolReasonToMessage(reason: string | null): string {
  switch (reason) {
    case 'pair_not_found':
      return 'Pair not found. Refresh the pair list and try again.'
    case 'ftmo_symbol_not_whitelisted':
      return 'Symbol not in FTMO whitelist.'
    case 'exness_account_has_no_mapping':
      return 'Exness wizard not yet run. Go to Settings → Accounts → Map Symbols.'
    case 'ftmo_symbol_not_mapped_for_exness_account':
      return 'This symbol has no Exness mapping for the selected pair. Edit the mapping or pick a different symbol.'
    default:
      return 'Cannot trade this symbol on the selected pair.'
  }
}

/**
 * Hedge order form.
 *
 * Phase 3 / step 3.11 wired POST /api/orders as market-only. Step
 * 3.12b adds an Order Type segmented selector (Market / Limit /
 * Stop) and rewires the Entry input to behave per type:
 *
 *  - Market: Entry input is hidden; ``entryPrice`` auto-populates
 *    from the throttled tick (ask for BUY, bid for SELL) so the
 *    volume calculator still has an entry value to size against.
 *    Submit ships ``entry_price: 0`` so the server uses live
 *    bid/ask for direction validation (D-110 unchanged for market).
 *  - Limit: Entry input visible + required; operator-supplied
 *    price ships through. Preflight: BUY entry must be < live ask,
 *    SELL entry must be > live bid (else cTrader would fire it
 *    immediately as market).
 *  - Stop: Entry input visible + required. Preflight: BUY entry
 *    must be > live ask, SELL entry must be < live bid (the
 *    breakout entry direction).
 *
 * Manual volume override (Phase 2 ``manualVolumePrimary``) is
 * preserved across all three types — once the operator pins a
 * volume, throttled-tick entry refreshes don't bump it.
 *
 * After a successful 202 the server's ``response_handler`` (step
 * 3.7) consumes the eventual cTrader fill on resp_stream and
 * broadcasts an ``order_updated`` message on the ``orders`` WS
 * channel. The ``useWebSocket`` hook calls ``upsertOrder`` and the
 * Open tab picks the row up reactively.
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
  const orderType = useAppStore((s) => s.orderType)
  const tickThrottled = useAppStore((s) => s.tickThrottled)
  const pairs = useAppStore((s) => s.pairs)
  const mappingStatusByAccount = useAppStore((s) => s.mappingStatusByAccount)

  // Phase 4.A.7: resolve the selected pair and its Exness mapping status
  // for the wizard-not-run banner + submit gate. Phase 3 single-leg pairs
  // (no exness_account_id) skip the wizard check entirely — same fallback
  // as the server's MappingService.is_pair_symbol_tradeable (D-4.A.5-2).
  const selectedPair = useMemo(
    () => pairs.find((p) => p.pair_id === selectedPairId) ?? null,
    [pairs, selectedPairId],
  )
  const pairExnessAccountId = selectedPair?.exness_account_id || null
  const pairMappingStatus = pairExnessAccountId
    ? (mappingStatusByAccount[pairExnessAccountId] ?? 'pending_mapping')
    : null
  const isWizardNotRun =
    pairExnessAccountId !== null && pairMappingStatus !== 'active'

  // Step 3.12 / 3.13a: block submit while no FTMO account is online,
  // AND surface a tooltip that names the specific reason so the
  // operator knows where to act.
  //
  // Three distinct block reasons in priority order (most actionable
  // wins):
  //   1. No FTMO accounts configured at all → operator must go to
  //      Settings → Accounts (or wait for the bootstrap to register one).
  //   2. Every FTMO account toggled off via Settings → not a client
  //      crash, just an operator pause. Different fix (re-enable).
  //   3. Heartbeat-dead client (status=offline) → the FTMO client
  //      process needs investigation. Different from a deliberate pause.
  //
  // Empty ``accountStatuses`` (transient: initial load before REST
  // returns) trips case 1 and disables submit; the state lasts at
  // most one REST roundtrip + 5 s WS tick.
  const ftmoAccounts = accountStatuses.filter((a) => a.broker === 'ftmo')
  const hasOnlineFtmoAccount = ftmoAccounts.some((a) => a.status === 'online')
  let ftmoBlockMessage: string | null = null
  if (ftmoAccounts.length === 0) {
    ftmoBlockMessage = 'Chưa có FTMO account được cấu hình'
  } else if (ftmoAccounts.every((a) => a.status === 'disabled')) {
    ftmoBlockMessage = 'FTMO account đã bị vô hiệu hóa (mở Settings → Accounts để bật lại)'
  } else if (!hasOnlineFtmoAccount) {
    ftmoBlockMessage = 'FTMO client offline (heartbeat đã expired)'
  }

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

  // Step 3.12b: market-mode auto-drive of entryPrice from the throttled
  // tick. Effect fires when type/side/throttled tick/symbol change; the
  // 1 Hz throttle means ``tickThrottled`` updates at most once per second,
  // which the VolumeCalculator's 300 ms debounce then smooths further.
  // entryPrice is deliberately NOT in the dep array — we are the writer
  // here, and including it would create a no-op self-loop.
  useEffect(() => {
    if (orderType !== 'market') return
    if (!tickThrottled) return
    // Defensive race: if the user switched symbols faster than the
    // throttle interval, the snapshot may still carry the old symbol.
    // useTickThrottle clears to null on no-match, but the in-flight
    // tickThrottled value might still be stale for one render. Skip
    // until the next snapshot lines up.
    if (selectedSymbol && tickThrottled.symbol !== selectedSymbol) return
    const newEntry = side === 'buy' ? tickThrottled.ask : tickThrottled.bid
    setEntryPrice(newEntry)
  }, [orderType, side, tickThrottled, selectedSymbol, setEntryPrice])

  // Step 3.12b: clear entryPrice when leaving market mode so the operator
  // sees a fresh blank input rather than the last auto-driven tick value.
  // No-op on mount when the persisted type is already non-market (entry
  // is null after persist hydration anyway).
  const prevOrderTypeRef = useRef<typeof orderType>(orderType)
  useEffect(() => {
    if (prevOrderTypeRef.current !== orderType && orderType !== 'market') {
      setEntryPrice(null)
    }
    prevOrderTypeRef.current = orderType
  }, [orderType, setEntryPrice])

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
    // Step 3.12b: limit/stop need an explicit entry price (market
    // auto-populates from the throttled tick). Block the submit
    // before the SL/TP / entry-direction checks below so the toast
    // names the actually-missing field instead of complaining about
    // a derived comparison.
    if (orderType !== 'market' && entryPrice === null) {
      return orderType === 'limit'
        ? 'Vui lòng nhập Entry Price cho lệnh Limit'
        : 'Vui lòng nhập Entry Price cho lệnh Stop'
    }

    // SL/TP direction check against latest tick (BUY: SL < bid, TP > ask;
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

      // Step 3.12b: limit/stop entry direction. Mirrors the server's
      // step-3.6 ``invalid_*`` rejections so the operator gets the
      // toast before the round-trip. ``entryPrice`` is already
      // non-null at this point (guarded above).
      if (orderType === 'limit' && entryPrice !== null) {
        if (side === 'buy' && entryPrice >= latestTick.ask) {
          return `Limit BUY: entry ${entryPrice} phải < ask hiện tại ${latestTick.ask}`
        }
        if (side === 'sell' && entryPrice <= latestTick.bid) {
          return `Limit SELL: entry ${entryPrice} phải > bid hiện tại ${latestTick.bid}`
        }
      } else if (orderType === 'stop' && entryPrice !== null) {
        if (side === 'buy' && entryPrice <= latestTick.ask) {
          return `Stop BUY: entry ${entryPrice} phải > ask hiện tại ${latestTick.ask}`
        }
        if (side === 'sell' && entryPrice >= latestTick.bid) {
          return `Stop SELL: entry ${entryPrice} phải < bid hiện tại ${latestTick.bid}`
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

    // Phase 4.A.7: server-side pre-flight via /api/pairs/{}/check-symbol/{}.
    // Catches the 4 documented TradeableReason failures so the operator
    // gets a precise toast instead of a generic 400 from the order endpoint.
    // Phase 3 single-leg pairs return tradeable=true silently (server fallback
    // D-4.A.5-2) so this call is also harmless on legacy paths.
    try {
      const check = await symbolMappingApi.checkPairSymbol(
        selectedPairId,
        selectedSymbol,
      )
      if (!check.tradeable) {
        toast.error(checkSymbolReasonToMessage(check.reason))
        return
      }
    } catch (err) {
      toast.error(formatOrderError(err))
      return
    }

    setSubmitting(true)
    try {
      const res = await createOrder({
        pair_id: selectedPairId,
        symbol: selectedSymbol,
        side,
        // Step 3.12b: ship the operator-chosen order_type. Server
        // (step 3.6) accepts market/limit/stop with the same shape.
        order_type: orderType,
        volume_lots: effectiveVolumeLots,
        sl: slPrice ?? 0,
        tp: tpPrice ?? 0,
        // Market mode: ``entry_price=0`` keeps D-110 — server uses
        // live bid/ask for direction. Limit/Stop: ship the operator
        // value; preflight already enforced direction.
        entry_price: orderType === 'market' ? 0 : (entryPrice ?? 0),
      })
      toast.success(`Order tạo: ${res.order_id}`)
      // Partial reset. Clear SL/TP always. Clear Entry only when not
      // in market mode — the market-mode auto-driver would just
      // re-populate it from the next throttled tick anyway, and
      // briefly nulling it would flash the volume calc into the
      // "Fill Entry, SL, Risk" placeholder for one render.
      setSlPrice(null)
      setTpPrice(null)
      if (orderType !== 'market') {
        setEntryPrice(null)
      }
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
  // Phase 4.A.7: ``isWizardNotRun`` blocks hedge orders when the wizard
  // hasn't been run for the pair's Exness account — CTO-confirmed hard
  // block (no silent degrade to single-leg). See plan §2.5 / D-4.A.5-2.
  const placeDisabled =
    submitting ||
    !selectedPairId ||
    !selectedSymbol ||
    !volumeReady ||
    effectiveVolumeLots === null ||
    !hasOnlineFtmoAccount ||
    isWizardNotRun

  const wizardBlockMessage = isWizardNotRun
    ? `Hedge leg blocked — Exness wizard not run for account ${pairExnessAccountId}. Open Settings → Accounts → Map Symbols.`
    : null

  const sideClasses =
    side === 'buy'
      ? 'bg-green-500 hover:bg-green-600 disabled:bg-green-300'
      : 'bg-red-500 hover:bg-red-600 disabled:bg-red-300'

  return (
    <div className="h-full bg-white border border-gray-200 rounded p-4 flex flex-col gap-3 overflow-y-auto">
      <h2 className="text-sm font-semibold text-gray-700">New Order</h2>

      {wizardBlockMessage && (
        <div
          data-testid="wizard-not-run-banner"
          className="border border-amber-300 bg-amber-50 text-amber-900 text-xs rounded px-3 py-2"
          role="alert"
        >
          ⚠ {wizardBlockMessage}
        </div>
      )}

      <PairPicker />

      <div>
        <label className="block text-xs font-medium text-gray-600 mb-1">Symbol</label>
        <div className="px-3 py-1.5 bg-gray-50 border border-gray-200 rounded text-sm font-mono text-gray-700">
          {selectedSymbol ?? '—'}
        </div>
      </div>

      <OrderTypeSelector />

      <SideSelector />

      {/* Step 3.12b: limit/stop need an explicit entry input; market
          auto-populates from the throttled tick and surfaces it as
          a read-only preview line so the operator still sees the
          number the volume calc is sizing against. */}
      {orderType === 'market' ? (
        <div className="text-xs text-gray-500">
          Market entry (auto):{' '}
          {tickThrottled ? (
            <span className="font-mono text-gray-700">
              {side === 'buy' ? tickThrottled.ask : tickThrottled.bid}
            </span>
          ) : (
            <span className="italic">đang chờ tick...</span>
          )}
        </div>
      ) : (
        <PriceInput
          label={`Entry Price (${orderType === 'limit' ? 'Limit' : 'Stop'})`}
          value={entryPrice}
          onChange={setEntryPrice}
          digits={symbolDigits}
        />
      )}
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
        data-testid="place-order-button"
        onClick={handleSubmit}
        disabled={placeDisabled}
        title={ftmoBlockMessage ?? wizardBlockMessage ?? ''}
        className={`w-full py-2 rounded text-sm font-bold text-white transition-colors disabled:cursor-not-allowed ${sideClasses}`}
      >
        {submitting
          ? 'Đang gửi...'
          : `${side === 'buy' ? 'BUY' : 'SELL'} ${selectedSymbol ?? ''} ${effectiveVolumeLots ?? ''}`.trim()}
      </button>
      {ftmoBlockMessage && (
        <div className="text-xs text-red-600 text-center -mt-1">{ftmoBlockMessage}</div>
      )}
    </div>
  )
}
