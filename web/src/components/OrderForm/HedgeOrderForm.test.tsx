/**
 * HedgeOrderForm Phase 4.A.7 wizard-not-run banner + pre-flight tests.
 *
 * The form depends on a long chain of sub-components (PairPicker reads
 * pairs from the store, etc.). To keep the test surface narrow we seed
 * the store directly with the minimum Phase 3 happy-path state, then
 * vary `mappingStatusByAccount` to exercise the Phase 4.A.7 branches.
 *
 * MSW intercepts /pairs/{}/check-symbol/{} so we can assert the
 * pre-flight is called and that the toast text matches the documented
 * TradeableReason → message mapping.
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import toast from 'react-hot-toast'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PairResponse } from '../../api/client'
import { server } from '../../mocks/node'
import { useAppStore } from '../../store'
import { HedgeOrderForm } from './HedgeOrderForm'

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}))

// Background MSW handlers so the form's eager side-effect fetches
// (VolumeCalculator's debounced calculate-volume, PairPicker's listPairs,
// etc.) don't trip MSW's onUnhandledRequest='error' guard.
const FORM_BACKGROUND_HANDLERS = [
  http.post('/api/symbols/:symbol/calculate-volume', () =>
    HttpResponse.json({
      symbol: 'EURUSD',
      volume_primary: 0.1,
      volume_secondary: 0.1,
      sl_pips: 50,
      pip_value_usd_per_lot: 10,
      sl_usd_per_lot: 500,
      quote_ccy: 'USD',
      quote_to_usd_rate: 1,
    }),
  ),
  http.get('/api/pairs/', () => HttpResponse.json([])),
  http.get('/api/accounts', () =>
    HttpResponse.json({ accounts: [], total: 0 }),
  ),
]

const PAIR_HEDGE: PairResponse = {
  pair_id: 'pair_001',
  name: 'Hedge pair',
  ftmo_account_id: 'ftmo_001',
  exness_account_id: 'exness_001',
  ratio: 1,
  created_at: 0,
  updated_at: 0,
}

const PAIR_SINGLE_LEG: PairResponse = {
  pair_id: 'pair_legacy',
  name: 'Phase-3 single-leg',
  ftmo_account_id: 'ftmo_001',
  exness_account_id: '',
  ratio: 1,
  created_at: 0,
  updated_at: 0,
}

function seedHappyPath(pair: PairResponse) {
  useAppStore.setState({
    pairs: [pair],
    selectedPairId: pair.pair_id,
    selectedSymbol: 'EURUSD',
    side: 'buy',
    orderType: 'market',
    entryPrice: 1.085,
    slPrice: 1.08,
    tpPrice: null,
    riskAmount: 100,
    // Manual volume override puts VolumeCalculator into the manual-mode
    // branch which sets volumeReady=true synchronously; otherwise its
    // initial useEffect would reset volumeReady to false until the
    // debounced calculate-volume API call resolves.
    manualVolumePrimary: 0.1,
    volumeReady: true,
    effectiveVolumeLots: 0.1,
    accountStatuses: [
      {
        broker: 'ftmo',
        account_id: 'ftmo_001',
        name: 'FTMO',
        enabled: true,
        status: 'online',
        balance_raw: '1000000',
        equity_raw: '1000000',
        margin_raw: '0',
        free_margin_raw: '1000000',
        currency: 'USD',
        money_digits: '2',
      },
    ],
    mappingStatusByAccount: {},
    latestTick: { bid: 1.085, ask: 1.0852, ts: 1 },
    tickThrottled: { bid: 1.085, ask: 1.0852, ts: 1, symbol: 'EURUSD' },
  })
}

describe('HedgeOrderForm wizard-not-run banner', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    server.use(...FORM_BACKGROUND_HANDLERS)
  })

  afterEach(() => {
    useAppStore.setState({
      pairs: [],
      selectedPairId: null,
      mappingStatusByAccount: {},
      accountStatuses: [],
    })
  })

  it('shows banner when pair has exness_account_id and status != active', () => {
    seedHappyPath(PAIR_HEDGE)
    useAppStore
      .getState()
      .setMappingStatusForAccount('exness_001', 'pending_mapping')
    render(<HedgeOrderForm />)
    expect(screen.getByTestId('wizard-not-run-banner')).toBeInTheDocument()
    expect(screen.getByTestId('wizard-not-run-banner')).toHaveTextContent(
      /Hedge leg blocked/,
    )
  })

  it('hides banner when status is active', () => {
    seedHappyPath(PAIR_HEDGE)
    useAppStore.getState().setMappingStatusForAccount('exness_001', 'active')
    render(<HedgeOrderForm />)
    expect(screen.queryByTestId('wizard-not-run-banner')).toBeNull()
  })

  it('hides banner when pair has no exness_account_id (Phase 3 single-leg)', () => {
    seedHappyPath(PAIR_SINGLE_LEG)
    render(<HedgeOrderForm />)
    expect(screen.queryByTestId('wizard-not-run-banner')).toBeNull()
  })

  it('shows banner for spec_mismatch status too', () => {
    seedHappyPath(PAIR_HEDGE)
    useAppStore
      .getState()
      .setMappingStatusForAccount('exness_001', 'spec_mismatch')
    render(<HedgeOrderForm />)
    expect(screen.getByTestId('wizard-not-run-banner')).toBeInTheDocument()
  })

  it('disables Place Order button when banner active', () => {
    seedHappyPath(PAIR_HEDGE)
    useAppStore
      .getState()
      .setMappingStatusForAccount('exness_001', 'pending_mapping')
    render(<HedgeOrderForm />)
    expect(screen.getByTestId('place-order-button')).toBeDisabled()
  })

  it('enables Place Order button once status flips to active (WS reactive)', () => {
    seedHappyPath(PAIR_HEDGE)
    useAppStore
      .getState()
      .setMappingStatusForAccount('exness_001', 'pending_mapping')
    const { rerender } = render(<HedgeOrderForm />)
    expect(screen.getByTestId('place-order-button')).toBeDisabled()

    // Simulate WS broadcast updating the status to active.
    useAppStore.getState().setMappingStatusForAccount('exness_001', 'active')
    rerender(<HedgeOrderForm />)
    expect(screen.queryByTestId('wizard-not-run-banner')).toBeNull()
    expect(screen.getByTestId('place-order-button')).not.toBeDisabled()
  })

  it('Phase 3 single-leg pair leaves Place Order enabled (no banner)', () => {
    seedHappyPath(PAIR_SINGLE_LEG)
    render(<HedgeOrderForm />)
    expect(screen.getByTestId('place-order-button')).not.toBeDisabled()
  })
})

describe('HedgeOrderForm pre-flight check', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    server.use(...FORM_BACKGROUND_HANDLERS)
    seedHappyPath(PAIR_HEDGE)
    useAppStore.getState().setMappingStatusForAccount('exness_001', 'active')
  })

  afterEach(() => {
    server.resetHandlers()
    useAppStore.setState({
      pairs: [],
      selectedPairId: null,
      mappingStatusByAccount: {},
      accountStatuses: [],
    })
  })

  async function clickPlaceOrder() {
    render(<HedgeOrderForm />)
    await userEvent.click(screen.getByTestId('place-order-button'))
  }

  it('calls /pairs/{}/check-symbol/{} before submit', async () => {
    let pathHit = ''
    server.use(
      http.get('/api/pairs/:pairId/check-symbol/:symbol', ({ request }) => {
        pathHit = new URL(request.url).pathname
        return HttpResponse.json({ tradeable: true, reason: null })
      }),
      http.post('/api/orders', () =>
        HttpResponse.json(
          { order_id: 'o1', request_id: 'r1', status: 'accepted' },
          { status: 202 },
        ),
      ),
    )
    await clickPlaceOrder()
    await waitFor(() => {
      expect(pathHit).toBe('/api/pairs/pair_001/check-symbol/EURUSD')
    })
  })

  it.each([
    ['pair_not_found', /Pair not found/],
    ['ftmo_symbol_not_whitelisted', /not in FTMO whitelist/],
    ['exness_account_has_no_mapping', /Exness wizard not yet run/],
    [
      'ftmo_symbol_not_mapped_for_exness_account',
      /no Exness mapping for the selected pair/,
    ],
  ])(
    'maps reason %s to the right toast message',
    async (reason, messageRegex) => {
      server.use(
        http.get('/api/pairs/:pairId/check-symbol/:symbol', () =>
          HttpResponse.json({ tradeable: false, reason }),
        ),
      )
      await clickPlaceOrder()
      await waitFor(() => {
        expect(toast.error).toHaveBeenCalled()
      })
      const errorMock = vi.mocked(toast.error)
      const lastArgs = errorMock.mock.calls[errorMock.mock.calls.length - 1]
      expect(String(lastArgs?.[0] ?? '')).toMatch(messageRegex)
    },
  )

  it('blocks order submit when tradeable=false', async () => {
    let orderPosted = false
    server.use(
      http.get('/api/pairs/:pairId/check-symbol/:symbol', () =>
        HttpResponse.json({ tradeable: false, reason: 'pair_not_found' }),
      ),
      http.post('/api/orders', () => {
        orderPosted = true
        return HttpResponse.json(
          { order_id: 'x', request_id: 'y', status: 'accepted' },
          { status: 202 },
        )
      }),
    )
    await clickPlaceOrder()
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalled()
    })
    expect(orderPosted).toBe(false)
  })

  it('proceeds with order submit when tradeable=true', async () => {
    let orderPosted = false
    server.use(
      http.get('/api/pairs/:pairId/check-symbol/:symbol', () =>
        HttpResponse.json({ tradeable: true, reason: null }),
      ),
      http.post('/api/orders', () => {
        orderPosted = true
        return HttpResponse.json(
          { order_id: 'o1', request_id: 'r1', status: 'accepted' },
          { status: 202 },
        )
      }),
    )
    await clickPlaceOrder()
    await waitFor(() => {
      expect(orderPosted).toBe(true)
    })
    expect(toast.success).toHaveBeenCalled()
  })
})
