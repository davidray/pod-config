import csv
import io
import json
from datetime import date

from ledgerlite.cli import main
from ledgerlite.exporters import EXPORTERS, get_exporter
from ledgerlite.exporters.base import invoice_row
from ledgerlite.exporters.csv_exporter import CsvExporter
from ledgerlite.models import Customer


def test_registered_and_content_type():
    assert EXPORTERS["csv"] is CsvExporter
    assert get_exporter("csv").content_type == "text/csv"


def test_header_matches_row_keys(invoice_factory):
    inv = invoice_factory(items=[("Widget", 1, "10.00")])
    text = CsvExporter().export([inv])
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == list(invoice_row(inv).keys())
    assert rows[1] == list(invoice_row(inv).values())
    assert "\r\n" not in text


def test_empty_list_is_header_only():
    text = CsvExporter().export([])
    assert text.strip("\n").count("\n") == 0
    assert text.startswith("number,customer,")


def test_quoting(invoice_factory):
    tricky = Customer(id="c9", name='Smith, "Bob" & Co', region="US-OR")
    inv = invoice_factory(customer=tricky, items=[("x", 1, "1.00")], issued=date(2026, 1, 1))
    rows = list(csv.reader(io.StringIO(CsvExporter().export([inv]))))
    assert rows[1][1] == 'Smith, "Bob" & Co'


def test_cli_csv(capsys):
    assert main(["export", "--format", "csv"]) == 0
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert [r["number"] for r in rows] == ["INV-1001", "INV-1002", "INV-1003"]
    main(["export", "--format", "json"])
    assert json.loads(capsys.readouterr().out)[0]["total"] == rows[0]["total"]
