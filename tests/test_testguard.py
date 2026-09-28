from qwenbench.agent import testguard


def patch(path, added=(), removed=()):
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"


def test_commented_out_tests_are_disabled():
    r = testguard.scan(patch("src/index.test.ts", added=['// describe("word counting", () => {',
                                                          '//   it("counts", () => {', "//     expect(1).toBe(1);"]))
    assert r.disabled and "commented-out" in r.disabled[0]


def test_python_commented_test_and_skip_markers():
    r = testguard.scan(patch("tests/test_app.py", added=["# def test_add():", "#     assert add(2, 3) == 5"]))
    assert r.disabled
    r = testguard.scan(patch("tests/test_app.py", added=["@pytest.mark.skip(reason='later')"]))
    assert r.disabled and "skip" in r.disabled[0]
    r = testguard.scan(patch("web/app.spec.tsx", added=['it.only("x", () => {'] ))
    assert r.disabled


def test_net_assertion_removal_is_only_a_warning():
    r = testguard.scan(patch("tests/test_app.py", removed=["    assert add(2, 3) == 5", "    assert add(0, 0) == 0"],
                             added=["    assert add(2, 3) == 5"]))
    assert not r.disabled and r.warnings == ["tests/test_app.py: 1 assertion(s) removed net"]


def test_new_real_tests_and_non_test_files_are_clean():
    assert testguard.scan(patch("src/index.test.ts", added=['it("counts", () => {', "  expect(n).toBe(3);"])).disabled == []
    assert testguard.scan(patch("src/app.py", added=["# assert this later", "x = 1"])) == testguard.GuardReport()
