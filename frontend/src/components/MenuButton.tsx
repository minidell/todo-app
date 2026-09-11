import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode, RefObject } from 'react';
import { FOCUS } from '../styles';

export interface MenuAction {
  /** Stable key for React. */
  id: string;
  /** Visible item text. */
  label: string;
  /**
   * The item's accessible name. It must contain `label` word for word, so the
   * visible text stays part of the name (WCAG 2.5.3 Label in Name).
   */
  name: string;
  onSelect: () => void;
  danger?: boolean;
}

interface MenuButtonProps {
  /** Accessible name of the trigger and of the menu it opens. */
  label: string;
  /** Visible trigger content — usually a short word or a glyph. */
  children: ReactNode;
  actions: MenuAction[];
  triggerClassName: string;
  /** Lets the owner return focus here after closing a panel the menu opened. */
  triggerRef?: RefObject<HTMLButtonElement | null>;
  align?: 'left' | 'right';
}

/**
 * A small actions menu: one trigger instead of a row of equally loud buttons.
 *
 * It is a real `menu`/`menuitem` widget — arrow keys move between items, Escape
 * closes and hands focus back to the trigger, Tab leaves — but each item is
 * still a plain `<button>` element, so choosing one activates it with Enter or
 * Space for free. Selecting an item restores focus to the trigger *before*
 * running the action: whatever the action opens can then take focus itself,
 * and if it does not (an AI panel that is still loading) focus never falls back
 * to the document body.
 */
export default function MenuButton({
  label,
  children,
  actions,
  triggerClassName,
  triggerRef,
  align = 'right',
}: MenuButtonProps) {
  const menuId = useId();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const fallbackTriggerRef = useRef<HTMLButtonElement>(null);
  const trigger = triggerRef ?? fallbackTriggerRef;
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  // Which end the menu opens on: ArrowUp on the trigger opens on the last item,
  // everything else on the first (WAI-ARIA menu button pattern).
  const openOn = useRef<'first' | 'last'>('first');

  // Opening a menu moves focus into it.
  useEffect(() => {
    if (!open) {
      return;
    }
    const items = itemRefs.current;
    const target = openOn.current === 'last' ? items[items.length - 1] : items[0];
    target?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    function handlePointerDown(event: PointerEvent): void {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [open]);

  function openMenu(where: 'first' | 'last'): void {
    openOn.current = where;
    setOpen(true);
  }

  function close(restoreFocus: boolean): void {
    setOpen(false);
    if (restoreFocus) {
      trigger.current?.focus();
    }
  }

  function handleTriggerKeyDown(event: KeyboardEvent<HTMLButtonElement>): void {
    if (open) {
      return; // While it is open the menu itself owns the arrow keys.
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      openMenu('first');
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      openMenu('last');
    }
  }

  function handleMenuKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    const items = itemRefs.current.filter((item): item is HTMLButtonElement => item !== null);
    if (items.length === 0) {
      return;
    }
    const current = items.indexOf(document.activeElement as HTMLButtonElement);

    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      items[(current + 1 + items.length) % items.length]?.focus();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      items[(current - 1 + items.length) % items.length]?.focus();
    } else if (event.key === 'Home') {
      event.preventDefault();
      items[0]?.focus();
    } else if (event.key === 'End') {
      event.preventDefault();
      items[items.length - 1]?.focus();
    } else if (event.key === 'Tab') {
      // No `preventDefault`: focus goes back to the trigger first, and the
      // browser then moves on from there, so the next stop is the element
      // after the menu button rather than wherever the closing menu left it.
      close(true);
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={trigger}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => (open ? close(false) : openMenu('first'))}
        onKeyDown={handleTriggerKeyDown}
        className={triggerClassName}
      >
        {children}
      </button>

      {open && (
        <div
          id={menuId}
          role="menu"
          aria-label={label}
          onKeyDown={handleMenuKeyDown}
          className={`absolute z-20 mt-1 flex min-w-52 flex-col rounded-lg border border-slate-200 bg-white p-1 shadow-lg ${
            align === 'right' ? 'right-0' : 'left-0'
          }`}
        >
          {actions.map((action, index) => (
            <button
              key={action.id}
              ref={(element) => {
                itemRefs.current[index] = element;
              }}
              type="button"
              role="menuitem"
              tabIndex={-1}
              aria-label={action.name}
              onClick={() => {
                close(true);
                action.onSelect();
              }}
              className={`rounded px-3 py-2 text-left text-sm ${FOCUS} ${
                action.danger
                  ? 'text-red-700 hover:bg-red-50'
                  : 'text-slate-900 hover:bg-slate-100'
              }`}
            >
              {action.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
