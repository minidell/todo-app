import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AI_AUTH_FAILED_BANNER,
  AI_MODEL_UNAVAILABLE_BANNER,
  AI_RATE_LIMIT_MESSAGE,
  AI_TIMEOUT_MESSAGE,
  AI_UNAVAILABLE_BANNER,
  AI_UNAVAILABLE_MESSAGE,
  AiTimeoutError,
  dailySummary,
  editTodo,
  getAiStatus,
  parseTodo,
  suggestMetadata,
  suggestSubtasks,
} from './api/ai';
import type { AiEditChangeSet, AiEditResult } from './api/ai';
import { createSubtask, createTodo, deleteTodo, listTodos, updateTodo } from './api/client';
import { listLists } from './api/lists';
import { listTags } from './api/tags';
import { me } from './api/auth';
import { ApiError } from './api/errors';
import { openEventStream } from './api/events';
import { EMPTY_SUMMARY_MESSAGE } from './components/DailySummaryPanel';
import { NO_SUBTASKS_MESSAGE } from './components/AiSuggestionPanel';
import { todayString } from './dates';
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
vi.mock('./api/events', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api/events')>();
  return { ...actual, openEventStream: vi.fn() };
});
vi.mock('./api/ai', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api/ai')>();
  return {
    ...actual,
    getAiStatus: vi.fn(),
    parseTodo: vi.fn(),
    suggestSubtasks: vi.fn(),
    suggestMetadata: vi.fn(),
    dailySummary: vi.fn(),
    editTodo: vi.fn(),
  };
});

const mockListTodos = vi.mocked(listTodos);
const mockCreateTodo = vi.mocked(createTodo);
const mockCreateSubtask = vi.mocked(createSubtask);
const mockDeleteTodo = vi.mocked(deleteTodo);
const mockUpdateTodo = vi.mocked(updateTodo);
const mockListLists = vi.mocked(listLists);
const mockListTags = vi.mocked(listTags);
const mockMe = vi.mocked(me);
const mockOpen = vi.mocked(openEventStream);
const mockGetAiStatus = vi.mocked(getAiStatus);
const mockParseTodo = vi.mocked(parseTodo);
const mockSuggestSubtasks = vi.mocked(suggestSubtasks);
const mockSuggestMetadata = vi.mocked(suggestMetadata);
const mockDailySummary = vi.mocked(dailySummary);
const mockEditTodo = vi.mocked(editTodo);

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  seedStoredSession();

  // The stream stays open for the whole test; realtime is not what we mock here.
  mockOpen.mockImplementation(() => new Promise<void>(() => {}));
  mockMe.mockResolvedValue(makeUser());
  mockListLists.mockResolvedValue([makeList()]);
  mockListTags.mockResolvedValue([]);
  mockListTodos.mockResolvedValue(makeTodoPage([makeTodo({ id: 'id-1', title: 'Buy milk' })]));
  mockGetAiStatus.mockResolvedValue({
    enabled: true,
    available: true,
    model: 'qwen2.5:3b',
    reason: null,
  });
});

async function renderSignedIn(): Promise<void> {
  renderApp();
  await screen.findByRole('heading', { name: 'Todos', level: 1 });
  await screen.findByText('Buy milk');
  await waitFor(() => expect(mockGetAiStatus).toHaveBeenCalled());
}

/** The natural-language box is the AI mode of the composer (iteration 3). */
async function openAiComposer(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(await screen.findByRole('tab', { name: 'Draft with AI' }));
}

/** The per-row AI helpers live behind one menu button (iteration 3). */
async function openRowAiMenu(
  user: ReturnType<typeof userEvent.setup>,
  title: string,
): Promise<void> {
  await user.click(screen.getByRole('button', { name: `AI actions for ${title}` }));
}

/** "Today at a glance" is a card that starts collapsed (iteration 3). */
async function openDailySummary(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(screen.getByRole('button', { name: 'Today at a glance' }));
}

