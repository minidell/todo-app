/// <reference lib="dom" />
import { test, expect } from './fixtures';
import type { Page, Locator } from '@playwright/test';

/**
 * Repeatedly presses Tab until the given locator becomes the focused
 * element, bounded so a broken tab order fails fast instead of hanging.
 */
async function tabTo(page: Page, target: Locator, maxTabs = 200): Promise<void> {
  for (let i = 0; i < maxTabs; i++) {
    if (await target.evaluate((el) => el === document.activeElement)) {
      return;
    }
    await page.keyboard.press('Tab');
  }
  throw new Error('Could not reach target element via Tab within the iteration budget');
}

test('add, toggle, and delete a todo using only the keyboard (Tab / Enter / Space)', async ({
  signedInPage: page,
}) => {
  const title = `Keyboard E2E todo ${Date.now()}`;

  // Reach the title input purely via Tab (never page.click / locator.focus()).
  const input = page.getByRole('textbox', { name: 'New todo title' });
  await tabTo(page, input);
  await expect(input).toBeFocused();

  // Visible focus ring: the focused input must not have outline suppressed.
  const inputOutline = await input.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(inputOutline).not.toBe('none');

  await page.keyboard.type(title);
  // Enter submits the form natively (no click on the Add button).
  await page.keyboard.press('Enter');
  await expect(input).toHaveValue('');

  const row = page.getByRole('listitem').filter({ hasText: title });
  await expect(row).toBeVisible();

  // Tab from the input, through the Add button, to this row's checkbox.
  const activeCheckbox = row.getByRole('checkbox', { name: `Mark ${title} as completed` });
  await tabTo(page, activeCheckbox);
  await expect(activeCheckbox).toBeFocused();
  await expect(activeCheckbox).not.toBeChecked();

  // Space toggles the checkbox. The row is no longer `disabled` while the
  // PATCH is in flight (C7 fix, master §12): the control gets `aria-busy` /
  // `aria-disabled` instead, so the browser never force-blurs it. Capture the
  // element handle (not the name-based locator, whose accessible name flips
  // from "…as completed" to "…as active" once the toggle lands) so the same
  // DOM node can be checked for focus both mid-flight and after settling.
  const checkboxHandle = await activeCheckbox.elementHandle();
  await page.keyboard.press('Space');

  // Immediately after activation — while the PATCH may still be in flight —
  // focus must still be on this exact checkbox (proves C7 end-to-end, not
  // just at the Vitest unit level).
  await expect(page.evaluate((el) => el === document.activeElement, checkboxHandle)).resolves.toBe(
    true,
  );

  const completedCheckbox = row.getByRole('checkbox', { name: `Mark ${title} as active` });
  await expect(completedCheckbox).toBeChecked();
  await expect(row.getByText(title, { exact: true })).toHaveClass(/line-through/);

  // Focus must still be on the same checkbox after the request settles too.
  await expect(page.evaluate((el) => el === document.activeElement, checkboxHandle)).resolves.toBe(
    true,
  );

  // Continue tabbing forward (from wherever focus currently is) to reach
  // this row's Delete control — proves the flow stays keyboard-operable
  // even across an intervening busy/re-enabled control.
  const deleteButton = row.getByRole('button', { name: `Delete ${title}` });
  await tabTo(page, deleteButton);
  await expect(deleteButton).toBeFocused();

  const deleteOutline = await deleteButton.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(deleteOutline).not.toBe('none');

  // Enter (or Space) activates a button.
  await page.keyboard.press('Enter');

  await expect(row).toHaveCount(0);
});

/**
 * IT3-7 end to end, keys only. Deleting a row destroys the control the user is
 * standing on; without a hand-off the keyboard lands on `body` and the user has
 * to Tab in from the top of the document again.
 */
