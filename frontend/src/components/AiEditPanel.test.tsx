import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AiEditChangeSet, AiEditResult } from '../api/ai';
import {
  AI_RATE_LIMIT_MESSAGE,
  AI_TIMEOUT_MESSAGE,
  AI_UNAVAILABLE_MESSAGE,
  AiTimeoutError,
  PartialApplyError,
  editTodo,
} from '../api/ai';
import { ApiError } from '../api/errors';
import type { Todo } from '../api/types';
import { todayString } from '../dates';
import { makeTodo } from '../test/helpers';
import TodoItem from './TodoItem';
import {
  EDIT_APPLY_ERROR_MESSAGE,
  NO_CHANGES_MESSAGE,
  SUBTASKS_TRUNCATED_NOTE,
} from './AiEditPanel';

vi.mock('../api/ai', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/ai')>();
  return { ...actual, editTodo: vi.fn() };
});

const mockEditTodo = vi.mocked(editTodo);

/** The §2.1 example, resolved: ten individually deselectable changes. */
const FULL_CHANGE_SET: AiEditChangeSet = {
  title: { from: 'Dentist', to: 'Call the dentist' },
  description: { from: 'Ask about the crown', to: null },
  priority: { from: 'medium', to: 'high' },
  due_date: { from: null, to: '2026-09-11' },
  completed: null,
  tags: { from: ['home'], to: ['health'], added: ['health'], removed: ['home'] },
  subtasks: [
    { action: 'rename', id: 'sub-1', title: 'Book the appointment', from_title: 'Book it' },
    { action: 'complete', id: 'sub-2', title: null, from_title: 'Pay the invoice' },
    { action: 'remove', id: 'sub-3', title: null, from_title: 'Old step' },
    { action: 'add', id: null, title: 'Find the phone number', from_title: null },
  ],
};

/** Every label the preview may render, exactly as spec §3.3 froze them. */
const LABELS = {
  title: 'Title: Dentist → Call the dentist',
  description: 'Description: remove “Ask about the crown”',
  priority: 'Priority: Medium → High',
  dueDate: 'Due date: none → 11 Sep 2026',
  addTag: 'Add tag health',
  removeTag: 'Remove tag home',
  rename: 'Rename subtask: Book it → Book the appointment',
  complete: 'Complete subtask: Pay the invoice',
  remove: 'Remove subtask: Old step',
  add: 'Add subtask: Find the phone number',
};

function makeResult(
  changeSet: Partial<AiEditChangeSet> = {},
  truncated = false,
): AiEditResult {
  return {
    todo_id: 'id-1',
    empty: false,
    context: { subtasks_truncated: truncated },
    change_set: {
      title: null,
      description: null,
      priority: null,
      due_date: null,
      completed: null,
      tags: null,
      subtasks: [],
      ...changeSet,
    },
  };
}

function emptyResult(): AiEditResult {
  const result = makeResult();
  return { ...result, empty: true };
}

interface RowHarness {
  onApplyAiEdit: ReturnType<typeof vi.fn>;
  todo: Todo;
}

/** Renders one real todo row, which is where the panel actually lives. */
function renderRow(overrides: Partial<Todo> = {}): RowHarness {
  const todo = makeTodo({ id: 'id-1', title: 'Buy milk', ...overrides });
  const onApplyAiEdit = vi.fn().mockResolvedValue(undefined);
  render(
    <ul>
      <TodoItem
        todo={todo}
        today="2026-09-10"
        busyIds={new Set()}
        onToggle={vi.fn()}
        onDelete={vi.fn()}
        onSave={vi.fn()}
        onAddSubtask={vi.fn()}
        onToggleSubtask={vi.fn()}
        onDeleteSubtask={vi.fn()}
        onAddSubtasks={vi.fn()}
        onApplyMetadata={vi.fn()}
        onApplyAiEdit={onApplyAiEdit}
        aiAvailable
      />
    </ul>,
  );
  return { onApplyAiEdit, todo };
}

async function openPanel(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  await user.click(screen.getByRole('button', { name: 'AI actions for Buy milk' }));
  await user.click(screen.getByRole('menuitem', { name: 'Edit with AI: Buy milk' }));
  return screen.getByRole('region', { name: 'Edit Buy milk with AI' });
}

/** Types an instruction and waits for the preview to replace the form. */
async function ask(
  user: ReturnType<typeof userEvent.setup>,
  instruction = 'rename it to Call the dentist',
): Promise<void> {
  await user.type(screen.getByLabelText('What should the AI change?'), instruction);
  await user.click(screen.getByRole('button', { name: 'Ask the AI' }));
}

beforeEach(() => {
  vi.resetAllMocks();
});

