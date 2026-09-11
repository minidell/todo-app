import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { Priority, Todo, TodoPatch } from '../api/types';
import DueDateField from './DueDateField';
import PrioritySelect from './PrioritySelect';
import TagInput from './TagInput';
import { BTN_PRIMARY, BTN_SECONDARY, FIELD_LABEL, INPUT } from '../styles';

export const SAVE_ERROR_MESSAGE = 'Could not save changes. Please try again.';

interface TodoEditFormProps {
  todo: Todo;
  /** Receives only the changed fields; never called with an empty patch. */
  onSave: (patch: TodoPatch) => Promise<void>;
  onCancel: () => void;
}

function sameTags(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const left = [...a].sort();
  const right = [...b].sort();
  return left.every((tag, index) => tag === right[index]);
}

/**
 * The editable fields of a todo. Builds a **partial** PATCH body containing
 * only what the user actually changed (master §6.3); an unchanged form closes
 * without touching the API, since `{}` would be a 400 `empty_update`.
 */
export default function TodoEditForm({ todo, onSave, onCancel }: TodoEditFormProps) {
  const [title, setTitle] = useState(todo.title);
  const [description, setDescription] = useState(todo.description ?? '');
  const [priority, setPriority] = useState<Priority>(todo.priority);
  const [dueDate, setDueDate] = useState<string | null>(todo.due_date);
  const [tags, setTags] = useState<string[]>(todo.tags);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const titleRef = useRef<HTMLInputElement>(null);
  const titleId = `edit-title-${todo.id}`;
  const descriptionId = `edit-description-${todo.id}`;

  // The panel opens on the Title field (slice-3 spec F5).
  useEffect(() => {
    titleRef.current?.focus();
  }, []);

  function buildPatch(): TodoPatch {
    const patch: TodoPatch = {};
    const nextTitle = title.trim();
    const nextDescription = description.trim() === '' ? null : description.trim();

    if (nextTitle !== todo.title) patch.title = nextTitle;
    if (nextDescription !== todo.description) patch.description = nextDescription;
    if (priority !== todo.priority) patch.priority = priority;
    if (dueDate !== todo.due_date) patch.due_date = dueDate;
    if (!sameTags(tags, todo.tags)) patch.tags = tags;

    return patch;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (saving || title.trim() === '') {
      return;
    }

    const patch = buildPatch();
    if (Object.keys(patch).length === 0) {
      onCancel();
      return;
    }

    setSaving(true);
    setError(null);
    try {
      await onSave(patch);
    } catch {
      setError(SAVE_ERROR_MESSAGE);
      setSaving(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <label htmlFor={titleId} className={FIELD_LABEL}>
          Title
        </label>
        <input
          ref={titleRef}
          id={titleId}
          type="text"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          maxLength={200}
          autoComplete="off"
          className={INPUT}
        />
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor={descriptionId} className={FIELD_LABEL}>
          Description
        </label>
        <textarea
          id={descriptionId}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          maxLength={2000}
          rows={3}
          className={INPUT}
        />
      </div>

      <div className="flex flex-wrap gap-4">
        <PrioritySelect
          id={`edit-priority-${todo.id}`}
          label="Priority"
          value={priority}
          onChange={setPriority}
        />
        <DueDateField
          id={`edit-due-date-${todo.id}`}
          label="Due date"
          value={dueDate}
          onChange={setDueDate}
        />
      </div>

      <TagInput label="Tags" tags={tags} onChange={setTags} />

      <div className="flex gap-2">
        <button
          type="submit"
          aria-busy={saving ? 'true' : undefined}
          aria-disabled={saving || undefined}
          disabled={title.trim() === ''}
          className={BTN_PRIMARY}
        >
          {saving ? 'Saving…' : 'Save changes'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className={BTN_SECONDARY}
        >
          Cancel
        </button>
      </div>

      {error && (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      )}
    </form>
  );
}
