"""Wire models for ``/api/ai/*`` (master §6.6).

Two boundaries meet here, and they are not the same boundary.

**Inbound** (the browser) is the ordinary strict edge: ``extra="forbid"``, like
every other request model. One deliberate exception to strictness — values that
the ai-agent contract bounds (``text`` length, ``max_items`` range) are
*clamped* rather than rejected. A 422 for a note that is 4001 characters long
would be a dead end for the user, whose only recovery is to guess which limit
they hit; truncating gives them a draft to edit, which is the whole point of a
draft (D-AI1).

**Outbound** (ai-agent) is the untrusted edge. ai-agent already sanitizes what
the model produced, but a draft that reaches the browser must be one the user
can submit straight back to ``POST /api/todos`` — so it is re-coerced against
*this* application's rules here. Two services agreeing on a contract is not the
same as this service being safe when the other one is wrong.
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Literal, Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models import Priority, Todo
from app.schemas.tags import normalize_tag, validate_tag
from app.schemas.todos import MAX_DESCRIPTION_LENGTH, MAX_TAGS_PER_TODO

#: ai-agent rejects a longer note with a 422 (its README, "request limits"), and
#: a 422 from ai-agent is a backend bug, not something to show a user. Longer
#: input is truncated here instead.
MAX_PARSE_TEXT_LENGTH = 4000

#: A todo title is ``String(200)`` (§3.1); a draft the user cannot save is not
#: a draft.
MAX_TITLE_LENGTH = 200

#: ai-agent's ``known_tags`` bound.
MAX_KNOWN_TAGS = 50

#: Subtask suggestions per call (§6.6 / ai-agent's ``max_items``).
MIN_SUBTASK_SUGGESTIONS = 1
MAX_SUBTASK_SUGGESTIONS = 10
DEFAULT_SUBTASK_SUGGESTIONS = 5

#: How many todos the daily summary may describe. Matching ai-agent's own cap:
#: more context than this makes a 3B model produce a list, not a briefing.
MAX_SUMMARY_TODOS = 50

#: The edit instruction cap (decision D-IT5-9). ai-agent validates ``1..500``
#: and answers 422 above it — and a 422 from ai-agent is a *backend* bug, not
#: something to show a user — so an over-long instruction is truncated here,
#: exactly like :attr:`ParseTodoRequest.text`.
MAX_INSTRUCTION_LENGTH = 500

#: How many subtasks the edit snapshot may carry (decision D-IT5-3). Twenty
#: positional entries still leave the 3B model room to answer inside its token
#: budget; past that the response says so (``context.subtasks_truncated``) so
#: an instruction about subtask 25 fails visibly rather than silently.
MAX_SNAPSHOT_SUBTASKS = 20

#: How far from the caller's own ``today`` a proposed due date may fall. The
#: same window ai-agent's ``clean_due_date`` uses (five leap-safe years), so a
#: date that survives one side is not silently rejected by the other.
DUE_DATE_HORIZON = timedelta(days=5 * 366)

#: How many subtask operations one change set may carry. A change set is a
#: checklist a human has to read before pressing Apply; beyond ten it stops
#: being reviewable, which is the only reason this endpoint is safe.
MAX_EDIT_SUBTASK_OPS = 10


#: Whitespace that survives :func:`_strip_control_chars`. Both are ``Cc``
#: characters, and both are ordinary text in a multi-line instruction.
_KEPT_CONTROL_CHARS = frozenset({"\n", "\t"})


def _strip_control_chars(value: str) -> str:
    """Drop Unicode control (``Cc``) and format (``Cf``) characters.

    The same rule as ai-agent's ``strip_control_chars`` with
    ``keep_newlines=True``, applied on this side too because ai-agent
    sanitizing its *own* input is not a reason for the backend to forward
    something it would not accept anywhere else.
    """
    return "".join(
        char
        for char in value
        if char in _KEPT_CONTROL_CHARS
        or unicodedata.category(char) not in ("Cc", "Cf")
    )


class _AiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class ParseTodoRequest(_AiRequest):
    """``POST /api/ai/parse-todo`` — free text becomes a draft todo.

    ``known_tags`` is deliberately **not** a field: the caller does not get to
    say what its own tag vocabulary is. The backend reads it from the database,
    which is the whole reason the proxy exists (D-AI2).
    """

    text: str = Field(min_length=1)
    #: The caller's *local* date. The server's date is the wrong one for a user
    #: in another timezone, and "tomorrow" has to mean their tomorrow.
    today: date

    @field_validator("text", mode="before")
    @classmethod
    def _strip_and_truncate(cls, value: Any) -> Any:
        """Normalize *before* ``min_length`` runs, not after.

        As an ``after`` validator this stripped a whitespace-only note down to
        ``""`` once ``min_length=1`` had already accepted it, so ``"   "`` was
        forwarded to ai-agent, rejected there by its own ``min_length=1``, and
        surfaced to the user as 503 "AI is unavailable" — a misleading answer
        to a plainly invalid request, plus a wasted round trip. Stripping first
        makes it the 422 naming ``text`` that it always was.

        (It does not save the caller a rate-limit slot: route-level
        dependencies run before body validation, so the limiter has already
        counted the request either way. ``test_ai_api`` pins that.)
        """
        if isinstance(value, str):
            return value.strip()[:MAX_PARSE_TEXT_LENGTH]
        return value


class SuggestSubtasksRequest(_AiRequest):
    """``POST /api/ai/suggest-subtasks`` — split an existing todo into steps."""

    #: A plain ``str``, resolved with ``parse_uuid`` in the router, so a
    #: malformed id is the same 404 ``todo_not_found`` as an unknown one
    #: (decision D1 + D-E2). A 422 here would tell a caller that its id was
    #: *well-formed but not yours*, which is exactly the distinction D-E2 exists
    #: to erase.
    todo_id: str
    max_items: int = DEFAULT_SUBTASK_SUGGESTIONS

    @field_validator("max_items")
    @classmethod
    def _clamp(cls, value: int) -> int:
        return max(MIN_SUBTASK_SUGGESTIONS, min(MAX_SUBTASK_SUGGESTIONS, value))


class SuggestMetadataRequest(_AiRequest):
    """``POST /api/ai/suggest-metadata`` — priority and tags for a todo."""

    todo_id: str
    today: date | None = None


class DailySummaryRequest(_AiRequest):
    """``POST /api/ai/daily-summary`` — a briefing over the open todos.

    The frontend sends an explicit ``null`` for "across all lists"; both the
    absent key and the explicit null mean the same thing.
    """

    #: A plain ``str``, resolved with ``parse_uuid`` in the router, exactly like
    #: ``SuggestSubtasksRequest.todo_id``: an unknown, malformed or *foreign*
    #: list is all one answer, 404 ``list_not_found`` (D1 + D-E2). Typed as
    #: ``UUID`` it was the one id in the API where a malformed value produced a
    #: different status from a well-formed one that is not yours — which is the
    #: distinction D-E2 exists to erase.
    list_id: str | None = None
    today: date


class EditTodoRequest(_AiRequest):
    """``POST /api/ai/edit-todo`` — one free-text instruction, one todo.

    The instruction is the first AI *input* in this API that is neither the
    user's own note nor content the backend assembled, so it is the one field
    that has to be bounded before it is forwarded: stripped, then truncated to
    :data:`MAX_INSTRUCTION_LENGTH`.
    """

    #: A plain ``str``, resolved with ``parse_uuid`` in the router, so unknown,
    #: malformed, *foreign* and "actually a subtask" are one answer — 404
    #: ``todo_not_found`` (D1 + D-E2 + D-IT5-6).
    todo_id: str
    instruction: str = Field(min_length=1)
    #: The caller's *local* date, required: "tomorrow" has to mean theirs, and
    #: the server's date is the wrong one for a user in another timezone.
    today: date

    @field_validator("instruction", mode="before")
    @classmethod
    def _strip_and_truncate(cls, value: Any) -> Any:
        """Normalize *before* ``min_length`` runs (as ``ParseTodoRequest.text``).

        As an ``after`` validator this would strip a whitespace-only
        instruction down to ``""`` once ``min_length=1`` had already accepted
        it, forward it, and surface ai-agent's own 422 to the user as 503 "AI
        is unavailable" — a misleading answer to a plainly invalid request.

        Control characters go first, before the length is measured: an
        instruction is the one string in this API that a *browser* writes and a
        *model* reads, and NUL, ANSI escapes or a bidi override (U+202E) in it
        are never an edit request — they are an attempt to make the prompt
        render differently from what it says. Newlines and tabs survive: a user
        may reasonably paste a two-line instruction, and the prompt builder
        JSON-encodes it anyway.
        """
        if isinstance(value, str):
            return _strip_control_chars(value).strip()[:MAX_INSTRUCTION_LENGTH]
        return value


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


def _clean_title(value: Any) -> str | None:
    """A model-produced title, or ``None`` if there is nothing usable in it."""
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())[:MAX_TITLE_LENGTH].strip()
    return collapsed or None


def _clean_tags(value: Any) -> list[str]:
    """Keep the tags this application would accept, drop the rest.

    Dropping rather than repairing: a tag the user did not ask for is noise
    they have to notice and remove, and a draft's tags are pre-checked
    checkboxes. ``validate_tag`` is the same function ``POST /api/todos`` uses,
    so anything that survives here is guaranteed to be submittable.
    """
    if not isinstance(value, list):
        return []
    kept: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            tag = validate_tag(item)
        except ValueError:
            continue
        if tag not in kept:
            kept.append(tag)
        if len(kept) == MAX_TAGS_PER_TODO:
            break
    return kept


def _clean_priority(value: Any) -> Priority:
    """Master §8.3: an unknown priority is ``medium``, never an error."""
    if isinstance(value, Priority):
        return value
    if isinstance(value, str):
        try:
            return Priority(value.strip().lower())
        except ValueError:
            return Priority.MEDIUM
    return Priority.MEDIUM


def _clean_due_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _clean_subtasks(value: Any, limit: int) -> list["SubtaskDraft"]:
    if not isinstance(value, list):
        return []
    titles: list[str] = []
    for item in value:
        raw = item.get("title") if isinstance(item, dict) else item
        title = _clean_title(raw)
        if title is None:
            continue
        if normalize_tag(title) in {normalize_tag(seen) for seen in titles}:
            # The same step twice is a model artefact, not two steps.
            continue
        titles.append(title)
        if len(titles) == limit:
            break
    return [SubtaskDraft(title=title) for title in titles]


class SubtaskDraft(BaseModel):
    """One suggested step. Titles only — the user confirms before anything
    is written (D-AI1), and the ordinary subtask endpoint does the writing."""

    title: str


class TodoDraft(BaseModel):
    """A suggested todo, shaped so the frontend can prefill its normal form."""

    title: str
    description: str | None = None
    priority: Priority = Priority.MEDIUM
    due_date: date | None = None
    tags: list[str] = Field(default_factory=list)
    subtasks: list[SubtaskDraft] = Field(default_factory=list)

    @classmethod
    def from_agent(cls, payload: dict[str, Any]) -> "TodoDraft":
        """Build a draft from ai-agent's response, trusting none of it."""
        description = payload.get("description")
        if isinstance(description, str):
            description = description.strip()[:MAX_DESCRIPTION_LENGTH] or None
        else:
            description = None

        return cls(
            # An empty title is possible in principle and useless in practice;
            # the user gets an empty Title field to fill in rather than a 500.
            title=_clean_title(payload.get("title")) or "",
            description=description,
            priority=_clean_priority(payload.get("priority")),
            due_date=_clean_due_date(payload.get("due_date")),
            tags=_clean_tags(payload.get("tags")),
            subtasks=_clean_subtasks(
                payload.get("subtasks"), MAX_SUBTASK_SUGGESTIONS
            ),
        )


