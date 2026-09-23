from decimal import Decimal

from ledgerlite.models import Customer, Discount
from ledgerlite.money import Money
from ledgerlite.pricing import compute_totals

NL = Customer(id="c5", name="Dutch Co", region="EU-NL")


def test_accounting_example(invoice_factory):
    inv = invoice_factory(customer=NL, items=[("Sticker", 1, "0.125")] * 4)
    t = compute_totals(inv)
    assert (t.subtotal, t.tax, t.grand_total) == (Money.of("0.52"), Money.of("0.11"), Money.of("0.63"))


def test_half_up_rounding():
    assert Money.of("0.125").rounded() == Money.of("0.13")
    assert Money.of("0.135").rounded() == Money.of("0.14")
    assert Money.of("2.345").rounded() == Money.of("2.35")
    assert Money.of("10").times(Decimal("0.0725")).rounded() == Money.of("0.73")


def test_tax_uses_rounded_line_totals_with_discount(invoice_factory):
    inv = invoice_factory(customer=NL, items=[("Sticker", 1, "0.125")] * 4,
                          discount=Discount("percent", Decimal("50")))
    t = compute_totals(inv)
    assert t.subtotal == Money.of("0.52")
    assert t.discount == Money.of("0.26")
    assert t.tax == Money.of("0.05")  # 21% of 0.26 = 0.0546
    assert t.grand_total == Money.of("0.31")


def test_existing_totals_unchanged(invoice_factory):
    inv = invoice_factory(items=[("Widget", 10, "12.50"), ("Setup", 1, "99.00", False)])
    assert compute_totals(inv).grand_total == Money.of("233.06")
