"""Mutation check: each mutant of reports.py must make tests/test_reports.py fail."""
import pathlib
import subprocess
import sys

TARGET = pathlib.Path("src/ledgerlite/reports.py")
MUTANTS = [
    ("if days_overdue <= 0:", "if days_overdue < 0:"),
    ("if days_overdue <= 30:", "if days_overdue < 30:"),
    ("if days_overdue <= 60:", "if days_overdue < 60:"),
    ("if days_overdue <= 90:", "if days_overdue < 90:"),
    ("if inv.status != InvoiceStatus.SENT:", "if inv.status == InvoiceStatus.VOID:"),
    ("if inv.currency != currency:", "if False:"),
    ("report.invoice_buckets[inv.number] = bucket", "pass"),
    ("result = result + amount", "result = amount"),
]

original = TARGET.read_text()
survivors = []
try:
    for before, after in MUTANTS:
        assert before in original, f"mutant anchor missing: {before}"
        TARGET.write_text(original.replace(before, after, 1))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                            "tests/test_reports.py"], capture_output=True)
        status = "killed" if r.returncode != 0 else "SURVIVED"
        print(f"{status:9} {before!r} -> {after!r}")
        if r.returncode == 0:
            survivors.append(before)
finally:
    TARGET.write_text(original)
print(f"{len(MUTANTS) - len(survivors)}/{len(MUTANTS)} mutants killed")
sys.exit(1 if survivors else 0)
