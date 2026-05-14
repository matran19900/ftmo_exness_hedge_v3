// MSW browser worker — used by the vite dev server when
// VITE_ENABLE_MSW=1 is set in the environment. Off by default so the
// real backend at /api proxies through to the FastAPI dev server.

import { setupWorker } from 'msw/browser'
import { handlers } from './handlers'

export const worker = setupWorker(...handlers)
