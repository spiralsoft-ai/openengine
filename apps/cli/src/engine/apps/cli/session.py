"""`engine agent claude|codex|opencode`: an agent CLI as the implementation node of a run.

The daemon starts a run of its built-in session graph: it checks the
repository out into a workspace of its own, then binds the run's repository
tools -- `git_subcommand`, `open_pull_request`, whatever `[sessions] tools` in
engine.toml grants -- to that checkout, exactly as it does for an
implementation node. Instead of opening an ACP session, the node hands those
tools to this command, which runs the agent's CLI in the checkout, in this
terminal. When the CLI exits, the node finishes and the run with it.

Each CLI is told the same three things in its own terms: the run's MCP
server, the publishing instructions, and Engine's shell rules (see
`AGENTS`).

The tools are served from the daemon's own process on 127.0.0.1 and the
checkout is on the daemon's disk, so the backend has to be this machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from engine.apps.cli import connect
from engine.cli import backends, repository
from engine.cli.backends import BackendError
from engine.cli.http import Client, RequestFailed

EXIT_OK = 0
EXIT_FAILED = 1
POLL_SECONDS = 0.5


DEFAULT_INSTRUCTIONS = "You are working in an OpenEngine workspace."


@dataclass(frozen=True, slots=True)
class Agent:
    """A CLI `engine agent <name>` can hand the terminal to."""

    name: str
    title: str
    install: str
    command: Callable[[dict[str, Any], Path, list[str]], tuple[list[str], dict[str, str]]]
    """The invocation and the environment it adds, given the session, a scratch directory and the passed arguments."""


def add_parser(agent_actions: argparse._SubParsersAction) -> None:
    """Add `claude`, `codex` and `opencode` to `engine agent`, beside the actions that manage a backend's agents."""
    for agent in AGENTS.values():
        parser = agent_actions.add_parser(
            agent.name,
            help=f"drive an implementation node with {agent.title} in this terminal",
            description=(
                f"Start a run whose implementation node is {agent.title} in this terminal, working in a "
                f"fresh workspace with the tools [sessions] grants in engine.toml. Arguments after -- go to {agent.name}."
            ),
        )
        parser.add_argument("--repo", default=".", help="a path inside the repository (default: the current directory)")
        parser.add_argument("--base", default="", metavar="REF", help="what the workspace starts from (default: the backend's)")
        parser.add_argument("--backend", metavar="NAME", help="the backend to use; it must run on this machine")
        parser.add_argument("agent_args", nargs=argparse.REMAINDER, help=f"passed to {agent.name} after --")


