# Iteration 2 · Slice 3 — Priority, Due Dates, Tags, Subtasks, Filter/Sort/Search

Status: **ready for dispatch after slice 2 merges** · Depends on: slice 2 · Unblocks: slice 4
Read together with `docs/specs/iteration-2-master.md` §3, §5, §6.3, §6.4 (binding).

## Goal in one sentence
Give todos real substance — description, priority, due date, tags and one level of subtasks — and
make the list endpoint filterable, sortable and searchable, with matching UI.

## Scheduled contract change (master R11)
`PATCH /api/todos/{id}` becomes a **partial** update in this slice. `{"completed": true}` remains
valid, so no existing call site changes. Tests that assert `PATCH {}` → 422 must be updated to
assert **400 `empty_update`**, and tests asserting that a body without `completed` is rejected
must be replaced.

---

## BACKEND — complexity **complex** (opus) · owns `backend/**`

### B1. Files
Create: `app/repositories/tags.py`, `app/routers/tags.py`, `app/schemas/tags.py`,
`app/query.py` (filter/sort → SQL builder), `tests/test_todos_filters.py`,
`tests/test_todos_sorting.py`, `tests/test_tags_api.py`, `tests/test_subtasks_api.py`,
`tests/test_todo_write_surface.py`.
Modify: `app/schemas/todos.py`, `app/repositories/protocols.py`, `app/repositories/todos.py`,
`app/routers/todos.py`, `app/main.py` (tags router), existing todo tests.

### B2. Schemas (`app/schemas/todos.py`) — all `extra="forbid"`
```python
TagName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]

class TodoCreate(BaseModel):
    title: TodoTitle                       # trimmed 1..200
    list_id: UUID | None = None
    parent_id: UUID | None = None
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    priority: Priority = Priority.MEDIUM
    due_date: date | None = None
    tags: list[TagName] = []               # <= 10 entries, de-duplicated after normalization

class SubtaskCreate(BaseModel):            # same minus list_id / parent_id
class TodoUpdate(BaseModel):               # every field Optional with an UNSET sentinel
    title: TodoTitle | UnsetType = UNSET
    description: str | None | UnsetType = UNSET
    completed: bool | UnsetType = UNSET
    priority: Priority | UnsetType = UNSET
    due_date: date | None | UnsetType = UNSET
    tags: list[TagName] | UnsetType = UNSET
    list_id: UUID | UnsetType = UNSET
    parent_id: UUID | None | UnsetType = UNSET
```
Implement the sentinel with a `model_validator(mode="after")` over
`model_fields_set` rather than a custom type if that is simpler in Pydantic v2: expose
`def changes(self) -> dict[str, Any]` returning only keys present in `model_fields_set`. Empty
`changes()` → `EmptyUpdateError` (400 `empty_update`).
Normalization: `description` that trims to `""` becomes `None`; `tags` are normalized with the
shared `normalize_tag(name)` helper (trim → lowercase → collapse inner whitespace) and validated
against `^[a-z0-9][a-z0-9 _-]{0,29}$` — an invalid tag yields **422** with the offending value in
the standard Pydantic error, and duplicates after normalization are collapsed silently.

### B3. Tags
`SqlAlchemyTagRepository`:
- `get_or_create_many(user_id, names)` — one `SELECT … WHERE user_id=:u AND name IN :names`,
  insert the missing ones, handle a concurrent-insert `IntegrityError` by re-selecting. Returns
  entities in the requested order.
- `list_tags(user_id)` — with `todo_count` via a grouped join (single query, no N+1), ordered
  `name ASC`.
- `rename(user_id, tag_id, name)` — normalize, 409 `tag_name_taken` on collision (also via
  `IntegrityError`), 404 `tag_not_found` otherwise.
- `delete(user_id, tag_id)` — removes the tag; `todo_tags` rows go with the FK cascade.
`app/routers/tags.py` implements master §6.4. There is **no** `POST /api/tags` (D-A3).
Tags orphaned by removing them from their last todo are **kept** (they stay in the user's
vocabulary) — decision, tested.

