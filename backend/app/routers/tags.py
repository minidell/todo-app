"""``/api/tags`` — the user's tag vocabulary (master §6.4).

There is deliberately **no** ``POST`` (D-A3): a tag exists because a todo uses
it. One creation path means a tag can never be created with a name the todo
write path would have normalized differently, and it matches how the slice-4 AI
suggests tags.

Decision D1 (iteration 1, preserved): ``{tag_id}`` is typed ``str``, so a
malformed id is 404 ``tag_not_found`` rather than 422 — and, with D-E2, another
user's tag is the same 404.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.db.models import User
from app.deps import (
    SessionDep,
    get_current_user,
    get_event_publisher,
    get_tag_repository,
)
from app.errors import TagNotFound
from app.events import EventPublisher, commit_and_publish
from app.repositories.tags import SqlAlchemyTagRepository
from app.routers.utils import parse_uuid
from app.schemas.common import ErrorResponse
from app.schemas.tags import TagResponse, TagUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tags", tags=["tags"])

CurrentUser = Annotated[User, Depends(get_current_user)]
Repo = Annotated[SqlAlchemyTagRepository, Depends(get_tag_repository)]
#: The shared, function-scoped session alias — never a local
#: ``Depends(get_session)``, which could resolve to a different session than
#: the one the repository writes through (see app/deps.py::SessionDep).
Session = SessionDep
#: The publisher already resolves ``X-Client-Id`` (it becomes ``origin`` on
#: every frame this request emits), so no handler declares the header itself.
Events = Annotated[EventPublisher, Depends(get_event_publisher)]

NOT_FOUND_RESPONSE = {404: {"model": ErrorResponse, "description": "Tag not found"}}
NAME_TAKEN_RESPONSE = {
    409: {"model": ErrorResponse, "description": "A tag with that name already exists"}
}


@router.get("", response_model=list[TagResponse])
async def list_tags(user: CurrentUser, repo: Repo) -> list[TagResponse]:
    """Every tag the user has ever used, ordered by name, with usage counts.

    ``todo_count`` counts top-level todos, matching what ``GET /api/todos?tag=``
    returns — a chip that claims five and filters to three is a bug report.

    A tag whose last todo dropped it stays here with ``todo_count`` 0: it is
    still part of the user's vocabulary, and silently deleting it would make a
    tag disappear from the filter bar as a side effect of an unrelated edit.
    """
    rows = await repo.list_with_counts(user.id)
    return [
        TagResponse(id=tag.id, name=tag.name, todo_count=count) for tag, count in rows
    ]


@router.patch(
    "/{tag_id}",
    response_model=TagResponse,
    responses={**NOT_FOUND_RESPONSE, **NAME_TAKEN_RESPONSE},
)
async def rename_tag(
    tag_id: str,
    payload: TagUpdate,
    user: CurrentUser,
    repo: Repo,
    session: Session,
    events: Events,
) -> TagResponse:
    """Rename a tag; every todo carrying it follows, since the rows join by id.

    §7.2: the frame carries the same ``TagResponse`` the caller gets, and it
    deliberately does **not** fan out one ``todo.updated`` per affected todo —
    a rename can touch arbitrarily many rows, so the contract makes the
    receiver refetch its todo view instead (D-IT4-2).
    """
    parsed = parse_uuid(tag_id)
    renamed = await repo.rename(user.id, parsed, payload.name) if parsed else None
    if renamed is None:
        raise TagNotFound()

    # The counts are read from the flushed-but-uncommitted transaction, so the
    # response and the frame are built from one state before either escapes.
    counts = await repo.list_with_counts(user.id)
    todo_count = next((count for tag, count in counts if tag.id == renamed.id), 0)
    response = TagResponse(id=renamed.id, name=renamed.name, todo_count=todo_count)

    events.stage_tag_updated(response.model_dump(mode="json"))
    # Commit before the response: the rename changes what every todo carrying
    # this tag reports, and the client's next fetch must not race the write.
    # Publish only afterwards — a rename that dies on the unique index (409)
    # must announce nothing at all.
    await commit_and_publish(session, events)
    return response


@router.delete(
    "/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND_RESPONSE,
)
async def delete_tag(
    tag_id: str,
    user: CurrentUser,
    repo: Repo,
    session: Session,
    events: Events,
) -> None:
    """Delete a tag and its associations — **never** the todos carrying it.

    The frame carries the id only, like ``list.deleted``; a receiver treats it
    as invalidating its todo view as well as its tag vocabulary (§7.2).
    """
    parsed = parse_uuid(tag_id)
    deleted = await repo.delete(user.id, parsed) if parsed else False
    if not deleted:
        raise TagNotFound()
    events.stage_tag_deleted(parsed)
    # A 204 must mean the tag is gone, including its todo_tags rows: the client
    # may re-list tags the moment it sees the status — and the frame goes out
    # only once that is durable.
    await commit_and_publish(session, events)
    logger.info("User %s deleted tag %s", user.id, tag_id)
