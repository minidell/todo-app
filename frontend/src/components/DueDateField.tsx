import { BTN_LINK, FIELD_LABEL, INPUT } from '../styles';

interface DueDateFieldProps {
  id: string;
  /** Visible label; the edit panel uses `Due date` (slice-3 spec F3). */
  label: string;
  /** Raw `YYYY-MM-DD` from the wire, or `null` for "no due date". */
  value: string | null;
  onChange: (value: string | null) => void;
}

/**
 * Native `<input type="date">` (master D-F3). The value is passed through as
 * the raw `YYYY-MM-DD` string in both directions — never via `new Date()`,
 * which would shift the day across timezones.
 */
export default function DueDateField({ id, label, value, onChange }: DueDateFieldProps) {
  return (
    <div className="flex min-w-40 flex-col gap-1">
      <label htmlFor={id} className={FIELD_LABEL}>
        {label}
      </label>
      <input
        id={id}
        type="date"
        value={value ?? ''}
        onChange={(event) => onChange(event.target.value === '' ? null : event.target.value)}
        className={INPUT}
      />
      {value !== null && (
        <button
          type="button"
          onClick={() => onChange(null)}
          className={`${BTN_LINK} mt-1 self-start`}
        >
          Clear due date
        </button>
      )}
    </div>
  );
}
