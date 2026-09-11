import { useId, useState } from 'react';
import type {
  DueFilter,
  Priority,
  SortOrder,
  StatusFilter,
  TagSummary,
  TodoSort,
} from '../api/types';
import { PRIORITY_LABELS, PRIORITY_ORDER } from '../dates';
import SearchBox from './SearchBox';
import SortSelect from './SortSelect';
import { BTN_LINK, CHECKBOX, FIELD_LABEL, FOCUS, SECTION_TITLE, SELECT, SURFACE } from '../styles';

export const NO_MATCH_MESSAGE = 'No todos match your filters.';

/** Filter/sort/search state. Lives in `App`, never in the URL (master D-F1). */
export interface TodoFilters {
  status: StatusFilter;
  priorities: Priority[];
  tags: string[];
  due: DueFilter;
  q: string;
  sort: TodoSort;
  order: SortOrder;
}

export const DEFAULT_FILTERS: TodoFilters = {
  status: 'all',
  priorities: [],
  tags: [],
  due: 'any',
  q: '',
  sort: 'created_at',
  order: 'asc',
};

/** Sort/order are presentation, not filtering: they never show Clear filters. */
export function hasActiveFilters(filters: TodoFilters): boolean {
  return (
    filters.status !== 'all' ||
    filters.priorities.length > 0 ||
    filters.tags.length > 0 ||
    filters.due !== 'any' ||
    filters.q !== ''
  );
}

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'completed', label: 'Completed' },
];

const DUE_OPTIONS: { value: DueFilter; label: string }[] = [
  { value: 'any', label: 'Any time' },
  { value: 'overdue', label: 'Overdue' },
  { value: 'today', label: 'Today' },
  { value: 'week', label: 'Next 7 days' },
  { value: 'none', label: 'No due date' },
];

interface FilterBarProps {
  filters: TodoFilters;
  onChange: (filters: TodoFilters) => void;
  /** The user's tag vocabulary, ordered `name ASC` (`GET /api/tags`). */
  tags: TagSummary[];
}

/**
 * The filters as one titled sidebar group (iteration-3 redesign): search,
 * Status, Priority, Due, Tags and Sort stacked in a fixed order, each with its
 * own small label, instead of a wide bar competing with the list. The result
 * summary it used to carry now lives in the main column header, next to the
 * name of the list it describes.
 */
export default function FilterBar({ filters, onChange, tags }: FilterBarProps) {
  const statusId = useId();
  const dueId = useId();
  const fieldsId = useId();
  // Below `lg` the sidebar is a block above the todos, where a full filter
  // stack would push the list off the first screen — so there it collapses.
  // From `lg` the fields are always shown and the toggle is not rendered.
  const [openOnSmall, setOpenOnSmall] = useState(false);

  const active = hasActiveFilters(filters);

  function patch(changes: Partial<TodoFilters>): void {
    onChange({ ...filters, ...changes });
  }

  function togglePriority(priority: Priority, checked: boolean): void {
    patch({
      priorities: checked
        ? [...filters.priorities, priority]
        : filters.priorities.filter((value) => value !== priority),
    });
  }

  function toggleTag(name: string): void {
    patch({
      tags: filters.tags.includes(name)
        ? filters.tags.filter((tag) => tag !== name)
        : [...filters.tags, name],
    });
  }

  return (
    <section aria-label="Filters" className={`${SURFACE} p-4`}>
      <div className="flex items-center justify-between gap-3">
        <h2 className={SECTION_TITLE}>Filters</h2>
        <div className="flex items-center gap-3">
          {active && (
            <button
              type="button"
              onClick={() =>
                onChange({ ...DEFAULT_FILTERS, sort: filters.sort, order: filters.order })
              }
              className={BTN_LINK}
            >
              Clear filters
            </button>
          )}
          <button
            type="button"
            aria-expanded={openOnSmall}
            aria-controls={fieldsId}
            aria-label={openOnSmall ? 'Hide filters' : 'Show filters'}
            onClick={() => setOpenOnSmall((open) => !open)}
            className={`text-sm font-medium text-slate-600 hover:text-slate-900 lg:hidden ${FOCUS}`}
          >
            {openOnSmall ? 'Hide' : 'Show'}
          </button>
        </div>
      </div>

      <div
        id={fieldsId}
        className={`${openOnSmall ? 'flex' : 'hidden lg:flex'} mt-4 flex-col gap-4`}
      >
        <SearchBox value={filters.q} onSearch={(q) => patch({ q })} />

        <div className="flex flex-col gap-1">
          <label htmlFor={statusId} className={FIELD_LABEL}>
            Status
          </label>
          <select
            id={statusId}
            value={filters.status}
            onChange={(event) => patch({ status: event.target.value as StatusFilter })}
            className={SELECT}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <fieldset className="flex flex-col gap-1.5 border-0 p-0">
          <legend className={FIELD_LABEL}>Priority</legend>
          {/* "Any priority" is the no-selection state; ticking it clears the rest. */}
          <label className="flex items-center gap-2 text-sm text-slate-900">
            <input
              type="checkbox"
              checked={filters.priorities.length === 0}
              onChange={() => patch({ priorities: [] })}
              className={CHECKBOX}
            />
            Any priority
          </label>
          {PRIORITY_ORDER.map((priority) => (
            <label key={priority} className="flex items-center gap-2 text-sm text-slate-900">
              <input
                type="checkbox"
                checked={filters.priorities.includes(priority)}
                onChange={(event) => togglePriority(priority, event.target.checked)}
                className={CHECKBOX}
              />
              {PRIORITY_LABELS[priority]}
            </label>
          ))}
        </fieldset>

        <div className="flex flex-col gap-1">
          <label htmlFor={dueId} className={FIELD_LABEL}>
            Due
          </label>
          <select
            id={dueId}
            value={filters.due}
            onChange={(event) => patch({ due: event.target.value as DueFilter })}
            className={SELECT}
          >
            {DUE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        {tags.length > 0 && (
          <fieldset className="flex flex-col gap-1.5 border-0 p-0">
            <legend className={FIELD_LABEL}>Filter by tag</legend>
            <div className="flex flex-wrap gap-1.5">
              {tags.map((tag) => {
                const pressed = filters.tags.includes(tag.name);
                return (
                  <button
                    key={tag.id}
                    type="button"
                    aria-pressed={pressed}
                    aria-label={`Filter by tag ${tag.name}`}
                    onClick={() => toggleTag(tag.name)}
                    className={`rounded-full px-2.5 py-1 text-xs font-medium ${FOCUS} ${
                      pressed
                        ? 'bg-indigo-600 text-white'
                        : 'border border-slate-300 bg-white text-slate-700 hover:bg-slate-50'
                    }`}
                  >
                    {tag.name}
                  </button>
                );
              })}
            </div>
          </fieldset>
        )}

        <SortSelect
          sort={filters.sort}
          order={filters.order}
          onSortChange={(sort) => patch({ sort })}
          onOrderChange={(order) => patch({ order })}
        />
      </div>
    </section>
  );
}
