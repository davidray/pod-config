from decimal import Decimal

import pytest

from ledgerlite.models import Customer, Discount
from ledgerlite.money import Money
from ledgerlite.pricing import compute_totals, discount_amount


def test_totals_without_discount(invoice_factory):
    inv = invoice_factory(items=[("Widget", 10, "12.50"), ("Setup", 1, "99.00", False)])
    t = compute_totals(inv)
    assert t.subtotal == Money.of("224.00")
    assert t.discount == Money.of("0")
    assert t.tax == Money.of("9.06")  # 7.25% of 125.00 taxable = 9.0625
    assert t.grand_total == Money.of("233.06")


def test_percent_discount_reduces_tax_base(invoice_factory):
    tx = Customer(id="c2", name="Globex", region="US-TX")
    inv = invoice_factory(customer=tx, items=[("Gadget", 3, "45.00")], discount=Discount("percent", Decimal("10")))
    t = compute_totals(inv)
    assert t.discount == Money.of("13.50")
    assert t.tax == Money.of("7.59")  # 6.25% of 121.50 = 7.59375
    assert t.grand_total == Money.of("129.09")


def test_fixed_discount_capped_at_subtotal(invoice_factory):
    inv = invoice_factory(items=[("Thing", 1, "20.00")], discount=Discount("fixed", Decimal("50")))
    t = compute_totals(inv)
    assert t.discount == Money.of("20.00")
    assert t.tax == Money.of("0")
    assert t.grand_total == Money.of("0")


def test_bulk_discount_threshold(invoice_factory):
    items = [("Bolt", 99, "1.00")]
    below = invoice_factory(items=items, discount=Discount("bulk", Decimal("5"), min_quantity=100))
    assert compute_totals(below).discount == Money.of("0")
    at = invoice_factory(items=[("Bolt", 100, "1.00")], discount=Discount("bulk", Decimal("5"), min_quantity=100))
    assert compute_totals(at).discount == Money.of("5.00")


def test_invalid_discounts(invoice_factory):
    inv = invoice_factory(items=[("Thing", 1, "20.00")], discount=Discount("percent", Decimal("120")))
    with pytest.raises(ValueError):
        discount_amount(inv, Money.of("20"))
    inv.discount = Discount("mystery", Decimal("1"))
    with pytest.raises(ValueError):
        discount_amount(inv, Money.of("20"))
