"""``/api/todos`` — the enriched todo surface (master §6.3).

Decision D1 (iteration 1, preserved): the ``{id}`` path parameter is typed
``str``, not ``UUID``, so a syntactically invalid id returns 404
"Todo not found", never 422. Combined with D-E2 (another user's row is 404
too) the endpoint leaks nothing about ids it does not own.

Query parameters are the opposite case: they are typed strictly (``Literal``,
``date``, bounded ``int``), so a malformed filter is a 422 that names the
parameter rather than a silently ignored one — a filter that quietly does
nothing shows the user the wrong list and tells them nothing.
"""

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import StringConstraints

from app.db.models import Priority, Todo, User
from app.deps import (
    SessionDep,
    get_current_user,
    get_event_publisher,
    get_list_repository,
    get_todo_repository,
)
from app.errors import ConflictingDueFilters, EmptyUpdate, ListNotFound, TodoNotFound
from app.events import EventPublisher, commit_and_publish
from app.repositories.lists import SqlAlchemyListRepository
from app.repositories.protocols import (
    TodoCreateData,
    TodoQuery,
    TodoRepository,
    TodoUpdateData,
)
from app.routers.utils import parse_uuid
from app.schemas.common import ErrorResponse
from app.schemas.tags import TAG_MAX_LENGTH, TagResponse, normalize_tag
from app.schemas.todos import (
    MAX_TAGS_PER_TODO,
    SubtaskCreate,
    TodoCreate,
    TodoResponse,
    TodoUpdate,
)

router = APIRouter(prefix="/api/todos", tags=["todos"])

CurrentUser = Annotated[User, Depends(get_current_user)]
Repo = Annotated[TodoRepository, Depends(get_todo_repository)]
Lists = Annotated[SqlAlchemyListRepository, Depends(get_list_repository)]
Session = SessionDep
#: The publisher already resolves ``X-Client-Id`` (it becomes ``origin`` on
#: every frame this request emits), so no handler declares the header itself.
Events = Annotated[EventPublisher, Depends(get_event_publisher)]

NOT_FOUND_RESPONSE = {404: {"model": ErrorResponse, "description": "Todo not found"}}
LIST_NOT_FOUND_RESPONSE = {
    404: {"model": ErrorResponse, "description": "Todo or list not found"}
}
SUBTASK_RESPONSES = {
    **NOT_FOUND_RESPONSE,
    400: {"model": ErrorResponse, "description": "Subtasks can only be one level deep"},
}
FILTER_RESPONSES = {
    **LIST_NOT_FOUND_RESPONSE,
    400: {"model": ErrorResponse, "description": "Conflicting due filters"},
}

#: A tag *filter* value. Bounded at the wire edge like every stored tag name:
#: the value is only ever compared against a ``String(30)`` column, so anything
#: longer is a malformed filter rather than a search that finds nothing.
TagFilterName = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=TAG_MAX_LENGTH)
]

TodoStatusParam = Literal["all", "active", "completed"]
DuePresetParam = Literal["any", "overdue", "today", "week", "none"]
TodoSortParam = Literal["created_at", "updated_at", "due_date", "priority", "title"]
SortOrderParam = Literal["asc", "desc"]

MAX_LIMIT = 500
DEFAULT_LIMIT = 200
MAX_SEARCH_LENGTH = 100


def _query_validation_error(
    parameter: str, message: str, value: object, *, error_type: str = "value_error"
) -> RequestValidationError:
    """A 422 in FastAPI's own shape for a query value we validate by hand.

    Hand-rolled parsing must fail the same way declarative parsing does, or a
    bad filter would surface as a 500 (or, worse, as a silently ignored
    parameter) instead of the standard body naming the offending parameter.
    """
    return RequestValidationError(
        [
            {
                "type": error_type,
                "loc": ("query", parameter),
                "msg": message,
                "input": value,
            }
        ]
    )


def _parse_priorities(values: list[str] | None) -> tuple[Priority, ...]:
    """``?priority=high&priority=low`` **or** ``?priority=high,low``.

    Both spellings are in the frozen contract (§6.3), which rules out declaring
    the parameter as ``list[Priority]`` and letting FastAPI validate it — the
    comma form would fail enum validation. The parsing is therefore manual, but
    an invalid value still raises ``RequestValidationError`` so the client gets
    the standard 422 body naming ``priority``, exactly as for every other bad
    query value.
    """
    if not values:
        return ()
    parsed: list[Priority] = []
    for raw in values:
        for part in raw.split(","):
            candidate = part.strip()
            if not candidate:
                continue
            try:
                parsed.append(Priority(candidate))
            except ValueError as exc:
                raise _query_validation_error(
                    "priority",
                    "Input should be 'low', 'medium' or 'high'",
                    candidate,
                    error_type="enum",
                ) from exc
    # Duplicates collapse: they would only repeat a value inside an IN clause.
    return tuple(dict.fromkeys(parsed))


