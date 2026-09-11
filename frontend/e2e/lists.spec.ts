import { test, expect } from './fixtures';
import type { Page } from '@playwright/test';

/** The lists sidebar; every list is an entry button carrying its count. */
function listNav(page: Page) {
  return page.getByRole('navigation', { name: 'Todo lists' });
}

/** Opens a list's actions menu, where Rename and Delete now live. */
async function openListMenu(page: Page, listName: string): Promise<void> {
  await page.getByRole('button', { name: `List actions for ${listName}` }).click();
}

test('create a list, add a todo there, and delete the list', async ({ signedInPage: page }) => {
  const listName = `Work ${Date.now()}`;
  const title = `List E2E todo ${Date.now()}`;

  const entries = listNav(page).getByRole('listitem');
  // A fresh account has exactly "All lists" + "Inbox".
  await expect(entries).toHaveCount(2);

  await page.getByRole('button', { name: 'New list' }).click();
  const nameInput = page.getByRole('textbox', { name: 'New list name' });
  await expect(nameInput).toBeFocused();
  await nameInput.fill(listName);
  await page.getByRole('button', { name: 'Create list' }).click();

  // The new list becomes the selection and the input clears for the next one.
  await expect(entries).toHaveCount(3);
  await expect(nameInput).toHaveValue('');
  const newList = page.getByRole('button', { name: `${listName} (0 active)` });
  await expect(newList).toHaveAttribute('aria-current', 'true');
  await page.getByRole('button', { name: 'Cancel' }).click();

  // Add a todo into the new list.
  const todoInput = page.getByRole('textbox', { name: 'New todo title' });
  await todoInput.fill(title);
  await page.getByRole('button', { name: 'Add' }).click();
  const row = page.getByRole('listitem').filter({ hasText: title });
  await expect(row).toBeVisible();
  await expect(page.getByRole('button', { name: `${listName} (1 active)` })).toBeVisible();

  // Entry order follows created_at ASC: 0 = All lists, 1 = Inbox, 2 = the new list.
  await page.getByRole('button', { name: 'Inbox (0 active)' }).click();
  await expect(page.getByText('No todos yet. Add your first one above.')).toBeVisible();
  await expect(row).toHaveCount(0);

  // "All lists" shows todos from every list.
  await page.getByRole('button', { name: 'All lists' }).click();
  await expect(row).toBeVisible();

  // Back to the new list, then delete it — its todos go with it.
  await page.getByRole('button', { name: `${listName} (1 active)` }).click();
  await expect(row).toBeVisible();

  await openListMenu(page, listName);
  await page.getByRole('menuitem', { name: `Delete list ${listName}` }).click();
  await expect(page.getByText(`Delete “${listName}” and its todos?`)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Delete list', exact: true })).toBeFocused();
  await page.getByRole('button', { name: 'Delete list', exact: true }).click();

  await expect(entries).toHaveCount(2);
  await expect(page.getByRole('button', { name: `${listName} (1 active)` })).toHaveCount(0);
  await expect(row).toHaveCount(0);
});

test('a duplicate list name is rejected with the exact copy', async ({
  signedInPage: page,
}) => {
  await page.getByRole('button', { name: 'New list' }).click();
  await page.getByRole('textbox', { name: 'New list name' }).fill('Inbox');
  await page.getByRole('button', { name: 'Create list' }).click();

  await expect(page.getByRole('alert')).toHaveText('A list with that name already exists.');
});

test('the only list cannot be deleted', async ({ signedInPage: page }) => {
  await openListMenu(page, 'Inbox');
  await page.getByRole('menuitem', { name: 'Delete list Inbox' }).click();
  await page.getByRole('button', { name: 'Delete list', exact: true }).click();

  await expect(page.getByRole('alert')).toHaveText('You must keep at least one list.');
  await expect(page.getByRole('button', { name: /^Inbox/ })).toBeVisible();
});

test('the selected list survives a reload', async ({ signedInPage: page }) => {
  const listName = `Persist ${Date.now()}`;

  await page.getByRole('button', { name: 'New list' }).click();
  await page.getByRole('textbox', { name: 'New list name' }).fill(listName);
  await page.getByRole('button', { name: 'Create list' }).click();

  const entry = page.getByRole('button', { name: `${listName} (0 active)` });
  await expect(entry).toHaveAttribute('aria-current', 'true');

  await page.reload();

  await expect(page.getByRole('button', { name: `${listName} (0 active)` })).toHaveAttribute(
    'aria-current',
    'true',
  );
});
