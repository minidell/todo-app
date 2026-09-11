import type { User } from '../api/types';
import { BTN_SECONDARY } from '../styles';

interface AppHeaderProps {
  user: User | null;
  onSignOut: () => void;
}

/**
 * The top bar: the product title on the left, who is signed in on the right.
 * It spans the full width above both columns, so the page has exactly one
 * level-1 heading and one place for account actions. The realtime indicator
 * lives at the foot of the sidebar instead (iteration-3 redesign) — it is
 * ambient information, not a header action.
 */
export default function AppHeader({ user, onSignOut }: AppHeaderProps) {
  const identity = user ? user.display_name?.trim() || user.email : null;

  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-[1120px] flex-wrap items-center justify-between gap-3 px-4 py-3">
        <h1 className="text-xl font-bold tracking-tight text-slate-900">Todos</h1>
        <div className="flex min-w-0 items-center gap-3">
          {/* A long address truncates instead of widening the bar — at 375px
              that would give the whole page a horizontal scrollbar. */}
          {identity && (
            <span className="min-w-0 truncate text-sm text-slate-600" title={identity}>
              {identity}
            </span>
          )}
          <button type="button" onClick={onSignOut} className={`${BTN_SECONDARY} shrink-0`}>
            Sign out
          </button>
        </div>
      </div>
    </header>
  );
}
