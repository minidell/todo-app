import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  ListSummary,
  SubtaskDraft,
  TagSummary,
  Todo,
  TodoDraft,
  TodoPatch,
  TodoQuery,
} from './api/types';
import type { AiEditOp, AiMetadataSuggestion, AiStatus } from './api/ai';
import { PartialApplyError, countChanges, getAiStatus } from './api/ai';
import { ApiError } from './api/errors';
import {
  createSubtask,
  createTodo,
  deleteTodo,
  listTodos,
  setCompleted,
  updateTodo,
} from './api/client';
import { isOwnEvent } from './api/events';
import { createList, deleteList, listLists, renameList } from './api/lists';
import { listTags } from './api/tags';
import { useAuth } from './auth/AuthContext';
import { useEventStream } from './hooks/useEventStream';
import { loadSelectedListId, saveSelectedListId } from './auth/storage';
import { todayString } from './dates';
import AppHeader from './components/AppHeader';
import ConnectionStatus from './components/ConnectionStatus';
import DailySummaryPanel from './components/DailySummaryPanel';
import FilterBar, {
  DEFAULT_FILTERS,
  NO_MATCH_MESSAGE,
  hasActiveFilters,
} from './components/FilterBar';
import type { TodoFilters } from './components/FilterBar';
import ListNav, { ALL_LISTS_LABEL, LIST_FAILED_MESSAGE } from './components/ListNav';
import LoginScreen from './components/LoginScreen';
import RegisterScreen from './components/RegisterScreen';
import { TITLE_INPUT_ID } from './components/AddTodoForm';
import TodoComposer, { COMPOSER_SECTION_ID } from './components/TodoComposer';
import type { AiEditSelection } from './components/AiEditPanel';
import { deleteButtonId } from './components/TodoItem';
import TodoList from './components/TodoList';
import { META, SURFACE } from './styles';

const LOADING_MESSAGE = 'Loading todos...';
const EMPTY_MESSAGE = 'No todos yet. Add your first one above.';
const LOAD_ERROR_MESSAGE = 'Could not load todos. Please try again.';
const CREATE_ERROR_MESSAGE = 'Could not add todo. Please try again.';
const TOGGLE_ERROR_MESSAGE = 'Could not update todo. Please try again.';
const DELETE_ERROR_MESSAGE = 'Could not delete todo. Please try again.';
const SESSION_LOADING_MESSAGE = 'Loading…';

/**
 * Realtime frames are coalesced for a moment and then answered with a plain
 * refetch of the current query rather than a surgical in-place merge. The
 * slice-4 spec (F3) explicitly allows and prefers this: the lists are small, a
 * refetch cannot disagree with the active filters or the chosen sort order,
 * and a burst of events (deleting a list emits one frame per todo) costs a
 * single request. It also handles the subtask case for free — a subtask change
 * arrives as a `todo.updated` carrying the whole top-level parent (§7.2), and
 * the refetched rows already embed their subtasks.
 */
const EVENT_REFETCH_DELAY_MS = 250;

function addBusy(ids: Set<string>, id: string): Set<string> {
  const next = new Set(ids);
  next.add(id);
  return next;
}

function removeBusy(ids: Set<string>, id: string): Set<string> {
  const next = new Set(ids);
  next.delete(id);
  return next;
}

/** Reads the `id` of a `*.deleted` frame payload (master §7.2). */
function eventId(data: unknown): string | null {
  if (data !== null && typeof data === 'object') {
    const { id } = data as { id?: unknown };
    if (typeof id === 'string') {
      return id;
    }
  }
  return null;
}

/** Reads the `tag` of a `tag.created` / `tag.updated` frame (master §7.2). */
function eventTag(data: unknown): { id: string; name: string } | null {
  if (data === null || typeof data !== 'object') {
    return null;
  }
  const { tag } = data as { tag?: unknown };
  if (tag === null || typeof tag !== 'object') {
    return null;
  }
  const { id, name } = tag as { id?: unknown; name?: unknown };
  return typeof id === 'string' && typeof name === 'string' ? { id, name } : null;
}

