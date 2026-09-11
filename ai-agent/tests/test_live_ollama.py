"""Live integration lane — skipped unless ``AI_AGENT_LIVE=1`` (slice-4 B6.7).

These tests talk to a **real** Ollama with a real model, so they are slow
(20-60 s per call on CPU) and are never part of CI. They assert only that the
contract holds — the shape validates and the values are sane. They must never
assert on the model's wording; that would make the suite a coin flip.

Run it, from ``ai-agent/``::

    docker run -d --name todo-app-ollama-aitest \\
        -v todo-app-ollama:/root/.ollama -p 127.0.0.1:11435:11434 ollama/ollama:latest
    AI_AGENT_LIVE=1 OLLAMA_BASE_URL=http://localhost:11435 \\
        OLLAMA_MODEL=qwen2.5:3b AI_AGENT_TOKEN=live-test-token-0123456789abcdef0123 \\
        uv run pytest tests/test_live_ollama.py -v -s
    docker rm -f todo-app-ollama-aitest

Note the port: host 11434 belongs to another project's Ollama (master D-P2).
"""

from __future__ import annotations

import os
from datetime import date
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.sanitize import PRIORITIES

pytestmark = pytest.mark.skipif(
    os.getenv("AI_AGENT_LIVE") != "1",
    reason="live Ollama test; set AI_AGENT_LIVE=1 (and OLLAMA_BASE_URL) to run it",
)

LIVE_TOKEN = "live-test-token-0123456789abcdef0123"
TODAY = date(2026, 9, 2)
HEADERS = {"X-Internal-Token": LIVE_TOKEN}


@pytest.fixture(scope="module")
def live_client() -> Iterator[TestClient]:
    settings = Settings(
        OLLAMA_BASE_URL=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11435"),
        OLLAMA_MODEL=os.environ.get("OLLAMA_MODEL", "qwen2.5:3b"),
        OLLAMA_TIMEOUT_SECONDS=float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "180")),
        AI_AGENT_TOKEN=LIVE_TOKEN,
        LOG_LEVEL="info",
        APP_ENV="dev",
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_health_sees_the_model(live_client: TestClient) -> None:
    body = live_client.get("/health").json()
    print("\n/health ->", body)

    assert body["ollama"] == "ok"
    assert body["model_present"] is True


def test_parse_todo_against_a_real_model(live_client: TestClient) -> None:
    response = live_client.post(
        "/ai/parse-todo",
        json={
            "text": "call the dentist tomorrow morning, urgent",
            "today": TODAY.isoformat(),
            "known_tags": ["home", "health", "work"],
        },
        headers=HEADERS,
    )
    print("\n/ai/parse-todo ->", response.json())

    assert response.status_code == 200
    body = response.json()
    assert body["title"].strip()
    assert len(body["title"]) <= 200
    assert body["priority"] in PRIORITIES
    assert len(body["tags"]) <= 5
    assert len(body["subtasks"]) <= 5


def test_suggest_subtasks_against_a_real_model(live_client: TestClient) -> None:
    response = live_client.post(
        "/ai/suggest-subtasks",
        json={
            "title": "Plan a long weekend in Lisbon",
            "description": "Travelling with two friends in October",
            "max_items": 5,
        },
        headers=HEADERS,
    )
    print("\n/ai/suggest-subtasks ->", response.json())

    assert response.status_code == 200
    subtasks = response.json()["subtasks"]
    assert len(subtasks) <= 5
    assert all(item["title"].strip() for item in subtasks)


def test_suggest_metadata_against_a_real_model(live_client: TestClient) -> None:
    response = live_client.post(
        "/ai/suggest-metadata",
        json={
            "title": "File the quarterly tax return before the deadline",
            "description": "The deadline is in three days",
            "known_tags": ["home", "work", "finance"],
            "today": TODAY.isoformat(),
        },
        headers=HEADERS,
    )
    print("\n/ai/suggest-metadata ->", response.json())

    assert response.status_code == 200
    body = response.json()
    assert body["priority"] in PRIORITIES
    assert len(body["tags"]) <= 5


def test_daily_summary_against_a_real_model(live_client: TestClient) -> None:
    response = live_client.post(
        "/ai/daily-summary",
        json={
            "today": TODAY.isoformat(),
            "todos": [
                {
                    "title": "File the quarterly tax return",
                    "priority": "high",
                    "due_date": "2026-09-01",
                    "completed": False,
                    "list_name": "Work",
                },
                {
                    "title": "Call the dentist",
                    "priority": "medium",
                    "due_date": "2026-09-02",
                    "completed": False,
                    "list_name": "Inbox",
                },
                {
                    "title": "Buy milk",
                    "priority": "low",
                    "due_date": None,
                    "completed": False,
                    "list_name": "Inbox",
                },
            ],
        },
        headers=HEADERS,
    )
    print("\n/ai/daily-summary ->", response.json())

    assert response.status_code == 200
    summary = response.json()["summary"]
    assert summary.strip()
    assert len(summary) <= 800
