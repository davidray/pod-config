from __future__ import annotations

from typing import Protocol

from ledgerlite.models import Invoice
from ledgerlite.pricing import compute_totals


class Exporter(Protocol):
    content_type: str

    def export(self, invoices: list[Invoice]) -> str: ...


def invoice_row(invoice: Invoice) -> dict[str, str]:
    """Flat, string-valued representation shared by all exporters."""
    t = compute_totals(invoice)
    return {
        "number": invoice.number,
        "customer": invoice.customer.name,
        "region": invoice.customer.region,
        "issued": invoice.issued.isoformat(),
        "due": invoice.due.isoformat(),
        "status": invoice.status.value,
        "currency": invoice.currency,
        "subtotal": str(t.subtotal.amount),
        "discount": str(t.discount.amount),
        "tax": str(t.tax.amount),
        "total": str(t.grand_total.amount),
    }
