import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type {
  AiEditChangeSet,
  AiEditOp,
  AiEditResult,
  AiFieldChange,
  AiTagsChange,
} from '../api/ai';
import {
  AI_SLOW_HINT,
  MAX_INSTRUCTION_LENGTH,
  PartialApplyError,
  aiErrorMessage,
  countChanges,
  editTodo,
  isAbortError,
} from '../api/ai';
import type { Priority, Todo } from '../api/types';
import { PRIORITY_LABELS, formatFullDate, todayString } from '../dates';
import {
  AI_SURFACE,
  BTN_PRIMARY,
  BTN_SECONDARY,
  CHECKBOX,
  FIELD_LABEL,
  FOCUS,
  INPUT,
  META_SMALL,
} from '../styles';

/**
 * Criterion 15. Client-side copy on purpose (decision D-IT5-8): no prose the
 * model wrote ever reaches the page, so a crafted instruction cannot be
 * reflected back into the UI.
 */
export const NO_CHANGES_MESSAGE =
  'The AI did not find anything to change for that instruction. Try being more specific.';
export const EDIT_APPLY_ERROR_MESSAGE = 'Could not apply the changes. Please try again.';
export const SUBTASKS_TRUNCATED_NOTE = 'Only the first 20 subtasks were sent to the AI.';

/** The honest terminal message of decision D-IT5-7. */
export function partialApplyMessage(applied: number, total: number): string {
  return `Applied ${applied} of ${total} changes. Please try again.`;
}

/** Mirrors the server's `MAX_TAGS_PER_TODO`; see `selectionOf`. */
const MAX_TAGS = 10;

/**
 * One row of the preview — one checkbox, one label. A tag change becomes one
 * of these *per name*, because that is the granularity the user deselects at.
 */
export type AiEditChange =
  | { kind: 'title'; change: AiFieldChange<string> }
  | { kind: 'description'; change: AiFieldChange<string | null> }
  | { kind: 'priority'; change: AiFieldChange<Priority> }
  | { kind: 'due_date'; change: AiFieldChange<string | null> }
  | { kind: 'completed'; change: AiFieldChange<boolean> }
  | { kind: 'tag'; op: 'add' | 'remove'; name: string }
  | { kind: 'subtask'; op: AiEditOp };

/** A change plus the key the selection is tracked by. */
export interface AiEditRow {
  key: string;
  change: AiEditChange;
}

/**
 * What the user left checked. It is a change set of the same shape as the one
 * the server sent — with every deselected entry `null` — so `countChanges` and
 * the apply loop work on it unchanged.
 */
export type AiEditSelection = AiEditChangeSet;

const tagKey = (op: 'add' | 'remove', name: string): string => `tag-${op}-${name}`;
const subtaskKey = (index: number): string => `subtask-${index}`;

/** Flattens a change set into the rows the preview renders, in reading order. */
export function listChanges(changeSet: AiEditChangeSet): AiEditRow[] {
  const rows: AiEditRow[] = [];
  if (changeSet.title !== null) {
    rows.push({ key: 'title', change: { kind: 'title', change: changeSet.title } });
  }
  if (changeSet.description !== null) {
    rows.push({
      key: 'description',
      change: { kind: 'description', change: changeSet.description },
    });
  }
  if (changeSet.priority !== null) {
    rows.push({ key: 'priority', change: { kind: 'priority', change: changeSet.priority } });
  }
  if (changeSet.due_date !== null) {
    rows.push({ key: 'due_date', change: { kind: 'due_date', change: changeSet.due_date } });
  }
  if (changeSet.completed !== null) {
    rows.push({ key: 'completed', change: { kind: 'completed', change: changeSet.completed } });
  }
  for (const name of changeSet.tags?.added ?? []) {
    rows.push({ key: tagKey('add', name), change: { kind: 'tag', op: 'add', name } });
  }
  for (const name of changeSet.tags?.removed ?? []) {
    rows.push({ key: tagKey('remove', name), change: { kind: 'tag', op: 'remove', name } });
  }
  changeSet.subtasks.forEach((op, index) => {
    rows.push({ key: subtaskKey(index), change: { kind: 'subtask', op } });
  });
  return rows;
}

