import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { TodoPage } from './api/types';
import { createTodo, listTodos } from './api/client';
import { listLists } from './api/lists';
import { listTags } from './api/tags';
import { me } from './api/auth';
import { todayString } from './dates';
import { SEARCH_DEBOUNCE_MS } from './components/SearchBox';
import {
  makeList,
  makeTag,
  makeTodo,
  makeTodoPage,
  makeUser,
  renderApp,
  seedStoredSession,
} from './test/helpers';

vi.mock('./api/client');
vi.mock('./api/lists');
vi.mock('./api/tags');
vi.mock('./api/auth');

const mockListTodos = vi.mocked(listTodos);
const mockListLists = vi.mocked(listLists);
const mockListTags = vi.mocked(listTags);
const mockMe = vi.mocked(me);

const TODAY = todayString();

/** The query the app sends with no filters applied. */
const BASE_QUERY = {
  list_id: 'list-1',
  status: 'all',
  priority: [],
  tag: [],
  due: 'any',
  q: '',
  today: TODAY,
  sort: 'created_at',
  order: 'asc',
};

function lastQuery(): Record<string, unknown> {
  return (mockListTodos.mock.calls.at(-1)?.[0] ?? {}) as Record<string, unknown>;
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  seedStoredSession();
  mockMe.mockResolvedValue(makeUser());
  mockListLists.mockResolvedValue([makeList()]);
  mockListTags.mockResolvedValue([
    makeTag({ id: 'tag-1', name: 'home', todo_count: 2 }),
    makeTag({ id: 'tag-2', name: 'work', todo_count: 1 }),
  ]);
  mockListTodos.mockResolvedValue(makeTodoPage([makeTodo()]));
});

async function renderSignedIn(): Promise<void> {
  renderApp();
  await screen.findByRole('heading', { name: 'Todos', level: 1 });
  await waitFor(() => expect(mockListTodos).toHaveBeenCalled());
}

