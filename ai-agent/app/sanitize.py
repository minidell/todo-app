"""The "never trust the model" layer (slice-4 spec B3 step 7).

Every value that comes back from Ollama passes through here before it leaves the
service. Validation only proves the *shape*; these helpers enforce the *limits*
the todo API actually accepts (master §3.1, §3.3), so a hallucinated 500-word
title, a tag containing ``!`` or a due date in 2099 can never reach the backend.

All helpers are pure and independently unit-tested in ``tests/test_sanitize.py``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

TITLE_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 2000
SUMMARY_MAX_LENGTH = 800
MAX_TAGS = 5
MAX_SUBTASKS = 10
#: ``POST /ai/edit-todo``: how many tag names one edit may add or remove, and
#: how many subtask operations survive (iteration 5, §2.2).
MAX_EDIT_TAGS = 10
MAX_EDIT_OPS = 10

#: The closed vocabulary of subtask operations; anything else is dropped.
#: Mirrored by ``schemas.EditAction``, which a test pins to this tuple.
EDIT_ACTIONS = ("add", "remove", "rename", "complete", "reopen")

#: The only two fields an edit may empty. ``null`` already means "unchanged",
#: so clearing needs a vocabulary of its own (decision D-IT5-2).
CLEARABLE_FIELDS = ("description", "due_date")
#: A due date further than this from "today" is treated as a hallucination.
DUE_DATE_HORIZON = timedelta(days=5 * 366)

PRIORITIES = ("low", "medium", "high")
DEFAULT_PRIORITY = "medium"

#: master §3.1 — tags are stored trimmed, lowercased, inner whitespace collapsed.
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,29}$")
TAG_MAX_LENGTH = 30

_WHITESPACE_RE = re.compile(r"\s+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")


def strip_control_chars(value: str, *, keep_newlines: bool = False) -> str:
    """Remove Unicode control (``Cc``) and format (``Cf``) characters.

    A model can emit NUL, ANSI escapes, bidi overrides (U+202E) or zero-width
    joiners — none of which belong in a todo title, and some of which make text
    render differently from what it says. ``\\t`` and ``\\n`` are kept when
    ``keep_newlines`` is set (descriptions and summaries may be multi-line);
    ``\\x00`` is always dropped.
    """
    kept = {"\n", "\t"} if keep_newlines else set()
    return "".join(
        char
        for char in value
        if char in kept or unicodedata.category(char) not in ("Cc", "Cf")
    )


def collapse_whitespace(value: str) -> str:
    """Trim and squeeze every run of whitespace into a single space."""
    return _WHITESPACE_RE.sub(" ", value).strip()


def clean_title(value: object, *, max_length: int = TITLE_MAX_LENGTH) -> str | None:
    """Normalize a model-produced title, or ``None`` when it is unusable.

    Collapses whitespace, drops trailing sentence punctuation, then truncates to
    ``max_length`` characters.

    Newlines and tabs are **kept** by the control-character pass so that
    ``collapse_whitespace`` can turn them into a single space: stripping them
    first would weld words together (``"Buy\\nmilk"`` → ``"Buymilk"``).
    """
    if not isinstance(value, str):
        return None
    title = collapse_whitespace(strip_control_chars(value, keep_newlines=True))
    title = title.rstrip(" .")
    if not title:
        return None
    if len(title) > max_length:
        title = title[:max_length].rstrip()
        if not title:
            return None
    return title


def clean_description(
    value: object, *, max_length: int = DESCRIPTION_MAX_LENGTH
) -> str | None:
    """Trim a description and truncate it; blank becomes ``None``."""
    if not isinstance(value, str):
        return None
    description = strip_control_chars(value, keep_newlines=True).strip()
    if not description:
        return None
    if len(description) > max_length:
        description = description[:max_length].rstrip()
    return description or None


def clean_priority(value: object) -> str:
    """Coerce anything that is not ``low``/``medium``/``high`` to ``medium``."""
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in PRIORITIES:
            return candidate
    return DEFAULT_PRIORITY


def clean_due_date(value: object, *, today: date) -> date | None:
    """Parse an ISO date, rejecting garbage and absurdly distant dates."""
    if isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            parsed = date.fromisoformat(candidate)
        except ValueError:
            return None
    else:
        return None

    if abs(parsed - today) > DUE_DATE_HORIZON:
        return None
    return parsed


def normalize_tag(value: object) -> str | None:
    """Apply the master §3.1 tag rules; ``None`` when the tag is invalid."""
    if not isinstance(value, str):
        return None
    tag = collapse_whitespace(value).lower()
    if not tag or len(tag) > TAG_MAX_LENGTH:
        return None
    if not TAG_RE.match(tag):
        return None
    return tag


def clean_tags(values: object, *, limit: int = MAX_TAGS) -> list[str]:
    """Normalize, drop invalid, dedupe (order preserving) and cap tags."""
    if not isinstance(values, (list, tuple)):
        return []
    cleaned: list[str] = []
    for value in values:
        tag = normalize_tag(value)
        if tag is not None and tag not in cleaned:
            cleaned.append(tag)
        if len(cleaned) >= limit:
            break
    return cleaned


def clean_titles(values: object, *, max_items: int) -> list[str]:
    """Sanitize a list of subtask titles: drop empties/duplicates, then cap.

    Duplicate detection is case-insensitive — a 3B model happily repeats the
    same step with different capitalization.
    """
    if not isinstance(values, (list, tuple)):
        return []
    limit = max(0, min(max_items, MAX_SUBTASKS))
    titles: list[str] = []
    seen: set[str] = set()
    for value in values:
        title = clean_title(value)
        if title is None:
            continue
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def clean_summary(value: object, *, max_length: int = SUMMARY_MAX_LENGTH) -> str:
    """Trim, collapse blank lines, and truncate on a word boundary.

    The result is always at most ``max_length`` characters, ellipsis included.
    """
    if not isinstance(value, str):
        return ""
    cleaned = strip_control_chars(value, keep_newlines=True)
    summary = _BLANK_LINES_RE.sub("\n", cleaned.strip())
    summary = "\n".join(line.strip() for line in summary.splitlines()).strip()
    if len(summary) <= max_length:
        return summary

    cut = summary[: max_length - 1]
    boundary = cut.rfind(" ")
    if boundary > max_length // 2:
        cut = cut[:boundary]
    return cut.rstrip(" ,;:.-\n") + "…"


# --------------------------------------------------------------------------- #
# ``POST /ai/edit-todo`` (iteration 5)
#
# Editing has a different safe default from drafting: on this endpoint an
# unusable value must mean "change nothing", never a coerced fallback, because
# every value here becomes a change the user is asked to confirm.
# --------------------------------------------------------------------------- #


def clean_optional_priority(value: object) -> str | None:
    """Like :func:`clean_priority`, except unknown input means **no change**.

    ``clean_priority`` coerces anything unrecognised to ``"medium"``, which is
    right when the model is *drafting* a todo and wrong when it is *editing*
    one: there, a defaulted value invents a priority change out of model
    garbage, and the user is asked to confirm an edit nobody requested. The same
    reasoning already holds for :func:`clean_due_date` (garbage → ``None``,
    which on this endpoint reads as "no change") and for ``completed``, where
    only a real ``bool`` may become a change (see :func:`clean_flag`).
    """
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in PRIORITIES:
            return candidate
    return None


def clean_flag(value: object) -> bool | None:
    """A real JSON boolean, or ``None`` — "no change" — for anything else.

    Deliberately *not* truthiness: ``"no"``, ``0`` and ``""`` are all things a
    3B model emits when it means "I am not touching this", and every one of them
    would flip a todo's ``completed`` state if it were coerced.
    """
    return value if isinstance(value, bool) else None


def clean_clear_fields(values: object) -> set[str]:
    """Keep only the literals the ``clear`` array is allowed to name."""
    if not isinstance(values, (list, tuple)):
        return set()
    named = {
        value.strip().lower() for value in values if isinstance(value, str)
    }
    return named & set(CLEARABLE_FIELDS)


def clean_tag_edits(
    add: object, remove: object, *, limit: int = MAX_EDIT_TAGS
) -> tuple[list[str], list[str]]:
    """Clean both tag lists, then drop any name that appears in **both**.

    A model that asks to add and remove ``health`` in one breath is
    contradicting itself, and neither half of that is a change the user asked
    for; guessing which one it meant would be inventing intent.
    """
    added = clean_tags(add, limit=limit)
    removed = clean_tags(remove, limit=limit)
    contradictory = set(added) & set(removed)
    return (
        [tag for tag in added if tag not in contradictory],
        [tag for tag in removed if tag not in contradictory],
    )


@dataclass(frozen=True, slots=True)
class EditOperation:
    """One sanitized subtask operation, positional and id-free.

    A plain dataclass rather than the wire model: ``app.schemas`` imports this
    module for its bounds, so the dependency only runs one way.
    """

    action: str
    index: int | None
    title: str | None


def clean_index(value: object, *, subtask_count: int) -> int | None:
    """A 1-based subtask position inside the snapshot, or ``None``.

    ``bool`` is excluded explicitly: it is an ``int`` subclass in Python, so
    ``True`` would otherwise sail through as position 1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        index = value
    elif isinstance(value, str):
        try:
            index = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    return index if 1 <= index <= subtask_count else None


