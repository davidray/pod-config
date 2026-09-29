"""Spot a dispatch that makes tests pass by disabling them.

Validation commands only prove the suite passes; a worker can get there by
commenting tests out or marking them skipped (seen in the first live trial,
where Qwen commented out its own failing tests). This scans the dispatch diff.

Strong signals (commented-out tests, skip/only markers) fail the dispatch.
Net removal of assertions is only a warning: it can be legitimate when the
code under test is removed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$")
COMMENTED_TEST = re.compile(
    r"^\s*(//|#|/?\*)\s*(describe|it|test|expect|assert\w*|def test_\w*|context)\b[\s(.]")
SKIPPED_TEST = re.compile(
    r"\b(describe|it|test|context)\.(skip|only|todo)\s*\(|\bx(describe|it|test)\s*\(|\bf(describe|it)\s*\("
    r"|@pytest\.mark\.(skip|xfail)\b|\bpytest\.skip\s*\(|@unittest\.skip|\bt\.Skip\s*\(")
ASSERTION = re.compile(r"\bexpect\s*\(|\bassert\w*\b|\bt\.(Error|Fatal)f?\s*\(")


@dataclass
class GuardReport:
    disabled: list[str] = field(default_factory=list)  # strong: fail the dispatch
    warnings: list[str] = field(default_factory=list)  # review carefully


def _file_hunks(patch: str) -> dict[str, tuple[list[str], list[str]]]:
    files: dict[str, tuple[list[str], list[str]]] = {}
    current: tuple[list[str], list[str]] | None = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
            current = files.setdefault(path, ([], [])) if path != "/dev/null" else None
        elif line.startswith("--- ") or line.startswith("diff --git"):
            continue
        elif current is not None and line.startswith("+"):
            current[0].append(line[1:])
        elif current is not None and line.startswith("-"):
            current[1].append(line[1:])
    return files


def scan(patch: str) -> GuardReport:
    report = GuardReport()
    for path, (added, removed) in _file_hunks(patch).items():
        if not TEST_PATH.search(path):
            continue
        commented = sum(1 for line in added if COMMENTED_TEST.match(line))
        if commented:
            report.disabled.append(f"{path}: {commented} added line(s) of commented-out test code")
        skipped = [line.strip() for line in added if SKIPPED_TEST.search(line)]
        if skipped:
            report.disabled.append(f"{path}: skip/only marker added: {skipped[0][:80]}")
        lost = sum(1 for line in removed if ASSERTION.search(line)) - sum(1 for line in added if ASSERTION.search(line))
        if lost > 0:
            report.warnings.append(f"{path}: {lost} assertion(s) removed net")
    return report
