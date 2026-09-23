`pricing.discount_amount` has grown an if/elif chain over `discount.kind`, and
we are about to add more discount types. Refactor it into a strategy registry.

Requirements:

- Create `src/ledgerlite/discounts.py` containing one function per discount kind
  and a module-level registry `DISCOUNT_STRATEGIES: dict[str, Callable[[Invoice, Money], Money]]`
  mapping `"percent"`, `"fixed"` and `"bulk"` to their strategies. Each strategy
  receives the invoice and the base amount and returns the discount `Money`.
- `pricing.discount_amount` keeps its signature, returns zero when there is no
  discount, looks the strategy up in the registry, and raises the same
  `ValueError` for unknown kinds. No per-kind branching may remain in pricing.py.
- Registering a new kind in `DISCOUNT_STRATEGIES` must be enough for
  `compute_totals` to apply it.
- Pure refactor: behavior must not change, and existing tests must pass
  without modification.