def _clean_edit_op(value: object, *, subtask_count: int) -> EditOperation | None:
    if not isinstance(value, dict):
        return None
    raw_action = value.get("action")
    action = raw_action.strip().lower() if isinstance(raw_action, str) else None
    if action not in EDIT_ACTIONS:
        return None

    title = clean_title(value.get("title"))
    if action == "add":
        # An add carries a title and nothing else; any index it came with is
        # meaningless (there is no position for a subtask that does not exist
        # yet) and is dropped rather than trusted.
        return EditOperation("add", None, title) if title else None

    index = clean_index(value.get("index"), subtask_count=subtask_count)
    if index is None:
        return None
    if action == "rename":
        return EditOperation("rename", index, title) if title else None
    return EditOperation(action, index, None)


def clean_edit_ops(
    values: object,
    *,
    subtask_count: int,
    existing_titles: object = (),
    limit: int = MAX_EDIT_OPS,
) -> list[EditOperation]:
    """Sanitize the model's subtask operations against the snapshot it was sent.

    Out-of-range positions are dropped *here* and again in the backend against
    the real subtask list — defence in depth for the one failure mode that would
    otherwise edit the wrong row (risk R3).

    Later operations on an index already targeted by ``remove`` are dropped
    (removing a subtask and then renaming it is not a sequence anyone can
    apply), as is a repeated ``(action, index)`` pair and a repeated ``add`` of
    the same title. Adds are *not* deduplicated by position, because they have
    none: several adds in one edit are legitimate.

    ``existing_titles`` are the snapshot's current subtask titles, and an ``add``
    matching one of them (case-insensitively) is dropped. That is not
    hypothetical tidiness: measured on the live lane, ``qwen2.5:3b`` answered a
    bare "rename it to X" by *re-adding both existing subtasks*, which is the
    one restatement the caller cannot recognise as a no-op — it would duplicate
    them. The cost is that "add a second step called Book it" is refused; the
    benefit is that an accidental duplicate of the user's own data is
    impossible. Renaming a subtask *to* an existing title is untouched, because
    there the position says which row is meant.
    """
    if not isinstance(values, (list, tuple)):
        return []

    ops: list[EditOperation] = []
    removed_indexes: set[int] = set()
    seen: set[tuple[str, int]] = set()
    # Normalized the same way an ``add`` title will be, so "Book it." and
    # "book  it" match the existing "Book it".
    current = existing_titles if isinstance(existing_titles, (list, tuple)) else ()
    seen_added_titles: set[str] = {
        cleaned.casefold()
        for cleaned in (clean_title(title) for title in current)
        if cleaned
    }
    for value in values:
        op = _clean_edit_op(value, subtask_count=subtask_count)
        if op is None:
            continue
        if op.index is None:
            key = (op.title or "").casefold()
            if key in seen_added_titles:
                continue
            seen_added_titles.add(key)
        else:
            if op.index in removed_indexes or (op.action, op.index) in seen:
                continue
            seen.add((op.action, op.index))
            if op.action == "remove":
                removed_indexes.add(op.index)
        ops.append(op)
        if len(ops) >= limit:
            break
    return ops
