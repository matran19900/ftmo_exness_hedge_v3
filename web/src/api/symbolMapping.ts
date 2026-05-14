// Symbol-mapping API client (Phase 4.A.6).
//
// Thin wrappers around the 7 wizard endpoints from server step 4.A.4 +
// the check-symbol endpoint from step 4.A.5. Re-uses the shared
// ``apiClient`` axios instance so the JWT interceptor + 401-on-expiry
// handling come for free.

import { apiClient } from './client'
import type {
  AutoMatchResponse,
  CacheListResponse,
  CheckSymbolResponse,
  MappingStatusResponse,
  RawSymbolsResponse,
  ResyncResponse,
  SaveMappingRequest,
  SaveMappingResponse,
} from './types/symbolMapping'

export const symbolMappingApi = {
  async getRawSymbols(accountId: string): Promise<RawSymbolsResponse> {
    const r = await apiClient.get<RawSymbolsResponse>(
      `/accounts/exness/${accountId}/raw-symbols`,
    )
    return r.data
  },

  async getMappingStatus(accountId: string): Promise<MappingStatusResponse> {
    const r = await apiClient.get<MappingStatusResponse>(
      `/accounts/exness/${accountId}/mapping-status`,
    )
    return r.data
  },

  async runAutoMatch(accountId: string): Promise<AutoMatchResponse> {
    const r = await apiClient.post<AutoMatchResponse>(
      `/accounts/exness/${accountId}/symbol-mapping/auto-match`,
      {},
    )
    return r.data
  },

  async saveMapping(
    accountId: string,
    body: SaveMappingRequest,
  ): Promise<SaveMappingResponse> {
    const r = await apiClient.post<SaveMappingResponse>(
      `/accounts/exness/${accountId}/symbol-mapping/save`,
      body,
    )
    return r.data
  },

  async editMapping(
    accountId: string,
    body: SaveMappingRequest,
  ): Promise<SaveMappingResponse> {
    const r = await apiClient.patch<SaveMappingResponse>(
      `/accounts/exness/${accountId}/symbol-mapping/edit`,
      body,
    )
    return r.data
  },

  async listCaches(): Promise<CacheListResponse> {
    const r = await apiClient.get<CacheListResponse>('/symbol-mapping-cache')
    return r.data
  },

  async triggerResync(accountId: string): Promise<ResyncResponse> {
    const r = await apiClient.post<ResyncResponse>(
      `/accounts/exness/${accountId}/symbols/resync`,
      {},
    )
    return r.data
  },

  async checkPairSymbol(
    pairId: string,
    symbol: string,
  ): Promise<CheckSymbolResponse> {
    const r = await apiClient.get<CheckSymbolResponse>(
      `/pairs/${pairId}/check-symbol/${symbol}`,
    )
    return r.data
  },
}
