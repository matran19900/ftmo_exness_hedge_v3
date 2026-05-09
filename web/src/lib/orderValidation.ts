import type { OrderSide } from '../store'

export interface SideValidationResult {
  /**
   * Hard error — blocks volume calc and submit. Set when SL is on the wrong
   * side of Entry for the chosen direction (BUY: SL ≥ Entry; SELL: SL ≤ Entry).
   */
  entrySlError: string | null
  /**
   * Soft warning — display only, never blocks. TP is optional and a wrong
   * direction TP would simply trigger immediately on fill, but the order
   * itself is still placeable.
   */
  tpWarning: string | null
}

/**
 * Validate Entry/SL/TP against side direction.
 *
 * Hard rules (block):
 *   BUY:  SL must be below Entry (SL < Entry).
 *   SELL: SL must be above Entry (SL > Entry).
 *
 * Soft rules (warning only):
 *   BUY:  TP should be above Entry.
 *   SELL: TP should be below Entry.
 */
export function validateSideDirection(
  side: OrderSide,
  entry: number | null,
  sl: number | null,
  tp: number | null
): SideValidationResult {
  let entrySlError: string | null = null
  let tpWarning: string | null = null

  if (entry !== null && sl !== null) {
    if (side === 'buy' && sl >= entry) {
      entrySlError = 'BUY: SL must be below Entry'
    } else if (side === 'sell' && sl <= entry) {
      entrySlError = 'SELL: SL must be above Entry'
    }
  }

  if (entry !== null && tp !== null) {
    if (side === 'buy' && tp <= entry) {
      tpWarning = 'BUY: TP should be above Entry'
    } else if (side === 'sell' && tp >= entry) {
      tpWarning = 'SELL: TP should be below Entry'
    }
  }

  return { entrySlError, tpWarning }
}
