import { useEffect, useId, useRef, useState } from 'react';
import type { AiMetadataSuggestion } from '../api/ai';
import {
  AI_SLOW_HINT,
  aiErrorMessage,
  isAbortError,
  suggestMetadata,
  suggestSubtasks,
} from '../api/ai';
import type { Todo } from '../api/types';
import { PRIORITY_LABELS } from '../dates';
import { AI_SURFACE, BTN_PRIMARY, BTN_SECONDARY, CHECKBOX, META_SMALL } from '../styles';

export const NO_SUBTASKS_MESSAGE = 'The AI had no subtasks to suggest.';
export const APPLY_ERROR_MESSAGE = 'Could not apply the suggestion. Please try again.';

/** Which of the two per-todo helpers this panel is showing. */
export type SuggestionKind = 'subtasks' | 'metadata';

interface AiSuggestionPanelProps {
  todo: Todo;
  kind: SuggestionKind;
  onAddSubtasks: (titles: string[]) => Promise<void>;
  onApplyMetadata: (suggestion: AiMetadataSuggestion) => Promise<void>;
  onClose: () => void;
}

/**
 * The per-todo AI suggestions. Both variants are drafts (decision D-AI1): the
 * suggestion is only written by `POST /api/todos/{id}/subtasks` or
 * `PATCH /api/todos/{id}` after the user accepts it.
 */
export default function AiSuggestionPanel({
  todo,
  kind,
  onAddSubtasks,
  onApplyMetadata,
  onClose,
}: AiSuggestionPanelProps) {
  const fieldPrefix = useId();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [titles, setTitles] = useState<string[]>([]);
  const [excluded, setExcluded] = useState<Set<number>>(new Set());
  const [metadata, setMetadata] = useState<AiMetadataSuggestion | null>(null);
  const [applying, setApplying] = useState(false);

  const requestRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    requestRef.current = controller;
    let cancelled = false;

    async function load(): Promise<void> {
      try {
        if (kind === 'subtasks') {
          const suggested = await suggestSubtasks(todo.id, 5, controller.signal);
          if (cancelled) return;
          setTitles(suggested);
        } else {
          const suggestion = await suggestMetadata(todo.id, controller.signal);
          if (cancelled) return;
          setMetadata(suggestion);
        }
      } catch (caught) {
        if (cancelled || isAbortError(caught)) return;
        setError(aiErrorMessage(caught));
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void load();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [kind, todo.id]);

  function toggle(index: number): void {
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

  async function apply(action: () => Promise<void>): Promise<void> {
    if (applying) return;
    setApplying(true);
    setError(null);
    try {
      await action();
      onClose();
    } catch {
      setError(APPLY_ERROR_MESSAGE);
      setApplying(false);
    }
  }

  const label =
    kind === 'subtasks'
      ? `AI subtask suggestions for ${todo.title}`
      : `AI priority and tag suggestions for ${todo.title}`;
  const selected = titles.filter((_, index) => !excluded.has(index));
  const closeLabel = kind === 'subtasks' ? 'Cancel' : 'Dismiss';

  return (
    <section
      aria-label={label}
      className={`mt-3 ml-8 ${AI_SURFACE} p-3`}
    >
      <div aria-live="polite">
        {loading && (
          <p role="status" className={META_SMALL}>
            {`Thinking… ${AI_SLOW_HINT}`}
          </p>
        )}

        {!loading && kind === 'subtasks' && titles.length === 0 && !error && (
          <p className="text-sm text-slate-700">{NO_SUBTASKS_MESSAGE}</p>
        )}

        {!loading && kind === 'subtasks' && titles.length > 0 && (
          <ul>
            {titles.map((title, index) => {
              const checkboxId = `${fieldPrefix}-subtask-${index}`;
              return (
                <li key={checkboxId} className="flex items-center gap-2 py-0.5">
                  <input
                    id={checkboxId}
                    type="checkbox"
                    checked={!excluded.has(index)}
                    aria-label={`Add subtask ${title}`}
                    onChange={() => toggle(index)}
                    className={CHECKBOX}
                  />
                  <label htmlFor={checkboxId} className="text-sm text-slate-900">
                    {title}
                  </label>
                </li>
              );
            })}
          </ul>
        )}

        {!loading && kind === 'metadata' && metadata && (
          <p className="text-sm text-slate-900">
            {`Suggested priority: ${PRIORITY_LABELS[metadata.priority]}. Suggested tags: ${
              metadata.tags.length > 0 ? metadata.tags.join(', ') : 'none'
            }.`}
          </p>
        )}
      </div>

      {error && (
        <p role="alert" className="mt-1 text-sm text-red-700">
          {error}
        </p>
      )}

      <div className="mt-2 flex gap-2">
        {!loading && !error && kind === 'subtasks' && titles.length > 0 && (
          <button
            type="button"
            aria-busy={applying ? 'true' : undefined}
            aria-disabled={applying || selected.length === 0 || undefined}
            onClick={() => {
              if (selected.length === 0) return;
              void apply(() => onAddSubtasks(selected));
            }}
            className={`${BTN_PRIMARY}`}
          >
            Add selected subtasks
          </button>
        )}

        {!loading && !error && kind === 'metadata' && metadata && (
          <button
            type="button"
            aria-busy={applying ? 'true' : undefined}
            aria-disabled={applying || undefined}
            onClick={() => {
              void apply(() => onApplyMetadata(metadata));
            }}
            className={`${BTN_PRIMARY}`}
          >
            Apply suggestion
          </button>
        )}

        <button
          type="button"
          onClick={onClose}
          className={`${BTN_SECONDARY}`}
        >
          {closeLabel}
        </button>
      </div>
    </section>
  );
}
