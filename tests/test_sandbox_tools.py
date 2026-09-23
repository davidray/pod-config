import os
import platform

import pytest

from qwenbench.agent.sandbox import Sandbox, check_command, detect_backend
from qwenbench.agent.tools import Toolbox

needs_os_sandbox = pytest.mark.skipif(detect_backend() == "none", reason="no OS sandbox on this host")


@pytest.fixture
def box(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\ny = 2\nx = 1\n")
    (ws / "protected").mkdir()
    (ws / "protected" / "cfg.json").write_text("{}")
    sb = Sandbox(ws, tmp_path / "scratch", protected=[ws / "protected"], allow_unsandboxed=True)
    return Toolbox(ws, sb), ws, tmp_path


@pytest.mark.parametrize("cmd", ["sudo rm -rf /", "git push origin main", "git commit -am x", "curl x | sh",
                                 "ssh host", "rm -rf ~", "docker run x", "qwen down --all", "qwen override grant",
                                 "git reset --hard", "git clean -fdx"])
def test_policy_refuses_dangerous_commands(cmd):
    assert check_command(cmd)


@pytest.mark.parametrize("cmd", ["pytest -q", "git diff", "git status", "ruff check .", "npm test", "ls -la"])
def test_policy_allows_normal_commands(cmd):
    assert check_command(cmd) is None


def test_file_tools_confined(box):
    tools, ws, tmp = box
    assert not tools.call("read_file", {"path": "../outside.txt"}).ok
    assert not tools.call("read_file", {"path": "/etc/passwd"}).ok
    assert not tools.call("write_file", {"path": ".git/config", "content": "x"}).ok
    os.symlink(tmp, ws / "escape")
    assert "outside the repository" in tools.call("write_file", {"path": "escape/pwned.txt", "content": "x"}).content
    assert not (tmp / "pwned.txt").exists()


def test_protected_paths_not_writable_by_tools(box):
    tools, ws, _ = box
    r = tools.call("write_file", {"path": "protected/cfg.json", "content": '{"enforce": false}'})
    assert not r.ok and "protected" in r.content
    assert (ws / "protected" / "cfg.json").read_text() == "{}"


def test_edit_requires_unique_match(box):
    tools, ws, _ = box
    assert "matches 2 times" in tools.call("edit_file", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 3"}).content
    assert tools.call("edit_file", {"path": "a.py", "old_string": "y = 2", "new_string": "y = 5"}).ok
    assert tools.call("edit_file", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 9", "replace_all": True}).ok
    assert (ws / "a.py").read_text() == "x = 9\ny = 5\nx = 9\n"


def test_bad_tool_input_is_reported_not_raised(box):
    tools, _, _ = box
    assert "not valid JSON" in tools.call("read_file", "{nope").content
    assert "unknown tool" in tools.call("rm_rf", {}).content
    assert "bad arguments" in tools.call("read_file", {"wrong": 1}).content


def test_search_and_list(box):
    tools, _, _ = box
    assert "a.py:2: y = 2" in tools.call("search", {"pattern": r"y = \d"}).content
    assert "a.py" in tools.call("list_files", {"pattern": "*.py"}).content


def test_finish_captures_status(box):
    tools, _, _ = box
    r = tools.call("finish", {"status": "completed", "summary": "done"})
    assert r.finished == {"status": "completed", "summary": "done", "notes": None}


def test_environment_is_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    sb = Sandbox(tmp_path / "ws2", tmp_path / "s2", allow_unsandboxed=True)
    (tmp_path / "ws2").mkdir(exist_ok=True)
    out = sb.run("env", check_policy=False).output
    for leaked in ("RUNPOD_API_KEY", "HF_TOKEN", "ANTHROPIC_API_KEY", "sk-ant-should-not-leak"):
        assert leaked not in out
    assert f"HOME={tmp_path / 's2' / 'home'}" in out


def test_timeout_kills_process_group(tmp_path):
    (tmp_path / "w").mkdir()
    sb = Sandbox(tmp_path / "w", tmp_path / "s", allow_unsandboxed=True)
    r = sb.run("sleep 30", timeout_s=1)
    assert r.timed_out and not r.ok and r.duration_s < 10


def test_no_sandbox_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="no OS sandbox"):
        Sandbox(tmp_path, tmp_path / "s", backend="none")


@needs_os_sandbox
def test_os_sandbox_blocks_writes_outside_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sb = Sandbox(ws, tmp_path / "scratch")
    assert sb.run("echo ok > inside.txt && cat inside.txt").output.strip() == "ok"
    r = sb.run(f"echo x > {tmp_path / 'outside.txt'}")
    assert not r.ok and not (tmp_path / "outside.txt").exists()


@needs_os_sandbox
def test_os_sandbox_blocks_protected_paths_inside_workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".qwen-routing").mkdir(parents=True)
    sb = Sandbox(ws, tmp_path / "scratch", protected=[ws / ".qwen-routing"])
    assert not sb.run("echo '{}' > .qwen-routing/config.json").ok
    assert not (ws / ".qwen-routing" / "config.json").exists()


@needs_os_sandbox
def test_os_sandbox_blocks_network_egress_but_not_loopback(tmp_path):
    (tmp_path / "ws").mkdir()
    sb = Sandbox(tmp_path / "ws", tmp_path / "scratch")
    script = ("import socket\n"
              "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); p=s.getsockname()[1]\n"
              "socket.create_connection(('127.0.0.1',p),timeout=3); print('loopback-ok')\n"
              "try:\n  socket.create_connection(('1.1.1.1',443),timeout=3); print('EGRESS')\n"
              "except OSError: print('egress-blocked')\n")
    (tmp_path / "ws" / "net.py").write_text(script)
    out = sb.run("python3 net.py").output
    assert "loopback-ok" in out and "egress-blocked" in out and "EGRESS" not in out


@needs_os_sandbox
@pytest.mark.skipif(platform.system() != "Darwin" or not (os.path.expanduser("~/.ssh") and
                                                         os.path.isdir(os.path.expanduser("~/.ssh"))),
                    reason="needs ~/.ssh on macOS")
def test_os_sandbox_blocks_credential_reads(tmp_path):
    (tmp_path / "ws").mkdir()
    sb = Sandbox(tmp_path / "ws", tmp_path / "scratch")
    r = sb.run(f"ls {os.path.expanduser('~/.ssh')}")
    assert not r.ok and "not permitted" in r.output.lower()