describe('the instruction stage', () => {
  it('is offered as a third menu item and opens with focus in the textarea', async () => {
    const user = userEvent.setup();
    renderRow();

    const panel = await openPanel(user);

    const textarea = within(panel).getByLabelText('What should the AI change?');
    expect(textarea).toHaveFocus();
    expect(textarea).toHaveAttribute('maxlength', '500');
    expect(textarea).toHaveAccessibleDescription(
      'This can take up to a minute on a slow machine.',
    );
    expect(within(panel).getByRole('button', { name: 'Ask the AI' })).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    // Nothing has been asked for, let alone written.
    expect(mockEditTodo).not.toHaveBeenCalled();
  });

  it('asks with the trimmed instruction and the caller local date', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult({ title: FULL_CHANGE_SET.title }));
    renderRow();
    await openPanel(user);

    await ask(user, '  mark it done  ');

    await waitFor(() => expect(mockEditTodo).toHaveBeenCalledTimes(1));
    expect(mockEditTodo).toHaveBeenCalledWith(
      'id-1',
      'mark it done',
      todayString(),
      expect.any(AbortSignal),
    );
  });

  it('does not ask for an empty instruction', async () => {
    const user = userEvent.setup();
    renderRow();
    await openPanel(user);

    await user.click(screen.getByRole('button', { name: 'Ask the AI' }));

    expect(mockEditTodo).not.toHaveBeenCalled();
  });

  it('keeps the instruction when the AI found nothing to change', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(emptyResult());
    renderRow();
    await openPanel(user);

    await ask(user, 'delete this todo');

    const notice = await screen.findByText(NO_CHANGES_MESSAGE);
    expect(notice).toHaveAttribute('role', 'status');
    expect(screen.getByLabelText('What should the AI change?')).toHaveValue('delete this todo');
    expect(screen.queryByRole('heading', { name: 'Suggested changes' })).not.toBeInTheDocument();
  });

  it('returns focus to the AI menu when cancelled, without asking anything', async () => {
    const user = userEvent.setup();
    renderRow();
    await openPanel(user);

    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(
      screen.queryByRole('region', { name: 'Edit Buy milk with AI' }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'AI actions for Buy milk' })).toHaveFocus();
    expect(mockEditTodo).not.toHaveBeenCalled();
  });
});

