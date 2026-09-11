import { useEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';
import type { ListSummary } from '../api/types';
import { ApiError } from '../api/errors';
import ListForm from './ListForm';
import MenuButton from './MenuButton';
import { BTN_DANGER, BTN_GHOST, BTN_SECONDARY, SECTION_TITLE, SURFACE } from '../styles';

export const LIST_NAME_TAKEN_MESSAGE = 'A list with that name already exists.';
export const LAST_LIST_MESSAGE = 'You must keep at least one list.';
export const LIST_FAILED_MESSAGE = 'Could not update your lists. Please try again.';
export const ALL_LISTS_LABEL = 'All lists';

const ERROR_ID = 'list-error';

type Mode =
  | { kind: 'idle' }
  | { kind: 'create' }
  | { kind: 'rename'; listId: string }
  | { kind: 'confirm-delete'; listId: string };

export function listErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'list_name_taken') {
      return LIST_NAME_TAKEN_MESSAGE;
    }
    if (error.code === 'cannot_delete_last_list') {
      return LAST_LIST_MESSAGE;
    }
  }
  return LIST_FAILED_MESSAGE;
}

/** The accessible name of a list entry: the name plus what the badge means. */
export function listNavLabel(list: ListSummary): string {
  return `${list.name} (${list.active_count} active)`;
}

interface ListNavProps {
  lists: ListSummary[];
  /** `''` means "All lists" — todos are then fetched without a `list_id`. */
  selectedListId: string;
  onSelect: (listId: string) => void;
  onCreate: (name: string) => Promise<void>;
  onRename: (listId: string, name: string) => Promise<void>;
  onDelete: (listId: string) => Promise<void>;
  disabled?: boolean;
}

/**
 * The user's lists as a vertical navigation (iteration-3 redesign): every list
 * is one row carrying its own active count, the current one is highlighted and
 * marked `aria-current`, and rename/delete live in a per-row actions menu
 * instead of two permanently loud buttons. Creating, renaming and deleting all
 * happen inline, right where the affected row is.
 */
