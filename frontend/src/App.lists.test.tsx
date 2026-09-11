import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { listTodos } from './api/client';
import { createList, deleteList, listLists, renameList } from './api/lists';
import { me } from './api/auth';
import { ApiError } from './api/errors';
import {
  makeList,
  makeTodo,
  makeTodoPage,
  makeUser,
  renderApp,
  seedStoredSession,
} from './test/helpers';

vi.mock('./api/client');
vi.mock('./api/lists');
vi.mock('./api/auth');

const mockListTodos = vi.mocked(listTodos);
const mockListLists = vi.mocked(listLists);
const mockCreateList = vi.mocked(createList);
const mockRenameList = vi.mocked(renameList);
const mockDeleteList = vi.mocked(deleteList);
const mockMe = vi.mocked(me);

const inbox = makeList({ id: 'list-1', name: 'Inbox', is_default: true, active_count: 3 });
const work = makeList({
  id: 'list-2',
  name: 'Work',
  is_default: false,
  active_count: 1,
  created_at: '2026-09-02T12:00:00.000000Z',
});

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  seedStoredSession();
  mockMe.mockResolvedValue(makeUser());
  mockListLists.mockResolvedValue([inbox, work]);
  mockListTodos.mockResolvedValue(makeTodoPage([makeTodo()]));
});

/** The lists navigation in the sidebar. */
function nav() {
  return within(screen.getByRole('navigation', { name: 'Todo lists' }));
}

/** The entry button of one list, addressed by its accessible name. */
function entry(name: string): HTMLElement {
  return nav().getByRole('button', { name });
}

/** The accessible name of every entry, in the order they are rendered. */
function entryNames(): (string | null)[] {
  return nav()
    .getAllByRole('listitem')
    .map((item) => within(item).getAllByRole('button')[0].getAttribute('aria-label'));
}

/** Opens a list's actions menu (rename / delete live there now). */
async function openListMenu(
  user: ReturnType<typeof userEvent.setup>,
  listName: string,
): Promise<void> {
  await user.click(entry(`List actions for ${listName}`));
}

/** The query object of the most recent `listTodos` call. */
function lastQuery(): Record<string, unknown> {
  return (mockListTodos.mock.calls.at(-1)?.[0] ?? {}) as Record<string, unknown>;
}

async function renderSignedIn(): Promise<void> {
  renderApp();
  await screen.findByRole('heading', { name: 'Todos', level: 1 });
  await waitFor(() => expect(mockListTodos).toHaveBeenCalled());
}

describe('list navigation', () => {
  it('renders All lists plus every list with its active count', async () => {
    await renderSignedIn();

    expect(entryNames()).toEqual(['All lists', 'Inbox (3 active)', 'Work (1 active)']);
    // The default list is selected when nothing is remembered.
    expect(entry('Inbox (3 active)')).toHaveAttribute('aria-current', 'true');
    expect(entry('Work (1 active)')).not.toHaveAttribute('aria-current');
    expect(mockListTodos).toHaveBeenCalledWith(expect.objectContaining({ list_id: 'list-1' }));
  });

  it('refetches todos with the chosen list_id and persists the selection', async () => {
    const user = userEvent.setup();
    await renderSignedIn();

    await user.click(entry('Work (1 active)'));
    await waitFor(() => expect(mockListTodos).toHaveBeenCalledWith(expect.objectContaining({ list_id: 'list-2' })));
    expect(localStorage.getItem('todo.selected-list')).toBe('list-2');

    await user.click(entry('All lists'));
    // "All lists" simply omits list_id, so the server spans every list.
    await waitFor(() => expect(lastQuery()).not.toHaveProperty('list_id'));
    // "All lists" is not remembered; a reload returns to the default list.
    expect(localStorage.getItem('todo.selected-list')).toBeNull();
  });

  it('restores the remembered list on load', async () => {
    localStorage.setItem('todo.selected-list', 'list-2');

    await renderSignedIn();

    expect(entry('Work (1 active)')).toHaveAttribute('aria-current', 'true');
    expect(mockListTodos).toHaveBeenCalledWith(expect.objectContaining({ list_id: 'list-2' }));
  });

  it('falls back to the default list when the remembered list is gone', async () => {
    localStorage.setItem('todo.selected-list', 'list-deleted');

    await renderSignedIn();

    expect(entry('Inbox (3 active)')).toHaveAttribute('aria-current', 'true');
    expect(mockListTodos).toHaveBeenCalledWith(expect.objectContaining({ list_id: 'list-1' }));
  });
});

