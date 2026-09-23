"""Loading invoices from JSON, plus built-in sample data."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from ledgerlite.models import Customer, Discount, Invoice, InvoiceStatus, LineItem
from ledgerlite.money import Money

SAMPLE = [
    {
        "number": "INV-1001", "issued": "2026-01-05", "due": "2026-02-04", "status": "sent",
        "customer": {"id": "c1", "name": "Acme Tools", "region": "US-CA"},
        "items": [{"description": "Widget", "quantity": "10", "unit_price": "12.50"},
                  {"description": "Setup", "quantity": "1", "unit_price": "99.00", "taxable": False}],
    },
    {
        "number": "INV-1002", "issued": "2026-01-09", "due": "2026-02-08", "status": "paid",
        "customer": {"id": "c2", "name": "Globex", "region": "US-TX"},
        "items": [{"description": "Gadget", "quantity": "3", "unit_price": "45.00"}],
        "discount": {"kind": "percent", "value": "10"},
    },
    {
        "number": "INV-1003", "issued": "2026-02-01", "due": "2026-03-03", "status": "draft",
        "customer": {"id": "c3", "name": "Initech", "region": "EU-DE"},
        "items": [{"description": "Consulting", "quantity": "7.5", "unit_price": "140.00"}],
        "discount": {"kind": "fixed", "value": "50"},
    },
]


def invoice_from_dict(d: dict) -> Invoice:
    c = d["customer"]
    inv = Invoice(
        number=d["number"],
        customer=Customer(id=c["id"], name=c["name"], region=c["region"], email=c.get("email")),
        issued=date.fromisoformat(d["issued"]),
        due=date.fromisoformat(d["due"]),
        status=InvoiceStatus(d.get("status", "draft")),
        currency=d.get("currency", "USD"),
    )
    for item in d.get("items", []):
        inv.add_item(LineItem(
            description=item["description"],
            quantity=Decimal(str(item["quantity"])),
            unit_price=Money.of(item["unit_price"], inv.currency),
            taxable=item.get("taxable", True),
        ))
    if d.get("discount"):
        disc = d["discount"]
        inv.discount = Discount(kind=disc["kind"], value=Decimal(str(disc["value"])),
                                min_quantity=int(disc.get("min_quantity", 0)))
    return inv


def load_invoices(path: str | None = None) -> list[Invoice]:
    data = json.loads(Path(path).read_text()) if path else SAMPLE
    return [invoice_from_dict(d) for d in data]