### B4. Subtasks
- `POST /api/todos/{id}/subtasks` → creates a child with `parent_id={id}`, inheriting
  `list_id` and `user_id` from the parent (invariants I2, I3).
- 400 `subtask_depth_exceeded` when `{id}` already has a `parent_id` (invariant I1).
- `POST /api/todos` with `parent_id` behaves identically (same code path); an unknown/foreign
  `parent_id` → 404 `todo_not_found`.
- `PATCH` moving `parent_id` `null → uuid` is rejected with 400 `subtask_depth_exceeded` when the
  todo already has subtasks; moving `uuid → null` promotes a subtask to top level (allowed).
- Changing a parent's `list_id` moves its subtasks in the same transaction (I3).
- Deleting a parent deletes its subtasks (DB cascade, already in the baseline schema).
- Completing a parent does **not** touch subtasks (D-A2).

### B5. `app/query.py` — `TodoQuery` → SQL (the heart of this slice)
`build_todo_query(user_id, q: TodoQuery) -> tuple[Select, Select]` returning the row select and
the matching `count()` select. Rules, implemented exactly:
- base: `Todo.user_id == user_id`, `Todo.parent_id.is_(None)`
- `list_id` → equality (validated by the router beforehand → 404 if foreign)
- `status`: `active` → `completed.is_(False)`, `completed` → `completed.is_(True)`, `all` → no clause
- `priorities` (non-empty) → `Todo.priority.in_(...)`
- `tags` (non-empty, **AND**) → for each tag one `EXISTS` correlated subquery over
  `todo_tags ⋈ tags` with `tags.name == normalized` and `tags.user_id == user_id`
  (an `IN` + `HAVING COUNT(DISTINCT)` form is also acceptable — but it must be AND semantics)
- `due` presets, evaluated against `today = q.today or date.today()` (UTC):
  `overdue` → `due_date < today AND completed IS false`; `today` → `due_date == today`;
  `week` → `today <= due_date <= today + 6 days`; `none` → `due_date IS NULL`; `any` → no clause
- `due_from` / `due_to` → inclusive bounds; the **router** raises 400 `conflicting_due_filters`
  when `due != "any"` and either bound is present
- `q` (search) → `or_(Todo.title.ilike(pattern), Todo.description.ilike(pattern))` with
  `pattern = f"%{escape_like(q)}%"`; `escape_like` escapes `%`, `_` and the escape char and the
  clause passes `escape="\\"` — a search for `100%` must not match everything (test it)
- ordering: `due_date` → `ORDER BY (due_date IS NULL) ASC, due_date <dir>`;
  `priority` → `CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END <dir>`
  (so `asc` = high first); `title` → `lower(title) <dir>`; `created_at`/`updated_at` → direct.
  **Always** append the tiebreak `created_at ASC, id ASC`.
- `limit`/`offset` applied to the row select only.
- Loader options on the row select: `selectinload(Todo.tags)`,
  `selectinload(Todo.subtasks).selectinload(Todo.tags)`.
Router sets `X-Total-Count` from the count select (already exposed by CORS in slice 1).

### B6. Write path
`SqlAlchemyTodoRepository.create/update` now honour every field, apply invariants I1–I4, sync
tags via `get_or_create_many` + association replacement (add/remove diff, not delete-all), and
always bump `updated_at`. All of it inside the single request transaction.

### B7. Tests
1. **write surface** — create with every field; create with only a title (defaults);
   `description` of 2001 chars → 422; 11 tags → 422; an invalid tag `"Bad!Tag"` → 422;
   `"  Home  "` and `"home"` collapse to one tag; unknown extra key → 422.
2. **partial PATCH** — each field individually; `{}` → 400 `empty_update`;
   `{"description": null}` clears; `{"due_date": null}` clears; `{"tags": []}` removes all;
   `{"completed": true}` still works (regression); an unknown key → 422; moving `list_id` moves
   subtasks too.
3. **subtasks** — create via both routes; depth violation → 400 `subtask_depth_exceeded`
   (nested create **and** `PATCH parent_id`); a subtask inherits `list_id`/`user_id`; deleting a
   parent deletes subtasks; completing a parent leaves subtasks untouched; a subtask never
   appears as a top-level row in `GET /api/todos`; a foreign `parent_id` → 404.
