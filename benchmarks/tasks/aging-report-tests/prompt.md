`src/ledgerlite/reports.py` (the accounts-receivable aging report) has no tests,
and we are about to change it. Write thorough tests for its *current* behavior
in `tests/test_reports.py`.

Cover at least: every bucket and its exact boundaries (0, 1, 30, 31, 60, 61, 90,
91 days past due), exclusion of draft/paid/void invoices, rejection of invoices
in another currency (the error must name the offending invoice), per-invoice bucket assignment, and `total_outstanding()`.

Do not modify anything under `src/`. Use the existing fixtures in
`tests/conftest.py` where they help.
