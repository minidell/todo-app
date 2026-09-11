import { useId } from 'react';
import type { SortOrder, TodoSort } from '../api/types';
import { BTN_SECONDARY, FIELD_LABEL, SELECT } from '../styles';

const SORT_OPTIONS: { value: TodoSort; label: string }[] = [
  { value: 'created_at', label: 'Created' },
  { value: 'updated_at', label: 'Updated' },
  { value: 'due_date', label: 'Due date' },
  { value: 'priority', label: 'Priority' },
  { value: 'title', label: 'Title' },
];

interface SortSelectProps {
  sort: TodoSort;
  order: SortOrder;
  onSortChange: (sort: TodoSort) => void;
  onOrderChange: (order: SortOrder) => void;
}

/**
 * Sort field plus a direction toggle. The button is named after the direction
 * currently in effect and is "pressed" while ascending, so the state is
 * readable without relying on an icon.
 */
export default function SortSelect({
  sort,
  order,
  onSortChange,
  onOrderChange,
}: SortSelectProps) {
  const selectId = useId();
  const ascending = order === 'asc';

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={selectId} className={FIELD_LABEL}>
        Sort by
      </label>
      <select
        id={selectId}
        value={sort}
        onChange={(event) => onSortChange(event.target.value as TodoSort)}
        className={SELECT}
      >
        {SORT_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <button
        type="button"
        aria-pressed={ascending}
        onClick={() => onOrderChange(ascending ? 'desc' : 'asc')}
        className={`${BTN_SECONDARY} mt-1`}
      >
        {ascending ? 'Sort ascending' : 'Sort descending'}
      </button>
    </div>
  );
}