describe('the change-set preview', () => {
  it('renders one checked checkbox per change, labelled exactly as specified', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult(FULL_CHANGE_SET));
    renderRow();
    await openPanel(user);

    await ask(user);

    expect(await screen.findByRole('heading', { name: 'Suggested changes' })).toBeInTheDocument();
    for (const label of Object.values(LABELS)) {
      const checkbox = screen.getByRole('checkbox', { name: label });
      expect(checkbox).toBeChecked();
      // Label in Name (WCAG 2.5.3): the visible text *is* the accessible name.
      expect(screen.getByText(label)).toHaveAttribute('for', checkbox.getAttribute('id'));
    }
    expect(screen.getByRole('button', { name: 'Apply 10 changes' })).toBeInTheDocument();
  });

  it('marks every removal with the word Remove, not with colour alone', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult(FULL_CHANGE_SET));
    renderRow();
    await openPanel(user);

    await ask(user);
    await screen.findByRole('heading', { name: 'Suggested changes' });

    for (const label of [LABELS.removeTag, LABELS.remove, LABELS.description]) {
      const text = screen.getByText(label);
      expect(text.className).toContain('text-red-700');
      expect(label.toLowerCase()).toContain('remove');
    }
  });

  /** The two label forms the ten-change fixture cannot show at the same time. */
  it('labels a written description and a reopened subtask', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(
      makeResult({
        description: { from: null, to: 'Ask about the crown' },
        subtasks: [{ action: 'reopen', id: 'sub-4', title: null, from_title: 'Call back' }],
      }),
    );
    renderRow();
    await openPanel(user);

    await ask(user, 'add a note and reopen the last step');

    for (const label of ['Description: → Ask about the crown', 'Reopen subtask: Call back']) {
      const checkbox = await screen.findByRole('checkbox', { name: label });
      expect(checkbox).toBeChecked();
      expect(screen.getByText(label)).toHaveAttribute('for', checkbox.getAttribute('id'));
      // Neither is a removal, so neither is coloured as one.
      expect(screen.getByText(label).className).not.toContain('text-red-700');
    }
    expect(screen.getByRole('button', { name: 'Apply 2 changes' })).toBeInTheDocument();
  });

  it('spells a completion toggle out in words', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult({ completed: { from: false, to: true } }));
    renderRow();
    await openPanel(user);

    await ask(user, 'mark it done');

    expect(await screen.findByRole('checkbox', { name: 'Mark as completed' })).toBeChecked();
    expect(screen.getByRole('button', { name: 'Apply 1 changes' })).toBeInTheDocument();
  });

  it('notes a snapshot that was truncated at 20 subtasks', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult({ title: FULL_CHANGE_SET.title }, true));
    renderRow();
    await openPanel(user);

    await ask(user);

    expect(await screen.findByText(SUBTASKS_TRUNCATED_NOTE)).toBeInTheDocument();
  });

  it('applies only what is still checked, and counts it honestly', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult(FULL_CHANGE_SET));
    const { onApplyAiEdit } = renderRow();
    await openPanel(user);

    await ask(user);
    await screen.findByRole('heading', { name: 'Suggested changes' });

    await user.click(screen.getByRole('checkbox', { name: LABELS.priority }));
    await user.click(screen.getByRole('checkbox', { name: LABELS.removeTag }));
    await user.click(screen.getByRole('checkbox', { name: LABELS.remove }));
    expect(screen.getByRole('checkbox', { name: LABELS.priority })).not.toBeChecked();

    await user.click(screen.getByRole('button', { name: 'Apply 7 changes' }));

    await waitFor(() => expect(onApplyAiEdit).toHaveBeenCalledTimes(1));
    expect(onApplyAiEdit).toHaveBeenCalledWith('id-1', {
      title: FULL_CHANGE_SET.title,
      description: FULL_CHANGE_SET.description,
      priority: null,
      due_date: FULL_CHANGE_SET.due_date,
      completed: null,
      // The deselected removal is gone, and `to` is recomputed to match: the
      // tag the user kept must not be dropped by the tag the AI added.
      tags: { from: ['home'], to: ['home', 'health'], added: ['health'], removed: [] },
      subtasks: [
        FULL_CHANGE_SET.subtasks[0],
        FULL_CHANGE_SET.subtasks[1],
        FULL_CHANGE_SET.subtasks[3],
      ],
    });
  });

  it('sends the server final tag list when nothing was deselected', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult({ tags: FULL_CHANGE_SET.tags }));
    const { onApplyAiEdit } = renderRow();
    await openPanel(user);

    await ask(user, 'tag it health instead of home');
    await user.click(await screen.findByRole('button', { name: 'Apply 2 changes' }));

    await waitFor(() => expect(onApplyAiEdit).toHaveBeenCalledTimes(1));
    expect(onApplyAiEdit.mock.calls[0][1].tags.to).toEqual(['health']);
  });

  /**
   * Review round 1, blocking 1. A todo already at the 10-tag cap, where the AI
   * pays for its new tag with a removal: unchecking the removal leaves no room,
   * and the add must then leave the count too — otherwise `Apply 1 changes`
   * would PATCH a byte-identical tag list and close as a success.
   */
  it('never counts an added tag that does not fit under the 10-tag cap', async () => {
    const user = userEvent.setup();
    const full = Array.from({ length: 10 }, (_, index) => `tag${index}`);
    mockEditTodo.mockResolvedValue(
      makeResult({
        tags: {
          from: full,
          to: [...full.filter((name) => name !== 'tag0'), 'health'],
          added: ['health'],
          removed: ['tag0'],
        },
      }),
    );
    const { onApplyAiEdit } = renderRow();
    await openPanel(user);

    await ask(user, 'tag it health');
    // Both proposed changes fit as long as the removal stands.
    expect(await screen.findByRole('button', { name: 'Apply 2 changes' })).toBeInTheDocument();

    await user.click(screen.getByRole('checkbox', { name: 'Remove tag tag0' }));

    const apply = screen.getByRole('button', { name: 'Apply 0 changes' });
    expect(apply).toHaveAttribute('aria-disabled', 'true');
    // The row stays visible and checked, with a reason next to it.
    expect(screen.getByRole('checkbox', { name: 'Add tag health' })).toBeChecked();
    expect(
      screen.getByText(
        'A todo can have at most 10 tags, so “health” will not be added. Keep a tag removal checked to make room.',
      ),
    ).toHaveAttribute('role', 'status');

    await user.click(apply);
    expect(onApplyAiEdit).not.toHaveBeenCalled();
  });

  it('still adds a tag that fits once a removal is kept', async () => {
    const user = userEvent.setup();
    const full = Array.from({ length: 10 }, (_, index) => `tag${index}`);
    mockEditTodo.mockResolvedValue(
      makeResult({
        title: FULL_CHANGE_SET.title,
        tags: {
          from: full,
          to: [...full.filter((name) => name !== 'tag0'), 'health'],
          added: ['health'],
          removed: ['tag0'],
        },
      }),
    );
    const { onApplyAiEdit } = renderRow();
    await openPanel(user);

    await ask(user, 'tag it health');
    // Deselecting an unrelated change still takes the partial-selection path.
    await user.click(await screen.findByRole('checkbox', { name: LABELS.title }));
    await user.click(screen.getByRole('button', { name: 'Apply 2 changes' }));

    await waitFor(() => expect(onApplyAiEdit).toHaveBeenCalledTimes(1));
    const applied = onApplyAiEdit.mock.calls[0][1].tags;
    expect(applied.added).toEqual(['health']);
    expect(applied.to).toHaveLength(10);
    expect(applied.to).toContain('health');
    expect(applied.to).not.toContain('tag0');
  });

  it('is inert once every change has been unchecked', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult({ title: FULL_CHANGE_SET.title }));
    const { onApplyAiEdit } = renderRow();
    await openPanel(user);

    await ask(user);
    await user.click(await screen.findByRole('checkbox', { name: LABELS.title }));

    const apply = screen.getByRole('button', { name: 'Apply 0 changes' });
    expect(apply).toHaveAttribute('aria-disabled', 'true');
    await user.click(apply);
    expect(onApplyAiEdit).not.toHaveBeenCalled();
  });

  it('goes back to the instruction with the text intact', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult({ title: FULL_CHANGE_SET.title }));
    renderRow();
    await openPanel(user);

    await ask(user, 'rename it');
    await user.click(await screen.findByRole('button', { name: 'Back' }));

    const textarea = screen.getByLabelText('What should the AI change?');
    expect(textarea).toHaveValue('rename it');
    expect(textarea).toHaveFocus();
    expect(screen.queryByRole('heading', { name: 'Suggested changes' })).not.toBeInTheDocument();
  });

  it('closes and returns focus to the menu after a successful apply', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult({ title: FULL_CHANGE_SET.title }));
    renderRow();
    await openPanel(user);

    await ask(user);
    await user.click(await screen.findByRole('button', { name: 'Apply 1 changes' }));

    await waitFor(() =>
      expect(screen.queryByRole('region', { name: 'Edit Buy milk with AI' })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: 'AI actions for Buy milk' })).toHaveFocus();
  });
});

