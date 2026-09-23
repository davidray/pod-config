"""Regional sales tax."""

from __future__ import annotations

from decimal import Decimal

from ledgerlite.models import Invoice
from ledgerlite.money import Money, total

RATES: dict[str, Decimal] = {
    "US-CA": Decimal("0.0725"),
    "US-TX": Decimal("0.0625"),
    "US-NY": Decimal("0.04"),
    "US-OR": Decimal("0"),
    "EU-DE": Decimal("0.19"),
    "EU-NL": Decimal("0.21"),
}


class UnknownRegion(KeyError):
    pass


def rate_for(region: str) -> Decimal:
    try:
        return RATES[region]
    except KeyError as e:
        raise UnknownRegion(region) from e


def invoice_tax(invoice: Invoice, discount_ratio: Decimal = Decimal("1")) -> Money:
    """Tax owed on the taxable items of an invoice.

    `discount_ratio` scales the taxable base when an invoice-level discount
    applies (tax is charged on the discounted price).
    """
    rate = rate_for(invoice.customer.region)
    taxable = total([i.line_total() for i in invoice.items if i.taxable], invoice.currency)
    return taxable.times(discount_ratio).times(rate).rounded()
