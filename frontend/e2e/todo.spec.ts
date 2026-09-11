import { test, expect } from './fixtures';

test('add, toggle completed, toggle back, then delete a todo', async ({
  signedInPage: page,
}) => {
  const title = `E2E todo ${Date.now()}`;

  await expect(page.getByRole('heading', { name: 'Todos', level: 1 })).toBeVisible();

  const input = page.getByRole('textbox', { name: 'New todo title' });
  await input.fill(title);
  await page.getByRole('button', { name: 'Add' }).click();

  await expect(input).toHaveValue('');

  const row = page.getByRole('listitem').filter({ hasText: title });
  await expect(row).toBeVisible();
  await expect(row.getByText(title, { exact: true })).toBeVisible();

  const activeCheckbox = row.getByRole('checkbox', {
    name: `Mark ${title} as completed`,
  });
  await expect(activeCheckbox).toBeVisible();
  await expect(activeCheckbox).not.toBeChecked();

  // Toggle to completed.
  await activeCheckbox.click();
  const completedCheckbox = row.getByRole('checkbox', {
    name: `Mark ${title} as active`,
  });
  await expect(completedCheckbox).toBeChecked();
  await expect(row.getByText(title, { exact: true })).toHaveClass(/line-through/);

  // Toggle back to active.
  await completedCheckbox.click();
  const activeAgainCheckbox = row.getByRole('checkbox', {
    name: `Mark ${title} as completed`,
  });
  await expect(activeAgainCheckbox).not.toBeChecked();

  // Delete the todo and confirm the row is gone.
  await row.getByRole('button', { name: `Delete ${title}` }).click();
  await expect(row).toHaveCount(0);
});
