import { useCallback, useEffect, useRef } from 'react'
import type {
  WsCandleMessage,
  WsClientMessage,
  WsOrderUpdatedMessage,
  WsPositionEventMessage,
  WsPositionsTickMessage,
  WsServerMessage,
  WsTickMessage,
} from '../api/client'
import { useAppStore } from '../store'

// Exponential backoff: 1s, 2s, 4s, 8s, 16s, then capped at 30s.
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 16000]
const MAX_RECONNECT_DELAY_MS = 30000

type CandleUpdateHandler = (msg: WsCandleMessage) => void

interface UseWebSocketResult {
  sendMessage: (msg: WsClientMessage) => void
  registerCandleHandler: (handler: CandleUpdateHandler | null) => void
}

/**
 * Single shared WebSocket connection.
 *
 * Lifecycle is driven by `token`: connects when set, disconnects (code 1000)
 * on logout. Reconnects with exponential backoff on any non-1000 close.
 *
 * Tick messages for the currently selected symbol are dispatched to
 * `store.latestTick`. Candle updates are dispatched to a single registered
 * handler (HedgeChart owns it) so the chart stays the source of truth for the
 * series instance.
 *
 * `set_symbol` is auto-sent when `selectedSymbol`/`selectedTimeframe` change
 * AND auto-resent on reconnect (read fresh from `useAppStore.getState()`).
 */