class ParseTodoResponse(BaseModel):
    """``{"draft": {...}}`` — the wrapper is the contract (§6.6).

    It leaves room for the response to grow (a confidence score, a "why")
    without changing the shape of the draft itself.
    """

    draft: TodoDraft


class SuggestSubtasksResponse(BaseModel):
    subtasks: list[SubtaskDraft] = Field(default_factory=list)

    @classmethod
    def from_agent(cls, payload: dict[str, Any], limit: int) -> "SuggestSubtasksResponse":
        return cls(subtasks=_clean_subtasks(payload.get("subtasks"), limit))


class SuggestMetadataResponse(BaseModel):
    priority: Priority = Priority.MEDIUM
    tags: list[str] = Field(default_factory=list)

    @classmethod
    def from_agent(cls, payload: dict[str, Any]) -> "SuggestMetadataResponse":
        return cls(
            priority=_clean_priority(payload.get("priority")),
            tags=_clean_tags(payload.get("tags")),
        )


class DailySummaryResponse(BaseModel):
    summary: str
    #: How many todos the summary is actually based on — the frontend prints
    #: it ("Based on 5 open todos") and uses 0 to show its empty-state copy.
    todo_count: int
    generated_at: datetime


# --------------------------------------------------------------------------- #
# edit-todo: the change set (iteration 5 §2.1)
# --------------------------------------------------------------------------- #
#
# ai-agent answers with a *positional, sanitized* proposal: what it thinks
# should change, with subtasks named by 1-based position because the model
# never sees an identifier (D-IT5-3). Turning that into a change set is this
# module's job and it is not a rename of the upstream payload — it is the step
# where the proposal meets the todo it is about:
#
#   * positions become the real subtask ids the frontend will call,
#   * anything that would be a no-op against the real row is dropped, because a
#     checkbox that changes nothing is noise the user has to read and reject,
#   * every surviving value is re-clamped against *this* application's rules,
#     so every change is one the ordinary endpoints will accept.
#
# Two services agreeing on a contract is not the same as this service being
# safe when the other one is wrong.


