import type { Priority } from './types';
import { request } from './client';
import { ApiError } from './errors';

/**
 * The AI endpoints (master §6.6). Every one of them returns a *draft*: the AI
 * never writes to the database (decision D-AI1), so applying a suggestion goes
 * through the ordinary todo endpoints once the user has confirmed it.
 */

/**
 * The outermost rung of the timeout ladder (master §8.3): ai-agent waits 45 s
 * on Ollama, the backend 50 s on ai-agent, and the browser gives up last at
 * 60 s — so a slow model surfaces as the backend's 504 `ai_timeout` rather
 * than as our own abort, and this only fires when the backend itself hangs.
 */
export const AI_TIMEOUT_MS = 60_000;

export const AI_UNAVAILABLE_BANNER =
  'AI features are unavailable right now. Everything else works as usual.';
export const AI_AUTH_FAILED_BANNER =
  "AI is unavailable: the AI service rejected this server's credentials. Everything else works as usual.";
export const AI_MODEL_UNAVAILABLE_BANNER =
  'The AI model is still starting or has not been downloaded yet. Everything else works as usual.';
export const AI_UNAVAILABLE_MESSAGE = 'AI is unavailable right now. Please try again later.';
export const AI_RATE_LIMIT_MESSAGE = 'Too many AI requests. Please wait a moment.';
export const AI_TIMEOUT_MESSAGE = 'That took too long. Please try again.';
export const AI_SLOW_HINT = 'This can take up to a minute on a slow machine.';

/**
 * The instruction cap of decision D-IT5-9. The backend truncates at the same
 * number rather than rejecting, so this is a courtesy to the user (the textarea
 * stops accepting characters) and never the only guard.
 */
export const MAX_INSTRUCTION_LENGTH = 500;

/** Raised when our own 60 s `AbortController` fires — not a server error. */
export class AiTimeoutError extends Error {
  constructor() {
    super('The AI request took too long.');
    this.name = 'AiTimeoutError';
  }
}

/**
 * Raised when an AI edit was applied *in part* before a write failed
 * (decision D-IT5-7). Applying a change set is a sequence of ordinary calls and
 * therefore not atomic, so the panel has to be able to say how far it got —
 * and must not offer a retry, because re-running an `add` would duplicate a
 * subtask.
 */
export class PartialApplyError extends Error {
  applied: number;
  total: number;

  constructor(applied: number, total: number) {
    super(`Applied ${applied} of ${total} changes.`);
    this.name = 'PartialApplyError';
    this.applied = applied;
    this.total = total;
  }
}

/**
 * Why the AI stack is unavailable (master §5.1, iteration-4 delta). It is a
 * closed machine enum: the server promises `reason === null` exactly when
 * `available` is true, and an unrecognised value must read as `null` so the
 * server can add one later without breaking this client.
 */
export type AiUnavailableReason =
  | 'disabled'
  | 'unreachable'
  | 'auth_failed'
  | 'model_unavailable';

const AI_UNAVAILABLE_REASONS: readonly AiUnavailableReason[] = [
  'disabled',
  'unreachable',
  'auth_failed',
  'model_unavailable',
];

export interface AiStatus {
  enabled: boolean;
  available: boolean;
  model: string | null;
  /** `null` when the AI is up, when the field is missing, or when unknown. */
  reason: AiUnavailableReason | null;
}

/**
 * The banner shown next to an inert AI control. It names no host, no service
 * internals and no configuration value — only what the user can conclude from
 * it: the rest of the app is unaffected.
 *
 * `disabled` never reaches this function: `enabled === false` hides the whole
 * AI section, so there is no control left to explain.
 */
export function aiUnavailableBanner(reason: AiUnavailableReason | null | undefined): string {
  if (reason === 'auth_failed') {
    return AI_AUTH_FAILED_BANNER;
  }
  if (reason === 'model_unavailable') {
    return AI_MODEL_UNAVAILABLE_BANNER;
  }
  return AI_UNAVAILABLE_BANNER;
}

export interface AiSubtaskDraft {
  title: string;
}

export interface AiTodoDraft {
  title: string;
  description: string | null;
  priority: Priority;
  due_date: string | null;
  tags: string[];
  subtasks: AiSubtaskDraft[];
}

export interface AiMetadataSuggestion {
  priority: Priority;
  tags: string[];
}

