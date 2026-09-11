import { useEffect, useRef, useState } from 'react';
import type { AiMetadataSuggestion } from '../api/ai';
import type { Priority, Todo, TodoPatch } from '../api/types';
import { PRIORITY_LABELS, dueLabel, isOverdue } from '../dates';
import AiEditPanel from './AiEditPanel';
import type { AiEditSelection } from './AiEditPanel';
import AiSuggestionPanel from './AiSuggestionPanel';
import type { SuggestionKind } from './AiSuggestionPanel';
import MenuButton from './MenuButton';
import SubtaskList from './SubtaskList';
import TagChips from './TagChips';
import TodoDetail from './TodoDetail';
import { BTN_GHOST, CHECKBOX, FOCUS, SURFACE } from '../styles';

export interface TodoRowHandlers {
  onToggle: (id: string, completed: boolean) => void;
  onDelete: (id: string) => void;
  onSave: (id: string, patch: TodoPatch) => Promise<void>;
  onAddSubtask: (parentId: string, title: string) => Promise<void>;
  onToggleSubtask: (parentId: string, id: string, completed: boolean) => Promise<void>;
  onDeleteSubtask: (parentId: string, id: string) => Promise<void>;
  /** Creates one subtask per accepted AI suggestion (decision D-AI1). */
  onAddSubtasks: (parentId: string, titles: string[]) => Promise<void>;
  /** One `PATCH` carrying exactly the accepted priority and tags. */
  onApplyMetadata: (id: string, suggestion: AiMetadataSuggestion) => Promise<void>;
  /**
   * Writes a confirmed AI change set through the ordinary endpoints
   * (decision D-IT5-1): one `PATCH` for the fields, then the subtask ops.
   */
  onApplyAiEdit: (id: string, selection: AiEditSelection) => Promise<void>;
  /** Hides the AI menu unless the AI stack is up (F5). */
  aiAvailable?: boolean;
}

/**
 * The DOM id of a row's Delete button. `App` addresses the neighbours of a
 * deleted row by id rather than by threading refs through `TodoList`: the
 * intent is one-shot and read exactly once, so a ref registry would be far
 * more machinery than the job needs.
 */
export const deleteButtonId = (id: string): string => `todo-delete-${id}`;

interface TodoItemProps extends TodoRowHandlers {
  todo: Todo;
  /** The browser's local date, used for the Overdue/Today copy. */
  today: string;
  busyIds: Set<string>;
}

/**
 * Priority is a word first and a colour second (F5): the pill always spells the
 * level out, the hue only reinforces it.
 */
const PRIORITY_PILL: Record<Priority, string> = {
  high: 'border-rose-200 bg-rose-100 text-rose-800',
  medium: 'border-amber-200 bg-amber-100 text-amber-800',
  low: 'border-slate-200 bg-slate-100 text-slate-700',
};

