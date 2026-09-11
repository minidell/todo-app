import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError } from '../api/errors';
import { useAuth } from '../auth/AuthContext';
import { BTN_LINK, BTN_PRIMARY, FIELD_LABEL, INPUT, SURFACE } from '../styles';

export const EMAIL_TAKEN_MESSAGE = 'That email is already registered.';
export const INVALID_FORM_MESSAGE = 'Please check the form and try again.';
export const REGISTER_FAILED_MESSAGE = 'Could not create your account. Please try again.';
export const PASSWORD_HINT = 'At least 8 characters.';

const EMAIL_ID = 'register-email';
const PASSWORD_ID = 'register-password';
const DISPLAY_NAME_ID = 'register-display-name';
const PASSWORD_HINT_ID = 'register-password-hint';
const ERROR_ID = 'register-error';

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return EMAIL_TAKEN_MESSAGE;
    }
    if (error.status === 422) {
      return INVALID_FORM_MESSAGE;
    }
  }
  return REGISTER_FAILED_MESSAGE;
}

interface RegisterScreenProps {
  onShowLogin: () => void;
}

export default function RegisterScreen({ onShowLogin }: RegisterScreenProps) {
  const { register, login, setNotice } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failureCount, setFailureCount] = useState(0);
  const errorRef = useRef<HTMLParagraphElement>(null);

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
      await register(email, password, displayName);
    } catch (caught) {
      setError(messageFor(caught));
      setSubmitting(false);
      setFailureCount((count) => count + 1);
      return;
    }

    try {
      // Registration returns no token (master §6.1) — establish the session
      // through the single login code path.
      await login(email, password);
    } catch {
      setNotice('account-created');
      onShowLogin();
    }
  }

  const describedBy = error ? ERROR_ID : undefined;

  return (
    <main className="mx-auto max-w-sm px-4 py-16">
      <h1 className="mb-6 text-2xl font-bold tracking-tight text-slate-900">
        Create your account
      </h1>

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
            autoComplete="new-password"
            required
            minLength={8}
            maxLength={128}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            aria-describedby={error ? `${PASSWORD_HINT_ID} ${ERROR_ID}` : PASSWORD_HINT_ID}
            disabled={submitting}
            className={INPUT}
          />
          <p id={PASSWORD_HINT_ID} className="text-sm text-slate-600">
            {PASSWORD_HINT}
          </p>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={DISPLAY_NAME_ID} className={FIELD_LABEL}>
            Display name (optional)
          </label>
          <input
            id={DISPLAY_NAME_ID}
            name="display-name"
            type="text"
            autoComplete="nickname"
            maxLength={100}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
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
          {submitting ? 'Creating account…' : 'Create account'}
        </button>
      </form>

      <p className="mt-6 text-sm text-slate-700">
        <button type="button" onClick={onShowLogin} className={BTN_LINK}>
          I already have an account
        </button>
      </p>
    </main>
  );
}
