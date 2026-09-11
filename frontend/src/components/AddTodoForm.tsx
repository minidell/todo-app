import { useId, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import type { FormEvent } from 'react';
import type { Priority, SubtaskDraft } from '../api/types';
import DueDateField from './DueDateField';
import PrioritySelect from './PrioritySelect';
import TagInput from './TagInput';
import { BTN_PRIMARY_LG, FOCUS, INPUT_LG } from '../styles';

interface AddTodoFormProps {
  /** Receives the title plus any enrichment the user filled in. */
  onAdd: (draft: SubtaskDraft) => Promise<void>;
  disabled?: boolean;
}

/** Stable, so `App` can hand focus back here after the last row is deleted. */
export const TITLE_INPUT_ID = 'new-todo-title';

/**
 * The manual composer: one prominent title field and one primary button.
 *
 * Priority, due date and tags are real but secondary, so they sit behind a
 * "More options" disclosure inside the same card (iteration-3 redesign) rather
 * than competing with the title. Once opened the panel stays open — adding
 * several enriched todos in a row is the case that needs it — and every field
 * resets to its default after a successful add.
 */
export default function AddTodoForm({ onAdd, disabled = false }: AddTodoFormProps) {
  const optionsId = useId();
  const [value, setValue] = useState('');
  const [priority, setPriority] = useState<Priority>('medium');
  const [dueDate, setDueDate] = useState<string | null>(null);
  const [tags, setTags] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [showOptions, setShowOptions] = useState(false);
  // Bumped after a successful add so the tag field drops any pending text.
  const [resetKey, setResetKey] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const trimmed = value.trim();
  const isDisabled = disabled || submitting;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!trimmed || isDisabled) {
      return;
    }
    // Only send what the user actually set; the rest takes the server default.
    const draft: SubtaskDraft = { title: trimmed };
    if (priority !== 'medium') draft.priority = priority;
    if (dueDate !== null) draft.due_date = dueDate;
    if (tags.length > 0) draft.tags = tags;

    setSubmitting(true);
    try {
      await onAdd(draft);
      // Flush synchronously so the input is re-enabled in the DOM before we
      // try to focus it (a disabled element cannot receive focus).
      flushSync(() => {
        setValue('');
        setPriority('medium');
        setDueDate(null);
        setTags([]);
        setResetKey((key) => key + 1);
        setSubmitting(false);
      });
      inputRef.current?.focus();
    } catch {
      // Error message is surfaced by the parent; keep the input value so the
      // user can retry without retyping.
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <div className="flex flex-col gap-2 sm:flex-row">
        <label htmlFor={TITLE_INPUT_ID} className="sr-only">
          New todo title
        </label>
        <input
          ref={inputRef}
          id={TITLE_INPUT_ID}
          type="text"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="What needs to be done?"
          maxLength={200}
          autoComplete="off"
          disabled={isDisabled}
          className={`${INPUT_LG} flex-1`}
        />
        <button
          type="submit"
          disabled={isDisabled || !trimmed}
          className={`${BTN_PRIMARY_LG} shrink-0`}
        >
          Add
        </button>
      </div>

      <div>
        <button
          type="button"
          aria-expanded={showOptions}
          aria-controls={optionsId}
          onClick={() => setShowOptions((open) => !open)}
          className={`rounded text-sm font-medium text-slate-600 hover:text-slate-900 ${FOCUS}`}
        >
          <span aria-hidden="true">{showOptions ? '▾ ' : '▸ '}</span>
          More options
        </button>
      </div>

      {showOptions && (
        <div
          id={optionsId}
          className="flex flex-wrap items-start gap-4 border-t border-slate-200 pt-3"
        >
          <PrioritySelect
            id="new-todo-priority"
            label="New todo priority"
            value={priority}
            onChange={setPriority}
          />
          <DueDateField
            id="new-todo-due-date"
            label="New todo due date"
            value={dueDate}
            onChange={setDueDate}
          />
          <TagInput key={resetKey} label="New todo tags" tags={tags} onChange={setTags} />
        </div>
      )}
    </form>
  );
}
