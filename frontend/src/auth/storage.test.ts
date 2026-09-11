import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { User } from '../api/types';
import {
  clearSelectedListId,
  clearSession,
  clearToken,
  clearUser,
  loadSelectedListId,
  loadToken,
  loadUser,
  saveSelectedListId,
  saveToken,
  saveUser,
} from './storage';

const user: User = {
  id: 'user-1',
  email: 'me@example.test',
  display_name: null,
  created_at: '2026-09-02T11:22:33.123456Z',
};

beforeEach(() => {
  localStorage.clear();
  clearSession();
  vi.restoreAllMocks();
});

describe('auth storage', () => {
  it('round-trips the token under todo.auth.token', () => {
    expect(loadToken()).toBeNull();
    saveToken('jwt-value');
    expect(localStorage.getItem('todo.auth.token')).toBe('jwt-value');
    expect(loadToken()).toBe('jwt-value');
    clearToken();
    expect(loadToken()).toBeNull();
  });

  it('round-trips the user as JSON under todo.auth.user', () => {
    saveUser(user);
    expect(JSON.parse(localStorage.getItem('todo.auth.user') ?? 'null')).toEqual(user);
    expect(loadUser()).toEqual(user);
    clearUser();
    expect(loadUser()).toBeNull();
  });

  it('returns null for a corrupt stored user', () => {
    localStorage.setItem('todo.auth.user', 'not json');
    expect(loadUser()).toBeNull();
  });

  it('persists a selected list id and drops the key for "All lists"', () => {
    saveSelectedListId('list-1');
    expect(loadSelectedListId()).toBe('list-1');
    saveSelectedListId('');
    expect(loadSelectedListId()).toBeNull();
    expect(localStorage.getItem('todo.selected-list')).toBeNull();
    saveSelectedListId('list-2');
    clearSelectedListId();
    expect(loadSelectedListId()).toBeNull();
  });

  it('clearSession removes token, user and the selected list', () => {
    saveToken('jwt-value');
    saveUser(user);
    saveSelectedListId('list-1');

    clearSession();

    expect(loadToken()).toBeNull();
    expect(loadUser()).toBeNull();
    expect(loadSelectedListId()).toBeNull();
  });

  it('falls back to memory when localStorage throws (private mode)', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceeded');
    });
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    });

    saveToken('memory-token');
    saveUser(user);
    expect(loadToken()).toBe('memory-token');
    expect(loadUser()).toEqual(user);

    setItem.mockRestore();
    getItem.mockRestore();
    clearSession();
    expect(loadToken()).toBeNull();
  });

  it('never stores a password-like key', () => {
    saveToken('jwt-value');
    saveUser(user);
    const keys = Object.keys(localStorage);
    expect(keys.some((key) => key.toLowerCase().includes('password'))).toBe(false);
  });
});