function subtaskLabel(op: AiEditOp): string {
  const verb = {
    add: 'Add subtask',
    rename: 'Rename subtask',
    remove: 'Remove subtask',
    complete: 'Complete subtask',
    reopen: 'Reopen subtask',
  }[op.action];

  if (op.action === 'add') {
    return `${verb}: ${op.title ?? ''}`;
  }
  if (op.from_title === null) {
    // The server always sends `from_title` for an operation on an existing
    // subtask; without it the verb alone still says what would happen.
    return op.action === 'rename' && op.title !== null ? `${verb}: ${op.title}` : verb;
  }
  return op.action === 'rename'
    ? `${verb}: ${op.from_title} → ${op.title ?? ''}`
    : `${verb}: ${op.from_title}`;
}

/**
 * The one place a change becomes words. The checkbox's `aria-label` and its
 * visible `<label>` are both this string, so "Label in Name" (WCAG 2.5.3)
 * holds by construction and the two can never drift apart.
 */
export function changeLabel(change: AiEditChange): string {
  switch (change.kind) {
    case 'title':
      return `Title: ${change.change.from} → ${change.change.to}`;
    case 'description':
      return change.change.to === null
        ? `Description: remove “${change.change.from ?? ''}”`
        : `Description: → ${change.change.to}`;
    case 'priority':
      return `Priority: ${PRIORITY_LABELS[change.change.from]} → ${
        PRIORITY_LABELS[change.change.to]
      }`;
    case 'due_date':
      return change.change.to === null
        ? `Due date: remove ${formatFullDate(change.change.from ?? '')}`
        : `Due date: ${
            change.change.from === null ? 'none' : formatFullDate(change.change.from)
          } → ${formatFullDate(change.change.to)}`;
    case 'completed':
      return change.change.to ? 'Mark as completed' : 'Mark as active';
    case 'tag':
      return change.op === 'add' ? `Add tag ${change.name}` : `Remove tag ${change.name}`;
    case 'subtask':
      return subtaskLabel(change.op);
  }
}

/**
 * True for a change that takes something away. Such a row is red *and* says
 * "remove" — state is never conveyed by colour alone (criterion 21).
 */
export function isRemoval(change: AiEditChange): boolean {
  switch (change.kind) {
    case 'description':
    case 'due_date':
      return change.change.to === null;
    case 'tag':
      return change.op === 'remove';
    case 'subtask':
      return change.op.action === 'remove';
    default:
      return false;
  }
}

/**
 * The tag change for the names still checked, or `null` when none are.
 *
 * `added` is the list that *fits*: deselecting a removal can take the todo
 * back to the 10-tag cap, and a name that cannot be written must not stay in
 * the selection — otherwise the panel would count a change it cannot make and
 * then report success for a PATCH that changed nothing.
 */
function selectedTags(tags: AiTagsChange | null, excluded: Set<string>): AiTagsChange | null {
  if (tags === null) {
    return null;
  }
  const wanted = tags.added.filter((name) => !excluded.has(tagKey('add', name)));
  const removed = tags.removed.filter((name) => !excluded.has(tagKey('remove', name)));
  if (wanted.length === 0 && removed.length === 0) {
    return null;
  }
  if (wanted.length === tags.added.length && removed.length === tags.removed.length) {
    // Nothing was deselected, so the server's own final list stands — it is
    // already de-duplicated, normalized and capped by the R6 ordering rule.
    return { ...tags };
  }
  const kept = tags.from.filter((name) => !removed.includes(name));
  // Same rule as the server's: over the cap the *added* names go first, so a
  // partial selection can never delete a tag the user already had.
  const room = Math.max(0, MAX_TAGS - kept.length);
  const added = wanted.filter((name) => !kept.includes(name)).slice(0, room);
  if (added.length === 0 && removed.length === 0) {
    return null;
  }
  return { from: tags.from, to: [...kept, ...added], added, removed };
}

/**
 * The checked "Add tag" names that `selectedTags` had to drop because the todo
 * is at the cap. They stay on screen as checked rows — silently unchecking a
 * row the user just looked at would be worse — so the panel says why they are
 * not in the count.
 */
export function droppedTagAdds(
  changeSet: AiEditChangeSet,
  selection: AiEditSelection,
  excluded: Set<string>,
): string[] {
  const accepted = selection.tags?.added ?? [];
  return (changeSet.tags?.added ?? []).filter(
    (name) => !excluded.has(tagKey('add', name)) && !accepted.includes(name),
  );
}

export function tagLimitMessage(names: string[]): string {
  const list = names.map((name) => `“${name}”`).join(', ');
  return `A todo can have at most ${MAX_TAGS} tags, so ${list} will not be added. Keep a tag removal checked to make room.`;
}

