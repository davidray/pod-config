import json

import pytest

from ledgerlite.cli import main
from ledgerlite.repository import InvoiceRepository


def test_repository_for_customer(invoice_factory):
    from ledgerlite.models import Customer
    other = Customer(id="c2", name="Globex", region="US-TX")
    repo = InvoiceRepository()
    repo.add(invoice_factory(number="A"))
    repo.add(invoice_factory(number="B", customer=other))
    repo.add(invoice_factory(number="C"))
    assert [i.number for i in repo.for_customer("c1")] == ["A", "C"]
    assert repo.for_customer("nobody") == []


def test_export_customer(capsys):
    assert main(["export", "--customer", "c2"]) == 0
    assert [r["number"] for r in json.loads(capsys.readouterr().out)] == ["INV-1002"]


def test_export_customer_and_status(capsys):
    assert main(["export", "--customer", "c1", "--status", "paid"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_list_customer_paginated(capsys):
    assert main(["list", "--customer", "c3", "--per-page", "1"]) == 0
    out = capsys.readouterr().out
    assert "INV-1003" in out and "INV-1001" not in out
    assert "page 1/1" in out


@pytest.mark.parametrize("cmd", ["export", "list"])
def test_unknown_customer(cmd, capsys):
    with pytest.raises(SystemExit) as exc:
        code = main([cmd, "--customer", "zzz"])
        raise SystemExit(code)
    assert exc.value.code == 2
    assert "unknown customer: zzz" in capsys.readouterr().err
