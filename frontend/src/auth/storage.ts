import type { User } from '../api/types';

const TOKEN_KEY = 'todo.auth.token';
const USER_KEY = 'todo.auth.user';
const SELECTED_LIST_KEY = 'todo.selected-list';

/**
 * Fallback for private-mode browsers where `localStorage` access throws.
 * The session then lives for the lifetime of the tab only, which is the
 * documented degradation rather than a crash.
 */
const memory = new Map<string, string>();

/** Set once localStorage has actually refused us; only then is memory read. */
let fallbackActive = false;

function readRaw(key: string): string | null {
  try {
    const stored = localStorage.getItem(key);
    if (stored !== null) {
      return stored;
    }
  } catch {
    fallbackActive = true;
    return memory.get(key) ?? null;
  }
  return fallbackActive ? (memory.get(key) ?? null) : null;
}

function writeRaw(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    fallbackActive = true;
    memory.set(key, value);
  }
}

function removeRaw(key: string): void {
  memory.delete(key);
  try {
    localStorage.removeItem(key);
  } catch {
    // Nothing else to do.
  }
}

export function loadToken(): string | null {
  return readRaw(TOKEN_KEY);
}

export function saveToken(token: string): void {
  writeRaw(TOKEN_KEY, token);
}

export function clearToken(): void {
  removeRaw(TOKEN_KEY);
}

export function loadUser(): User | null {
  const raw = readRaw(USER_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && typeof (parsed as User).id === 'string') {
      return parsed as User;
    }
  } catch {
    // Corrupt payload; treat it as absent.
  }
  return null;
}

export function saveUser(user: User): void {
  try {
    writeRaw(USER_KEY, JSON.stringify(user));
  } catch {
    // Unserializable payload; nothing to store.
  }
}

export function clearUser(): void {
  removeRaw(USER_KEY);
}

export function loadSelectedListId(): string | null {
  return readRaw(SELECTED_LIST_KEY);
}

export function saveSelectedListId(listId: string): void {
  if (listId) {
    writeRaw(SELECTED_LIST_KEY, listId);
  } else {
    // "All lists" is not persisted: on reload we fall back to the default list.
    removeRaw(SELECTED_LIST_KEY);
  }
}

export function clearSelectedListId(): void {
  removeRaw(SELECTED_LIST_KEY);
}

/** Drops every trace of the signed-in session. Passwords are never stored. */
export function clearSession(): void {
  clearToken();
  clearUser();
  clearSelectedListId();
}
