/**
 * The app's visual language in one place (iteration-3 UI redesign).
 *
 * Three rules hold the layout together and every component composes these
 * strings instead of inventing its own:
 *  - one surface treatment — white card, `slate-200` hairline, small shadow —
 *    on a `slate-100` page, so a group of controls always reads as a group;
 *  - one type scale — page title, section title, body, meta — and one spacing
 *    rhythm (Tailwind's 4px step used in multiples of two, i.e. 8px);
 *  - weight follows importance: exactly one primary button per surface, plain
 *    secondary buttons around it, ghost buttons for row-level actions.
 *
 * Focus rings stay on `focus:` (not `focus-visible:`) for buttons and text
 * fields, matching iteration 2 and the E2E assertion that a Tab-focused
 * control has a non-`none` outline.
 */

export const FOCUS = 'focus:outline-2 focus:outline-offset-2 focus:outline-indigo-600';
export const FOCUS_VISIBLE =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600';

/** The one card treatment used for every grouped surface. */
export const SURFACE = 'rounded-xl border border-slate-200 bg-white shadow-sm';

/** A titled group inside the sidebar or the main column. */
export const SECTION_TITLE = 'text-xs font-semibold tracking-wider text-slate-500 uppercase';
export const FIELD_LABEL = 'text-sm font-medium text-slate-700';
export const META = 'text-sm text-slate-600';
export const META_SMALL = 'text-xs text-slate-600';

/**
 * Every variant carries its own padding, and the shared shape carries none.
 * That is not cosmetic: Tailwind orders utilities in the generated stylesheet,
 * not by their position in the `class` attribute, so appending `py-1.5` at a
 * call site whose class list already has `py-2` changes nothing — `py-2` still
 * wins. A different size means a constant here, never an override there.
 */
const BUTTON_SHAPE = `inline-flex items-center justify-center gap-1.5 rounded-md text-sm font-medium ${FOCUS} disabled:opacity-60 aria-disabled:opacity-60`;
const BUTTON_PRIMARY_SKIN = 'bg-indigo-600 text-white shadow-sm hover:bg-indigo-700';

/** One per surface: the action the user came for. */
export const BTN_PRIMARY = `${BUTTON_SHAPE} px-4 py-2 ${BUTTON_PRIMARY_SKIN}`;
/** The composer's Add — the one button that leads a whole card. */
export const BTN_PRIMARY_LG = `${BUTTON_SHAPE} px-6 py-2.5 ${BUTTON_PRIMARY_SKIN}`;
export const BTN_SECONDARY = `${BUTTON_SHAPE} px-3 py-2 border border-slate-300 bg-white text-slate-900 hover:bg-slate-50`;
/** Row-level actions: compact, and no border until hovered or focused. */
export const BTN_GHOST = `${BUTTON_SHAPE} border border-transparent px-2 py-1 text-slate-600 hover:border-slate-300 hover:bg-white hover:text-slate-900`;
export const BTN_DANGER = `${BUTTON_SHAPE} px-3 py-2 bg-red-700 text-white hover:bg-red-800`;
export const BTN_LINK = `rounded text-sm font-medium text-indigo-700 underline underline-offset-2 hover:text-indigo-900 ${FOCUS}`;

const FIELD_SHAPE = `w-full rounded-md border border-slate-300 bg-white text-slate-900 placeholder:text-slate-500 ${FOCUS} disabled:opacity-60 aria-disabled:opacity-60`;

export const INPUT = `${FIELD_SHAPE} px-3 py-2 text-sm`;
/** The composer's title field — the one input that leads a whole card. */
export const INPUT_LG = `${FIELD_SHAPE} px-4 py-2.5 text-base`;
export const SELECT = `w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 ${FOCUS} disabled:opacity-60`;
export const CHECKBOX = `h-4 w-4 shrink-0 accent-indigo-600 ${FOCUS_VISIBLE} aria-disabled:opacity-60`;

/**
 * AI output is a proposal, never a result: every panel that shows something the
 * model produced gets the same accent surface so it reads as "confirm me".
 */
export const AI_SURFACE = 'rounded-lg border border-indigo-200 bg-indigo-50/60';
