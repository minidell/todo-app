import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { listTodos } from './api/client';
import { listLists } from './api/lists';
import { login, me, register } from './api/auth';
import { ApiError } from './api/errors';
import {
  TEST_TOKEN,
  makeList,
  makeToken,
  makeTodo,
  makeTodoPage,
  makeUser,
  renderApp,
  seedStoredSession,
} from './test/helpers';

vi.mock('./api/client');
vi.mock('./api/lists');
vi.mock('./api/auth');

const mockListTodos = vi.mocked(listTodos);
const mockListLists = vi.mocked(listLists);
const mockLogin = vi.mocked(login);
const mockRegister = vi.mocked(register);
const mockMe = vi.mocked(me);

function storedToken(): string | null {
  return localStorage.getItem('todo.auth.token');
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  mockListLists.mockResolvedValue([makeList()]);
  mockListTodos.mockResolvedValue(makeTodoPage([makeTodo()]));
});

async function signIn(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.type(screen.getByLabelText('Email'), 'me@example.test');
  await user.type(screen.getByLabelText('Password'), 'password123');
  await user.click(screen.getByRole('button', { name: 'Sign in' }));
}

describe('signed-out app', () => {
  it('renders the Sign in screen and requests no todos when there is no token', async () => {
    renderApp();

    expect(await screen.findByRole('heading', { name: 'Sign in', level: 1 })).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toHaveAttribute('type', 'email');
    expect(screen.getByLabelText('Email')).toHaveAttribute('autocomplete', 'email');
    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password');
    expect(screen.getByLabelText('Password')).toHaveAttribute(
      'autocomplete',
      'current-password',
    );
    expect(screen.getByRole('button', { name: 'Create an account' })).toBeInTheDocument();
    expect(mockMe).not.toHaveBeenCalled();
    expect(mockListTodos).not.toHaveBeenCalled();
    expect(mockListLists).not.toHaveBeenCalled();
  });
});

describe('session restore', () => {
  it('renders the todo app when a stored token is still valid', async () => {
    seedStoredSession();
    mockMe.mockResolvedValue(makeUser());

    renderApp();

    expect(await screen.findByRole('heading', { name: 'Todos', level: 1 })).toBeInTheDocument();
    expect(mockMe).toHaveBeenCalledTimes(1);
    expect(screen.getByText('me@example.test')).toBeInTheDocument();
  });

  it('shows the login screen and clears storage when the stored token is rejected', async () => {
    seedStoredSession();
    mockMe.mockRejectedValue(new ApiError(401, 'unauthorized', 'Not authenticated'));

    renderApp();

    expect(await screen.findByRole('heading', { name: 'Sign in', level: 1 })).toBeInTheDocument();
    expect(storedToken()).toBeNull();
    expect(localStorage.getItem('todo.auth.user')).toBeNull();
    expect(mockListTodos).not.toHaveBeenCalled();
  });

  it('offers a retry when the session cannot be restored because of a network failure', async () => {
    const user = userEvent.setup();
    seedStoredSession();
    mockMe.mockRejectedValueOnce(new TypeError('Failed to fetch'));

    renderApp();

    expect(await screen.findByRole('heading', { name: 'Sign in', level: 1 })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Could not reach the server. Please try again.',
    );
    // The token survives a network blip so a retry can succeed.
    expect(storedToken()).toBe(TEST_TOKEN);

    mockMe.mockResolvedValue(makeUser());
    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('heading', { name: 'Todos', level: 1 })).toBeInTheDocument();
  });
});

describe('sign in', () => {
  it('stores the token and loads lists and todos on success', async () => {
    const user = userEvent.setup();
    mockLogin.mockResolvedValue(makeToken());

    renderApp();
    await screen.findByRole('heading', { name: 'Sign in', level: 1 });
    await signIn(user);

    expect(await screen.findByRole('heading', { name: 'Todos', level: 1 })).toBeInTheDocument();
    expect(mockLogin).toHaveBeenCalledWith('me@example.test', 'password123');
    expect(storedToken()).toBe(TEST_TOKEN);
    expect(JSON.parse(localStorage.getItem('todo.auth.user') ?? 'null')).toEqual(makeUser());
    expect(mockListLists).toHaveBeenCalled();
    await waitFor(() => expect(mockListTodos).toHaveBeenCalledWith(expect.objectContaining({ list_id: 'list-1' })));
  });

  it('shows the invalid-credentials alert, keeps no token and moves focus to it', async () => {
    const user = userEvent.setup();
    mockLogin.mockRejectedValue(
      new ApiError(401, 'invalid_credentials', 'Invalid email or password'),
    );

    renderApp();
    await screen.findByRole('heading', { name: 'Sign in', level: 1 });
    await signIn(user);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Invalid email or password.');
    expect(storedToken()).toBeNull();
    await waitFor(() => expect(alert).toHaveFocus());
    expect(screen.getByLabelText('Email')).toHaveAttribute('aria-describedby', alert.id);
    expect(screen.getByRole('heading', { name: 'Sign in', level: 1 })).toBeInTheDocument();
  });

  it('shows the rate-limit copy on 429', async () => {
    const user = userEvent.setup();
    mockLogin.mockRejectedValue(
      new ApiError(429, 'rate_limited', 'Too many requests. Please wait and try again.'),
    );

    renderApp();
    await screen.findByRole('heading', { name: 'Sign in', level: 1 });
    await signIn(user);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Too many attempts. Please wait a minute and try again.',
    );
  });

  it('shows a generic failure for anything else', async () => {
    const user = userEvent.setup();
    mockLogin.mockRejectedValue(new TypeError('Failed to fetch'));

    renderApp();
    await screen.findByRole('heading', { name: 'Sign in', level: 1 });
    await signIn(user);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not sign in. Please try again.',
    );
  });

  it('marks the form busy and relabels the button while signing in', async () => {
    const user = userEvent.setup();
    let resolveLogin!: (value: ReturnType<typeof makeToken>) => void;
    mockLogin.mockReturnValue(
      new Promise((resolve) => {
        resolveLogin = resolve;
      }),
    );

    renderApp();
    await screen.findByRole('heading', { name: 'Sign in', level: 1 });
    await signIn(user);

    const submitting = await screen.findByRole('button', { name: 'Signing in…' });
    expect(submitting.closest('form')).toHaveAttribute('aria-busy', 'true');

    resolveLogin(makeToken());
    expect(await screen.findByRole('heading', { name: 'Todos', level: 1 })).toBeInTheDocument();
  });
});

