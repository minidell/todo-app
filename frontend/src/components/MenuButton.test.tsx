import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import MenuButton from './MenuButton';

const LABEL = 'AI actions for Buy milk';

interface Handlers {
  split: (active: Element | null) => void;
  suggest: () => void;
  remove: () => void;
}

/**
 * The menu with a focusable button on either side, so "Tab leaves the menu" and
 * "a click outside closes it" have somewhere real to go.
 */
function renderMenu(handlers: Partial<Handlers> = {}) {
  const split = handlers.split ?? vi.fn();
  const suggest = handlers.suggest ?? vi.fn();
  const remove = handlers.remove ?? vi.fn();

  render(
    <div>
      <button type="button">Before</button>
      <MenuButton
        label={LABEL}
        triggerClassName="trigger"
        actions={[
          {
            id: 'subtasks',
            label: 'Split into subtasks',
            name: 'Split into subtasks: Buy milk',
            // Reports where focus is at the moment the action runs.
            onSelect: () => split(document.activeElement),
          },
          {
            id: 'metadata',
            label: 'Suggest priority and tags',
            name: 'Suggest priority and tags: Buy milk',
            onSelect: suggest,
          },
          {
            id: 'delete',
            label: 'Delete',
            name: 'Delete Buy milk',
            danger: true,
            onSelect: remove,
          },
        ]}
      >
        AI
      </MenuButton>
      <button type="button">After</button>
    </div>,
  );

  return { split, suggest, remove };
}

const trigger = (): HTMLElement => screen.getByRole('button', { name: LABEL });
const items = (): HTMLElement[] => screen.getAllByRole('menuitem');

describe('MenuButton', () => {
  it('is a closed menu button until it is asked to open', () => {
    renderMenu();

    expect(trigger()).toHaveAttribute('aria-haspopup', 'menu');
    expect(trigger()).toHaveAttribute('aria-expanded', 'false');
    expect(trigger()).not.toHaveAttribute('aria-controls');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('opens on Enter with focus on the first item and points aria-controls at the menu', async () => {
    const user = userEvent.setup();
    renderMenu();

    trigger().focus();
    await user.keyboard('{Enter}');

    const menu = screen.getByRole('menu', { name: LABEL });
    expect(trigger()).toHaveAttribute('aria-expanded', 'true');
    expect(trigger()).toHaveAttribute('aria-controls', menu.id);
    expect(items()[0]).toHaveFocus();
    // Every item keeps the visible label inside its accessible name.
    expect(items().map((item) => item.getAttribute('aria-label'))).toEqual([
      'Split into subtasks: Buy milk',
      'Suggest priority and tags: Buy milk',
      'Delete Buy milk',
    ]);
  });

  it('opens on Space with focus on the first item', async () => {
    const user = userEvent.setup();
    renderMenu();

    trigger().focus();
    await user.keyboard(' ');

    expect(items()[0]).toHaveFocus();
  });

  it('opens on ArrowDown at the first item and on ArrowUp at the last', async () => {
    const user = userEvent.setup();
    renderMenu();

    trigger().focus();
    await user.keyboard('{ArrowDown}');
    expect(items()[0]).toHaveFocus();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();

    await user.keyboard('{ArrowUp}');
    expect(items()[2]).toHaveFocus();
  });

  it('walks the items with the arrow keys, wrapping at both ends', async () => {
    const user = userEvent.setup();
    renderMenu();

    await user.click(trigger());
    expect(items()[0]).toHaveFocus();

    await user.keyboard('{ArrowDown}');
    expect(items()[1]).toHaveFocus();
    await user.keyboard('{ArrowDown}{ArrowDown}');
    // Past the end it comes back to the first item.
    expect(items()[0]).toHaveFocus();

    await user.keyboard('{ArrowUp}');
    expect(items()[2]).toHaveFocus();
  });

  it('jumps to the ends with Home and End', async () => {
    const user = userEvent.setup();
    renderMenu();

    await user.click(trigger());
    await user.keyboard('{End}');
    expect(items()[2]).toHaveFocus();

    await user.keyboard('{Home}');
    expect(items()[0]).toHaveFocus();
  });

  it('closes on Escape and hands focus back to the trigger', async () => {
    const user = userEvent.setup();
    renderMenu();

    await user.click(trigger());
    await user.keyboard('{Escape}');

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    expect(trigger()).toHaveAttribute('aria-expanded', 'false');
  });

  it('closes on Tab from the trigger, leaving the browser to pick the next stop', async () => {
    const user = userEvent.setup();
    renderMenu();

    await user.click(trigger());
    // Raw event: this asserts what the handler itself does, without the test
    // runner's own idea of where Tab should land.
    const allowedDefault = fireEvent.keyDown(items()[1], { key: 'Tab' });

    // The default is left alone, so focus moves on from the trigger — not from
    // a menu item that is about to be unmounted.
    expect(allowedDefault).toBe(true);
    expect(trigger()).toHaveFocus();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('closes when the pointer goes somewhere else', async () => {
    const user = userEvent.setup();
    renderMenu();

    await user.click(trigger());
    expect(screen.getByRole('menu')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'After' }));

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('closes again when the trigger itself is clicked, without reopening', async () => {
    const user = userEvent.setup();
    renderMenu();

    await user.click(trigger());
    expect(screen.getByRole('menu')).toBeInTheDocument();

    await user.click(trigger());

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger()).toHaveAttribute('aria-expanded', 'false');
  });

  it('restores focus to the trigger before it runs the chosen action', async () => {
    const user = userEvent.setup();
    const split = vi.fn();
    renderMenu({ split });

    await user.click(trigger());
    await user.click(screen.getByRole('menuitem', { name: 'Split into subtasks: Buy milk' }));

    // Whatever the action opens can take focus from here; if it opens nothing,
    // focus is on the trigger rather than lost to the document body.
    expect(split).toHaveBeenCalledWith(trigger());
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
  });
});
