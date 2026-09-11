"""``/api/ai/*`` — the proxy in front of the private ai-agent (master §6.6, §8).

Three rules hold for every endpoint in this module.

**Nothing here writes to the database (decision D-AI1).** Every response is a
*draft*. The user reviews it and the frontend then calls the ordinary
``POST /api/todos`` / ``POST /api/todos/{id}/subtasks`` / ``PATCH /api/todos/{id}``.
An LLM is not an unreviewed write path, and the rule also makes every endpoint
here idempotent and trivially testable. ``tests/test_ai_api.py`` asserts the
database is byte-identical after each call.

**Prompt context is assembled here, never in ai-agent.** ai-agent has no
database and no idea who is calling; the backend is the only component allowed
to read a user's todos, and it only ever reads *that* user's rows — the same
owner-scoped repositories every other route uses.

**Every ai-agent failure becomes one of four documented answers**: 503
``ai_disabled``, 503 ``ai_unavailable``, 504 ``ai_timeout``, 429
``rate_limited``. The user never sees an ai-agent error body, a prompt, or a
model name they did not ask for.
"""

import logging
from datetime import date, datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.ai_client import AiAgentClient
from app.config import Settings
from app.db.models import Priority, Todo, User
from app.deps import (
    enforce_ai_rate_limit,
    get_ai_client,
    get_app_settings,
    get_current_user,
    get_list_repository,
    get_tag_repository,
    get_todo_repository,
)
from app.errors import AiDisabled, ListNotFound, TodoNotFound
from app.query import PRIORITY_RANK
from app.repositories.lists import SqlAlchemyListRepository
from app.repositories.protocols import TodoQuery, TodoRepository
from app.repositories.tags import SqlAlchemyTagRepository
from app.routers.utils import parse_uuid
from app.schemas.ai import (
    MAX_KNOWN_TAGS,
    MAX_SNAPSHOT_SUBTASKS,
    MAX_SUMMARY_TODOS,
    AiStatusResponse,
    DailySummaryRequest,
    DailySummaryResponse,
    EditTodoRequest,
    EditTodoResponse,
    ParseTodoRequest,
    ParseTodoResponse,
    SuggestMetadataRequest,
    SuggestMetadataResponse,
    SuggestSubtasksRequest,
    SuggestSubtasksResponse,
    TodoDraft,
)
from app.schemas.common import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])

CurrentUser = Annotated[User, Depends(get_current_user)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]
AiClient = Annotated[AiAgentClient, Depends(get_ai_client)]
Todos = Annotated[TodoRepository, Depends(get_todo_repository)]
Tags = Annotated[SqlAlchemyTagRepository, Depends(get_tag_repository)]
Lists = Annotated[SqlAlchemyListRepository, Depends(get_list_repository)]

AI_ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Not authenticated"},
    429: {"model": ErrorResponse, "description": "Too many AI requests"},
    503: {"model": ErrorResponse, "description": "AI disabled or unavailable"},
    504: {"model": ErrorResponse, "description": "The AI service timed out"},
}
TODO_NOT_FOUND_RESPONSE = {
    404: {"model": ErrorResponse, "description": "Todo not found"}
}


def require_ai_enabled(settings: AppSettings) -> None:
    """Refuse every AI endpoint except ``/status`` when AI is switched off."""
    if not settings.AI_ENABLED:
        raise AiDisabled()


#: The dependency stack every AI POST shares, **in the order they must run**.
#:
#: Authentication first: with ``AI_ENABLED=false`` an anonymous caller used to
#: get 503 ``ai_disabled``, which answers a question it was never entitled to
#: ask — whether this deployment runs AI at all — and makes the endpoint behave
#: differently from every other authenticated route in the API. Everyone
#: unauthenticated now gets the same 401 regardless of configuration.
#:
#: Then the master switch, then the rate limit: a deployment with AI off must
#: not spend a caller's 20-per-5-minutes budget on an endpoint that was never
#: going to reach a model.
AI_GUARDS = [
    Depends(get_current_user),
    Depends(require_ai_enabled),
    Depends(enforce_ai_rate_limit),
]


