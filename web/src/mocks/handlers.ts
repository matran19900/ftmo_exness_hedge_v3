// MSW handlers for the 7 wizard endpoints + the check-symbol endpoint.
// Used by both the dev-mode browser worker and the vitest node server.

import { HttpResponse, http } from 'msw'
import {
  mockAutoMatchResponse,
  mockCacheList,
  mockMappingStatusPending,
  mockRawSymbols,
  mockSaveResponseCreated,
} from './fixtures'

export const symbolMappingHandlers = [
  http.get('/api/accounts/exness/:id/raw-symbols', ({ params }) =>
    HttpResponse.json({ ...mockRawSymbols, account_id: String(params.id) }),
  ),

  http.get('/api/accounts/exness/:id/mapping-status', ({ params }) =>
    HttpResponse.json({ ...mockMappingStatusPending, account_id: String(params.id) }),
  ),

  http.post('/api/accounts/exness/:id/symbol-mapping/auto-match', ({ params }) =>
    HttpResponse.json({ ...mockAutoMatchResponse, account_id: String(params.id) }),
  ),

  http.post('/api/accounts/exness/:id/symbol-mapping/save', () =>
    HttpResponse.json(mockSaveResponseCreated, { status: 201 }),
  ),

  http.patch('/api/accounts/exness/:id/symbol-mapping/edit', () =>
    HttpResponse.json({ ...mockSaveResponseCreated, created_new_cache: false }),
  ),

  http.get('/api/symbol-mapping-cache', () => HttpResponse.json(mockCacheList)),

  http.post('/api/accounts/exness/:id/symbols/resync', ({ params }) =>
    HttpResponse.json(
      {
        status: 'resync_requested' as const,
        account_id: String(params.id),
        request_id: 'req-mock-1',
      },
      { status: 202 },
    ),
  ),

  http.get('/api/pairs/:pairId/check-symbol/:symbol', () =>
    HttpResponse.json({ tradeable: true, reason: null }),
  ),
]

export const handlers = [...symbolMappingHandlers]
