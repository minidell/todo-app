import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Todo } from './api/types';
import {
  createSubtask,
  deleteTodo,
  listTodos,
  setCompleted,
  updateTodo,
} from './api/client';
import { listLists } from './api/lists';
import { listTags } from './api/tags';
import { me } from './api/auth';
import { formatDay, todayString } from './dates';
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
vi.mock('./api/tags');
vi.mock('./api/auth');

const mockListTodos = vi.mocked(listTodos);
const mockUpdateTodo = vi.mocked(updateTodo);
const mockCreateSubtask = vi.mocked(createSubtask);
const mockSetCompleted = vi.mocked(setCompleted);
const mockDeleteTodo = vi.mocked(deleteTodo);
const mockListLists = vi.mocked(listLists);
const mockListTags = vi.mocked(listTags);
const mockMe = vi.mocked(me);

const TODAY = todayString();

/** A local date `days` away from today, in the wire format. */
function shift(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() + days);
  return todayString(date);
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  seedStoredSession();
  mockMe.mockResolvedValue(makeUser());
  mockListLists.mockResolvedValue([makeList()]);
  mockListTags.mockResolvedValue([]);
  mockListTodos.mockResolvedValue(makeTodoPage([]));
});

async function renderWith(todos: Todo[]): Promise<void> {
  mockListTodos.mockResolvedValue(makeTodoPage(todos));
  renderApp();
  await screen.findByRole('heading', { name: 'Todos', level: 1 });
  await waitFor(() => expect(mockListTodos).toHaveBeenCalled());
}

function row(title: string): HTMLElement {
  return screen.getByText(title).closest('li') as HTMLElement;
}

describe('todo row', () => {
  it('renders the priority word, due-date copy, tags and subtask progress', async () => {
    await renderWith([
      makeTodo({
        id: 'id-1',
        title: 'Buy milk',
        priority: 'high',
        due_date: shift(3),
        tags: ['errand', 'home'],
        subtasks: [
          makeTodo({ id: 'sub-1', title: 'Find the shop', parent_id: 'id-1', completed: true }),
          makeTodo({ id: 'sub-2', title: 'Pay', parent_id: 'id-1', completed: true }),
          makeTodo({ id: 'sub-3', title: 'Carry it home', parent_id: 'id-1' }),
        ],
      }),
    ]);

    const item = within(row('Buy milk'));
    // The word is always present — priority is never colour-only.
    expect(item.getByLabelText('Priority: High')).toHaveTextContent('High');
    expect(item.getByText(`Due ${formatDay(shift(3))}`)).toBeInTheDocument();
    expect(item.getByLabelText('Tag: errand')).toBeInTheDocument();
    expect(item.getByLabelText('Tag: home')).toBeInTheDocument();
    expect(item.getByText('2/3 subtasks')).toBeInTheDocument();
  });

  it('says "Due today" and flags an overdue active todo in words', async () => {
    await renderWith([
      makeTodo({ id: 'id-1', title: 'Today task', due_date: TODAY }),
      makeTodo({ id: 'id-2', title: 'Late task', due_date: shift(-2) }),
      makeTodo({
        id: 'id-3',
        title: 'Done late task',
        due_date: shift(-2),
        completed: true,
      }),
    ]);

    expect(within(row('Today task')).getByText('Due today')).toBeInTheDocument();

    const overdue = within(row('Late task')).getByText(`Overdue — ${formatDay(shift(-2))}`);
    expect(overdue).toHaveClass('text-red-700');

    // A completed todo is late, not "Overdue" — matches the backend filter.
    expect(
      within(row('Done late task')).getByText(`Due ${formatDay(shift(-2))}`),
    ).toBeInTheDocument();
  });

  it('renders no due-date or subtask copy when there is none', async () => {
    await renderWith([makeTodo({ id: 'id-1', title: 'Bare' })]);

    const item = within(row('Bare'));
    expect(item.getByLabelText('Priority: Medium')).toBeInTheDocument();
    expect(item.queryByText(/subtasks$/)).not.toBeInTheDocument();
    expect(item.queryByText(/^Due /)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Show subtasks/ })).not.toBeInTheDocument();
  });
});