export interface AiDailySummary {
  summary: string;
  todo_count: number;
  generated_at: string;
}

/**
 * One resolved field change of `POST /api/ai/edit-todo` (iteration-5 spec
 * §2.1). The wire key really is `from` — a Python keyword the backend aliases
 * on purpose, pinned there by a serialization test and here by a fixture.
 */
export interface AiFieldChange<T> {
  from: T;
  to: T;
}

/**
 * `to` is the full list to PATCH; `added`/`removed` are what the panel renders,
 * one checkbox per name.
 */
export interface AiTagsChange {
  from: string[];
  to: string[];
  added: string[];
  removed: string[];
}

export const AI_EDIT_ACTIONS = ['add', 'remove', 'rename', 'complete', 'reopen'] as const;

export type AiEditAction = (typeof AI_EDIT_ACTIONS)[number];

/**
 * One subtask operation. `add` carries a title and no id; `rename` carries
 * both; the other three carry an id only. `from_title` is the *server's* copy
 * of the targeted subtask's title, so the panel can label an operation without
 * trusting its own possibly-stale row.
 */
export interface AiEditOp {
  action: AiEditAction;
  id: string | null;
  title: string | null;
  from_title: string | null;
}

export interface AiEditChangeSet {
  title: AiFieldChange<string> | null;
  description: AiFieldChange<string | null> | null;
  priority: AiFieldChange<Priority> | null;
  due_date: AiFieldChange<string | null> | null;
  completed: AiFieldChange<boolean> | null;
  tags: AiTagsChange | null;
  /** Already in the order the client must apply them (§2.1). */
  subtasks: AiEditOp[];
}

export interface AiEditContext {
  /** True when the todo had more than 20 subtasks and the AI saw only the first 20. */
  subtasks_truncated: boolean;
}

export interface AiEditResult {
  todo_id: string;
  /** Derived from the parsed change set — see `editTodo`. */
  empty: boolean;
  context: AiEditContext;
  change_set: AiEditChangeSet;
}

/**
 * How many individually selectable changes a change set contains — i.e. how
 * many checkboxes the preview renders. One tag change counts once per name,
 * because that is how the user sees (and deselects) it.
 */
export function countChanges(changeSet: AiEditChangeSet): number {
  return (
    (changeSet.title === null ? 0 : 1) +
    (changeSet.description === null ? 0 : 1) +
    (changeSet.priority === null ? 0 : 1) +
    (changeSet.due_date === null ? 0 : 1) +
    (changeSet.completed === null ? 0 : 1) +
    (changeSet.tags === null ? 0 : changeSet.tags.added.length + changeSet.tags.removed.length) +
    changeSet.subtasks.length
  );
}

/** An empty change set — the shape a missing or unusable `change_set` reads as. */
export function emptyChangeSet(): AiEditChangeSet {
  return {
    title: null,
    description: null,
    priority: null,
    due_date: null,
    completed: null,
    tags: null,
    subtasks: [],
  };
}

/** True for the `AbortError` raised when the *caller* cancelled the request. */
export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

/** Maps a failed AI call onto the exact user-facing copy of slice-4 F5. */
export function aiErrorMessage(error: unknown): string {
  if (error instanceof AiTimeoutError) {
    return AI_TIMEOUT_MESSAGE;
  }
  if (error instanceof ApiError && error.status === 429) {
    return AI_RATE_LIMIT_MESSAGE;
  }
  // 503 `ai_unavailable` / `ai_disabled`, 504 `ai_timeout` and anything else
  // all read the same to the user: the app still works, the AI does not.
  return AI_UNAVAILABLE_MESSAGE;
}

/**
 * POSTs to an AI endpoint under a 60 s abort (master §8.3). The caller's
 * `signal` cancels it too, so unmounting or discarding a draft drops the
 * request instead of leaving it to land on a dead component.
 */
async function aiRequest<T>(url: string, body: object, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;

  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, AI_TIMEOUT_MS);

  const forwardAbort = () => controller.abort();
  if (signal?.aborted) {
    controller.abort();
  } else {
    signal?.addEventListener('abort', forwardAbort);
  }

  try {
    return await request<T>(url, 'POST', body, { signal: controller.signal });
  } catch (error) {
    if (timedOut) {
      throw new AiTimeoutError();
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener('abort', forwardAbort);
  }
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function asPriority(value: unknown): Priority {
  return value === 'low' || value === 'high' ? value : 'medium';
}

/** Accepts only the §5.1 literals; everything else — including a missing
 *  field from an older backend — reads as "no reason given". */
function asReason(value: unknown): AiUnavailableReason | null {
  return AI_UNAVAILABLE_REASONS.find((reason) => reason === value) ?? null;
}

function asTitles(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => asString((item as AiSubtaskDraft | null)?.title))
    .filter((title): title is string => title !== null);
}