/**
 * The neighbours of a row that has just been deleted, captured *before* it left
 * the list. Set only by `handleDelete`, and only after the DELETE succeeded;
 * consumed and cleared by exactly one effect (IT3-7 guard 1).
 */
interface PostDeleteFocus {
  nextId: string | null;
  prevId: string | null;
}

/**
 * Moves focus to the first of `ids` that will actually take it. Hidden
 * subtrees are skipped explicitly — the composer's manual panel is `hidden`
 * while the "Draft with AI" tab is active, so `#new-todo-title` exists but is
 * not a focus target — and the result is verified rather than assumed.
 */
function focusFirstAvailable(ids: string[]): void {
  for (const id of ids) {
    const element = document.getElementById(id);
    if (element === null || element.closest('[hidden]') !== null) {
      continue;
    }
    element.focus();
    if (document.activeElement === element) {
      return;
    }
  }
}

/** Picks the list to show on load: the remembered one, else the default. */
function initialSelection(lists: ListSummary[]): string {
  const stored = loadSelectedListId();
  if (stored && lists.some((list) => list.id === stored)) {
    return stored;
  }
  return lists.find((list) => list.is_default)?.id ?? lists[0]?.id ?? '';
}

/** Serializes the UI filter state into `GET /api/todos` parameters (§6.3). */
function toQuery(
  listId: string,
  filters: TodoFilters,
  today: string
): TodoQuery {
  return {
    ...(listId ? { list_id: listId } : {}),
    status: filters.status,
    priority: filters.priorities,
    tag: filters.tags,
    due: filters.due,
    q: filters.q,
    today,
    sort: filters.sort,
    order: filters.order,
  };
}

/**
 * Appending a created todo to the end of the list is only honest for an
 * unfiltered, default-ordered view. With a filter, a search term or a
 * non-default sort the server owns both membership and position, so the page
 * is refetched instead.
 */
function needsRefetchAfterCreate(filters: TodoFilters): boolean {
  return (
    hasActiveFilters(filters) ||
    filters.sort !== DEFAULT_FILTERS.sort ||
    filters.order !== DEFAULT_FILTERS.order
  );
}

