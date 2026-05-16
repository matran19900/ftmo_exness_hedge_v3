/**
 * MainPage boot-effect tests (Step 4.8g).
 *
 * Verifies that the app-level lifecycle now seeds ``mappingStatusByAccount``
 * via ``useMappingStatusSubscription`` at mount, so the
 * ``wizard-not-run-banner`` in the order form doesn't false-fire on a
 * browser refresh while ``mapping_status`` is already ``"active"`` in
 * Redis.
 *
 * The test mocks every collaborator that MainPage touches so the assert
 * is narrow:
 *   - ``useMappingStatusSubscription`` → ``vi.fn()`` so we can read the
 *     argument list directly.
 *   - ``useWebSocket`` / ``useTickThrottle`` → no-ops; this test doesn't
 *     exercise the real WS connection.
 *   - ``listPairs`` → ``vi.fn()`` returning ``[]``; MainPage swallows
 *     errors but MSW is set to ``onUnhandledRequest: 'error'``.
 *   - Heavy child components are stubbed so jsdom doesn't have to render
 *     the chart / position list / order form (which have their own
 *     deps).
 */
import { render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AccountStatusEntry } from '../api/client'
import { useAppStore } from '../store'

vi.mock('../hooks/useMappingStatusSubscription', () => ({
  useMappingStatusSubscription: vi.fn(),
}))

vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: () => ({ registerCandleHandler: () => undefined }),
}))

vi.mock('../hooks/useTickThrottle', () => ({
  useTickThrottle: () => undefined,
}))

vi.mock('../api/client', async () => {
  const actual =
    await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    listPairs: vi.fn().mockResolvedValue([]),
  }
})

// Children carry their own subscription / DOM measurement logic that is
// out of scope for this hook-invocation test — stub them to null so the
// render() call is cheap and deterministic.
vi.mock('./Chart/HedgeChart', () => ({ HedgeChart: () => null }))
vi.mock('./Header/Header', () => ({ Header: () => null }))
vi.mock('./OrderForm/HedgeOrderForm', () => ({ HedgeOrderForm: () => null }))
vi.mock('./PositionList/PositionList', () => ({ PositionList: () => null }))

const { MainPage } = await import('./MainPage')
const { useMappingStatusSubscription } = await import(
  '../hooks/useMappingStatusSubscription'
)

const subscriptionMock = vi.mocked(useMappingStatusSubscription)

function seedAccount(
  broker: 'ftmo' | 'exness',
  account_id: string,
): AccountStatusEntry {
  return {
    broker,
    account_id,
    name: `${broker} ${account_id}`,
    status: 'online',
    enabled: true,
    balance_raw: '1000000',
    equity_raw: '1000000',
    margin_raw: '0',
    free_margin_raw: '1000000',
    currency: 'USD',
    money_digits: '2',
  }
}

describe('MainPage — Step 4.8g boot-level mapping_status seed', () => {
  beforeEach(() => {
    subscriptionMock.mockClear()
    useAppStore.setState({ accountStatuses: [], pairs: [] })
  })

  afterEach(() => {
    useAppStore.setState({ accountStatuses: [], pairs: [] })
  })

  it('invokes useMappingStatusSubscription at mount with Exness account IDs only', () => {
    useAppStore.setState({
      accountStatuses: [
        seedAccount('ftmo', 'ftmo_001'),
        seedAccount('exness', 'exness_001'),
        seedAccount('exness', 'exness_002'),
      ],
    })
    render(<MainPage />)
    expect(subscriptionMock).toHaveBeenCalled()
    const lastArg = subscriptionMock.mock.calls.at(-1)?.[0]
    expect(lastArg).toEqual(['exness_001', 'exness_002'])
  })

  it('invokes the hook with an empty array when no Exness accounts are loaded yet', () => {
    useAppStore.setState({ accountStatuses: [seedAccount('ftmo', 'ftmo_001')] })
    render(<MainPage />)
    expect(subscriptionMock).toHaveBeenCalled()
    const lastArg = subscriptionMock.mock.calls.at(-1)?.[0]
    expect(lastArg).toEqual([])
  })
})
