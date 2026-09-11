import { test, expect } from '@playwright/test';

test('register, stay signed in, sign out, then sign back in', async ({ page }) => {
  const email = `e2e+${Date.now()}-ui@example.com`;
  const password = 'e2e-password-123';

  await page.goto('/');

  // Signed out: the app starts on the login screen.
  await expect(page.getByRole('heading', { name: 'Sign in', level: 1 })).toBeVisible();

  await page.getByRole('button', { name: 'Create an account' }).click();
  await expect(
    page.getByRole('heading', { name: 'Create your account', level: 1 }),
  ).toBeVisible();

  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill(password);
  await page.getByLabel('Display name (optional)').fill('E2E user');
  await page.getByRole('button', { name: 'Create account' }).click();

  // Registration auto-signs in and lands on the todo app with its Inbox list.
  await expect(page.getByRole('heading', { name: 'Todos', level: 1 })).toBeVisible();
  await expect(page.getByRole('button', { name: /^Inbox/ })).toBeVisible();
  await expect(page.getByText('E2E user')).toBeVisible();

  // The session survives a reload (token in localStorage).
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Todos', level: 1 })).toBeVisible();

  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in', level: 1 })).toBeVisible();

  // Signing out really drops the session: a reload stays on the login screen.
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Sign in', level: 1 })).toBeVisible();

  // Wrong password is rejected with the exact copy.
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill('not-the-password');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('alert')).toHaveText('Invalid email or password.');

  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();

  await expect(page.getByRole('heading', { name: 'Todos', level: 1 })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'New todo title' })).toBeVisible();
});

test('registering an email twice reports it as taken', async ({ page, request }) => {
  const email = `e2e+${Date.now()}-dup@example.com`;
  const password = 'e2e-password-123';

  const created = await request.post('/api/auth/register', { data: { email, password } });
  expect(created.status()).toBe(201);

  await page.goto('/');
  await page.getByRole('button', { name: 'Create an account' }).click();
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Create account' }).click();

  await expect(page.getByRole('alert')).toHaveText('That email is already registered.');
});
