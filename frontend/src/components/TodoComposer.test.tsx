import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AiStatus, AiTodoDraft } from '../api/ai';
import { parseTodo } from '../api/ai';
import TodoComposer from './TodoComposer';

vi.mock('../api/ai', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/ai')>();
  return { ...actual, parseTodo: vi.fn() };
});

const mockParseTodo = vi.mocked(parseTodo);

const AI_UP: AiStatus = { enabled: true, available: true, model: 'qwen2.5:3b', reason: null };

const DRAFT: AiTodoDraft = {
  title: 'Call the dentist',
  description: null,
  priority: 'high',
  due_date: '2026-09-04',
  tags: ['health'],
  subtasks: [],
};

beforeEach(() => {
  vi.resetAllMocks();
});

function renderComposer(aiStatus: AiStatus | null = AI_UP) {
  const onAdd = vi.fn().mockResolvedValue(undefined);
  const onConfirmDraft = vi.fn().mockResolvedValue(undefined);
  render(
    <TodoComposer aiStatus={aiStatus} onAdd={onAdd} onConfirmDraft={onConfirmDraft} />,
  );
  return { onAdd, onConfirmDraft };
}

const tab = (name: string): HTMLElement => screen.getByRole('tab', { name });

describe('TodoComposer', () => {
  it('offers no mode switch at all when AI is off', () => {
    renderComposer(null);

    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    // The manual form is still right there, unwrapped.
    expect(screen.getByRole('textbox', { name: 'New todo title' })).toBeInTheDocument();
  });

  it('starts on Add with a roving tabindex over the two modes', () => {
    renderComposer();

    expect(tab('Add')).toHaveAttribute('aria-selected', 'true');
    expect(tab('Add')).toHaveAttribute('tabindex', '0');
    expect(tab('Draft with AI')).toHaveAttribute('aria-selected', 'false');
    // Only the selected tab is in the tab order; the arrows do the rest.
    expect(tab('Draft with AI')).toHaveAttribute('tabindex', '-1');

    // Both panels are mounted, but only the selected one is in the
    // accessibility tree — `getByRole` would throw on two matches.
    const panel = screen.getByRole('tabpanel');
    expect(panel).toHaveAttribute('aria-labelledby', tab('Add').id);
    expect(panel.id).toBe(tab('Add').getAttribute('aria-controls'));
  });

  it('gives each tab its own panel id, in both directions', async () => {
    const user = userEvent.setup();
    renderComposer();

    const manualPanelId = tab('Add').getAttribute('aria-controls');
    const aiPanelId = tab('Draft with AI').getAttribute('aria-controls');
    expect(manualPanelId).not.toBe(aiPanelId);

    // Each `aria-controls` resolves to a real panel labelled by that tab.
    for (const [tabName, id] of [
      ['Add', manualPanelId],
      ['Draft with AI', aiPanelId],
    ] as const) {
      const panel = document.getElementById(id as string);
      expect(panel).not.toBeNull();
      expect(panel).toHaveAttribute('role', 'tabpanel');
      expect(panel).toHaveAttribute('aria-labelledby', tab(tabName).id);
    }

    // The inactive panel is hidden, so it is neither read nor tabbable.
    expect(document.getElementById(aiPanelId as string)).toHaveAttribute('hidden');
    await user.click(tab('Draft with AI'));
    expect(document.getElementById(manualPanelId as string)).toHaveAttribute('hidden');
    expect(document.getElementById(aiPanelId as string)).not.toHaveAttribute('hidden');
  });

  it('hides the inactive panel from the accessibility tree', async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.click(tab('Draft with AI'));

    // The manual title field is still mounted (that is the whole point) but
    // unreachable by role, so nothing can land on it while it is hidden.
    expect(screen.queryByRole('textbox', { name: 'New todo title' })).not.toBeInTheDocument();
    expect(screen.getByRole('tabpanel')).toBe(
      document.getElementById(tab('Draft with AI').getAttribute('aria-controls') as string),
    );
  });

  it('switches modes with ArrowRight/ArrowLeft and with Home/End', async () => {
    const user = userEvent.setup();
    renderComposer();

    tab('Add').focus();
    await user.keyboard('{ArrowRight}');

    expect(tab('Draft with AI')).toHaveAttribute('aria-selected', 'true');
    expect(tab('Draft with AI')).toHaveFocus();
    expect(tab('Draft with AI')).toHaveAttribute('tabindex', '0');
    expect(tab('Add')).toHaveAttribute('tabindex', '-1');
    expect(screen.getByLabelText('Describe a todo in your own words')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'New todo title' })).not.toBeInTheDocument();

    await user.keyboard('{ArrowLeft}');
    expect(tab('Add')).toHaveAttribute('aria-selected', 'true');
    expect(tab('Add')).toHaveFocus();
    expect(screen.getByRole('textbox', { name: 'New todo title' })).toBeInTheDocument();

    await user.keyboard('{End}');
    expect(tab('Draft with AI')).toHaveAttribute('aria-selected', 'true');

    await user.keyboard('{Home}');
    expect(tab('Add')).toHaveAttribute('aria-selected', 'true');
  });

  it('wraps around at both ends', async () => {
    const user = userEvent.setup();
    renderComposer();

    tab('Add').focus();
    await user.keyboard('{ArrowLeft}');
    expect(tab('Draft with AI')).toHaveAttribute('aria-selected', 'true');

    await user.keyboard('{ArrowRight}');
    expect(tab('Add')).toHaveAttribute('aria-selected', 'true');
  });

  it('switches modes on click too', async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.click(tab('Draft with AI'));

    expect(screen.getByLabelText('Describe a todo in your own words')).toBeInTheDocument();
    // The submit button keeps its own name and stays a button, so it never
    // collides with the tab that carries the same words.
    expect(screen.getByRole('button', { name: 'Draft with AI' })).toBeInTheDocument();
  });
});

