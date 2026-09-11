let cachedClientId: string | null = null;

function generateId(): string | null {
  // crypto.randomUUID is undefined in non-secure contexts (plain HTTP on LAN)
  if (typeof crypto?.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return null;
}

// For testing only - reset the cached client ID
export function _resetClientId(): void {
  cachedClientId = null;
}

export function getClientId(): string | null {
  if (cachedClientId) {
    return cachedClientId;
  }

  try {
    const stored = sessionStorage.getItem('todo.client-id');
    if (stored) {
      cachedClientId = stored;
      return cachedClientId;
    }

    const newId = generateId();
    if (newId) {
      sessionStorage.setItem('todo.client-id', newId);
      cachedClientId = newId;
    }
    return cachedClientId;
  } catch {
    // sessionStorage may throw in private mode; fall back to module-level variable
    if (!cachedClientId) {
      cachedClientId = generateId();
    }
    return cachedClientId;
  }
}