async def _known_tags(tags: Tags, user_id: UUID) -> list[str]:
    """The caller's own tag vocabulary, capped at ai-agent's ``known_tags`` limit.

    Truncation is by usage rather than alphabetically: a user with 60 tags
    would otherwise silently lose everything after "m", and the tags they
    actually use are the ones worth spending prompt budget on. Sorted by name
    afterwards so the same vocabulary always produces the same prompt.
    """
    counted = await tags.list_with_counts(user_id)
    counted.sort(key=lambda row: (-row[1], row[0].name))
    return sorted(tag.name for tag, _count in counted[:MAX_KNOWN_TAGS])


async def _require_todo(todos: Todos, user_id: UUID, todo_id: str) -> Todo:
    """Resolve a body-supplied todo id within the caller's own todos.

    Unknown, malformed and *somebody else's* all collapse into the same 404
    (D1 + D-E2): an AI endpoint must not become the one place in the API where
    a caller can probe which ids exist.
    """
    parsed = parse_uuid(todo_id)
    todo = await todos.get(user_id, parsed) if parsed else None
    if todo is None:
        raise TodoNotFound()
    return todo


def _summary_sort_key(todo: Todo) -> tuple[int, date, int]:
    """Order for the summary context: due date first, then priority.

    Undated todos sort last (``1`` before the date) rather than first — a todo
    with no deadline is not more urgent than one due today, and the model reads
    the earlier entries as the more important ones.
    """
    return (
        1 if todo.due_date is None else 0,
        todo.due_date or date.max,
        PRIORITY_RANK.get(todo.priority, len(PRIORITY_RANK)),
    )


@router.get("/status", response_model=AiStatusResponse)
async def ai_status(
    _user: CurrentUser, settings: AppSettings, client: AiClient
) -> AiStatusResponse:
    """Whether the AI features can be offered. **Always 200** (master §6.6).

    This is the endpoint the UI asks before rendering anything AI-related, so
    it must answer even when every AI component is dead: ``enabled`` hides the
    section, ``available`` disables it with an explanation. An error here would
    make a broken *optional* subsystem look like a broken app.

    Authenticated like every other route, but deliberately **not** rate-limited:
    §6.6 scopes the 20-per-5-minutes budget to the POST endpoints (the ones that
    cost model time), and a 429 on the endpoint whose job is to say "AI is
    fine" would black out the UI's AI section for five minutes.

    ``reason`` (iteration 4, master §5.1) says *why* it is unavailable, as a
    closed machine enum — never prose, never a quoted upstream body. It is
    ``null`` exactly when ``available`` is true.
    """
    if not settings.AI_ENABLED:
        # Nothing is called: with AI off there is no service to ask, and the
        # answer would be the same either way.
        return AiStatusResponse(
            enabled=False, available=False, model=None, reason="disabled"
        )

    health = await client.health()
    return AiStatusResponse(
        enabled=True,
        available=health.available,
        model=health.model,
        # ``AiStatusResponse`` enforces "reason is null iff available", and this
        # endpoint may never fail — so the one combination the client half could
        # not interpret is coalesced here rather than raised at the very last
        # step of a request whose whole contract is "always 200".
        reason=health.reason or (None if health.available else "unreachable"),
    )


@router.post(
    "/parse-todo",
    response_model=ParseTodoResponse,
    dependencies=AI_GUARDS,
    responses=AI_ERROR_RESPONSES,
)
async def parse_todo(
    payload: ParseTodoRequest, user: CurrentUser, client: AiClient, tags: Tags
) -> ParseTodoResponse:
    """Turn a natural-language note into a draft todo.

    The caller sends only its text and its local date; the tag vocabulary is
    added here from the caller's own tags, so a client can neither inflate the
    prompt nor ask about tags that are not its own.
    """
    body = await client.post(
        "/ai/parse-todo",
        {
            "text": payload.text,
            "today": payload.today.isoformat(),
            "known_tags": await _known_tags(tags, user.id),
        },
    )
    # Never at info: the model's output is the user's own note, reflected.
    logger.debug("ai-agent parse-todo returned keys %s", sorted(body))
    return ParseTodoResponse(draft=TodoDraft.from_agent(body))