describe('filter bar', () => {
  it('sends the documented defaults, including the browser-local today', async () => {
    await renderSignedIn();
    expect(lastQuery()).toEqual(BASE_QUERY);
    expect(screen.queryByRole('button', { name: 'Clear filters' })).not.toBeInTheDocument();
  });

  it('sends the chosen status', async () => {
    const user = userEvent.setup();
    await renderSignedIn();

    await user.selectOptions(screen.getByLabelText('Status'), 'active');

    await waitFor(() => expect(lastQuery()).toEqual({ ...BASE_QUERY, status: 'active' }));
  });

  it('sends every ticked priority and clears them with Any priority', async () => {
    const user = userEvent.setup();
    await renderSignedIn();

    await user.click(screen.getByRole('checkbox', { name: 'High' }));
    await waitFor(() => expect(lastQuery()).toEqual({ ...BASE_QUERY, priority: ['high'] }));

    await user.click(screen.getByRole('checkbox', { name: 'Medium' }));
    await waitFor(() =>
      expect(lastQuery()).toEqual({ ...BASE_QUERY, priority: ['high', 'medium'] }),
    );

    await user.click(screen.getByRole('checkbox', { name: 'Any priority' }));
    await waitFor(() => expect(lastQuery()).toEqual(BASE_QUERY));
  });

  it('sends the chosen due preset', async () => {
    const user = userEvent.setup();
    await renderSignedIn();

    await user.selectOptions(screen.getByLabelText('Due'), 'week');
    await waitFor(() => expect(lastQuery()).toEqual({ ...BASE_QUERY, due: 'week' }));

    await user.selectOptions(screen.getByLabelText('Due'), 'overdue');
    await waitFor(() => expect(lastQuery()).toEqual({ ...BASE_QUERY, due: 'overdue' }));
  });

  it('toggles tag chips with aria-pressed and AND semantics', async () => {
    const user = userEvent.setup();
    await renderSignedIn();

    const home = await screen.findByRole('button', { name: 'Filter by tag home' });
    expect(home).toHaveAttribute('aria-pressed', 'false');

    await user.click(home);
    await waitFor(() => expect(lastQuery()).toEqual({ ...BASE_QUERY, tag: ['home'] }));
    expect(screen.getByRole('button', { name: 'Filter by tag home' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    await user.click(screen.getByRole('button', { name: 'Filter by tag work' }));
    await waitFor(() => expect(lastQuery()).toEqual({ ...BASE_QUERY, tag: ['home', 'work'] }));

    await user.click(screen.getByRole('button', { name: 'Filter by tag home' }));
    await waitFor(() => expect(lastQuery()).toEqual({ ...BASE_QUERY, tag: ['work'] }));
  });

  it('changes the sort field and toggles the direction', async () => {
    const user = userEvent.setup();
    await renderSignedIn();

    await user.selectOptions(screen.getByLabelText('Sort by'), 'due_date');
    await waitFor(() => expect(lastQuery()).toEqual({ ...BASE_QUERY, sort: 'due_date' }));

    const direction = screen.getByRole('button', { name: 'Sort ascending' });
    expect(direction).toHaveAttribute('aria-pressed', 'true');

    await user.click(direction);
    await waitFor(() =>
      expect(lastQuery()).toEqual({ ...BASE_QUERY, sort: 'due_date', order: 'desc' }),
    );
    const flipped = screen.getByRole('button', { name: 'Sort descending' });
    expect(flipped).toHaveAttribute('aria-pressed', 'false');

    await user.click(flipped);
    await waitFor(() =>
      expect(lastQuery()).toEqual({ ...BASE_QUERY, sort: 'due_date', order: 'asc' }),
    );
  });

  it('resets the filters with Clear filters but keeps the sort', async () => {
    const user = userEvent.setup();
    await renderSignedIn();

    await user.selectOptions(screen.getByLabelText('Status'), 'completed');
    await user.selectOptions(screen.getByLabelText('Sort by'), 'title');
    await user.click(screen.getByRole('checkbox', { name: 'Low' }));
    await user.type(screen.getByLabelText('Search todos'), 'milk');
    await waitFor(() => expect(lastQuery()).toMatchObject({ q: 'milk' }));

    const clear = await screen.findByRole('button', { name: 'Clear filters' });
    await user.click(clear);

    await waitFor(() => expect(lastQuery()).toEqual({ ...BASE_QUERY, sort: 'title' }));
    expect(screen.queryByRole('button', { name: 'Clear filters' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Status')).toHaveValue('all');
    // The search field follows the reset instead of showing a dead term.
    expect(screen.getByLabelText('Search todos')).toHaveValue('');
  });
});

describe('search', () => {
  // Real timers: React Testing Library cannot drive Vitest's fake clock, so the
  // debounce is exercised against the real 300 ms window.
  it('issues a single request for a burst of keystrokes and clears it', async () => {
    const user = userEvent.setup();
    await renderSignedIn();
    const before = mockListTodos.mock.calls.length;

    await user.type(screen.getByLabelText('Search todos'), 'milk');

    // Four keystrokes collapse into one request…
    await waitFor(() => expect(mockListTodos).toHaveBeenCalledTimes(before + 1));
    expect(lastQuery()).toEqual({ ...BASE_QUERY, q: 'milk' });

    // …and nothing else follows once the window has fully elapsed.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, SEARCH_DEBOUNCE_MS + 100));
    });
    expect(mockListTodos).toHaveBeenCalledTimes(before + 1);

    await user.click(screen.getByRole('button', { name: 'Clear search' }));
    await waitFor(() => expect(lastQuery()).toEqual(BASE_QUERY));
    expect(screen.getByLabelText('Search todos')).toHaveValue('');
    expect(screen.getByLabelText('Search todos')).toHaveFocus();
  });
});

describe('result summary', () => {
  it('reports the shown rows against the X-Total-Count total', async () => {
    mockListTodos.mockResolvedValue(
      makeTodoPage([makeTodo({ id: 'id-1' }), makeTodo({ id: 'id-2', title: 'Walk the dog' })], 5),
    );

    await renderSignedIn();

    expect(await screen.findByText('Showing 2 of 5 todos')).toBeInTheDocument();
  });

  it('announces the no-match copy politely when filters exclude everything', async () => {
    const user = userEvent.setup();
    await renderSignedIn();
    await screen.findByText('Showing 1 of 1 todos');

    mockListTodos.mockResolvedValue(makeTodoPage([], 0));
    await user.selectOptions(screen.getByLabelText('Status'), 'completed');

    const summary = await screen.findByText('No todos match your filters.');
    expect(summary).toHaveAttribute('aria-live', 'polite');
    // The genuinely-empty copy is reserved for an unfiltered empty list.
    expect(
      screen.queryByText('No todos yet. Add your first one above.'),
    ).not.toBeInTheDocument();
  });

  it('keeps the empty-list copy and no summary when nothing is filtered', async () => {
    mockListTodos.mockResolvedValue(makeTodoPage([], 0));

    await renderSignedIn();

    expect(await screen.findByText('No todos yet. Add your first one above.')).toBeInTheDocument();
    expect(screen.queryByText(/^Showing /)).not.toBeInTheDocument();
    expect(screen.queryByText('No todos match your filters.')).not.toBeInTheDocument();
  });
});

describe('creating a todo while a filter is active', () => {
  it('refetches the current query instead of showing a row that does not match', async () => {
    const user = userEvent.setup();
    const mockCreateTodo = vi.mocked(createTodo);
    await renderSignedIn();

    mockListTodos.mockResolvedValue(makeTodoPage([], 0));
    await user.selectOptions(screen.getByLabelText('Status'), 'completed');
    await screen.findByText('No todos match your filters.');
    const before = mockListTodos.mock.calls.length;

    mockCreateTodo.mockResolvedValue(makeTodo({ id: 'new-id', title: 'New task' }));
    await user.type(screen.getByLabelText('New todo title'), 'New task{Enter}');

    await waitFor(() => expect(mockCreateTodo).toHaveBeenCalled());
    // The freshly created todo is active, so the Completed view must stay empty.
    await waitFor(() => expect(mockListTodos).toHaveBeenCalledTimes(before + 1));
    expect(lastQuery()).toEqual({ ...BASE_QUERY, status: 'completed' });
    expect(screen.queryByText('New task')).not.toBeInTheDocument();
    expect(screen.getByText('No todos match your filters.')).toBeInTheDocument();
  });

  it('refetches when only the sort differs from the default', async () => {
    const user = userEvent.setup();
    const mockCreateTodo = vi.mocked(createTodo);
    await renderSignedIn();

    await user.selectOptions(screen.getByLabelText('Sort by'), 'title');
    await waitFor(() => expect(lastQuery()).toMatchObject({ sort: 'title' }));
    const before = mockListTodos.mock.calls.length;

    mockCreateTodo.mockResolvedValue(makeTodo({ id: 'new-id', title: 'Aardvark' }));
    mockListTodos.mockResolvedValue(
      makeTodoPage([makeTodo({ id: 'new-id', title: 'Aardvark' }), makeTodo()], 2),
    );
    await user.type(screen.getByLabelText('New todo title'), 'Aardvark{Enter}');

    // The server decides the position under a title sort, not the client.
    await waitFor(() => expect(mockListTodos).toHaveBeenCalledTimes(before + 1));
    expect(await screen.findByText('Showing 2 of 2 todos')).toBeInTheDocument();
  });

  it('appends without a refetch when nothing is filtered or resorted', async () => {
    const user = userEvent.setup();
    const mockCreateTodo = vi.mocked(createTodo);
    await renderSignedIn();
    const before = mockListTodos.mock.calls.length;

    mockCreateTodo.mockResolvedValue(makeTodo({ id: 'new-id', title: 'New task' }));
    await user.type(screen.getByLabelText('New todo title'), 'New task{Enter}');

    expect(await screen.findByText('New task')).toBeInTheDocument();
    expect(screen.getByText('Showing 2 of 2 todos')).toBeInTheDocument();
    expect(mockListTodos).toHaveBeenCalledTimes(before);
  });
});

describe('overlapping requests', () => {
  it('ignores a stale response that lands after a newer one', async () => {
    const user = userEvent.setup();
    let resolveFirst!: (page: TodoPage) => void;
    let resolveSecond!: (page: TodoPage) => void;

    mockListTodos
      .mockReturnValueOnce(
        new Promise<TodoPage>((resolve) => {
          resolveFirst = resolve;
        }),
      )
      .mockReturnValueOnce(
        new Promise<TodoPage>((resolve) => {
          resolveSecond = resolve;
        }),
      );

    renderApp();
    await screen.findByRole('heading', { name: 'Todos', level: 1 });
    await waitFor(() => expect(mockListTodos).toHaveBeenCalledTimes(1));

    await user.selectOptions(screen.getByLabelText('Status'), 'active');
    await waitFor(() => expect(mockListTodos).toHaveBeenCalledTimes(2));

    // The newer request answers first…
    await act(async () => {
      resolveSecond(makeTodoPage([makeTodo({ id: 'id-2', title: 'Newer result' })], 1));
    });
    expect(await screen.findByText('Newer result')).toBeInTheDocument();

    // …and the slow first response must not resurrect the old page.
    await act(async () => {
      resolveFirst(makeTodoPage([makeTodo({ id: 'id-1', title: 'Stale result' })], 9));
    });

    expect(screen.queryByText('Stale result')).not.toBeInTheDocument();
    expect(screen.getByText('Newer result')).toBeInTheDocument();
    expect(screen.getByText('Showing 1 of 1 todos')).toBeInTheDocument();
  });
});