4. **filters** — a fixture seeding ~12 todos across two lists, three priorities, tags, due dates
   (past/today/+3d/+10d/null) and both completion states. Assert each parameter alone and these
   combinations: `status=active&priority=high`, `tag=home&tag=work` (AND — only todos with both),
   `due=overdue` excludes completed overdue todos, `due=week` boundary days (today and +6 inclusive,
   +7 excluded), `due=none`, `due_from`/`due_to` inclusivity, `due=today&due_from=…` → 400
   `conflicting_due_filters`, `q` case-insensitivity, `q` matching description only, `q="100%"`
   literalness, an unknown tag name → `[]` (not 404), a bad enum value → 422.
5. **sorting** — each `sort` in both directions, null `due_date` last in **both** directions,
   `priority=asc` yields high→medium→low, `title` sorts case-insensitively, ties break by
   `created_at ASC, id ASC` deterministically across repeated calls.
6. **pagination** — `limit`/`offset` slice correctly, `X-Total-Count` reports the **unpaginated**
   match count, `limit=0` and `limit=501` → 422.
7. **tags API** — `GET /api/tags` counts and ordering; rename happy/409/404; delete removes
   associations but not the todos; another user's tag id → 404; a tag orphaned by removal from
   its last todo still appears in `GET /api/tags`.
8. **isolation** — every new endpoint added to the slice-2 cross-user matrix.
9. **N+1 guard** — listing 20 todos with tags and subtasks issues a bounded number of SQL
   statements (assert ≤ 5 via a SQLAlchemy `before_cursor_execute` counter).

Commit stepwise: `backend: <step>`.

---

## FRONTEND — complexity **complex** (opus) · owns `frontend/**`

### F1. Files
Create: `src/components/TodoDetail.tsx`, `src/components/TodoEditForm.tsx`,
`src/components/PrioritySelect.tsx`, `src/components/DueDateField.tsx`,
`src/components/TagInput.tsx`, `src/components/TagChips.tsx`,
`src/components/SubtaskList.tsx`, `src/components/FilterBar.tsx`,
`src/components/SearchBox.tsx`, `src/components/SortSelect.tsx`,
`src/hooks/useDebouncedValue.ts`, `src/api/tags.ts`, tests alongside,
`e2e/enrichment.spec.ts`.
Modify: `src/App.tsx`, `src/components/AddTodoForm.tsx`, `src/components/TodoItem.tsx`,
`src/api/client.ts`, `src/api/types.ts`.

### F2. Controls (native HTML only — master D-F3)
- Priority: `<select>` with options `Low`, `Medium`, `High` (values `low|medium|high`).
- Due date: `<input type="date">`; the value is the raw `YYYY-MM-DD` string — **never** run it
  through `new Date()` for round-tripping (timezone shift bug).
- Tags: a text input plus a chip list. Enter or `,` commits the typed tag; Backspace on an empty
  input removes the last chip; each chip has a remove button named `Remove tag {name}`. The input
  normalizes to lowercase on commit and rejects invalid characters inline with
  `Tags can use letters, numbers, spaces, hyphens and underscores.`
- Search: debounced 300 ms (`useDebouncedValue`), minimum 1 character, cleared with a button
  named `Clear search`.

### F3. Exact UI copy (tests assert on these strings)

**Row (`TodoItem`) additions**
| Element | Copy / accessible name |
|---|---|
| Priority badge | `Low` / `Medium` / `High`, with `aria-label` `Priority: {level}`; **not colour-only** — always render the word |
| Due date | `Due {D MMM}` e.g. `Due 5 Sep`; today → `Due today`; overdue & active → `Overdue — {D MMM}` with `text-red-700` **and** the word `Overdue` |
| Tag chips | the tag name, `aria-label` `Tag: {name}` |
| Subtask progress | `{done}/{total} subtasks` (hidden when total = 0) |
| Expand/collapse subtasks | button named `Show subtasks of {title}` / `Hide subtasks of {title}`, `aria-expanded` set |
| Edit | button named `Edit {title}` |