/** The change set minus everything the user unchecked. */
export function selectionOf(changeSet: AiEditChangeSet, excluded: Set<string>): AiEditSelection {
  const keep = <T,>(key: string, value: T | null): T | null =>
    excluded.has(key) ? null : value;

  return {
    title: keep('title', changeSet.title),
    description: keep('description', changeSet.description),
    priority: keep('priority', changeSet.priority),
    due_date: keep('due_date', changeSet.due_date),
    completed: keep('completed', changeSet.completed),
    tags: selectedTags(changeSet.tags, excluded),
    subtasks: changeSet.subtasks.filter((_, index) => !excluded.has(subtaskKey(index))),
  };
}

interface AiEditPanelProps {
  todo: Todo;
  /** Applies the confirmed selection through the ordinary endpoints (D-IT5-1). */
  onApply: (selection: AiEditSelection) => Promise<void>;
  onClose: () => void;
}

/** Where a one-shot focus move should land after the next render. */
type FocusTarget = 'instruction' | 'preview' | 'close' | null;

/**
 * "Edit with AI": a free-text instruction becomes a change set the user reads,
 * deselects parts of and confirms. Two stages — instruction, then preview —
 * plus one terminal state for an apply that failed part-way through.
 *
 * Nothing is written until `Apply` (decision D-AI1), and the apply itself never
 * touches an AI endpoint, so it still succeeds if the AI stack died between the
 * preview and the confirmation.
 */
