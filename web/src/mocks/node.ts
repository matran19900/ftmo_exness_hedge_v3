// MSW node server — used by the vitest setup (src/test/setup.ts).

import { setupServer } from 'msw/node'
import { handlers } from './handlers'

export const server = setupServer(...handlers)
