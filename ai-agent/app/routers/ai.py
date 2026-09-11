"""The AI endpoints (master §8.2, slice-4 spec B3; ``/edit-todo`` iteration 5).

Every endpoint runs the same pipeline:

1. validate the request (``extra="forbid"``);
2. build a small user message from ``prompts.py``;
3. ask Ollama for JSON constrained by the endpoint's JSON schema;
4. validate the reply with a permissive "raw" model;
5. on a ``ValidationError``, retry **once** with the validator errors fed back;
6. on a second failure, 503 ``ai_invalid_response``;
7. **sanitize** every field (``sanitize.py``) — this step is not optional;
8. return the sanitized draft. Nothing is ever persisted (master D-AI1).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ValidationError

from app import prompts, sanitize
from app.auth import verify_internal_token
from app.deps import get_ollama_client
from app.errors import ai_invalid_response
from app.ollama import MIN_RETRY_BUDGET_SECONDS, OllamaClient
from app.schemas import (
    DailySummaryRequest,
    DailySummaryResponse,
    EditOp,
    EditTodoRequest,
    EditTodoResponse,
    ParseTodoRequest,
    ParseTodoResponse,
    PingResponse,
    RawEditProposal,
    RawMetadata,
    RawSubtaskList,
    RawSummary,
    RawTodoDraft,
    SubtaskDraft,
    SuggestMetadataRequest,
    SuggestMetadataResponse,
    SuggestSubtasksRequest,
    SuggestSubtasksResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ai",
    tags=["ai"],
    dependencies=[Depends(verify_internal_token)],
)

RawT = TypeVar("RawT", bound=BaseModel)

#: How much of the validator output is fed back to the model in a repair turn.
MAX_REPAIR_ERROR_CHARS = 400


def _format_errors(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)[:MAX_REPAIR_ERROR_CHARS]


async def _generate(
    client: OllamaClient,
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    raw_model: type[RawT],
    max_tokens: int = 512,
) -> RawT:
    """Steps 3-6 of the pipeline: one call, one repair retry, then give up.

    All upstream calls share one deadline, so the endpoint as a whole cannot
    exceed ``OLLAMA_TIMEOUT_SECONDS`` however many retries it takes.
    """
    deadline = client.new_deadline()
    result = await client.chat_json(
        system=system,
        user=user,
        schema=schema,
        max_tokens=max_tokens,
        deadline=deadline,
    )
    logger.debug("Ollama reply: %s", result.raw)
    try:
        return raw_model.model_validate(result.data)
    except ValidationError as first_error:
        errors = _format_errors(first_error)
        logger.info(
            "Model reply failed validation for %s, retrying once: %s",
            raw_model.__name__,
            errors,
        )
        deadline.check(minimum=MIN_RETRY_BUDGET_SECONDS)
        repair = await client.chat_json(
            system=system,
            user=user,
            schema=schema,
            max_tokens=max_tokens,
            deadline=deadline,
            follow_up=[
                {"role": "assistant", "content": result.raw},
                {"role": "user", "content": prompts.REPAIR_TEMPLATE.format(errors=errors)},
            ],
        )
        logger.debug("Ollama repair reply: %s", repair.raw)
        try:
            return raw_model.model_validate(repair.data)
        except ValidationError as second_error:
            logger.warning(
                "Model reply failed validation twice for %s: %s",
                raw_model.__name__,
                _format_errors(second_error),
            )
            raise ai_invalid_response() from second_error


ClientDep = Annotated[OllamaClient, Depends(get_ollama_client)]


@router.get("/ping", response_model=PingResponse)
async def ping() -> PingResponse:
    """Authenticated liveness echo (master §8.2, iteration-4 IT3-2).

    Deliberately the cheapest endpoint in the service: no Ollama client
    dependency, no I/O, no model load. It sits on this router purely so that it
    inherits ``verify_internal_token`` — a missing or wrong token gets the same
    401 as every other ``/ai/*`` route, and *that* is the signal the backend's
    ``GET /api/ai/status`` reads to report ``reason="auth_failed"`` instead of
    the misleading ``available: true`` it reports today.

    Because it never touches Ollama it is safe to call on every backend health
    probe, alongside the unauthenticated ``/health``.
    """
    return PingResponse(status="ok")


@router.post("/parse-todo", response_model=ParseTodoResponse)
async def parse_todo(payload: ParseTodoRequest, client: ClientDep) -> ParseTodoResponse:
    """Turn a free-text note into a single todo draft."""
    raw = await _generate(
        client,
        system=prompts.PARSE_TODO_SYSTEM,
        user=prompts.parse_todo_user(
            text=payload.text, today=payload.today, known_tags=payload.known_tags
        ),
        schema=prompts.PARSE_TODO_SCHEMA,
        raw_model=RawTodoDraft,
    )

    title = sanitize.clean_title(raw.title)
    if title is None:
        logger.warning("Model returned an empty todo title")
        raise ai_invalid_response()

    titles = sanitize.clean_titles(
        [subtask.title for subtask in raw.subtasks], max_items=5
    )
    return ParseTodoResponse(
        title=title,
        description=sanitize.clean_description(raw.description),
        priority=sanitize.clean_priority(raw.priority),
        due_date=sanitize.clean_due_date(raw.due_date, today=payload.today),
        tags=sanitize.clean_tags(raw.tags),
        subtasks=[SubtaskDraft(title=item) for item in titles],
    )


@router.post("/suggest-subtasks", response_model=SuggestSubtasksResponse)
async def suggest_subtasks(
    payload: SuggestSubtasksRequest, client: ClientDep
) -> SuggestSubtasksResponse:
    """Split one todo into at most ``max_items`` ordered steps."""
    raw = await _generate(
        client,
        system=prompts.SUGGEST_SUBTASKS_SYSTEM.format(max_items=payload.max_items),
        user=prompts.suggest_subtasks_user(
            title=payload.title,
            description=payload.description,
            max_items=payload.max_items,
        ),
        schema=prompts.SUGGEST_SUBTASKS_SCHEMA,
        raw_model=RawSubtaskList,
    )

    titles = sanitize.clean_titles(
        [subtask.title for subtask in raw.subtasks], max_items=payload.max_items
    )
    return SuggestSubtasksResponse(
        subtasks=[SubtaskDraft(title=item) for item in titles]
    )


@router.post("/suggest-metadata", response_model=SuggestMetadataResponse)
async def suggest_metadata(
    payload: SuggestMetadataRequest, client: ClientDep
) -> SuggestMetadataResponse:
    """Suggest a priority and up to five tags for one todo."""
    raw = await _generate(
        client,
        system=prompts.SUGGEST_METADATA_SYSTEM,
        user=prompts.suggest_metadata_user(
            title=payload.title,
            description=payload.description,
            known_tags=payload.known_tags,
            today=payload.today,
        ),
        schema=prompts.SUGGEST_METADATA_SCHEMA,
        raw_model=RawMetadata,
        max_tokens=256,
    )

    return SuggestMetadataResponse(
        priority=sanitize.clean_priority(raw.priority),
        tags=sanitize.clean_tags(raw.tags),
    )


@router.post("/edit-todo", response_model=EditTodoResponse)
async def edit_todo(payload: EditTodoRequest, client: ClientDep) -> EditTodoResponse:
    """Turn one free-text instruction into a positional change proposal (§2.2).

    Three properties are worth stating, because each of them is a rule an
    obvious implementation would break:

    * **An empty proposal is a valid answer, not an error.** "Delete this todo"
      and "add three todos about the car" are requests the schema deliberately
      cannot express, and the correct reply is every field null. Raising
      ``ai_invalid_response`` here would turn a well-behaved refusal into a 503
      and tell the user the AI is down.
    * **Nothing defaults.** An unusable priority, date or flag means "no
      change"; see ``sanitize.clean_optional_priority`` for why the drafting
      endpoints' habit of coercing to ``medium`` is a bug on this one.
    * **Indices, never ids.** The snapshot the caller sent is positional, so an
      operation the model invents can only ever name a position — and one
      outside ``1..len(subtasks)`` is dropped here before the backend drops it
      again against the real list (decision D-IT5-3).
    """
    raw = await _generate(
        client,
        system=prompts.EDIT_TODO_SYSTEM,
        user=prompts.edit_todo_user(
            instruction=payload.instruction,
            today=payload.today,
            known_tags=payload.known_tags,
            todo=payload.todo.model_dump(),
        ),
        schema=prompts.EDIT_TODO_SCHEMA,
        raw_model=RawEditProposal,
        max_tokens=512,
    )

    clear = sanitize.clean_clear_fields(raw.clear)
    description = sanitize.clean_description(raw.description)
    due_date = sanitize.clean_due_date(raw.due_date, today=payload.today)
    tags_add, tags_remove = sanitize.clean_tag_edits(raw.tags_add, raw.tags_remove)
    operations = sanitize.clean_edit_ops(
        [operation.model_dump() for operation in raw.subtasks],
        subtask_count=len(payload.todo.subtasks),
        existing_titles=[subtask.title for subtask in payload.todo.subtasks],
    )

    return EditTodoResponse(
        title=sanitize.clean_title(raw.title),
        description=description,
        # A model that sends both a new description and "clear the description"
        # is contradicting itself; the explicit value wins, because it is the
        # more specific of the two and the destructive reading is the one we do
        # not want to guess at.
        clear_description="description" in clear and description is None,
        priority=sanitize.clean_optional_priority(raw.priority),
        due_date=due_date,
        clear_due_date="due_date" in clear and due_date is None,
        completed=sanitize.clean_flag(raw.completed),
        tags_add=tags_add,
        tags_remove=tags_remove,
        subtasks=[
            EditOp(
                action=operation.action,
                index=operation.index,
                title=operation.title,
            )
            for operation in operations
        ],
    )


@router.post("/daily-summary", response_model=DailySummaryResponse)
async def daily_summary(
    payload: DailySummaryRequest, client: ClientDep
) -> DailySummaryResponse:
    """Write a short plain-text briefing about the caller's open todos."""
    raw = await _generate(
        client,
        system=prompts.DAILY_SUMMARY_SYSTEM,
        user=prompts.daily_summary_user(
            today=payload.today,
            todos=[todo.model_dump() for todo in payload.todos],
        ),
        schema=prompts.DAILY_SUMMARY_SCHEMA,
        raw_model=RawSummary,
    )

    summary = sanitize.clean_summary(raw.summary)
    if not summary:
        logger.warning("Model returned an empty summary")
        raise ai_invalid_response()
    return DailySummaryResponse(summary=summary)