@router.post(
    "/suggest-subtasks",
    response_model=SuggestSubtasksResponse,
    dependencies=AI_GUARDS,
    responses={**AI_ERROR_RESPONSES, **TODO_NOT_FOUND_RESPONSE},
)
async def suggest_subtasks(
    payload: SuggestSubtasksRequest, user: CurrentUser, client: AiClient, todos: Todos
) -> SuggestSubtasksResponse:
    """Suggest the steps an existing todo breaks down into."""
    todo = await _require_todo(todos, user.id, payload.todo_id)

    body = await client.post(
        "/ai/suggest-subtasks",
        {
            "title": todo.title,
            "description": todo.description,
            "max_items": payload.max_items,
        },
    )
    return SuggestSubtasksResponse.from_agent(body, payload.max_items)


@router.post(
    "/suggest-metadata",
    response_model=SuggestMetadataResponse,
    dependencies=AI_GUARDS,
    responses={**AI_ERROR_RESPONSES, **TODO_NOT_FOUND_RESPONSE},
)
async def suggest_metadata(
    payload: SuggestMetadataRequest,
    user: CurrentUser,
    client: AiClient,
    todos: Todos,
    tags: Tags,
) -> SuggestMetadataResponse:
    """Suggest a priority and tags for an existing todo."""
    todo = await _require_todo(todos, user.id, payload.todo_id)

    body = await client.post(
        "/ai/suggest-metadata",
        {
            "title": todo.title,
            "description": todo.description,
            "known_tags": await _known_tags(tags, user.id),
            # The caller's local date when it sent one; the server's is the
            # only other option and "is this due soon" needs *a* today.
            "today": (payload.today or datetime.now(timezone.utc).date()).isoformat(),
        },
    )
    return SuggestMetadataResponse.from_agent(body)


@router.post(
    "/edit-todo",
    response_model=EditTodoResponse,
    dependencies=AI_GUARDS,
    responses={**AI_ERROR_RESPONSES, **TODO_NOT_FOUND_RESPONSE},
)
async def edit_todo(
    payload: EditTodoRequest,
    user: CurrentUser,
    client: AiClient,
    todos: Todos,
    tags: Tags,
) -> EditTodoResponse:
    """Turn a free-text instruction into a reviewable change set (§2.1).

    Like every endpoint here it **writes nothing** (D-AI1). The answer is a
    diff the user deselects parts of and confirms; the frontend then applies
    what survived through the ordinary ``PATCH /api/todos/{id}`` and subtask
    endpoints (D-IT5-1), which is also why an apply still works after the AI
    stack has died.

    What ai-agent gets is a **positional** snapshot: this todo's own fields and
    at most :data:`MAX_SNAPSHOT_SUBTASKS` subtasks as ``{title, completed}``,
    in repository order, with no identifier anywhere in it (D-IT5-3). A model
    that never sees a UUID cannot echo, mangle or invent one, and a 3B model
    copies a single digit far more reliably than 36 hex characters.

    What comes back is resolved against the real todo in
    :meth:`EditTodoResponse.from_agent` — positions become ids, no-ops are
    dropped, every value is re-clamped. That step is not bookkeeping: two
    services agreeing on a contract is not the same as this service being safe
    when the other one is wrong.
    """
    todo = await _require_todo(todos, user.id, payload.todo_id)
    if todo.parent_id is not None:
        # Decision D-IT5-6. This endpoint edits top-level todos — a snapshot of
        # a subtask has no subtasks of its own and the UI never offers the
        # action there — and D-E2 already collapses "not a valid target for
        # you" into the same 404 an unknown id gets. A distinct code here would
        # confirm that the id exists.
        raise TodoNotFound()

    # The relationship is ordered by ``(created_at, id)`` and eager-loaded by
    # the repository, so this is the same order ``TodoResponse`` shows the user
    # — which is what makes a position mean anything to them.
    ordered = list(todo.subtasks)
    snapshot = ordered[:MAX_SNAPSHOT_SUBTASKS]

    body = await client.post(
        "/ai/edit-todo",
        {
            "instruction": payload.instruction,
            "today": payload.today.isoformat(),
            "known_tags": await _known_tags(tags, user.id),
            "todo": {
                "title": todo.title,
                "description": todo.description,
                "priority": (
                    todo.priority.value
                    if isinstance(todo.priority, Priority)
                    else str(todo.priority)
                ),
                "due_date": todo.due_date.isoformat() if todo.due_date else None,
                "completed": todo.completed,
                "tags": [tag.name for tag in todo.tags],
                "subtasks": [
                    {"title": subtask.title, "completed": subtask.completed}
                    for subtask in snapshot
                ],
            },
        },
    )
    # Never at info: the reply is the user's own todo, reflected.
    logger.debug("ai-agent edit-todo returned keys %s", sorted(body))
    return EditTodoResponse.from_agent(
        body,
        todo=todo,
        subtasks=snapshot,
        truncated=len(ordered) > MAX_SNAPSHOT_SUBTASKS,
        today=payload.today,
    )


