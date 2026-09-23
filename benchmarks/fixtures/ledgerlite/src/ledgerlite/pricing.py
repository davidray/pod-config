"""Invoice totals: subtotal, discount, tax, grand total."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ledgerlite.models import Invoice
from ledgerlite.money import Money, total
from ledgerlite.tax import invoice_tax


@dataclass(frozen=True)
class Totals:
    subtotal: Money
    discount: Money
    tax: Money
    grand_total: Money


def subtotal(invoice: Invoice) -> Money:
    return total([i.line_total().rounded() for i in invoice.items], invoice.currency)


def discount_amount(invoice: Invoice, base: Money) -> Money:
    d = invoice.discount
    if d is None:
        return Money.zero(invoice.currency)
    if d.kind == "percent":
        if not (Decimal("0") <= d.value <= Decimal("100")):
            raise ValueError("percent discount must be between 0 and 100")
        return base.times(d.value / Decimal("100")).rounded()
    elif d.kind == "fixed":
        amount = Money(d.value, invoice.currency)
        # A fixed discount can never exceed the invoice amount.
        return amount if amount.amount <= base.amount else base
    elif d.kind == "bulk":
        if invoice.total_quantity() >= d.min_quantity:
            return base.times(d.value / Decimal("100")).rounded()
        return Money.zero(invoice.currency)
    else:
        raise ValueError(f"unknown discount kind {d.kind!r}")


def compute_totals(invoice: Invoice) -> Totals:
    sub = subtotal(invoice)
    disc = discount_amount(invoice, sub)
    ratio = (sub.amount - disc.amount) / sub.amount if sub.amount else Decimal("1")
    tax = invoice_tax(invoice, ratio)
    grand = sub - disc + tax
    return Totals(subtotal=sub, discount=disc, tax=tax, grand_total=grand)
