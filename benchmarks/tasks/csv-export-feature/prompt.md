Finance wants to pull invoices into spreadsheets. Add a CSV export format.

Spec:

1. New module `src/ledgerlite/exporters/csv_exporter.py` with class `CsvExporter`
   implementing the existing `Exporter` protocol (`content_type = "text/csv"`).
2. Register it in the exporter registry under the name `"csv"` so that
   `ledgerlite export --format csv` works (the CLI derives its choices from the
   registry; do not hardcode the format in the CLI).
3. Output: a header row followed by one row per invoice. Columns and their order
   are exactly the keys of `invoice_row()` (shared with the JSON exporter). Use
   the standard library `csv` module, quote only when needed (RFC 4180), and use
   `"\n"` line endings. An empty invoice list produces just the header row.
4. Add tests for the new exporter in `tests/test_csv_export.py`.
