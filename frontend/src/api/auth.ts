import type { TokenResponse, User } from './types';
import { request } from './client';

/**
 * `POST /api/auth/register` — creates the account and its default `Inbox`
 * list. Returns the user; no token (the client logs in next, master §6.1).
 */
export async function register(
  email: string,
  password: string,
  displayName?: string
): Promise<User> {
  const body: { email: string; password: string; display_name?: string } = {
    email,
    password,
  };
  const trimmed = displayName?.trim();
  if (trimmed) {
    body.display_name = trimmed;
  }
  return request<User>('/api/auth/register', 'POST', body, { auth: false });
}

/** `POST /api/auth/login` — 401 `invalid_credentials` on bad credentials. */
export async function login(email: string, password: string): Promise<TokenResponse> {
  return request<TokenResponse>(
    '/api/auth/login',
    'POST',
    { email, password },
    { auth: false }
  );
}

/** `GET /api/auth/me` — the caller behind the current bearer token. */
export async function me(): Promise<User> {
  return request<User>('/api/auth/me');
}
