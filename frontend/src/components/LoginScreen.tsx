import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError } from '../api/errors';
import { useAuth } from '../auth/AuthContext';
import { BTN_LINK, BTN_PRIMARY, BTN_SECONDARY, FIELD_LABEL, INPUT, SURFACE } from '../styles';

export const INVALID_CREDENTIALS_MESSAGE = 'Invalid email or password.';
export const RATE_LIMITED_MESSAGE = 'Too many attempts. Please wait a minute and try again.';
export const SIGN_IN_FAILED_MESSAGE = 'Could not sign in. Please try again.';
export const SESSION_EXPIRED_MESSAGE = 'Your session expired. Please sign in again.';
export const ACCOUNT_CREATED_MESSAGE = 'Account created. Please sign in.';
export const RESTORE_FAILED_MESSAGE = 'Could not reach the server. Please try again.';

const EMAIL_ID = 'login-email';
const PASSWORD_ID = 'login-password';
const ERROR_ID = 'login-error';

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return INVALID_CREDENTIALS_MESSAGE;
    }
    if (error.status === 429) {
      return RATE_LIMITED_MESSAGE;
    }
  }
  return SIGN_IN_FAILED_MESSAGE;
}

interface LoginScreenProps {
  onShowRegister: () => void;
}

export default function LoginScreen({ onShowRegister }: LoginScreenProps) {
  const { login, notice, retryRestore } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failureCount, setFailureCount] = useState(0);
  const errorRef = useRef<HTMLParagraphElement>(null);

  // Move focus to the alert after every failed submit so the failure is
  // announced and immediately reachable (WCAG 3.3.1).
  useEffect(() => {
    if (failureCount > 0) {
      errorRef.current?.focus();
    }
  }, [failureCount]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await login(email, password);
    } catch (caught) {
      setError(messageFor(caught));
      setSubmitting(false);
      setFailureCount((count) => count + 1);
    }
  }

  const noticeMessage =
    notice === 'session-expired'
      ? SESSION_EXPIRED_MESSAGE
      : notice === 'account-created'
        ? ACCOUNT_CREATED_MESSAGE
        : notice === 'restore-failed'
          ? RESTORE_FAILED_MESSAGE
          : null;

  const describedBy = error ? ERROR_ID : undefined;

  return (
    <main className="mx-auto max-w-sm px-4 py-16">
      <h1 className="mb-6 text-2xl font-bold tracking-tight text-slate-900">Sign in</h1>

      {noticeMessage && (
        <p
          role="status"
          className="mb-4 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800"
        >
          {noticeMessage}
          {notice === 'restore-failed' && (
            <button type="button" onClick={retryRestore} className={`${BTN_SECONDARY} ml-2`}>
              Retry
            </button>
          )}
        </p>
      )}

      {error && (
        <p
          ref={errorRef}
          id={ERROR_ID}
          role="alert"
          tabIndex={-1}
          className="mb-4 text-sm font-medium text-red-700"
        >
          {error}
        </p>
      )}

      <form
        onSubmit={handleSubmit}
        aria-busy={submitting ? 'true' : undefined}
        className={`${SURFACE} flex flex-col gap-4 p-5`}
      >
        <div className="flex flex-col gap-1">
          <label htmlFor={EMAIL_ID} className={FIELD_LABEL}>
            Email
          </label>
          <input
            id={EMAIL_ID}
            name="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            aria-describedby={describedBy}
            disabled={submitting}
            className={INPUT}
          />
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={PASSWORD_ID} className={FIELD_LABEL}>
            Password
          </label>
          <input
            id={PASSWORD_ID}
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            aria-describedby={describedBy}
            disabled={submitting}
            className={INPUT}
          />
        </div>

        <button
          type="submit"
          disabled={submitting}
          className={BTN_PRIMARY}
        >
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>

      <p className="mt-6 text-sm text-slate-700">
        <button type="button" onClick={onShowRegister} className={BTN_LINK}>
          Create an account
        </button>
      </p>
    </main>
  );
}