describe('failures', () => {
  it.each([
    [new ApiError(503, 'ai_unavailable', 'Down'), AI_UNAVAILABLE_MESSAGE],
    [new ApiError(503, 'ai_disabled', 'Off'), AI_UNAVAILABLE_MESSAGE],
    [new ApiError(504, 'ai_timeout', 'Slow'), AI_UNAVAILABLE_MESSAGE],
    [new ApiError(429, 'rate_limited', 'Too many'), AI_RATE_LIMIT_MESSAGE],
    [new AiTimeoutError(), AI_TIMEOUT_MESSAGE],
  ])('reuses the existing copy for %s', async (error, message) => {
    const user = userEvent.setup();
    mockEditTodo.mockRejectedValue(error);
    renderRow();
    await openPanel(user);

    await ask(user);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(message);
    // The instruction survives, so the user can simply try again.
    expect(screen.getByRole('button', { name: 'Ask the AI' })).toBeInTheDocument();
  });

  it('keeps the preview when an apply failed before writing anything', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult({ title: FULL_CHANGE_SET.title }));
    const { onApplyAiEdit } = renderRow();
    onApplyAiEdit.mockRejectedValue(new ApiError(500, null, 'boom'));
    await openPanel(user);

    await ask(user);
    await user.click(await screen.findByRole('button', { name: 'Apply 1 changes' }));

    expect(await screen.findByText(EDIT_APPLY_ERROR_MESSAGE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply 1 changes' })).toBeInTheDocument();
  });

  it('is terminal after a partial apply: no retry, only Close', async () => {
    const user = userEvent.setup();
    mockEditTodo.mockResolvedValue(makeResult(FULL_CHANGE_SET));
    const { onApplyAiEdit } = renderRow();
    onApplyAiEdit.mockRejectedValue(new PartialApplyError(2, 5));
    await openPanel(user);

    await ask(user);
    await user.click(await screen.findByRole('button', { name: 'Apply 10 changes' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Applied 2 of 5 changes. Please try again.');
    const close = screen.getByRole('button', { name: 'Close' });
    expect(close).toHaveFocus();
    expect(screen.queryByRole('button', { name: /^Apply/ })).not.toBeInTheDocument();
    const panel = screen.getByRole('region', { name: 'Edit Buy milk with AI' });
    expect(within(panel).queryByRole('checkbox')).not.toBeInTheDocument();

    await user.click(close);
    expect(screen.getByRole('button', { name: 'AI actions for Buy milk' })).toHaveFocus();
    expect(onApplyAiEdit).toHaveBeenCalledTimes(1);
  });
});
