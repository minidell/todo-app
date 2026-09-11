import { send } from './client';
import { getClientId } from './clientId';

/**
 * Server-Sent Events client for `GET /api/events` (master §7.1).
 *
 * The native `EventSource` cannot set request headers, which would force the
 * bearer token into the query string — where it would land in every proxy and
 * server log. So the stream is opened with `fetch()` (through the ordinary api
 * client, so it carries `Authorization` and routes a 401 into the global
 * session-expiry handler) and the `ReadableStream` body is parsed by the small
 * reader below. No `EventSource`, no token in a URL, no extra dependency.
 */

/**
 * The server ended the stream in an orderly way — no network or HTTP failure.
 * The usual cause is the broker evicting our subscriber (the per-user stream
 * cap), so the caller can treat it differently from a genuine error.
 */
export class EventStreamClosedError extends Error {
  constructor() {
    super('The event stream closed.');
    this.name = 'EventStreamClosedError';
  }
}

export interface StreamHandlers {
  /** The server's first frame; the caller refetches the current view. */
  onReady(): void;
  /** Any other named frame. `data` is the parsed JSON payload. */
  onEvent(name: string, data: unknown): void;
  /** Abnormal termination. An intentional abort never reports an error. */
  onError(error: unknown): void;
}

/** Payload shape shared by every frame the backend emits (master §7.2). */
interface OriginCarrier {
  origin?: string | null;
}

/**
 * Echo suppression (master §7.2): the mutating tab already applied the change
 * from its own HTTP response, so it drops the event it caused. The broker
 * copies our `X-Client-Id` request header into `origin`.
 */
export function isOwnEvent(data: unknown): boolean {
  const clientId = getClientId();
  if (!clientId || data === null || typeof data !== 'object') {
    return false;
  }
  return (data as OriginCarrier).origin === clientId;
}

type FrameHandler = (name: string, data: unknown) => void;

export interface SseParser {
  push(chunk: string): void;
}

/**
 * Incremental `text/event-stream` parser. Frames are separated by a blank line;
 * within a frame, `event:` names it, every `data:` line contributes a line of
 * the payload, lines starting with `:` are comments (our 25 s keep-alives) and
 * any other field is ignored. `\r\n` and lone `\r` line endings are tolerated,
 * including when a `\r\n` pair is split across two network chunks.
 */
export function createSseParser(onFrame: FrameHandler): SseParser {
  let buffer = '';
  // True when the previous chunk ended on a CR whose LF may still be coming.
  let pendingCr = false;

  function dispatch(block: string): void {
    let name = 'message';
    const dataLines: string[] = [];

    for (const line of block.split('\n')) {
      if (line === '' || line.startsWith(':')) {
        continue;
      }
      const colon = line.indexOf(':');
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? '' : line.slice(colon + 1);
      // A single leading space after the colon is part of the framing.
      if (value.startsWith(' ')) {
        value = value.slice(1);
      }

      if (field === 'event') {
        name = value;
      } else if (field === 'data') {
        dataLines.push(value);
      }
      // `id` and `retry` are unused here; unknown fields are ignored.
    }

    if (dataLines.length === 0) {
      return;
    }

    let data: unknown;
    try {
      data = JSON.parse(dataLines.join('\n'));
    } catch {
      // A frame we cannot parse is dropped rather than killing the stream.
      return;
    }
    onFrame(name, data);
  }

  return {
    push(chunk: string): void {
      let text = chunk;

      if (pendingCr) {
        pendingCr = false;
        // The LF of a CRLF split across chunks belongs to the CR we emitted.
        if (text.startsWith('\n')) {
          text = text.slice(1);
        }
        buffer += '\n';
      }
      if (text.endsWith('\r')) {
        text = text.slice(0, -1);
        pendingCr = true;
      }

      buffer += text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

      let index = buffer.indexOf('\n\n');
      while (index !== -1) {
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        dispatch(block);
        index = buffer.indexOf('\n\n');
      }
    },
  };
}

/**
 * Opens the stream and pumps it until it ends. The promise settles when the
 * connection is over — for **any** reason — so the caller can reconnect from a
 * single place. A close that was not requested via `signal` (network error,
 * HTTP error, or an orderly server close) is reported through `onError` first.
 */
export async function openEventStream(
  handlers: StreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  try {
    const response = await send('/api/events', 'GET', undefined, {
      signal,
      accept: 'text/event-stream',
    });

    if (!response.body) {
      throw new Error('The event stream returned no body.');
    }

    const parser = createSseParser((name, data) => {
      if (name === 'ready') {
        handlers.onReady();
        return;
      }
      handlers.onEvent(name, data);
    });

    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        if (value) {
          parser.push(value);
        }
      }
    } finally {
      // Releasing the lock lets an in-flight abort tear the body down cleanly.
      reader.releaseLock();
    }

    if (!signal.aborted) {
      // The server hung up. That is not an error the user should ever see, but
      // it does mean we are no longer live, so the caller must reconnect.
      handlers.onError(new EventStreamClosedError());
    }
  } catch (error) {
    if (signal.aborted) {
      // We closed it ourselves (logout, unmount, a deliberate reconnect).
      return;
    }
    handlers.onError(error);
  }
}
