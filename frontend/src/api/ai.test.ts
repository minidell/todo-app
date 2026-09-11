import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AI_AUTH_FAILED_BANNER,
  AI_MODEL_UNAVAILABLE_BANNER,
  AI_RATE_LIMIT_MESSAGE,
  AI_TIMEOUT_MESSAGE,
  AI_TIMEOUT_MS,
  AI_UNAVAILABLE_BANNER,
  AI_UNAVAILABLE_MESSAGE,
  AiTimeoutError,
  MAX_INSTRUCTION_LENGTH,
  PartialApplyError,
  aiErrorMessage,
  aiUnavailableBanner,
  countChanges,
  dailySummary,
  editTodo,
  emptyChangeSet,
  getAiStatus,
  parseTodo,
  suggestMetadata,
  suggestSubtasks,
} from './ai';
import { setAuthToken, setUnauthorizedHandler } from './client';
import { ApiError } from './errors';

let fetchMock: ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function bodyOf(call = 0): unknown {
  const init = fetchMock.mock.calls[call]?.[1] as RequestInit | undefined;
  return JSON.parse(init?.body as string);
}

beforeEach(() => {
  sessionStorage.clear();
  setAuthToken('ai-token');
  setUnauthorizedHandler(null);
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  setAuthToken(null);
});

describe('getAiStatus', () => {
  it('reads the flags from the always-200 status endpoint', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ enabled: true, available: true, model: 'qwen2.5:3b' }),
    );

    await expect(getAiStatus()).resolves.toEqual({
      enabled: true,
      available: true,
      model: 'qwen2.5:3b',
      reason: null,
    });
    expect(fetchMock.mock.calls[0][0]).toBe('/api/ai/status');
  });

  it('treats a disabled backend as disabled with no model', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ enabled: false, available: false, model: null, reason: 'disabled' }),
    );

    await expect(getAiStatus()).resolves.toEqual({
      enabled: false,
      available: false,
      model: null,
      reason: 'disabled',
    });
  });

  it.each(['disabled', 'unreachable', 'auth_failed', 'model_unavailable'] as const)(
    'keeps the reason %s exactly as the contract spells it',
    async (reason) => {
      fetchMock.mockResolvedValue(
        jsonResponse({ enabled: true, available: false, model: 'qwen2.5:3b', reason }),
      );

      await expect(getAiStatus()).resolves.toMatchObject({ available: false, reason });
    },
  );

  it('maps an unknown reason to null so a newer server cannot break the client', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ enabled: true, available: false, model: null, reason: 'sun_spots' }),
    );

    await expect(getAiStatus()).resolves.toMatchObject({ reason: null });
  });

  it('maps a missing reason to null, so an older server keeps working', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ enabled: true, available: false, model: null }));

    await expect(getAiStatus()).resolves.toMatchObject({ available: false, reason: null });
  });

  it('never pairs an available AI with a reason, whatever the server sends', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ enabled: true, available: true, model: 'm', reason: 'auth_failed' }),
    );

    await expect(getAiStatus()).resolves.toMatchObject({ available: true, reason: null });
  });
});