export default function ListNav({
  lists,
  selectedListId,
  onSelect,
  onCreate,
  onRename,
  onDelete,
  disabled = false,
}: ListNavProps) {
  const [mode, setMode] = useState<Mode>({ kind: 'idle' });
  const [error, setError] = useState<string | null>(null);
  const [failureCount, setFailureCount] = useState(0);
  // Bumped after a delete so the effect below runs once the parent has decided
  // what is selected now. The ref is what actually arms it: without a one-shot
  // flag the effect would fire again on *every* later selection change — after
  // a create, or when another tab deletes the open list — and rip focus out of
  // whatever the user was typing in.
  const [deleteCount, setDeleteCount] = useState(0);
  const refocusSelection = useRef(false);

  const newListButtonRef = useRef<HTMLButtonElement>(null);
  const confirmDeleteRef = useRef<HTMLButtonElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  const itemRefs = useRef(new Map<string, HTMLButtonElement>());
  const menuRefs = useRef(new Map<string, RefObject<HTMLButtonElement | null>>());

  /** One stable ref object per list for its actions-menu trigger. */
  function menuRef(listId: string): RefObject<HTMLButtonElement | null> {
    const existing = menuRefs.current.get(listId);
    if (existing) {
      return existing;
    }
    const created: RefObject<HTMLButtonElement | null> = { current: null };
    menuRefs.current.set(listId, created);
    return created;
  }

  const target =
    mode.kind === 'rename' || mode.kind === 'confirm-delete'
      ? (lists.find((list) => list.id === mode.listId) ?? null)
      : null;

  // Focus the alert after each failed list action (WCAG 3.3.1).
  useEffect(() => {
    if (failureCount > 0) {
      errorRef.current?.focus();
    }
  }, [failureCount]);

  // The inline confirmation places focus on its destructive action.
  useEffect(() => {
    if (mode.kind === 'confirm-delete') {
      confirmDeleteRef.current?.focus();
    }
  }, [mode]);

  // A list can disappear (deleted in another tab) while a form is open on it.
  useEffect(() => {
    if ((mode.kind === 'rename' || mode.kind === 'confirm-delete') && !target) {
      setMode({ kind: 'idle' });
    }
  }, [mode, target]);

  useEffect(() => {
    if (!refocusSelection.current) {
      return;
    }
    refocusSelection.current = false;
    itemRefs.current.get(selectedListId)?.focus();
  }, [deleteCount, selectedListId]);

  function fail(caught: unknown): void {
    setError(listErrorMessage(caught));
    setFailureCount((count) => count + 1);
  }

  // The form clears/keeps its field based on whether these reject, so both
  // handlers rethrow after recording the message.
  async function handleCreate(name: string): Promise<void> {
    setError(null);
    try {
      await onCreate(name);
    } catch (caught) {
      fail(caught);
      throw caught;
    }
  }

  async function handleRename(name: string): Promise<void> {
    if (!target) return;
    const listId = target.id;
    setError(null);
    try {
      await onRename(listId, name);
    } catch (caught) {
      fail(caught);
      throw caught;
    }
    setMode({ kind: 'idle' });
    menuRef(listId).current?.focus();
  }

  async function handleDelete(listId: string): Promise<void> {
    setError(null);
    try {
      await onDelete(listId);
      setMode({ kind: 'idle' });
      // Arm the one-shot, then trigger the effect that consumes it.
      refocusSelection.current = true;
      setDeleteCount((count) => count + 1);
    } catch (caught) {
      setMode({ kind: 'idle' });
      fail(caught);
    }
  }

  function itemClassName(active: boolean): string {
    return [
      'flex min-w-0 flex-1 items-center justify-between gap-2 rounded-md px-3 py-2 text-left text-sm',
      'focus:outline-2 focus:outline-offset-2 focus:outline-indigo-600',
      active
        ? 'bg-indigo-50 font-semibold text-indigo-900'
        : 'font-medium text-slate-700 hover:bg-slate-100 hover:text-slate-900',
      disabled ? 'opacity-60' : '',
    ].join(' ');
  }

  function badgeClassName(active: boolean): string {
    return `shrink-0 rounded-full px-2 py-0.5 text-xs font-semibold ${
      active ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-700'
    }`;
  }

  return (
    <nav aria-label="Todo lists" className={`${SURFACE} p-3`}>
      <div className="mb-2 flex items-center justify-between gap-2 px-1">
        <h2 className={SECTION_TITLE}>Lists</h2>
        <button
          ref={newListButtonRef}
          type="button"
          onClick={() => {
            setError(null);
            setMode({ kind: 'create' });
          }}
          className={`${BTN_GHOST} text-indigo-700 hover:text-indigo-900`}
        >
          <span aria-hidden="true">+</span> New list
        </button>
      </div>

      <ul className="flex flex-col gap-0.5">
        <li>
          <button
            ref={(element) => {
              if (element) {
                itemRefs.current.set('', element);
              } else {
                itemRefs.current.delete('');
              }
            }}
            type="button"
            aria-label={ALL_LISTS_LABEL}
            aria-current={selectedListId === '' ? 'true' : undefined}
            aria-disabled={disabled || undefined}
            onClick={() => {
              if (disabled) return;
              setError(null);
              setMode({ kind: 'idle' });
              onSelect('');
            }}
            className={`${itemClassName(selectedListId === '')} w-full`}
          >
            <span className="truncate">{ALL_LISTS_LABEL}</span>
          </button>
        </li>

        {lists.map((list) => {
          const active = list.id === selectedListId;
          const renaming = mode.kind === 'rename' && mode.listId === list.id;
          const confirming = mode.kind === 'confirm-delete' && mode.listId === list.id;

          return (
            <li key={list.id}>
              <div className="flex items-center gap-1">
                <button
                  ref={(element) => {
                    if (element) {
                      itemRefs.current.set(list.id, element);
                    } else {
                      itemRefs.current.delete(list.id);
                    }
                  }}
                  type="button"
                  aria-label={listNavLabel(list)}
                  aria-current={active ? 'true' : undefined}
                  aria-disabled={disabled || undefined}
                  onClick={() => {
                    if (disabled) return;
                    setError(null);
                    setMode({ kind: 'idle' });
                    onSelect(list.id);
                  }}
                  className={itemClassName(active)}
                >
                  <span className="truncate">{list.name}</span>
                  <span className={badgeClassName(active)}>{list.active_count}</span>
                </button>

                <MenuButton
                  label={`List actions for ${list.name}`}
                  triggerRef={menuRef(list.id)}
                  triggerClassName={`${BTN_GHOST}`}
                  actions={[
                    {
                      id: 'rename',
                      label: 'Rename',
                      name: `Rename ${list.name}`,
                      onSelect: () => {
                        setError(null);
                        setMode({ kind: 'rename', listId: list.id });
                      },
                    },
                    {
                      id: 'delete',
                      label: 'Delete',
                      name: `Delete list ${list.name}`,
                      danger: true,
                      onSelect: () => {
                        setError(null);
                        setMode({ kind: 'confirm-delete', listId: list.id });
                      },
                    },
                  ]}
                >
                  <span aria-hidden="true">•••</span>
                </MenuButton>
              </div>

              {renaming && target && (
                <ListForm
                  key={`rename-${target.id}`}
                  submitLabel="Save list name"
                  initialValue={target.name}
                  errorId={error ? ERROR_ID : undefined}
                  onSubmit={handleRename}
                  onCancel={() => {
                    setError(null);
                    setMode({ kind: 'idle' });
                    menuRef(target.id).current?.focus();
                  }}
                />
              )}

              {confirming && target && (
                <div className="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2">
                  <p className="text-sm text-slate-900">
                    {`Delete “${target.name}” and its todos?`}
                  </p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button
                      ref={confirmDeleteRef}
                      type="button"
                      onClick={() => void handleDelete(target.id)}
                      className={`${BTN_DANGER}`}
                    >
                      Delete list
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setMode({ kind: 'idle' });
                        menuRef(target.id).current?.focus();
                      }}
                      className={`${BTN_SECONDARY}`}
                    >
                      Keep list
                    </button>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {mode.kind === 'create' && (
        <ListForm
          key="create"
          submitLabel="Create list"
          errorId={error ? ERROR_ID : undefined}
          resetOnSuccess
          onSubmit={handleCreate}
          onCancel={() => {
            setError(null);
            setMode({ kind: 'idle' });
            newListButtonRef.current?.focus();
          }}
        />
      )}

      {error && (
        <p
          ref={errorRef}
          id={ERROR_ID}
          role="alert"
          tabIndex={-1}
          className="mt-3 text-sm text-red-700"
        >
          {error}
        </p>
      )}
    </nav>
  );
}
