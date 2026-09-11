import type { SubtaskDraft, Todo, TodoDraft, TodoPage, TodoPatch, TodoQuery } from './types';
import { toApiError } from './errors';
import { getClientId } from './clientId';

/**
 * Bearer token used for authenticated requests. Kept in a module variable and
 * synchronised by AuthContext so we never touch localStorage per request.
 */
let authToken: string | null = null;

export function setAuthToken(token: string | null): void {
  authToken = token;
}

type UnauthorizedHandler = () => void;

let unauthorizedHandler: UnauthorizedHandler | null = null;

/**
 * Registered by AuthContext. Invoked whenever an authenticated request comes
 * back 401 `unauthorized`, i.e. the session is no longer valid. A 401
 * `invalid_credentials` (a failed login) is deliberately not routed here.
 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

interface RequestOptions {
  /** Send `Authorization: Bearer …`. Off for register/login. */
  auth?: boolean;
  /** Aborts the request — used by the SSE stream and the AI 60 s timeout. */
  signal?: AbortSignal;
  /** Overrides `Accept`; the event stream asks for `text/event-stream`. */
  accept?: string;
}

/**
 * Performs the request and turns a non-2xx response into an `ApiError`.
 * Returns the raw `Response` so callers that need headers (`X-Total-Count`)
 * can read them; `request()` is the JSON-parsing wrapper on top.
 */
export async function send(
  url: string,
  method: string = 'GET',
  body?: object,
  options: RequestOptions = {}
): Promise<Response> {
  const { auth = true, signal, accept } = options;

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };

  if (accept) {
    headers['Accept'] = accept;
  }

  if (auth && authToken) {
    headers['Authorization'] = `Bearer ${authToken}`;
  }

  const clientId = getClientId();
  if (clientId && (method === 'POST' || method === 'PATCH' || method === 'DELETE')) {
    headers['X-Client-Id'] = clientId;
  }

  const response = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });

  if (!response.ok) {
    const error = await toApiError(response);
    if (auth && error.status === 401 && error.code === 'unauthorized') {
      unauthorizedHandler?.();
    }
    throw error;
  }

  return response;
}

export async function request<T>(
  url: string,
  method: string = 'GET',
  body?: object,
  options: RequestOptions = {}
): Promise<T> {
  const response = await send(url, method, body, options);

  // 204 No Content responses have no body
  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

/**
 * Serializes a `TodoQuery` into the `GET /api/todos` query string (master
 * §6.3). Documented defaults (`status=all`, `due=any`), empty repeatables and
 * blank strings are omitted so the URL only ever carries active filters.
 */
export function todosQueryString(query: TodoQuery): string {
  const params = new URLSearchParams();

  if (query.list_id) params.set('list_id', query.list_id);
  if (query.status && query.status !== 'all') params.set('status', query.status);
  for (const priority of query.priority ?? []) params.append('priority', priority);
  for (const tag of query.tag ?? []) params.append('tag', tag);
  if (query.due && query.due !== 'any') params.set('due', query.due);
  if (query.due_from) params.set('due_from', query.due_from);
  if (query.due_to) params.set('due_to', query.due_to);
  if (query.today) params.set('today', query.today);
  if (query.q) params.set('q', query.q);
  if (query.sort) params.set('sort', query.sort);
  if (query.order) params.set('order', query.order);
  if (query.limit !== undefined) params.set('limit', String(query.limit));
  if (query.offset) params.set('offset', String(query.offset));

  const serialized = params.toString();
  return serialized ? `?${serialized}` : '';
}

/**
 * `GET /api/todos` — top-level todos only, each with its `subtasks`. The
 * unpaginated match count comes from `X-Total-Count`; when a proxy strips the
 * header we fall back to the number of rows received.
 */
export async function listTodos(query: TodoQuery = {}): Promise<TodoPage> {
  const response = await send(`/api/todos${todosQueryString(query)}`);
  const payload = (await response.json()) as Todo[];
  const todos = Array.isArray(payload) ? payload : [];

  const header = response.headers.get('X-Total-Count');
  const parsed = header === null ? Number.NaN : Number(header);
  const total = Number.isInteger(parsed) && parsed >= 0 ? parsed : todos.length;

  return { todos, total };
}

/** Drops `undefined` keys so an omitted field never reaches `extra="forbid"`. */
function definedFields<T extends object>(input: T): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(input).filter(([, value]) => value !== undefined)
  );
}

export async function createTodo(draft: TodoDraft): Promise<Todo> {
  return request<Todo>('/api/todos', 'POST', definedFields(draft));
}

/** `PATCH /api/todos/{id}` — partial from slice 3; never send `{}` (400). */
export async function updateTodo(id: string, patch: TodoPatch): Promise<Todo> {
  return request<Todo>(
    `/api/todos/${encodeURIComponent(id)}`,
    'PATCH',
    definedFields(patch)
  );
}

export async function setCompleted(id: string, completed: boolean): Promise<Todo> {
  return updateTodo(id, { completed });
}

/** `POST /api/todos/{id}/subtasks` — one level deep only (400 otherwise). */
export async function createSubtask(
  parentId: string,
  draft: SubtaskDraft
): Promise<Todo> {
  return request<Todo>(
    `/api/todos/${encodeURIComponent(parentId)}/subtasks`,
    'POST',
    definedFields(draft)
  );
}

export async function deleteTodo(id: string): Promise<void> {
  return request<void>(`/api/todos/${encodeURIComponent(id)}`, 'DELETE');
}
