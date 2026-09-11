"""``/api/lists`` — multiple named todo lists per user (master §6.2).

Decision D1 (iteration 1, preserved): ``{list_id}`` is typed ``str``, so a
malformed id is 404 ``list_not_found`` rather than 422 — combined with D-E2
(another user's list is also 404) an id tells a caller nothing about whether it
exists.
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.exc import IntegrityError

from app.db.models import User
from app.deps import (
    SessionDep,
    get_current_user,
    get_event_publisher,
    get_list_repository,
)
from app.errors import ListNameTaken, ListNotFound
from app.events import EventPublisher, commit_and_publish
from app.repositories.lists import SqlAlchemyListRepository
from app.routers.utils import parse_uuid
from app.schemas.common import ErrorResponse
from app.schemas.lists import ListCreate, ListResponse, ListUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lists", tags=["lists"])

CurrentUser = Annotated[User, Depends(get_current_user)]
Repo = Annotated[SqlAlchemyListRepository, Depends(get_list_repository)]
Session = SessionDep
Events = Annotated[EventPublisher, Depends(get_event_publisher)]

#: Upper bound on the per-todo fan-out when a list is deleted.
#:
#: §7.2 asks for one ``todo.deleted`` per top-level todo the list held, and the
#: subscriber queues hold 100 frames before a subscriber is *dropped*. Deleting
#: a 500-todo list would therefore evict every tab it was meant to inform. Past
#: this many todos the ``list.deleted`` frame goes out alone — the spec already
#: allows a client to simply refetch on it, and a refetch is cheaper than a
#: reconnect.
MAX_DELETE_FANOUT = 50

NOT_FOUND_RESPONSE = {404: {"model": ErrorResponse, "description": "List not found"}}
NAME_TAKEN_RESPONSE = {
    409: {"model": ErrorResponse, "description": "A list with that name already exists"}
}


async def _require_name_free(
    repo: SqlAlchemyListRepository,
    user_id: UUID,
    name: str,
    *,
    excluding: UUID | None = None,
) -> None:
    """Application-side uniqueness check, case-insensitive per user.

    The ``UNIQUE (user_id, name)`` index is the real guarantee, but it is
    case-sensitive; this check is what makes "Work" and "work" collide, and it
    turns the race the index catches into the same 409 rather than a 500.
    ``excluding`` lets a rename keep its own row (so changing only the casing of
    a name is allowed).
    """
    existing = await repo.find_by_name(user_id, name)
    if existing is not None and existing.id != excluding:
        raise ListNameTaken()


@router.get("", response_model=list[ListResponse])
async def list_lists(user: CurrentUser, repo: Repo) -> list[ListResponse]:
    rows = await repo.list_lists(user.id)
    counts = await repo.todo_counts(user.id)
    return [ListResponse.from_entity(row, counts.get(row.id, (0, 0))) for row in rows]


@router.post(
    "",
    response_model=ListResponse,
    status_code=status.HTTP_201_CREATED,
    responses=NAME_TAKEN_RESPONSE,
)
async def create_list(
    payload: ListCreate,
    user: CurrentUser,
    repo: Repo,
    session: Session,
    events: Events,
) -> ListResponse:
    await _require_name_free(repo, user.id, payload.name)
    try:
        created = await repo.create(user.id, payload.name)
        # A brand-new list holds nothing yet, so the counts are known without
        # a query.
        response = ListResponse.from_entity(created, (0, 0))
        events.stage_list_created(response.model_dump(mode="json"))
        # Committed here rather than in the session dependency's teardown,
        # which runs after the response has already been sent: the client
        # switches to the new list and refetches immediately. The staged frame
        # goes out only once that commit has returned — and, on the unique-index
        # race below, never goes out at all.
        await commit_and_publish(session, events)
    except IntegrityError as exc:
        await session.rollback()
        raise ListNameTaken() from exc

    logger.info("User %s created list %s", user.id, created.id)
    return response


@router.patch(
    "/{list_id}",
    response_model=ListResponse,
    responses={**NOT_FOUND_RESPONSE, **NAME_TAKEN_RESPONSE},
)
async def rename_list(
    list_id: str,
    payload: ListUpdate,
    user: CurrentUser,
    repo: Repo,
    session: Session,
    events: Events,
) -> ListResponse:
    parsed = parse_uuid(list_id)
    existing = await repo.get(user.id, parsed) if parsed else None
    if existing is None:
        raise ListNotFound()

    await _require_name_free(repo, user.id, payload.name, excluding=existing.id)
    try:
        renamed = await repo.rename(user.id, existing.id, payload.name)
        if renamed is None:  # pragma: no cover - the row was fetched above
            raise ListNotFound()

        counts = await repo.todo_counts(user.id, [renamed.id])
        response = ListResponse.from_entity(renamed, counts.get(renamed.id, (0, 0)))
        events.stage_list_updated(response.model_dump(mode="json"))
        await commit_and_publish(session, events)
    except IntegrityError as exc:
        await session.rollback()
        raise ListNameTaken() from exc

    return response


@router.delete(
    "/{list_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **NOT_FOUND_RESPONSE,
        409: {"model": ErrorResponse, "description": "You must keep at least one list"},
    },
)
async def delete_list(
    list_id: str,
    user: CurrentUser,
    repo: Repo,
    session: Session,
    events: Events,
) -> None:
    """Delete a list and, by DB cascade, its todos and their subtasks.

    ``cannot_delete_last_list`` (409) comes from the repository, which also
    promotes the oldest survivor when the deleted list was the default.
    """
    parsed = parse_uuid(list_id)
    # Collect the ids before the cascade removes the rows: a receiver can then
    # drop each todo without waiting for a refetch (§7.2).
    doomed = (
        await repo.top_level_todo_ids(user.id, parsed, limit=MAX_DELETE_FANOUT + 1)
        if parsed
        else []
    )

    deleted = await repo.delete(user.id, parsed) if parsed else False
    if not deleted:
        raise ListNotFound()

    events.stage_list_deleted(parsed)
    if len(doomed) <= MAX_DELETE_FANOUT:
        for todo_id in doomed:
            events.stage_todo_deleted(todo_id, parsed)
    else:
        logger.info(
            "Skipping the per-todo fan-out for list %s: more than %d todos",
            list_id,
            MAX_DELETE_FANOUT,
        )

    await commit_and_publish(session, events)
    logger.info("User %s deleted list %s", user.id, list_id)
