import { useEffect, useId, useRef, useState } from 'react';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { BTN_LINK, FIELD_LABEL, INPUT } from '../styles';

export const SEARCH_DEBOUNCE_MS = 300;

interface SearchBoxProps {
  /** The committed query currently applied to the list. */
  value: string;
  onSearch: (q: string) => void;
}

/**
 * Debounced search over title and description. The typed text lives here; the
 * parent only hears about it once the user pauses, so a burst of keystrokes
 * costs one request.
 */
export default function SearchBox({ value, onSearch }: SearchBoxProps) {
  const inputId = useId();
  const [text, setText] = useState(value);
  const debounced = useDebouncedValue(text, SEARCH_DEBOUNCE_MS);
  const inputRef = useRef<HTMLInputElement>(null);
  const lastEmitted = useRef(value);
  const onSearchRef = useRef(onSearch);

  useEffect(() => {
    onSearchRef.current = onSearch;
  });

  useEffect(() => {
    const next = debounced.trim();
    if (next === lastEmitted.current) {
      return;
    }
    lastEmitted.current = next;
    onSearchRef.current(next);
  }, [debounced]);

  // The query can also be reset from outside (Clear filters); follow it so the
  // field never shows a term that is no longer applied.
  useEffect(() => {
    if (value !== lastEmitted.current) {
      lastEmitted.current = value;
      setText(value);
    }
  }, [value]);

  function clear(): void {
    setText('');
    lastEmitted.current = '';
    onSearchRef.current('');
    inputRef.current?.focus();
  }

  return (
    <form
      role="search"
      onSubmit={(event) => event.preventDefault()}
      className="flex flex-col gap-1"
    >
      <label htmlFor={inputId} className={FIELD_LABEL}>
        Search todos
      </label>
      <input
        ref={inputRef}
        id={inputId}
        type="search"
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="Search"
        maxLength={100}
        autoComplete="off"
        className={INPUT}
      />
      {text !== '' && (
        <button type="button" onClick={clear} className={`${BTN_LINK} mt-1 self-start`}>
          Clear search
        </button>
      )}
    </form>
  );
}
