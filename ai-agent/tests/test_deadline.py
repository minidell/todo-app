"""One request-wide deadline across every upstream call.

``OLLAMA_TIMEOUT_SECONDS`` is a *per-call* httpx read timeout. Without a shared
deadline an endpoint that fell back on the schema ``format`` and then took a
repair retry could spend three times that budget, blowing past the backend's
50 s and the browser's 60 s ceilings (master §8.3).

The ``settings`` fixture sets ``OLLAMA_TIMEOUT_SECONDS=5``; the fake clock in
``fake_ollama`` makes each upstream call appear to take ``seconds_per_call``.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ollama import CONNECT_TIMEOUT_SECONDS, Deadline, OllamaTimeout
from tests.conftest import AUTH_HEADERS, FakeClock, FakeOllama

BUDGET = 5.0
REQUEST = {"text": "call the dentist", "today": "2026-09-02"}
GOOD_DRAFT = {
    "title": "Call the dentist",
    "description": None,
    "priority": "high",
    "due_date": None,
    "tags": [],
    "subtasks": [],
}


def post(client: TestClient) -> httpx.Response:
    return client.post("/ai/parse-todo", json=REQUEST, headers=AUTH_HEADERS)


class TestDeadlineUnit:
    def test_unbounded_budget_never_expires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("app.ollama._now", clock)
        deadline = Deadline(None)

        clock.advance(10_000)

        assert deadline.remaining is None
        deadline.check()  # does not raise

    def test_remaining_counts_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        monkeypatch.setattr("app.ollama._now", clock)
        deadline = Deadline(10.0)

        clock.advance(4.0)

        assert deadline.remaining == pytest.approx(6.0)
        deadline.check()

    def test_check_raises_once_the_budget_is_spent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("app.ollama._now", clock)
        deadline = Deadline(10.0)

        clock.advance(10.5)

        with pytest.raises(OllamaTimeout):
            deadline.check()

    def test_a_minimum_reserves_budget_for_a_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("app.ollama._now", clock)
        deadline = Deadline(10.0)

        clock.advance(9.5)  # 0.5 s left: enough to be non-zero, not to retry

        deadline.check()
        with pytest.raises(OllamaTimeout):
            deadline.check(minimum=1.0)


class TestRepairRetryIsBudgeted:
    def test_a_slow_first_call_blocks_the_repair_retry(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        fake_ollama.seconds_per_call = BUDGET + 1
        fake_ollama.queue_object({"description": "no title"})  # would trigger repair
        fake_ollama.queue_object(GOOD_DRAFT)

        response = post(client)

        assert response.status_code == 504
        assert response.json() == {
            "detail": "AI request timed out",
            "code": "ai_timeout",
        }
        assert fake_ollama.chat_calls == 1  # the retry never happened

    def test_a_fast_first_call_still_allows_the_repair_retry(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        fake_ollama.seconds_per_call = 0.1
        fake_ollama.queue_object({"description": "no title"})
        fake_ollama.queue_object(GOOD_DRAFT)

        response = post(client)

        assert response.status_code == 200
        assert fake_ollama.chat_calls == 2

    def test_a_retry_is_refused_when_under_a_second_remains(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        """Do not start a call that is certain to be cut off mid-flight."""
        fake_ollama.seconds_per_call = BUDGET - 0.5  # 0.5 s left afterwards
        fake_ollama.queue_object({"description": "no title"})
        fake_ollama.queue_object(GOOD_DRAFT)

        response = post(client)

        assert response.status_code == 504
        assert fake_ollama.chat_calls == 1


class TestSchemaFallbackIsBudgeted:
    def test_a_slow_rejected_call_blocks_the_format_fallback(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        fake_ollama.seconds_per_call = BUDGET + 1
        fake_ollama.queue(httpx.Response(400, json={"error": "invalid format"}))
        fake_ollama.queue_object(GOOD_DRAFT)

        response = post(client)

        assert response.status_code == 504
        assert response.json()["code"] == "ai_timeout"
        assert fake_ollama.chat_calls == 1

    def test_a_fast_rejected_call_still_falls_back(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        fake_ollama.seconds_per_call = 0.1
        fake_ollama.queue(httpx.Response(400, json={"error": "invalid format"}))
        fake_ollama.queue_object(GOOD_DRAFT)

        response = post(client)

        assert response.status_code == 200
        assert fake_ollama.chat_calls == 2


class TestPerCallTimeoutShrinks:
    def test_each_call_is_capped_by_the_remaining_budget(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        """The second call may not use a fresh full-length read timeout."""
        fake_ollama.seconds_per_call = 2.0
        fake_ollama.queue_object({"description": "no title"})
        fake_ollama.queue_object(GOOD_DRAFT)

        assert post(client).status_code == 200

        timeouts = fake_ollama.chat_read_timeouts
        assert len(timeouts) == 2
        assert timeouts[0] == pytest.approx(BUDGET)
        assert timeouts[1] == pytest.approx(BUDGET - 2.0)

    def test_the_connect_ceiling_survives_a_large_remaining_budget(
        self, make_client, fake_ollama: FakeOllama
    ) -> None:
        """A scalar timeout would widen connect to the whole budget."""
        client = make_client(OLLAMA_TIMEOUT_SECONDS=120.0)
        fake_ollama.queue_object(GOOD_DRAFT)

        assert post(client).status_code == 200

        connects = fake_ollama.chat_connect_timeouts
        assert connects == [pytest.approx(CONNECT_TIMEOUT_SECONDS)]
        assert fake_ollama.chat_read_timeouts == [pytest.approx(120.0)]

    def test_connect_shrinks_below_the_ceiling_when_little_budget_is_left(
        self, make_client, fake_ollama: FakeOllama
    ) -> None:
        client = make_client(OLLAMA_TIMEOUT_SECONDS=2.0)
        fake_ollama.queue_object(GOOD_DRAFT)

        assert post(client).status_code == 200

        assert fake_ollama.chat_connect_timeouts == [pytest.approx(2.0)]
