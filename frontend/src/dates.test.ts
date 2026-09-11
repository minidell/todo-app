import { describe, expect, it } from 'vitest';
import {
  dueLabel,
  formatDay,
  formatFullDate,
  formatTimeOfDay,
  isOverdue,
  todayString,
} from './dates';

describe('todayString', () => {
  it('returns the local calendar date as YYYY-MM-DD', () => {
    // Late evening local time: a UTC-based conversion would report the next day
    // east of Greenwich, which is exactly the bug `today` has to avoid.
    const evening = new Date(2026, 8, 5, 23, 30, 0);
    expect(todayString(evening)).toBe('2026-09-05');
    expect(todayString()).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
});

describe('formatTimeOfDay', () => {
  it('renders a 24-hour HH:MM in the reader’s timezone', () => {
    const local = new Date(2026, 8, 2, 8, 4, 0);
    expect(formatTimeOfDay(local.toISOString())).toBe('08:04');
  });

  it('returns an empty string for a timestamp it cannot parse', () => {
    expect(formatTimeOfDay('not a timestamp')).toBe('');
    expect(formatTimeOfDay('')).toBe('');
  });
});

describe('formatDay', () => {
  it('formats a wire date as "D MMM" without a leading zero', () => {
    expect(formatDay('2026-09-05')).toBe('5 Sep');
    expect(formatDay('2026-01-31')).toBe('31 Jan');
    expect(formatDay('2026-12-01')).toBe('1 Dec');
  });

  it('never round-trips through Date, so the day is stable', () => {
    // A Date-based implementation renders this as 4 Sep in any negative offset.
    expect(formatDay('2026-09-05')).toBe('5 Sep');
  });

  it('returns unparseable input unchanged', () => {
    expect(formatDay('not-a-date')).toBe('not-a-date');
    expect(formatDay('2026-13-05')).toBe('2026-13-05');
  });
});

describe('formatFullDate', () => {
  it('spells the year out for a date read on its own', () => {
    expect(formatFullDate('2026-09-11')).toBe('11 Sep 2026');
    expect(formatFullDate('2027-01-01')).toBe('1 Jan 2027');
  });

  it('returns unparseable input unchanged', () => {
    expect(formatFullDate('someday')).toBe('someday');
    expect(formatFullDate('2026-13-05')).toBe('2026-13-05');
  });
});

describe('dueLabel', () => {
  const today = '2026-09-05';

  it('labels a future date, today and an overdue active todo', () => {
    expect(dueLabel('2026-09-08', today, false)).toBe('Due 8 Sep');
    expect(dueLabel(today, today, false)).toBe('Due today');
    expect(dueLabel('2026-09-01', today, false)).toBe('Overdue — 1 Sep');
  });

  it('does not call a completed todo overdue', () => {
    expect(dueLabel('2026-09-01', today, true)).toBe('Due 1 Sep');
    expect(isOverdue('2026-09-01', today, true)).toBe(false);
    expect(isOverdue('2026-09-01', today, false)).toBe(true);
    expect(isOverdue(today, today, false)).toBe(false);
  });
});
