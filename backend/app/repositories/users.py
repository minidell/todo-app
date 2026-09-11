"""SQLAlchemy ``UserRepository``.

Emails are stored **normalized** (trimmed + lowercased) so a plain ``UNIQUE``
constraint gives case-insensitive uniqueness on both engines (D-T1).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


def normalize_email(email: str) -> str:
    return email.strip().lower()


class SqlAlchemyUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == normalize_email(email))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get(self, user_id: UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def create(
        self, email: str, password_hash: str, display_name: str | None
    ) -> User:
        user = User(
            email=normalize_email(email),
            password_hash=password_hash,
            display_name=display_name.strip() if display_name else None,
        )
        self._session.add(user)
        await self._session.flush()
        return user
