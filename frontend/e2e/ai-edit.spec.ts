import { expect, test } from './fixtures';

// The AI stack is not part of the default E2E environment. Start it first:
//   docker compose --profile ai up -d --build
//   docker compose --profile ai run --rm ollama-pull      # if the pull failed
// then run with the browser cache inside the project root:
//   E2E_AI=1 PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright npm run e2e
//
// This spec drives the row "Edit with AI" panel (iteration 5) against the
// real model: type an instruction, review the resolved change set, apply it
// through the ordinary endpoints. It never asserts on the model's wording,
// only on structural outcomes (a row exists, a chip exists, focus moved) or on
// values the instruction itself dictates exactly (a literal new title).
test.skip(
  process.env.E2E_AI !== '1',
  'Set E2E_AI=1 with the AI compose profile running (docker compose --profile ai up -d) to run this spec.',
);

/** A small model on CPU is slow; the panel itself gives up well before this. */
const AI_TIMEOUT = { timeout: 90_000 };

test('cancels the panel without asking the AI and returns focus to the menu trigger', async ({
  signedInPage: page,
}) => {
  const title = `Cancel edit check ${Date.now()}`;

  await page.getByRole('textbox', { name: 'New todo title' }).fill(title);
  await page.getByRole('button', { name: 'Add' }).click();

  const row = page.getByTestId('todo-row').filter({ hasText: title });
  await expect(row).toBeVisible();

  const aiButton = row.getByRole('button', { name: `AI actions for ${title}` });
  await aiButton.click();
  await page.getByRole('menuitem', { name: `Edit with AI: ${title}` }).click();

  const panel = page.getByRole('region', { name: `Edit ${title} with AI` });
  await expect(panel).toBeVisible();

  // Criterion 12: focus lands in the textarea on open.
  const textarea = panel.getByLabel('What should the AI change?');
  await expect(textarea).toBeFocused();
  await expect(textarea).toHaveAttribute('maxlength', '500');

  // No model call at all: Cancel closes the panel from stage 1.
  await panel.getByRole('button', { name: 'Cancel' }).click();
  await expect(panel).toHaveCount(0);

  // Criterion 18: focus returns to the row's AI menu trigger.
  await expect(aiButton).toBeFocused();
});

test('renames a todo through a keyboard-only instruction flow and the change survives a reload', async ({
  signedInPage: page,
}) => {
  test.setTimeout(240_000);

  const title = `Dentist appointment ${Date.now()}`;
  const newTitle = `Call the dentist ${Date.now()}`;

  await page.getByRole('textbox', { name: 'New todo title' }).fill(title);
  await page.getByRole('button', { name: 'Add' }).click();

  const row = page.getByTestId('todo-row').filter({ hasText: title });
  await expect(row).toBeVisible();

  // Keyboard-only: Enter opens the menu (focuses the first item), ArrowDown
  // twice reaches the third item ("Edit with AI"), Enter selects it.
  const aiButton = row.getByRole('button', { name: `AI actions for ${title}` });
  await aiButton.focus();
  await page.keyboard.press('Enter');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');

  const panel = page.getByRole('region', { name: `Edit ${title} with AI` });
  await expect(panel).toBeVisible();
  const textarea = panel.getByLabel('What should the AI change?');
  await expect(textarea).toBeFocused();

  // The instruction dictates the exact resulting title, so the outcome is
  // asserted literally rather than on the model's own wording.
  await textarea.fill(`rename it to ${newTitle}`);
  await page.keyboard.press('Tab'); // reaches "Ask the AI"
  await expect(panel.getByRole('button', { name: 'Ask the AI' })).toBeFocused();
  await page.keyboard.press('Enter');

  // D-AI1: nothing is written before Apply — the row still shows the old title.
  await expect(row).toBeVisible();

  const heading = panel.getByRole('heading', { name: 'Suggested changes', level: 4 });
  await expect(heading).toBeVisible(AI_TIMEOUT);
  // Criterion: focus moves to the "Suggested changes" heading when the
  // preview arrives.
  await expect(heading).toBeFocused();

  const checkboxes = panel.getByRole('checkbox');
  await expect(async () => {
    expect(await checkboxes.count()).toBeGreaterThan(0);
  }).toPass(AI_TIMEOUT);

  const applyButton = panel.getByRole('button', { name: /^Apply \d+ changes$/ });
  await expect(applyButton).toBeVisible();
  await applyButton.click();

  await expect(panel).toHaveCount(0);

  const renamedRow = page.getByTestId('todo-row').filter({ hasText: newTitle });
  await expect(renamedRow).toBeVisible();
  // Criterion 18: a successful apply returns focus to the menu trigger too.
  // The trigger's accessible name now includes the new title, so it has to
  // be looked up again rather than reusing the pre-rename `aiButton` locator.
  await expect(
    renamedRow.getByRole('button', { name: `AI actions for ${newTitle}` }),
  ).toBeFocused();

  await page.reload();
  await expect(page.getByTestId('todo-row').filter({ hasText: newTitle })).toBeVisible();
});