def _serialize(todo: Todo) -> dict[str, Any]:
    """The event payload, produced by the same code path as the HTTP response.

    Going through ``TodoResponse`` rather than hand-building a dict is what
    keeps the two from drifting: a field added to the response appears in the
    frame automatically, and a receiver can treat an event's ``todo`` as
    interchangeable with one from ``GET /api/todos``.
    """
    return TodoResponse.model_validate(todo).model_dump(mode="json")


async def _stage_top_level(
    events: EventPublisher, repo: TodoRepository, user_id, todo_id
) -> None:
    """Stage ``todo.updated`` for a top-level todo, re-read from the session.

    The reload is the point: after a subtask changed, the parent's cached
    ``subtasks`` collection is stale (the repository expires it), and a frame
    carrying the *old* children would tell every other tab to render exactly
    the state that just stopped being true.
    """
    parent = await repo.get(user_id, todo_id)
    if parent is not None:  # pragma: no branch - the caller just touched it
        events.stage_todo_updated(_serialize(parent))


def _stage_created_tags(
    events: EventPublisher, repo: TodoRepository, todo: Todo
) -> None:
    """Announce the tags this request brought into existence (§7.2, IT3-1).

    Called **before** the request's ``todo.*`` frame is staged, so a receiver
    applying frames in order is never told about a todo naming a tag it has not
    heard of.

    ``todo_count`` is computed, not queried, and it is exact: a tag that did not
    exist before this request can be attached to nothing but the todo being
    written, and ``TagResponse.todo_count`` counts top-level todos only — hence
    1 for a top-level write and 0 for a subtask. The same reasoning as
    ``list.created`` in ``app/routers/lists.py``, and it saves a query on the
    hot write path.
    """
    todo_count = 0 if todo.parent_id else 1
    for tag in repo.take_created_tags():
        events.stage_tag_created(
            TagResponse(
                id=tag.id, name=tag.name, todo_count=todo_count
            ).model_dump(mode="json")
        )


async def _stage_todo_change(
    events: EventPublisher,
    repo: TodoRepository,
    user_id,
    todo: Todo,
    *,
    created: bool,
) -> None:
    """Announce a change to ``todo`` as a change to its top-level row.

    Master §7.2: a client renders top-level todos with their subtasks embedded,
    so **any** subtask mutation is broadcast as a ``todo.updated`` carrying the
    whole reloaded parent. A receiver replaces one array element instead of
    trying to reconcile a frame about a row it never rendered on its own.
    """
    if todo.parent_id is None:
        payload = _serialize(todo)
        if created:
            events.stage_todo_created(payload)
        else:
            events.stage_todo_updated(payload)
        return
    await _stage_top_level(events, repo, user_id, todo.parent_id)


def _parse_tags(values: list[str] | None) -> tuple[str, ...]:
    """Normalize, de-duplicate and bound the repeatable ``tag`` filter.

    Each distinct tag becomes its own correlated ``EXISTS`` subquery (AND
    semantics, §6.3), so the *number of distinct names* is the size of the
    generated SQL. Left unbounded that is a denial of service with no
    authentication cost beyond a token: 500 repeats of one name took ~3.8 s,
    and 1000 blew past SQLite's expression-tree limit into a 500.

    De-duplication happens **before** the cap on purpose, and after
    normalization: ``tag=home&tag=Home&tag=%20home`` is one filter, not three,
    so a client repeating a name — which the UI does when a chip is toggled
    quickly — is answered rather than rejected. Only genuinely distinct names
    count towards the limit, which is the same ten a todo may carry.
    """
    if not values:
        return ()
    names = list(
        dict.fromkeys(
            normalized for raw in values if (normalized := normalize_tag(raw))
        )
    )
    if len(names) > MAX_TAGS_PER_TODO:
        raise _query_validation_error(
            "tag",
            f"At most {MAX_TAGS_PER_TODO} distinct tags may be combined in one "
            f"filter (got {len(names)})",
            len(names),
            error_type="too_long",
        )
    return tuple(names)


