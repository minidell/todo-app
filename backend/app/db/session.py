"""Async engine + sessionmaker factories (master spec §4.3).

SQLite (the default test lane) needs a ``StaticPool`` so an ``:memory:``
database survives across connections, and an explicit ``PRAGMA
foreign_keys=ON`` — without it the FK cascades the cascade tests rely on
silently do nothing.
"""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


def is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite")


def create_engine_from_url(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an ``AsyncEngine`` tuned for the dialect behind ``url``."""
    if is_sqlite_url(url):
        engine = create_async_engine(
            url,
            echo=echo,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    return create_async_engine(
        url,
        echo=echo,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    """Sessions never expire on commit — responses are serialised after it."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
