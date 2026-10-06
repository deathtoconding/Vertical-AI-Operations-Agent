"""Repository base.

Repositories are the only place that knows about SQL. They translate between ORM rows and
domain models, so the domain layer never imports SQLAlchemy and the API never sees a row
object. That separation is what lets the domain be tested without a database and the API be
tested without a domain fixture.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.base import ORMBackedModel

RowT = TypeVar("RowT")
DomainT = TypeVar("DomainT", bound=ORMBackedModel)


class BaseRepository(Generic[RowT, DomainT]):
    """Generic create/read/update helpers shared by every repository."""

    row_class: type[RowT]
    domain_class: type[DomainT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- mapping ----------------------------------------------------------- #

    def to_domain(self, row: RowT) -> DomainT:
        return self.domain_class.model_validate(row, from_attributes=True)

    def to_domain_list(self, rows: list[RowT] | tuple[RowT, ...]) -> list[DomainT]:
        return [self.to_domain(row) for row in rows]

    def apply(self, row: RowT, values: dict[str, Any]) -> RowT:
        """Assign only the keys present in ``values`` — used for partial updates."""
        for key, value in values.items():
            setattr(row, key, value)
        return row

    # -- queries ----------------------------------------------------------- #

    async def get_row(self, identifier: str) -> RowT | None:
        result = await self.session.execute(
            select(self.row_class).where(self.row_class.id == identifier)  # type: ignore[attr-defined]
        )
        return result.scalar_one_or_none()

    async def get(self, identifier: str) -> DomainT | None:
        row = await self.get_row(identifier)
        return self.to_domain(row) if row is not None else None

    async def add(self, row: RowT) -> RowT:
        self.session.add(row)
        await self.session.flush()
        return row

    async def count(self) -> int:
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count()).select_from(self.row_class)  # type: ignore[arg-type]
        )
        return int(result.scalar_one())