@router.get("", response_model=list[TodoResponse], responses=FILTER_RESPONSES)
async def list_todos(
    user: CurrentUser,
    repo: Repo,
    lists: Lists,
    response: Response,
    list_id: Annotated[
        str | None,
        Query(description="Restrict to one of the caller's lists; omit for all."),
    ] = None,
    status_filter: Annotated[
        TodoStatusParam, Query(alias="status", description="Completion state.")
    ] = "all",
    priority: Annotated[
        list[str] | None,
        Query(description="Repeatable or comma-separated: low, medium, high."),
    ] = None,
    tag: Annotated[
        list[TagFilterName] | None,
        Query(
            description=(
                "Repeatable; a todo must carry ALL of them. Names are "
                f"normalized and de-duplicated; at most {MAX_TAGS_PER_TODO} "
                "distinct names."
            )
        ),
    ] = None,
    due: Annotated[
        DuePresetParam, Query(description="Due-date preset, relative to `today`.")
    ] = "any",
    due_from: Annotated[date | None, Query(description="Inclusive lower bound.")] = None,
    due_to: Annotated[date | None, Query(description="Inclusive upper bound.")] = None,
    today: Annotated[
        date | None,
        Query(description="The caller's local date; drives the due presets."),
    ] = None,
    q: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=MAX_SEARCH_LENGTH,
            description="Case-insensitive substring of title or description.",
        ),
    ] = None,
    sort: TodoSortParam = "created_at",
    order: SortOrderParam = "asc",
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TodoResponse]:
    # The list filter is resolved against the caller's own lists first: an
    # unknown, malformed or someone else's id is all one answer — 404
    # list_not_found (D-E2) — rather than an empty array, which would look like
    # "your list is empty" and hide the mistake.
    scoped_list_id = None
    if list_id is not None:
        parsed = parse_uuid(list_id)
        owned = await lists.get(user.id, parsed) if parsed else None
        if owned is None:
            raise ListNotFound()
        scoped_list_id = owned.id

    if due != "any" and (due_from is not None or due_to is not None):
        # Silently letting one win would show a list that matches neither
        # request; the contract makes the contradiction explicit (§5).
        raise ConflictingDueFilters()

    query = TodoQuery(
        list_id=scoped_list_id,
        status=status_filter,
        priorities=_parse_priorities(priority),
        tags=_parse_tags(tag),
        due=due,
        due_from=due_from,
        due_to=due_to,
        today=today,
        q=q,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    rows, total = await repo.list_todos(user.id, query)
    # The body stays a bare array (iteration-1 compatible); the number of rows
    # matching the filters — before limit/offset — travels in a header (§6.3),
    # which CORS already exposes.
    response.headers["X-Total-Count"] = str(total)
    return [TodoResponse.model_validate(row) for row in rows]


@router.post(
    "",
    response_model=TodoResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**LIST_NOT_FOUND_RESPONSE, **SUBTASK_RESPONSES},
)
async def create_todo(
    payload: TodoCreate,
    user: CurrentUser,
    repo: Repo,
    session: Session,
    events: Events,
) -> TodoResponse:
    # An omitted list_id lands in the caller's default list; a list_id or
    # parent_id that is not the caller's raises inside the repository, which is
    # the only place that can check ownership without a second query.
    todo = await repo.create(
        user.id,
        TodoCreateData(
            title=payload.title,
            list_id=payload.list_id,
            parent_id=payload.parent_id,
            description=payload.description,
            priority=payload.priority,
            due_date=payload.due_date,
            tags=tuple(payload.tags),
        ),
    )
    response = TodoResponse.model_validate(todo)
    _stage_created_tags(events, repo, todo)
    await _stage_todo_change(events, repo, user.id, todo, created=True)
    # Commit before the response, not in the session dependency's teardown —
    # that runs after the client already has the 201 and can have issued its
    # next request (QA saw stale list counts exactly this way) — and publish
    # only once that commit has returned, so no tab can refetch on an event
    # about a row that is not there yet. Safe to serialise afterwards because
    # sessions never expire on commit.
    await commit_and_publish(session, events)
    return response


