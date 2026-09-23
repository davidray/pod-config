"""Accounts-receivable aging report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ledgerlite.models import Invoice, InvoiceStatus
from ledgerlite.money import Money
from ledgerlite.pricing import compute_totals

BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


@dataclass
class AgingReport:
    as_of: date
    currency: str
    buckets: dict[str, Money] = field(default_factory=dict)
    invoice_buckets: dict[str, str] = field(default_factory=dict)

    def total_outstanding(self) -> Money:
        result = Money.zero(self.currency)
        for amount in self.buckets.values():
            result = result + amount
        return result


def bucket_for(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


def aging_report(invoices: list[Invoice], as_of: date, currency: str = "USD") -> AgingReport:
    """Bucket outstanding (SENT) invoices by days past due.

    Draft, paid and void invoices are excluded. Invoices in another currency
    are rejected rather than silently mixed.
    """
    report = AgingReport(as_of=as_of, currency=currency, buckets={b: Money.zero(currency) for b in BUCKETS})
    for inv in invoices:
        if inv.status != InvoiceStatus.SENT:
            continue
        if inv.currency != currency:
            raise ValueError(f"invoice {inv.number} is in {inv.currency}, report is in {currency}")
        days = (as_of - inv.due).days
        bucket = bucket_for(days)
        report.buckets[bucket] = report.buckets[bucket] + compute_totals(inv).grand_total
        report.invoice_buckets[inv.number] = bucket
    return report