describe('aiUnavailableBanner', () => {
  it('names a credential mismatch without naming a host, service or value', () => {
    const copy = aiUnavailableBanner('auth_failed');

    expect(copy).toBe(AI_AUTH_FAILED_BANNER);
    expect(copy).toMatch(/rejected this server's credentials/);
    expect(copy).not.toMatch(/token|AI_AGENT_TOKEN|ai-agent|http|localhost/i);
  });

  it('explains a model that is not ready yet', () => {
    expect(aiUnavailableBanner('model_unavailable')).toBe(AI_MODEL_UNAVAILABLE_BANNER);
  });

  it.each([['unreachable' as const], [null], [undefined]])(
    'falls back to the generic banner for %s',
    (reason) => {
      expect(aiUnavailableBanner(reason)).toBe(AI_UNAVAILABLE_BANNER);
    },
  );

  it('falls back to the generic banner for an unknown reason', () => {
    expect(aiUnavailableBanner('sun_spots' as never)).toBe(AI_UNAVAILABLE_BANNER);
  });
});

describe('AI endpoints', () => {
  it('parse-todo sends the text and the local date and returns the draft', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        draft: {
          title: 'Call the dentist',
          description: null,
          priority: 'high',
          due_date: '2026-09-03',
          tags: ['health'],
          subtasks: [{ title: 'Find the phone number' }],
        },
      }),
    );

    const draft = await parseTodo('call the dentist tomorrow, urgent', '2026-09-02');

    expect(fetchMock.mock.calls[0][0]).toBe('/api/ai/parse-todo');
    expect(bodyOf()).toEqual({
      text: 'call the dentist tomorrow, urgent',
      today: '2026-09-02',
    });
    expect(draft).toEqual({
      title: 'Call the dentist',
      description: null,
      priority: 'high',
      due_date: '2026-09-03',
      tags: ['health'],
      subtasks: [{ title: 'Find the phone number' }],
    });
  });

  it('parse-todo survives a partial payload', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ draft: { title: 'Something' } }));

    await expect(parseTodo('something', '2026-09-02')).resolves.toEqual({
      title: 'Something',
      description: null,
      priority: 'medium',
      due_date: null,
      tags: [],
      subtasks: [],
    });
  });

  it('suggest-subtasks clamps to titles and forwards max_items', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ subtasks: [{ title: 'Step one' }, { title: '' }, { title: 'Step two' }] }),
    );

    await expect(suggestSubtasks('todo-1', 5)).resolves.toEqual(['Step one', 'Step two']);
    expect(bodyOf()).toEqual({ todo_id: 'todo-1', max_items: 5 });
  });

  it('suggest-metadata coerces an unknown priority to medium', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ priority: 'URGENT', tags: ['work'] }));

    await expect(suggestMetadata('todo-1')).resolves.toEqual({
      priority: 'medium',
      tags: ['work'],
    });
    expect(bodyOf()).toEqual({ todo_id: 'todo-1' });
  });

  it('daily-summary sends a null list id for "all lists"', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        summary: 'You have 5 open todos.',
        todo_count: 5,
        generated_at: '2026-09-02T08:00:00Z',
      }),
    );

    const summary = await dailySummary(null, '2026-09-02');

    expect(bodyOf()).toEqual({ list_id: null, today: '2026-09-02' });
    expect(summary.todo_count).toBe(5);
  });
});

/**
 * The literal §2.1 example, kept verbatim so the `from` wire key (R7) and the
 * positional-to-resolved shape are pinned on this side of the contract too.
 */
const CONTRACT_EXAMPLE = {
  todo_id: 'b6f0',
  empty: false,
  context: { subtasks_truncated: false },
  change_set: {
    title: { from: 'Dentist', to: 'Call the dentist' },
    description: null,
    priority: { from: 'medium', to: 'high' },
    due_date: { from: null, to: '2026-09-11' },
    completed: null,
    tags: { from: ['home'], to: ['home', 'health'], added: ['health'], removed: [] },
    subtasks: [
      { action: 'add', id: null, title: 'Find the phone number', from_title: null },
      { action: 'rename', id: '1c2a', title: 'Book the appointment', from_title: 'Book it' },
      { action: 'remove', id: '7d9e', title: null, from_title: 'Old step' },
      { action: 'complete', id: '44ab', title: null, from_title: 'Pay the invoice' },
      { action: 'reopen', id: '90cd', title: null, from_title: 'Call back' },
    ],
  },
};

