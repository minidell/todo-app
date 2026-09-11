import { useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { AiStatus } from '../api/ai';
import type { SubtaskDraft } from '../api/types';
import AddTodoForm from './AddTodoForm';
import AiAddBox from './AiAddBox';
import type { ConfirmDraft } from './AiDraftPreview';
import { FOCUS, FOCUS_VISIBLE, SURFACE } from '../styles';

type Mode = 'manual' | 'ai';

/**
 * The composer's own DOM id. It is the last resort of the post-delete focus
 * rule (IT3-7): when the AI tab is active the title input is inside a hidden
 * panel and cannot take focus, so the section itself does — announced through
 * its `aria-label`, thanks to `tabIndex={-1}`.
 */
export const COMPOSER_SECTION_ID = 'todo-composer';

const MODES: { value: Mode; label: string }[] = [
  { value: 'manual', label: 'Add' },
  { value: 'ai', label: 'Draft with AI' },
];

interface TodoComposerProps {
  /** `null` until `GET /api/ai/status` answers, or if that call failed. */
  aiStatus: AiStatus | null;
  onAdd: (draft: SubtaskDraft) => Promise<void>;
  onConfirmDraft: ConfirmDraft;
  disabled?: boolean;
}

/**
 * One card for "get a todo into the list", with the two ways of doing it as a
 * segmented control (iteration-3 redesign). The natural-language box is no
 * longer a detached section at the foot of the page: it is the AI mode of the
 * same composer, keeping its own copy.
 *
 * The segmented control is a `tablist`, so the two mode buttons never collide
 * with the submit buttons inside the panels — those keep the accessible names
 * `Add` and `Draft with AI`. When AI is switched off entirely there is nothing
 * to choose between and the manual form is rendered on its own.
 */
export default function TodoComposer({
  aiStatus,
  onAdd,
  onConfirmDraft,
  disabled = false,
}: TodoComposerProps) {
  const prefix = useId();
  const [mode, setMode] = useState<Mode>('manual');
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const aiEnabled = aiStatus?.enabled === true;
  const active: Mode = aiEnabled ? mode : 'manual';

  const tabId = (value: Mode): string => `${prefix}-tab-${value}`;
  const panelId = (value: Mode): string => `${prefix}-panel-${value}`;

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number): void {
    let next: number | null = null;
    if (event.key === 'ArrowRight') next = (index + 1) % MODES.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + MODES.length) % MODES.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = MODES.length - 1;
    if (next === null) {
      return;
    }
    event.preventDefault();
    setMode(MODES[next].value);
    tabRefs.current[next]?.focus();
  }

  return (
    // `tabIndex={-1}` makes the card itself a focus target for the post-delete
    // hand-off. The ring is `focus-visible`, not `focus`: it belongs to the
    // keyboard user that hand-off exists for, and clicking the card's padding —
    // which focuses a `tabindex="-1"` container in some browsers — must not
    // draw it.
    <section
      id={COMPOSER_SECTION_ID}
      aria-label="Add a todo"
      tabIndex={-1}
      className={`${SURFACE} p-4 ${FOCUS_VISIBLE}`}
    >
      {aiEnabled && (
        <div
          role="tablist"
          aria-label="How to add a todo"
          className="mb-4 inline-flex gap-1 rounded-lg bg-slate-100 p-1"
        >
          {MODES.map((option, index) => {
            const selected = option.value === active;
            return (
              <button
                key={option.value}
                ref={(element) => {
                  tabRefs.current[index] = element;
                }}
                type="button"
                role="tab"
                id={tabId(option.value)}
                aria-selected={selected}
                aria-controls={panelId(option.value)}
                tabIndex={selected ? 0 : -1}
                onClick={() => setMode(option.value)}
                onKeyDown={(event) => handleTabKeyDown(event, index)}
                className={`rounded-md px-3 py-1.5 text-sm font-medium ${FOCUS} ${
                  selected
                    ? 'bg-white text-slate-900 shadow-sm'
                    : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      )}

      {aiEnabled ? (
        // Both panels stay mounted and the inactive one is `hidden`
        // (decision D-IT4-6). Conditional rendering is what used to throw away
        // a typed title or an unconfirmed AI draft on every tab round trip;
        // `hidden` keeps that state where it already lives, in the component
        // that owns it, and — being `display: none` — takes the panel out of
        // the accessibility tree and out of the tab order at the same time.
        // It is also what makes each tab's `aria-controls` point at a panel
        // that actually exists, in both directions.
        <>
          <div
            role="tabpanel"
            id={panelId('manual')}
            aria-labelledby={tabId('manual')}
            tabIndex={-1}
            hidden={active !== 'manual'}
          >
            <AddTodoForm onAdd={onAdd} disabled={disabled} />
          </div>
          <div
            role="tabpanel"
            id={panelId('ai')}
            aria-labelledby={tabId('ai')}
            tabIndex={-1}
            hidden={active !== 'ai'}
          >
            <AiAddBox
              available={aiStatus?.available === true}
              reason={aiStatus?.reason ?? null}
              onConfirm={onConfirmDraft}
            />
          </div>
        </>
      ) : (
        <AddTodoForm onAdd={onAdd} disabled={disabled} />
      )}
    </section>
  );
}
