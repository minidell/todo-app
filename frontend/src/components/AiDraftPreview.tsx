import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { AiTodoDraft } from '../api/ai';
import type { Priority, TodoDraft } from '../api/types';
import DueDateField from './DueDateField';
import PrioritySelect from './PrioritySelect';
import TagInput from './TagInput';
import { AI_SURFACE, BTN_PRIMARY, BTN_SECONDARY, CHECKBOX, FIELD_LABEL, INPUT } from '../styles';

export const DRAFT_SAVE_ERROR_MESSAGE = 'Could not add todo. Please try again.';

export interface ConfirmDraft {
  (draft: TodoDraft, subtaskTitles: string[]): Promise<void>;
}

interface AiDraftPreviewProps {
  draft: AiTodoDraft;
  onConfirm: ConfirmDraft;
  onDiscard: () => void;
}

/**
 * The draft-then-confirm step of decision D-AI1: nothing the model produced is
 * persisted until the user has read it, edited whatever they want and pressed
 * `Add this todo`. Confirming uses the ordinary `POST /api/todos` (+ one
 * `POST /api/todos/{id}/subtasks` per checked subtask), never an AI endpoint.
 */
export default function AiDraftPreview({ draft, onConfirm, onDiscard }: AiDraftPreviewProps) {
  const fieldPrefix = useId();
  const [title, setTitle] = useState(draft.title);
  const [description, setDescription] = useState(draft.description ?? '');
  const [priority, setPriority] = useState<Priority>(draft.priority);
  const [dueDate, setDueDate] = useState<string | null>(draft.due_date);
  const [tags, setTags] = useState<string[]>(draft.tags);
  // Every suggested subtask starts checked; unchecking one leaves it out.
  const [excluded, setExcluded] = useState<Set<number>>(new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const titleRef = useRef<HTMLInputElement>(null);
  const headingId = `${fieldPrefix}-heading`;
  const titleId = `${fieldPrefix}-title`;
  const descriptionId = `${fieldPrefix}-description`;

  // Focus lands on the first thing the user may want to correct (F5).
  useEffect(() => {
    titleRef.current?.focus();
  }, []);

  function toggleSubtask(index: number): void {
    setExcluded((previous) => {
      const next = new Set(previous);
      if (next.has(index)) {
        next.delete(index);
      } else {
        next.add(index);
      }
      return next;
    });
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const trimmed = title.trim();
    if (saving || trimmed === '') {
      return;
    }

    const todo: TodoDraft = {
      title: trimmed,
      description: description.trim() === '' ? null : description.trim(),
      priority,
      due_date: dueDate,
      tags,
    };
    const subtaskTitles = draft.subtasks
      .map((subtask, index) => (excluded.has(index) ? null : subtask.title))
      .filter((subtaskTitle): subtaskTitle is string => subtaskTitle !== null);

    setSaving(true);
    setError(null);
    try {
      await onConfirm(todo, subtaskTitles);
    } catch {
      setError(DRAFT_SAVE_ERROR_MESSAGE);
      setSaving(false);
    }
  }

  return (
    <section
      aria-labelledby={headingId}
      className={`mt-3 ${AI_SURFACE} p-3`}
    >
      <h3 id={headingId} className="text-sm font-semibold text-indigo-900">
        Suggested todo
      </h3>

      <form onSubmit={handleSubmit} className="mt-3 flex flex-col gap-3">
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
            rows={2}
            className={INPUT}
          />
        </div>

        <div className="flex flex-wrap gap-4">
          <PrioritySelect
            id={`${fieldPrefix}-priority`}
            label="Priority"
            value={priority}
            onChange={setPriority}
          />
          <DueDateField
            id={`${fieldPrefix}-due-date`}
            label="Due date"
            value={dueDate}
            onChange={setDueDate}
          />
        </div>

        <TagInput label="Tags" tags={tags} onChange={setTags} />

        {draft.subtasks.length > 0 && (
          <fieldset className="flex flex-col gap-1">
            <legend className={FIELD_LABEL}>Suggested subtasks</legend>
            <ul>
              {draft.subtasks.map((subtask, index) => {
                const checkboxId = `${fieldPrefix}-subtask-${index}`;
                return (
                  <li key={checkboxId} className="flex items-center gap-2 py-0.5">
                    {/* The visible text is part of the accessible name, so
                        "Label in Name" (WCAG 2.5.3) still holds. */}
                    <input
                      id={checkboxId}
                      type="checkbox"
                      checked={!excluded.has(index)}
                      aria-label={`Include subtask ${subtask.title}`}
                      onChange={() => toggleSubtask(index)}
                      className={CHECKBOX}
                    />
                    <label htmlFor={checkboxId} className="text-sm text-slate-900">
                      {subtask.title}
                    </label>
                  </li>
                );
              })}
            </ul>
          </fieldset>
        )}

        <div className="flex gap-2">
          {/* Busy is `aria-busy` only, never `disabled` (carry-over C7). */}
          <button
            type="submit"
            aria-busy={saving ? 'true' : undefined}
            aria-disabled={saving || undefined}
            disabled={title.trim() === ''}
            className={BTN_PRIMARY}
          >
            Add this todo
          </button>
          <button
            type="button"
            onClick={onDiscard}
            className={BTN_SECONDARY}
          >
            Discard suggestion
          </button>
        </div>

        {error && (
          <p role="alert" className="text-sm text-red-700">
            {error}
          </p>
        )}
      </form>
    </section>
  );
}
