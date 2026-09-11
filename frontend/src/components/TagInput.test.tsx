import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import TagInput, {
  MAX_TAGS,
  TAG_FORMAT_MESSAGE,
  TAG_LIMIT_MESSAGE,
  isValidTag,
  normalizeTag,
} from './TagInput';
import TagChips from './TagChips';

/** Wraps the controlled input so committed chips actually appear. */
function Harness({
  initial = [],
  onChange,
  onSubmit,
}: {
  initial?: string[];
  onChange?: (tags: string[]) => void;
  onSubmit?: () => void;
}) {
  const [tags, setTags] = useState<string[]>(initial);
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit?.();
      }}
    >
      <TagInput
        label="Tags"
        tags={tags}
        onChange={(next) => {
          setTags(next);
          onChange?.(next);
        }}
      />
      <button type="submit">Submit</button>
    </form>
  );
}

describe('tag normalization', () => {
  it('trims, lowercases and collapses inner whitespace', () => {
    expect(normalizeTag('  Home  ')).toBe('home');
    expect(normalizeTag('Week   End')).toBe('week end');
  });

  it('accepts the documented pattern only', () => {
    expect(isValidTag('home')).toBe(true);
    expect(isValidTag('week end')).toBe(true);
    expect(isValidTag('a_b-2')).toBe(true);
    expect(isValidTag('bad!tag')).toBe(false);
    expect(isValidTag('-lead')).toBe(false);
    expect(isValidTag('')).toBe(false);
  });
});

describe('TagInput', () => {
  it('commits a tag with Enter without submitting the form', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);

    await user.type(screen.getByLabelText('Tags'), 'Home{Enter}');

    expect(screen.getByLabelText('Remove tag home')).toBeInTheDocument();
    expect(screen.getByLabelText('Tags')).toHaveValue('');
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('commits a tag with a comma', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.type(screen.getByLabelText('Tags'), 'errand,');

    expect(screen.getByLabelText('Remove tag errand')).toBeInTheDocument();
    expect(screen.getByLabelText('Tags')).toHaveValue('');
  });

  it('removes the last chip on Backspace in an empty field', async () => {
    const user = userEvent.setup();
    render(<Harness initial={['home', 'errand']} />);

    const input = screen.getByLabelText('Tags');
    await user.click(input);
    await user.keyboard('{Backspace}');

    expect(screen.queryByLabelText('Remove tag errand')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Remove tag home')).toBeInTheDocument();

    // With text in the field, Backspace edits the text instead.
    await user.type(input, 'ab');
    await user.keyboard('{Backspace}');
    expect(input).toHaveValue('a');
    expect(screen.getByLabelText('Remove tag home')).toBeInTheDocument();
  });

  it('rejects an invalid character with the tag-format message and keeps the text', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    const input = screen.getByLabelText('Tags');
    await user.type(input, 'Bad!Tag{Enter}');

    expect(await screen.findByRole('alert')).toHaveTextContent(TAG_FORMAT_MESSAGE);
    expect(input).toHaveValue('Bad!Tag');
    expect(input).toHaveAttribute('aria-describedby', screen.getByRole('alert').id);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('collapses duplicates after normalization', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const input = screen.getByLabelText('Tags');
    await user.type(input, '  Home  {Enter}');
    await user.type(input, 'home{Enter}');

    expect(screen.getAllByLabelText(/^Remove tag /)).toHaveLength(1);
  });

  it('refuses an eleventh tag with the limit message', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const ten = Array.from({ length: MAX_TAGS }, (_, index) => `tag${index}`);
    render(<Harness initial={ten} onChange={onChange} />);

    const input = screen.getByLabelText('Tags');
    await user.type(input, 'eleven{Enter}');

    expect(await screen.findByRole('alert')).toHaveTextContent(TAG_LIMIT_MESSAGE);
    expect(onChange).not.toHaveBeenCalled();
    // The text is kept so the user can make room and commit it.
    expect(input).toHaveValue('eleven');
    expect(screen.getAllByLabelText(/^Remove tag /)).toHaveLength(MAX_TAGS);

    // A duplicate of an existing chip is a no-op, not a limit error.
    await user.clear(input);
    await user.type(input, 'tag0{Enter}');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getAllByLabelText(/^Remove tag /)).toHaveLength(MAX_TAGS);

    // Freeing a slot lets the next tag through.
    await user.click(screen.getByLabelText('Remove tag tag0'));
    await user.type(input, 'eleven{Enter}');
    expect(screen.getByLabelText('Remove tag eleven')).toBeInTheDocument();
  });

  it('accepts the tenth tag', async () => {
    const user = userEvent.setup();
    const nine = Array.from({ length: MAX_TAGS - 1 }, (_, index) => `tag${index}`);
    render(<Harness initial={nine} />);

    await user.type(screen.getByLabelText('Tags'), 'tenth{Enter}');

    expect(screen.getByLabelText('Remove tag tenth')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('removes a chip with its remove button', async () => {
    const user = userEvent.setup();
    render(<Harness initial={['home', 'errand']} />);

    await user.click(screen.getByLabelText('Remove tag home'));

    expect(screen.queryByLabelText('Remove tag home')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Remove tag errand')).toBeInTheDocument();
  });

  it('commits a pending tag when the field loses focus', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.type(screen.getByLabelText('Tags'), 'errand');
    await user.click(screen.getByRole('button', { name: 'Submit' }));

    expect(screen.getByLabelText('Remove tag errand')).toBeInTheDocument();
  });
});

describe('TagChips', () => {
  it('names each chip for assistive technology', () => {
    render(<TagChips tags={['home', 'errand']} />);

    expect(screen.getByLabelText('Tag: home')).toHaveTextContent('home');
    expect(screen.getByLabelText('Tag: errand')).toBeInTheDocument();
  });

  it('renders nothing without tags', () => {
    const { container } = render(<TagChips tags={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
