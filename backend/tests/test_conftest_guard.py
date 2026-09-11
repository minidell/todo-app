"""The test harness drops every table — make sure it can only do so safely."""

import pytest

from tests.conftest import SQLITE_URL, assert_safe_to_wipe


@pytest.mark.parametrize(
    "url",
    [
        SQLITE_URL,
        "postgresql+asyncpg://todo:todo@localhost:5433/todo_test",
        "postgresql+asyncpg://postgres:pw@127.0.0.1:5434/todo_test",
        "postgresql+asyncpg://u:p@host:5432/anything_test",
    ],
)
def test_test_databases_are_allowed(url: str) -> None:
    assert_safe_to_wipe(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://todo:todo@localhost:5433/todo",
        "postgresql+asyncpg://todo:todo@prod.example.com:5432/production",
        "postgresql+asyncpg://u:p@host:5432/todo_testing",
        "postgresql+asyncpg://u:p@host:5432/",
        "sqlite+aiosqlite:///./app.db",
    ],
)
def test_non_test_databases_are_refused(url: str) -> None:
    with pytest.raises(RuntimeError, match="Refusing to run the test suite"):
        assert_safe_to_wipe(url)


def test_the_refusal_names_the_database_but_not_the_credentials() -> None:
    url = "postgresql+asyncpg://todo:sup3rs3cret@localhost:5432/todo"
    with pytest.raises(RuntimeError) as excinfo:
        assert_safe_to_wipe(url)
    message = str(excinfo.value)
    assert "'todo'" in message
    assert "sup3rs3cret" not in message
