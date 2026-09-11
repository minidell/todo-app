import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { User } from '../api/types';
import { ApiError } from '../api/errors';
import { setAuthToken, setUnauthorizedHandler } from '../api/client';
import * as authApi from '../api/auth';
import {
  clearSession,
  loadToken,
  loadUser,
  saveToken,
  saveUser,
} from './storage';

export type AuthStatus = 'loading' | 'anonymous' | 'authenticated';

/**
 * Semantic banner shown on the login screen. The copy itself lives in the
 * screen so this state stays presentation-free.
 */
export type AuthNotice = 'session-expired' | 'account-created' | 'restore-failed' | null;

export interface AuthContextValue {
  status: AuthStatus;
  user: User | null;
  token: string | null;
  notice: AuthNotice;
  setNotice: (notice: AuthNotice) => void;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, displayName?: string) => Promise<User>;
  logout: () => void;
  sessionExpired: () => void;
  retryRestore: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => loadToken());
  const [user, setUser] = useState<User | null>(() => loadUser());
  const [status, setStatus] = useState<AuthStatus>(() =>
    loadToken() ? 'loading' : 'anonymous',
  );
  const [notice, setNotice] = useState<AuthNotice>(null);
  const [restoreAttempt, setRestoreAttempt] = useState(0);

  /** Drops the session everywhere: memory, api client and storage. */
  const forgetSession = useCallback(() => {
    setAuthToken(null);
    clearSession();
    setToken(null);
    setUser(null);
    setStatus('anonymous');
  }, []);

  const sessionExpired = useCallback(() => {
    forgetSession();
    setNotice('session-expired');
  }, [forgetSession]);

  // The api client keeps a single callback; route it through a ref so the
  // handler is registered once and never goes stale.
  const sessionExpiredRef = useRef(sessionExpired);
  sessionExpiredRef.current = sessionExpired;

  useEffect(() => {
    setUnauthorizedHandler(() => {
      sessionExpiredRef.current();
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  // Restore a stored session: the token is only trusted once `me()` confirms it.
  useEffect(() => {
    const stored = loadToken();
    if (!stored) {
      setAuthToken(null);
      setStatus('anonymous');
      return;
    }

    setAuthToken(stored);
    setStatus('loading');
    let cancelled = false;

    async function restore(storedToken: string) {
      try {
        const current = await authApi.me();
        if (cancelled) return;
        saveUser(current);
        setToken(storedToken);
        setUser(current);
        setStatus('authenticated');
        setNotice(null);
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 401) {
          // The global 401 handler already cleared the session and set the
          // session-expiry notice; make sure the state is settled either way.
          forgetSession();
          return;
        }
        // Network/server trouble: keep the token so a retry can succeed.
        setStatus('anonymous');
        setNotice('restore-failed');
      }
    }

    void restore(stored);

    return () => {
      cancelled = true;
    };
  }, [restoreAttempt, forgetSession]);

  const retryRestore = useCallback(() => {
    setNotice(null);
    setRestoreAttempt((attempt) => attempt + 1);
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const response = await authApi.login(email, password);
    setAuthToken(response.access_token);
    saveToken(response.access_token);
    saveUser(response.user);
    setToken(response.access_token);
    setUser(response.user);
    setStatus('authenticated');
    setNotice(null);
  }, []);

  const register = useCallback(
    async (email: string, password: string, displayName?: string) =>
      authApi.register(email, password, displayName),
    [],
  );

  const logout = useCallback(() => {
    forgetSession();
    setNotice(null);
  }, [forgetSession]);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      token,
      notice,
      setNotice,
      login,
      register,
      logout,
      sessionExpired,
      retryRestore,
    }),
    [status, user, token, notice, login, register, logout, sessionExpired, retryRestore],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used inside an AuthProvider');
  }
  return context;
}
