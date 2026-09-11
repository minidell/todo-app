import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setAuthToken, setUnauthorizedHandler } from './api/client';
import { TEST_TOKEN, makeList, makeTodo, makeUser, renderApp, seedStoredSession } from './test/helpers';

/**
 * These tests exercise the real api modules against a mocked `fetch`, which is
 * the only way to cover the global 401 -> session-expiry path end to end.
 */

interface Route {
  status: number;
  body: unknown;
}

function jsonResponse({ status, body }: Route): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

let todosResponses: Route[];
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  setAuthToken(null);
  setUnauthorizedHandler(null);

  todosResponses = [{ status: 200, body: [makeTodo()] }];

  fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    if (url === '/api/auth/me') {
      return Promise.resolve(jsonResponse({ status: 200, body: makeUser() }));
    }
    if (url === '/api/lists' && method === 'GET') {
      return Promise.resolve(jsonResponse({ status: 200, body: [makeList()] }));
    }
    if (url.startsWith('/api/todos') && method === 'GET') {
      const next = todosResponses.shift() ?? { status: 200, body: [] };
      return Promise.resolve(jsonResponse(next));
    }
    return Promise.resolve(jsonResponse({ status: 404, body: { detail: 'Not Found' } }));
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setAuthToken(null);
  setUnauthorizedHandler(null);
});

function authHeaderFor(url: string): string | undefined {
  const call = fetchMock.mock.calls.find(([callUrl]) => (callUrl as string).startsWith(url));
  const init = call?.[1] as RequestInit | undefined;
  return (init?.headers as Record<string, string> | undefined)?.['Authorization'];
}

describe('mid-session 401', () => {
  it('signs the user out and shows the session-expiry copy', async () => {
    const user = userEvent.setup();
    seedStoredSession();
    // The second todo fetch (after switching lists) is rejected.
    todosResponses.push({
      status: 401,
      body: { detail: 'Not authenticated', code: 'unauthorized' },
    });

    renderApp();

    await screen.findByRole('heading', { name: 'Todos', level: 1 });
    await screen.findByText('Buy milk');

    // Every authenticated request carries the bearer token.
    expect(authHeaderFor('/api/auth/me')).toBe(`Bearer ${TEST_TOKEN}`);
    expect(authHeaderFor('/api/lists')).toBe(`Bearer ${TEST_TOKEN}`);
    expect(authHeaderFor('/api/todos')).toBe(`Bearer ${TEST_TOKEN}`);

    await user.click(screen.getByRole('button', { name: 'All lists' }));

    expect(await screen.findByRole('heading', { name: 'Sign in', level: 1 })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Your session expired. Please sign in again.',
    );
    await waitFor(() => expect(localStorage.getItem('todo.auth.token')).toBeNull());
    expect(localStorage.getItem('todo.auth.user')).toBeNull();
  });
});