**TodoDetail / TodoEditForm** (inline panel below the row; no modal)
| Element | Copy |
|---|---|
| Title field label | `Title` |
| Description label | `Description` (`<textarea>`, `maxLength={2000}`) |
| Priority label | `Priority` |
| Due date label | `Due date` · clear button `Clear due date` |
| Tags label | `Tags` |
| Save | `Save changes` (busy: `Saving…`) · Cancel: `Cancel` |
| Save failure | `Could not save changes. Please try again.` |
| Subtask section heading (`h3`) | `Subtasks` |
| Add-subtask input label | `New subtask title` (placeholder `Add a subtask`) · button `Add subtask` |
| Subtask checkbox names | `Mark {title} as completed` / `Mark {title} as active` (same pattern as top-level) |
| Delete subtask | `Delete subtask {title}` |
| Subtask failure | `Could not update the subtask. Please try again.` |

**FilterBar**
| Control | Label / options |
|---|---|
| Status | `Status` → `All`, `Active`, `Completed` |
| Priority | `Priority` → `Any priority`, `High`, `Medium`, `Low` (multi-select via checkboxes in a `<fieldset>` with legend `Priority`) |
| Due | `Due` → `Any time`, `Overdue`, `Today`, `Next 7 days`, `No due date` |
| Tag filter | `Filter by tag` — chips toggled on/off, each `aria-pressed`; accessible name `Filter by tag {name}` |
| Sort | `Sort by` → `Created`, `Updated`, `Due date`, `Priority`, `Title`; plus a direction button named `Sort ascending` / `Sort descending` (`aria-pressed`) |
| Search | label `Search todos`, placeholder `Search` |
| Reset | button `Clear filters` — visible only when a filter is active |
| Result summary (`aria-live="polite"`) | `Showing {n} of {total} todos` · when filters are active and n = 0: `No todos match your filters.` |

The empty-state copy for a genuinely empty list stays `No todos yet. Add your first one above.`
The active counter (`{n} items left`) stays and counts **top-level active todos in the current
result set**.

### F4. Behaviour
- Filters/sort/search live in `App` state (master D-F1 — no URL sync) and are serialized into the
  `GET /api/todos` query string. Every change refetches; overlapping requests are guarded by a
  request-sequence number so a slow earlier response cannot overwrite a newer one.
- `today` is sent as the browser's **local** date (`toLocaleDateString('en-CA')` → `YYYY-MM-DD`)
  on every list fetch, so `Overdue`/`Today` match the user's calendar.
- No optimistic updates (iteration-1 D6): every mutation awaits the response and replaces the item
  (or its parent) in state.
- Subtask mutations refetch/replace the **parent** todo so `subtasks` and the progress counter
  stay consistent.
- The busy affordance keeps the C7 rule: `aria-busy` + `aria-disabled`, never `disabled`.

### F5. Accessibility
Filter controls live in a `<form role="search">` (search box) and a labelled `<fieldset>` group;
the result summary is `aria-live="polite"`; expanding subtasks moves focus nowhere but sets
`aria-expanded`; the edit panel is `role="group"` with `aria-label="Edit {title}"` and focus moves
to the Title field on open and back to the `Edit {title}` button on close; priority and due state
are never conveyed by colour alone.

### F6. Vitest tests (mock the API modules)
1. A row renders priority word, due-date copy (future / today / overdue), tag chips and
   `2/3 subtasks`.
2. Expanding subtasks shows them; the toggle name and `aria-expanded` flip.
3. Edit panel: opening prefills all fields; saving calls `updateTodo` with **only changed** fields;
   cancel discards; a failure shows the exact copy in a `role="alert"`.
4. Tag input: Enter commits, `,` commits, Backspace removes the last chip, an invalid character
   shows the tag-format message and does not commit, duplicates are collapsed.
5. Due date: picking a date sends the raw string; `Clear due date` sends `null`.
6. Adding a subtask calls the subtask endpoint and updates the parent's progress counter.
7. Filter bar: each control updates the query string of the next `listTodos` call exactly
   (assert the serialized params, including `today`).
