import type { Priority } from '../api/types';
import { PRIORITY_LABELS, PRIORITY_ORDER } from '../dates';
import { FIELD_LABEL, SELECT } from '../styles';

interface PrioritySelectProps {
  id: string;
  /** Visible label; the edit panel uses `Priority` (slice-3 spec F3). */
  label: string;
  value: Priority;
  onChange: (priority: Priority) => void;
}

/** Native `<select>` per master D-F3 — no component library. */
export default function PrioritySelect({ id, label, value, onChange }: PrioritySelectProps) {
  return (
    <div className="flex min-w-36 flex-col gap-1">
      <label htmlFor={id} className={FIELD_LABEL}>
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value as Priority)}
        className={SELECT}
      >
        {/* Low first so the visual order matches the wire enum. */}
        {[...PRIORITY_ORDER].reverse().map((priority) => (
          <option key={priority} value={priority}>
            {PRIORITY_LABELS[priority]}
          </option>
        ))}
      </select>
    </div>
  );
}