@router.post(
    "/daily-summary",
    response_model=DailySummaryResponse,
    dependencies=AI_GUARDS,
    responses={
        **AI_ERROR_RESPONSES,
        404: {"model": ErrorResponse, "description": "List not found"},
    },
)
async def daily_summary(
    payload: DailySummaryRequest,
    user: CurrentUser,
    client: AiClient,
    todos: Todos,
    lists: Lists,
) -> DailySummaryResponse:
    """A short briefing over the caller's open todos.

    Only **active top-level** todos are sent: a completed todo is not something
    to plan around, and a subtask read out of the context of its parent is
    noise. The context is capped at 50 items — enough for a real backlog, few
    enough that a 3B model writes a briefing instead of reciting a list.
    """
    scoped_list_id = None
    if payload.list_id is not None:
        # Resolved against the caller's own lists, so an unknown, malformed or
        # foreign id is one answer — 404 ``list_not_found``, the same one
        # GET /api/todos gives — rather than a summary of everything, which
        # would silently describe the wrong todos.
        parsed = parse_uuid(payload.list_id)
        owned = await lists.get(user.id, parsed) if parsed else None
        if owned is None:
            raise ListNotFound()
        scoped_list_id = owned.id

    rows, _total = await todos.list_todos(
        user.id,
        TodoQuery(
            list_id=scoped_list_id,
            status="active",
            sort="due_date",
            order="asc",
            limit=MAX_SUMMARY_TODOS,
            today=payload.today,
        ),
    )
    # The query orders by due date (undated last); priority is applied here as
    # the secondary key. Sorting ≤ 50 loaded rows in Python is cheaper than a
    # second ORDER BY term that every other caller of list_todos would pay for.
    rows.sort(key=_summary_sort_key)

    list_names = {row.id: row.name for row in await lists.list_lists(user.id)}
    context = [
        {
            "title": todo.title,
            "priority": (
                todo.priority.value
                if isinstance(todo.priority, Priority)
                else str(todo.priority)
            ),
            "due_date": todo.due_date.isoformat() if todo.due_date else None,
            "completed": todo.completed,
            "list_name": list_names.get(todo.list_id),
        }
        for todo in rows
    ]

    generated_at = datetime.now(timezone.utc)
    if not context:
        # Nothing to summarise: no model call, no waiting, no invented prose
        # about an empty list. The frontend shows its own empty copy off
        # ``todo_count == 0`` (slice-4 F5), so the contract still holds.
        return DailySummaryResponse(
            summary="", todo_count=0, generated_at=generated_at
        )

    body = await client.post(
        "/ai/daily-summary",
        {"today": payload.today.isoformat(), "todos": context},
    )
    summary = body.get("summary")
    return DailySummaryResponse(
        summary=summary if isinstance(summary, str) else "",
        todo_count=len(context),
        generated_at=generated_at,
    )