describe('AI availability', () => {
  it('renders nothing at all when AI is disabled', async () => {
    mockGetAiStatus.mockResolvedValue({
      enabled: false,
      available: false,
      model: null,
      reason: 'disabled',
    });

    await renderSignedIn();

    expect(screen.queryByRole('tab', { name: 'Draft with AI' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Today at a glance' })).not.toBeInTheDocument();
    expect(screen.queryByText(AI_UNAVAILABLE_BANNER)).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'AI actions for Buy milk' }),
    ).not.toBeInTheDocument();
    // The manual controls are untouched.
    expect(screen.getByRole('textbox', { name: 'New todo title' })).toBeEnabled();
  });

  it('hides the section when the status call itself fails', async () => {
    mockGetAiStatus.mockRejectedValue(new Error('network'));

    await renderSignedIn();

    expect(screen.queryByRole('tab', { name: 'Draft with AI' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Today at a glance' })).not.toBeInTheDocument();
  });

  it('explains itself and goes inert when the model is unreachable', async () => {
    const user = userEvent.setup();
    mockGetAiStatus.mockResolvedValue({
      enabled: true,
      available: false,
      model: 'qwen2.5:3b',
      reason: 'unreachable',
    });

    await renderSignedIn();
    await openAiComposer(user);

    expect(await screen.findByText(AI_UNAVAILABLE_BANNER)).toBeInTheDocument();
    const draftButton = screen.getByRole('button', { name: 'Draft with AI' });
    expect(draftButton).toHaveAttribute('aria-disabled', 'true');
    await openDailySummary(user);
    expect(screen.getByRole('button', { name: 'Generate summary' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    // The per-row AI menu is not offered at all while the model is down.
    expect(
      screen.queryByRole('button', { name: 'AI actions for Buy milk' }),
    ).not.toBeInTheDocument();

    await user.type(
      screen.getByLabelText('Describe a todo in your own words'),
      'call the dentist',
    );
    await user.click(draftButton);
    await user.click(screen.getByRole('button', { name: 'Generate summary' }));

    expect(mockParseTodo).not.toHaveBeenCalled();
    expect(mockDailySummary).not.toHaveBeenCalled();
  });

  /**
   * IT3-2 client half. `/api/ai/status.reason` says *why* the AI is down; the
   * two banners that used to read the same now differ, because the two states
   * ask different things of the reader: one is an operator misconfiguration,
   * the other simply takes time.
   */
  it('names a credential mismatch in the composer and in the summary card', async () => {
    const user = userEvent.setup();
    mockGetAiStatus.mockResolvedValue({
      enabled: true,
      available: false,
      model: 'qwen2.5:3b',
      reason: 'auth_failed',
    });

    await renderSignedIn();
    await openAiComposer(user);

    expect(await screen.findByText(AI_AUTH_FAILED_BANNER)).toBeInTheDocument();
    expect(screen.queryByText(AI_UNAVAILABLE_BANNER)).not.toBeInTheDocument();

    await openDailySummary(user);
    const card = screen.getByRole('button', { name: 'Generate summary' }).closest('section');
    expect(within(card as HTMLElement).getByText(AI_AUTH_FAILED_BANNER)).toHaveAttribute(
      'role',
      'status',
    );
  });

  it('says the model is still starting when it has not been pulled yet', async () => {
    const user = userEvent.setup();
    mockGetAiStatus.mockResolvedValue({
      enabled: true,
      available: false,
      model: 'qwen2.5:3b',
      reason: 'model_unavailable',
    });

    await renderSignedIn();
    await openAiComposer(user);

    expect(await screen.findByText(AI_MODEL_UNAVAILABLE_BANNER)).toBeInTheDocument();
    expect(screen.queryByText(AI_UNAVAILABLE_BANNER)).not.toBeInTheDocument();
  });

  it('keeps the generic banner for an unknown reason the server may add later', async () => {
    const user = userEvent.setup();
    mockGetAiStatus.mockResolvedValue({
      enabled: true,
      available: false,
      model: 'qwen2.5:3b',
      // What `getAiStatus` produces for a value it does not recognise.
      reason: null,
    });

    await renderSignedIn();
    await openAiComposer(user);

    expect(await screen.findByText(AI_UNAVAILABLE_BANNER)).toBeInTheDocument();
    expect(screen.queryByText(AI_AUTH_FAILED_BANNER)).not.toBeInTheDocument();
    expect(screen.queryByText(AI_MODEL_UNAVAILABLE_BANNER)).not.toBeInTheDocument();
  });

  it('renders no banner at all when the whole AI section is switched off', async () => {
    mockGetAiStatus.mockResolvedValue({
      enabled: false,
      available: false,
      model: null,
      reason: 'disabled',
    });

    await renderSignedIn();

    expect(screen.queryByText(AI_UNAVAILABLE_BANNER)).not.toBeInTheDocument();
    expect(screen.queryByText(AI_AUTH_FAILED_BANNER)).not.toBeInTheDocument();
    expect(screen.queryByText(AI_MODEL_UNAVAILABLE_BANNER)).not.toBeInTheDocument();
  });
});

describe('drafting a todo from natural language', () => {
  const draft = {
    title: 'Call the dentist',
    description: null,
    priority: 'high' as const,
    due_date: '2026-09-03',
    tags: ['health'],
    subtasks: [{ title: 'Find the phone number' }, { title: 'Check the calendar' }],
  };

  it('parses the note, prefills the draft and creates only what is checked', async () => {
    const user = userEvent.setup();
    mockParseTodo.mockResolvedValue(draft);
    mockCreateTodo.mockResolvedValue(makeTodo({ id: 'id-2', title: 'Call the dentist' }));
    mockCreateSubtask.mockResolvedValue(
      makeTodo({ id: 'sub-1', title: 'Find the phone number', parent_id: 'id-2' }),
    );
    await renderSignedIn();
    await openAiComposer(user);

    await user.type(
      screen.getByLabelText('Describe a todo in your own words'),
      'call the dentist tomorrow, urgent',
    );
    await user.click(screen.getByRole('button', { name: 'Draft with AI' }));

    expect(mockParseTodo).toHaveBeenCalledWith(
      'call the dentist tomorrow, urgent',
      todayString(),
      expect.any(AbortSignal),
    );

    const preview = (await screen.findByRole('heading', { name: 'Suggested todo' })).closest(
      'section',
    ) as HTMLElement;
    const fields = within(preview);
    expect(fields.getByLabelText('Title')).toHaveValue('Call the dentist');
    expect(fields.getByLabelText('Priority')).toHaveValue('high');
    expect(fields.getByLabelText('Due date')).toHaveValue('2026-09-03');
    expect(fields.getByText('health')).toBeInTheDocument();
    // Focus lands on the field the user is most likely to correct.
    expect(fields.getByLabelText('Title')).toHaveFocus();

    const both = fields.getByLabelText('Include subtask Find the phone number');
    expect(both).toBeChecked();
    await user.click(fields.getByLabelText('Include subtask Check the calendar'));

    await user.clear(fields.getByLabelText('Title'));
    await user.type(fields.getByLabelText('Title'), 'Call the dentist today');
    await user.click(fields.getByRole('button', { name: 'Add this todo' }));

    await waitFor(() => expect(mockCreateTodo).toHaveBeenCalledTimes(1));
    expect(mockCreateTodo).toHaveBeenCalledWith({
      title: 'Call the dentist today',
      description: null,
      priority: 'high',
      due_date: '2026-09-03',
      tags: ['health'],
      list_id: 'list-1',
    });
    expect(mockCreateSubtask).toHaveBeenCalledTimes(1);
    expect(mockCreateSubtask).toHaveBeenCalledWith('id-2', { title: 'Find the phone number' });

    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Suggested todo' })).not.toBeInTheDocument(),
    );
  });

  it('discards a suggestion without writing anything and restores focus', async () => {
    const user = userEvent.setup();
    mockParseTodo.mockResolvedValue(draft);
    await renderSignedIn();
    await openAiComposer(user);

    await user.type(screen.getByLabelText('Describe a todo in your own words'), 'dentist');
    await user.click(screen.getByRole('button', { name: 'Draft with AI' }));
    await screen.findByRole('heading', { name: 'Suggested todo' });

    await user.click(screen.getByRole('button', { name: 'Discard suggestion' }));

    expect(screen.queryByRole('heading', { name: 'Suggested todo' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Draft with AI' })).toHaveFocus();
    expect(mockCreateTodo).not.toHaveBeenCalled();
    expect(mockCreateSubtask).not.toHaveBeenCalled();
  });
});

describe('splitting a todo into subtasks', () => {
  it('creates one subtask per checked suggestion', async () => {
    const user = userEvent.setup();
    mockSuggestSubtasks.mockResolvedValue(['Find the shop', 'Pay', 'Carry it home']);
    mockCreateSubtask.mockResolvedValue(
      makeTodo({ id: 'sub-1', title: 'Find the shop', parent_id: 'id-1' }),
    );
    await renderSignedIn();

    await openRowAiMenu(user, 'Buy milk');
    await user.click(screen.getByRole('menuitem', { name: 'Split into subtasks: Buy milk' }));

    const panel = await screen.findByRole('region', {
      name: 'AI subtask suggestions for Buy milk',
    });
    expect(mockSuggestSubtasks).toHaveBeenCalledWith('id-1', 5, expect.any(AbortSignal));
    const items = within(panel);
    expect(items.getByLabelText('Add subtask Find the shop')).toBeChecked();
    await user.click(items.getByLabelText('Add subtask Pay'));

    await user.click(items.getByRole('button', { name: 'Add selected subtasks' }));

    await waitFor(() => expect(mockCreateSubtask).toHaveBeenCalledTimes(2));
    expect(mockCreateSubtask).toHaveBeenNthCalledWith(1, 'id-1', { title: 'Find the shop' });
    expect(mockCreateSubtask).toHaveBeenNthCalledWith(2, 'id-1', { title: 'Carry it home' });
    // The panel closes and hands focus back to the menu that opened it.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'AI actions for Buy milk' })).toHaveFocus(),
    );
  });

  it('says so when the AI has nothing to suggest', async () => {
    const user = userEvent.setup();
    mockSuggestSubtasks.mockResolvedValue([]);
    await renderSignedIn();

    await openRowAiMenu(user, 'Buy milk');
    await user.click(screen.getByRole('menuitem', { name: 'Split into subtasks: Buy milk' }));

    expect(await screen.findByText(NO_SUBTASKS_MESSAGE)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Add selected subtasks' }),
    ).not.toBeInTheDocument();
    expect(mockCreateSubtask).not.toHaveBeenCalled();
  });
});

describe('suggesting priority and tags', () => {
  it('applies exactly the priority and the tags in one PATCH', async () => {
    const user = userEvent.setup();
    mockSuggestMetadata.mockResolvedValue({ priority: 'high', tags: ['work', 'urgent'] });
    mockUpdateTodo.mockResolvedValue(
      makeTodo({ id: 'id-1', title: 'Buy milk', priority: 'high', tags: ['work', 'urgent'] }),
    );
    await renderSignedIn();

    await openRowAiMenu(user, 'Buy milk');
    await user.click(
      screen.getByRole('menuitem', { name: 'Suggest priority and tags: Buy milk' }),
    );

    expect(
      await screen.findByText('Suggested priority: High. Suggested tags: work, urgent.'),
    ).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Apply suggestion' }));

    await waitFor(() => expect(mockUpdateTodo).toHaveBeenCalledTimes(1));
    expect(mockUpdateTodo).toHaveBeenCalledWith('id-1', {
      priority: 'high',
      tags: ['work', 'urgent'],
    });
  });

  it('writes nothing when the suggestion is dismissed', async () => {
    const user = userEvent.setup();
    mockSuggestMetadata.mockResolvedValue({ priority: 'low', tags: [] });
    await renderSignedIn();

    await openRowAiMenu(user, 'Buy milk');
    await user.click(
      screen.getByRole('menuitem', { name: 'Suggest priority and tags: Buy milk' }),
    );
    expect(
      await screen.findByText('Suggested priority: Low. Suggested tags: none.'),
    ).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Dismiss' }));

    expect(mockUpdateTodo).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'AI actions for Buy milk' })).toHaveFocus(),
    );
  });
});

/**
 * Iteration 5. The change set is a draft like every other AI answer (D-AI1);
 * confirming it writes through the ordinary endpoints only (D-IT5-1), which is
 * what these tests pin: one PATCH for all field changes, then one call per
 * subtask operation in the server's order, then one quiet refetch.
 */
describe('editing a todo by instruction', () => {
  const CHANGE_SET: AiEditChangeSet = {
    title: { from: 'Buy milk', to: 'Call the dentist' },
    description: null,
    priority: null,
    due_date: null,
    completed: null,
    tags: null,
    subtasks: [
      { action: 'rename', id: 'sub-1', title: 'Book the appointment', from_title: 'Book it' },
      { action: 'complete', id: 'sub-2', title: null, from_title: 'Pay the invoice' },
      { action: 'remove', id: 'sub-3', title: null, from_title: 'Old step' },
      { action: 'add', id: null, title: 'Find the phone number', from_title: null },
    ],
  };

  const RESULT: AiEditResult = {
    todo_id: 'id-1',
    empty: false,
    context: { subtasks_truncated: false },
    change_set: CHANGE_SET,
  };

  /** Every write, in the order it was issued. */
  let writes: string[];

  function recordWrites(): void {
    writes = [];
    mockUpdateTodo.mockImplementation(async (id, patch) => {
      writes.push(`PATCH ${id} ${JSON.stringify(patch)}`);
      return makeTodo({ id });
    });
    mockCreateSubtask.mockImplementation(async (parentId, draft) => {
      writes.push(`POST ${parentId}/subtasks ${draft.title}`);
      return makeTodo({ id: 'sub-new', parent_id: parentId });
    });
    mockDeleteTodo.mockImplementation(async (id) => {
      writes.push(`DELETE ${id}`);
    });
  }

  async function openEditPanel(user: ReturnType<typeof userEvent.setup>): Promise<void> {
    await openRowAiMenu(user, 'Buy milk');
    await user.click(screen.getByRole('menuitem', { name: 'Edit with AI: Buy milk' }));
    await user.type(
      screen.getByLabelText('What should the AI change?'),
      'rename it and tidy the steps',
    );
    await user.click(screen.getByRole('button', { name: 'Ask the AI' }));
    await screen.findByRole('heading', { name: 'Suggested changes' });
  }

  it('writes nothing until Apply is pressed', async () => {
    const user = userEvent.setup();
    recordWrites();
    mockEditTodo.mockResolvedValue(RESULT);
    await renderSignedIn();

    await openEditPanel(user);

    expect(mockEditTodo).toHaveBeenCalledWith(
      'id-1',
      'rename it and tidy the steps',
      todayString(),
      expect.any(AbortSignal),
    );
    expect(writes).toEqual([]);
  });

  it('applies one PATCH plus one call per operation, then refetches once', async () => {
    const user = userEvent.setup();
    recordWrites();
    mockEditTodo.mockResolvedValue(RESULT);
    await renderSignedIn();
    await openEditPanel(user);

    const listCalls = mockListTodos.mock.calls.length;
    await user.click(screen.getByRole('button', { name: 'Apply 5 changes' }));

    await waitFor(() => expect(writes).toHaveLength(5));
    expect(writes).toEqual([
      'PATCH id-1 {"title":"Call the dentist"}',
      'PATCH sub-1 {"title":"Book the appointment"}',
      'PATCH sub-2 {"completed":true}',
      'DELETE sub-3',
      'POST id-1/subtasks Find the phone number',
    ]);
    // D-IT5-4: exactly one quiet resync, not one merge per operation.
    await waitFor(() => expect(mockListTodos.mock.calls.length).toBe(listCalls + 1));
    expect(screen.queryByText('Loading todos...')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'AI actions for Buy milk' })).toHaveFocus(),
    );
  });

  /** Every editable field at once, so the single PATCH body is pinned whole. */
  it('carries every selected field change in one PATCH, then every operation', async () => {
    const user = userEvent.setup();
    recordWrites();
    mockEditTodo.mockResolvedValue({
      todo_id: 'id-1',
      empty: false,
      context: { subtasks_truncated: false },
      change_set: {
        title: { from: 'Buy milk', to: 'Call the dentist' },
        description: { from: null, to: 'Ask about the crown' },
        priority: { from: 'medium', to: 'high' },
        due_date: { from: null, to: '2026-09-11' },
        completed: { from: false, to: true },
        tags: { from: ['home'], to: ['home', 'health'], added: ['health'], removed: [] },
        subtasks: [
          { action: 'rename', id: 'sub-1', title: 'Book the appointment', from_title: 'Book it' },
          { action: 'complete', id: 'sub-2', title: null, from_title: 'Pay the invoice' },
          { action: 'reopen', id: 'sub-3', title: null, from_title: 'Call back' },
          { action: 'remove', id: 'sub-4', title: null, from_title: 'Old step' },
          { action: 'add', id: null, title: 'Find the phone number', from_title: null },
        ],
      },
    });
    await renderSignedIn();
    await openEditPanel(user);

    const listCalls = mockListTodos.mock.calls.length;
    // Five field changes plus one tag plus five operations.
    await user.click(screen.getByRole('button', { name: 'Apply 11 changes' }));

    await waitFor(() => expect(writes).toHaveLength(6));
    expect(writes).toEqual([
      'PATCH id-1 {"title":"Call the dentist","description":"Ask about the crown",' +
        '"priority":"high","due_date":"2026-09-11","completed":true,"tags":["home","health"]}',
      'PATCH sub-1 {"title":"Book the appointment"}',
      'PATCH sub-2 {"completed":true}',
      'PATCH sub-3 {"completed":false}',
      'DELETE sub-4',
      'POST id-1/subtasks Find the phone number',
    ]);
    await waitFor(() => expect(mockListTodos.mock.calls.length).toBe(listCalls + 1));
  });

  it('skips a subtask that vanished elsewhere and keeps going', async () => {
    const user = userEvent.setup();
    recordWrites();
    mockEditTodo.mockResolvedValue(RESULT);
    mockUpdateTodo.mockImplementation(async (id, patch) => {
      if (id === 'sub-1') {
        throw new ApiError(404, 'todo_not_found', 'Gone');
      }
      writes.push(`PATCH ${id} ${JSON.stringify(patch)}`);
      return makeTodo({ id });
    });
    await renderSignedIn();
    await openEditPanel(user);

    await user.click(screen.getByRole('button', { name: 'Apply 5 changes' }));

    await waitFor(() => expect(writes).toHaveLength(4));
    expect(writes).toEqual([
      'PATCH id-1 {"title":"Call the dentist"}',
      'PATCH sub-2 {"completed":true}',
      'DELETE sub-3',
      'POST id-1/subtasks Find the phone number',
    ]);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('reports how far it got when a write fails part-way through', async () => {
    const user = userEvent.setup();
    recordWrites();
    mockEditTodo.mockResolvedValue(RESULT);
    mockDeleteTodo.mockRejectedValue(new ApiError(500, null, 'boom'));
    await renderSignedIn();
    await openEditPanel(user);

    const listCalls = mockListTodos.mock.calls.length;
    await user.click(screen.getByRole('button', { name: 'Apply 5 changes' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Applied 3 of 5 changes. Please try again.',
    );
    // No retry — re-applying the `add` would duplicate a subtask (D-IT5-7).
    expect(screen.queryByRole('button', { name: /^Apply/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
    // The `add` after the failure never ran, and the view was resynced anyway.
    expect(writes).not.toContain('POST id-1/subtasks Find the phone number');
    await waitFor(() => expect(mockListTodos.mock.calls.length).toBe(listCalls + 1));
  });

  /** Review round 1, item 3: a skipped operation is not an applied one. */
  it('does not count a skipped 404 as applied', async () => {
    const user = userEvent.setup();
    recordWrites();
    mockEditTodo.mockResolvedValue(RESULT);
    mockUpdateTodo.mockImplementation(async (id, patch) => {
      if (id === 'sub-1') {
        throw new ApiError(404, 'todo_not_found', 'Gone');
      }
      writes.push(`PATCH ${id} ${JSON.stringify(patch)}`);
      return makeTodo({ id });
    });
    mockDeleteTodo.mockRejectedValue(new ApiError(500, null, 'boom'));
    await renderSignedIn();
    await openEditPanel(user);

    await user.click(screen.getByRole('button', { name: 'Apply 5 changes' }));

    // The parent PATCH and one subtask landed; the skipped rename did not.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Applied 2 of 5 changes. Please try again.',
    );
  });

  /**
   * Review round 1, item 5: a 404 from `POST /subtasks` means the *parent* is
   * gone, so the apply stops instead of closing as though it had worked.
   */
  it('stops when adding a subtask 404s, because the parent is gone', async () => {
    const user = userEvent.setup();
    recordWrites();
    mockEditTodo.mockResolvedValue(RESULT);
    mockCreateSubtask.mockRejectedValue(new ApiError(404, 'todo_not_found', 'Gone'));
    await renderSignedIn();
    await openEditPanel(user);

    await user.click(screen.getByRole('button', { name: 'Apply 5 changes' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Applied 4 of 5 changes. Please try again.',
    );
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
  });

  /**
   * Review round 1, item 4 (security, low). A subtask operation may only ever
   * touch a subtask: if a misbehaving server named the todo itself, a `remove`
   * would delete the row the user is editing.
   */
  it('never deletes the todo itself, whatever id an operation carries', async () => {
    const user = userEvent.setup();
    recordWrites();
    mockEditTodo.mockResolvedValue({
      ...RESULT,
      change_set: {
        ...CHANGE_SET,
        subtasks: [
          { action: 'remove', id: 'id-1', title: null, from_title: 'Buy milk' },
          { action: 'complete', id: 'sub-2', title: null, from_title: 'Pay the invoice' },
        ],
      },
    });
    await renderSignedIn();
    await openEditPanel(user);

    await user.click(screen.getByRole('button', { name: 'Apply 3 changes' }));

    await waitFor(() => expect(writes).toHaveLength(2));
    expect(mockDeleteTodo).not.toHaveBeenCalled();
    expect(writes).toEqual([
      'PATCH id-1 {"title":"Call the dentist"}',
      'PATCH sub-2 {"completed":true}',
    ]);
    expect(screen.getByText('Buy milk')).toBeInTheDocument();
  });

  it('keeps the preview when the very first write fails, since nothing landed', async () => {
    const user = userEvent.setup();
    recordWrites();
    mockEditTodo.mockResolvedValue(RESULT);
    mockUpdateTodo.mockRejectedValue(new ApiError(500, null, 'boom'));
    await renderSignedIn();
    await openEditPanel(user);

    await user.click(screen.getByRole('button', { name: 'Apply 5 changes' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not apply the changes. Please try again.',
    );
    expect(screen.getByRole('button', { name: 'Apply 5 changes' })).toBeInTheDocument();
    expect(writes).toEqual([]);
  });

  it('is not offered at all when the AI stack is down', async () => {
    const user = userEvent.setup();
    mockGetAiStatus.mockResolvedValue({
      enabled: true,
      available: false,
      model: 'qwen2.5:3b',
      reason: 'unreachable',
    });
    await renderSignedIn();

    expect(
      screen.queryByRole('button', { name: 'AI actions for Buy milk' }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('menuitem', { name: 'Edit with AI: Buy milk' }),
    ).not.toBeInTheDocument();
    expect(mockEditTodo).not.toHaveBeenCalled();
    // The manual edit path is untouched.
    await user.click(screen.getByRole('button', { name: 'Edit Buy milk' }));
    expect(screen.getByLabelText('Title')).toHaveValue('Buy milk');
  });
});

describe('daily summary', () => {
  it('renders the briefing and the meta line', async () => {
    const user = userEvent.setup();
    mockDailySummary.mockResolvedValue({
      summary: 'You have 5 open todos and one is overdue.',
      todo_count: 5,
      generated_at: '2026-09-02T08:04:00Z',
    });
    await renderSignedIn();
    await openDailySummary(user);

    await user.click(screen.getByRole('button', { name: 'Generate summary' }));

    expect(
      await screen.findByText('You have 5 open todos and one is overdue.'),
    ).toBeInTheDocument();
    expect(mockDailySummary).toHaveBeenCalledWith(
      'list-1',
      todayString(),
      expect.any(AbortSignal),
    );
    expect(screen.getByText(/^Based on 5 open todos · generated \d{2}:\d{2}$/)).toBeInTheDocument();
  });

  it('says "1 open todo" when there is exactly one', async () => {
    const user = userEvent.setup();
    mockDailySummary.mockResolvedValue({
      summary: 'One thing left: buy milk.',
      todo_count: 1,
      generated_at: '2026-09-02T08:04:00Z',
    });
    await renderSignedIn();
    await openDailySummary(user);

    await user.click(screen.getByRole('button', { name: 'Generate summary' }));

    expect(
      await screen.findByText(/^Based on 1 open todo · generated \d{2}:\d{2}$/),
    ).toBeInTheDocument();
  });

  it('says why the button is inert when the model is unreachable', async () => {
    const user = userEvent.setup();
    mockGetAiStatus.mockResolvedValue({
      enabled: true,
      available: false,
      model: 'qwen2.5:3b',
      reason: 'unreachable',
    });
    await renderSignedIn();
    await openDailySummary(user);

    // The reason sits inside the card, next to the control it explains.
    const card = screen.getByRole('button', { name: 'Generate summary' }).closest('section');
    const reason = within(card as HTMLElement).getByText(AI_UNAVAILABLE_BANNER);
    expect(reason).toHaveAttribute('role', 'status');
    expect(screen.getByRole('button', { name: 'Generate summary' })).toHaveAttribute(
      'aria-disabled',
      'true',
    );
  });

  it('reports an empty day without pretending to summarise it', async () => {
    const user = userEvent.setup();
    mockDailySummary.mockResolvedValue({
      summary: '',
      todo_count: 0,
      generated_at: '2026-09-02T08:04:00Z',
    });
    await renderSignedIn();
    await openDailySummary(user);

    await user.click(screen.getByRole('button', { name: 'Generate summary' }));

    expect(await screen.findByText(EMPTY_SUMMARY_MESSAGE)).toBeInTheDocument();
  });
});

describe('AI failures', () => {
  /** Every failure must leave the ordinary controls fully usable. */
  async function expectManualControlsUsable(
    user: ReturnType<typeof userEvent.setup>,
  ): Promise<void> {
    expect(screen.getByRole('button', { name: 'Edit Buy milk' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Delete Buy milk' })).toBeEnabled();
    // The manual composer is one click away and fully usable.
    await user.click(screen.getByRole('tab', { name: 'Add' }));
    expect(screen.getByRole('textbox', { name: 'New todo title' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Add' })).toBeInTheDocument();
  }

  it('shows the unavailable copy for a 503', async () => {
    const user = userEvent.setup();
    mockParseTodo.mockRejectedValue(new ApiError(503, 'ai_unavailable', 'AI is unavailable'));
    await renderSignedIn();
    await openAiComposer(user);

    await user.type(screen.getByLabelText('Describe a todo in your own words'), 'dentist');
    await user.click(screen.getByRole('button', { name: 'Draft with AI' }));

    expect(await screen.findByText(AI_UNAVAILABLE_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Suggested todo' })).not.toBeInTheDocument();
    await expectManualControlsUsable(user);
  });

  it('shows the rate-limit copy for a 429', async () => {
    const user = userEvent.setup();
    mockSuggestMetadata.mockRejectedValue(new ApiError(429, 'rate_limited', 'Too many'));
    await renderSignedIn();

    await openRowAiMenu(user, 'Buy milk');
    await user.click(
      screen.getByRole('menuitem', { name: 'Suggest priority and tags: Buy milk' }),
    );

    expect(await screen.findByText(AI_RATE_LIMIT_MESSAGE)).toBeInTheDocument();
    await expectManualControlsUsable(user);
  });

  it('shows the timeout copy when our own 60 s abort fires', async () => {
    const user = userEvent.setup();
    mockDailySummary.mockRejectedValue(new AiTimeoutError());
    await renderSignedIn();
    await openDailySummary(user);

    await user.click(screen.getByRole('button', { name: 'Generate summary' }));

    expect(await screen.findByText(AI_TIMEOUT_MESSAGE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Generate summary' })).toBeInTheDocument();
    await expectManualControlsUsable(user);
  });
});

/**
 * IT3-7's last resort. With the composer on the "Draft with AI" tab the manual
 * title input is inside a `hidden` panel: it exists, but nothing can focus it.
 * The composer section itself — `tabindex="-1"`, named by its `aria-label` — is
 * what catches the hand-off, instead of leaving the keyboard stranded on `body`.
 */
describe('focus after deleting the last todo with the AI tab open', () => {
  it('lands on the composer section, not on the hidden title input', async () => {
    const user = userEvent.setup();
    mockDeleteTodo.mockResolvedValue(undefined);
    await renderSignedIn();
    await openAiComposer(user);

    await user.click(screen.getByRole('button', { name: 'Delete Buy milk' }));

    await waitFor(() => expect(screen.queryByText('Buy milk')).not.toBeInTheDocument());
    const composer = document.getElementById('todo-composer');
    expect(composer).toHaveAccessibleName('Add a todo');
    expect(composer).toHaveFocus();
    // The hidden input is still mounted (that is IT4-1) but never focusable.
    expect(document.getElementById('new-todo-title')).not.toHaveFocus();
  });
});
