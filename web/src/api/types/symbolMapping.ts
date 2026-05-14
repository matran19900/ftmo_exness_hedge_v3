// TypeScript shapes mirroring the Pydantic models in
// server/app/api/symbol_mapping.py + pairs.py (Phase 4.A.4 + 4.A.5).
// Keep field names identical so JSON serialisation lines up without
// transformation at the API boundary.

export type MatchType = 'exact' | 'suffix_strip' | 'manual_hint' | 'override'
export type Confidence = 'high' | 'medium' | 'low'
export type DecisionAction = 'accept' | 'override' | 'skip'
export type MappingStatus =
  | 'pending_mapping'
  | 'active'
  | 'spec_mismatch'
  | 'disconnected'
export type DivergenceSeverity = 'BLOCK' | 'WARN'

export interface RawSymbolResponse {
  name: string
  contract_size: number
  digits: number
  pip_size: number
  volume_min: number
  volume_step: number
  volume_max: number
  currency_profit: string
}

export interface RawSymbolsResponse {
  account_id: string
  symbols: RawSymbolResponse[]
}

export interface MappingStatusResponse {
  account_id: string
  status: MappingStatus
  signature: string | null
  cache_filename: string | null
}

export interface MatchProposalResponse {
  ftmo: string
  exness: string
  match_type: MatchType
  confidence: Confidence
}

export interface AutoMatchResponse {
  account_id: string
  signature: string
  proposals: MatchProposalResponse[]
  unmapped_ftmo: string[]
  unmapped_exness: string[]
  fuzzy_match_source: string | null
  fuzzy_match_score: number | null
}

export interface MappingDecisionRequest {
  ftmo: string
  action: DecisionAction
  exness_override: string | null
}

export interface SaveMappingRequest {
  decisions: MappingDecisionRequest[]
}

export interface SaveMappingResponse {
  signature: string
  cache_filename: string
  created_new_cache: boolean
  mapping_count: number
}

export interface SpecDivergenceResponse {
  symbol: string
  field: string
  cached_value: number | string
  raw_value: number | string
  severity: DivergenceSeverity
  delta_percent: number | null
}

export interface SpecDivergenceErrorResponse {
  detail: string
  divergences: SpecDivergenceResponse[]
}

export interface CacheListEntryResponse {
  signature: string
  filename: string
  created_at: string
  used_by_accounts: string[]
  mapping_count: number
}

export interface CacheListResponse {
  caches: CacheListEntryResponse[]
}

export interface ResyncResponse {
  status: 'resync_requested'
  account_id: string
  request_id: string
}

export interface CheckSymbolResponse {
  tradeable: boolean
  reason: string | null
}

// WebSocket payload for the `mapping_status:{account_id}` channel
// (server-side broadcaster in MappingCacheService.set_mapping_status).
export interface MappingStatusWsData {
  type: 'status_changed'
  account_id: string
  status: MappingStatus
  signature: string | null
  cache_filename: string | null
}
