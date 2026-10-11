"""`engine agent claude` from the terminal side: the daemon is a recording stand-in, claude is stubbed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from engine.apps.cli import __main__ as cli
from engine.apps.cli import session
from engine.cli import backends

READY = {
    "sessionId": "s-1", "agent": "claude", "runId": "run-1", "status": "ready",
    "workspace": {"path": "/tmp/engine-workspaces/ws-1", "ref": "engine/ws-1", "id": "ws-1"},
    "mcp": {"name": "engine", "command": "/venv/bin/python", "args": ["-m", "engine.runtime.terminal_mcp_server", "--token", "t"], "env": []},
    "instructions": "Every git operation goes through the git_subcommand tool.",
    "settings": {"permissions": {"deny": ["Bash(git push:*)"]}},
}


class Daemon:
    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, str, Any]] = []


    def client(self, backend: Any) -> Daemon:
        return self

    def get(self, path: str, **_query: str) -> dict[str, Any]:
        self.requests.append(("GET", path, None))
        return self.answers.pop(0)

    def post(self, path: str, body: dict[str, Any], **_options: Any) -> dict[str, Any]:
        self.requests.append(("POST", path, body))
        return self.answers.pop(0)


@pytest.fixture
def repository(tmp_path, monkeypatch, *, git_repo) -> Path:
    root = tmp_path / "repo"
    git_repo(root)
    monkeypatch.setenv(backends.FILE_ENVIRONMENT_VARIABLE, str(tmp_path / "backends.json"))
    monkeypatch.delenv(backends.SELECTED_ENVIRONMENT_VARIABLE, raising=False)
    monkeypatch.setattr(session.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(session.connect, "ensure_ready", lambda _backend: None)
    monkeypatch.setattr(session.time, "sleep", lambda _seconds: None)
    return root


def test_claude_runs_in_the_runs_workspace_with_its_tools_and_ends_the_node(repository, monkeypatch, capsys):
    daemon = Daemon({**READY, "status": "starting"}, READY, {**READY, "status": "ended"})
    monkeypatch.setattr(session, "Client", daemon.client)
    launched = []

    def run(root: str, command: list[str]) -> int:
        mcp = json.loads(Path(command[command.index("--mcp-config") + 1]).read_text())
        settings = json.loads(Path(command[command.index("--settings") + 1]).read_text())
        launched.append((root, command, mcp, settings))
        return 3

    monkeypatch.setattr(session, "run_claude", run)

    assert cli.main(["agent", "claude", "--repo", str(repository), "--base", "origin/dev", "--", "--model", "opus"]) == 3

    assert daemon.requests == [
        ("POST", "/sessions", {"agent": "claude", "repository": str(repository.resolve()), "baseRef": "origin/dev"}),
        ("GET", "/sessions/s-1", None),
        ("POST", "/sessions/s-1/end", {}),
    ]
    root, command, mcp, settings = launched[0]
    assert root == READY["workspace"]["path"]
    assert mcp == {"mcpServers": {"engine": {"command": "/venv/bin/python", "args": READY["mcp"]["args"]}}}
    assert settings == READY["settings"]
    assert command[command.index("--append-system-prompt") + 1] == READY["instructions"]
    assert command[-2:] == ["--model", "opus"]
    assert "engine run get run-1" in capsys.readouterr().err


def test_the_node_is_ended_even_when_claude_cannot_start(repository, monkeypatch):
    daemon = Daemon(READY, {**READY, "status": "ended"})
    monkeypatch.setattr(session, "Client", daemon.client)

    def broken(_root: str, _command: list[str]) -> int:
        raise OSError("claude vanished")

    monkeypatch.setattr(session, "run_claude", broken)
    with pytest.raises(OSError):
        cli.main(["agent", "claude", "--repo", str(repository)])
    assert daemon.requests[-1] == ("POST", "/sessions/s-1/end", {})


def test_a_session_that_fails_to_start_says_why(repository, monkeypatch, capsys):
    daemon = Daemon({**READY, "status": "failed", "error": "no checkout"})
    monkeypatch.setattr(session, "Client", daemon.client)
    assert cli.main(["agent", "claude", "--repo", str(repository)]) == 1
    assert "did not start a session: no checkout" in capsys.readouterr().err


def test_a_remote_backend_is_refused(repository, capsys):
    backends.add("mini", "http://mac-mini.local:4364", use=True)
    assert cli.main(["agent", "claude", "--repo", str(repository)]) == 1
    assert "not on this machine" in capsys.readouterr().err


def test_outside_a_repository_or_without_claude(repository, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(session, "Client", Daemon().client)
    assert cli.main(["agent", "claude", "--repo", str(tmp_path)]) == 1
    assert "not inside a git repository" in capsys.readouterr().err
    monkeypatch.setattr(session.shutil, "which", lambda _name: None)
    assert cli.main(["agent", "claude"]) == 1
    assert "claude is not installed" in capsys.readouterr().err