describe('editTodo', () => {
  it('sends the todo, the instruction and the local date', async () => {
    fetchMock.mockResolvedValue(jsonResponse(CONTRACT_EXAMPLE));

    await editTodo('b6f0', 'rename it to Call the dentist', '2026-09-10');

    expect(fetchMock.mock.calls[0][0]).toBe('/api/ai/edit-todo');
    expect(bodyOf()).toEqual({
      todo_id: 'b6f0',
      instruction: 'rename it to Call the dentist',
      today: '2026-09-10',
    });
  });

  it('truncates an over-long instruction rather than sending it whole', async () => {
    fetchMock.mockResolvedValue(jsonResponse(CONTRACT_EXAMPLE));

    await editTodo('b6f0', 'x'.repeat(600), '2026-09-10');

    const body = bodyOf() as { instruction: string };
    expect(body.instruction).toHaveLength(MAX_INSTRUCTION_LENGTH);
  });

  it('parses the frozen contract example, `from` wire key included', async () => {
    fetchMock.mockResolvedValue(jsonResponse(CONTRACT_EXAMPLE));

    const result = await editTodo('b6f0', 'do it', '2026-09-10');

    expect(result).toEqual({
      todo_id: 'b6f0',
      empty: false,
      context: { subtasks_truncated: false },
      change_set: CONTRACT_EXAMPLE.change_set,
    });
    expect(result.change_set.title?.from).toBe('Dentist');
    expect(countChanges(result.change_set)).toBe(3 + 1 + 5);
  });

  it('reads an all-null change set as empty', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        todo_id: 'b6f0',
        empty: true,
        context: { subtasks_truncated: false },
        change_set: {
          title: null,
          description: null,
          priority: null,
          due_date: null,
          completed: null,
          tags: null,
          subtasks: [],
        },
      }),
    );

    const result = await editTodo('b6f0', 'delete this todo', '2026-09-10');

    expect(result.empty).toBe(true);
    expect(result.change_set).toEqual(emptyChangeSet());
  });

  it('reads a missing change_set as empty instead of throwing', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ todo_id: 'b6f0', empty: true }));

    const result = await editTodo('b6f0', 'anything', '2026-09-10');

    expect(result.empty).toBe(true);
    expect(result.change_set).toEqual(emptyChangeSet());
    expect(result.context.subtasks_truncated).toBe(false);
  });

  it('reports a truncated snapshot', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ ...CONTRACT_EXAMPLE, context: { subtasks_truncated: true } }),
    );

    await expect(editTodo('b6f0', 'do it', '2026-09-10')).resolves.toMatchObject({
      context: { subtasks_truncated: true },
    });
  });

  it('drops operations it could not act on', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        ...CONTRACT_EXAMPLE,
        change_set: {
          ...CONTRACT_EXAMPLE.change_set,
          subtasks: [
            { action: 'explode', id: '1', title: 'Nope', from_title: null },
            { action: 'remove', id: null, title: null, from_title: 'No id' },
            { action: 'rename', id: '2', title: '  ', from_title: 'No new title' },
            { action: 'add', id: null, title: '', from_title: null },
            { action: 'complete', id: '3', title: null, from_title: 'Keep me' },
          ],
        },
      }),
    );

    const result = await editTodo('b6f0', 'do it', '2026-09-10');

    expect(result.change_set.subtasks).toEqual([
      { action: 'complete', id: '3', title: null, from_title: 'Keep me' },
    ]);
  });

  it('drops a field change whose value it cannot use', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        ...CONTRACT_EXAMPLE,
        change_set: {
          ...CONTRACT_EXAMPLE.change_set,
          title: { from: 'Dentist', to: '   ' },
          priority: { from: 'medium', to: 'URGENT' },
          due_date: { from: null, to: 'next tuesday' },
          completed: { from: false, to: 'yes' },
          subtasks: [],
        },
      }),
    );

    const result = await editTodo('b6f0', 'do it', '2026-09-10');

    expect(result.change_set.title).toBeNull();
    expect(result.change_set.priority).toBeNull();
    expect(result.change_set.due_date).toBeNull();
    expect(result.change_set.completed).toBeNull();
  });

  it('keeps a clear-the-field entry, which is a change in itself', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        ...CONTRACT_EXAMPLE,
        change_set: {
          ...CONTRACT_EXAMPLE.change_set,
          description: { from: 'Ask about the crown', to: null },
          due_date: { from: '2026-09-11', to: null },
          tags: null,
          subtasks: [],
        },
      }),
    );

    const result = await editTodo('b6f0', 'clear it', '2026-09-10');

    expect(result.change_set.description).toEqual({ from: 'Ask about the crown', to: null });
    expect(result.change_set.due_date).toEqual({ from: '2026-09-11', to: null });
  });

  it('never turns a malformed tag change into "remove every tag"', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        ...CONTRACT_EXAMPLE,
        change_set: {
          ...CONTRACT_EXAMPLE.change_set,
          tags: { from: ['home'], added: ['health'], removed: [] },
          subtasks: [],
        },
      }),
    );

    const result = await editTodo('b6f0', 'tag it', '2026-09-10');

    expect(result.change_set.tags).toBeNull();
  });

  it('ignores a tag change that adds and removes nothing', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        ...CONTRACT_EXAMPLE,
        change_set: {
          ...CONTRACT_EXAMPLE.change_set,
          tags: { from: ['home'], to: ['home'], added: [], removed: [] },
          subtasks: [],
        },
      }),
    );

    await expect(editTodo('b6f0', 'tag it', '2026-09-10')).resolves.toMatchObject({
      change_set: { tags: null },
    });
  });

  it('propagates the server error so the panel can map the copy', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'Too many', code: 'rate_limited' }, 429));

    await expect(editTodo('b6f0', 'do it', '2026-09-10')).rejects.toMatchObject({
      status: 429,
      code: 'rate_limited',
    });
  });
});

