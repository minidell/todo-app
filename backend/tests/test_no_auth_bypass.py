"""D-S1: the slice-1 auth bypass is gone, and nothing can reintroduce it.

The 401 behaviour is tested through the API in ``test_auth_api.py``. What this
module adds is structural: a new endpoint added later must *fail this test*
rather than quietly ship without an owner check.
"""

import importlib
from pathlib import Path

import pytest

from app.deps import get_current_user
from tests.conftest import iter_api_routes, iter_dependencies

BACKEND_ROOT = Path(__file__).resolve().parent.parent

#: The only endpoints that may be served without a verified token
#: (master §6: health probes + the two ways to get a token in the first place).
PUBLIC_PATHS = {
    "/api/health",
    "/api/health/ready",
    "/api/auth/register",
    "/api/auth/login",
    # FastAPI's own dev-only documentation endpoints.
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/openapi.json",
}


def _source_files() -> list[Path]:
    """Every shipped Python file: the application and its migrations.

    ``tests/`` is excluded on purpose — a test may *name* the forbidden strings
    precisely in order to assert they are gone (this module, and the "a
    leftover AUTH_ENABLED env var is ignored" test in ``test_config.py``).
    """
    return [
        path
        for directory in ("app", "migrations")
        for path in (BACKEND_ROOT / directory).rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def test_auth_enabled_no_longer_exists_in_the_backend() -> None:
    """The flag is not "set to true" — it is deleted, so it cannot be flipped."""
    offenders = [
        str(path.relative_to(BACKEND_ROOT))
        for path in _source_files()
        if "AUTH_ENABLED" in path.read_text()
    ]
    assert offenders == []


def test_the_bootstrap_local_user_module_is_gone() -> None:
    assert not (BACKEND_ROOT / "app" / "bootstrap.py").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.bootstrap")


def test_no_source_file_mentions_the_local_bootstrap_user() -> None:
    offenders = [
        str(path.relative_to(BACKEND_ROOT))
        for path in _source_files()
        if "local@todo.app" in path.read_text()
    ]
    assert offenders == []


def test_settings_has_no_auth_toggle() -> None:
    from app.config import Settings

    assert "AUTH_ENABLED" not in Settings.model_fields


def test_the_route_walk_reaches_every_router(app) -> None:
    """Guards the guard below.

    ``app.routes`` holds an included router as one opaque entry, so walking it
    directly sees only the endpoints declared on the app itself — the two
    health probes — and the protection check below would pass without ever
    looking at /api/auth, /api/lists or /api/todos.
    """
    paths = {route.path for route in iter_api_routes(app)}
    assert {
        "/api/auth/me",
        "/api/lists",
        "/api/lists/{list_id}",
        "/api/todos",
        "/api/todos/{todo_id}",
    } <= paths


def test_every_non_public_route_depends_on_get_current_user(app) -> None:
    unprotected: list[tuple[str, list[str]]] = []

    for route in iter_api_routes(app):
        if route.path in PUBLIC_PATHS:
            continue
        calls = {
            dependency.call for dependency in iter_dependencies(route.dependant)
        }
        if get_current_user not in calls:
            unprotected.append((route.path, sorted(route.methods or [])))

    assert unprotected == []
