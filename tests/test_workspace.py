import hashlib
import subprocess
from pathlib import Path

from qwenbench.agent import snapshot
from qwenbench.bench import workspace as wsmod
from qwenbench.bench.runner import reset_to_tree
from qwenbench.bench.suite import load_case, load_suite


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            h.update(str(p.relative_to(root)).encode() + p.read_bytes())
    return h.hexdigest()


def test_suite_loads_all_categories():
    suite, cases = load_suite("daveeval")
    assert {c.category for c in cases} == {"fix-failing-test", "feature-from-spec", "refactor", "multi-file-bug",
                                           "exploration", "add-tests"}
    assert all(c.prompt.strip() for c in cases)


def test_workspace_is_isolated_deterministic_and_cleaned():
    case = load_case("fix-pagination-regression")
    src = case.resolve(case.source.path)
    before = tree_hash(src)
    a, b = wsmod.create(case), wsmod.create(case)
    try:
        assert a.starting_commit == b.starting_commit  # same content -> same SHA (reproducible)
        assert a.root != b.root
        assert (a.repo / "tests" / "test_pagination.py").exists()  # seed overlay applied
        (a.repo / "src" / "ledgerlite" / "pagination.py").write_text("broken")
        assert tree_hash(src) == before  # the source fixture is never mutated
    finally:
        a.cleanup()
        b.cleanup()
    assert not a.root.exists() and not b.root.exists()


def test_git_source_clones_pinned_ref(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    run = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=origin,  # noqa: E731
                                    check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q")
    (origin / "f.txt").write_text("v1")
    run("add", "-A")
    run("commit", "-qm", "v1")
    v1 = run("rev-parse", "HEAD")
    (origin / "f.txt").write_text("v2")
    run("commit", "-qam", "v2")
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "prompt.md").write_text("x")
    (case_dir / "case.yaml").write_text(f"""id: g
title: g
source: {{git: {origin}, ref: {v1}}}
task: {{prompt_file: prompt.md}}
validation: {{commands: ["true"]}}
""")
    case = load_case(str(case_dir / "case.yaml"))
    ws = wsmod.create(case)
    try:
        assert ws.starting_commit == v1 and (ws.repo / "f.txt").read_text() == "v1"
        (ws.repo / "f.txt").write_text("agent edit")
        assert (origin / "f.txt").read_text() == "v2"
    finally:
        ws.cleanup()


def test_restore_overlay_and_reset_roundtrip():
    case = load_case("csv-export-feature")
    ws = wsmod.create(case)
    try:
        (ws.repo / "tests" / "test_cli.py").write_text("# agent weakened this test\n")
        (ws.repo / "src" / "ledgerlite" / "new.py").write_text("x = 1\n")
        agent_tree = snapshot.snapshot(ws.repo)
        wsmod.restore_paths(ws, ["tests/test_cli.py"])
        wsmod.apply_overlay(ws, case.resolve(case.validation.overlay))
        assert "weakened" not in (ws.repo / "tests" / "test_cli.py").read_text()
        assert (ws.repo / "tests" / "test_csv_export_hidden.py").exists()
        reset_to_tree(ws, agent_tree)
        assert snapshot.snapshot(ws.repo) == agent_tree
        assert not (ws.repo / "tests" / "test_csv_export_hidden.py").exists()
    finally:
        ws.cleanup()


def test_snapshot_does_not_touch_index_or_head(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run("git init -q && echo a > a && git add a && git -c user.name=t -c user.email=t@t commit -qm i",
                   shell=True, cwd=repo, check=True)
    (repo / "b").write_text("new")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout
    t1 = snapshot.snapshot(repo)
    t2 = snapshot.snapshot(repo)
    assert t1 == t2
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == "?? b\n"
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout == head
    base = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert [c.path for c in snapshot.changes(repo, base, t1)] == ["b"]


def test_snapshot_sees_same_size_edit_in_same_second_as_index_write(tmp_path):
    """Regression: a copied index with a fresh mtime hid racily-clean edits."""
    repo = tmp_path / "racy"
    repo.mkdir()
    for i in range(20):  # the race is timing-dependent; repeat to make it reliable
        (repo / "f.py").write_text("return a - b\n")
        subprocess.run("git init -q 2>/dev/null; git add -A && git -c user.name=t -c user.email=t@t "
                       "commit -qm c --allow-empty", shell=True, cwd=repo, check=True)
        before = snapshot.snapshot(repo)
        (repo / "f.py").write_text("return a + b\n")  # same size, same second
        after = snapshot.snapshot(repo)
        assert before != after, f"iteration {i}: same-size edit missed"