@router.post(
    "/{todo_id}/subtasks",
    response_model=TodoResponse,
    status_code=status.HTTP_201_CREATED,
    responses=SUBTASK_RESPONSES,
)
async def create_subtask(
    todo_id: str,
    payload: SubtaskCreate,
    user: CurrentUser,
    repo: Repo,
    session: Session,
    events: Events,
) -> TodoResponse:
    """Convenience route for ``POST /api/todos`` with ``parent_id`` (§6.3).

    It shares the repository call rather than the plumbing, so both routes
    enforce invariants I1–I3 identically: the subtask inherits its parent's
    owner and list, and a parent that is itself a subtask is 400
    ``subtask_depth_exceeded``.
    """
    parsed = parse_uuid(todo_id)
    if parsed is None:
        raise TodoNotFound()

    subtask = await repo.create(
        user.id,
        TodoCreateData(
            title=payload.title,
            parent_id=parsed,
            description=payload.description,
            priority=payload.priority,
            due_date=payload.due_date,
            tags=tuple(payload.tags),
        ),
    )
    response = TodoResponse.model_validate(subtask)
    _stage_created_tags(events, repo, subtask)
    # A subtask is not a row anyone renders alone: the frame carries the
    # reloaded parent (§7.2), so a receiver replaces one array element.
    await _stage_todo_change(events, repo, user.id, subtask, created=True)
    # Same rule as every mutating handler: durable before the client is told
    # it happened. The subtask and the parent's changed collection land in one
    # transaction, so a reader can never see the 201 without the row — and the
    # frame goes out only after that transaction has committed.
    await commit_and_publish(session, events)
    return response


@router.get("/{todo_id}", response_model=TodoResponse, responses=NOT_FOUND_RESPONSE)
async def get_todo(todo_id: str, user: CurrentUser, repo: Repo) -> TodoResponse:
    parsed = parse_uuid(todo_id)
    todo = await repo.get(user.id, parsed) if parsed else None
    if todo is None:
        raise TodoNotFound()
    return TodoResponse.model_validate(todo)


@router.patch(
    "/{todo_id}", response_model=TodoResponse, responses={**SUBTASK_RESPONSES}
)
async def update_todo(
    todo_id: str,
    payload: TodoUpdate,
    user: CurrentUser,
    repo: Repo,
    session: Session,
    events: Events,
) -> TodoResponse:
    """Partial update (master R11).

    Only the keys actually present in the body are applied; an explicit ``null``
    clears ``description``, ``due_date`` or ``parent_id``. ``{"completed": …}``
    — the whole of the slice-1/2 contract — still means exactly what it did.
    """
    changes: dict[str, Any] = payload.changes()
    if not changes:
        # A body with nothing to apply is a client mistake worth naming, not a
        # 200 that pretends something happened (§5 ``empty_update``).
        raise EmptyUpdate()

    parsed = parse_uuid(todo_id)

    # Re-parenting moves a row between two top-level families, so the frames
    # have to describe *both*. The extra read is paid only by the requests that
    # actually ask for it — an ordinary edit still costs one query.
    was_top_level = False
    old_parent_id = None
    if "parent_id" in changes and parsed is not None:
        before = await repo.get(user.id, parsed)
        if before is not None:
            old_parent_id = before.parent_id
            was_top_level = before.parent_id is None

    updated = (
        await repo.update(user.id, parsed, TodoUpdateData(**changes))
        if parsed
        else None
    )
    if updated is None:
        raise TodoNotFound()

    response = TodoResponse.model_validate(updated)

    _stage_created_tags(events, repo, updated)

    if was_top_level and updated.parent_id is not None:
        # It stopped being a row of its own. ``todo.deleted`` is what a client
        # needs here — every consumer renders top-level todos with subtasks
        # embedded, so "no longer top level" and "gone" are the same edit to
        # its list. The ``todo.updated`` staged next carries the row in its new
        # home, so nothing is actually lost.
        events.stage_todo_deleted(updated.id, updated.list_id)

    await _stage_todo_change(events, repo, user.id, updated, created=False)

    if old_parent_id is not None and old_parent_id != updated.parent_id:
        # The todo left this parent; that parent's subtask list changed too.
        await _stage_top_level(events, repo, user.id, old_parent_id)

    await commit_and_publish(session, events)
    return response


@router.delete(
    "/{todo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND_RESPONSE,
)
async def delete_todo(
    todo_id: str,
    user: CurrentUser,
    repo: Repo,
    session: Session,
    events: Events,
) -> None:
    parsed = parse_uuid(todo_id)
    # Read before deleting: the frame needs the row's ``list_id`` (so a client
    # can scope the removal) and its ``parent_id`` (so a subtask deletion is
    # announced as an update of its parent), and neither survives the delete.
    todo = await repo.get(user.id, parsed) if parsed else None
    if todo is None:
        raise TodoNotFound()
    deleted_id, list_id, parent_id = todo.id, todo.list_id, todo.parent_id

    deleted = await repo.delete(user.id, parsed)
    if not deleted:  # pragma: no cover - it was there one statement ago
        raise TodoNotFound()

    if parent_id is None:
        events.stage_todo_deleted(deleted_id, list_id)
    else:
        # §7.2: a deleted subtask is an update of the parent, whose reloaded
        # ``subtasks`` no longer contains it.
        await _stage_top_level(events, repo, user.id, parent_id)

    await commit_and_publish(session, events)
