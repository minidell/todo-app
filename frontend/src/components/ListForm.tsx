import { useEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import type { FormEvent } from 'react';
import { BTN_PRIMARY, BTN_SECONDARY, FIELD_LABEL, INPUT } from '../styles';

interface ListFormProps {
  /** `Create list` or `Save list name`. */
  submitLabel: string;
  initialValue?: string;
  /** Id of the error alert rendered by the parent, when there is one. */
  errorId?: string;
  /** Clear the field and refocus it after a successful submit (create flow). */
  resetOnSuccess?: boolean;
  /** Must reject when the request failed so the field can stay as typed. */
  onSubmit: (name: string) => Promise<void>;
  onCancel: () => void;
}

const INPUT_ID = 'list-form-name';

export default function ListForm({
  submitLabel,
  initialValue = '',
  errorId,
  resetOnSuccess = false,
  onSubmit,
  onCancel,
}: ListFormProps) {
  const [value, setValue] = useState(initialValue);
  const [submitting, setSubmitting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const trimmed = value.trim();

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!trimmed || submitting) {
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit(trimmed);
    } catch {
      // The parent surfaces the message; keep what was typed so the user can
      // fix it without retyping.
      setSubmitting(false);
      return;
    }
    if (resetOnSuccess) {
      // Flush synchronously so the input is enabled again before we focus it
      // (a disabled element cannot take focus).
      flushSync(() => {
        setValue('');
        setSubmitting(false);
      });
      inputRef.current?.focus();
    } else {
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      aria-busy={submitting ? 'true' : undefined}
      className="mt-2 flex flex-col gap-2 rounded-lg border border-slate-200 bg-slate-50 p-3"
    >
      <div className="flex flex-col gap-1">
        <label htmlFor={INPUT_ID} className={FIELD_LABEL}>
          New list name
        </label>
        <input
          ref={inputRef}
          id={INPUT_ID}
          type="text"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="List name"
          maxLength={100}
          autoComplete="off"
          aria-describedby={errorId}
          disabled={submitting}
          className={INPUT}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          disabled={submitting || !trimmed}
          className={`${BTN_PRIMARY}`}
        >
          {submitLabel}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className={`${BTN_SECONDARY}`}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