test('deleting a row with the keyboard hands focus to the next row Delete', async ({
  signedInPage: page,
}) => {
  const stamp = Date.now();
  const first = `Focus E2E first ${stamp}`;
  const second = `Focus E2E second ${stamp}`;

  const input = page.getByRole('textbox', { name: 'New todo title' });
  await tabTo(page, input);
  for (const title of [first, second]) {
    await page.keyboard.type(title);
    await page.keyboard.press('Enter');
    // The form returns focus to the input after a successful add.
    await expect(input).toHaveValue('');
  }

  const firstRow = page.getByRole('listitem').filter({ hasText: first });
  const secondDelete = page.getByRole('button', { name: `Delete ${second}` });
  await expect(firstRow).toBeVisible();
  await expect(secondDelete).toBeVisible();

  // Reach the *first* row's Delete purely by tabbing, then activate it.
  const firstDelete = firstRow.getByRole('button', { name: `Delete ${first}` });
  await tabTo(page, firstDelete);
  await expect(firstDelete).toBeFocused();
  await page.keyboard.press('Enter');

  await expect(firstRow).toHaveCount(0);
  // Focus followed the deletion to the next row instead of falling to `body`.
  await expect(secondDelete).toBeFocused();
  const outline = await secondDelete.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).not.toBe('none');

  // And deleting that last row hands the keyboard back to the composer.
  await page.keyboard.press('Enter');
  await expect(page.getByRole('listitem').filter({ hasText: second })).toHaveCount(0);
  await expect(input).toBeFocused();
});

test('sign in using only the keyboard, and a failed submit moves focus to the alert', async ({
  page,
  registeredUser,
}) => {
  await page.goto('/');

  const email = page.getByLabel('Email');
  await tabTo(page, email);
  const emailOutline = await email.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(emailOutline).not.toBe('none');

  await page.keyboard.type(registeredUser.email);
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Password')).toBeFocused();
  await page.keyboard.type('definitely-wrong');
  // Enter submits the form natively.
  await page.keyboard.press('Enter');

  const alert = page.getByRole('alert');
  await expect(alert).toHaveText('Invalid email or password.');
  await expect(alert).toBeFocused();

  // Recover with the keyboard alone: Tab from the alert reaches Email again.
  await tabTo(page, page.getByLabel('Password'));
  await page.keyboard.press('Control+a');
  await page.keyboard.type(registeredUser.password);
  await page.keyboard.press('Enter');

  await expect(page.getByRole('heading', { name: 'Todos', level: 1 })).toBeVisible();
});

test('operate the list navigation with the keyboard', async ({ signedInPage: page }) => {
  const listName = `Keyboard list ${Date.now()}`;

  const newListButton = page.getByRole('button', { name: 'New list' });
  await tabTo(page, newListButton);
  await expect(newListButton).toBeFocused();
  await page.keyboard.press('Enter');

  // The form takes focus on open, so the name can be typed straight away.
  const nameInput = page.getByRole('textbox', { name: 'New list name' });
  await expect(nameInput).toBeFocused();
  await page.keyboard.type(listName);
  await page.keyboard.press('Enter');

  const entry = page.getByRole('button', { name: `${listName} (0 active)` });
  await expect(entry).toHaveAttribute('aria-current', 'true');
  // The input clears and keeps focus, ready for another list.
  await expect(nameInput).toHaveValue('');
  await expect(nameInput).toBeFocused();

  // Close the create form; focus goes back to the button that opened it.
  await tabTo(page, page.getByRole('button', { name: 'Cancel' }));
  await page.keyboard.press('Enter');
  await expect(newListButton).toBeFocused();

  // Rename and Delete live in the row's actions menu: Enter opens it and puts
  // focus on the first item, the arrow keys walk it, Enter chooses.
  const menu = page.getByRole('button', { name: `List actions for ${listName}` });
  await tabTo(page, menu);
  await page.keyboard.press('Enter');
  await expect(page.getByRole('menuitem', { name: `Rename ${listName}` })).toBeFocused();
  await page.keyboard.press('ArrowDown');
  const deleteItem = page.getByRole('menuitem', { name: `Delete list ${listName}` });
  await expect(deleteItem).toBeFocused();
  await page.keyboard.press('Enter');

  // The inline confirmation puts focus on its destructive action.
  const confirm = page.getByRole('button', { name: 'Delete list', exact: true });
  await expect(confirm).toBeFocused();
  await page.keyboard.press('Enter');

  await expect(page.getByRole('button', { name: `${listName} (0 active)` })).toHaveCount(0);
});
