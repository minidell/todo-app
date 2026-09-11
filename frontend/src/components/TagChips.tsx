interface TagChipsProps {
  tags: string[];
}

/** Read-only tag chips for a todo row. Names are displayed as stored (D-D2). */
export default function TagChips({ tags }: TagChipsProps) {
  if (tags.length === 0) {
    return null;
  }

  return (
    <ul className="flex flex-wrap gap-1.5">
      {tags.map((tag) => (
        <li
          key={tag}
          aria-label={`Tag: ${tag}`}
          className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs font-medium text-slate-700"
        >
          {tag}
        </li>
      ))}
    </ul>
  );
}
