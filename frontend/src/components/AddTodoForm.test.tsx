import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import AddTodoForm from './AddTodoForm';

describe('AddTodoForm', () => {
  it('keeps priority, due date and tags behind the More options disclosure', async () => {
    const user = userEvent.setup();
    const onAdd = vi.fn().mockResolvedValue(undefined);
    render(<AddTodoForm onAdd={onAdd} />);

    const disclosure = screen.getByRole('button', { name: 'More options' });
    expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    expect(disclosure).toHaveAttribute('aria-controls');
    expect(screen.queryByLabelText('New todo priority')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('New todo due date')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('New todo tags')).not.toBeInTheDocument();

    await user.click(disclosure);

    expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    const fields = document.getElementById(disclosure.getAttribute('aria-controls') ?? '');
    expect(fields).not.toBeNull();
    expect(screen.getByLabelText('New todo priority')).toBeInTheDocument();
    expect(fields).toContainElement(screen.getByLabelText('New todo due date'));
    expect(fields).toContainElement(screen.getByLabelText('New todo tags'));

    await user.click(disclosure);

    expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByLabelText('New todo priority')).not.toBeInTheDocument();
  });

  it('sends what the secondary row was filled in with and clears it afterwards', async () => {
    const user = userEvent.setup();
    const onAdd = vi.fn().mockResolvedValue(undefined);
    render(<AddTodoForm onAdd={onAdd} />);

    await user.click(screen.getByRole('button', { name: 'More options' }));
    await user.selectOptions(screen.getByLabelText('New todo priority'), 'high');
    await user.type(screen.getByLabelText('New todo tags'), 'home{Enter}');
    await user.type(screen.getByLabelText('New todo title'), 'Water the plants{Enter}');

    expect(onAdd).toHaveBeenCalledWith({
      title: 'Water the plants',
      priority: 'high',
      tags: ['home'],
    });
    // The panel stays open — several enriched todos in a row is the case that
    // needs it — but every field is back to its default.
    expect(screen.getByRole('button', { name: 'More options' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByLabelText('New todo priority')).toHaveValue('medium');
    expect(screen.queryByLabelText('Remove tag home')).not.toBeInTheDocument();
    expect(screen.getByLabelText('New todo title')).toHaveValue('');
    expect(screen.getByLabelText('New todo title')).toHaveFocus();
  });
});
