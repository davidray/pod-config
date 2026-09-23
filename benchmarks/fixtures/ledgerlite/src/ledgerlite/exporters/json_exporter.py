from __future__ import annotations

import json

from ledgerlite.exporters.base import invoice_row
from ledgerlite.models import Invoice


class JsonExporter:
    content_type = "application/json"

    def export(self, invoices: list[Invoice]) -> str:
        return json.dumps([invoice_row(i) for i in invoices], indent=2)
