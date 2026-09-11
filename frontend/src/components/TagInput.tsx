import { useId, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { FIELD_LABEL, FOCUS, INPUT } from '../styles';

export const TAG_FORMAT_MESSAGE =
  'Tags can use letters, numbers, spaces, hyphens and underscores.';

/** The backend accepts at most 10 tags per todo; refuse the 11th in the UI. */
export const MAX_TAGS = 10;
export const TAG_LIMIT_MESSAGE = 'You can add up to 10 tags.';

/** trim → lowercase → collapse inner whitespace (the backend's `normalize_tag`). */
export function normalizeTag(name: string): string {
  return name.trim().toLowerCase().replace(/\s+/g, ' ');
}

/** The backend pattern: `^[a-z0-9][a-z0-9 _-]{0,29}$` on the normalized name. */
export function isValidTag(name: string): boolean {
  return /^[a-z0-9][a-z0-9 _-]{0,29}$/.test(name);
}

interface TagInputProps {
  /** Visible label; the edit panel uses `Tags` (slice-3 spec F3). */
  label: string;
  tags: string[];
  onChange: (tags: string[]) => void;
}

/**
 * Text field plus a chip list. Enter or `,` commits the typed tag, Backspace on
 * an empty field removes the last chip, and every chip has its own remove
 * button. Names are normalized on commit and duplicates collapse silently.
 */
export default function TagInput({ label, tags, onChange }: TagInputProps) {
  const inputId = useId();
  const errorId = `${inputId}-error`;
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);

  /** Returns true when the draft was consumed (committed or empty). */
  function commit(): boolean {
    const name = normalizeTag(draft);
    if (name === '') {
      setDraft('');
      setError(null);
      return true;
    }
    if (!isValidTag(name)) {
      setError(TAG_FORMAT_MESSAGE);
      return false;
    }
    // A duplicate is a no-op, so it never runs into the limit.
    if (!tags.includes(name) && tags.length >= MAX_TAGS) {
      setError(TAG_LIMIT_MESSAGE);
      return false;
    }
    setError(null);
    setDraft('');
    if (!tags.includes(name)) {
      onChange([...tags, name]);
    }
    return true;
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>): void {
    if (event.key === 'Enter' || event.key === ',') {
      // Enter must not submit the surrounding form while a tag is pending.
      event.preventDefault();
      commit();
      return;
    }
    if (event.key === 'Backspace' && draft === '' && tags.length > 0) {
      event.preventDefault();
      onChange(tags.slice(0, -1));
      setError(null);
    }
  }

  function removeTag(name: string): void {
    setError(null);
    onChange(tags.filter((tag) => tag !== name));
  }

  return (
    <div className="flex min-w-48 flex-col gap-1">
      <label htmlFor={inputId} className={FIELD_LABEL}>
        {label}
      </label>
      {tags.length > 0 && (
        <ul className="flex flex-wrap gap-1.5">
          {tags.map((tag) => (
            <li
              key={tag}
              className="flex items-center gap-1 rounded-full border border-slate-200 bg-slate-100 py-0.5 pr-1 pl-2.5 text-xs font-medium text-slate-700"
            >
              <span>{tag}</span>
              <button
                type="button"
                aria-label={`Remove tag ${tag}`}
                onClick={() => removeTag(tag)}
                className={`rounded-full px-1 text-slate-600 hover:text-slate-900 ${FOCUS}`}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      <input
        id={inputId}
        type="text"
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value);
          setError(null);
        }}
        onKeyDown={handleKeyDown}
        // Committing on blur keeps a typed-but-uncommitted tag from being
        // silently dropped when the user goes straight for Save/Add.
        onBlur={() => commit()}
        placeholder="Add a tag"
        maxLength={30}
        autoComplete="off"
        aria-describedby={error ? errorId : undefined}
        className={INPUT}
      />
      {error && (
        <p id={errorId} role="alert" className="text-sm text-red-700">
          {error}
        </p>
      )}
    </div>
  );
}
