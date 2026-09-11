import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createSseParser, isOwnEvent, openEventStream } from './events';
import { setAuthToken, setUnauthorizedHandler } from './client';
import { _resetClientId, getClientId } from './clientId';
import { ApiError } from './errors';

interface Frame {
  name: string;
  data: unknown;
}

function collect(): { frames: Frame[]; push: (chunk: string) => void } {
  const frames: Frame[] = [];
  const parser = createSseParser((name, data) => frames.push({ name, data }));
  return { frames, push: (chunk) => parser.push(chunk) };
}

describe('SSE frame parser', () => {
  it('reads the event name and the JSON payload', () => {
    const { frames, push } = collect();

    push('event: ready\ndata: {"user_id":"u-1"}\n\n');

    expect(frames).toEqual([{ name: 'ready', data: { user_id: 'u-1' } }]);
  });

  it('concatenates multiple data lines with a newline', () => {
    const { frames, push } = collect();

    push('event: todo.created\ndata: {"origin":null,\ndata: "todo":{"id":"t-1"}}\n\n');

    expect(frames).toEqual([
      { name: 'todo.created', data: { origin: null, todo: { id: 't-1' } } },
    ]);
  });

  it('ignores comment keep-alives and unknown fields', () => {
    const { frames, push } = collect();

    push(': keep-alive\n\n');
    push('retry: 5000\nid: 7\nevent: todo.deleted\ndata: {"id":"t-1"}\n\n');
    push(': keep-alive\n\n');

    expect(frames).toEqual([{ name: 'todo.deleted', data: { id: 't-1' } }]);
  });

  it('tolerates CRLF line endings, including a pair split across chunks', () => {
    const { frames, push } = collect();

    push('event: todo.updated\r\ndata: {"id":"t-1"}\r');
    push('\n\r\n');

    expect(frames).toEqual([{ name: 'todo.updated', data: { id: 't-1' } }]);
  });

  it('reassembles a payload split across two chunks', () => {
    const { frames, push } = collect();

    push('event: todo.created\ndata: {"origin":"c-9","to');
    expect(frames).toHaveLength(0);
    push('do":{"id":"t-2"}}\n\n');

    expect(frames).toEqual([
      { name: 'todo.created', data: { origin: 'c-9', todo: { id: 't-2' } } },
    ]);
  });

  it('passes an unknown event name straight through', () => {
    const { frames, push } = collect();

    push('event: something.new\ndata: {"a":1}\n\n');

    expect(frames).toEqual([{ name: 'something.new', data: { a: 1 } }]);
  });

  it('defaults an unnamed frame to "message"', () => {
    const { frames, push } = collect();

    push('data: {"a":1}\n\n');

    expect(frames).toEqual([{ name: 'message', data: { a: 1 } }]);
  });

  it('drops a frame with no data and a frame whose payload is not JSON', () => {
    const { frames, push } = collect();

    push('event: ready\n\n');
    push('event: todo.created\ndata: not json\n\n');
    push('event: todo.created\ndata: {"ok":true}\n\n');

    expect(frames).toEqual([{ name: 'todo.created', data: { ok: true } }]);
  });

  it('handles several frames arriving in one chunk', () => {
    const { frames, push } = collect();

    push('event: a\ndata: 1\n\nevent: b\ndata: 2\n\n');

    expect(frames).toEqual([
      { name: 'a', data: 1 },
      { name: 'b', data: 2 },
    ]);
  });
});

describe('isOwnEvent', () => {
  beforeEach(() => {
    sessionStorage.clear();
    _resetClientId();
  });

  it('is true only for an event carrying our own client id', () => {
    const clientId = getClientId();

    expect(isOwnEvent({ origin: clientId })).toBe(true);
    expect(isOwnEvent({ origin: 'someone-else' })).toBe(false);
    expect(isOwnEvent({ origin: null })).toBe(false);
    expect(isOwnEvent({})).toBe(false);
    expect(isOwnEvent(null)).toBe(false);
  });
});

/** Feeds `chunks` through a real `ReadableStream`, then closes it. */
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
}

describe('openEventStream', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    sessionStorage.clear();
    _resetClientId();
    setAuthToken('stream-token');
    setUnauthorizedHandler(null);
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setAuthToken(null);
    setUnauthorizedHandler(null);
  });

  it('sends the bearer token in a header and never in the URL', async () => {
    fetchMock.mockResolvedValue(
      new Response(streamOf(['event: ready\ndata: {"user_id":"u-1"}\n\n']), {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }),
    );
    const onReady = vi.fn();
    const onEvent = vi.fn();
    const onError = vi.fn();

    await openEventStream(
      { onReady, onEvent, onError },
      new AbortController().signal,
    );

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/events');
    expect(url).not.toContain('token');
    expect(url).not.toContain('?');
    const headers = init.headers as Record<string, string>;
    expect(headers['Authorization']).toBe('Bearer stream-token');
    expect(headers['Accept']).toBe('text/event-stream');
    expect(onReady).toHaveBeenCalledTimes(1);
  });

  it('delivers frames to the handlers and reports the close', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        streamOf([
          'event: ready\ndata: {"user_id":"u-1"}\n\n',
          ': keep-alive\n\n',
          'event: todo.created\ndata: {"origin":null,"todo":{"id":"t-1"}}\n\n',
        ]),
        { status: 200 },
      ),
    );
    const onReady = vi.fn();
    const onEvent = vi.fn();
    const onError = vi.fn();

    await openEventStream(
      { onReady, onEvent, onError },
      new AbortController().signal,
    );

    expect(onReady).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledWith('todo.created', {
      origin: null,
      todo: { id: 't-1' },
    });
    // The server hung up: not an error the user sees, but a reconnect trigger.
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it('routes a 401 into the global session-expiry handler', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Not authenticated', code: 'unauthorized' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const sessionExpired = vi.fn();
    setUnauthorizedHandler(sessionExpired);
    const onError = vi.fn();

    await openEventStream(
      { onReady: vi.fn(), onEvent: vi.fn(), onError },
      new AbortController().signal,
    );

    expect(sessionExpired).toHaveBeenCalledTimes(1);
    const error = onError.mock.calls[0]?.[0];
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
  });

  it('stays silent when the caller aborts', async () => {
    const controller = new AbortController();
    fetchMock.mockImplementation((_url: string, init: RequestInit) => {
      const signal = init.signal as AbortSignal;
      return Promise.reject(
        signal.aborted
          ? new DOMException('Aborted', 'AbortError')
          : new Error('unexpected'),
      );
    });
    controller.abort();
    const onError = vi.fn();

    await openEventStream({ onReady: vi.fn(), onEvent: vi.fn(), onError }, controller.signal);

    expect(onError).not.toHaveBeenCalled();
  });

  it('reports a network failure', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    const onError = vi.fn();

    await openEventStream(
      { onReady: vi.fn(), onEvent: vi.fn(), onError },
      new AbortController().signal,
    );

    expect(onError).toHaveBeenCalledTimes(1);
  });
});

describe('the realtime client never falls back to EventSource', () => {
  it('has no EventSource usage and no token query parameter in its source', async () => {
    const source = await import('./events?raw').then((module) => module.default as string);

    expect(source).not.toContain('new EventSource');
    expect(source).not.toMatch(/[?&]token=/);
  });
});