describe('register', () => {
  async function goToRegister(user: ReturnType<typeof userEvent.setup>): Promise<void> {
    renderApp();
    await screen.findByRole('heading', { name: 'Sign in', level: 1 });
    await user.click(screen.getByRole('button', { name: 'Create an account' }));
    await screen.findByRole('heading', { name: 'Create your account', level: 1 });
  }

  it('registers, auto-signs in and shows the todo app', async () => {
    const user = userEvent.setup();
    mockRegister.mockResolvedValue(makeUser());
    mockLogin.mockResolvedValue(makeToken());

    await goToRegister(user);

    const password = screen.getByLabelText('Password');
    expect(password).toHaveAttribute('autocomplete', 'new-password');
    expect(
      screen.getByText('At least 8 characters.').id,
    ).toBe(password.getAttribute('aria-describedby'));

    await user.type(screen.getByLabelText('Email'), 'me@example.test');
    await user.type(password, 'password123');
    await user.type(screen.getByLabelText('Display name (optional)'), 'Me');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    expect(await screen.findByRole('heading', { name: 'Todos', level: 1 })).toBeInTheDocument();
    expect(mockRegister).toHaveBeenCalledWith('me@example.test', 'password123', 'Me');
    expect(mockLogin).toHaveBeenCalledWith('me@example.test', 'password123');
  });

  it('shows the email-taken copy on 409', async () => {
    const user = userEvent.setup();
    mockRegister.mockRejectedValue(
      new ApiError(409, 'email_taken', 'That email is already registered'),
    );

    await goToRegister(user);
    await user.type(screen.getByLabelText('Email'), 'me@example.test');
    await user.type(screen.getByLabelText('Password'), 'password123');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('That email is already registered.');
    await waitFor(() => expect(alert).toHaveFocus());
    expect(mockLogin).not.toHaveBeenCalled();
  });

  it('shows the validation copy on 422', async () => {
    const user = userEvent.setup();
    mockRegister.mockRejectedValue(new ApiError(422, null, 'Unprocessable Entity'));

    await goToRegister(user);
    await user.type(screen.getByLabelText('Email'), 'me@example.test');
    await user.type(screen.getByLabelText('Password'), 'password123');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Please check the form and try again.',
    );
  });

  it('shows a generic failure for anything else', async () => {
    const user = userEvent.setup();
    mockRegister.mockRejectedValue(new TypeError('Failed to fetch'));

    await goToRegister(user);
    await user.type(screen.getByLabelText('Email'), 'me@example.test');
    await user.type(screen.getByLabelText('Password'), 'password123');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not create your account. Please try again.',
    );
  });

  it('falls back to the login screen when the auto-login fails', async () => {
    const user = userEvent.setup();
    mockRegister.mockResolvedValue(makeUser());
    mockLogin.mockRejectedValue(new TypeError('Failed to fetch'));

    await goToRegister(user);
    await user.type(screen.getByLabelText('Email'), 'me@example.test');
    await user.type(screen.getByLabelText('Password'), 'password123');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    expect(await screen.findByRole('heading', { name: 'Sign in', level: 1 })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Account created. Please sign in.');
  });

  it('goes back to the login screen from "I already have an account"', async () => {
    const user = userEvent.setup();
    await goToRegister(user);

    await user.click(screen.getByRole('button', { name: 'I already have an account' }));

    expect(await screen.findByRole('heading', { name: 'Sign in', level: 1 })).toBeInTheDocument();
  });
});

describe('sign out', () => {
  it('clears the stored session and returns to Sign in', async () => {
    const user = userEvent.setup();
    seedStoredSession();
    mockMe.mockResolvedValue(makeUser());

    renderApp();
    await screen.findByRole('heading', { name: 'Todos', level: 1 });

    await user.click(screen.getByRole('button', { name: 'Sign out' }));

    expect(await screen.findByRole('heading', { name: 'Sign in', level: 1 })).toBeInTheDocument();
    expect(storedToken()).toBeNull();
    expect(localStorage.getItem('todo.auth.user')).toBeNull();
    expect(localStorage.getItem('todo.selected-list')).toBeNull();
    // No session-expiry banner: this was a deliberate sign-out.
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});