def _clean_optional_priority(value: Any) -> Priority | None:
    """A proposed priority, or ``None`` for "no change".

    Deliberately **not** :func:`_clean_priority`, which coerces anything
    unrecognised to ``medium`` because a *draft* always needs a priority. Here
    the value is a proposed *change*: coercing would invent a priority change
    out of model garbage and put a pre-checked box in front of the user that
    nothing in their instruction asked for.
    """
    if isinstance(value, Priority):
        return value
    if isinstance(value, str):
        try:
            return Priority(value.strip().lower())
        except ValueError:
            return None
    return None


def _clean_bounded_due_date(value: Any, *, today: date) -> date | None:
    """A proposed due date, or ``None`` when it is garbage or absurd.

    Same horizon as ai-agent's ``clean_due_date``, re-applied here rather than
    trusted: ``9999-12-31`` is not a date the user asked for, it is a model (or
    a buggy upstream) mis-resolving "next year", and it would arrive as a
    pre-checked box that quietly rewrites a real deadline. Measured against the
    *caller's* today, which is the only "now" this request knows.
    """
    parsed = _clean_due_date(value)
    if parsed is None or abs(parsed - today) > DUE_DATE_HORIZON:
        return None
    return parsed


def _clean_description(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()[:MAX_DESCRIPTION_LENGTH] or None


def _clean_tag_names(value: Any) -> list[str]:
    """Normalize the names this application would accept, drop the rest.

    ``validate_tag`` is the same function ``POST /api/todos`` uses, so anything
    surviving here is guaranteed to be submittable by the client that applies
    the change set.
    """
    if not isinstance(value, list):
        return []
    kept: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            name = validate_tag(item)
        except ValueError:
            continue
        if name not in kept:
            kept.append(name)
    return kept


class _WireChange(BaseModel):
    """One before/after pair as it goes onto the wire.

    The wire key is ``from`` (§2.1), which is a Python keyword, so the field is
    ``previous`` in Python and pinned to the wire by an alias.
    ``serialize_by_alias`` makes a bare ``model_dump()`` agree with what
    FastAPI emits — otherwise anything dumping a change by hand would ship
    ``previous`` to a frontend reading ``from`` and render an empty diff (risk
    R7). ``tests/test_schemas.py`` pins the key.
    """

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class TitleChange(_WireChange):
    previous: str = Field(alias="from")
    to: str


class TextChange(_WireChange):
    """A nullable text field. ``to: null`` means *clear it*, not "unchanged" —
    the entry's presence is the signal that something changes at all."""

    previous: str | None = Field(alias="from")
    to: str | None


class PriorityChange(_WireChange):
    previous: Priority = Field(alias="from")
    to: Priority


class DueDateChange(_WireChange):
    previous: date | None = Field(alias="from")
    to: date | None


class CompletedChange(_WireChange):
    previous: bool = Field(alias="from")
    to: bool


class TagsChange(_WireChange):
    """The whole final list *and* the difference.

    ``to`` is what the client PATCHes (de-duplicated, normalized, ≤10);
    ``added``/``removed`` are what it renders, one deselectable row per name.
    Deriving one from the other in the client would be a second implementation
    of a rule that already lives here.
    """

    previous: list[str] = Field(alias="from")
    to: list[str]
    added: list[str]
    removed: list[str]


EditAction = Literal["add", "remove", "rename", "complete", "reopen"]

#: The order the client must apply the operations in, and therefore the order
#: they are returned in. Renames first (they cannot fail on a row the later ops
#: touch), then completion toggles, then removals, and additions last — so a
#: partial apply never deletes a subtask it has not yet re-created, and a
#: retry-free failure (D-IT5-7) leaves the todo in the most recognisable state.
_ACTION_ORDER: dict[str, int] = {
    "rename": 0,
    "complete": 1,
    "reopen": 1,
    "remove": 2,
    "add": 3,
}


class EditOpResponse(BaseModel):
    """One subtask operation, resolved to a real id the frontend can call."""

    action: EditAction
    #: ``None`` only for ``add`` — there is nothing to point at yet.
    id: UUID | None = None
    #: The new title (``add``/``rename``); ``None`` for the other three.
    title: str | None = None
    #: The targeted subtask's *current* title, so the panel can render a
    #: readable label without trusting its own possibly-stale copy of the row.
    from_title: str | None = None


class EditTodoChangeSet(BaseModel):
    """A field entry is ``null`` **iff** that field does not change."""

    title: TitleChange | None = None
    description: TextChange | None = None
    priority: PriorityChange | None = None
    due_date: DueDateChange | None = None
    completed: CompletedChange | None = None
    tags: TagsChange | None = None
    subtasks: list[EditOpResponse] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.subtasks and not any(
            getattr(self, name) is not None
            for name in ("title", "description", "priority", "due_date", "completed", "tags")
        )


class EditTodoContext(BaseModel):
    """What the model was *not* told, when that changes how to read the answer."""

    subtasks_truncated: bool = False


class EditTodoResponse(BaseModel):
    todo_id: UUID
    #: Redundant with ``change_set`` on purpose, like
    #: :attr:`DailySummaryResponse.todo_count`: the client branches on one
    #: boolean instead of re-deriving the rule, and the server can test it.
    empty: bool
    context: EditTodoContext
    change_set: EditTodoChangeSet

    @classmethod
    def from_agent(
        cls,
        payload: dict[str, Any],
        *,
        todo: Todo,
        subtasks: Sequence[Todo],
        truncated: bool,
        today: date,
    ) -> "EditTodoResponse":
        """Resolve a positional proposal against the todo it is about.

        ``subtasks`` must be exactly the ordered slice that was sent upstream:
        the proposal's ``index`` values are 1-based positions into *that* list,
        and mapping them against anything else would point an operation at the
        wrong row. ``today`` is the caller's own local date — the same one the
        proposal's relative dates were resolved from, and the only anchor a
        proposed due date can sensibly be bounded against.
        """
        change_set = EditTodoChangeSet(
            title=_resolve_title(payload.get("title"), todo),
            description=_resolve_description(payload, todo),
            priority=_resolve_priority(payload.get("priority"), todo),
            due_date=_resolve_due_date(payload, todo, today),
            completed=_resolve_completed(payload.get("completed"), todo),
            tags=_resolve_tags(payload, todo),
            subtasks=_resolve_subtasks(payload.get("subtasks"), subtasks),
        )
        return cls(
            todo_id=todo.id,
            empty=change_set.is_empty(),
            context=EditTodoContext(subtasks_truncated=truncated),
            change_set=change_set,
        )


def _resolve_title(value: Any, todo: Todo) -> TitleChange | None:
    title = _clean_title(value)
    if title is None or title == todo.title:
        return None
    return TitleChange(previous=todo.title, to=title)


def _resolve_description(payload: dict[str, Any], todo: Todo) -> TextChange | None:
    """A value wins over a clear: a model asking for both contradicts itself,
    and the non-destructive reading is the one to keep."""
    description = _clean_description(payload.get("description"))
    if description is None:
        if payload.get("clear_description") is not True:
            return None
    if description == todo.description:
        return None
    return TextChange(previous=todo.description, to=description)


def _resolve_priority(value: Any, todo: Todo) -> PriorityChange | None:
    priority = _clean_optional_priority(value)
    if priority is None or priority == todo.priority:
        return None
    return PriorityChange(previous=todo.priority, to=priority)


def _resolve_due_date(
    payload: dict[str, Any], todo: Todo, today: date
) -> DueDateChange | None:
    due_date = _clean_bounded_due_date(payload.get("due_date"), today=today)
    if due_date is None:
        if payload.get("clear_due_date") is not True:
            return None
    if due_date == todo.due_date:
        return None
    return DueDateChange(previous=todo.due_date, to=due_date)


def _resolve_completed(value: Any, todo: Todo) -> CompletedChange | None:
    """Only a real ``bool``. Anything else is "no change": a truthy string is
    not a user asking to close their todo."""
    if not isinstance(value, bool) or value == todo.completed:
        return None
    return CompletedChange(previous=todo.completed, to=value)


def _resolve_tags(payload: dict[str, Any], todo: Todo) -> TagsChange | None:
    current = [tag.name for tag in todo.tags]
    added = _clean_tag_names(payload.get("tags_add"))
    removed = _clean_tag_names(payload.get("tags_remove"))

    # Add *and* remove the same name is the model contradicting itself; the
    # only reading that changes nothing is to honour neither.
    contradictory = set(added) & set(removed)
    added = [name for name in added if name not in contradictory]
    removed = [name for name in removed if name not in contradictory]

    kept = [name for name in current if name not in removed]
    fresh = [name for name in added if name not in kept]
    # The cap is applied **after** de-duplication and it drops the *added*
    # names first (risk R6): an over-eager suggestion must never silently
    # delete a tag the user put there themselves.
    room = max(0, MAX_TAGS_PER_TODO - len(kept))
    final = kept[:MAX_TAGS_PER_TODO] + fresh[:room]

    if set(final) == set(current):
        return None
    return TagsChange(
        previous=current,
        to=final,
        added=[name for name in final if name not in current],
        removed=[name for name in current if name not in final],
    )


def _target_subtask(index: Any, subtasks: Sequence[Todo]) -> Todo | None:
    """The subtask a 1-based position names, or ``None`` if it names nothing.

    ai-agent already dropped out-of-range indices against the snapshot length
    it received; this is the second half of that defence in depth (risk R3) and
    the only one checking against the *real* list. ``bool`` is excluded
    explicitly because ``True == 1`` in Python, and ``{"index": true}`` is not
    a position.
    """
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    if not 1 <= index <= len(subtasks):
        return None
    return subtasks[index - 1]


def _one_operation_per_subtask(ops: list[EditOpResponse]) -> list[EditOpResponse]:
    """At most one operation per id, with ``remove`` winning (§2.1).

    Renaming a subtask that is about to be deleted is at best wasted work and
    at worst a 404 mid-apply; a subtask named twice is a model artefact, not
    two intentions.
    """
    doomed = {op.id for op in ops if op.action == "remove"}
    kept: list[EditOpResponse] = []
    seen: set[UUID | None] = set()
    for op in ops:
        if op.action == "add":
            kept.append(op)
            continue
        if op.id in seen or (op.action != "remove" and op.id in doomed):
            continue
        seen.add(op.id)
        kept.append(op)
    return kept


def _resolve_subtasks(value: Any, subtasks: Sequence[Todo]) -> list[EditOpResponse]:
    if not isinstance(value, list):
        return []

    ops: list[EditOpResponse] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        # ``isinstance`` first: a membership test on an unhashable value —
        # ``{"action": ["add"]}`` from a confused model — raises ``TypeError``,
        # and a 500 is exactly the answer this whole resolution step exists to
        # make impossible.
        if not isinstance(action, str) or action not in _ACTION_ORDER:
            continue
        title = _clean_title(item.get("title"))

        if action == "add":
            # An add carries no position, whatever the model attached to it.
            if title is not None:
                ops.append(EditOpResponse(action="add", title=title))
            continue

        target = _target_subtask(item.get("index"), subtasks)
        if target is None:
            continue
        # No-ops are dropped here rather than shown and ignored: "complete a
        # subtask that is already done" reads to the user as a change, and
        # applying it would issue a PATCH that changes nothing.
        if action == "rename" and (title is None or title == target.title):
            continue
        if action == "complete" and target.completed:
            continue
        if action == "reopen" and not target.completed:
            continue

        ops.append(
            EditOpResponse(
                action=action,
                id=target.id,
                title=title if action == "rename" else None,
                from_title=target.title,
            )
        )

    capped = _one_operation_per_subtask(ops)[:MAX_EDIT_SUBTASK_OPS]
    # ``sorted`` is stable, so operations of equal rank keep the order the
    # model proposed them in.
    return sorted(capped, key=lambda op: _ACTION_ORDER[op.action])


#: Why AI is not available (master §5.1, iteration-4 delta). A closed machine
#: enum, never prose and never a quote from an ai-agent response body: the
#: client branches on it to choose its copy, and an unrecognised value must be
#: treated as ``None``, so adding one later is not a breaking change.
AiUnavailableReason = Literal[
    "disabled", "unreachable", "auth_failed", "model_unavailable"
]


class AiStatusResponse(BaseModel):
    """``GET /api/ai/status`` — always 200, even when everything is down."""

    model_config = ConfigDict(protected_namespaces=())

    enabled: bool
    available: bool
    model: str | None = None
    #: ``null`` **iff** :attr:`available` is true. The invariant is enforced
    #: here rather than trusted of each call site, because the frontend uses it
    #: to decide whether to render a banner at all.
    reason: AiUnavailableReason | None = None

    @model_validator(mode="after")
    def _reason_matches_availability(self) -> "AiStatusResponse":
        if self.available and self.reason is not None:
            raise ValueError("reason must be null when available is true")
        if not self.available and self.reason is None:
            raise ValueError("an unavailable status must say why")
        return self