function TodoApp() {
  const { user, logout } = useAuth();
  const [todos, setTodos] = useState<Todo[]>([]);
  const [total, setTotal] = useState(0);
  const [lists, setLists] = useState<ListSummary[]>([]);
  const [tags, setTags] = useState<TagSummary[]>([]);
  // `null` until the lists have been fetched; `''` means "All lists".
  const [selectedListId, setSelectedListId] = useState<string | null>(null);
  const [filters, setFilters] = useState<TodoFilters>(DEFAULT_FILTERS);
  const [today, setToday] = useState<string>(todayString);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  // `null` while the status call is in flight, or when it failed — either way
  // the AI section stays hidden and every manual control keeps working.
  const [aiStatus, setAiStatus] = useState<AiStatus | null>(null);

  /**
   * Guards against overlapping list requests: only the response of the most
   * recently issued fetch is allowed to touch state, so a slow earlier request
   * can never overwrite a newer one.
   */
  const requestSeq = useRef(0);

  /** Armed by `handleDelete` alone; see `PostDeleteFocus` and the effect below. */
  const postDeleteFocus = useRef<PostDeleteFocus | null>(null);

  /**
   * Hands the keyboard back a sensible landing place after a Delete button
   * unmounted under the user's fingers (IT3-7): the next row's Delete, else the
   * previous row's, else the composer.
   *
   * Guard 3: it acts only when focus was *actually* lost by that unmount —
   * `document.activeElement` falls back to `body` (or `null`) — so a user who
   * has already moved on somewhere else is never yanked back. Together with the
   * one-shot ref this is what keeps the IT3-9 regression from returning: an
   * event-driven refetch removes rows without ever arming the intent, and even
   * an armed intent stays unused if focus is somewhere real.
   */
  useEffect(() => {
    const intent = postDeleteFocus.current;
    if (intent === null) {
      return;
    }
    postDeleteFocus.current = null;

    const active = document.activeElement;
    if (active !== null && active !== document.body) {
      return;
    }

    focusFirstAvailable([
      ...(intent.nextId === null ? [] : [deleteButtonId(intent.nextId)]),
      ...(intent.prevId === null ? [] : [deleteButtonId(intent.prevId)]),
      TITLE_INPUT_ID,
      COMPOSER_SECTION_ID,
    ]);
  }, [todos]);

  useEffect(() => {
    let cancelled = false;

    async function loadLists() {
      try {
        const data = await listLists();
        if (cancelled) return;
        const safe = Array.isArray(data) ? data : [];
        setLists(safe);
        setSelectedListId(initialSelection(safe));
      } catch {
        if (cancelled) return;
        setLists([]);
        // Still show the todos across all lists rather than a dead screen.
        setSelectedListId('');
        setError(LIST_FAILED_MESSAGE);
      }
    }

    void loadLists();

    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Fetches the current page. `quiet` skips the loading placeholder for
   * refreshes that follow a user action — or arrive over the event stream — so
   * the list updates in place and the control the user was on keeps its focus.
   */
  const loadTodos = useCallback(
    async (listId: string, activeFilters: TodoFilters, quiet = false) => {
      const seq = requestSeq.current + 1;
      requestSeq.current = seq;
      // The caller's *local* date drives the Overdue/Today presets (F4).
      const localToday = todayString();

      if (!quiet) {
        setLoading(true);
      }
      try {
        const page = await listTodos(toQuery(listId, activeFilters, localToday));
        if (requestSeq.current !== seq) return;
        setTodos(page.todos);
        setTotal(page.total);
        setToday(localToday);
        setError(null);
      } catch {
        if (requestSeq.current !== seq) return;
        setError(LOAD_ERROR_MESSAGE);
      } finally {
        if (requestSeq.current === seq && !quiet) {
          setLoading(false);
        }
      }
    },
    []
  );

  useEffect(() => {
    if (selectedListId === null) {
      return;
    }
    void loadTodos(selectedListId, filters);
  }, [selectedListId, filters, loadTodos]);

  /** Keeps the per-list active counts in the switcher honest after a change. */
  const refreshLists = useCallback(async () => {
    try {
      const data = await listLists();
      if (Array.isArray(data)) {
        setLists(data);
      }
    } catch {
      // Counts may be stale; not worth an error message.
    }
  }, []);

  /** The tag vocabulary powering the filter chips; refreshed after writes. */
  const refreshTags = useCallback(async () => {
    try {
      const data = await listTags();
      if (Array.isArray(data)) {
        setTags(data);
      }
    } catch {
      // The chips are an aid, not a requirement; stay silent.
    }
  }, []);

  useEffect(() => {
    void refreshTags();
  }, [refreshTags]);

  useEffect(() => {
    let cancelled = false;

    async function loadAiStatus(): Promise<void> {
      try {
        const status = await getAiStatus();
        if (!cancelled) {
          setAiStatus(status);
        }
      } catch {
        // `GET /api/ai/status` is meant never to fail; if it does, the AI
        // section simply stays hidden.
        if (!cancelled) {
          setAiStatus(null);
        }
      }
    }

    void loadAiStatus();

    return () => {
      cancelled = true;
    };
  }, []);

  const handleSelectList = useCallback((listId: string): void => {
    saveSelectedListId(listId);
    setSelectedListId(listId);
  }, []);

  /** Brings the whole view back in step with the server, without a spinner. */
  const refetchView = useCallback((): void => {
    if (selectedListId !== null) {
      // Quiet: an event-driven refresh must not blank the list or steal focus.
      void loadTodos(selectedListId, filters, true);
    }
    void refreshLists();
    void refreshTags();
  }, [filters, loadTodos, refreshLists, refreshTags, selectedListId]);

  // Held in a ref so the debounce below never has to be rebuilt (and so a
  // pending timer always calls the *current* query, not the one that was
  // active when the event arrived).
  const refetchViewRef = useRef(refetchView);
  refetchViewRef.current = refetchView;

  const refetchTimer = useRef<number | null>(null);

  const cancelScheduledRefetch = useCallback((): void => {
    if (refetchTimer.current !== null) {
      window.clearTimeout(refetchTimer.current);
      refetchTimer.current = null;
    }
  }, []);

  const scheduleRefetch = useCallback((): void => {
    if (refetchTimer.current !== null) {
      return; // A refresh is already pending; this event rides along with it.
    }
    refetchTimer.current = window.setTimeout(() => {
      refetchTimer.current = null;
      refetchViewRef.current();
    }, EVENT_REFETCH_DELAY_MS);
  }, []);

  useEffect(() => cancelScheduledRefetch, [cancelScheduledRefetch]);

  /**
   * Follow-up to a repaired tag filter. Changing `filters` already refetches
   * the todos through the effect that watches it, so the debounced event
   * refetch would only issue the same request a second time — cancel it. The
   * rest of the view still has to catch up on its own: the vocabulary in
   * particular, because it is what the filter chips are drawn from.
   */
  const refreshAfterFilterRepair = useCallback((): void => {
    cancelScheduledRefetch();
    void refreshLists();
    void refreshTags();
  }, [cancelScheduledRefetch, refreshLists, refreshTags]);

  /** A `ready` frame covers whatever we missed while disconnected (§7.4). */
  const handleStreamReady = useCallback((): void => {
    cancelScheduledRefetch();
    refetchViewRef.current();
  }, [cancelScheduledRefetch]);

  const handleStreamEvent = useCallback(
    (name: string, data: unknown): void => {
      // Echo suppression: our own mutations were already applied from their
      // HTTP responses, so re-applying them would only cause flicker.
      if (isOwnEvent(data)) {
        return;
      }
      if (name === 'list.deleted' && selectedListId && eventId(data) === selectedListId) {
        const remaining = lists.filter((list) => list.id !== selectedListId);
        handleSelectList(remaining.find((list) => list.is_default)?.id ?? remaining[0]?.id ?? '');
      }
      // A `tag.*` frame invalidates the todo view as well as the vocabulary
      // (master §7.2: no per-todo fan-out), and the refetch below covers that.
      // What a refetch cannot fix is the *filter*: it works in tag names while
      // the frames carry ids, so a tag renamed or deleted elsewhere would leave
      // us querying a name the server no longer knows — a permanent, silent
      // "no todos match". The id→name mapping can only come from local state.
      if (name === 'tag.deleted') {
        const gone = tags.find((tag) => tag.id === eventId(data));
        if (gone !== undefined && filters.tags.includes(gone.name)) {
          setFilters((prev) => ({
            ...prev,
            tags: prev.tags.filter((tag) => tag !== gone.name),
          }));
          // The repaired filter is what refetches the todos, exactly once.
          refreshAfterFilterRepair();
          return;
        }
      } else if (name === 'tag.updated') {
        const renamed = eventTag(data);
        const known = renamed === null ? undefined : tags.find((tag) => tag.id === renamed.id);
        if (
          renamed !== null &&
          known !== undefined &&
          known.name !== renamed.name &&
          filters.tags.includes(known.name)
        ) {
          setFilters((prev) => ({
            ...prev,
            tags: prev.tags.map((tag) => (tag === known.name ? renamed.name : tag)),
          }));
          refreshAfterFilterRepair();
          return;
        }
      }
      // Nothing to repair — an unrelated tag, or an id we have never seen. The
      // ordinary debounced refetch is enough on its own.
      scheduleRefetch();
    },
    [
      filters.tags,
      handleSelectList,
      lists,
      refreshAfterFilterRepair,
      scheduleRefetch,
      selectedListId,
      tags,
    ]
  );

  const { mode: connectionMode } = useEventStream({
    enabled: true,
    onReady: handleStreamReady,
    onEvent: handleStreamEvent,
    onPoll: refetchView,
  });

  async function handleCreateList(name: string): Promise<void> {
    const created = await createList(name);
    setLists((prev) => [...prev, created]);
    handleSelectList(created.id);
  }

  async function handleRenameList(listId: string, name: string): Promise<void> {
    const updated = await renameList(listId, name);
    setLists((prev) => prev.map((list) => (list.id === listId ? updated : list)));
  }

  async function handleDeleteList(listId: string): Promise<void> {
    await deleteList(listId);
    const removed = lists.find((list) => list.id === listId);
    const remaining = lists.filter((list) => list.id !== listId);
    // The server promotes the oldest remaining list when the default goes
    // away (master §6.2); lists arrive ordered created_at ASC.
    if (removed?.is_default && remaining.length > 0) {
      remaining[0] = { ...remaining[0], is_default: true };
    }
    setLists(remaining);
    if (selectedListId === listId) {
      handleSelectList(remaining.find((list) => list.is_default)?.id ?? remaining[0]?.id ?? '');
    }
  }

  async function handleAdd(draft: SubtaskDraft): Promise<void> {
    try {
      const todo = await createTodo({
        ...draft,
        ...(selectedListId ? { list_id: selectedListId } : {}),
      });
      setError(null);
      if (needsRefetchAfterCreate(filters)) {
        // The new todo may not match the current query, and even when it does
        // the server decides where it belongs in the order — ask it.
        await loadTodos(selectedListId ?? '', filters, true);
      } else {
        setTodos((prev) => [...prev, todo]);
        setTotal((prev) => prev + 1);
      }
      void refreshLists();
      void refreshTags();
    } catch {
      setError(CREATE_ERROR_MESSAGE);
      throw new Error('create failed');
    }
  }

  async function handleToggle(id: string, completed: boolean): Promise<void> {
    setBusyIds((prev) => addBusy(prev, id));
    try {
      const updated = await setCompleted(id, completed);
      setTodos((prev) => prev.map((todo) => (todo.id === id ? updated : todo)));
      setError(null);
      void refreshLists();
    } catch {
      setError(TOGGLE_ERROR_MESSAGE);
    } finally {
      setBusyIds((prev) => removeBusy(prev, id));
    }
  }

  async function handleDelete(id: string): Promise<void> {
    setBusyIds((prev) => addBusy(prev, id));
    // Guard 2: the neighbours are read from the list as it stands now, before
    // the row is filtered out — afterwards there is nothing left to point at.
    const index = todos.findIndex((todo) => todo.id === id);
    try {
      await deleteTodo(id);
      // Guard 1: this is the only place in the app that arms the focus move,
      // and only on a delete that succeeded. A refetch, a poll, a filter change
      // or an event-driven removal therefore cannot trigger one (IT3-9).
      if (index !== -1) {
        postDeleteFocus.current = {
          nextId: todos[index + 1]?.id ?? null,
          prevId: todos[index - 1]?.id ?? null,
        };
      }
      setTodos((prev) => prev.filter((todo) => todo.id !== id));
      setTotal((prev) => Math.max(0, prev - 1));
      setError(null);
      void refreshLists();
      void refreshTags();
    } catch {
      setError(DELETE_ERROR_MESSAGE);
    } finally {
      setBusyIds((prev) => removeBusy(prev, id));
    }
  }

  /**
   * Partial PATCH from the edit panel. Failures are rethrown so the panel can
   * show its own copy next to the fields the user was editing.
   */
  async function handleSave(id: string, patch: TodoPatch): Promise<void> {
    setBusyIds((prev) => addBusy(prev, id));
    try {
      const updated = await updateTodo(id, patch);
      setTodos((prev) => prev.map((todo) => (todo.id === id ? updated : todo)));
      setError(null);
      void refreshLists();
      void refreshTags();
    } finally {
      setBusyIds((prev) => removeBusy(prev, id));
    }
  }

  /** Replaces the parent in state so `subtasks` and the counter stay in step. */
  function replaceSubtasks(parentId: string, update: (subtasks: Todo[]) => Todo[]): void {
    setTodos((prev) =>
      prev.map((todo) =>
        todo.id === parentId ? { ...todo, subtasks: update(todo.subtasks) } : todo
      )
    );
  }

  async function handleAddSubtask(parentId: string, title: string): Promise<void> {
    const created = await createSubtask(parentId, { title });
    replaceSubtasks(parentId, (subtasks) => [...subtasks, created]);
  }

  /**
   * Confirms an AI draft (decision D-AI1). The AI endpoints wrote nothing; the
   * todo is created here through the ordinary endpoints, one
   * `POST /api/todos/{id}/subtasks` per subtask the user left checked.
   */
  async function handleConfirmAiDraft(
    draft: TodoDraft,
    subtaskTitles: string[]
  ): Promise<void> {
    const todo = await createTodo({
      ...draft,
      ...(selectedListId ? { list_id: selectedListId } : {}),
    });

    const subtasks: Todo[] = [];
    for (const title of subtaskTitles) {
      subtasks.push(await createSubtask(todo.id, { title }));
    }

    setError(null);
    if (needsRefetchAfterCreate(filters)) {
      // Same rule as the manual add: under an active filter or a non-default
      // sort, only the server knows whether — and where — the row belongs.
      await loadTodos(selectedListId ?? '', filters, true);
    } else {
      setTodos((prev) => [...prev, { ...todo, subtasks }]);
      setTotal((prev) => prev + 1);
    }
    void refreshLists();
    void refreshTags();
  }

  /** Accepted subtask suggestions, created one call at a time. */
  async function handleAddSubtasks(parentId: string, titles: string[]): Promise<void> {
    const created: Todo[] = [];
    for (const title of titles) {
      created.push(await createSubtask(parentId, { title }));
    }
    replaceSubtasks(parentId, (subtasks) => [...subtasks, ...created]);
  }

  /** An accepted metadata suggestion: exactly `priority` and `tags`. */
  async function handleApplyMetadata(
    id: string,
    suggestion: AiMetadataSuggestion
  ): Promise<void> {
    await handleSave(id, { priority: suggestion.priority, tags: suggestion.tags });
  }

  /**
   * One selected subtask operation, on the endpoint that already owns the SSE
   * frames for that kind of write (decision D-IT5-1). The API layer guarantees
   * the shape: `add` has a title, everything else has an id.
   *
   * Returns whether a write actually went out, so the caller only ever counts
   * changes that landed.
   */
  async function applySubtaskOp(parentId: string, op: AiEditOp): Promise<boolean> {
    if (op.action === 'add') {
      await createSubtask(parentId, { title: op.title ?? '' });
      return true;
    }
    // Defence in depth: a *subtask* operation may never touch the todo the
    // user is editing. Deleting a todo is not expressible in the AI schema at
    // all (§1.5), so an id echoing the parent's can only mean a server bug or
    // a tampered response — and a `remove` acting on it would destroy the row.
    if (op.id === null || op.id === parentId) {
      return false;
    }
    if (op.action === 'rename') {
      await updateTodo(op.id, { title: op.title ?? '' });
    } else if (op.action === 'complete') {
      await updateTodo(op.id, { completed: true });
    } else if (op.action === 'reopen') {
      await updateTodo(op.id, { completed: false });
    } else {
      await deleteTodo(op.id);
    }
    return true;
  }

  /**
   * Writes a confirmed AI change set (iteration-5 spec C4). Every field change
   * travels in **one** `PATCH`, so the common case ("rename it", "tag it work")
   * is atomic; the subtask operations then follow in the order the server sent
   * them (`rename` → `complete`/`reopen` → `remove` → `add`).
   *
   * No AI endpoint is involved, which is the point of D-IT5-1: an apply still
   * works if the AI stack died between the preview and the confirmation.
   */
  async function handleApplyAiEdit(id: string, selection: AiEditSelection): Promise<void> {
    const patch: TodoPatch = {};
    if (selection.title !== null) patch.title = selection.title.to;
    if (selection.description !== null) patch.description = selection.description.to;
    if (selection.priority !== null) patch.priority = selection.priority.to;
    if (selection.due_date !== null) patch.due_date = selection.due_date.to;
    if (selection.completed !== null) patch.completed = selection.completed.to;
    if (selection.tags !== null) patch.tags = selection.tags.to;

    // Counted the way the user counted them: one checkbox is one change, and
    // the single PATCH carries however many of them are field changes.
    const total = countChanges(selection);
    const fieldChanges = total - selection.subtasks.length;
    let applied = 0;

    setBusyIds((prev) => addBusy(prev, id));
    try {
      if (fieldChanges > 0) {
        await updateTodo(id, patch);
        applied = fieldChanges;
      }

      for (const op of selection.subtasks) {
        try {
          if (await applySubtaskOp(id, op)) {
            applied += 1;
          }
        } catch (caught) {
          // A 404 on an existing subtask means it was deleted elsewhere
          // between the preview and now (R8): nothing more can be done to it,
          // and for `remove` the wanted end state already holds, so the rest
          // of the change set still goes through. A 404 on `add` is a
          // different animal — the *parent* is gone — and must stop the loop
          // rather than let the panel close as if the edit had worked.
          const stale =
            caught instanceof ApiError && caught.status === 404 && op.action !== 'add';
          if (!stale) {
            throw caught;
          }
          // Skipped, not applied: `Applied n of m` counts writes that landed.
        }
      }
      setError(null);
    } catch (caught) {
      // Nothing written yet means the whole apply can safely be retried, so
      // the panel keeps its preview; once something *has* landed, the failure
      // is terminal and has to be reported honestly (D-IT5-7).
      throw applied === 0 ? caught : new PartialApplyError(applied, total);
    } finally {
      // D-IT5-4: one quiet refetch rather than five state merges. It restores
      // the row, its subtasks, the filter membership and `X-Total-Count` in a
      // single request, and `quiet` keeps the list from blanking.
      await loadTodos(selectedListId ?? '', filters, true);
      void refreshLists();
      void refreshTags();
      setBusyIds((prev) => removeBusy(prev, id));
    }
  }

  async function handleToggleSubtask(
    parentId: string,
    id: string,
    completed: boolean
  ): Promise<void> {
    setBusyIds((prev) => addBusy(prev, id));
    try {
      const updated = await setCompleted(id, completed);
      replaceSubtasks(parentId, (subtasks) =>
        subtasks.map((subtask) => (subtask.id === id ? updated : subtask))
      );
    } finally {
      setBusyIds((prev) => removeBusy(prev, id));
    }
  }

  async function handleDeleteSubtask(parentId: string, id: string): Promise<void> {
    setBusyIds((prev) => addBusy(prev, id));
    try {
      await deleteTodo(id);
      replaceSubtasks(parentId, (subtasks) => subtasks.filter((subtask) => subtask.id !== id));
    } finally {
      setBusyIds((prev) => removeBusy(prev, id));
    }
  }

  const activeCount = todos.filter((todo) => !todo.completed).length;
  const counterText = activeCount === 1 ? '1 item left' : `${activeCount} items left`;
  const filtersActive = hasActiveFilters(filters);
  const listName =
    lists.find((list) => list.id === selectedListId)?.name ?? ALL_LISTS_LABEL;

  // The result summary belongs next to the name of the list it describes; it
  // stays silent while loading and for a genuinely empty, unfiltered list.
  const showSummary = !loading && (todos.length > 0 || filtersActive);
  const summary = !showSummary
    ? ''
    : filtersActive && todos.length === 0
      ? NO_MATCH_MESSAGE
      : `Showing ${todos.length} of ${total} todos`;

  return (
    <div className="min-h-screen bg-slate-100 text-slate-900">
      <AppHeader user={user} onSignOut={logout} />

      <div className="mx-auto flex max-w-[1120px] flex-col gap-6 px-4 py-6 lg:flex-row lg:items-start lg:gap-8">
        <aside className="flex w-full flex-col gap-4 lg:w-[280px] lg:shrink-0">
          <ListNav
            lists={lists}
            selectedListId={selectedListId ?? ''}
            onSelect={handleSelectList}
            onCreate={handleCreateList}
            onRename={handleRenameList}
            onDelete={handleDeleteList}
            disabled={selectedListId === null}
          />
          <FilterBar filters={filters} onChange={setFilters} tags={tags} />
          <div className="px-1">
            <ConnectionStatus mode={connectionMode} />
          </div>
        </aside>

        <main className="flex w-full min-w-0 flex-col gap-4 lg:max-w-[760px]">
          {aiStatus?.enabled === true && (
            <DailySummaryPanel
              available={aiStatus.available}
              reason={aiStatus.reason}
              listId={
                selectedListId === null || selectedListId === '' ? null : selectedListId
              }
            />
          )}

          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <h2 className="text-lg font-semibold text-slate-900">{listName}</h2>
            <div className={`flex items-center gap-2 ${META}`}>
              <p aria-live="polite">{summary}</p>
              {summary !== '' && <span aria-hidden="true">·</span>}
              <p>{counterText}</p>
            </div>
          </div>

          <TodoComposer
            aiStatus={aiStatus}
            onAdd={handleAdd}
            onConfirmDraft={handleConfirmAiDraft}
            disabled={loading}
          />

          {error && (
            <p role="alert" className="text-sm font-medium text-red-700">
              {error}
            </p>
          )}

          {loading ? (
            <p className={META}>{LOADING_MESSAGE}</p>
          ) : todos.length === 0 ? (
            // A filtered-out list is reported by the header's live region.
            filtersActive ? null : (
              <p className={`${SURFACE} p-6 text-center ${META}`}>{EMPTY_MESSAGE}</p>
            )
          ) : (
            <TodoList
              todos={todos}
              today={today}
              busyIds={busyIds}
              onToggle={handleToggle}
              onDelete={handleDelete}
              onSave={handleSave}
              onAddSubtask={handleAddSubtask}
              onToggleSubtask={handleToggleSubtask}
              onDeleteSubtask={handleDeleteSubtask}
              onAddSubtasks={handleAddSubtasks}
              onApplyMetadata={handleApplyMetadata}
              onApplyAiEdit={handleApplyAiEdit}
              aiAvailable={aiStatus?.enabled === true && aiStatus.available}
            />
          )}
        </main>
      </div>
    </div>
  );
}

export default function App() {
  const { status, notice, setNotice } = useAuth();
  const [view, setView] = useState<'login' | 'register'>('login');

  // After signing in, the next signed-out render always starts at Sign in.
  useEffect(() => {
    if (status === 'authenticated') {
      setView('login');
    }
  }, [status]);

  if (status === 'loading') {
    return (
      <main className="mx-auto max-w-xl px-4 py-10">
        <p role="status" className="text-slate-600">
          {SESSION_LOADING_MESSAGE}
        </p>
      </main>
    );
  }

  if (status === 'authenticated') {
    return <TodoApp />;
  }

  const activeView = notice === 'session-expired' ? 'login' : view;

  if (activeView === 'register') {
    return <RegisterScreen onShowLogin={() => setView('login')} />;
  }

  return (
    <LoginScreen
      onShowRegister={() => {
        setNotice(null);
        setView('register');
      }}
    />
  );
}
