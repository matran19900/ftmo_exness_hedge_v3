// Realistic mock fixtures for the symbol-mapping wizard.
// Used by both the MSW node server (tests) and browser worker (manual dev).

import type {
  AutoMatchResponse,
  CacheListResponse,
  MappingStatusResponse,
  RawSymbolsResponse,
  SaveMappingResponse,
} from '../api/types/symbolMapping'

export const mockRawSymbols: RawSymbolsResponse = {
  account_id: 'exness_001',
  symbols: [
    {
      name: 'EURUSDm',
      contract_size: 100000,
      digits: 5,
      pip_size: 0.0001,
      volume_min: 0.01,
      volume_step: 0.01,
      volume_max: 200,
      currency_profit: 'USD',
    },
    {
      name: 'GBPUSDm',
      contract_size: 100000,
      digits: 5,
      pip_size: 0.0001,
      volume_min: 0.01,
      volume_step: 0.01,
      volume_max: 200,
      currency_profit: 'USD',
    },
    {
      name: 'USDJPYm',
      contract_size: 100000,
      digits: 3,
      pip_size: 0.01,
      volume_min: 0.01,
      volume_step: 0.01,
      volume_max: 200,
      currency_profit: 'JPY',
    },
    {
      name: 'XAUUSDm',
      contract_size: 100,
      digits: 3,
      pip_size: 0.01,
      volume_min: 0.01,
      volume_step: 0.01,
      volume_max: 100,
      currency_profit: 'USD',
    },
    {
      name: 'BTCUSDm',
      contract_size: 1,
      digits: 2,
      pip_size: 0.01,
      volume_min: 0.01,
      volume_step: 0.01,
      volume_max: 50,
      currency_profit: 'USD',
    },
    {
      name: 'XNGUSD',
      contract_size: 1000,
      digits: 3,
      pip_size: 0.001,
      volume_min: 0.01,
      volume_step: 0.01,
      volume_max: 50,
      currency_profit: 'USD',
    },
  ],
}

export const mockAutoMatchResponse: AutoMatchResponse = {
  account_id: 'exness_001',
  signature: 'abc123def456',
  proposals: [
    { ftmo: 'EURUSD', exness: 'EURUSDm', match_type: 'suffix_strip', confidence: 'medium' },
    { ftmo: 'GBPUSD', exness: 'GBPUSDm', match_type: 'suffix_strip', confidence: 'medium' },
    { ftmo: 'USDJPY', exness: 'USDJPYm', match_type: 'suffix_strip', confidence: 'medium' },
    { ftmo: 'XAUUSD', exness: 'XAUUSDm', match_type: 'suffix_strip', confidence: 'medium' },
    { ftmo: 'NATGAS.cash', exness: 'XNGUSD', match_type: 'manual_hint', confidence: 'low' },
  ],
  unmapped_ftmo: ['SPX500'],
  unmapped_exness: ['BTCUSDm'],
  fuzzy_match_source: null,
  fuzzy_match_score: null,
}

export const mockMappingStatusPending: MappingStatusResponse = {
  account_id: 'exness_001',
  status: 'pending_mapping',
  signature: null,
  cache_filename: null,
}

export const mockSaveResponseCreated: SaveMappingResponse = {
  signature: 'abc123def456',
  cache_filename: 'exness_001_abc123def456.json',
  created_new_cache: true,
  mapping_count: 5,
}

export const mockSaveResponseLinked: SaveMappingResponse = {
  signature: 'abc123def456',
  cache_filename: 'exness_001_abc123def456.json',
  created_new_cache: false,
  mapping_count: 5,
}

export const mockCacheList: CacheListResponse = {
  caches: [
    {
      signature: 'abc123def456',
      filename: 'exness_001_abc123def456.json',
      created_at: '2026-05-14T10:00:00Z',
      used_by_accounts: ['exness_001'],
      mapping_count: 5,
    },
  ],
}
