import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { StreamHandlers } from './api/events';
import { openEventStream } from './api/events';
import { listTodos } from './api/client';
import { deleteList, listLists } from './api/lists';
import { listTags } from './api/tags';
import { me } from './api/auth';
import { _resetClientId, getClientId } from './api/clientId';
import { POLLING_MESSAGE } from './components/ConnectionStatus';
import { POLLING_INTERVAL_MS } from './hooks/useEventStream';
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
vi.mock('./api/events', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api/events')>();
  return { ...actual, openEventStream: vi.fn() };
});

const mockListTodos = vi.mocked(listTodos);
const mockListLists = vi.mocked(listLists);
const mockDeleteList = vi.mocked(deleteList);
const mockListTags = vi.mocked(listTags);
const mockMe = vi.mocked(me);
const mockOpen = vi.mocked(openEventStream);

interface Connection {
  handlers: StreamHandlers;
  end: () => void;
}

let connections: Connection[];

function stream(): Connection {
  const connection = connections[connections.length - 1];
  if (!connection) throw new Error('the app never opened an event stream');
  return connection;
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  _resetClientId();
  connections = [];

  mockOpen.mockImplementation(
    (handlers: StreamHandlers) =>
      new Promise<void>((resolve) => {
        connections.push({ handlers, end: resolve });
      }),
  );

  seedStoredSession();
  mockMe.mockResolvedValue(makeUser());
  mockListLists.mockResolvedValue([makeList()]);
  mockListTags.mockResolvedValue([]);
  mockListTodos.mockResolvedValue(makeTodoPage([makeTodo({ id: 'id-1', title: 'Buy milk' })]));
});

afterEach(() => {
  vi.useRealTimers();
});

async function renderSignedIn(): Promise<void> {
  renderApp();
  await screen.findByRole('heading', { name: 'Todos', level: 1 });
  await screen.findByText('Buy milk');
  await waitFor(() => expect(mockOpen).toHaveBeenCalled());
}

/** Delivers a frame from another client and waits for the debounced refetch. */
async function deliver(name: string, data: unknown): Promise<void> {
  act(() => {
    stream().handlers.onEvent(name, data);
  });
}

