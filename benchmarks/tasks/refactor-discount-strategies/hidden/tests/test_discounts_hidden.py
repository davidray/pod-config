from decimal import Decimal

import pytest

from ledgerlite import discounts, pricing
from ledgerlite.models import Discount
from ledgerlite.money import Money


def test_registry_keys():
    assert set(discounts.DISCOUNT_STRATEGIES) == {"percent", "fixed", "bulk"}


def test_new_strategy_is_picked_up(invoice_factory, monkeypatch):
    monkeypatch.setitem(discounts.DISCOUNT_STRATEGIES, "flat-five", lambda inv, base: Money.of("5.00"))
    inv = invoice_factory(items=[("Thing", 2, "10.00")], discount=Discount("flat-five", Decimal("0")))
    assert pricing.compute_totals(inv).discount == Money.of("5.00")


def test_unknown_kind_still_value_error(invoice_factory):
    inv = invoice_factory(items=[("Thing", 1, "1.00")], discount=Discount("nope", Decimal("1")))
    with pytest.raises(ValueError):
        pricing.discount_amount(inv, Money.of("1"))


def test_percent_bounds_preserved(invoice_factory):
    inv = invoice_factory(items=[("Thing", 1, "1.00")], discount=Discount("percent", Decimal("-1")))
    with pytest.raises(ValueError):
        pricing.compute_totals(inv)
