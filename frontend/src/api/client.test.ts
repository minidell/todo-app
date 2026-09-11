import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { getClientId, _resetClientId } from './clientId';
import {
  createSubtask,
  createTodo,
  deleteTodo,
  listTodos,
  setAuthToken,
  setCompleted,
  setUnauthorizedHandler,
  todosQueryString,
  updateTodo,
} from './client';
import { login, me, register } from './auth';
import { createList, deleteList, listLists, renameList } from './lists';
import { listTags } from './tags';
import { ApiError } from './errors';

describe('client ID generation', () => {
  beforeEach(() => {
    // Reset both sessionStorage and module-level cache between tests
    sessionStorage.clear();
    _resetClientId();
  });

  it('generates a client ID when crypto.randomUUID is available', () => {
    // First call should generate a new ID
    const id1 = getClientId();
    expect(id1).toBeTruthy();
    expect(typeof id1).toBe('string');
    expect(id1).toMatch(/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i);

    // Second call should return cached ID (same instance)
    const id2 = getClientId();
    expect(id1).toBe(id2);
  });

  it('persists client ID to sessionStorage on first call', () => {
    const id = getClientId();

    // Should be stored in sessionStorage
    const stored = sessionStorage.getItem('todo.client-id');
    expect(stored).toBe(id);
  });

  it('retrieves client ID from sessionStorage if available', () => {
    const testId = '12345678-1234-5678-1234-567812345678';
    sessionStorage.setItem('todo.client-id', testId);

    const id = getClientId();
    expect(id).toBe(testId);
  });
});

type FetchMock = ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function headersOf(fetchMock: FetchMock, call = 0): Record<string, string> {
  const init = fetchMock.mock.calls[call]?.[1] as RequestInit | undefined;
  return (init?.headers ?? {}) as Record<string, string>;
}

function urlOf(fetchMock: FetchMock, call = 0): string {
  return fetchMock.mock.calls[call]?.[0] as string;
}

function bodyOf(fetchMock: FetchMock, call = 0): unknown {
  const init = fetchMock.mock.calls[call]?.[1] as RequestInit | undefined;
  return init?.body ? JSON.parse(init.body as string) : undefined;
}

