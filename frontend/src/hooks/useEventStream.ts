import { useEffect, useRef, useState } from 'react';
import { EventStreamClosedError, openEventStream } from '../api/events';
import { ApiError } from '../api/errors';

/**
 * `live` — the SSE stream is up. `polling` — three connects in a row failed, so
 * the view is refreshed on a timer instead (master §7.4). Realtime is an
 * enhancement: neither mode is ever surfaced as an error.
 */
export type ConnectionMode = 'live' | 'polling';

/** Reconnect delays, capped at the last entry; reset by a `ready` frame. */
export const BACKOFF_MS = [1000, 2000, 5000, 10000, 30000];
export const FAILURES_BEFORE_POLLING = 3;
export const POLLING_INTERVAL_MS = 15000;

/**
 * Wait after a stream that had reached `ready` and then closed *cleanly* — the
 * signature of the broker evicting us because the account is over its per-user
 * stream cap. Racing back in at 1 s would let a user with more tabs than the
 * cap settle into a ~1 Hz evict/reconnect loop across their devices, so this
 * rung is deliberately slower than `BACKOFF_MS[0]`. The trade-off: after a
 * legitimate server restart the tab is stale for up to 5 s instead of 1 s,
 * which the `ready` refetch then repairs anyway. Error endings keep the
 * ordinary backoff, because those need a fast first retry.
 */
export const CLEAN_END_RECONNECT_MS = 5000;

export interface UseEventStreamOptions {
  /** Connect only while there is a session; aborts on logout/unmount. */
  enabled: boolean;
  /** A `ready` frame arrived: refetch once to cover the reconnect gap. */
  onReady: () => void;
  onEvent: (name: string, data: unknown) => void;
  /** Called every 15 s while in `polling` mode. */
  onPoll: () => void;
}

export interface EventStreamState {
  mode: ConnectionMode;
}

/**
 * Keeps a single `GET /api/events` stream open for the signed-in user,
 * reconnecting with backoff and degrading to polling after repeated failures.
 */
export function useEventStream({
  enabled,
  onReady,
  onEvent,
  onPoll,
}: UseEventStreamOptions): EventStreamState {
  const [mode, setMode] = useState<ConnectionMode>('live');

  // The callbacks are recreated on most renders; routing them through a ref
  // keeps the connection effect keyed on `enabled` alone, so a re-render can
  // never drop and reopen the stream.
  const handlersRef = useRef({ onReady, onEvent, onPoll });
  handlersRef.current = { onReady, onEvent, onPoll };

  useEffect(() => {
    if (!enabled) {
      setMode('live');
      return;
    }

    let stopped = false;
    /** Consecutive connects that never reached `ready`. */
    let failures = 0;
    let controller: AbortController | null = null;
    let timer: number | null = null;

    function scheduleReconnect(delay: number): void {
      if (stopped) return;
      timer = window.setTimeout(() => {
        timer = null;
        void connect();
      }, delay);
    }

    async function connect(): Promise<void> {
      if (stopped) return;

      controller = new AbortController();
      let ready = false;
      // A 401 means the session is gone, not that the network blipped: the
      // api client has already started the session-expiry flow, so stop.
      let unauthorized = false;
      // The server closed the stream itself, with nothing having gone wrong.
      let closedCleanly = false;

      await openEventStream(
        {
          onReady() {
            ready = true;
            failures = 0;
            setMode('live');
            handlersRef.current.onReady();
          },
          onEvent(name, data) {
            handlersRef.current.onEvent(name, data);
          },
          onError(error) {
            if (error instanceof ApiError && error.status === 401) {
              unauthorized = true;
            }
            closedCleanly = error instanceof EventStreamClosedError;
          },
        },
        controller.signal,
      );

      if (stopped || unauthorized) return;

      // A stream that delivered `ready` and then dropped is a healthy
      // connection that ended, not a failed connect: it must not count
      // towards the polling fallback.
      failures = ready ? 0 : failures + 1;
      if (failures >= FAILURES_BEFORE_POLLING) {
        setMode('polling');
      }

      if (ready && closedCleanly) {
        // Most likely a broker eviction — back off further than the first
        // backoff rung so extra tabs cannot spin (see CLEAN_END_RECONNECT_MS).
        scheduleReconnect(CLEAN_END_RECONNECT_MS);
        return;
      }
      // Otherwise the first retry waits 1 s, whether the stream had been
      // healthy or the connect failed; each further consecutive failure steps
      // along the backoff.
      const index = Math.min(Math.max(failures - 1, 0), BACKOFF_MS.length - 1);
      scheduleReconnect(BACKOFF_MS[index]);
    }

    void connect();

    return () => {
      stopped = true;
      if (timer !== null) {
        window.clearTimeout(timer);
      }
      controller?.abort();
    };
  }, [enabled]);

  useEffect(() => {
    if (!enabled || mode !== 'polling') {
      return;
    }
    // The stream keeps retrying in the background; this only keeps the view
    // fresh meanwhile.
    const id = window.setInterval(() => {
      handlersRef.current.onPoll();
    }, POLLING_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [enabled, mode]);

  return { mode };
}
