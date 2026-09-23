"""Command line interface.

    ledgerlite export --format json [--status sent] [--data invoices.json]
    ledgerlite list [--page N] [--per-page N]
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ledgerlite.exporters import EXPORTERS, get_exporter
from ledgerlite.models import InvoiceStatus
from ledgerlite.repository import InvoiceRepository
from ledgerlite.sample import load_invoices


def build_repository(path: str | None) -> InvoiceRepository:
    repo = InvoiceRepository()
    for inv in load_invoices(path):
        repo.add(inv)
    return repo


def cmd_export(args: argparse.Namespace) -> int:
    repo = build_repository(args.data)
    invoices = repo.all()
    if args.status:
        invoices = [i for i in invoices if i.status == InvoiceStatus(args.status)]
    sys.stdout.write(get_exporter(args.format).export(invoices))
    sys.stdout.write("\n")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    repo = build_repository(args.data)
    page = repo.list(page=args.page, per_page=args.per_page)
    for inv in page.items:
        print(f"{inv.number}\t{inv.customer.name}\t{inv.status.value}")
    print(f"-- page {page.page}/{page.total_pages} ({page.total_items} invoices)")
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledgerlite")
    parser.add_argument("--data", help="JSON file of invoices (default: built-in sample data)")
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="export invoices")
    exp.add_argument("--format", choices=sorted(EXPORTERS), default="json")
    exp.add_argument("--status", choices=[s.value for s in InvoiceStatus])
    exp.set_defaults(func=cmd_export)

    lst = sub.add_parser("list", help="list invoices")
    lst.add_argument("--page", type=int, default=1)
    lst.add_argument("--per-page", type=int, default=20)
    lst.set_defaults(func=cmd_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
