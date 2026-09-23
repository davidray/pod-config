"""Invoice exporters, looked up by format name.

To add a format, implement the `Exporter` protocol and register it in
`EXPORTERS`. The CLI's `--format` choices come from this registry.
"""

from __future__ import annotations

from ledgerlite.exporters.base import Exporter
from ledgerlite.exporters.json_exporter import JsonExporter

EXPORTERS: dict[str, type[Exporter]] = {
    "json": JsonExporter,
}


def get_exporter(fmt: str) -> Exporter:
    try:
        return EXPORTERS[fmt]()
    except KeyError as e:
        raise ValueError(f"unknown export format {fmt!r}; choose from {sorted(EXPORTERS)}") from e
