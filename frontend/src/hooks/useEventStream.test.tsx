import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { StreamHandlers } from '../api/events';
import { EventStreamClosedError, openEventStream } from '../api/events';
import { ApiError } from '../api/errors';
import ConnectionStatus, { POLLING_MESSAGE } from '../components/ConnectionStatus';
import {
  BACKOFF_MS,
  CLEAN_END_RECONNECT_MS,
  POLLING_INTERVAL_MS,
  useEventStream,
} from './useEventStream';

vi.mock('../api/events', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/events')>();
  return { ...actual, openEventStream: vi.fn() };
});

const mockOpen = vi.mocked(openEventStream);

/** One simulated connection: its handlers plus the resolver that ends it. */
interface Connection {
  handlers: StreamHandlers;
  end: () => void;
}

let connections: Connection[];

function latest(): Connection {
  const connection = connections[connections.length - 1];
  if (!connection) throw new Error('no connection was opened');
  return connection;
}

const onReady = vi.fn();
const onEvent = vi.fn();
const onPoll = vi.fn();

function Harness({ enabled = true }: { enabled?: boolean }) {
  const { mode } = useEventStream({ enabled, onReady, onEvent, onPoll });
  return (
    <div>
      <span data-testid="mode">{mode}</span>
      <ConnectionStatus mode={mode} />
    </div>
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  connections = [];
  onReady.mockClear();
  onEvent.mockClear();
  onPoll.mockClear();
  mockOpen.mockReset();
  mockOpen.mockImplementation((handlers: StreamHandlers) => {
    return new Promise<void>((resolve) => {
      connections.push({ handlers, end: resolve });
    });
  });
});

afterEach(() => {
  vi.useRealTimers();
});

/** Lets the microtask queue drain inside `act`. */
async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

/** Ends the current connection and lets the reconnect timer fire. */
async function dropAndReconnect(): Promise<void> {
  await act(async () => {
    latest().end();
    await Promise.resolve();
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(BACKOFF_MS[BACKOFF_MS.length - 1]);
  });
}

describe('useEventStream', () => {
  it('opens one stream and starts in live mode', async () => {
    render(<Harness />);
    await settle();

    expect(mockOpen).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('mode')).toHaveTextContent('live');
    expect(screen.queryByText(POLLING_MESSAGE)).not.toBeInTheDocument();
  });

  it('does not connect while signed out', async () => {
    render(<Harness enabled={false} />);
    await settle();

    expect(mockOpen).not.toHaveBeenCalled();
  });

  it('aborts the stream on unmount', async () => {
    const { unmount } = render(<Harness />);
    await settle();
    const signal = mockOpen.mock.calls[0][1];

    unmount();

    expect(signal.aborted).toBe(true);
  });

  it('forwards ready and events to the caller', async () => {
    render(<Harness />);
    await settle();

    act(() => {
      latest().handlers.onReady();
      latest().handlers.onEvent('todo.created', { origin: null, todo: { id: 't-1' } });
    });

    expect(onReady).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledWith('todo.created', {
      origin: null,
      todo: { id: 't-1' },
    });
  });

  it('reconnects with the documented backoff after a failed connect', async () => {
    render(<Harness />);
    await settle();

    // First failure -> 1 s.
    await act(async () => {
      latest().handlers.onError(new Error('boom'));
      latest().end();
      await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(999);
    });
    expect(mockOpen).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(mockOpen).toHaveBeenCalledTimes(2);

    // Second failure -> 2 s.
    await act(async () => {
      latest().end();
      await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1999);
    });
    expect(mockOpen).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(mockOpen).toHaveBeenCalledTimes(3);
  });

  it('switches to polling after three failed connects and back on ready', async () => {
    render(<Harness />);
    await settle();

    await dropAndReconnect();
    expect(screen.getByTestId('mode')).toHaveTextContent('live');
    await dropAndReconnect();
    expect(screen.getByTestId('mode')).toHaveTextContent('live');
    await dropAndReconnect();

    expect(screen.getByTestId('mode')).toHaveTextContent('polling');
    const banner = screen.getByRole('status');
    expect(banner).toHaveTextContent(POLLING_MESSAGE);
    expect(banner).toHaveAttribute('aria-live', 'polite');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    // Polling refreshes the view every 15 s while the stream keeps retrying.
    expect(onPoll).toHaveBeenCalled();
    onPoll.mockClear();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLLING_INTERVAL_MS);
    });
    expect(onPoll).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLLING_INTERVAL_MS);
    });
    expect(onPoll).toHaveBeenCalledTimes(2);

    // A successful reconnect returns to live and stops the polling timer.
    act(() => {
      latest().handlers.onReady();
    });
    expect(screen.getByTestId('mode')).toHaveTextContent('live');
    expect(onReady).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLLING_INTERVAL_MS * 2);
    });
    expect(onPoll).toHaveBeenCalledTimes(2);
  });

  it('does not count a healthy stream that drops towards the polling fallback', async () => {
    render(<Harness />);
    await settle();

    for (let i = 0; i < 4; i += 1) {
      act(() => {
        latest().handlers.onReady();
      });
      await dropAndReconnect();
    }

    expect(screen.getByTestId('mode')).toHaveTextContent('live');
    expect(onReady).toHaveBeenCalledTimes(4);
  });

  it('waits longer after a clean close so an evicted tab cannot spin', async () => {
    render(<Harness />);
    await settle();

    // A healthy stream the server then closed itself: the broker evicting us
    // because the account is over its per-user stream cap.
    await act(async () => {
      latest().handlers.onReady();
      latest().handlers.onError(new EventStreamClosedError());
      latest().end();
      await Promise.resolve();
    });

    // The ordinary first backoff rung would already have reconnected here.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(BACKOFF_MS[0]);
    });
    expect(mockOpen).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(CLEAN_END_RECONNECT_MS - BACKOFF_MS[0]);
    });
    expect(mockOpen).toHaveBeenCalledTimes(2);
    // It is still a healthy connection, so it never counts towards polling.
    expect(screen.getByTestId('mode')).toHaveTextContent('live');
  });

  it('keeps the fast first retry when a healthy stream dies with an error', async () => {
    render(<Harness />);
    await settle();

    await act(async () => {
      latest().handlers.onReady();
      latest().handlers.onError(new TypeError('network error'));
      latest().end();
      await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(BACKOFF_MS[0]);
    });

    expect(mockOpen).toHaveBeenCalledTimes(2);
  });

  it('stops retrying after a 401 — the session-expiry flow owns it', async () => {
    render(<Harness />);
    await settle();

    await act(async () => {
      latest().handlers.onError(new ApiError(401, 'unauthorized', 'Not authenticated'));
      latest().end();
      await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(mockOpen).toHaveBeenCalledTimes(1);
  });
});
