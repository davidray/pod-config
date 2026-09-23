Customer success wants per-customer views. Add a `--customer <customer id>`
option to both `ledgerlite export` and `ledgerlite list`.

- Add `InvoiceRepository.for_customer(customer_id) -> list[Invoice]` and use it
  from both commands (keep the repository's existing ordering).
- `list --customer X` must still paginate (page/per-page and the footer line).
- `export --customer X` must combine with `--status` and `--format`.
- An unknown customer id exits with status 2 and prints exactly
  `unknown customer: <id>` to stderr.
- Add tests.
