import json

from ledgerlite.cli import main


def test_export_json(capsys):
    assert main(["export", "--format", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["number"] for r in rows] == ["INV-1001", "INV-1002", "INV-1003"]
    assert rows[0]["total"] == "233.06"


def test_export_status_filter(capsys):
    main(["export", "--status", "paid"])
    rows = json.loads(capsys.readouterr().out)
    assert [r["number"] for r in rows] == ["INV-1002"]


def test_list(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "INV-1001\tAcme Tools\tsent" in out