describe('creating a list', () => {
  it('selects the new list, clears the input and refocuses it', async () => {
    const user = userEvent.setup();
    const created = makeList({ id: 'list-3', name: 'Errands', is_default: false, active_count: 0 });
    mockCreateList.mockResolvedValue(created);

    await renderSignedIn();
    await user.click(screen.getByRole('button', { name: 'New list' }));

    const input = screen.getByLabelText('New list name');
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute('maxlength', '100');
    expect(input).toHaveAttribute('placeholder', 'List name');

    await user.type(input, '  Errands  ');
    await user.click(screen.getByRole('button', { name: 'Create list' }));

    await waitFor(() => expect(mockCreateList).toHaveBeenCalledWith('Errands'));
    await screen.findByRole('button', { name: 'Errands (0 active)' });
    expect(entry('Errands (0 active)')).toHaveAttribute('aria-current', 'true');
    await waitFor(() => expect(mockListTodos).toHaveBeenCalledWith(expect.objectContaining({ list_id: 'list-3' })));
    expect(input).toHaveValue('');
    expect(input).toHaveFocus();
  });

  it('shows the name-taken copy on 409 and moves focus to the alert', async () => {
    const user = userEvent.setup();
    mockCreateList.mockRejectedValue(
      new ApiError(409, 'list_name_taken', 'A list with that name already exists'),
    );

    await renderSignedIn();
    await user.click(screen.getByRole('button', { name: 'New list' }));
    await user.type(screen.getByLabelText('New list name'), 'Work');
    await user.click(screen.getByRole('button', { name: 'Create list' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('A list with that name already exists.');
    await waitFor(() => expect(alert).toHaveFocus());
    expect(screen.getByLabelText('New list name')).toHaveAttribute(
      'aria-describedby',
      alert.id,
    );
  });

  it('returns focus to the New list button on cancel', async () => {
    const user = userEvent.setup();
    await renderSignedIn();

    const newListButton = screen.getByRole('button', { name: 'New list' });
    await user.click(newListButton);
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByLabelText('New list name')).not.toBeInTheDocument();
    expect(newListButton).toHaveFocus();
  });
});

describe('renaming a list', () => {
  it('saves the new name and updates the option', async () => {
    const user = userEvent.setup();
    mockRenameList.mockResolvedValue({ ...inbox, name: 'Inbox zero' });

    await renderSignedIn();
    await openListMenu(user, 'Inbox');
    await user.click(screen.getByRole('menuitem', { name: 'Rename Inbox' }));

    const input = screen.getByLabelText('New list name');
    expect(input).toHaveValue('Inbox');

    await user.clear(input);
    await user.type(input, 'Inbox zero');
    await user.click(screen.getByRole('button', { name: 'Save list name' }));

    await waitFor(() => expect(mockRenameList).toHaveBeenCalledWith('list-1', 'Inbox zero'));
    await screen.findByRole('button', { name: 'Inbox zero (3 active)' });
    expect(screen.queryByLabelText('New list name')).not.toBeInTheDocument();
    // Focus goes back to the menu the rename was started from.
    expect(entry('List actions for Inbox zero')).toHaveFocus();
  });

  it('shows the name-taken copy on 409', async () => {
    const user = userEvent.setup();
    mockRenameList.mockRejectedValue(
      new ApiError(409, 'list_name_taken', 'A list with that name already exists'),
    );

    await renderSignedIn();
    await openListMenu(user, 'Inbox');
    await user.click(screen.getByRole('menuitem', { name: 'Rename Inbox' }));
    await user.clear(screen.getByLabelText('New list name'));
    await user.type(screen.getByLabelText('New list name'), 'Work');
    await user.click(screen.getByRole('button', { name: 'Save list name' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'A list with that name already exists.',
    );
  });
});

describe('deleting a list', () => {
  it('confirms inline, deletes and selects the remaining default list', async () => {
    const user = userEvent.setup();
    mockDeleteList.mockResolvedValue(undefined);

    await renderSignedIn();
    await openListMenu(user, 'Inbox');
    await user.click(screen.getByRole('menuitem', { name: 'Delete list Inbox' }));

    expect(screen.getByText('Delete “Inbox” and its todos?')).toBeInTheDocument();
    const confirm = screen.getByRole('button', { name: 'Delete list' });
    await waitFor(() => expect(confirm).toHaveFocus());

    await user.click(confirm);

    await waitFor(() => expect(mockDeleteList).toHaveBeenCalledWith('list-1'));
    await waitFor(() =>
      expect(nav().queryByRole('button', { name: 'Inbox (3 active)' })).not.toBeInTheDocument(),
    );
    // Deleting the default promotes the oldest remaining list, which becomes
    // the selection — and takes focus, so the keyboard user is not stranded.
    expect(entry('Work (1 active)')).toHaveAttribute('aria-current', 'true');
    expect(entry('Work (1 active)')).toHaveFocus();
    await waitFor(() => expect(mockListTodos).toHaveBeenCalledWith(expect.objectContaining({ list_id: 'list-2' })));
  });

  it('keeps the list and restores focus when the confirmation is dismissed', async () => {
    const user = userEvent.setup();
    await renderSignedIn();

    await openListMenu(user, 'Inbox');
    await user.click(screen.getByRole('menuitem', { name: 'Delete list Inbox' }));
    await user.click(screen.getByRole('button', { name: 'Keep list' }));

    expect(screen.queryByText('Delete “Inbox” and its todos?')).not.toBeInTheDocument();
    expect(entry('List actions for Inbox')).toHaveFocus();
    expect(mockDeleteList).not.toHaveBeenCalled();
  });

  it('shows the last-list copy on 409 cannot_delete_last_list', async () => {
    const user = userEvent.setup();
    mockListLists.mockResolvedValue([inbox]);
    mockDeleteList.mockRejectedValue(
      new ApiError(409, 'cannot_delete_last_list', 'You must keep at least one list'),
    );

    await renderSignedIn();
    await openListMenu(user, 'Inbox');
    await user.click(screen.getByRole('menuitem', { name: 'Delete list Inbox' }));
    await user.click(screen.getByRole('button', { name: 'Delete list' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('You must keep at least one list.');
    await waitFor(() => expect(alert).toHaveFocus());
    expect(entry('Inbox (3 active)')).toBeInTheDocument();
  });

  it('shows the generic list failure for anything else', async () => {
    const user = userEvent.setup();
    mockDeleteList.mockRejectedValue(new TypeError('Failed to fetch'));

    await renderSignedIn();
    await openListMenu(user, 'Inbox');
    await user.click(screen.getByRole('menuitem', { name: 'Delete list Inbox' }));
    await user.click(screen.getByRole('button', { name: 'Delete list' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not update your lists. Please try again.',
    );
  });
});

describe('creating a todo while All lists is selected', () => {
  it('posts without a list_id so the server default applies', async () => {
    const user = userEvent.setup();
    const { createTodo } = await import('./api/client');
    const mockCreateTodo = vi.mocked(createTodo);
    mockCreateTodo.mockResolvedValue(makeTodo({ id: 'new-id', title: 'New task' }));

    await renderSignedIn();
    await user.click(entry('All lists'));
    await waitFor(() => expect(lastQuery()).not.toHaveProperty('list_id'));

    await user.type(screen.getByLabelText('New todo title'), 'New task{Enter}');

    await waitFor(() => expect(mockCreateTodo).toHaveBeenCalledWith({ title: 'New task' }));
  });
});
