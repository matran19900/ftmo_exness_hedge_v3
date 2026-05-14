/**
 * API client signature verification (Phase 4.A.7).
 *
 * The server's POST /api/symbols/{sym}/calculate-volume now requires
 * `pair_id` in the body (step 4.A.5 breaking change). We assert that the
 * client adds it and that omitting it is a TypeScript error at compile
 * time (the test file would not have type-checked otherwise).
 */
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { calculateVolume } from './client'
import { server } from '../mocks/node'

describe('calculateVolume API client', () => {
  afterEach(() => {
    server.resetHandlers()
  })

  it('POSTs to /symbols/{symbol}/calculate-volume with pair_id in body', async () => {
    let observedBody: Record<string, unknown> | null = null
    server.use(
      http.post('/api/symbols/:symbol/calculate-volume', async ({ request }) => {
        observedBody = (await request.json()) as Record<string, unknown>
        return HttpResponse.json({
          symbol: 'EURUSD',
          volume_primary: 0.1,
          volume_secondary: 0.1,
          sl_pips: 50,
          pip_value_usd_per_lot: 10,
          sl_usd_per_lot: 500,
          quote_ccy: 'USD',
          quote_to_usd_rate: 1,
        })
      }),
    )

    const result = await calculateVolume('EURUSD', {
      pair_id: 'pair_001',
      entry: 1.085,
      sl: 1.08,
      risk_amount: 100,
      ratio: 1,
    })

    expect(observedBody).toEqual({
      pair_id: 'pair_001',
      entry: 1.085,
      sl: 1.08,
      risk_amount: 100,
      ratio: 1,
    })
    expect(result.volume_primary).toBe(0.1)
  })

  it('threads symbol into the URL path', async () => {
    let observedUrl = ''
    server.use(
      http.post('/api/symbols/:symbol/calculate-volume', ({ request }) => {
        observedUrl = new URL(request.url).pathname
        return HttpResponse.json({
          symbol: 'GBPUSD',
          volume_primary: 0.05,
          volume_secondary: 0.05,
          sl_pips: 30,
          pip_value_usd_per_lot: 10,
          sl_usd_per_lot: 300,
          quote_ccy: 'USD',
          quote_to_usd_rate: 1,
        })
      }),
    )

    await calculateVolume('GBPUSD', {
      pair_id: 'pair_002',
      entry: 1.27,
      sl: 1.265,
      risk_amount: 100,
      ratio: 1,
    })
    expect(observedUrl).toBe('/api/symbols/GBPUSD/calculate-volume')
  })
})
