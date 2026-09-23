"""In-memory invoice storage."""

from __future__ import annotations

from ledgerlite.models import Invoice, InvoiceStatus
from ledgerlite.pagination import Page, paginate


class DuplicateInvoice(ValueError):
    pass


class InvoiceRepository:
    def __init__(self) -> None:
        self._invoices: dict[str, Invoice] = {}

    def add(self, invoice: Invoice) -> None:
        if invoice.number in self._invoices:
            raise DuplicateInvoice(invoice.number)
        self._invoices[invoice.number] = invoice

    def get(self, number: str) -> Invoice | None:
        return self._invoices.get(number)

    def all(self) -> list[Invoice]:
        return sorted(self._invoices.values(), key=lambda i: (i.issued, i.number))

    def list(self, page: int = 1, per_page: int = 20, status: InvoiceStatus | None = None) -> Page[Invoice]:
        rows = self.all()
        if status is not None:
            rows = [i for i in rows if i.status == status]
        return paginate(rows, page=page, per_page=per_page)
