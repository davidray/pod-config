"""Offset pagination shared by repositories and the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

MAX_PER_PAGE = 100


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    page: int
    per_page: int
    total_items: int

    @property
    def total_pages(self) -> int:
        if self.total_items == 0:
            return 1
        return self.total_items // self.per_page

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


def paginate(items: list[T], page: int = 1, per_page: int = 20) -> Page[T]:
    if page < 1:
        raise ValueError("page must be >= 1")
    if not 1 <= per_page <= MAX_PER_PAGE:
        raise ValueError(f"per_page must be between 1 and {MAX_PER_PAGE}")
    start = (page - 1) * per_page
    return Page(items=items[start:start + per_page], page=page, per_page=per_page, total_items=len(items))