test('adds a subtask and a tag, writing nothing until Apply, and both persist', async ({
  signedInPage: page,
}) => {
  test.setTimeout(240_000);

  const title = `Plan the office party ${Date.now()}`;
  const tag = 'party';

  await page.getByRole('textbox', { name: 'New todo title' }).fill(title);
  await page.getByRole('button', { name: 'Add' }).click();

  const row = page.getByTestId('todo-row').filter({ hasText: title });
  await expect(row).toBeVisible();

  await row.getByRole('button', { name: `AI actions for ${title}` }).click();
  await page.getByRole('menuitem', { name: `Edit with AI: ${title}` }).click();

  const panel = page.getByRole('region', { name: `Edit ${title} with AI` });
  const textarea = panel.getByLabel('What should the AI change?');
  await expect(textarea).toBeFocused();
  await textarea.fill(`Add a subtask called Buy candles and tag it ${tag}`);
  await panel.getByRole('button', { name: 'Ask the AI' }).click();

  const heading = panel.getByRole('heading', { name: 'Suggested changes', level: 4 });
  await expect(heading).toBeVisible(AI_TIMEOUT);

  // D-AI1: nothing is written yet — no subtask progress control, no tag chip.
  await expect(row.getByRole('button', { name: /subtasks of/ })).toHaveCount(0);
  await expect(row.locator(`[aria-label="Tag: ${tag}"]`)).toHaveCount(0);

  const checkboxes = panel.getByRole('checkbox');
  await expect(async () => {
    expect(await checkboxes.count()).toBeGreaterThan(0);
  }).toPass(AI_TIMEOUT);

  const applyButton = panel.getByRole('button', { name: /^Apply \d+ changes$/ });
  await expect(applyButton).toBeVisible();
  await applyButton.click();

  await expect(panel).toHaveCount(0);

  // The row now reflects at least one of the two requested changes — a 3B
  // model on an unambiguous two-part instruction is not guaranteed to act on
  // both, so the structural assertion is "at least one landed", never wording.
  const hasSubtaskProgress = row.getByRole('button', { name: /\(\d+\/\d+ subtasks\)/ });
  const hasTagChip = row.locator(`[aria-label="Tag: ${tag}"]`);
  await expect(async () => {
    const [subtaskCount, tagCount] = await Promise.all([
      hasSubtaskProgress.count(),
      hasTagChip.count(),
    ]);
    expect(subtaskCount + tagCount).toBeGreaterThan(0);
  }).toPass(AI_TIMEOUT);

  await page.reload();
  const reloadedRow = page.getByTestId('todo-row').filter({ hasText: title });
  await expect(async () => {
    const [subtaskCount, tagCount] = await Promise.all([
      reloadedRow.getByRole('button', { name: /\(\d+\/\d+ subtasks\)/ }).count(),
      reloadedRow.locator(`[aria-label="Tag: ${tag}"]`).count(),
    ]);
    expect(subtaskCount + tagCount).toBeGreaterThan(0);
  }).toPass(AI_TIMEOUT);
});

test('reports nothing to change for an out-of-scope instruction and keeps it for rephrasing', async ({
  signedInPage: page,
}) => {
  test.setTimeout(240_000);

  const title = `Out of scope check ${Date.now()}`;
  const instruction = 'delete this todo';

  await page.getByRole('textbox', { name: 'New todo title' }).fill(title);
  await page.getByRole('button', { name: 'Add' }).click();

  const row = page.getByTestId('todo-row').filter({ hasText: title });
  await expect(row).toBeVisible();

  await row.getByRole('button', { name: `AI actions for ${title}` }).click();
  await page.getByRole('menuitem', { name: `Edit with AI: ${title}` }).click();

  const panel = page.getByRole('region', { name: `Edit ${title} with AI` });
  const textarea = panel.getByLabel('What should the AI change?');
  await expect(textarea).toBeFocused();
  await textarea.fill(instruction);
  await panel.getByRole('button', { name: 'Ask the AI' }).click();

  // Criterion 15: the empty-result copy, exactly, and the instruction stays
  // in the textarea so the user can rephrase it.
  await expect(
    panel.getByText(
      'The AI did not find anything to change for that instruction. Try being more specific.',
    ),
  ).toBeVisible(AI_TIMEOUT);
  await expect(textarea).toHaveValue(instruction);

  // Still on stage 1: no preview, no "Apply" button rendered.
  await expect(panel.getByRole('heading', { name: 'Suggested changes' })).toHaveCount(0);
  await expect(panel.getByRole('button', { name: /^Apply \d+ changes$/ })).toHaveCount(0);
  await expect(panel.getByRole('button', { name: 'Ask the AI' })).toBeVisible();

  // The todo itself was never touched: the row still shows its original title.
  await expect(row).toBeVisible();

  // Nothing was written (D-AI1): the row is unaffected by the attempted edit.
  await page.reload();
  await expect(page.getByTestId('todo-row').filter({ hasText: title })).toBeVisible();
});
