import { expect, test } from './fixtures';

// The AI stack is not part of the default E2E environment. Start it first:
//   docker compose --profile ai up -d --build
//   docker compose --profile ai run --rm ollama-pull      # if the pull failed
// then run with the browser cache inside the project root:
//   E2E_AI=1 PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright npm run e2e
test.skip(
  process.env.E2E_AI !== '1',
  'Set E2E_AI=1 with the AI compose profile running (docker compose --profile ai up -d) to run this spec.',
);

/** A small model on CPU is slow; the UI itself gives up at 60 s. */
const AI_TIMEOUT = { timeout: 90_000 };

test('drafts a todo from natural language and only writes it once confirmed', async ({
  signedInPage: page,
}) => {
  const stamp = `${Date.now()}`;
  // The natural-language box is the AI mode of the composer (iteration 3).
  await page.getByRole('tab', { name: 'Draft with AI' }).click();
  // If this fails the model is unreachable: bring the AI profile up first.
  await expect(page.getByRole('button', { name: 'Draft with AI' })).not.toHaveAttribute(
    'aria-disabled',
    'true',
  );

  // Counting `listitem`s would also pick up the draft preview's tag chips and
  // suggested subtasks, and — since a row nests its own tag-chip list — the
  // chips of the todo just created. Only real rows carry the test id, so a
  // tagged draft cannot inflate the "nothing persisted" assertion below.
  const todoRows = page.getByTestId('todo-row');
  const before = await todoRows.count();

  await page
    .getByLabel('Describe a todo in your own words')
    .fill(`buy sunflower seeds for the garden, reference ${stamp}`);
  await page.getByRole('button', { name: 'Draft with AI' }).click();

  const preview = page.getByRole('region', { name: 'Suggested todo' });
  await expect(preview).toBeVisible(AI_TIMEOUT);

  // Nothing has been persisted yet — the AI never writes (decision D-AI1).
  expect(await todoRows.count()).toBe(before);

  // The model picks the wording, so the assertion is on the draft being
  // present and editable, never on what it says.
  const titleField = preview.getByLabel('Title');
  await expect(titleField).toBeFocused();
  await expect(titleField).not.toHaveValue('');

  const title = `AI ${stamp}`;
  await titleField.fill(title);
  await preview.getByRole('button', { name: 'Add this todo' }).click();

  await expect(todoRows.filter({ hasText: title })).toBeVisible();
  await expect(preview).toHaveCount(0);
  expect(await todoRows.count()).toBe(before + 1);

  // It really is persisted: a reload still shows it.
  await page.reload();
  await expect(todoRows.filter({ hasText: title })).toBeVisible();
});