def main(arguments: argparse.Namespace) -> int:
    agent = AGENTS[arguments.action]
    if shutil.which(agent.name) is None:
        print(f"engine: {agent.name} is not installed; see {agent.install}", file=sys.stderr)
        return EXIT_FAILED
    try:
        backend = backends.load().selected(arguments.backend)
        if not backend.is_local:
            raise RuntimeError(
                f"backend {backend.name} is not on this machine; a session's workspace and tools "
                f"live with its daemon, so run engine agent {agent.name} there"
            )
        connect.ensure_ready(backend)
        client = Client(backend)
        session = start(client, agent.name, arguments)
    except (BackendError, RequestFailed, RuntimeError, ValueError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_FAILED
    workspace = session["workspace"]
    print(f"Run {session['runId']}: workspace {workspace['path']} on {workspace['ref']}", file=sys.stderr)
    status = EXIT_FAILED
    try:
        with tempfile.TemporaryDirectory(prefix="engine-session-") as directory:
            passed = arguments.agent_args
            command, environment = agent.command(session, Path(directory), passed[1:] if passed[:1] == ["--"] else passed)
            status = run_agent(workspace["path"], command, environment)
    finally:
        try:
            client.post(f"/sessions/{quote(session['sessionId'])}/end", {})
        except RequestFailed as error:
            print(f"engine: could not end the session: {error}", file=sys.stderr)
    print(
        f"Session ended. The work is on {workspace['ref']} at {workspace['path']}; "
        f"see `engine run get {session['runId']} --pretty`.",
        file=sys.stderr,
    )
    return status


def start(client: Client, agent: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Start the run, then wait for its node to be ready for a terminal."""
    found = repository.current(Path(arguments.repo))
    if found is None:
        raise RuntimeError(f"{arguments.repo} is not inside a git repository")
    session = client.post("/sessions", {"agent": agent, "repository": found.target(local=True), "baseRef": arguments.base})
    told = False
    while session["status"] == "starting":
        if not told:
            print(f"Starting run {session['runId']}…", file=sys.stderr)
            told = True
        time.sleep(POLL_SECONDS)
        session = client.get(f"/sessions/{quote(session['sessionId'])}")
    if session["status"] != "ready":
        raise RuntimeError(f"run {session['runId']} did not start a session: {session.get('error') or session['status']}")
    return session


def claude_command(session: dict[str, Any], directory: Path, passed: list[str]) -> tuple[list[str], dict[str, str]]:
    """claude with the run's MCP server, its instructions, and Engine's shell rules as deny rules."""
    mcp = session["mcp"]
    servers = {"mcpServers": {mcp["name"]: {"command": mcp["command"], "args": list(mcp["args"])}}}
    (directory / "mcp.json").write_text(json.dumps(servers), encoding="utf-8")
    command = ["claude"]
    if session.get("settings"):
        (directory / "settings.json").write_text(json.dumps(session["settings"]), encoding="utf-8")
        command += ["--settings", str(directory / "settings.json")]
    # `--mcp-config` takes several files, and would take a prompt passed after
    # it for one more; an option following it is what ends the list.
    command += [
        "--mcp-config", str(directory / "mcp.json"),
        "--append-system-prompt", session.get("instructions") or DEFAULT_INSTRUCTIONS,
    ]
    return [*command, *passed], {}


def codex_command(session: dict[str, Any], _directory: Path, passed: list[str]) -> tuple[list[str], dict[str, str]]:
    """codex with the run's MCP server and its instructions as `-c` overrides.

    Codex parses each override's value as TOML. A JSON string or array of
    strings is one too, as long as nothing outside ASCII is written as a
    surrogate pair, which TOML has no escape for.
    """
    mcp = session["mcp"]
    server = f"mcp_servers.{mcp['name']}"
    overrides = {
        f"{server}.command": mcp["command"],
        f"{server}.args": list(mcp["args"]),
        "developer_instructions": session.get("instructions") or DEFAULT_INSTRUCTIONS,
    }
    command = ["codex"]
    for key, value in overrides.items():
        command += ["-c", f"{key}={json.dumps(value, ensure_ascii=False)}"]
    return [*command, *passed], {}


def opencode_command(session: dict[str, Any], directory: Path, passed: list[str]) -> tuple[list[str], dict[str, str]]:
    """opencode with a configuration merged over the operator's: the MCP server, instructions and shell rules.

    OpenCode takes instructions only as files, so they are written beside it.
    """
    mcp = session["mcp"]
    instructions = directory / "instructions.md"
    instructions.write_text(session.get("instructions") or DEFAULT_INSTRUCTIONS, encoding="utf-8")
    config = {
        **(session.get("settings") or {}),
        "mcp": {mcp["name"]: {"type": "local", "command": [mcp["command"], *mcp["args"]], "enabled": True}},
        "instructions": [str(instructions)],
    }
    return ["opencode", *passed], {"OPENCODE_CONFIG_CONTENT": json.dumps(config)}


AGENTS = {
    agent.name: agent
    for agent in (
        Agent("claude", "Claude Code", "https://docs.claude.com/en/docs/claude-code", claude_command),
        Agent("codex", "Codex", "https://developers.openai.com/codex/cli", codex_command),
        Agent("opencode", "OpenCode", "https://opencode.ai", opencode_command),
    )
}


def run_agent(root: str, command: list[str], environment: dict[str, str] | None = None) -> int:
    """Hand this terminal to the agent's CLI until it exits.

    Ctrl-C belongs to the CLI, which uses it to interrupt a turn, so this
    process ignores it rather than dying and leaving the run's node waiting.
    """
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return subprocess.call(command, cwd=root, env={**os.environ, **(environment or {})})
    finally:
        signal.signal(signal.SIGINT, previous)