function asTags(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((tag): tag is string => typeof tag === 'string' && tag !== '');
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** A `{from, to}` entry, or `null` for "this field does not change" (§2.1). */
function asEntry(value: unknown): { from: unknown; to: unknown } | null {
  const record = asRecord(value);
  return record === null ? null : { from: record.from, to: record.to };
}

/** Only the three literals; unlike `asPriority` an unknown value is no change. */
function asExactPriority(value: unknown): Priority | null {
  return value === 'low' || value === 'medium' || value === 'high' ? value : null;
}

function asIsoDate(value: unknown): string | null {
  return typeof value === 'string' && ISO_DATE.test(value) ? value : null;
}

function asTitleChange(value: unknown): AiFieldChange<string> | null {
  const entry = asEntry(value);
  const to = asString(entry?.to);
  // `title` can never be cleared (§2.1), so a missing `to` is not a change.
  return entry === null || to === null
    ? null
    : { from: typeof entry.from === 'string' ? entry.from : '', to };
}

/** `description`: `to: null` means *clear it*, which is a change in itself. */
function asTextChange(value: unknown): AiFieldChange<string | null> | null {
  const entry = asEntry(value);
  if (entry === null) {
    return null;
  }
  const from = asString(entry.from);
  const to = asString(entry.to);
  // Clearing something that is already empty is not a change worth a checkbox.
  return from === null && to === null ? null : { from, to };
}

function asPriorityChange(value: unknown): AiFieldChange<Priority> | null {
  const entry = asEntry(value);
  const to = asExactPriority(entry?.to);
  return entry === null || to === null
    ? null
    : { from: asExactPriority(entry.from) ?? 'medium', to };
}

function asDueDateChange(value: unknown): AiFieldChange<string | null> | null {
  const entry = asEntry(value);
  if (entry === null) {
    return null;
  }
  const from = asIsoDate(entry.from);
  const to = asIsoDate(entry.to);
  return from === null && to === null ? null : { from, to };
}

function asCompletedChange(value: unknown): AiFieldChange<boolean> | null {
  const entry = asEntry(value);
  return entry === null || typeof entry.to !== 'boolean'
    ? null
    : { from: entry.from === true, to: entry.to };
}

function asTagsChange(value: unknown): AiTagsChange | null {
  const record = asRecord(value);
  if (record === null) {
    return null;
  }
  const added = asTags(record.added);
  const removed = asTags(record.removed);
  // `to` is what we would PATCH; without a real list a "tag change" would
  // silently clear every tag the todo has, so drop it instead.
  if (!Array.isArray(record.to) || (added.length === 0 && removed.length === 0)) {
    return null;
  }
  return { from: asTags(record.from), to: asTags(record.to), added, removed };
}

/**
 * Subtask operations, dropping anything this client could not act on: an
 * unknown `action`, a non-`add` operation without an id, and an `add` or
 * `rename` without a usable title.
 */
function asEditOps(value: unknown): AiEditOp[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const ops: AiEditOp[] = [];
  for (const item of value) {
    const record = asRecord(item);
    if (record === null) {
      continue;
    }
    const action = AI_EDIT_ACTIONS.find((known) => known === record.action);
    if (action === undefined) {
      continue;
    }
    const title = asString(record.title);
    const fromTitle = asString(record.from_title);
    if (action === 'add') {
      if (title !== null) {
        ops.push({ action, id: null, title, from_title: null });
      }
      continue;
    }
    const id = asString(record.id);
    if (id === null || (action === 'rename' && title === null)) {
      continue;
    }
    ops.push({ action, id, title: action === 'rename' ? title : null, from_title: fromTitle });
  }
  return ops;
}

/**
 * `GET /api/ai/status` — the one AI endpoint that never fails. Anything other
 * than a well-formed "enabled" answer means: hide the AI section entirely.
 */
export async function getAiStatus(): Promise<AiStatus> {
  const raw = await request<Partial<AiStatus>>('/api/ai/status');
  const available = raw?.available === true;
  return {
    enabled: raw?.enabled === true,
    available,
    model: asString(raw?.model),
    // The contract guarantees `reason === null` when `available`; enforcing it
    // here means the UI can never pair a working AI with a failure banner.
    reason: available ? null : asReason(raw?.reason),
  };
}

/** `POST /api/ai/parse-todo` — a natural-language note becomes a draft todo. */
export async function parseTodo(
  text: string,
  today: string,
  signal?: AbortSignal,
): Promise<AiTodoDraft> {
  const response = await aiRequest<{ draft?: Partial<AiTodoDraft> }>(
    '/api/ai/parse-todo',
    { text, today },
    signal,
  );
  const draft = response?.draft ?? {};
  return {
    title: asString(draft.title) ?? '',
    description: asString(draft.description),
    priority: asPriority(draft.priority),
    due_date: asString(draft.due_date),
    tags: asTags(draft.tags),
    subtasks: asTitles(draft.subtasks).map((title) => ({ title })),
  };
}

/** `POST /api/ai/suggest-subtasks` — returns the suggested titles only. */
export async function suggestSubtasks(
  todoId: string,
  maxItems = 5,
  signal?: AbortSignal,
): Promise<string[]> {
  const response = await aiRequest<{ subtasks?: unknown }>(
    '/api/ai/suggest-subtasks',
    { todo_id: todoId, max_items: maxItems },
    signal,
  );
  return asTitles(response?.subtasks);
}

/** `POST /api/ai/suggest-metadata` — a priority and tags for an existing todo. */
export async function suggestMetadata(
  todoId: string,
  signal?: AbortSignal,
): Promise<AiMetadataSuggestion> {
  const response = await aiRequest<Partial<AiMetadataSuggestion>>(
    '/api/ai/suggest-metadata',
    { todo_id: todoId },
    signal,
  );
  return { priority: asPriority(response?.priority), tags: asTags(response?.tags) };
}

/**
 * `POST /api/ai/edit-todo` — a free-text instruction becomes a change set
 * (iteration-5 spec §2.1). Like every other AI endpoint it writes nothing
 * (D-AI1): the answer is a diff the user confirms, and applying it goes back
 * through the ordinary todo endpoints (D-IT5-1).
 */
export async function editTodo(
  todoId: string,
  instruction: string,
  today: string,
  signal?: AbortSignal,
): Promise<AiEditResult> {
  const response = await aiRequest<Record<string, unknown>>(
    '/api/ai/edit-todo',
    {
      todo_id: todoId,
      // The backend truncates too (D-IT5-9); doing it here as well means the
      // preview can never describe more text than we actually sent.
      instruction: instruction.slice(0, MAX_INSTRUCTION_LENGTH),
      today,
    },
    signal,
  );

  const raw = asRecord(response?.change_set) ?? {};
  const changeSet: AiEditChangeSet = {
    title: asTitleChange(raw.title),
    description: asTextChange(raw.description),
    priority: asPriorityChange(raw.priority),
    due_date: asDueDateChange(raw.due_date),
    completed: asCompletedChange(raw.completed),
    tags: asTagsChange(raw.tags),
    subtasks: asEditOps(raw.subtasks),
  };

  return {
    todo_id: asString(response?.todo_id) ?? todoId,
    // The server sends `empty` as well, and the two must agree — but what the
    // panel can actually offer is what survived parsing above, so that is what
    // decides. A server flag disagreeing with a renderable change set would
    // otherwise hide changes behind "nothing to change".
    empty: countChanges(changeSet) === 0,
    context: { subtasks_truncated: asRecord(response?.context)?.subtasks_truncated === true },
    change_set: changeSet,
  };
}

/** `POST /api/ai/daily-summary` — `listId` `null` means "across all lists". */
export async function dailySummary(
  listId: string | null,
  today: string,
  signal?: AbortSignal,
): Promise<AiDailySummary> {
  const response = await aiRequest<Partial<AiDailySummary>>(
    '/api/ai/daily-summary',
    { list_id: listId, today },
    signal,
  );
  return {
    summary: typeof response?.summary === 'string' ? response.summary : '',
    todo_count: typeof response?.todo_count === 'number' ? response.todo_count : 0,
    generated_at: asString(response?.generated_at) ?? '',
  };
}
