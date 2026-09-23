"""Domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

from ledgerlite.money import Money


class InvoiceStatus(str, Enum):
    DRAFT = "draft"
    SENT = "sent"
    PAID = "paid"
    VOID = "void"


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    region: str  # e.g. "US-CA", "US-TX", "EU-DE"
    email: str | None = None


@dataclass(frozen=True)
class LineItem:
    description: str
    quantity: Decimal
    unit_price: Money
    taxable: bool = True

    def line_total(self) -> Money:
        return self.unit_price.times(self.quantity)


@dataclass(frozen=True)
class Discount:
    kind: str            # "percent" | "fixed" | "bulk"
    value: Decimal       # percent (0-100), fixed amount, or bulk percent
    min_quantity: int = 0  # only for "bulk"


@dataclass
class Invoice:
    number: str
    customer: Customer
    issued: date
    due: date
    items: list[LineItem] = field(default_factory=list)
    discount: Discount | None = None
    status: InvoiceStatus = InvoiceStatus.DRAFT
    currency: str = "USD"

    def add_item(self, item: LineItem) -> None:
        if item.unit_price.currency != self.currency:
            raise ValueError(f"line item currency {item.unit_price.currency} != invoice {self.currency}")
        self.items.append(item)

    def total_quantity(self) -> Decimal:
        return sum((i.quantity for i in self.items), Decimal("0"))