export default function TodoItem({
  todo,
  today,
  busyIds,
  onToggle,
  onDelete,
  onSave,
  onAddSubtask,
  onToggleSubtask,
  onDeleteSubtask,
  onAddSubtasks,
  onApplyMetadata,
  onApplyAiEdit,
  aiAvailable = false,
}: TodoItemProps) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  // The three AI panels are mutually exclusive: one row, one proposal at a time.
  const [suggestion, setSuggestion] = useState<SuggestionKind | 'edit' | null>(null);
  const editButtonRef = useRef<HTMLButtonElement>(null);
  const aiMenuRef = useRef<HTMLButtonElement>(null);
  const shouldRestoreFocus = useRef(false);

  const busy = busyIds.has(todo.id);
  const checkboxId = `todo-checkbox-${todo.id}`;
  const toggleLabel = todo.completed
    ? `Mark ${todo.title} as active`
    : `Mark ${todo.title} as completed`;

  const total = todo.subtasks.length;
  const done = todo.subtasks.filter((subtask) => subtask.completed).length;
  const overdue = todo.due_date !== null && isOverdue(todo.due_date, today, todo.completed);
  const showSubtasks = expanded && !editing;

  // Closing the panel returns focus to the control that opened it (F5).
  useEffect(() => {
    if (!editing && shouldRestoreFocus.current) {
      shouldRestoreFocus.current = false;
      editButtonRef.current?.focus();
    }
  }, [editing]);

  const handleToggle = () => {
    if (busy) return;
    onToggle(todo.id, !todo.completed);
  };

  const handleDelete = () => {
    if (busy) return;
    onDelete(todo.id);
  };

  function closeEditor(): void {
    shouldRestoreFocus.current = true;
    setEditing(false);
  }

  async function handleSave(patch: TodoPatch): Promise<void> {
    await onSave(todo.id, patch);
    closeEditor();
  }

  /** Closing a suggestion returns focus to the menu that opened it. */
  function closeSuggestion(): void {
    setSuggestion(null);
    aiMenuRef.current?.focus();
  }

  return (
    // A todo row nests further lists (tag chips, subtasks, AI suggestions), so
    // "a row" cannot be expressed as `listitem` alone — this marks the real ones.
    <li
      data-testid="todo-row"
      aria-busy={busy ? 'true' : undefined}
      className={`group ${SURFACE} p-4 ${todo.completed ? 'bg-slate-50' : ''}`}
    >
      <div className="flex flex-wrap items-start gap-3">
        <input
          id={checkboxId}
          type="checkbox"
          checked={todo.completed}
          aria-disabled={busy || undefined}
          onChange={handleToggle}
          className={`${CHECKBOX} mt-1 h-5 w-5`}
        />
        <label htmlFor={checkboxId} className="sr-only">
          {toggleLabel}
        </label>

        <div className="min-w-0 flex-1 basis-56">
          <p
            className={
              todo.completed
                ? 'text-base text-slate-500 line-through'
                : 'text-base font-medium text-slate-900'
            }
          >
            {todo.title}
          </p>

          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            {/* The priority word is always rendered: never colour alone (F5). */}
            <span
              aria-label={`Priority: ${PRIORITY_LABELS[todo.priority]}`}
              className={`rounded-full border px-2 py-0.5 text-xs font-semibold ${
                PRIORITY_PILL[todo.priority]
              }`}
            >
              {PRIORITY_LABELS[todo.priority]}
            </span>

            {todo.due_date !== null && (
              <span
                className={
                  overdue ? 'text-xs font-semibold text-red-700' : 'text-xs text-slate-600'
                }
              >
                {dueLabel(todo.due_date, today, todo.completed)}
              </span>
            )}

            {todo.tags.length > 0 && <TagChips tags={todo.tags} />}
          </div>

          {total > 0 && (
            <button
              type="button"
              aria-expanded={showSubtasks}
              // The visible "1/2 subtasks" text is part of the accessible name,
              // so Label in Name (WCAG 2.5.3) holds for the progress control.
              aria-label={`${showSubtasks ? 'Hide' : 'Show'} subtasks of ${todo.title} (${done}/${total} subtasks)`}
              onClick={() => setExpanded((open) => !open)}
              className={`mt-2 -ml-1 flex items-center gap-2 rounded px-1 py-0.5 text-xs text-slate-600 hover:text-slate-900 ${FOCUS}`}
            >
              <span>{`${done}/${total} subtasks`}</span>
              <span
                aria-hidden="true"
                className="h-1.5 w-20 overflow-hidden rounded-full bg-slate-200"
              >
                <span
                  className="block h-full rounded-full bg-indigo-500"
                  style={{ width: `${total === 0 ? 0 : Math.round((done / total) * 100)}%` }}
                />
              </span>
              <span aria-hidden="true" className="font-medium">
                {showSubtasks ? 'Hide' : 'Show'}
              </span>
            </button>
          )}
        </div>

        {/* One quiet action group per row: it gains a surface on hover and
            whenever anything inside it has focus, so it never competes with the
            todo itself. On a narrow screen it drops to its own line rather than
            squeezing the title into a column. */}
        <div className="flex w-full shrink-0 items-center justify-end gap-1 rounded-lg p-0.5 group-focus-within:bg-slate-100 group-hover:bg-slate-100 sm:w-auto">
          <button
            ref={editButtonRef}
            type="button"
            aria-label={`Edit ${todo.title}`}
            aria-expanded={editing}
            onClick={() => (editing ? closeEditor() : setEditing(true))}
            className={BTN_GHOST}
          >
            Edit
          </button>
          <button
            type="button"
            id={deleteButtonId(todo.id)}
            aria-label={`Delete ${todo.title}`}
            aria-disabled={busy || undefined}
            onClick={handleDelete}
            className={BTN_GHOST}
          >
            Delete
          </button>
          {aiAvailable && (
            <MenuButton
              label={`AI actions for ${todo.title}`}
              triggerRef={aiMenuRef}
              triggerClassName={BTN_GHOST}
              actions={[
                // `<label>: <title>` so the visible words are the start of the
                // accessible name (WCAG 2.5.3 Label in Name).
                {
                  id: 'subtasks',
                  label: 'Split into subtasks',
                  name: `Split into subtasks: ${todo.title}`,
                  onSelect: () => setSuggestion('subtasks'),
                },
                {
                  id: 'metadata',
                  label: 'Suggest priority and tags',
                  name: `Suggest priority and tags: ${todo.title}`,
                  onSelect: () => setSuggestion('metadata'),
                },
                {
                  id: 'edit',
                  label: 'Edit with AI',
                  name: `Edit with AI: ${todo.title}`,
                  onSelect: () => setSuggestion('edit'),
                },
              ]}
            >
              AI
            </MenuButton>
          )}
        </div>
      </div>

      {aiAvailable && suggestion === 'edit' && (
        <AiEditPanel
          todo={todo}
          onApply={(selection) => onApplyAiEdit(todo.id, selection)}
          onClose={closeSuggestion}
        />
      )}

      {aiAvailable && suggestion !== null && suggestion !== 'edit' && (
        <AiSuggestionPanel
          todo={todo}
          kind={suggestion}
          onAddSubtasks={(titles) => onAddSubtasks(todo.id, titles)}
          onApplyMetadata={(applied) => onApplyMetadata(todo.id, applied)}
          onClose={closeSuggestion}
        />
      )}

      {/* The panel already lists the subtasks, so the two never show at once. */}
      {showSubtasks && (
        <SubtaskList
          parentId={todo.id}
          subtasks={todo.subtasks}
          busyIds={busyIds}
          onToggle={onToggleSubtask}
          onDelete={onDeleteSubtask}
        />
      )}

      {editing && (
        <TodoDetail
          todo={todo}
          busyIds={busyIds}
          onSave={handleSave}
          onCancel={closeEditor}
          onAddSubtask={onAddSubtask}
          onToggleSubtask={onToggleSubtask}
          onDeleteSubtask={onDeleteSubtask}
        />
      )}
    </li>
  );
}
