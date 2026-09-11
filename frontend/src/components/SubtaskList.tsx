import { useId, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { Todo } from '../api/types';
import { BTN_GHOST, BTN_SECONDARY, CHECKBOX, INPUT } from '../styles';

export const SUBTASK_ERROR_MESSAGE = 'Could not update the subtask. Please try again.';

interface SubtaskListProps {
  parentId: string;
  subtasks: Todo[];
  busyIds: Set<string>;
  onToggle: (parentId: string, id: string, completed: boolean) => Promise<void>;
  onDelete: (parentId: string, id: string) => Promise<void>;
  /** When present, the add-a-subtask form is rendered below the list. */
  onAdd?: (parentId: string, title: string) => Promise<void>;
}

/**
 * One level of subtasks under a parent todo (invariant I1). Completion is
 * independent of the parent (decision D-A2). Busy controls follow the C7 rule:
 * `aria-disabled`, never `disabled`, so focus is never taken away.
 */
export default function SubtaskList({
  parentId,
  subtasks,
  busyIds,
  onToggle,
  onDelete,
  onAdd,
}: SubtaskListProps) {
  const inputId = useId();
  const [title, setTitle] = useState('');
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const trimmed = title.trim();

  async function run(action: () => Promise<void>): Promise<void> {
    setError(null);
    try {
      await action();
    } catch {
      setError(SUBTASK_ERROR_MESSAGE);
    }
  }

  async function handleAdd(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!onAdd || !trimmed || adding) {
      return;
    }
    setAdding(true);
    setError(null);
    try {
      await onAdd(parentId, trimmed);
      setTitle('');
      setAdding(false);
      // Nothing was ever `disabled`, so focus was never taken away; this only
      // brings it back when the user submitted with the button.
      inputRef.current?.focus();
    } catch {
      setError(SUBTASK_ERROR_MESSAGE);
      setAdding(false);
    }
  }

  return (
    <div className="mt-3 ml-8">
      {subtasks.length > 0 && (
        <ul className="flex flex-col divide-y divide-slate-100 border-l-2 border-slate-200 pl-3">
          {subtasks.map((subtask) => {
            const busy = busyIds.has(subtask.id);
            const checkboxId = `subtask-checkbox-${subtask.id}`;
            const toggleLabel = subtask.completed
              ? `Mark ${subtask.title} as active`
              : `Mark ${subtask.title} as completed`;

            return (
              <li
                key={subtask.id}
                aria-busy={busy ? 'true' : undefined}
                className="flex items-center gap-3 py-1.5"
              >
                <input
                  id={checkboxId}
                  type="checkbox"
                  checked={subtask.completed}
                  aria-disabled={busy || undefined}
                  onChange={() => {
                    if (busy) return;
                    void run(() => onToggle(parentId, subtask.id, !subtask.completed));
                  }}
                  className={CHECKBOX}
                />
                <label htmlFor={checkboxId} className="sr-only">
                  {toggleLabel}
                </label>
                <span
                  className={
                    subtask.completed
                      ? 'flex-1 text-sm text-slate-500 line-through'
                      : 'flex-1 text-sm text-slate-900'
                  }
                >
                  {subtask.title}
                </span>
                <button
                  type="button"
                  aria-label={`Delete subtask ${subtask.title}`}
                  aria-disabled={busy || undefined}
                  onClick={() => {
                    if (busy) return;
                    void run(() => onDelete(parentId, subtask.id));
                  }}
                  className={`${BTN_GHOST} text-xs`}
                >
                  Delete
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {onAdd && (
        <form
          onSubmit={handleAdd}
          aria-busy={adding ? 'true' : undefined}
          className="mt-2 flex gap-2 pl-3"
        >
          <label htmlFor={inputId} className="sr-only">
            New subtask title
          </label>
          <input
            ref={inputRef}
            id={inputId}
            type="text"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Add a subtask"
            maxLength={200}
            autoComplete="off"
            aria-disabled={adding || undefined}
            className={`${INPUT} flex-1`}
          />
          {/* Busy uses aria-disabled only (C7): focus must never be taken. */}
          <button
            type="submit"
            disabled={!trimmed}
            aria-disabled={adding || undefined}
            className={`${BTN_SECONDARY} shrink-0`}
          >
            Add subtask
          </button>
        </form>
      )}

      {error && (
        <p role="alert" className="mt-2 pl-3 text-sm text-red-700">
          {error}
        </p>
      )}
    </div>
  );
}