/**
 * IT4-1. Switching tabs is navigation, not a reset: nothing the user typed —
 * on either side — may be thrown away by a round trip through the other tab.
 */
describe('TodoComposer state across tab switches', () => {
  it('keeps the typed title, the disclosure and the enrichment fields', async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.type(screen.getByRole('textbox', { name: 'New todo title' }), 'Buy oat milk');
    await user.click(screen.getByRole('button', { name: /More options/ }));
    await user.selectOptions(screen.getByLabelText('New todo priority'), 'high');
    await user.type(screen.getByLabelText('New todo due date'), '2026-09-30');
    await user.type(screen.getByLabelText('New todo tags'), 'errands{Enter}');

    await user.click(tab('Draft with AI'));
    await user.click(tab('Add'));

    expect(screen.getByRole('textbox', { name: 'New todo title' })).toHaveValue('Buy oat milk');
    expect(screen.getByRole('button', { name: /More options/ })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByLabelText('New todo priority')).toHaveValue('high');
    expect(screen.getByLabelText('New todo due date')).toHaveValue('2026-09-30');
    expect(screen.getByRole('button', { name: 'Remove tag errands' })).toBeInTheDocument();
  });

  it('keeps the AI note across a round trip through the manual tab', async () => {
    const user = userEvent.setup();
    renderComposer();

    await user.click(tab('Draft with AI'));
    await user.type(
      screen.getByLabelText('Describe a todo in your own words'),
      'call the dentist tomorrow',
    );

    await user.click(tab('Add'));
    await user.click(tab('Draft with AI'));

    expect(screen.getByLabelText('Describe a todo in your own words')).toHaveValue(
      'call the dentist tomorrow',
    );
  });

  it('keeps an unconfirmed draft, and the edits made to it, and does not steal focus', async () => {
    const user = userEvent.setup();
    mockParseTodo.mockResolvedValue(DRAFT);
    renderComposer();

    await user.click(tab('Draft with AI'));
    await user.type(screen.getByLabelText('Describe a todo in your own words'), 'dentist');
    await user.click(screen.getByRole('button', { name: 'Draft with AI' }));

    const title = await screen.findByRole('textbox', { name: 'Title' });
    // The preview autofocuses its title on mount, so the user can correct it.
    await waitFor(() => expect(title).toHaveFocus());
    await user.clear(title);
    await user.type(title, 'Call the dental practice');

    await user.click(tab('Add'));
    await user.click(tab('Draft with AI'));

    // Nothing was re-parsed and nothing was lost.
    expect(mockParseTodo).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('textbox', { name: 'Title' })).toHaveValue('Call the dental practice');
    // Returning to the tab leaves focus on the tab button: the preview did not
    // remount, so its mount-time autofocus cannot fire a second time.
    expect(tab('Draft with AI')).toHaveFocus();
  });
});