describe('request layer', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    fetchMock = vi.fn(() => Promise.resolve(jsonResponse([])));
    vi.stubGlobal('fetch', fetchMock);
    setAuthToken(null);
    setUnauthorizedHandler(null);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setAuthToken(null);
    setUnauthorizedHandler(null);
  });

  it('sends Authorization: Bearer on every authenticated request', async () => {
    setAuthToken('token-abc');

    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse([])));
    await listTodos();
    await listLists();
    await me();

    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ id: 'x' }, 201)));
    await createTodo({ title: 'Buy milk' });
    await createList('Work');

    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ id: 'x' })));
    await setCompleted('todo-1', true);
    await renameList('list-1', 'Work stuff');

    fetchMock.mockImplementation(() => Promise.resolve(new Response(null, { status: 204 })));
    await deleteTodo('todo-1');
    await deleteList('list-1');

    expect(fetchMock).toHaveBeenCalledTimes(9);
    for (let call = 0; call < 9; call++) {
      expect(headersOf(fetchMock, call)['Authorization']).toBe('Bearer token-abc');
    }
  });

  it('never sends Authorization on register or login', async () => {
    setAuthToken('token-abc');
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ id: 'u1' }, 201)));

    await register('me@example.test', 'password123', 'Me');
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ access_token: 't' })));
    await login('me@example.test', 'password123');

    expect(headersOf(fetchMock, 0)['Authorization']).toBeUndefined();
    expect(headersOf(fetchMock, 1)['Authorization']).toBeUndefined();
  });

  it('omits Authorization when no token is set', async () => {
    await listTodos();
    expect(headersOf(fetchMock)['Authorization']).toBeUndefined();
  });

  it('omits display_name from the register body when it is blank', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ id: 'u1' }, 201)));

    await register('me@example.test', 'password123', '   ');
    expect(bodyOf(fetchMock)).toEqual({
      email: 'me@example.test',
      password: 'password123',
    });

    await register('me@example.test', 'password123', ' Me ');
    expect(bodyOf(fetchMock, 1)).toEqual({
      email: 'me@example.test',
      password: 'password123',
      display_name: 'Me',
    });
  });

  it('scopes listTodos and createTodo by list_id when one is given', async () => {
    await listTodos({ list_id: 'list-1' });
    expect(urlOf(fetchMock)).toBe('/api/todos?list_id=list-1');

    await listTodos();
    expect(urlOf(fetchMock, 1)).toBe('/api/todos');

    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ id: 'x' }, 201)));
    await createTodo({ title: 'Buy milk', list_id: 'list-1' });
    expect(bodyOf(fetchMock, 2)).toEqual({ title: 'Buy milk', list_id: 'list-1' });

    await createTodo({ title: 'Buy milk' });
    expect(bodyOf(fetchMock, 3)).toEqual({ title: 'Buy milk' });
  });

  it('serializes every todo filter, dropping defaults and empty repeatables', () => {
    expect(todosQueryString({})).toBe('');
    expect(todosQueryString({ status: 'all', due: 'any', priority: [], tag: [], q: '' })).toBe('');

    expect(
      todosQueryString({
        list_id: 'list-1',
        status: 'active',
        priority: ['high', 'medium'],
        tag: ['home', 'week end'],
        due: 'week',
        today: '2026-09-02',
        q: '100%',
        sort: 'due_date',
        order: 'desc',
      }),
    ).toBe(
      '?list_id=list-1&status=active&priority=high&priority=medium' +
        '&tag=home&tag=week+end&due=week&today=2026-09-02&q=100%25' +
        '&sort=due_date&order=desc',
    );

    expect(todosQueryString({ due_from: '2026-09-01', due_to: '2026-09-30' })).toBe(
      '?due_from=2026-09-01&due_to=2026-09-30',
    );
    expect(todosQueryString({ limit: 50, offset: 100 })).toBe('?limit=50&offset=100');
  });

  it('reads the unpaginated total from X-Total-Count and falls back to the row count', async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify([{ id: 'a' }, { id: 'b' }]), {
          status: 200,
          headers: { 'Content-Type': 'application/json', 'X-Total-Count': '17' },
        }),
      ),
    );
    await expect(listTodos()).resolves.toMatchObject({ total: 17 });

    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse([{ id: 'a' }, { id: 'b' }])));
    const page = await listTodos();
    expect(page.total).toBe(2);
    expect(page.todos).toHaveLength(2);

    // A garbage header must not produce NaN in the summary copy.
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json', 'X-Total-Count': 'lots' },
        }),
      ),
    );
    await expect(listTodos()).resolves.toEqual({ todos: [], total: 0 });
  });

  it('sends only the fields present in a partial PATCH', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ id: 'x' })));

    await updateTodo('todo-1', { title: 'New title', due_date: null, tags: [] });
    expect(urlOf(fetchMock)).toBe('/api/todos/todo-1');
    expect(bodyOf(fetchMock)).toEqual({ title: 'New title', due_date: null, tags: [] });

    // `undefined` means "do not touch" and must never reach the wire.
    await updateTodo('todo-1', { description: undefined, priority: 'low' });
    expect(bodyOf(fetchMock, 1)).toEqual({ priority: 'low' });

    await setCompleted('todo-1', true);
    expect(bodyOf(fetchMock, 2)).toEqual({ completed: true });
  });

  it('posts a subtask to the parent subtask route', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ id: 'sub' }, 201)));

    await createSubtask('todo-1', { title: 'Step one' });

    expect(urlOf(fetchMock)).toBe('/api/todos/todo-1/subtasks');
    expect(bodyOf(fetchMock)).toEqual({ title: 'Step one' });
  });

  it('lists tags from /api/tags', async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(jsonResponse([{ id: 't1', name: 'home', todo_count: 2 }])),
    );

    await expect(listTags()).resolves.toEqual([{ id: 't1', name: 'home', todo_count: 2 }]);
    expect(urlOf(fetchMock)).toBe('/api/tags');
  });

  it('calls the unauthorized handler on a 401 unauthorized and rethrows', async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    setAuthToken('token-abc');
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: 'Not authenticated', code: 'unauthorized' }, 401),
    );

    await expect(listTodos()).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('does not treat a failed login (401 invalid_credentials) as a session expiry', async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: 'Invalid email or password', code: 'invalid_credentials' }, 401),
    );

    await expect(login('me@example.test', 'nope')).rejects.toMatchObject({
      status: 401,
      code: 'invalid_credentials',
    });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});

describe('client ID guard for non-secure contexts', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns null when crypto.randomUUID is undefined', () => {
    sessionStorage.clear();
    _resetClientId();

    // Stub crypto to undefined to simulate non-secure context (plain HTTP)
    vi.stubGlobal('crypto', undefined);

    const id = getClientId();
    expect(id).toBeNull();
  });

  it('sends mutation requests without X-Client-Id header when crypto is unavailable', async () => {
    sessionStorage.clear();
    _resetClientId();

    // Stub crypto to undefined
    vi.stubGlobal('crypto', undefined);

    // Mock fetch to capture the request
    const mockFetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 'test-id', title: 'Test', completed: false, created_at: '2026-09-02T00:00:00Z', updated_at: '2026-09-02T00:00:00Z', list_id: 'list-1', parent_id: null, description: null, completed_at: null, priority: 'medium', due_date: null, tags: [], subtasks: [] }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    vi.stubGlobal('fetch', mockFetch);

    // Make a mutation request
    try {
      await createTodo({ title: 'Test todo' });
    } catch {
      // May fail due to stubbed crypto, but we just want to verify fetch was called
    }

    // Verify fetch was called
    expect(mockFetch).toHaveBeenCalled();

    // Verify X-Client-Id header was NOT included
    const [, options] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(options.headers).toBeDefined();
    const headers = options.headers as Record<string, string>;
    expect(headers['X-Client-Id']).toBeUndefined();

    // But Content-Type should still be set
    expect(headers['Content-Type']).toBe('application/json');
  });
});
