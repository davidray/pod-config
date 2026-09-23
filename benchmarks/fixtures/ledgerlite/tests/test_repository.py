from datetime import date

import pytest

from ledgerlite.models import InvoiceStatus
from ledgerlite.repository import DuplicateInvoice, InvoiceRepository


def test_add_get_and_duplicates(invoice_factory):
    repo = InvoiceRepository()
    inv = invoice_factory(number="INV-9")
    repo.add(inv)
    assert repo.get("INV-9") is inv
    with pytest.raises(DuplicateInvoice):
        repo.add(invoice_factory(number="INV-9"))


def test_list_is_sorted_and_filtered(invoice_factory):
    repo = InvoiceRepository()
    repo.add(invoice_factory(number="B", issued=date(2026, 2, 1)))
    repo.add(invoice_factory(number="A", issued=date(2026, 1, 1), status=InvoiceStatus.PAID))
    assert [i.number for i in repo.list().items] == ["A", "B"]
    assert [i.number for i in repo.list(status=InvoiceStatus.PAID).items] == ["A"]


def test_list_exact_pages(invoice_factory):
    repo = InvoiceRepository()
    for n in range(40):
        repo.add(invoice_factory(number=f"INV-{n:03d}"))
    page = repo.list(page=2, per_page=20)
    assert len(page.items) == 20
    assert page.total_pages == 2
    assert not page.has_next
