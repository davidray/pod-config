# ledgerlite

A small invoicing library: customers, invoices with line items, discounts,
regional sales tax, paginated listing, exporters and a CLI.

    ledgerlite export --format json

Money is always handled as `Money` (Decimal-backed, currency-aware). Amounts
are rounded to cents using commercial rounding (half-up) at line level, as
required by our accountants.

Development:

    uv venv .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
    .venv/bin/python -m pytest
