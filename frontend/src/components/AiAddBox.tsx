import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { AiTodoDraft, AiUnavailableReason } from '../api/ai';
import { AI_SLOW_HINT, aiErrorMessage, aiUnavailableBanner, isAbortError, parseTodo } from '../api/ai';
import { todayString } from '../dates';
import AiDraftPreview from './AiDraftPreview';
import type { ConfirmDraft } from './AiDraftPreview';
import { BTN_PRIMARY, FIELD_LABEL, INPUT, META_SMALL } from '../styles';

interface AiAddBoxProps {
  /** False when Ollama/ai-agent is down: the controls stay visible but inert. */
  available: boolean;
  /** Why it is unavailable (master §5.1); picks the banner copy. */
  reason?: AiUnavailableReason | null;
  onConfirm: ConfirmDraft;
}

/**
 * "Describe a todo in your own words" → `POST /api/ai/parse-todo` → a draft the
 * user confirms. The manual add form above it keeps working at all times; this
 * box is never the only way to create a todo (slice-4 spec F5).
 */
export default function AiAddBox({ available, reason = null, onConfirm }: AiAddBoxProps) {
  const fieldPrefix = useId();
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<AiTodoDraft | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const submitRef = useRef<HTMLButtonElement>(null);
  const requestRef = useRef<AbortController | null>(null);

  const textareaId = `${fieldPrefix}-text`;
  const hintId = `${fieldPrefix}-hint`;

  // An in-flight parse must not outlive the component (F5).
  useEffect(() => () => requestRef.current?.abort(), []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const trimmed = text.trim();
    if (!available || busy || trimmed === '') {
      return;
    }

    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;

    setBusy(true);
    setError(null);
    setDraft(null);
    try {
      // The caller's *local* date: "tomorrow" must mean their tomorrow.
      const parsed = await parseTodo(trimmed, todayString(), controller.signal);
      setDraft(parsed);
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

  function discardDraft(): void {
    setDraft(null);
    // Focus goes back to the control that produced the draft (F5).
    submitRef.current?.focus();
  }

  async function confirmDraft(...args: Parameters<ConfirmDraft>): Promise<void> {
    await onConfirm(...args);
    setDraft(null);
    setText('');
    textareaRef.current?.focus();
  }

  return (
    <div>
      {!available && (
        <p className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          {aiUnavailableBanner(reason)}
        </p>
      )}
      <form onSubmit={handleSubmit} className="flex flex-col gap-2">
        <label htmlFor={textareaId} className={FIELD_LABEL}>
          Describe a todo in your own words
        </label>
        <textarea
          ref={textareaRef}
          id={textareaId}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="e.g. call the dentist tomorrow, urgent"
          aria-describedby={hintId}
          aria-disabled={!available || undefined}
          rows={2}
          maxLength={2000}
          className={INPUT}
        />
        <p id={hintId} className={META_SMALL}>
          {AI_SLOW_HINT}
        </p>
        <div>
          {/* Busy is `aria-busy`, never `disabled`: focus must not be taken. */}
          <button
            ref={submitRef}
            type="submit"
            aria-busy={busy ? 'true' : undefined}
            aria-disabled={!available || busy || undefined}
            className={BTN_PRIMARY}
          >
            {busy ? 'Thinking…' : 'Draft with AI'}
          </button>
        </div>
      </form>

      {error && (
        <p role="alert" className="mt-2 text-sm text-red-700">
          {error}
        </p>
      )}

      {/* The draft is announced by moving focus into it rather than by a live
          region: a live region here would be interrupted by that focus move,
          and the panel is a form, not a status message. */}
      {draft && (
        <AiDraftPreview draft={draft} onConfirm={confirmDraft} onDiscard={discardDraft} />
      )}
    </div>
  );
}
