import { expect, signIn, test } from './fixtures';

// Run with the browser cache inside the project root, as everywhere else:
//   PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright npm run e2e

/** Realtime frames travel the network; give them room without being flaky. */
const ARRIVES = { timeout: 10_000 };

test('a second tab follows create, complete and delete without reloading', async ({
  browser,
  signedInPage: tabA,
  registeredUser,
}, testInfo) => {
  // A separate context means separate storage, so tab B has its own client id
  // and therefore does *not* suppress tab A's events as its own echo.
  const contextB = await browser.newContext({
    baseURL: testInfo.project.use.baseURL,
  });
  const tabB = await contextB.newPage();

  try {
    await signIn(tabB, registeredUser);

    const title = `Realtime ${Date.now()}`;
    const rowA = tabA.getByRole('listitem').filter({ hasText: title });
    const rowB = tabB.getByRole('listitem').filter({ hasText: title });

    // --- create ------------------------------------------------------------
    await tabA.getByRole('textbox', { name: 'New todo title' }).fill(title);
    await tabA.getByRole('button', { name: 'Add' }).click();
    await expect(rowA).toHaveCount(1);
    await expect(rowB).toBeVisible(ARRIVES);
    // The originating tab applied its own HTTP response and ignored the echo.
    await expect(rowA).toHaveCount(1);

    // --- complete ----------------------------------------------------------
    await rowA.getByRole('checkbox', { name: `Mark ${title} as completed` }).click();
    await expect(
      rowB.getByRole('checkbox', { name: `Mark ${title} as active` }),
    ).toBeChecked(ARRIVES);
    await expect(rowA).toHaveCount(1);

    // --- delete ------------------------------------------------------------
    await rowA.getByRole('button', { name: `Delete ${title}` }).click();
    await expect(rowA).toHaveCount(0);
    await expect(rowB).toHaveCount(0, ARRIVES);
  } finally {
    await contextB.close();
  }
});
