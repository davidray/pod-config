from decimal import Decimal

import pytest

from ledgerlite.money import CurrencyMismatch, Money, total


def test_addition_and_subtraction():
    assert Money.of("1.10") + Money.of("2.20") == Money.of("3.30")
    assert Money.of("5") - Money.of("1.5") == Money.of("3.5")


def test_currency_mismatch():
    with pytest.raises(CurrencyMismatch):
        Money.of(1, "USD") + Money.of(1, "EUR")


def test_times_and_rounding():
    assert Money.of("19.99").times(3).rounded() == Money.of("59.97")
    assert Money.of("10").times(Decimal("0.0725")).rounded() == Money.of("0.72")


def test_total_and_str():
    assert total([Money.of("1"), Money.of("2.5")]) == Money.of("3.5")
    assert str(Money.of("3.5")) == "3.50 USD"
