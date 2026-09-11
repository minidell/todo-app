"""Carry-over C5: interactive docs exist in dev only."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app

DOC_PATHS = ["/docs", "/redoc", "/openapi.json"]


async def _get_all(settings, paths: list[str]) -> dict[str, int]:
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return {path: (await client.get(path)).status_code for path in paths}


@pytest.mark.parametrize("path", DOC_PATHS)
async def test_docs_are_served_in_dev(test_settings, path: str) -> None:
    settings = test_settings.model_copy(update={"APP_ENV": "dev"})
    statuses = await _get_all(settings, [path])
    assert statuses[path] == 200


async def test_docs_are_404_outside_dev(test_settings) -> None:
    settings = test_settings.model_copy(update={"APP_ENV": "prod"})
    statuses = await _get_all(settings, DOC_PATHS)
    assert statuses == {path: 404 for path in DOC_PATHS}
