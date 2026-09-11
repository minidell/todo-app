import { render } from '@testing-library/react';
import type { RenderResult } from '@testing-library/react';
import App from '../App';
import { AuthProvider } from '../auth/AuthContext';
import type { ListSummary, TagSummary, Todo, TodoPage, TokenResponse, User } from '../api/types';

export const TEST_TOKEN = 'test-token';

export function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 'user-1',
    email: 'me@example.test',
    display_name: null,
    created_at: '2026-09-02T11:22:33.123456Z',
    ...overrides,
  };
}

export function makeToken(overrides: Partial<TokenResponse> = {}): TokenResponse {
  return {
    access_token: TEST_TOKEN,
    token_type: 'bearer',
    expires_in: 3600,
    user: makeUser(),
    ...overrides,
  };
}

export function makeList(overrides: Partial<ListSummary> = {}): ListSummary {
  return {
    id: 'list-1',
    name: 'Inbox',
    is_default: true,
    todo_count: 0,
    active_count: 0,
    created_at: '2026-09-02T11:22:33.123456Z',
    updated_at: '2026-09-02T11:22:33.123456Z',
    ...overrides,
  };
}

export function makeTodo(overrides: Partial<Todo> = {}): Todo {
  return {
    id: 'id-1',
    title: 'Buy milk',
    completed: false,
    created_at: '2026-09-02T11:22:33.123456Z',
    updated_at: '2026-09-02T11:22:33.123456Z',
    list_id: 'list-1',
    parent_id: null,
    description: null,
    completed_at: null,
    priority: 'medium',
    due_date: null,
    tags: [],
    subtasks: [],
    ...overrides,
  };
}

/** A `listTodos` result: the rows plus the `X-Total-Count` match count. */
export function makeTodoPage(todos: Todo[] = [], total: number = todos.length): TodoPage {
  return { todos, total };
}

export function makeTag(overrides: Partial<TagSummary> = {}): TagSummary {
  return { id: 'tag-1', name: 'home', todo_count: 1, ...overrides };
}

/** Puts a token in storage so the app boots straight into session restore. */
export function seedStoredSession(token: string = TEST_TOKEN, user: User = makeUser()): void {
  localStorage.setItem('todo.auth.token', token);
  localStorage.setItem('todo.auth.user', JSON.stringify(user));
}

export function renderApp(): RenderResult {
  return render(
    <AuthProvider>
      <App />
    </AuthProvider>,
  );
}
