"""Live measurement of edit-by-instruction (iteration 5, spec §3.1 A6).

Skipped unless ``AI_AGENT_LIVE=1``; never part of CI. Like ``test_live_dates``
it measures *the model*, not the contract — everything the contract promises is
already pinned by ``test_edit_todo.py`` against a mocked Ollama.

It asserts **structure, never wording**: whether a rename produced a title at
all, whether an add produced an ``add`` operation, whether an out-of-scope
request produced the empty change set. What the model *called* the renamed todo
is its business; the user reads the draft and confirms it before anything is
written (master D-AI1).

Acceptance is the aggregate: at least 2 of the 3 cases (spec §3.1 A6). A lower
score is **not** a merge blocker — the documented fallback is documentation
(risk R10): the score goes into ``DASHBOARD.md`` and feeds backlog row IT5-2
(``qwen2.5:7b`` or a deterministic pre-parser).

Run it, from ``ai-agent/``::

    docker run -d --name todo-app-ollama-it5 \\
        -v todo-app-ollama:/root/.ollama -p 127.0.0.1:11436:11434 ollama/ollama:latest
    AI_AGENT_LIVE=1 OLLAMA_BASE_URL=http://localhost:11436 \\
        OLLAMA_MODEL=qwen2.5:3b OLLAMA_TIMEOUT_SECONDS=240 \\
        uv run pytest tests/test_live_edit.py -v -s
    docker rm -f todo-app-ollama-it5

Note the port: host 11434 belongs to another project's Ollama (master D-P2),
11435 is used by ``test_live_ollama.py`` and 11436 by ``test_live_dates.py`` —
one container on 11436 can serve both lanes. Never remove the
``todo-app-ollama`` volume; it holds the pulled model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

pytestmark = pytest.mark.skipif(
    os.getenv("AI_AGENT_LIVE") != "1",
    reason="live Ollama test; set AI_AGENT_LIVE=1 (and OLLAMA_BASE_URL) to run it",
)

LIVE_TOKEN = "live-test-token-0123456789abcdef0123"
HEADERS = {"X-Internal-Token": LIVE_TOKEN}

TODAY = date(2026, 9, 2)

#: The bar from spec §3.1 A6. Recorded in DASHBOARD.md at merge time.
REQUIRED_SCORE = 2

#: One realistic todo, reused by every case: two subtasks (so positions 1 and 2
#: exist and 3 does not), one tag, no due date. Also the closest thing this lane
#: has to a token-budget check (risk R9).
SNAPSHOT: dict[str, Any] = {
    "title": "Dentist",
    "description": "The one on Market Street",
    "priority": "medium",
    "due_date": None,
    "completed": False,
    "tags": ["health"],
    "subtasks": [
        {"title": "Book it", "completed": False},
        {"title": "Pay the invoice", "completed": True},
    ],
}


def _field_changes(body: dict[str, Any]) -> dict[str, Any]:
    """Every field entry that is not "no change"."""
    return {
        key: value
        for key, value in body.items()
        if key != "subtasks" and value not in (None, False, [])
    }


def _only_the_title_changed(body: dict[str, Any]) -> bool:
    title = body.get("title")
    return (
        isinstance(title, str)
        and "dentist" in title.casefold()
        and set(_field_changes(body)) == {"title"}
        and body["subtasks"] == []
    )


def _one_step_was_added(body: dict[str, Any]) -> bool:
    adds = [op for op in body["subtasks"] if op["action"] == "add"]
    return (
        len(adds) >= 1
        and all(op["index"] is None for op in adds)
        and body["title"] is None
    )


def _nothing_at_all_changed(body: dict[str, Any]) -> bool:
    return _field_changes(body) == {} and body["subtasks"] == []


@dataclass(frozen=True, slots=True)
class Case:
    label: str
    instruction: str
    check: Callable[[dict[str, Any]], bool]
    why: str


CASES = [
    Case(
        label="rename",
        instruction="rename it to Call the dentist",
        check=_only_the_title_changed,
        why="a bare rename must set title and touch nothing else",
    ),
    Case(
        label="add a subtask",
        instruction="add a step to find the phone number",
        check=_one_step_was_added,
        why="an add carries a title and no index; the todo's own fields stay put",
    ),
    Case(
        label="out of scope",
        instruction="delete this todo and make one about the car",
        check=_nothing_at_all_changed,
        why="nothing here is expressible in the schema, so the answer is empty",
    ),
]


@dataclass(frozen=True, slots=True)
class Result:
    case: Case
    body: dict[str, Any] | None
    status_code: int

    @property
    def ok(self) -> bool:
        return self.body is not None and self.case.check(self.body)

    @property
    def verdict(self) -> str:
        return "PASS" if self.ok else "FAIL"

    @property
    def summary(self) -> str:
        """A one-line rendering of what the model actually proposed."""
        if self.body is None:
            return f"HTTP {self.status_code}"
        fields = _field_changes(self.body)
        ops = [
            f"{op['action']}({op['index'] if op['index'] is not None else op['title']})"
            for op in self.body["subtasks"]
        ]
        return f"{fields or '{}'} {ops}"[:60]


@pytest.fixture(scope="module")
def live_client() -> Iterator[TestClient]:
    settings = Settings(
        OLLAMA_BASE_URL=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11436"),
        OLLAMA_MODEL=os.environ.get("OLLAMA_MODEL", "qwen2.5:3b"),
        OLLAMA_TIMEOUT_SECONDS=float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "240")),
        AI_AGENT_TOKEN=LIVE_TOKEN,
        LOG_LEVEL="warning",
        APP_ENV="dev",
    )
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture(scope="module")
def results(live_client: TestClient) -> list[Result]:
    """Run all three instructions once; module scope keeps the model resident."""
    collected: list[Result] = []
    for case in CASES:
        response = live_client.post(
            "/ai/edit-todo",
            json={
                "instruction": case.instruction,
                "today": TODAY.isoformat(),
                "known_tags": ["health", "home", "work"],
                "todo": SNAPSHOT,
            },
            headers=HEADERS,
        )
        body = response.json() if response.status_code == 200 else None
        collected.append(
            Result(case=case, body=body, status_code=response.status_code)
        )
    return collected


def _table(results: list[Result]) -> str:
    lines = [
        "",
        f"Edit by instruction — model={os.environ.get('OLLAMA_MODEL', 'qwen2.5:3b')}",
        f"{'instruction':<46} {'proposed':<62} verdict",
        "-" * 118,
    ]
    for result in results:
        lines.append(
            f"{result.case.instruction:<46} {result.summary:<62} {result.verdict}"
        )
    score = sum(1 for result in results if result.ok)
    lines.append("-" * 118)
    lines.append(f"score: {score}/{len(results)} (bar: {REQUIRED_SCORE})")
    return "\n".join(lines)


def test_every_case_reached_the_model(results: list[Result]) -> None:
    """A 503 or a timeout is infrastructure, not a model score."""
    broken = [result for result in results if result.status_code != 200]

    assert not broken, "\n".join(
        f"{result.case.instruction!r} -> HTTP {result.status_code}"
        for result in broken
    )


def test_the_schema_survived_constrained_decoding(results: list[Result]) -> None:
    """Spec §6 open question 2, answered by running it.

    ``EDIT_TODO_SCHEMA`` puts ``null`` in an ``enum``; if Ollama rejects that,
    ``chat_json`` silently falls back to ``format: "json"`` and every clamp
    still applies. Either way the *response* must be the documented shape, so
    that is what is asserted — this test failing means something worse than a
    rejected schema.
    """
    for result in results:
        assert result.body is not None
        assert set(result.body) == {
            "title",
            "description",
            "clear_description",
            "priority",
            "due_date",
            "clear_due_date",
            "completed",
            "tags_add",
            "tags_remove",
            "subtasks",
        }


def test_no_operation_names_a_subtask_that_does_not_exist(
    results: list[Result],
) -> None:
    """The D-IT5-3 property, measured rather than assumed.

    Asserted for every case regardless of the aggregate score: a wrong *edit*
    is a draft the user can reject, but an operation pointing at a position
    outside the snapshot is a bug in this service, not a model opinion.
    """
    for result in results:
        assert result.body is not None
        for operation in result.body["subtasks"]:
            index = operation["index"]
            if operation["action"] == "add":
                assert index is None
            else:
                assert index in (1, 2), operation


def test_edit_by_instruction_meets_the_bar(results: list[Result]) -> None:
    """Acceptance: at least 2 of 3 (spec §3.1 A6).

    The table prints either way — a passing 2/3 still tells the Team Lead which
    instruction is the weak one, and that is what goes into DASHBOARD.md.
    """
    print(_table(results))

    score = sum(1 for result in results if result.ok)
    missed = [f"{r.case.label}: {r.case.why}" for r in results if not r.ok]

    assert score >= REQUIRED_SCORE, (
        f"edit-by-instruction score {score}/{len(results)} is below the "
        f"{REQUIRED_SCORE}/3 bar.\nMissed: " + "; ".join(missed) + "\n" + _table(results)
    )
