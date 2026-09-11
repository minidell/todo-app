"""Wire models for the ai-agent service (master §8.2, slice-4 spec B3).

Three families live here:

* **Requests** — what the backend proxy sends. ``extra="forbid"`` (master D-E3).
* **Raw models** — what the *language model* returned. ``extra="ignore"``: a 3B
  model happily adds keys, and that alone must not trigger a repair retry. They
  are permissive about values (``priority`` is a plain string, ``due_date`` a
  plain string) because clamping — not validation — is what fixes bad values.
* **Responses** — the sanitized result the backend proxy receives.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.sanitize import MAX_EDIT_OPS

MAX_TEXT_LENGTH = 4000
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 2000
MAX_KNOWN_TAGS = 50
MAX_SUMMARY_TODOS = 50
MAX_TAG_LENGTH = 40
MAX_PRIORITY_LENGTH = 10
MAX_LIST_NAME_LENGTH = 100

#: ``POST /ai/edit-todo`` (iteration 5, §2.2). The instruction is capped at the
#: *backend* edge by truncation (decision D-IT5-9), so a 501-character
#: instruction arriving here is a backend bug and earns a 422, not a clamp.
MAX_INSTRUCTION_LENGTH = 500
#: How many subtasks the positional snapshot may carry. The backend truncates
#: beyond this and reports ``subtasks_truncated`` to the user, so an instruction
#: about subtask 25 fails visibly rather than silently (D-IT5-3).
MAX_SNAPSHOT_SUBTASKS = 20
#: Tags on one todo, the backend's own ``MAX_TAGS_PER_TODO``.
MAX_TODO_TAGS = 10
#: Cap on surviving subtask operations. Enforced in ``sanitize`` — which is where
#: every "how much model output do we keep" bound lives — and re-exported under
#: the contract's name (§2.2) so there is one source of truth, not two literals.
MAX_EDIT_SUBTASK_OPS = MAX_EDIT_OPS

#: The closed vocabulary of subtask operations (§2.2). ``sanitize.EDIT_ACTIONS``
#: is the runtime copy the clamping layer filters against; they are pinned
#: together by a test, because a value that survives sanitization but is not in
#: this Literal would fail *response* validation — a 500 for a model quirk.
EditAction = Literal["add", "remove", "rename", "complete", "reopen"]

#: Collection bounds alone are not enough: 50 tags of 1 MB each still fits
#: ``max_length=50``. Every string that reaches a prompt is bounded per item.
#:
#: Bounded *and* charset-constrained, to the same rule as the backend's tag
#: validator (``backend/app/schemas/tags.py``) and this service's own
#: ``sanitize.TAG_RE``: lowercase alphanumerics, spaces, hyphens and
#: underscores, starting with a letter or digit, at most 30 characters.
#: ``known_tags`` is interpolated into a prompt line as ``KNOWN_TAGS=a, b, c``,
#: so without the pattern a tag containing a newline could forge a prompt line
#: of its own — the injection vector the note itself is JSON-encoded to close
#: (see ``prompts.parse_todo_user``). A tag is not free text, so it is rejected
#: at the boundary with a 422 rather than sanitized later.
#: The pattern's ``{0,29}`` also caps the length, making ``max_length`` below a
#: redundant-but-harmless second bound at the older, looser 40.
TagName = Annotated[
    str,
    StringConstraints(max_length=MAX_TAG_LENGTH, pattern=r"^[a-z0-9][a-z0-9 _-]{0,29}$"),
]
PriorityName = Annotated[str, StringConstraints(max_length=MAX_PRIORITY_LENGTH)]
ListName = Annotated[str, StringConstraints(max_length=MAX_LIST_NAME_LENGTH)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class ParseTodoRequest(_Request):
    """``POST /ai/parse-todo``."""

    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    today: date
    known_tags: list[TagName] = Field(
        default_factory=list, max_length=MAX_KNOWN_TAGS
    )


class SuggestSubtasksRequest(_Request):
    """``POST /ai/suggest-subtasks``."""

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    max_items: int = Field(default=5, ge=1, le=10)


class SuggestMetadataRequest(_Request):
    """``POST /ai/suggest-metadata``."""

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    known_tags: list[TagName] = Field(
        default_factory=list, max_length=MAX_KNOWN_TAGS
    )
    today: date


class SummaryTodo(_Request):
    """One todo in the daily-summary context assembled by the backend."""

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    priority: PriorityName = "medium"
    due_date: date | None = None
    completed: bool = False
    list_name: ListName | None = None


class DailySummaryRequest(_Request):
    """``POST /ai/daily-summary``."""

    today: date
    todos: list[SummaryTodo] = Field(
        default_factory=list, max_length=MAX_SUMMARY_TODOS
    )


class EditTodoSnapshotSubtask(_Request):
    """One subtask of the edited todo — **positional**, so it carries no id.

    The model is never shown a UUID (decision D-IT5-3): it works in 1-based
    positions and the backend maps those back to real ids. A 3B model reliably
    copies a single digit; it does not reliably copy 36 hex characters.
    """

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    completed: bool = False


class EditTodoSnapshot(_Request):
    """The todo as the backend currently has it (§2.2).

    Every string is bounded per item for the same reason ``known_tags`` is: a
    collection bound alone still admits 20 subtasks of 1 MB each, and all of
    this text is interpolated into a prompt.
    """

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    priority: PriorityName = "medium"
    due_date: date | None = None
    completed: bool = False
    tags: list[TagName] = Field(default_factory=list, max_length=MAX_TODO_TAGS)
    subtasks: list[EditTodoSnapshotSubtask] = Field(
        default_factory=list, max_length=MAX_SNAPSHOT_SUBTASKS
    )


class EditTodoRequest(_Request):
    """``POST /ai/edit-todo`` (iteration 5, §2.2)."""

    instruction: str = Field(min_length=1, max_length=MAX_INSTRUCTION_LENGTH)
    today: date
    known_tags: list[TagName] = Field(
        default_factory=list, max_length=MAX_KNOWN_TAGS
    )
    todo: EditTodoSnapshot


# --------------------------------------------------------------------------- #
# Raw model output
# --------------------------------------------------------------------------- #


class _RawModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RawSubtask(_RawModel):
    title: str


class RawTodoDraft(_RawModel):
    title: str
    description: str | None = None
    priority: str | None = None
    due_date: str | None = None
    tags: list[str] = Field(default_factory=list)
    subtasks: list[RawSubtask] = Field(default_factory=list)


class RawSubtaskList(_RawModel):
    subtasks: list[RawSubtask] = Field(default_factory=list)


class RawMetadata(_RawModel):
    priority: str | None = None
    tags: list[str] = Field(default_factory=list)


class RawSummary(_RawModel):
    summary: str


class RawEditOp(_RawModel):
    """One subtask operation as the model wrote it.

    Every field is defaulted, so a missing key is a *clamping* problem rather
    than a repair-retry trigger. ``index`` is deliberately untyped: with
    ``int | None`` a model that answers ``"two"`` would fail validation and burn
    the single repair turn for the whole proposal, when the right answer is to
    drop that one operation and keep the rest (spec §3.1 A5). ``sanitize``
    accepts an ``int`` or a numeric string and bounds-checks it against the
    snapshot; everything else becomes ``None`` and the op is dropped.
    """

    action: str = ""
    index: Any = None
    title: str | None = None


class RawEditProposal(_RawModel):
    """The whole change proposal as the model wrote it (§2.2).

    ``completed`` is untyped for the reason spelled out in ``sanitize``: only a
    real JSON boolean may become a change, and anything else must mean "no
    change" rather than a validation failure.
    """

    title: str | None = None
    description: str | None = None
    priority: str | None = None
    due_date: str | None = None
    completed: Any = None
    tags_add: list[str] = Field(default_factory=list)
    tags_remove: list[str] = Field(default_factory=list)
    clear: list[str] = Field(default_factory=list)
    subtasks: list[RawEditOp] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class SubtaskDraft(BaseModel):
    title: str


class ParseTodoResponse(BaseModel):
    title: str
    description: str | None = None
    priority: str = "medium"
    due_date: date | None = None
    tags: list[str] = Field(default_factory=list)
    subtasks: list[SubtaskDraft] = Field(default_factory=list)


class SuggestSubtasksResponse(BaseModel):
    subtasks: list[SubtaskDraft] = Field(default_factory=list)


class SuggestMetadataResponse(BaseModel):
    priority: str = "medium"
    tags: list[str] = Field(default_factory=list)


class DailySummaryResponse(BaseModel):
    summary: str


class EditOp(BaseModel):
    """One sanitized subtask operation (§2.2).

    ``index`` is 1-based into the snapshot the caller sent and is ``None`` for
    ``add``; ``title`` is set for ``add`` and ``rename`` and ``None`` otherwise.
    """

    action: EditAction
    index: int | None = None
    title: str | None = None


class EditTodoResponse(BaseModel):
    """A sanitized, positional change proposal (§2.2).

    ``null`` means "leave this field alone", which is why clearing needs the two
    explicit ``clear_*`` flags: the absence of a value cannot also mean "remove
    the value". Nothing here is a decision — the backend resolves this proposal
    against the real todo, drops no-ops and maps indices to ids before any of it
    is shown to a user, and nothing is written until the user confirms (D-AI1).
    """

    title: str | None = None
    description: str | None = None
    clear_description: bool = False
    priority: str | None = None
    due_date: date | None = None
    clear_due_date: bool = False
    completed: bool | None = None
    tags_add: list[str] = Field(default_factory=list)
    tags_remove: list[str] = Field(default_factory=list)
    subtasks: list[EditOp] = Field(default_factory=list)


class PingResponse(BaseModel):
    """``GET /ai/ping`` — an authenticated liveness echo (master §8.2, IT3-2).

    It carries no information beyond "you reached me and your token matched":
    the *status code* is the whole answer. The backend uses it to tell
    "ai-agent is unreachable" apart from "ai-agent is up but we disagree about
    ``AI_AGENT_TOKEN``", which unauthenticated ``/health`` can never reveal.
    """

    status: str = "ok"


class HealthResponse(BaseModel):
    """``GET /health`` — never fails, only reports (slice-4 spec B4)."""

    model_config = ConfigDict(protected_namespaces=())

    status: str = "ok"
    ollama: str
    model: str
    model_present: bool