describe('realtime updates', () => {
  it('connects once the session is live and stays visually quiet', async () => {
    await renderSignedIn();

    expect(mockOpen).toHaveBeenCalledTimes(1);
    // Live is announced only to assistive tech; nothing shouts at the user.
    expect(screen.getByRole('status')).toHaveTextContent('Live updates on');
    expect(screen.getByRole('status')).toHaveClass('sr-only');
    expect(screen.queryByText(POLLING_MESSAGE)).not.toBeInTheDocument();
  });

  it('shows a todo another tab created', async () => {
    await renderSignedIn();
    mockListTodos.mockResolvedValue(
      makeTodoPage([
        makeTodo({ id: 'id-1', title: 'Buy milk' }),
        makeTodo({ id: 'id-2', title: 'Walk the dog' }),
      ]),
    );

    await deliver('todo.created', {
      origin: 'another-tab',
      todo: makeTodo({ id: 'id-2', title: 'Walk the dog' }),
    });

    expect(await screen.findByText('Walk the dog')).toBeInTheDocument();
  });

  it('reflects an update from another tab', async () => {
    await renderSignedIn();
    mockListTodos.mockResolvedValue(
      makeTodoPage([makeTodo({ id: 'id-1', title: 'Buy oat milk' })]),
    );

    await deliver('todo.updated', {
      origin: null,
      todo: makeTodo({ id: 'id-1', title: 'Buy oat milk' }),
    });

    expect(await screen.findByText('Buy oat milk')).toBeInTheDocument();
    expect(screen.queryByText('Buy milk')).not.toBeInTheDocument();
  });

  it('removes a row another tab deleted', async () => {
    await renderSignedIn();
    mockListTodos.mockResolvedValue(makeTodoPage([]));

    await deliver('todo.deleted', {
      origin: 'another-tab',
      id: 'id-1',
      list_id: 'list-1',
    });

    await waitFor(() => expect(screen.queryByText('Buy milk')).not.toBeInTheDocument());
  });

  it('ignores the echo of our own change', async () => {
    await renderSignedIn();
    const callsBefore = mockListTodos.mock.calls.length;
    mockListTodos.mockResolvedValue(makeTodoPage([]));

    await deliver('todo.deleted', {
      origin: getClientId(),
      id: 'id-1',
      list_id: 'list-1',
    });
    // Give the debounce window a chance to fire if it were scheduled at all.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });

    expect(mockListTodos).toHaveBeenCalledTimes(callsBefore);
    expect(screen.getByText('Buy milk')).toBeInTheDocument();
  });

  it('coalesces a burst of events into a single refetch', async () => {
    await renderSignedIn();
    const callsBefore = mockListTodos.mock.calls.length;

    await deliver('todo.deleted', { origin: null, id: 'a', list_id: 'list-1' });
    await deliver('todo.deleted', { origin: null, id: 'b', list_id: 'list-1' });
    await deliver('todo.deleted', { origin: null, id: 'c', list_id: 'list-1' });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });

    expect(mockListTodos).toHaveBeenCalledTimes(callsBefore + 1);
  });

  it('refetches exactly once when a reconnect delivers ready', async () => {
    await renderSignedIn();
    const callsBefore = mockListTodos.mock.calls.length;

    act(() => {
      stream().handlers.onReady();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });

    expect(mockListTodos).toHaveBeenCalledTimes(callsBefore + 1);
  });

  it('falls back to 15-second polling after three failed connects', async () => {
    // `waitFor` cannot be used here: it would need the timers we are faking.
    vi.useFakeTimers();
    renderApp();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(screen.getByRole('heading', { name: 'Todos', level: 1 })).toBeInTheDocument();
    expect(mockOpen).toHaveBeenCalled();

    for (let attempt = 0; attempt < 3; attempt += 1) {
      await act(async () => {
        stream().end();
        await Promise.resolve();
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
    }

    expect(screen.getByText(POLLING_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    const callsBefore = mockListTodos.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLLING_INTERVAL_MS);
    });
    expect(mockListTodos.mock.calls.length).toBeGreaterThan(callsBefore);
  });

  it('switches away from a list another tab deleted', async () => {
    mockListLists.mockResolvedValue([
      makeList({ id: 'list-1', name: 'Inbox', is_default: true }),
      makeList({ id: 'list-2', name: 'Work', is_default: false }),
    ]);
    localStorage.setItem('todo.selected-list', 'list-2');
    await renderSignedIn();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Work (0 active)' })).toHaveAttribute(
        'aria-current',
        'true',
      ),
    );
    mockListLists.mockResolvedValue([makeList({ id: 'list-1', name: 'Inbox', is_default: true })]);

    await deliver('list.deleted', { origin: 'another-tab', id: 'list-2' });

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Inbox (0 active)' })).toHaveAttribute(
        'aria-current',
        'true',
      ),
    );
  });

  /**
   * Deleting a list hands focus to whatever ends up selected — once. That
   * hand-off must not turn into a standing claim on focus: a later selection
   * change the user did not ask for (here: another tab deleting the open list)
   * has to leave the caret where it is.
   */
  it('never steals focus again after the post-delete hand-off', async () => {
    const user = userEvent.setup();
    mockListLists.mockResolvedValue([
      makeList({ id: 'list-1', name: 'Inbox', is_default: true }),
      makeList({ id: 'list-2', name: 'Work', is_default: false }),
      makeList({ id: 'list-3', name: 'Errands', is_default: false }),
    ]);
    mockDeleteList.mockResolvedValue(undefined);
    await renderSignedIn();

    // Delete a list that is not the selected one; focus lands on the selection.
    await user.click(screen.getByRole('button', { name: 'List actions for Errands' }));
    await user.click(screen.getByRole('menuitem', { name: 'Delete list Errands' }));
    await user.click(screen.getByRole('button', { name: 'Delete list' }));
    await waitFor(() => expect(mockDeleteList).toHaveBeenCalledWith('list-3'));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Inbox (0 active)' })).toHaveFocus(),
    );

    // The user moves on to the composer…
    const composer = screen.getByLabelText('New todo title');
    await user.click(composer);
    await user.type(composer, 'Half-typed todo');
    expect(composer).toHaveFocus();

    // …and another tab deletes the list they are on, which switches the
    // selection programmatically.
    mockListLists.mockResolvedValue([makeList({ id: 'list-2', name: 'Work', is_default: true })]);
    await deliver('list.deleted', { origin: 'another-tab', id: 'list-1' });

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Work (0 active)' })).toHaveAttribute(
        'aria-current',
        'true',
      ),
    );
    expect(composer).toHaveFocus();
    expect(composer).toHaveValue('Half-typed todo');
  });

  /**
   * The IT3-9 regression. The post-delete focus rule (IT3-7) exists for the
   * user who *pressed* Delete; a row that vanishes because another tab, a poll
   * or a filter change removed it must never move focus. The first iteration of
   * that feature keyed off "the list got shorter" and had to be reverted — this
   * pins the fixed behaviour down from the outside.
   */
  it('never moves focus when another tab removes a row', async () => {
    mockListTodos.mockResolvedValue(
      makeTodoPage([
        makeTodo({ id: 'id-1', title: 'Buy milk' }),
        makeTodo({ id: 'id-2', title: 'Walk the dog' }),
      ]),
    );
    await renderSignedIn();
    await screen.findByText('Walk the dog');

    const deleteMilk = screen.getByRole('button', { name: 'Delete Buy milk' });
    deleteMilk.focus();
    expect(deleteMilk).toHaveFocus();

    mockListTodos.mockResolvedValue(
      makeTodoPage([makeTodo({ id: 'id-1', title: 'Buy milk' })]),
    );
    await deliver('todo.deleted', { origin: 'another-tab', id: 'id-2', list_id: 'list-1' });

    await waitFor(() => expect(screen.queryByText('Walk the dog')).not.toBeInTheDocument());
    expect(deleteMilk).toHaveFocus();
  });

  /**
   * The same rule at its sharpest: the removed row *is* the one holding focus,
   * so the browser drops focus to `body`. Even then the app must not claim it —
   * the user did not ask for anything, and a stolen caret is worse than a
   * neutral one.
   */
  it('leaves focus on the body when the focused row is removed by an event', async () => {
    await renderSignedIn();

    screen.getByRole('button', { name: 'Delete Buy milk' }).focus();
    mockListTodos.mockResolvedValue(makeTodoPage([]));

    await deliver('todo.deleted', { origin: 'another-tab', id: 'id-1', list_id: 'list-1' });

    await waitFor(() => expect(screen.queryByText('Buy milk')).not.toBeInTheDocument());
    expect(document.activeElement).toBe(document.body);
    expect(screen.getByRole('textbox', { name: 'New todo title' })).not.toHaveFocus();
  });
});