describe('expanding subtasks', () => {
  it('flips the toggle name and aria-expanded and lists the subtasks', async () => {
    const user = userEvent.setup();
    await renderWith([
      makeTodo({
        id: 'id-1',
        title: 'Buy milk',
        subtasks: [makeTodo({ id: 'sub-1', title: 'Find the shop', parent_id: 'id-1' })],
      }),
    ]);

    // The progress text is part of the toggle's accessible name (WCAG 2.5.3),
    // so the name is matched by its leading phrase.
    const toggle = screen.getByRole('button', { name: /^Show subtasks of Buy milk/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Find the shop')).not.toBeInTheDocument();

    await user.click(toggle);

    expect(screen.getByText('Find the shop')).toBeInTheDocument();
    const hide = screen.getByRole('button', { name: /^Hide subtasks of Buy milk/ });
    expect(hide).toHaveAttribute('aria-expanded', 'true');

    await user.click(hide);
    expect(screen.queryByText('Find the shop')).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /^Show subtasks of Buy milk/ }),
    ).toHaveAttribute('aria-expanded', 'false');
  });

  it('toggles and deletes a subtask without touching the parent', async () => {
    const user = userEvent.setup();
    const subtask = makeTodo({ id: 'sub-1', title: 'Find the shop', parent_id: 'id-1' });
    await renderWith([makeTodo({ id: 'id-1', title: 'Buy milk', subtasks: [subtask] })]);

    await user.click(screen.getByRole('button', { name: /^Show subtasks of Buy milk/ }));

    mockSetCompleted.mockResolvedValue({ ...subtask, completed: true });
    await user.click(screen.getByRole('checkbox', { name: 'Mark Find the shop as completed' }));

    expect(mockSetCompleted).toHaveBeenCalledWith('sub-1', true);
    await screen.findByRole('checkbox', { name: 'Mark Find the shop as active' });
    // The parent's own state is untouched (decision D-A2).
    expect(screen.getByRole('checkbox', { name: 'Mark Buy milk as completed' })).not.toBeChecked();
    expect(screen.getByText('1/1 subtasks')).toBeInTheDocument();

    mockDeleteTodo.mockResolvedValue(undefined);
    await user.click(screen.getByRole('button', { name: 'Delete subtask Find the shop' }));

    expect(mockDeleteTodo).toHaveBeenCalledWith('sub-1');
    await waitFor(() => expect(screen.queryByText('Find the shop')).not.toBeInTheDocument());
    expect(screen.getByText('Buy milk')).toBeInTheDocument();
  });

  it('shows the subtask failure copy when a subtask request fails', async () => {
    const user = userEvent.setup();
    const subtask = makeTodo({ id: 'sub-1', title: 'Find the shop', parent_id: 'id-1' });
    await renderWith([makeTodo({ id: 'id-1', title: 'Buy milk', subtasks: [subtask] })]);

    await user.click(screen.getByRole('button', { name: /^Show subtasks of Buy milk/ }));
    mockSetCompleted.mockRejectedValue(new Error('boom'));
    await user.click(screen.getByRole('checkbox', { name: 'Mark Find the shop as completed' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not update the subtask. Please try again.',
    );
  });
});

describe('edit panel', () => {
  const todo = makeTodo({
    id: 'id-1',
    title: 'Buy milk',
    description: '2% please',
    priority: 'low',
    due_date: '2026-12-01',
    tags: ['errand'],
  });

  function panel(): HTMLElement {
    return screen.getByRole('group', { name: 'Edit Buy milk' });
  }

  async function openPanel(user: ReturnType<typeof userEvent.setup>): Promise<void> {
    await user.click(screen.getByRole('button', { name: 'Edit Buy milk' }));
  }

  it('prefills every field and focuses the title', async () => {
    const user = userEvent.setup();
    await renderWith([todo]);
    await openPanel(user);

    const fields = within(panel());
    expect(fields.getByLabelText('Title')).toHaveValue('Buy milk');
    expect(fields.getByLabelText('Title')).toHaveFocus();
    expect(fields.getByLabelText('Description')).toHaveValue('2% please');
    expect(fields.getByLabelText('Description')).toHaveAttribute('maxlength', '2000');
    expect(fields.getByLabelText('Priority')).toHaveValue('low');
    expect(fields.getByLabelText('Due date')).toHaveValue('2026-12-01');
    expect(fields.getByLabelText('Remove tag errand')).toBeInTheDocument();
  });

  it('saves only the changed fields', async () => {
    const user = userEvent.setup();
    mockUpdateTodo.mockResolvedValue({ ...todo, priority: 'high' });
    await renderWith([todo]);
    await openPanel(user);

    await user.selectOptions(within(panel()).getByLabelText('Priority'), 'high');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(mockUpdateTodo).toHaveBeenCalledWith('id-1', { priority: 'high' }));
    // The panel closes and focus returns to the control that opened it.
    await waitFor(() =>
      expect(screen.queryByRole('group', { name: 'Edit Buy milk' })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: 'Edit Buy milk' })).toHaveFocus();
  });

  it('sends the raw due-date string and clears it with Clear due date', async () => {
    const user = userEvent.setup();
    mockUpdateTodo.mockResolvedValue(todo);
    await renderWith([makeTodo({ id: 'id-1', title: 'Buy milk' })]);
    await openPanel(user);

    fireEvent.change(within(panel()).getByLabelText('Due date'), {
      target: { value: '2026-12-01' },
    });
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    // The exact wire string, never a Date round-trip.
    await waitFor(() =>
      expect(mockUpdateTodo).toHaveBeenCalledWith('id-1', { due_date: '2026-12-01' }),
    );

    mockUpdateTodo.mockClear();
    await renderWith([todo]);
    await openPanel(user);
    await user.click(within(panel()).getByRole('button', { name: 'Clear due date' }));
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(mockUpdateTodo).toHaveBeenCalledWith('id-1', { due_date: null }));
  });

  it('sends description and tag changes together, clearing an emptied description', async () => {
    const user = userEvent.setup();
    mockUpdateTodo.mockResolvedValue(todo);
    await renderWith([todo]);
    await openPanel(user);

    await user.clear(within(panel()).getByLabelText('Description'));
    await user.click(within(panel()).getByLabelText('Remove tag errand'));
    await user.type(within(panel()).getByLabelText('Tags'), 'Home{Enter}');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() =>
      expect(mockUpdateTodo).toHaveBeenCalledWith('id-1', {
        description: null,
        tags: ['home'],
      }),
    );
  });

  it('discards the edits on cancel and returns focus to the Edit button', async () => {
    const user = userEvent.setup();
    await renderWith([todo]);
    await openPanel(user);

    await user.clear(within(panel()).getByLabelText('Title'));
    await user.type(within(panel()).getByLabelText('Title'), 'Something else');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(mockUpdateTodo).not.toHaveBeenCalled();
    expect(screen.queryByRole('group', { name: 'Edit Buy milk' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit Buy milk' })).toHaveFocus();
    expect(screen.getByText('Buy milk')).toBeInTheDocument();
  });

  it('closes without a request when nothing changed', async () => {
    const user = userEvent.setup();
    await renderWith([todo]);
    await openPanel(user);

    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    // `{}` would be a 400 empty_update, so no call is made at all.
    expect(mockUpdateTodo).not.toHaveBeenCalled();
    expect(screen.queryByRole('group', { name: 'Edit Buy milk' })).not.toBeInTheDocument();
  });

  it('shows the save-failure copy and keeps the panel open', async () => {
    const user = userEvent.setup();
    mockUpdateTodo.mockRejectedValue(new Error('boom'));
    await renderWith([todo]);
    await openPanel(user);

    await user.selectOptions(within(panel()).getByLabelText('Priority'), 'high');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not save changes. Please try again.');
    expect(panel()).toBeInTheDocument();
    expect(within(panel()).getByLabelText('Priority')).toHaveValue('high');
  });
});

describe('adding a subtask', () => {
  it('posts to the subtask endpoint and updates the progress counter', async () => {
    const user = userEvent.setup();
    await renderWith([makeTodo({ id: 'id-1', title: 'Buy milk' })]);

    await user.click(screen.getByRole('button', { name: 'Edit Buy milk' }));
    expect(screen.getByRole('heading', { name: 'Subtasks', level: 3 })).toBeInTheDocument();

    mockCreateSubtask.mockResolvedValue(
      makeTodo({ id: 'sub-1', title: 'Find the shop', parent_id: 'id-1' }),
    );
    const input = screen.getByLabelText('New subtask title');
    expect(input).toHaveAttribute('placeholder', 'Add a subtask');
    await user.type(input, 'Find the shop');
    await user.click(screen.getByRole('button', { name: 'Add subtask' }));

    await waitFor(() =>
      expect(mockCreateSubtask).toHaveBeenCalledWith('id-1', { title: 'Find the shop' }),
    );
    expect(await screen.findByText('0/1 subtasks')).toBeInTheDocument();
    expect(input).toHaveValue('');

    mockSetCompleted.mockResolvedValue(
      makeTodo({ id: 'sub-1', title: 'Find the shop', parent_id: 'id-1', completed: true }),
    );
    await user.click(screen.getByRole('checkbox', { name: 'Mark Find the shop as completed' }));

    expect(await screen.findByText('1/1 subtasks')).toBeInTheDocument();
  });

  it('never disables the field while the add is in flight (C7)', async () => {
    const user = userEvent.setup();
    let resolveAdd!: (todo: Todo) => void;
    mockCreateSubtask.mockReturnValue(
      new Promise((resolve) => {
        resolveAdd = resolve;
      }),
    );
    await renderWith([makeTodo({ id: 'id-1', title: 'Buy milk' })]);

    await user.click(screen.getByRole('button', { name: 'Edit Buy milk' }));
    const input = screen.getByLabelText('New subtask title');
    await user.type(input, 'Find the shop{Enter}');

    expect(input).not.toBeDisabled();
    expect(input).toHaveAttribute('aria-disabled', 'true');
    expect(input).toHaveFocus();

    resolveAdd(makeTodo({ id: 'sub-1', title: 'Find the shop', parent_id: 'id-1' }));
    await screen.findByText('0/1 subtasks');
    expect(input).toHaveFocus();
  });

  it('shows the subtask failure copy when the add fails', async () => {
    const user = userEvent.setup();
    mockCreateSubtask.mockRejectedValue(new Error('boom'));
    await renderWith([makeTodo({ id: 'id-1', title: 'Buy milk' })]);

    await user.click(screen.getByRole('button', { name: 'Edit Buy milk' }));
    await user.type(screen.getByLabelText('New subtask title'), 'Find the shop');
    await user.click(screen.getByRole('button', { name: 'Add subtask' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not update the subtask. Please try again.',
    );
  });
});

describe('creating an enriched todo', () => {
  it('sends the priority, due date and tags chosen in the add form', async () => {
    const user = userEvent.setup();
    const { createTodo } = await import('./api/client');
    const mockCreateTodo = vi.mocked(createTodo);
    mockCreateTodo.mockResolvedValue(makeTodo({ id: 'new-id', title: 'New task' }));

    await renderWith([]);

    // Priority, due date and tags live behind the composer's disclosure.
    await user.click(screen.getByRole('button', { name: 'More options' }));
    await user.selectOptions(screen.getByLabelText('New todo priority'), 'high');
    fireEvent.change(screen.getByLabelText('New todo due date'), {
      target: { value: '2026-12-01' },
    });
    await user.type(screen.getByLabelText('New todo tags'), 'Home{Enter}errand{Enter}');
    await user.type(screen.getByLabelText('New todo title'), 'New task{Enter}');

    await waitFor(() =>
      expect(mockCreateTodo).toHaveBeenCalledWith({
        title: 'New task',
        priority: 'high',
        due_date: '2026-12-01',
        tags: ['home', 'errand'],
        list_id: 'list-1',
      }),
    );
  });
});
