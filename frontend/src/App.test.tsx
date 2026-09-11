import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import type { Todo, TodoPage } from './api/types';
import { createTodo, deleteTodo, listTodos, setCompleted } from './api/client';
import { listLists } from './api/lists';
import { me } from './api/auth';
import { makeList, makeTodo, makeTodoPage, renderApp, seedStoredSession } from './test/helpers';

vi.mock('./api/client');
vi.mock('./api/lists');
vi.mock('./api/auth');

const mockListTodos = vi.mocked(listTodos);
const mockCreateTodo = vi.mocked(createTodo);
const mockSetCompleted = vi.mocked(setCompleted);
const mockDeleteTodo = vi.mocked(deleteTodo);
const mockListLists = vi.mocked(listLists);
const mockMe = vi.mocked(me);

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  // Every test in this file runs as a signed-in user with a single list.
  seedStoredSession();
  mockMe.mockResolvedValue({
    id: 'user-1',
    email: 'me@example.test',
    display_name: null,
    created_at: '2026-09-02T11:22:33.123456Z',
  });
  mockListLists.mockResolvedValue([makeList()]);
});

describe('App', () => {
  it('renders the loading state and then the list', async () => {
    let resolveList!: (page: TodoPage) => void;
    mockListTodos.mockReturnValue(
      new Promise((resolve) => {
        resolveList = resolve;
      }),
    );

    renderApp();

    expect(await screen.findByText('Loading todos...')).toBeInTheDocument();

    resolveList(makeTodoPage([makeTodo()]));

    await waitFor(() => {
      expect(screen.queryByText('Loading todos...')).not.toBeInTheDocument();
    });
    expect(screen.getByText('Buy milk')).toBeInTheDocument();
  });

  it('shows the empty state and 0 items left for an empty list', async () => {
    mockListTodos.mockResolvedValue(makeTodoPage([]));

    renderApp();

    await screen.findByText('No todos yet. Add your first one above.');
    expect(screen.getByText('0 items left')).toBeInTheDocument();
  });

  it('renders N todos with their titles, marking completed ones', async () => {
    mockListTodos.mockResolvedValue(makeTodoPage([
      makeTodo({ id: 'id-1', title: 'Buy milk', completed: false }),
      makeTodo({ id: 'id-2', title: 'Walk the dog', completed: true }),
    ]));

    renderApp();

    await screen.findByText('Buy milk');
    expect(screen.getByText('Walk the dog')).toBeInTheDocument();

    const activeCheckbox = screen.getByRole('checkbox', {
      name: 'Mark Buy milk as completed',
    });
    expect(activeCheckbox).not.toBeChecked();

    const completedCheckbox = screen.getByRole('checkbox', {
      name: 'Mark Walk the dog as active',
    });
    expect(completedCheckbox).toBeChecked();
    expect(screen.getByText('Walk the dog')).toHaveClass('line-through');
  });

  it('adds a todo via the Add button and clears the input', async () => {
    const user = userEvent.setup();
    mockListTodos.mockResolvedValue(makeTodoPage([]));
    const created = makeTodo({ id: 'new-id', title: 'New task' });
    mockCreateTodo.mockResolvedValue(created);

    renderApp();
    await screen.findByText('No todos yet. Add your first one above.');

    const input = screen.getByLabelText('New todo title');
    await user.type(input, '  New task  ');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    await screen.findByText('New task');
    expect(mockCreateTodo).toHaveBeenCalledTimes(1);
    expect(mockCreateTodo).toHaveBeenCalledWith({ title: 'New task', list_id: 'list-1' });
    expect(input).toHaveValue('');
    expect(input).toHaveFocus();
  });

  it('adds a todo via the Enter key', async () => {
    const user = userEvent.setup();
    mockListTodos.mockResolvedValue(makeTodoPage([]));
    const created = makeTodo({ id: 'new-id', title: 'New task' });
    mockCreateTodo.mockResolvedValue(created);

    renderApp();
    await screen.findByText('No todos yet. Add your first one above.');

    const input = screen.getByLabelText('New todo title');
    await user.type(input, 'New task{Enter}');

    await screen.findByText('New task');
    expect(mockCreateTodo).toHaveBeenCalledTimes(1);
    expect(mockCreateTodo).toHaveBeenCalledWith({ title: 'New task', list_id: 'list-1' });
  });

  it('disables Add for empty/whitespace-only input and does not call createTodo', async () => {
    const user = userEvent.setup();
    mockListTodos.mockResolvedValue(makeTodoPage([]));

    renderApp();
    await screen.findByText('No todos yet. Add your first one above.');

    const input = screen.getByLabelText('New todo title');
    const button = screen.getByRole('button', { name: 'Add' });
    expect(button).toBeDisabled();

    await user.type(input, '   ');
    expect(button).toBeDisabled();

    await user.keyboard('{Enter}');
    expect(mockCreateTodo).not.toHaveBeenCalled();
  });

  it('toggles a todo active -> completed -> active', async () => {
    const user = userEvent.setup();
    const todo = makeTodo({ id: 'id-1', title: 'Buy milk', completed: false });
    mockListTodos.mockResolvedValue(makeTodoPage([todo]));
    mockSetCompleted.mockResolvedValueOnce({ ...todo, completed: true });

    renderApp();
    await screen.findByText('Buy milk');

    const checkbox = screen.getByRole('checkbox', {
      name: 'Mark Buy milk as completed',
    });
    await user.click(checkbox);

    expect(mockSetCompleted).toHaveBeenCalledWith('id-1', true);
    await screen.findByRole('checkbox', { name: 'Mark Buy milk as active' });

    mockSetCompleted.mockResolvedValueOnce({ ...todo, completed: false });
    const completedCheckbox = screen.getByRole('checkbox', {
      name: 'Mark Buy milk as active',
    });
    await user.click(completedCheckbox);

    expect(mockSetCompleted).toHaveBeenCalledWith('id-1', false);
    await screen.findByRole('checkbox', { name: 'Mark Buy milk as completed' });
  });

  it('deletes a todo and removes the row', async () => {
    const user = userEvent.setup();
    const todo = makeTodo({ id: 'id-1', title: 'Buy milk' });
    mockListTodos.mockResolvedValue(makeTodoPage([todo]));
    mockDeleteTodo.mockResolvedValue(undefined);

    renderApp();
    await screen.findByText('Buy milk');

    await user.click(screen.getByRole('button', { name: 'Delete Buy milk' }));

    expect(mockDeleteTodo).toHaveBeenCalledWith('id-1');
    await waitFor(() => {
      expect(screen.queryByText('Buy milk')).not.toBeInTheDocument();
    });
  });

  it('sets aria-disabled on checkbox and Delete button while a request is in flight', async () => {
    const user = userEvent.setup();
    const todo = makeTodo({ id: 'id-1', title: 'Buy milk', completed: false });
    mockListTodos.mockResolvedValue(makeTodoPage([todo]));

    let resolveToggle!: (value: Todo) => void;
    mockSetCompleted.mockReturnValue(
      new Promise((resolve) => {
        resolveToggle = resolve;
      }),
    );

    renderApp();
    await screen.findByText('Buy milk');

    const checkbox = screen.getByRole('checkbox', {
      name: 'Mark Buy milk as completed',
    });
    const deleteButton = screen.getByRole('button', { name: 'Delete Buy milk' });
    const listItem = checkbox.closest('li');

    expect(checkbox).not.toHaveAttribute('aria-disabled');
    expect(deleteButton).not.toHaveAttribute('aria-disabled');
    expect(listItem).not.toHaveAttribute('aria-busy');

    await user.click(checkbox);

    expect(checkbox).toHaveAttribute('aria-disabled');
    expect(deleteButton).toHaveAttribute('aria-disabled');
    expect(listItem).toHaveAttribute('aria-busy', 'true');

    resolveToggle({ ...todo, completed: true });

    await waitFor(() => {
      const updatedCheckbox = screen.getByRole('checkbox', {
        name: 'Mark Buy milk as active',
      });
      expect(updatedCheckbox).not.toHaveAttribute('aria-disabled');
    });
    expect(
      screen.getByRole('button', { name: 'Delete Buy milk' }),
    ).not.toHaveAttribute('aria-disabled');
  });

  it('retains focus on checkbox during and after a toggle', async () => {
    const user = userEvent.setup();
    const todo = makeTodo({ id: 'id-1', title: 'Buy milk', completed: false });
    mockListTodos.mockResolvedValue(makeTodoPage([todo]));

    let resolveToggle!: (value: Todo) => void;
    mockSetCompleted.mockReturnValue(
      new Promise((resolve) => {
        resolveToggle = resolve;
      }),
    );

    renderApp();
    await screen.findByText('Buy milk');

    const checkbox = screen.getByRole('checkbox', {
      name: 'Mark Buy milk as completed',
    });

    await user.click(checkbox);

    // Focus should remain on the checkbox while request is in flight
    expect(document.activeElement).toBe(checkbox);

    resolveToggle({ ...todo, completed: true });

    await waitFor(() => {
      // Focus should still be on the checkbox (now updated label) after request completes
      expect(document.activeElement).toBe(
        screen.getByRole('checkbox', { name: 'Mark Buy milk as active' }),
      );
    });
  });

  it('does not issue a second setCompleted call when clicking while busy', async () => {
    const user = userEvent.setup();
    const todo = makeTodo({ id: 'id-1', title: 'Buy milk', completed: false });
    mockListTodos.mockResolvedValue(makeTodoPage([todo]));

    let resolveToggle!: (value: Todo) => void;
    mockSetCompleted.mockReturnValue(
      new Promise((resolve) => {
        resolveToggle = resolve;
      }),
    );

    renderApp();
    await screen.findByText('Buy milk');

    const checkbox = screen.getByRole('checkbox', {
      name: 'Mark Buy milk as completed',
    });

    await user.click(checkbox);
    expect(mockSetCompleted).toHaveBeenCalledTimes(1);

    // Try to click again while request is in flight
    await user.click(checkbox);
    expect(mockSetCompleted).toHaveBeenCalledTimes(1); // Should still be 1

    resolveToggle({ ...todo, completed: true });
  });

  it('shows the correct active-item counter text', async () => {
    mockListTodos.mockResolvedValue(makeTodoPage([
      makeTodo({ id: 'id-1', title: 'One', completed: false }),
    ]));

    const { unmount } = renderApp();
    await screen.findByText('1 item left');
    unmount();

    mockListTodos.mockResolvedValue(makeTodoPage([
      makeTodo({ id: 'id-1', title: 'One', completed: false }),
      makeTodo({ id: 'id-2', title: 'Two', completed: false }),
    ]));
    renderApp();
    await screen.findByText('2 items left');
  });

  it('shows a load-failure alert and an empty list on load error', async () => {
    mockListTodos.mockRejectedValue(new Error('boom'));

    renderApp();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load todos. Please try again.');
    expect(screen.getByText('No todos yet. Add your first one above.')).toBeInTheDocument();
  });

  it('shows a create-failure alert and leaves the list unchanged', async () => {
    const user = userEvent.setup();
    mockListTodos.mockResolvedValue(makeTodoPage([]));
    mockCreateTodo.mockRejectedValue(new Error('boom'));

    renderApp();
    await screen.findByText('No todos yet. Add your first one above.');

    const input = screen.getByLabelText('New todo title');
    await user.type(input, 'New task{Enter}');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not add todo. Please try again.');
    expect(screen.getByText('No todos yet. Add your first one above.')).toBeInTheDocument();
  });

  it('shows a toggle-failure alert and leaves the row unchanged', async () => {
    const user = userEvent.setup();
    const todo = makeTodo({ id: 'id-1', title: 'Buy milk', completed: false });
    mockListTodos.mockResolvedValue(makeTodoPage([todo]));
    mockSetCompleted.mockRejectedValue(new Error('boom'));

    renderApp();
    await screen.findByText('Buy milk');

    await user.click(
      screen.getByRole('checkbox', { name: 'Mark Buy milk as completed' }),
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not update todo. Please try again.');
    expect(
      screen.getByRole('checkbox', { name: 'Mark Buy milk as completed' }),
    ).not.toBeChecked();
  });

  it('shows a delete-failure alert and leaves the row in place', async () => {
    const user = userEvent.setup();
    const todo = makeTodo({ id: 'id-1', title: 'Buy milk' });
    mockListTodos.mockResolvedValue(makeTodoPage([todo]));
    mockDeleteTodo.mockRejectedValue(new Error('boom'));

    renderApp();
    await screen.findByText('Buy milk');

    await user.click(screen.getByRole('button', { name: 'Delete Buy milk' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not delete todo. Please try again.');
    expect(screen.getByText('Buy milk')).toBeInTheDocument();
  });
});

/**
 * IT3-7. Activating Delete with the keyboard destroys the very control the
 * user was standing on, and the browser drops focus to `body` — a dead end for
 * anyone not using a mouse. The rule: hand focus to the next row's Delete, else
 * the previous row's, else the composer.
 */
describe('focus after deleting a todo', () => {
  const rows = [
    makeTodo({ id: 'id-1', title: 'First' }),
    makeTodo({ id: 'id-2', title: 'Second' }),
    makeTodo({ id: 'id-3', title: 'Third' }),
  ];

  it('moves focus to the next row Delete button', async () => {
    const user = userEvent.setup();
    mockListTodos.mockResolvedValue(makeTodoPage(rows));
    mockDeleteTodo.mockResolvedValue(undefined);
    renderApp();
    await screen.findByText('Second');

    await user.click(screen.getByRole('button', { name: 'Delete Second' }));

    await waitFor(() => expect(screen.queryByText('Second')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Delete Third' })).toHaveFocus();
  });

  it('falls back to the previous row when the last one is deleted', async () => {
    const user = userEvent.setup();
    mockListTodos.mockResolvedValue(makeTodoPage(rows));
    mockDeleteTodo.mockResolvedValue(undefined);
    renderApp();
    await screen.findByText('Third');

    await user.click(screen.getByRole('button', { name: 'Delete Third' }));

    await waitFor(() => expect(screen.queryByText('Third')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Delete Second' })).toHaveFocus();
  });

  it('falls back to the composer title when the only row is deleted', async () => {
    const user = userEvent.setup();
    mockListTodos.mockResolvedValue(makeTodoPage([makeTodo({ id: 'id-1', title: 'Only' })]));
    mockDeleteTodo.mockResolvedValue(undefined);
    renderApp();
    await screen.findByText('Only');

    await user.click(screen.getByRole('button', { name: 'Delete Only' }));

    await waitFor(() => expect(screen.queryByText('Only')).not.toBeInTheDocument());
    expect(screen.getByRole('textbox', { name: 'New todo title' })).toHaveFocus();
  });

  it('leaves focus alone when the delete failed', async () => {
    const user = userEvent.setup();
    mockListTodos.mockResolvedValue(makeTodoPage(rows));
    mockDeleteTodo.mockRejectedValue(new Error('boom'));
    renderApp();
    await screen.findByText('Second');

    const deleteButton = screen.getByRole('button', { name: 'Delete Second' });
    await user.click(deleteButton);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not delete todo. Please try again.',
    );
    // The row is still there and so is the button the user is standing on.
    expect(deleteButton).toHaveFocus();
    expect(screen.getByText('Second')).toBeInTheDocument();
  });

  it('does not chase focus when the user has already moved on', async () => {
    const user = userEvent.setup();
    mockListTodos.mockResolvedValue(makeTodoPage(rows));
    let resolveDelete!: () => void;
    mockDeleteTodo.mockReturnValue(
      new Promise<void>((resolve) => {
        resolveDelete = resolve;
      }),
    );
    renderApp();
    await screen.findByText('Second');

    await user.click(screen.getByRole('button', { name: 'Delete Second' }));
    // While the DELETE is in flight the user tabs away to the composer.
    const title = screen.getByRole('textbox', { name: 'New todo title' });
    title.focus();
    await act(async () => {
      resolveDelete();
    });

    await waitFor(() => expect(screen.queryByText('Second')).not.toBeInTheDocument());
    expect(title).toHaveFocus();
  });
});

// Sanity check that role-based queries find items scoped within the list only.
describe('TodoList scoping', () => {
  it('renders todos inside list markup', async () => {
    mockListTodos.mockResolvedValue(makeTodoPage([makeTodo({ id: 'id-1', title: 'Buy milk' })]));
    renderApp();
    // The page now has more than one list (the sidebar navigation is one too),
    // so the todo list is addressed by its name.
    const list = await screen.findByRole('list', { name: 'Todos' });
    expect(within(list).getByText('Buy milk')).toBeInTheDocument();
  });
});