/**
 * IT3-1, client half. Renaming or deleting a tag changes arbitrarily many
 * todos, so the contract deliberately does *not* fan out one `todo.updated`
 * per row (master §7.2): the receiver refetches both its todo view and its tag
 * vocabulary instead. The one thing a refetch cannot repair is the active
 * filter, which works in names while the frames carry ids.
 */
describe('tag.* frames', () => {
  const tags = [
    makeTag({ id: 'tag-1', name: 'home', todo_count: 2 }),
    makeTag({ id: 'tag-2', name: 'work', todo_count: 1 }),
  ];

  /** The `tag` values the last `GET /api/todos` was filtered by. */
  function lastTagQuery(): string[] {
    const query = mockListTodos.mock.calls.at(-1)?.[0] as { tag?: string[] } | undefined;
    return query?.tag ?? [];
  }

  beforeEach(() => {
    mockListTags.mockResolvedValue(tags);
  });

  /** Turns the given tag chip into an active filter. */
  async function filterByTag(
    user: ReturnType<typeof userEvent.setup>,
    name: string,
  ): Promise<void> {
    await user.click(await screen.findByRole('button', { name: `Filter by tag ${name}` }));
    await waitFor(() => expect(lastTagQuery()).toEqual([name]));
  }

  it('refetches the todos and the vocabulary once for a rename from another tab', async () => {
    await renderSignedIn();
    const todoCalls = mockListTodos.mock.calls.length;
    const tagCalls = mockListTags.mock.calls.length;

    await deliver('tag.updated', {
      origin: 'another-tab',
      tag: { id: 'tag-1', name: 'household', todo_count: 2 },
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });

    expect(mockListTodos).toHaveBeenCalledTimes(todoCalls + 1);
    expect(mockListTags).toHaveBeenCalledTimes(tagCalls + 1);
  });

  it('refetches the todos and the vocabulary once for a delete from another tab', async () => {
    await renderSignedIn();
    const todoCalls = mockListTodos.mock.calls.length;
    const tagCalls = mockListTags.mock.calls.length;

    await deliver('tag.deleted', { origin: null, id: 'tag-2' });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });

    expect(mockListTodos).toHaveBeenCalledTimes(todoCalls + 1);
    expect(mockListTags).toHaveBeenCalledTimes(tagCalls + 1);
  });

  it('ignores the echo of a tag we changed ourselves', async () => {
    await renderSignedIn();
    const todoCalls = mockListTodos.mock.calls.length;
    const tagCalls = mockListTags.mock.calls.length;

    await deliver('tag.created', {
      origin: getClientId(),
      tag: { id: 'tag-3', name: 'errand', todo_count: 1 },
    });
    await deliver('tag.updated', {
      origin: getClientId(),
      tag: { id: 'tag-1', name: 'household', todo_count: 2 },
    });
    await deliver('tag.deleted', { origin: getClientId(), id: 'tag-2' });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });

    expect(mockListTodos).toHaveBeenCalledTimes(todoCalls);
    expect(mockListTags).toHaveBeenCalledTimes(tagCalls);
  });

  it('follows a rename in the active filter, so the view is not stranded', async () => {
    const user = userEvent.setup();
    await renderSignedIn();
    await filterByTag(user, 'home');

    mockListTags.mockResolvedValue([
      makeTag({ id: 'tag-1', name: 'household', todo_count: 2 }),
      tags[1],
    ]);
    await deliver('tag.updated', {
      origin: 'another-tab',
      tag: { id: 'tag-1', name: 'household', todo_count: 2 },
    });

    // The query follows the rename instead of asking for a name the server has
    // forgotten, which would filter to empty for ever (master §6.3).
    await waitFor(() => expect(lastTagQuery()).toEqual(['household']));
    expect(
      await screen.findByRole('button', { name: 'Filter by tag household' }),
    ).toHaveAttribute('aria-pressed', 'true');
  });

  it('drops a deleted tag from the active filter', async () => {
    const user = userEvent.setup();
    await renderSignedIn();
    await filterByTag(user, 'home');

    mockListTags.mockResolvedValue([tags[1]]);
    await deliver('tag.deleted', { origin: 'another-tab', id: 'tag-1' });

    await waitFor(() => expect(lastTagQuery()).toEqual([]));
    // The user is back to an unfiltered view rather than a permanent no-match.
    expect(await screen.findByText('Buy milk')).toBeInTheDocument();
    expect(screen.queryByText('No todos match your filters.')).not.toBeInTheDocument();
  });

  /**
   * A repaired filter refetches the todos by itself, through the effect that
   * watches `filters`. Letting the debounced event refetch run as well would
   * issue the very same query twice for one frame — harmless thanks to the
   * request-sequence guard, but wasteful and confusing to read in a network
   * log, so the repair cancels it.
   */
  it('issues exactly one todo fetch when a delete repairs the filter', async () => {
    const user = userEvent.setup();
    await renderSignedIn();
    await filterByTag(user, 'home');
    const todoCalls = mockListTodos.mock.calls.length;
    const tagCalls = mockListTags.mock.calls.length;

    mockListTags.mockResolvedValue([tags[1]]);
    await deliver('tag.deleted', { origin: 'another-tab', id: 'tag-1' });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });

    await waitFor(() => expect(lastTagQuery()).toEqual([]));
    expect(mockListTodos).toHaveBeenCalledTimes(todoCalls + 1);
    // The vocabulary still catches up — it is what the chips are drawn from.
    expect(mockListTags).toHaveBeenCalledTimes(tagCalls + 1);
  });

  it('issues exactly one todo fetch when a rename repairs the filter', async () => {
    const user = userEvent.setup();
    await renderSignedIn();
    await filterByTag(user, 'home');
    const todoCalls = mockListTodos.mock.calls.length;

    mockListTags.mockResolvedValue([
      makeTag({ id: 'tag-1', name: 'household', todo_count: 2 }),
      tags[1],
    ]);
    await deliver('tag.updated', {
      origin: 'another-tab',
      tag: { id: 'tag-1', name: 'household', todo_count: 2 },
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });

    await waitFor(() => expect(lastTagQuery()).toEqual(['household']));
    expect(mockListTodos).toHaveBeenCalledTimes(todoCalls + 1);
  });

  it('leaves the filter alone for a tag the user is not filtering on', async () => {
    const user = userEvent.setup();
    await renderSignedIn();
    await filterByTag(user, 'home');

    await deliver('tag.updated', {
      origin: 'another-tab',
      tag: { id: 'tag-2', name: 'office', todo_count: 1 },
    });
    await deliver('tag.deleted', { origin: 'another-tab', id: 'tag-2' });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });

    expect(lastTagQuery()).toEqual(['home']);
  });

  it('refetches but repairs nothing when the tag id is unknown locally', async () => {
    const user = userEvent.setup();
    await renderSignedIn();
    await filterByTag(user, 'home');
    const todoCalls = mockListTodos.mock.calls.length;

    await deliver('tag.deleted', { origin: 'another-tab', id: 'tag-never-seen' });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });

    expect(mockListTodos).toHaveBeenCalledTimes(todoCalls + 1);
    expect(lastTagQuery()).toEqual(['home']);
  });
});
