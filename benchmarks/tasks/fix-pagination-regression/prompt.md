CI is red on `main`: `tests/test_pagination.py::test_last_partial_page_is_reachable` fails.

Users also report that `ledgerlite list --per-page 20` with 45 invoices says
"page 2/2" and never offers the third page.

Find and fix the bug. Do not change the failing test; it describes the
correct behavior.
