import { test, expect } from './fixtures';

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

/** The browser's local calendar date, `days` from today, as `YYYY-MM-DD`. */
function localDate(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() + days);
  return date.toLocaleDateString('en-CA');
}

/** `"2026-09-05"` → `"5 Sep"`, matching the row copy. */
function formatDay(date: string): string {
  const [, month, day] = date.split('-');
  return `${Number(day)} ${MONTHS[Number(month) - 1]}`;
}

test('enrich a todo, work its subtasks, then filter and search for it', async ({
  signedInPage: page,
}) => {
  const stamp = `${Date.now()}`;
  const title = `Enrichment ${stamp} groceries`;
  const tomorrow = localDate(1);

  // --- create with priority, due date and two tags ---------------------------
  await page.getByRole('textbox', { name: 'New todo title' }).fill(title);
  // Priority, due date and tags sit behind the composer's disclosure.
  await page.getByRole('button', { name: 'More options' }).click();
  await page.getByLabel('New todo priority').selectOption('high');
  await page.getByLabel('New todo due date').fill(tomorrow);

  const tagInput = page.getByRole('textbox', { name: 'New todo tags' });
  await tagInput.fill('home');
  await tagInput.press('Enter');
  await tagInput.fill('errand');
  await tagInput.press('Enter');

  await page.getByRole('button', { name: 'Add' }).click();

  const row = page.getByRole('listitem').filter({ hasText: title });
  await expect(row).toBeVisible();
  await expect(row.getByLabel('Priority: High')).toHaveText('High');
  await expect(row.getByText(`Due ${formatDay(tomorrow)}`)).toBeVisible();
  await expect(row.getByLabel('Tag: home')).toBeVisible();
  await expect(row.getByLabel('Tag: errand')).toBeVisible();

  // --- two subtasks, one completed ------------------------------------------
  await page.getByRole('button', { name: `Edit ${title}` }).click();
  const panel = page.getByRole('group', { name: `Edit ${title}` });
  await expect(panel).toBeVisible();
  // `exact` matters: "New subtask title" also contains "title".
  await expect(panel.getByLabel('Title', { exact: true })).toBeFocused();

  const subtaskInput = panel.getByRole('textbox', { name: 'New subtask title' });
  await subtaskInput.fill('Step one');
  await panel.getByRole('button', { name: 'Add subtask' }).click();
  // `exact` matters: the sr-only checkbox label "Mark Step one as completed"
  // also contains the substring "Step one".
  await expect(panel.getByText('Step one', { exact: true })).toBeVisible();

  await subtaskInput.fill('Step two');
  await panel.getByRole('button', { name: 'Add subtask' }).click();
  await expect(panel.getByText('Step two', { exact: true })).toBeVisible();

  await panel.getByRole('checkbox', { name: 'Mark Step one as completed' }).click();
  await expect(panel.getByRole('checkbox', { name: 'Mark Step one as active' })).toBeChecked();
  await expect(row.getByText('1/2 subtasks')).toBeVisible();

  await panel.getByRole('button', { name: 'Cancel' }).click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByRole('button', { name: `Edit ${title}` })).toBeFocused();

  // --- filter by Active + tag home ------------------------------------------
  await page.getByLabel('Status').selectOption('active');
  await page.getByRole('button', { name: 'Filter by tag home' }).click();
  await expect(page.getByRole('button', { name: 'Filter by tag home' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(row).toBeVisible();
  await expect(page.getByText('Showing 1 of 1 todos')).toBeVisible();

  // --- search for a unique fragment of the title ----------------------------
  await page.getByLabel('Search todos').fill(stamp);
  await expect(page.getByText('Showing 1 of 1 todos')).toBeVisible();
  await expect(row).toBeVisible();

  // A term that matches nothing reports the no-match copy.
  await page.getByLabel('Search todos').fill(`${stamp}-nothing`);
  await expect(page.getByText('No todos match your filters.')).toBeVisible();

  // --- clear the filters, then delete ---------------------------------------
  await page.getByRole('button', { name: 'Clear filters' }).click();
  await expect(page.getByLabel('Search todos')).toHaveValue('');
  await expect(page.getByLabel('Status')).toHaveValue('all');
  await expect(row).toBeVisible();

  await row.getByRole('button', { name: `Delete ${title}` }).click();
  await expect(row).toHaveCount(0);
});
