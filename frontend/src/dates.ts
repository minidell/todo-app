import type { Priority } from './api/types';

/**
 * Date helpers for `due_date`, which is a plain `YYYY-MM-DD` string on the wire
 * (master D-D1). None of these ever build a `Date` from that string: parsing
 * "2026-09-05" as a Date yields UTC midnight, which renders as the previous day
 * west of Greenwich. Everything below works on the string itself.
 */

const MONTHS = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
];

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * The browser's *local* calendar date as `YYYY-MM-DD`. Sent as `today` on every
 * list fetch so the `Overdue`/`Today` presets match the user's calendar rather
 * than the server's UTC day.
 */
export function todayString(now: Date = new Date()): string {
  return now.toLocaleDateString('en-CA');
}

/** `"2026-09-05"` → `"5 Sep"`. Unparseable input is returned unchanged. */
export function formatDay(date: string): string {
  const match = ISO_DATE.exec(date);
  if (!match) {
    return date;
  }
  const month = MONTHS[Number(match[2]) - 1];
  return month ? `${Number(match[3])} ${month}` : date;
}

/**
 * `"2026-09-11"` → `"11 Sep 2026"`. Unparseable input is returned unchanged.
 * The year is spelled out because the only caller is the AI edit diff, where a
 * proposed due date is read on its own rather than next to today's rows — and
 * "11 Sep" would hide a model that picked the wrong year.
 */
export function formatFullDate(date: string): string {
  const match = ISO_DATE.exec(date);
  if (!match) {
    return date;
  }
  const month = MONTHS[Number(match[2]) - 1];
  return month ? `${Number(match[3])} ${month} ${match[1]}` : date;
}

/** A todo is overdue when it is still active and its due date is in the past. */
export function isOverdue(dueDate: string, today: string, completed: boolean): boolean {
  return !completed && dueDate < today;
}

/**
 * Row copy for a due date (slice-3 spec F3): `Due 5 Sep`, `Due today`, or
 * `Overdue — 5 Sep`. The word "Overdue" is always present, so the state is
 * never conveyed by colour alone.
 */
export function dueLabel(dueDate: string, today: string, completed: boolean): string {
  if (isOverdue(dueDate, today, completed)) {
    return `Overdue — ${formatDay(dueDate)}`;
  }
  if (dueDate === today) {
    return 'Due today';
  }
  return `Due ${formatDay(dueDate)}`;
}

/**
 * `"2026-09-02T08:04:00Z"` → `"09:04"` in the reader's own timezone, for the
 * daily-summary meta line. Unparseable input yields an empty string.
 */
export function formatTimeOfDay(timestamp: string): string {
  const parsed = new Date(timestamp);
  if (Number.isNaN(parsed.getTime())) {
    return '';
  }
  // `en-GB` pins the 24-hour `HH:MM` shape the spec asks for.
  return parsed.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
}

export const PRIORITY_LABELS: Record<Priority, string> = {
  low: 'Low',
  medium: 'Medium',
  high: 'High',
};

export const PRIORITY_ORDER: Priority[] = ['high', 'medium', 'low'];