export function useWebSocket(): UseWebSocketResult {
  const token = useAppStore((s) => s.token)
  const selectedSymbol = useAppStore((s) => s.selectedSymbol)
  const selectedTimeframe = useAppStore((s) => s.selectedTimeframe)

  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<number | null>(null)
  const reconnectAttemptRef = useRef(0)
  const candleHandlerRef = useRef<CandleUpdateHandler | null>(null)
  const explicitCloseRef = useRef(false)

  const sendMessage = useCallback((msg: WsClientMessage) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg))
    } else {
      console.warn('WS not open; message dropped:', msg)
    }
  }, [])

  const registerCandleHandler = useCallback((handler: CandleUpdateHandler | null) => {
    candleHandlerRef.current = handler
  }, [])

  // Connection lifecycle keyed on token. Store setters and selectedSymbol are
  // intentionally excluded from deps — store setters are stable refs and
  // selectedSymbol is read fresh from getState() so symbol changes don't tear
  // the socket down.
  useEffect(() => {
    if (!token) {
      if (wsRef.current) {
        explicitCloseRef.current = true
        wsRef.current.close(1000, 'logout')
        wsRef.current = null
      }
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      useAppStore.getState().setWsState('disconnected')
      return
    }

    explicitCloseRef.current = false

    // Step 3.10: positions channel handlers. Pulled into local
    // functions so the onmessage switch stays readable.
    const handlePositionsChannel = (msg: WsPositionsTickMessage | WsPositionEventMessage) => {
      const store = useAppStore.getState()
      if (msg.data.type === 'positions_tick') {
        // The batched envelope carries N position updates. The store
        // merges each into the existing positions array; rows for
        // order_ids not in the array are dropped (next REST refresh
        // will pull them).
        for (const p of msg.data.positions) {
          store.upsertPositionTick({
            order_id: p.order_id,
            symbol: p.symbol,
            current_price: String(p.current_price),
            unrealized_pnl: p.unrealized_pnl,
            is_stale: p.is_stale ? 'true' : 'false',
            tick_age_ms: String(p.tick_age_ms),
          })
        }
      } else if (msg.data.type === 'position_event') {
        // ``closed`` removes the position from the live list;
        // ``modified`` / ``pending_filled`` will be reflected via
        // the next REST refresh of the orders table (no immediate
        // store mutation here to keep the contract narrow).
        if (msg.data.event_type === 'closed') {
          store.removePosition(msg.data.order_id)
        }
      }
    }

    const handleOrdersChannel = (msg: WsOrderUpdatedMessage) => {
      const store = useAppStore.getState()
      if (msg.data.type === 'order_updated') {
        store.upsertOrder(msg.data)
        // If the broadcast says the order went to closed, drop it
        // from the live-positions list — the next history REST
        // refresh will pull the close-detail fields.
        if (msg.data.p_status === 'closed' || msg.data.status === 'closed') {
          store.removePosition(msg.data.order_id)
        }
      }
    }

    const connect = () => {
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }

      useAppStore.getState().setWsState('connecting')

      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      // Vite dev proxy forwards /ws to the backend with `ws: true`. In prod we
      // rely on the same-origin reverse proxy (Phase 5).
      const wsUrl = `${proto}://${window.location.host}/ws?token=${encodeURIComponent(token)}`

      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        useAppStore.getState().setWsState('connected')
        reconnectAttemptRef.current = 0

        // Re-send set_symbol on (re)connect so the server resubscribes to
        // spots/trendbars for whatever symbol is currently active.
        const state = useAppStore.getState()
        if (state.selectedSymbol) {
          ws.send(
            JSON.stringify({
              type: 'set_symbol',
              symbol: state.selectedSymbol,
              timeframe: state.selectedTimeframe,
            } satisfies WsClientMessage)
          )
        }
        // Step 3.10: subscribe to the live-positions broadcast
        // channel emitted by step-3.8's position_tracker_loop. The
        // server-side BroadcastService.VALID_CHANNEL_PREFIXES allows
        // exact-match ``"positions"``. We also speculatively
        // subscribe to ``"orders"`` — at the time of writing the
        // server WS handler hadn't whitelisted that channel, so the
        // subscribe will be acknowledged with an error message
        // (logged + ignored). The redundant subscribe is harmless
        // and keeps the client future-proof if step 3.14 opens the
        // channel; for now we rely on the next REST refresh to
        // pick up ``order_updated`` events.
        ws.send(
          JSON.stringify({
            type: 'subscribe',
            channels: ['positions', 'orders'],
          } satisfies WsClientMessage)
        )
      }

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data) as WsServerMessage

          if ('channel' in msg) {
            if (msg.channel.startsWith('ticks:')) {
              const tickMsg = msg as WsTickMessage
              // Drop ticks for stale symbols (server may still be flushing
              // from a previous subscription right after set_symbol).
              if (tickMsg.data.symbol === useAppStore.getState().selectedSymbol) {
                useAppStore.getState().setLatestTick({
                  bid: tickMsg.data.bid,
                  ask: tickMsg.data.ask,
                  ts: tickMsg.data.ts,
                })
              }
            } else if (msg.channel.startsWith('candles:')) {
              candleHandlerRef.current?.(msg as WsCandleMessage)
            } else if (msg.channel === 'positions') {
              handlePositionsChannel(msg as WsPositionsTickMessage | WsPositionEventMessage)
            } else if (msg.channel === 'orders') {
              handleOrdersChannel(msg as WsOrderUpdatedMessage)
            }
            return
          }

          if (msg.type === 'ping') {
            ws.send(JSON.stringify({ type: 'pong' } satisfies WsClientMessage))
          } else if (msg.type === 'error') {
            console.error('WS error from server:', msg.detail)
          }
        } catch (err) {
          console.error('Failed to parse WS message:', err)
        }
      }

      ws.onerror = (event) => {
        console.error('WS error event:', event)
      }

      ws.onclose = (event) => {
        wsRef.current = null
        useAppStore.getState().setWsState('disconnected')
        useAppStore.getState().setLatestTick(null)

        if (explicitCloseRef.current || event.code === 1000) {
          return
        }

        const attempt = reconnectAttemptRef.current
        const idx = Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)
        const delay = Math.min(
          RECONNECT_DELAYS_MS[idx] ?? MAX_RECONNECT_DELAY_MS,
          MAX_RECONNECT_DELAY_MS
        )
        reconnectAttemptRef.current = attempt + 1
        console.log(`WS reconnecting in ${delay}ms (attempt ${attempt + 1})`)
        reconnectTimerRef.current = window.setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      if (wsRef.current) {
        explicitCloseRef.current = true
        wsRef.current.close(1000, 'effect cleanup')
        wsRef.current = null
      }
    }
  }, [token])

  // Auto-send set_symbol on symbol/timeframe change (during a live connection).
  // The onopen handler covers the (re)connect case; this covers in-flight
  // changes while the socket is already OPEN.
  useEffect(() => {
    if (!token || !selectedSymbol) return
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({
          type: 'set_symbol',
          symbol: selectedSymbol,
          timeframe: selectedTimeframe,
        } satisfies WsClientMessage)
      )
      // Clear stale tick from previous symbol so the chart price lines snap
      // away immediately rather than lingering on the old bid/ask.
      useAppStore.getState().setLatestTick(null)
    }
  }, [token, selectedSymbol, selectedTimeframe])

  return { sendMessage, registerCandleHandler }
}
