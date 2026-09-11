import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import FilterBar, { DEFAULT_FILTERS } from './FilterBar';

function renderFilters() {
  const onChange = vi.fn();
  render(<FilterBar filters={DEFAULT_FILTERS} onChange={onChange} tags={[]} />);
  return { onChange };
}

describe('FilterBar', () => {
  /**
   * Below `lg` the sidebar sits above the todos, so the fields collapse behind
   * a Show/Hide control there. From `lg` the same control is `lg:hidden` and
   * the fields are always laid out — that part is CSS, so what is asserted here
   * is the state the control drives.
   */
  it('toggles the field group on small screens', async () => {
    const user = userEvent.setup();
    renderFilters();

    const toggle = screen.getByRole('button', { name: 'Show filters' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    const fields = document.getElementById(toggle.getAttribute('aria-controls') ?? '');
    expect(fields).not.toBeNull();
    expect(fields).toContainElement(screen.getByLabelText('Status'));
    // Collapsed on a phone, still laid out from `lg` up.
    expect(fields).toHaveClass('hidden', 'lg:flex');

    await user.click(toggle);

    const opened = screen.getByRole('button', { name: 'Hide filters' });
    expect(opened).toHaveAttribute('aria-expanded', 'true');
    expect(fields).toHaveClass('flex');
    expect(fields).not.toHaveClass('hidden');

    await user.click(opened);

    expect(screen.getByRole('button', { name: 'Show filters' })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
    expect(fields).toHaveClass('hidden', 'lg:flex');
  });

  it('offers Clear filters only once something is filtered', async () => {
    const user = userEvent.setup();
    const { onChange } = renderFilters();

    expect(screen.queryByRole('button', { name: 'Clear filters' })).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText('Status'), 'active');

    expect(onChange).toHaveBeenCalledWith({ ...DEFAULT_FILTERS, status: 'active' });
  });
});