export default function AiEditPanel({ todo, onApply, onClose }: AiEditPanelProps) {
  const fieldPrefix = useId();
  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [result, setResult] = useState<AiEditResult | null>(null);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [applying, setApplying] = useState(false);
  const [partial, setPartial] = useState<{ applied: number; total: number } | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const requestRef = useRef<AbortController | null>(null);
  // Armed by the transition that needs it and read exactly once, like the
  // row's own post-edit focus guard: a re-render for any other reason must
  // never move the keyboard again.
  const pendingFocus = useRef<FocusTarget>('instruction');

  const textareaId = `${fieldPrefix}-instruction`;
  const hintId = `${fieldPrefix}-hint`;

  useEffect(() => {
    const target = pendingFocus.current;
    if (target === null) {
      return;
    }
    pendingFocus.current = null;
    if (target === 'instruction') {
      textareaRef.current?.focus();
    } else if (target === 'preview') {
      // The `Ask the AI` button is gone once the preview replaces the form, so
      // "leave focus where it was" would mean leaving it on `body`. The
      // heading is the first thing in the new content instead.
      headingRef.current?.focus();
    } else {
      closeRef.current?.focus();
    }
  });

  // An in-flight proposal must not outlive the panel.
  useEffect(() => () => requestRef.current?.abort(), []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const trimmed = instruction.trim();
    if (busy || trimmed === '') {
      return;
    }

    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;

    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      // The caller's *local* date: "tomorrow" must mean their tomorrow.
      const proposal = await editTodo(todo.id, trimmed, todayString(), controller.signal);
      if (proposal.empty) {
        // Stay on stage 1 with the instruction intact so it can be rephrased.
        setNotice(NO_CHANGES_MESSAGE);
        return;
      }
      setResult(proposal);
      setExcluded(new Set());
      pendingFocus.current = 'preview';
    } catch (caught) {
      if (isAbortError(caught)) {
        return;
      }
      setError(aiErrorMessage(caught));
    } finally {
      if (requestRef.current === controller) {
        requestRef.current = null;
        setBusy(false);
      }
    }
  }

  function toggle(key: string): void {
    setExcluded((previous) => {
      const next = new Set(previous);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  function handleBack(): void {
    setResult(null);
    setError(null);
    pendingFocus.current = 'instruction';
  }

  // Derived once per render: the preview, what is still checked, and what the
  // tag cap forced out of the selection.
  const rows = result === null ? [] : listChanges(result.change_set);
  const selection = result === null ? null : selectionOf(result.change_set, excluded);
  const selectedCount = selection === null ? 0 : countChanges(selection);
  const droppedTags =
    result === null || selection === null
      ? []
      : droppedTagAdds(result.change_set, selection, excluded);

  async function handleApply(): Promise<void> {
    if (applying || selection === null || selectedCount === 0) {
      return;
    }

    setApplying(true);
    setError(null);
    try {
      await onApply(selection);
      onClose();
    } catch (caught) {
      if (caught instanceof PartialApplyError) {
        // Terminal: part of the edit is already written, and re-applying an
        // `add` would duplicate a subtask (D-IT5-7).
        setPartial({ applied: caught.applied, total: caught.total });
        pendingFocus.current = 'close';
      } else {
        setError(EDIT_APPLY_ERROR_MESSAGE);
      }
      setApplying(false);
    }
  }

  return (
    <section aria-label={`Edit ${todo.title} with AI`} className={`mt-3 ml-8 ${AI_SURFACE} p-3`}>
      {partial !== null ? (
        <>
          <p role="alert" className="text-sm font-medium text-red-700">
            {partialApplyMessage(partial.applied, partial.total)}
          </p>
          <div className="mt-2 flex gap-2">
            {/* No retry: the panel must never re-apply what it already applied. */}
            <button ref={closeRef} type="button" onClick={onClose} className={BTN_SECONDARY}>
              Close
            </button>
          </div>
        </>
      ) : result === null ? (
        <>
          <form onSubmit={handleSubmit} className="flex flex-col gap-2">
            <label htmlFor={textareaId} className={FIELD_LABEL}>
              What should the AI change?
            </label>
            <textarea
              ref={textareaRef}
              id={textareaId}
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              placeholder="e.g. rename it to Call the dentist and add a step to find the number"
              aria-describedby={hintId}
              rows={2}
              maxLength={MAX_INSTRUCTION_LENGTH}
              className={INPUT}
            />
            <p id={hintId} className={META_SMALL}>
              {AI_SLOW_HINT}
            </p>
            <div className="flex gap-2">
              {/* Busy is `aria-busy`, never `disabled`: focus must not be taken. */}
              <button
                type="submit"
                aria-busy={busy ? 'true' : undefined}
                aria-disabled={busy || undefined}
                className={BTN_PRIMARY}
              >
                {busy ? 'Thinking…' : 'Ask the AI'}
              </button>
              <button type="button" onClick={onClose} className={BTN_SECONDARY}>
                Cancel
              </button>
            </div>
          </form>

          {notice !== null && (
            <p role="status" className="mt-2 text-sm text-slate-700">
              {notice}
            </p>
          )}
        </>
      ) : (
        <>
          <div aria-live="polite">
            <h4
              ref={headingRef}
              tabIndex={-1}
              className={`rounded text-sm font-semibold text-indigo-900 ${FOCUS}`}
            >
              Suggested changes
            </h4>

            {result.context.subtasks_truncated && (
              <p className={`mt-1 ${META_SMALL}`}>{SUBTASKS_TRUNCATED_NOTE}</p>
            )}

            <ul className="mt-2">
              {rows.map(({ key, change }) => {
                const checkboxId = `${fieldPrefix}-${key}`;
                const label = changeLabel(change);
                return (
                  <li key={key} className="flex items-start gap-2 py-0.5">
                    {/* The `aria-label` is the visible label, word for word. */}
                    <input
                      id={checkboxId}
                      type="checkbox"
                      checked={!excluded.has(key)}
                      aria-label={label}
                      onChange={() => toggle(key)}
                      className={`${CHECKBOX} mt-0.5`}
                    />
                    {/* A label can be a whole description, so it wraps rather
                        than pushing the row into a horizontal scroll at 390px. */}
                    <label
                      htmlFor={checkboxId}
                      className={`min-w-0 text-sm break-words ${
                        isRemoval(change) ? 'text-red-700' : 'text-slate-900'
                      }`}
                    >
                      {label}
                    </label>
                  </li>
                );
              })}
            </ul>

            {/* Why a checked row is not in the count: never a silent no-op. */}
            {droppedTags.length > 0 && (
              <p role="status" className="mt-1 text-xs font-medium text-red-700">
                {tagLimitMessage(droppedTags)}
              </p>
            )}
          </div>

          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              aria-busy={applying ? 'true' : undefined}
              aria-disabled={applying || selectedCount === 0 || undefined}
              onClick={() => void handleApply()}
              className={BTN_PRIMARY}
            >
              {`Apply ${selectedCount} changes`}
            </button>
            <button type="button" onClick={handleBack} className={BTN_SECONDARY}>
              Back
            </button>
            <button type="button" onClick={onClose} className={BTN_SECONDARY}>
              Cancel
            </button>
          </div>
        </>
      )}

      {/* One alert for both stages: an AI failure on stage 1 and an apply
          failure on stage 2 read in the same place, under the buttons. */}
      {error !== null && (
        <p role="alert" className="mt-2 text-sm text-red-700">
          {error}
        </p>
      )}
    </section>
  );
}
