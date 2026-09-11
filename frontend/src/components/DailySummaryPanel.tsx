import { useEffect, useId, useRef, useState } from 'react';
import type { AiDailySummary, AiUnavailableReason } from '../api/ai';
import {
  AI_SLOW_HINT,
  aiErrorMessage,
  aiUnavailableBanner,
  dailySummary,
  isAbortError,
} from '../api/ai';
import { formatTimeOfDay, todayString } from '../dates';
import { BTN_SECONDARY, FOCUS, META_SMALL, SURFACE } from '../styles';

export const EMPTY_SUMMARY_MESSAGE = 'Nothing to summarise — you have no open todos.';

interface DailySummaryPanelProps {
  /** False when the AI stack is down: the button stays visible but inert. */
  available: boolean;
  /** Why it is unavailable (master §5.1); picks the banner copy. */
  reason?: AiUnavailableReason | null;
  /** The selected list, or `null` for "across all lists". */
  listId: string | null;
}

/**
 * A read-only briefing over the caller's open todos. It writes nothing, so
 * there is no confirmation step — but it is still an AI draft in the sense of
 * D-AI1: plain text, rendered as text, never as HTML.
 *
 * It sits at the top of the main column as a card that is collapsed by default
 * (iteration-3 redesign): the briefing is an occasional treat, not something
 * that should push the todos down the page every time it is on screen.
 */
export default function DailySummaryPanel({
  available,
  reason = null,
  listId,
}: DailySummaryPanelProps) {
  const headingId = useId();
  const bodyId = useId();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<AiDailySummary | null>(null);

  const requestRef = useRef<AbortController | null>(null);

  useEffect(() => () => requestRef.current?.abort(), []);

  async function handleGenerate(): Promise<void> {
    if (!available || busy) {
      return;
    }

    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;

    setBusy(true);
    setError(null);
    try {
      const result = await dailySummary(listId, todayString(), controller.signal);
      setSummary(result);
    } catch (caught) {
      if (isAbortError(caught)) {
        return;
      }
      setSummary(null);
      setError(aiErrorMessage(caught));
    } finally {
      if (requestRef.current === controller) {
        requestRef.current = null;
        setBusy(false);
      }
    }
  }

  return (
    <section aria-labelledby={headingId} className={SURFACE}>
      <h2 id={headingId}>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpen((value) => !value)}
          className={`flex w-full items-center justify-between gap-3 rounded-xl px-4 py-3 text-left text-sm font-semibold text-slate-900 hover:bg-slate-50 ${FOCUS}`}
        >
          Today at a glance
          <span aria-hidden="true" className="text-slate-500">
            {open ? '▾' : '▸'}
          </span>
        </button>
      </h2>

      {open && (
        <div id={bodyId} className="border-t border-slate-200 px-4 py-4">
          {/* An inert button with no explanation is a dead end: say why. */}
          {!available && (
            <p
              role="status"
              className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900"
            >
              {aiUnavailableBanner(reason)}
            </p>
          )}
          <button
            type="button"
            aria-busy={busy ? 'true' : undefined}
            aria-disabled={!available || busy || undefined}
            onClick={() => {
              void handleGenerate();
            }}
            className={BTN_SECONDARY}
          >
            {busy ? 'Writing your summary…' : 'Generate summary'}
          </button>

          {busy && (
            <p role="status" className={`mt-2 ${META_SMALL}`}>
              {AI_SLOW_HINT}
            </p>
          )}

          <div aria-live="polite" className="mt-3 empty:mt-0">
            {summary && summary.todo_count === 0 && (
              <p className="text-sm text-slate-700">{EMPTY_SUMMARY_MESSAGE}</p>
            )}
            {summary && summary.todo_count > 0 && (
              <div className="rounded-lg border border-indigo-200 bg-indigo-50/60 p-3">
                {/* Model output is plain text — never `dangerouslySetInnerHTML`. */}
                <p className="text-sm text-slate-900">{summary.summary}</p>
                <p className={`mt-2 ${META_SMALL}`}>
                  {`Based on ${summary.todo_count} open ${
                    summary.todo_count === 1 ? 'todo' : 'todos'
                  } · generated ${formatTimeOfDay(summary.generated_at)}`}
                </p>
              </div>
            )}
          </div>

          {error && (
            <p role="alert" className="mt-2 text-sm text-red-700">
              {error}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
