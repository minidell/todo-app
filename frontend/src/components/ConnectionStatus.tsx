import type { ConnectionMode } from '../hooks/useEventStream';

export const LIVE_MESSAGE = 'Live updates on';
export const POLLING_MESSAGE = 'Live updates paused — refreshing every 15 seconds';

interface ConnectionStatusProps {
  mode: ConnectionMode;
}

/**
 * An unobtrusive sidebar indicator (slice-4 spec F4). While the stream is up it
 * is visually hidden — losing realtime is never an error the user has to act
 * on, so there is no dialog and no alert, just a polite live region.
 */
export default function ConnectionStatus({ mode }: ConnectionStatusProps) {
  const polling = mode === 'polling';

  return (
    <p
      role="status"
      aria-live="polite"
      className={polling ? 'text-xs text-slate-600' : 'sr-only'}
    >
      {polling ? POLLING_MESSAGE : LIVE_MESSAGE}
    </p>
  );
}
