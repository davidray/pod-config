from datetime import date
from decimal import Decimal

import pytest

from ledgerlite.models import Customer, Invoice, InvoiceStatus, LineItem
from ledgerlite.money import Money


@pytest.fixture
def acme():
    return Customer(id="c1", name="Acme Tools", region="US-CA")


def make_invoice(number="INV-1", customer=None, items=(), status=InvoiceStatus.SENT,
                 issued=date(2026, 1, 1), due=date(2026, 1, 31), discount=None):
    customer = customer or Customer(id="c1", name="Acme Tools", region="US-CA")
    inv = Invoice(number=number, customer=customer, issued=issued, due=due, status=status, discount=discount)
    for desc, qty, price, *rest in items:
        inv.add_item(LineItem(desc, Decimal(str(qty)), Money.of(price), taxable=rest[0] if rest else True))
    return inv


@pytest.fixture
def invoice_factory():
    return make_invoice