8. Search is debounced (only one call after several fast keystrokes) and `Clear search` resets it.
9. Sort control toggles `order` and updates `sort`.
10. `Showing {n} of {total} todos` reflects the response and `X-Total-Count`; the no-match copy
    appears when the filtered result is empty but the list is not.
11. A stale in-flight response does not overwrite a newer one (resolve the promises out of order).

### F7. Playwright — `e2e/enrichment.spec.ts` (happy path)
Sign in → create a todo with priority High, a due date of tomorrow and tags `home, errand` →
verify the row shows `High`, `Due …` and both chips → add two subtasks and complete one →
verify `1/2 subtasks` → filter by `Active` + tag `home` and confirm the todo is listed → search
for a unique fragment of the title and confirm exactly that row → clear filters → delete the todo.
Run with `PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright`.

Commit stepwise: `frontend: <step>`.

---

## DEVOPS
No infrastructure change. Optional (mechanical): extend the README API table with the new query
parameters and endpoints. If the orchestrator prefers, the planner's spec is enough documentation
and devops is not dispatched for this slice.

## QA — complexity **intermediate** (sonnet)
1. Both backend lanes, frontend unit + build, all E2E specs.
2. Filter/sort matrix by `curl` against the compose stack: every `due` preset around the
   today/+6/+7 boundaries, tag AND semantics, `q` with `%` and `_`, all five sorts in both
   directions with null due dates present, `X-Total-Count` vs `limit`.
3. Subtask depth: attempt every route to a two-level subtask and confirm 400
   `subtask_depth_exceeded`.
4. Cross-user checks for `/api/tags/{id}` and `/api/todos/{id}/subtasks` → 404.
5. Keyboard-only pass over the filter bar, tag input and edit panel; verify no control conveys
   meaning by colour alone (priority word and `Overdue` text present).
6. Confirm `PATCH {"completed": true}` still works (iteration-1 regression).

---

## Acceptance criteria
1. `POST /api/todos` accepts `description`, `priority`, `due_date`, `tags`, `parent_id` and
   `list_id`; omitted fields take the documented defaults.
2. `PATCH /api/todos/{id}` is partial: any subset updates only those fields, explicit `null`
   clears `description`/`due_date`/`parent_id`, `tags: []` clears tags, `{}` → 400 `empty_update`,
   and `{"completed": true}` still works.
3. Tags are auto-created per user on first use, normalized to lowercase, deduplicated, and
   validated against the documented pattern (invalid → 422).
4. `GET /api/tags` returns the user's tags with correct `todo_count`, ordered by name; rename and
   delete work with 409 `tag_name_taken` / 404 `tag_not_found`; deleting a tag does not delete todos.
5. `POST /api/todos/{id}/subtasks` creates a one-level subtask inheriting list and owner; every
   attempt to nest deeper returns 400 `subtask_depth_exceeded`.
6. `GET /api/todos` returns top-level todos only, each with its full `subtasks` array; deleting a
   parent removes its subtasks; completing a parent does not complete its subtasks.
7. All filters of master §6.3 work as specified, including tag **AND** semantics, the `due`
   presets against the caller-supplied `today`, inclusive `due_from`/`due_to`, and 400
   `conflicting_due_filters` when a preset is combined with a range.
8. Search is case-insensitive over title and description and treats `%` and `_` literally.
9. All five sorts work in both directions; null `due_date` sorts last in both; `priority` ascending
   is high → medium → low; ordering is stable across identical requests.
10. `X-Total-Count` reports the unpaginated match count and `limit`/`offset` page correctly.
11. Listing 20 enriched todos issues no more than 5 SQL statements (no N+1).
12. The UI can set and display priority, due date (with `Overdue`/`Today` states), description and
    tags, and add/complete/delete subtasks with a `{done}/{total} subtasks` counter.
13. The filter bar, sort control and search box drive the API exactly as specified, search is
    debounced, and `Showing {n} of {total} todos` is announced politely.
14. No status is conveyed by colour alone; every new control has a label or accessible name and is
    keyboard-operable with visible focus.
15. `uv run pytest` (both lanes), `npm test`, `npm run build` and all E2E specs are green.