describe('PartialApplyError', () => {
  it('carries how far the apply got', () => {
    const error = new PartialApplyError(2, 5);

    expect(error.applied).toBe(2);
    expect(error.total).toBe(5);
    expect(error.name).toBe('PartialApplyError');
    // It is not an AI failure: the four AI strings must not claim it.
    expect(error).toBeInstanceOf(Error);
  });
});

describe('failure handling', () => {
  it('turns our own 60 s abort into an AiTimeoutError', async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          (init.signal as AbortSignal).addEventListener('abort', () =>
            reject(new DOMException('Aborted', 'AbortError')),
          );
        }),
    );

    const pending = parseTodo('slow', '2026-09-02');
    const assertion = expect(pending).rejects.toBeInstanceOf(AiTimeoutError);
    await vi.advanceTimersByTimeAsync(AI_TIMEOUT_MS);
    await assertion;
  });

  it('propagates the caller cancelling the request', async () => {
    const controller = new AbortController();
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          (init.signal as AbortSignal).addEventListener('abort', () =>
            reject(new DOMException('Aborted', 'AbortError')),
          );
        }),
    );

    const pending = parseTodo('cancel me', '2026-09-02', controller.signal);
    controller.abort();

    await expect(pending).rejects.toThrowError(/Aborted/);
  });

  it('propagates the server error so the caller can map the copy', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: 'AI is unavailable', code: 'ai_unavailable' }, 503),
    );

    await expect(suggestMetadata('todo-1')).rejects.toMatchObject({
      status: 503,
      code: 'ai_unavailable',
    });
  });
});

describe('aiErrorMessage', () => {
  it('uses the exact copy for each failure mode', () => {
    expect(aiErrorMessage(new AiTimeoutError())).toBe(AI_TIMEOUT_MESSAGE);
    expect(aiErrorMessage(new ApiError(429, 'rate_limited', 'Too many'))).toBe(
      AI_RATE_LIMIT_MESSAGE,
    );
    expect(aiErrorMessage(new ApiError(503, 'ai_unavailable', 'Down'))).toBe(
      AI_UNAVAILABLE_MESSAGE,
    );
    expect(aiErrorMessage(new ApiError(504, 'ai_timeout', 'Slow'))).toBe(AI_UNAVAILABLE_MESSAGE);
    expect(aiErrorMessage(new ApiError(503, 'ai_disabled', 'Off'))).toBe(AI_UNAVAILABLE_MESSAGE);
    expect(aiErrorMessage(new Error('network'))).toBe(AI_UNAVAILABLE_MESSAGE);
  });
});
