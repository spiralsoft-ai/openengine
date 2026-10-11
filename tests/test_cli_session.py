"""`engine agent claude|codex|opencode` from the terminal side: the daemon is a recording stand-in, the CLI is stubbed."""

from __future__ import annotations

import json
import subprocess
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
def repository(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
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

    def run(root: str, command: list[str], _environment: dict[str, str]) -> int:
        mcp = json.loads(Path(command[command.index("--mcp-config") + 1]).read_text())
        settings = json.loads(Path(command[command.index("--settings") + 1]).read_text())
        launched.append((root, command, mcp, settings))
        return 3

    monkeypatch.setattr(session, "run_agent", run)

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

    def broken(_root: str, _command: list[str], _environment: dict[str, str]) -> int:
        raise OSError("claude vanished")

    monkeypatch.setattr(session, "run_agent", broken)
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


def launch(repository: Path, monkeypatch, agent: str, *passed: str, settings: dict[str, Any] | None = None) -> tuple[Daemon, list[Any]]:
    answer = {**READY, "agent": agent, "settings": settings or {}}
    daemon = Daemon(answer, {**answer, "status": "ended"})
    monkeypatch.setattr(session, "Client", daemon.client)
    launched: list[Any] = []

    def run(root: str, command: list[str], environment: dict[str, str]) -> int:
        config = json.loads(environment.get("OPENCODE_CONFIG_CONTENT", "{}"))
        files = {path: Path(path).read_text() for path in config.get("instructions", [])}
        launched.append((root, command, environment, config, files))
        return 0

    monkeypatch.setattr(session, "run_agent", run)
    assert cli.main(["agent", agent, "--repo", str(repository), *passed]) == 0
    return daemon, launched[0]


def test_codex_is_given_the_server_and_instructions_as_config_overrides(repository, monkeypatch):
    import tomllib

    daemon, (root, command, environment, _config, _files) = launch(repository, monkeypatch, "codex", "--", "-m", "gpt-5")
    assert daemon.requests[0][2]["agent"] == "codex"
    assert root == READY["workspace"]["path"] and environment == {}
    assert command[0] == "codex" and command[-2:] == ["-m", "gpt-5"]
    overrides = {}
    for index, part in enumerate(command):
        if part == "-c":
            key, _, value = command[index + 1].partition("=")
            overrides[key] = tomllib.loads(f"v = {value}")["v"]
    assert overrides == {
        "mcp_servers.engine.command": READY["mcp"]["command"],
        "mcp_servers.engine.args": READY["mcp"]["args"],
        "developer_instructions": READY["instructions"],
    }


def test_opencode_is_given_a_config_with_the_server_instructions_and_shell_rules(repository, monkeypatch):
    rules = {"permission": {"bash": {"git push": "deny", "git push *": "deny"}}}
    daemon, (root, command, _environment, config, files) = launch(repository, monkeypatch, "opencode", settings=rules)
    assert daemon.requests[0][2]["agent"] == "opencode"
    assert root == READY["workspace"]["path"] and command == ["opencode"]
    assert config["permission"] == rules["permission"]
    assert config["mcp"] == {
        "engine": {"type": "local", "command": [READY["mcp"]["command"], *READY["mcp"]["args"]], "enabled": True},
    }
    assert list(files.values()) == [READY["instructions"]]
