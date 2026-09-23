Bug report from accounting:

> Our ledger doesn't match ledgerlite for invoices with fractional-cent unit
> prices. Example: customer in region EU-NL, four line items, each quantity 1 at
> 0.125 USD, no discount. ledgerlite shows subtotal 0.48, tax 0.10, total 0.58.
> Our ledger (which follows the rounding policy in the ledgerlite README) shows
> subtotal 0.52, tax 0.11, total 0.63.

Diagnose the root cause(s), fix them so ledgerlite follows the documented
policy, and add a regression test reproducing this report. If an existing test
encodes the incorrect behavior, correct that test and explain why in your summary.
