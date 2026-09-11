import { randomUUID } from 'node:crypto';
import { test as base, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

export interface RegisteredUser {
  email: string;
  password: string;
}

/**
 * `example.com` rather than the reserved `.test` TLD: email-validator (used by
 * pydantic's EmailStr on the backend) rejects `.test` addresses with a 422.
 *
 * A UUID, not a timestamp plus a counter: the counter restarts at 0 in every
 * Playwright worker, so two workers registering in the same millisecond used
 * to collide on 409 `email_taken`. 52 characters, well inside the address
 * length the backend accepts.
 */
function uniqueEmail(): string {
  return `e2e+${randomUUID()}@example.com`;
}

interface Fixtures {
  /** A freshly registered account, created through the API. */
  registeredUser: RegisteredUser;
  /** A page signed in as `registeredUser`, sitting on the todo app. */
  signedInPage: Page;
}

/**
 * Signs `page` in as `user` and waits for the todo app. Exported so a spec can
 * sign a *second* browser context into the same account (realtime needs two).
 */
export async function signIn(page: Page, user: RegisteredUser): Promise<void> {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Sign in', level: 1 })).toBeVisible();
  await page.getByLabel('Email').fill(user.email);
  await page.getByLabel('Password').fill(user.password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'Todos', level: 1 })).toBeVisible();
}

export const test = base.extend<Fixtures>({
  registeredUser: async ({ request }, use) => {
    const user: RegisteredUser = { email: uniqueEmail(), password: 'e2e-password-123' };
    const response = await request.post('/api/auth/register', {
      data: { email: user.email, password: user.password },
    });
    expect(response.status(), await response.text()).toBe(201);
    await use(user);
  },

  signedInPage: async ({ page, registeredUser }, use) => {
    await signIn(page, registeredUser);
    await use(page);
  },
});

export { expect };
