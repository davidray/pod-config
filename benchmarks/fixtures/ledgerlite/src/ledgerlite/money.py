"""Currency-aware money values backed by Decimal."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

CENT = Decimal("0.01")


class CurrencyMismatch(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Money:
    amount: Decimal
    currency: str = "USD"

    @classmethod
    def of(cls, value: str | int | Decimal, currency: str = "USD") -> Money:
        return cls(Decimal(str(value)), currency)

    @classmethod
    def zero(cls, currency: str = "USD") -> Money:
        return cls(Decimal("0"), currency)

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(f"{self.currency} != {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount - other.amount, self.currency)

    def times(self, factor: Decimal | int | str) -> Money:
        return Money(self.amount * Decimal(str(factor)), self.currency)

    def rounded(self) -> Money:
        """Round to whole cents."""
        return Money(self.amount.quantize(CENT, rounding=ROUND_HALF_EVEN), self.currency)

    def is_negative(self) -> bool:
        return self.amount < 0

    def __str__(self) -> str:
        return f"{self.amount.quantize(CENT)} {self.currency}"


def total(values: list[Money], currency: str = "USD") -> Money:
    result = Money.zero(currency)
    for v in values:
        result = result + v
    return result
