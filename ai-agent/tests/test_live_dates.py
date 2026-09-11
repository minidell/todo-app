"""Live measurement of relative-date resolution (iteration 4, IT3-8).

Skipped unless ``AI_AGENT_LIVE=1``; never part of CI. This is the *only* test
in the repository that measures model quality rather than contract shape, and
it exists because backlog row IT3-8 was opened by an observation ("relative
dates beyond tomorrow are often wrong") that nothing could re-check.

It asserts **dates, never wording**: each note has one deterministic expected
answer that follows from the DATES block in ``prompts.date_anchors``, so a
failure means the model did not copy the label it was told to copy.

Acceptance is the **aggregate**: at least 4 of the 5 cases. Individual cases
are reported in a table rather than asserted, because a single flip on a 3B
model at temperature 0.2 is noise, and reddening the suite on noise would only
teach the next agent to skip the lane. If the bar is missed the documented
fallback (spec §2 A6, risk R8) is documentation, not a blocked merge: the AI
draft is editable before anything is written (master D-AI1).

Run it, from ``ai-agent/``::

    docker run -d --name todo-app-ollama-it4 \\
        -v todo-app-ollama:/root/.ollama -p 127.0.0.1:11436:11434 ollama/ollama:latest
    AI_AGENT_LIVE=1 OLLAMA_BASE_URL=http://localhost:11436 \\
        OLLAMA_MODEL=qwen2.5:3b OLLAMA_TIMEOUT_SECONDS=240 \\
        uv run pytest tests/test_live_dates.py -v -s
    docker rm -f todo-app-ollama-it4

Note the port: host 11434 belongs to another project's Ollama (master D-P2),
and 11435 is used by ``test_live_ollama.py``. Never remove the
``todo-app-ollama`` volume — it holds the pulled model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Iterator

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

#: A Wednesday. Every expectation below is derived from this and nothing else,
#: so the table stays valid whatever day the lane is actually run on.
TODAY = date(2026, 9, 2)

#: The bar from spec §2 A6. Recorded in DASHBOARD.md at merge time.
REQUIRED_SCORE = 4


@dataclass(frozen=True, slots=True)
class Case:
    """One note with a deterministic expected answer.

    ``expected`` is a set so "next week" can accept the whole week it might
    reasonably mean — the English is genuinely fuzzy there, and pinning it to
    Monday would measure our opinion rather than the model's arithmetic.
    """

    label: str
    note: str
    expected: frozenset[date]
    why: str


def _week(start: date, days: int) -> frozenset[date]:
    return frozenset(date.fromordinal(start.toordinal() + n) for n in range(days))


CASES = [
    Case(
        label="next weekday",
        note="call the accountant next Tuesday",
        expected=frozenset({date(2026, 9, 8)}),
        why="NEXT_TUESDAY, strictly after Wednesday 2026-09-02",
    ),
    Case(
        label="in N days (no label)",
        note="return the library books in 10 days",
        expected=frozenset({date(2026, 9, 12)}),
        why="no IN_10_DAYS label exists, so this one must be counted from TODAY",
    ),
    Case(
        label="day after tomorrow",
        note="water the plants the day after tomorrow",
        expected=frozenset({date(2026, 9, 4)}),
        why="DAY_AFTER_TOMORROW",
    ),
    Case(
        label="bare weekday",
        note="submit the timesheet on friday",
        expected=frozenset({date(2026, 9, 4)}),
        why="NEXT_FRIDAY; a bare weekday means the next one",
    ),
    Case(
        label="next week",
        note="start the new training plan next week",
        expected=_week(date(2026, 9, 7), 7),
        why="NEXT_WEEK is Monday 2026-09-07; anywhere in that week is accepted",
    ),
]


@dataclass(frozen=True, slots=True)
class Result:
    case: Case
    resolved: date | None
    status_code: int

    @property
    def ok(self) -> bool:
        return self.resolved in self.case.expected

    @property
    def verdict(self) -> str:
        return "PASS" if self.ok else "FAIL"


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
    """Run all five notes once; the module scope keeps the model resident."""
    collected: list[Result] = []
    for case in CASES:
        response = live_client.post(
            "/ai/parse-todo",
            json={
                "text": case.note,
                "today": TODAY.isoformat(),
                "known_tags": ["home", "work", "health"],
            },
            headers=HEADERS,
        )
        resolved: date | None = None
        if response.status_code == 200:
            raw = response.json().get("due_date")
            resolved = date.fromisoformat(raw) if raw else None
        collected.append(
            Result(case=case, resolved=resolved, status_code=response.status_code)
        )
    return collected


def _table(results: list[Result]) -> str:
    lines = [
        "",
        f"Relative-date resolution — TODAY={TODAY.isoformat()} ({TODAY:%A}), "
        f"model={os.environ.get('OLLAMA_MODEL', 'qwen2.5:3b')}",
        f"{'note':<42} {'expected':<24} {'resolved':<12} verdict",
        "-" * 92,
    ]
    for result in results:
        expected = sorted(result.case.expected)
        shown = (
            expected[0].isoformat()
            if len(expected) == 1
            else f"{expected[0].isoformat()}..{expected[-1].isoformat()}"
        )
        lines.append(
            f"{result.case.note:<42} {shown:<24} "
            f"{(result.resolved.isoformat() if result.resolved else 'null'):<12} "
            f"{result.verdict}"
        )
    score = sum(1 for result in results if result.ok)
    lines.append("-" * 92)
    lines.append(f"score: {score}/{len(results)} (bar: {REQUIRED_SCORE})")
    return "\n".join(lines)


def test_every_case_reached_the_model(results: list[Result]) -> None:
    """A 503 or a timeout is an infrastructure failure, not a model score.

    Without this the aggregate test below could "fail the model" for a stopped
    container, which is exactly the misdiagnosis IT3-8 started as.
    """
    broken = [result for result in results if result.status_code != 200]

    assert not broken, "\n".join(
        f"{result.case.note!r} -> HTTP {result.status_code}" for result in broken
    )


def test_relative_dates_meet_the_bar(results: list[Result]) -> None:
    """The IT3-8 acceptance criterion: at least 4 of 5 (spec §2 A6).

    The table is printed either way — a passing 4/5 still tells the Team Lead
    which case is the weak one, and that is what gets recorded in DASHBOARD.md.
    """
    print(_table(results))

    score = sum(1 for result in results if result.ok)
    failed = [f"{r.case.label}: {r.case.why}" for r in results if not r.ok]

    assert score >= REQUIRED_SCORE, (
        f"relative-date score {score}/{len(results)} is below the "
        f"{REQUIRED_SCORE}/5 bar.\nMissed: " + "; ".join(failed) + "\n" + _table(results)
    )
