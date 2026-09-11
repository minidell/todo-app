import { expect, test } from './fixtures';

// The AI stack is not part of the default E2E environment. Start it first:
//   docker compose --profile ai up -d --build
//   docker compose --profile ai run --rm ollama-pull      # if the pull failed
// then run with the browser cache inside the project root:
//   E2E_AI=1 PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright npm run e2e
//
// This spec drives the row AI menu ("AI actions for <title>") against the
// real model — the composer path is covered by ai.spec.ts, this one is the
// per-todo "Split into subtasks" / "Suggest priority and tags" path
// (iteration 4, IT4-2), which was never exercised end-to-end before.
test.skip(
  process.env.E2E_AI !== '1',
  'Set E2E_AI=1 with the AI compose profile running (docker compose --profile ai up -d) to run this spec.',
);

/** A small model on CPU is slow; the panel itself gives up well before this. */
const AI_TIMEOUT = { timeout: 90_000 };

test('splits a todo into subtasks and applies a suggested priority/tags, via the live model', async ({
  signedInPage: page,
}) => {
  // Two live model calls plus ordinary UI steps comfortably exceed the
  // default 30 s Playwright test timeout on a slow/CPU-only host.
  test.setTimeout(240_000);

  // Descriptive enough for the model to actually find subtasks in it — a
  // bare timestamp gives it nothing to split.
  const title = `Plan a small birthday party ${Date.now()}`;

  await page.getByRole('textbox', { name: 'New todo title' }).fill(title);
  await page.getByRole('button', { name: 'Add' }).click();

  const row = page.getByTestId('todo-row').filter({ hasText: title });
  await expect(row).toBeVisible();

  // --- Split into subtasks -------------------------------------------------
  await row.getByRole('button', { name: `AI actions for ${title}` }).click();
  await page.getByRole('menuitem', { name: `Split into subtasks: ${title}` }).click();

  const subtaskPanel = page.getByRole('region', {
    name: `AI subtask suggestions for ${title}`,
  });
  await expect(subtaskPanel).toBeVisible(AI_TIMEOUT);

  // D-AI1: nothing is written before the user accepts — the row shows no
  // subtask progress control yet (it only renders once total > 0).
  await expect(row.getByRole('button', { name: /subtasks of/ })).toHaveCount(0);

  // Wait for the model call to actually finish before looking at the result.
  await expect(subtaskPanel.getByRole('status')).toHaveCount(0, AI_TIMEOUT);

  const addSubtasksButton = subtaskPanel.getByRole('button', { name: 'Add selected subtasks' });
  await expect(addSubtasksButton).toBeVisible();
  await addSubtasksButton.click();

  // The panel closes and the row now shows real, persisted progress.
  await expect(subtaskPanel).toHaveCount(0);
  await expect(row.getByRole('button', { name: /\(\d+\/\d+ subtasks\)/ })).toBeVisible();

  await page.reload();
  const rowAfterFirstReload = page.getByTestId('todo-row').filter({ hasText: title });
  await expect(
    rowAfterFirstReload.getByRole('button', { name: /\(\d+\/\d+ subtasks\)/ }),
  ).toBeVisible();

  // --- Suggest priority and tags, driven with the keyboard only -----------
  const aiButton = rowAfterFirstReload.getByRole('button', { name: `AI actions for ${title}` });
  await aiButton.focus();
  await page.keyboard.press('Enter'); // opens the menu, focuses the first item
  await page.keyboard.press('ArrowDown'); // moves to "Suggest priority and tags"
  await page.keyboard.press('Enter'); // selects it, closes the menu, opens the panel

  const metaPanel = page.getByRole('region', {
    name: `AI priority and tag suggestions for ${title}`,
  });
  await expect(metaPanel).toBeVisible(AI_TIMEOUT);
  await expect(metaPanel.getByRole('status')).toHaveCount(0, AI_TIMEOUT);

  // Assert on shape only (never on the model's wording, per spec §4.5): a
  // priority from the fixed set, and at most five tags.
  const suggestionText = await metaPanel
    .getByText(/^Suggested priority: .+\. Suggested tags: .+\.$/)
    .textContent();
  const match = suggestionText?.match(
    /^Suggested priority: (Low|Medium|High)\. Suggested tags: (none|.+)\.$/,
  );
  expect(match, `unexpected suggestion shape: ${suggestionText}`).not.toBeNull();
  const [, priorityLabel, tagsPart] = match as RegExpMatchArray;
  const suggestedTags = tagsPart === 'none' ? [] : tagsPart.split(', ');
  expect(suggestedTags.length).toBeLessThanOrEqual(5);

  await metaPanel.getByRole('button', { name: 'Apply suggestion' }).click();
  await expect(metaPanel).toHaveCount(0);

  // Focus returns to the menu trigger after the panel closes (the keyboard
  // interaction above only opened/selected; closing happens either way).
  await expect(aiButton).toBeFocused();

  // The row reflects exactly what was applied.
  await expect(
    rowAfterFirstReload.locator(`[aria-label="Priority: ${priorityLabel}"]`),
  ).toBeVisible();
  for (const tag of suggestedTags) {
    await expect(rowAfterFirstReload.locator(`[aria-label="Tag: ${tag}"]`)).toBeVisible();
  }

  await page.reload();
  const rowAfterSecondReload = page.getByTestId('todo-row').filter({ hasText: title });
  await expect(
    rowAfterSecondReload.locator(`[aria-label="Priority: ${priorityLabel}"]`),
  ).toBeVisible();
  for (const tag of suggestedTags) {
    await expect(rowAfterSecondReload.locator(`[aria-label="Tag: ${tag}"]`)).toBeVisible();
  }
});
