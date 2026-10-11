"""`engine backend|graph|graphs|run|runs|loop|loops|node|nodes|agent|agents|runner`.

Backend commands speak to one backend -- `--backend`, else `ENGINE_BACKEND`, else
the one `engine backend use` chose -- and print JSON unless `--pretty` asks
for something to read. List commands print a table unless `--json` asks for JSON. Errors go to stderr; the exit status is 0 on success,
1 on a refusal or a failed run, and 2 on a usage mistake.
`graph spec` and `loop spec` print local Markdown specifications offline.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from inspect import getdoc
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import yaml

from engine.cli import backends, repository, starters
from engine.cli.backends import Backend, BackendError
from engine.cli.http import Client, RequestFailed
from engine.cli.specs import graph_spec, loop_spec

COMMANDS = frozenset({
    "backend", "backends", "graph", "graphs", "run", "runs", "loop", "loops", "node", "nodes", "agent", "agents",
    "runner",
})
#: Commands that still parse but are not advertised in `engine --help`.
HIDDEN = frozenset({"runner"})
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
WAIT_INTERVAL_SECONDS = 2.0
TERMINAL_STEERING = frozenset({"applied", "rejected", "undelivered"})

#: The harnesses an agent runs on.
AGENT_KINDS = ("claude", "codex", "opencode")

#: How each kind of agent signs in on the machine its daemon runs on.
SIGNIN = {
    "codex": (["codex", "login"], ""),
    "claude": (["claude"], "type /login, finish signing in, then /exit"),
    "opencode": (["opencode", "auth", "login"], ""),
}


def add_parsers(commands: argparse._SubParsersAction) -> argparse._SubParsersAction:
    """Add these commands to the `engine` parser.

    Answers `engine agent`'s actions, so the terminal app can add the ones that
    run here rather than on a backend, such as `engine agent claude`, `codex` and `opencode`.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--backend", metavar="NAME", help="the backend to use (default: the selected one)")
    common.add_argument("--pretty", action="store_true", help="human-readable output instead of JSON")
    scoped = argparse.ArgumentParser(add_help=False, parents=[common])
    # List commands print a table by default; `--pretty` is still accepted so older scripts keep working.
    listed = argparse.ArgumentParser(add_help=False)
    listed.add_argument("--json", action="store_true", help="JSON instead of a table")
    listed.add_argument("--pretty", action="store_true", help=argparse.SUPPRESS)
    scoped.add_argument("--project", metavar="NAME", help="the project names resolve in (default: the backend's)")

    backend = commands.add_parser("backend", help="choose which engine daemon commands talk to")
    actions = backend.add_subparsers(dest="action", required=True)
    adding = actions.add_parser("add", help="name a daemon URL, such as the Mac mini's")
    adding.add_argument("name")
    adding.add_argument("url", help="such as http://mac-mini.local:4364")
    adding.add_argument("--project", default=backends.DEFAULT_PROJECT, help="project graph names resolve in")
    adding.add_argument("--token-env", default="", metavar="VAR", help="environment variable holding its bearer token")
    adding.add_argument("--repo", default="", help="repository runs check out when none is given")
    adding.add_argument("--use", action="store_true", help="also make it the selected backend")
    adding.add_argument("--replace", action="store_true", help="overwrite a backend with this name")
    adding.add_argument("--pretty", action="store_true")
    for name, help_text in (("list", "list backends"), ("use", "select a backend"), ("remove", "forget a backend")):
        action = actions.add_parser(name, parents=[listed] if name == "list" else [], help=help_text)
        if name != "list":
            action.add_argument("name")
            action.add_argument("--pretty", action="store_true")
        else:
            action.add_argument("--check", action="store_true", help="also ask each backend whether it is up")
    listing = commands.add_parser("backends", parents=[listed], help="list backends")
    listing.add_argument("action", nargs="?", choices=("list",), default="list")
    listing.add_argument("--check", action="store_true", help="also ask each backend whether it is up")

    graph = commands.add_parser("graph", help="register, inspect and run graphs")
    actions = graph.add_subparsers(dest="action", required=True)
    actions.add_parser(
        "spec", help="print the current graph specification",
        description=getdoc(graph_spec), formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    adding = actions.add_parser("add", parents=[scoped], help="register a graph (YAML or a Python file)")
    adding.add_argument("file", help="the graph file, or - for YAML on stdin")
    getting = actions.add_parser("get", parents=[scoped], help="a graph and its exact definition")
    getting.add_argument("graph", help="a name, name@VERSION, graph id or version id")
    running = actions.add_parser("run", parents=[scoped], help="run a graph with an instruction")
    running.add_argument("graph", help=f"a graph, or a built-in one: {', '.join(starters.NAMES)}")
    running.add_argument("instruction", nargs="?", default="", help="what to do (default: run the graph)")
    running.add_argument("--input", "-i", action="append", default=[], metavar="NAME=VALUE")
    running.add_argument("--branch", default="", help="the branch to check out (sets the branch input)")
    running.add_argument("--agent", default="", help="the agent to run (sets the agent input)")
    running.add_argument("--repo", default="", help="repository to check out")
    running.add_argument("--wait", action="store_true", help="wait for the run to finish")
    running.add_argument("--timeout", type=float, default=0.0, metavar="SECONDS", help="give up waiting after this long")
    running.add_argument("--idempotency-key", default="", help="reuse to make a resubmission safe")
    graphs = commands.add_parser("graphs", parents=[listed], help="list graphs")
    graphs.add_argument("action", nargs="?", choices=("list",), default="list")
    graphs.add_argument("--backend", metavar="NAME")
    graphs.add_argument("--project", metavar="NAME")
    graphs.add_argument("--all-projects", action="store_true")

    run = commands.add_parser("run", help="inspect a run")
    actions = run.add_subparsers(dest="action", required=True)
    getting = actions.add_parser("get", parents=[common], help="status, nodes, results and failure")
    getting.add_argument("run_id")
    waiting = actions.add_parser("wait", parents=[common], help="wait for a run to finish")
    waiting.add_argument("run_id")
    waiting.add_argument("--timeout", type=float, default=0.0, metavar="SECONDS")
    runs = commands.add_parser("runs", parents=[listed], help="list runs, newest first")
    runs.add_argument("action", nargs="?", choices=("list",), default="list")
    runs.add_argument("--backend", metavar="NAME")
    runs.add_argument("--project", metavar="NAME")
    runs.add_argument("--all-projects", action="store_true")
    runs.add_argument("--graph", default="", help="only runs of this graph")
    runs.add_argument("--loop", default="", help="only runs this loop started")
    runs.add_argument("--status", default="", help="only runs in this status, such as running or failed")
    runs.add_argument("--limit", type=int, default=20, help="at most this many (default: 20)")

    loop = commands.add_parser("loop", help="recurring runs of a graph, within limits")
    actions = loop.add_subparsers(dest="action", required=True)
    actions.add_parser(
        "spec", help="print the current loop specification",
        description=getdoc(loop_spec), formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    adding = actions.add_parser("add", parents=[scoped], help="run a graph on a cadence")
    adding.add_argument("graph")
    adding.add_argument("--name", default="", help="loop name (default: the graph's)")
    adding.add_argument("--instruction", default="", help="override the graph's loop.instruction")
    adding.add_argument("--every", default=None, help="cadence such as 30m, 6h, 1d (default: the graph's loop.every)")
    adding.add_argument("--max-prs", type=int, default=None, help="pause after this many new pull requests")
    adding.add_argument("--max-spend", type=float, default=None, metavar="USD", help="pause, and stop the active run, at this spend")
    adding.add_argument("--repo", default="")
    adding.add_argument("--input", "-i", action="append", default=[], metavar="NAME=VALUE")
    adding.add_argument("--no-run-now", action="store_true", help="first run after one interval, not now")
    for name, help_text in (("get", "one loop's state and budget"), ("pause", "stop starting runs"), ("resume", "start runs again")):
        action = actions.add_parser(name, parents=[scoped], help=help_text)
        action.add_argument("loop", help="a loop name or id")
        if name == "pause":
            action.add_argument("--reason", default="")
        if name == "resume":
            action.add_argument("--max-prs", type=int, default=None)
            action.add_argument("--max-spend", type=float, default=None, metavar="USD")
    loops = commands.add_parser("loops", parents=[listed], help="list loops")
    loops.add_argument("action", nargs="?", choices=("list",), default="list")
    loops.add_argument("--backend", metavar="NAME")
    loops.add_argument("--project", metavar="NAME")
    loops.add_argument("--all-projects", action="store_true")

    nodes = commands.add_parser("nodes", parents=[listed], help="list a run's node executions")
    nodes.add_argument("action", nargs="?", choices=("list",), default="list")
    nodes.add_argument("--run", required=True, metavar="RUN_ID")
    nodes.add_argument("--backend", metavar="NAME")
    node = commands.add_parser("node", help="inspect or steer one node execution")
    actions = node.add_subparsers(dest="action", required=True)
    getting = actions.add_parser("get", parents=[common], help="one node execution and its steering")
    getting.add_argument("execution_id")
    steering = actions.add_parser("steer", parents=[common], help="send a running node an updated instruction")
    steering.add_argument("execution_id")
    steering.add_argument("message")
    steering.add_argument("--idempotency-key", default="", help="reuse to make a resend safe")
    steering.add_argument("--wait", action="store_true", help="wait until it is applied or cannot be")
    steering.add_argument("--timeout", type=float, default=0.0, metavar="SECONDS")

    agent = commands.add_parser("agent", help="add, inspect, remove and sign in the agents graphs name")
    actions = agent_actions = agent.add_subparsers(dest="action", required=True)
    adding = actions.add_parser("add", parents=[common], help="offer claude, codex or opencode under a name")
    adding.add_argument("kind", choices=AGENT_KINDS)
    adding.add_argument("--name", default="", help="what graphs call it (default: the kind)")
    adding.add_argument("--model", default="", help="the model it runs when a node names none")
    adding.add_argument("--url", default="", help="an OpenAI-compatible endpoint (opencode only; needs --model)")
    adding.add_argument("--replace", action="store_true", help="overwrite an agent with this name")
    for name, help_text in (("get", "one agent"), ("remove", "stop offering an added agent")):
        action = actions.add_parser(name, parents=[common], help=help_text)
        action.add_argument("name")
    signin = actions.add_parser("signin", help="sign an agent in where the backend runs")
    signin.add_argument("name", help="an agent name, such as claude")
    signin.add_argument("--backend", metavar="NAME")
    agents = commands.add_parser("agents", parents=[listed], help="list agents")
    agents.add_argument("action", nargs="?", choices=("list",), default="list")
    agents.add_argument("--backend", metavar="NAME")

    # `engine runner signin` before agents had their own command; kept for one release.
    # Without `help`, it is left out of the command list; HIDDEN keeps it out of the usage line.
    runner = commands.add_parser("runner")
    actions = runner.add_subparsers(dest="action", required=True)
    signin = actions.add_parser("signin")
    signin.add_argument("name", choices=sorted(SIGNIN))
    signin.add_argument("--backend", metavar="NAME")
    return agent_actions


def main(arguments: argparse.Namespace) -> int:
    try:
        handler = _HANDLERS[(arguments.command, getattr(arguments, "action", None))]  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md
        return handler(arguments)
    except (BackendError, RequestFailed, _UsageError, OSError) as error:
        if isinstance(error, _UsageError):
            print(f"engine: {error}", file=sys.stderr)
            return EXIT_USAGE
        _report(error)
        return EXIT_FAILED


# --- backends ---------------------------------------------------------------


def backend_add(arguments: argparse.Namespace) -> int:
    config = backends.add(
        arguments.name, arguments.url, project=arguments.project, token_env=arguments.token_env,
        repository=arguments.repo, use=arguments.use, replace_existing=arguments.replace,
    )
    added = config.get(arguments.name)
    _emit(added.json(current=config.current == added.name), arguments.pretty, _backend_line)
    return EXIT_OK


def backend_list(arguments: argparse.Namespace) -> int:
    config = backends.load()
    rows = []
    for backend in config.all().values():
        row = backend.json(current=backend.name == config.current)
        if getattr(arguments, "check", False):
            row["health"] = _health(backend)
        rows.append(row)
    if not arguments.json:
        _table(rows, [("", lambda r: "*" if r["current"] else " "), ("NAME", "name"), ("URL", "url"),
                      ("PROJECT", "project"), ("TOKEN", lambda r: r["token_env"] or "-"),
                      *([("HEALTH", "health")] if getattr(arguments, "check", False) else [])])
    else:
        _print({"current": config.current, "backends": rows})
    return EXIT_OK


def backend_use(arguments: argparse.Namespace) -> int:
    config = backends.use(arguments.name)
    _emit(config.get(arguments.name).json(current=True), arguments.pretty, _backend_line)
    return EXIT_OK


def backend_remove(arguments: argparse.Namespace) -> int:
    config = backends.remove(arguments.name)
    _emit({"removed": arguments.name, "current": config.current}, arguments.pretty,
          lambda body: f"removed {body['removed']}; selected backend is {body['current']}")
    return EXIT_OK


# --- graphs -----------------------------------------------------------------


def graph_add(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    format, source = _read_graph(arguments.file)
    try:
        graph = client.post("/graphs", {"project": _project(arguments, backend), "format": format, "source": source})
    except RequestFailed as refused:
        if refused.payload.get("problems"):
            for problem in refused.payload["problems"]:
                print(f"  {problem['path']}: {problem['message']}", file=sys.stderr)
        raise
    _emit(graph, arguments.pretty, lambda g: (
        f"{'registered' if g['created'] else 'unchanged'} {g['name']} v{g['version']} "
        f"({g['graphId']}, version {g['versionId']}) in project {g['project']}"
    ))
    return EXIT_OK


def graphs_list(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    project = None if arguments.all_projects else _project(arguments, backend)
    graphs = client.get("/graphs", project=project)["graphs"]
    if not arguments.json:
        _table(graphs, [("NAME", "name"), ("VERSION", "version"), ("PROJECT", "project"),
                        ("GRAPH ID", "graphId"), ("UPDATED", "updatedAt"), ("DESCRIPTION", "description")])
    else:
        _print({"graphs": graphs})
    return EXIT_OK


def graph_get(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    graph = client.get(f"/graphs/{quote(arguments.graph, safe='@')}", project=_project(arguments, backend))
    if arguments.pretty:
        print(f"{graph['name']} v{graph['version']}  ({graph['graphId']}, version {graph['versionId']})")
        print(f"project: {graph['project']}")
        if graph.get("description"):
            print(graph["description"])
        print("versions: " + ", ".join(f"v{v['version']} {v['versionId']}" for v in graph.get("versions", [])))
        print()
        # What was registered, as written; `definition` is the backend's parse of it.
        print((graph.get("source") or yaml.safe_dump(graph["definition"], sort_keys=False)).rstrip())
    else:
        _print(graph)
    return EXIT_OK


def graph_run(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    key = arguments.idempotency_key or f"cli-{uuid.uuid4()}"
    inputs = _inputs(arguments.input)
    for name in ("branch", "agent"):
        if getattr(arguments, name):
            inputs[name] = getattr(arguments, name)
    request = {
        "project": _project(arguments, backend),
        "graph": arguments.graph,
        "instruction": arguments.instruction or _default_instruction(arguments),
        "inputs": inputs,
        "repository": _repository(arguments, backend),
        "idempotencyKey": key,
    }
    try:
        run = client.post("/runs", request, idempotent=True)
    except RequestFailed as refused:
        if refused.status != 404 or arguments.graph not in starters.NAMES:
            raise
        # A built-in graph this project has not run yet: register it, then run it.
        client.post("/graphs", {"project": request["project"], "format": "yaml",
                                "source": starters.source(arguments.graph)})
        run = client.post("/runs", request, idempotent=True)
    if arguments.wait:
        if not arguments.pretty:
            print(json.dumps({"runId": run["runId"], "status": run["status"]}), file=sys.stderr)
        else:
            print(f"started {run['runId']} ({run['graph']} v{run['version']}); waiting...", file=sys.stderr)
        run = _wait_for_run(client, run["runId"], arguments.timeout)
    return _show_run(run, backend, arguments.pretty)


def runs_list(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    project = None if arguments.all_projects else _project(arguments, backend)
    runs = client.get(
        "/runs", project=project, graph=arguments.graph or None, loop=arguments.loop or None,
        status=arguments.status or None, limit=str(arguments.limit),
    )["runs"]
    if not arguments.json:
        _table(runs, [
            ("RUN ID", "runId"), ("GRAPH", lambda r: f"{r['graph']} v{r['version']}"), ("STATUS", "status"),
            ("LOOP", "loop"), ("STARTED", "startedAt"), ("SPEND", _run_spend),
        ])
    else:
        _print({"runs": runs})
    return EXIT_OK


def run_get(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    return _show_run(client.get(f"/runs/{quote(arguments.run_id)}"), backend, arguments.pretty)


def run_wait(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    return _show_run(_wait_for_run(client, arguments.run_id, arguments.timeout), backend, arguments.pretty)


# --- loops ------------------------------------------------------------------


def loop_add(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    loop = client.post("/loops", {
        "project": _project(arguments, backend),
        "graph": arguments.graph,
        "name": arguments.name,
        "instruction": arguments.instruction,
        "every": arguments.every,
        "maxPrs": arguments.max_prs,
        "maxSpendUsd": arguments.max_spend,
        "repository": _repository(arguments, backend),
        "inputs": _inputs(arguments.input),
        "startNow": not arguments.no_run_now,
    })
    _emit(loop, arguments.pretty, _loop_summary)
    if loop["limits"]["maxSpendUsd"] is not None:
        print(f"note: max-spend counts {loop['limits']['spendScope'][0].lower()}{loop['limits']['spendScope'][1:]}",
              file=sys.stderr)
    return EXIT_OK


def loops_list(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    project = None if arguments.all_projects else _project(arguments, backend)
    loops = client.get("/loops", project=project)["loops"]
    if not arguments.json:
        _table(loops, [
            ("NAME", "name"), ("GRAPH", lambda l: f"{l['graph']} v{l['version']}"), ("STATE", "state"),
            ("EVERY", lambda l: _duration(l["everySeconds"])),
            ("NEXT", lambda l: l["nextRunAt"] or "-"), ("ACTIVE RUN", lambda l: l["activeRunId"] or "-"),
            ("PRS", lambda l: f"{l['prCount']}/{l['limits']['maxPrs'] or '∞'}"),
            ("SPEND", _spend), ("PAUSED BECAUSE", lambda l: l["pauseReason"] or "-"),
        ])
    else:
        _print({"loops": loops})
    return EXIT_OK


def loop_get(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    _emit(client.get(f"/loops/{quote(arguments.loop)}", project=_project(arguments, backend)),
          arguments.pretty, _loop_summary)
    return EXIT_OK


def loop_pause(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    loop = client.post(f"/loops/{quote(arguments.loop)}/pause",
                       {"project": _project(arguments, backend), "reason": arguments.reason})
    _emit(loop, arguments.pretty, _loop_summary)
    return EXIT_OK


def loop_resume(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    loop = client.post(f"/loops/{quote(arguments.loop)}/resume", {
        "project": _project(arguments, backend),
        "maxPrs": arguments.max_prs,
        "maxSpendUsd": arguments.max_spend,
    })
    _emit(loop, arguments.pretty, _loop_summary)
    return EXIT_OK


# --- nodes ------------------------------------------------------------------


def nodes_list(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    nodes = client.get(f"/runs/{quote(arguments.run)}/nodes")["nodes"]
    if not arguments.json:
        _table(nodes, _NODE_COLUMNS)
    else:
        _print({"nodes": nodes})
    return EXIT_OK


def node_get(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    node = client.get(f"/nodes/{quote(arguments.execution_id)}")
    if arguments.pretty:
        _table([node], _NODE_COLUMNS)
        if node.get("steering"):
            print()
            _table(node["steering"], _STEERING_COLUMNS)
    else:
        _print(node)
    return EXIT_OK


def node_steer(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    key = arguments.idempotency_key or f"cli-{uuid.uuid4()}"
    path = f"/nodes/{quote(arguments.execution_id)}/steering"
    steering = client.post(path, {"message": arguments.message, "idempotencyKey": key}, idempotent=True)
    if arguments.wait:
        steering = _wait_for_steering(client, arguments.execution_id, steering["steeringId"], arguments.timeout)
    _emit(steering, arguments.pretty, lambda s: (
        f"{s['steeringId']} {s['status']}" + (f": {s['error']}" if s.get("error") else "")
    ))
    return EXIT_OK if steering["status"] not in ("rejected", "undelivered") else EXIT_FAILED


# --- agents -----------------------------------------------------------------


def agent_add(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    agent = client.post("/agents", {
        "kind": arguments.kind, "name": arguments.name, "model": arguments.model,
        "url": arguments.url, "replace": arguments.replace,
    })
    _emit(agent, arguments.pretty, _agent_line)
    return EXIT_OK


def agents_list(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    agents = client.get("/agents")["agents"]
    if not arguments.json:
        _table(agents, _AGENT_COLUMNS)
    else:
        _print({"agents": agents})
    return EXIT_OK


def agent_get(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    _emit(client.get(f"/agents/{quote(arguments.name)}"), arguments.pretty, _agent_line)
    return EXIT_OK


def agent_remove(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    removed = client.delete(f"/agents/{quote(arguments.name)}")
    _emit(removed, arguments.pretty, lambda body: f"removed {body['name']}")
    return EXIT_OK


def agent_signin(arguments: argparse.Namespace) -> int:
    """Sign in the harness an agent runs on, on the machine its backend runs on."""
    backend, client = _connect(arguments)
    if arguments.name in SIGNIN:
        kind, url = arguments.name, ""
    else:
        agent = client.get(f"/agents/{quote(arguments.name)}")
        kind, url = agent["kind"], agent["url"]
    if url:
        print(f"{arguments.name} reaches its model at {url}; it has nothing to sign in to.")
        return EXIT_OK
    command, follow_up = SIGNIN[kind]
    shown = " ".join(command)
    if not backend.is_local:
        host = urlsplit(backend.url).hostname
        print(
            f"{arguments.name} signs in on the machine backend {backend.name} runs on. There, as the "
            f"user its daemon runs as, run:\n\n    {shown}\n"
            + (f"\nthen {follow_up}.\n" if follow_up else "")
            + f"\nFor example: ssh -t {host} {shown}"
        )
        return EXIT_OK
    if shutil.which(command[0]) is None:
        print(f"engine: {command[0]} is not installed here; install it, then run `engine daemon setup`",
              file=sys.stderr)
        return EXIT_FAILED
    if follow_up:
        print(f"Starting {shown}: {follow_up}.", file=sys.stderr)
    return subprocess.call(command)


def runner_signin(arguments: argparse.Namespace) -> int:
    print(f"engine: `engine runner signin` is now `engine agent signin {arguments.name}`", file=sys.stderr)
    return agent_signin(arguments)


# --- helpers ----------------------------------------------------------------


class _UsageError(ValueError):
    pass


def _connect(arguments: argparse.Namespace) -> tuple[Backend, Client]:
    backend = backends.load().selected(getattr(arguments, "backend", None))
    return backend, Client(backend)


def _project(arguments: argparse.Namespace, backend: Backend) -> str:
    return getattr(arguments, "project", None) or backend.project


def _repository(arguments: argparse.Namespace, backend: Backend) -> str:
    """`--repo`, else the repository you are in, else the backend's default.

    A local daemon is sent the checkout's path; a remote one its `owner/repo`.
    """
    if arguments.repo:
        return arguments.repo
    here = repository.current()
    if here is not None:
        return here.target(local=backend.is_local)
    return backend.repository


def _read_graph(file: str) -> tuple[str, str]:
    """The graph's format and its source, sent verbatim so the backend keeps it."""
    text = sys.stdin.read() if file == "-" else Path(file).read_text(encoding="utf-8")
    if file.endswith(".py"):
        return "python", text
    try:
        manifest = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise _UsageError(f"{file} is not valid YAML or JSON: {error}") from None
    if not isinstance(manifest, dict):
        raise _UsageError(f"{file} must hold a mapping")
    return "yaml", text


def _default_instruction(arguments: argparse.Namespace) -> str:
    """What a run is asked when no instruction is given: just to run the graph."""
    return f"Run {arguments.graph}" + (f" on {arguments.branch}" if arguments.branch else "") + "."


def _inputs(pairs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator or not name.strip():
            raise _UsageError(f"--input takes NAME=VALUE, not {pair!r}")
        values[name.strip()] = value
    return values


def _wait_for_run(client: Client, run_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout if timeout > 0 else None
    while True:
        run = client.get(f"/runs/{quote(run_id)}")
        if run["terminal"]:
            return run
        if run["status"] == "awaiting_approval" and run.get("pendingApprovals"):
            first = run["pendingApprovals"][0]
            print(f"waiting on approval {first['approvalId']} at {first['node']}: {first['reason']}",
                  file=sys.stderr)
        if deadline is not None and time.monotonic() >= deadline:
            print(f"engine: still {run['status']} after {timeout:g}s", file=sys.stderr)
            return run
        time.sleep(WAIT_INTERVAL_SECONDS)


def _wait_for_steering(client: Client, execution_id: str, steering_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout if timeout > 0 else None
    while True:
        node = client.get(f"/nodes/{quote(execution_id)}")
        steering = next(item for item in node["steering"] if item["steeringId"] == steering_id)
        if steering["status"] in TERMINAL_STEERING:
            return steering
        if deadline is not None and time.monotonic() >= deadline:
            return steering
        time.sleep(WAIT_INTERVAL_SECONDS)


def _show_run(run: dict[str, Any], backend: Backend, pretty: bool) -> int:
    auth = (run.get("failure") or {}).get("authRequired")
    if auth and backend.name != backends.LOCAL:
        auth["command"] = f"{auth['command']} --backend {backend.name}"
    if pretty:
        print(f"{run['runId']}  {run['graph']} v{run['version']}  {run['status']}")
        if run.get("nodes"):
            _table(run["nodes"], _NODE_COLUMNS)
        for node, value in (run.get("results") or {}).items():
            print(f"\n[{node}]\n{value}")
        cost = run["usage"].get("costUsd")
        print(f"\nusage: {'unknown' if cost is None else f'${cost:.2f}'}"
              + ("" if run["usage"].get("complete", True) else " (some costs unknown)"))
        for pull in run.get("pullRequests") or []:
            print(f"pull request: {pull['repository']}#{pull['number']}")
    else:
        _print(run)
    failure = run.get("failure")
    if failure:
        print(f"engine: run failed{' at ' + failure['node'] if failure.get('node') else ''}: {failure['error']}",
              file=sys.stderr)
        if auth:
            print(f"engine: {auth['runner'] or 'a runner'} is not signed in; run `{auth['command']}`",
                  file=sys.stderr)
    return EXIT_FAILED if run["status"] == "failed" else EXIT_OK


def _health(backend: Backend) -> str:
    try:
        info = Client(backend, timeout=3.0).get("/backend")
    except RequestFailed as error:
        return f"down: {error}"
    return "up (" + ", ".join(info.get("runners", [])) + ")"


def _report(error: Exception) -> None:
    print(f"engine: {error}", file=sys.stderr)
    payload = getattr(error, "payload", None) or {}
    for candidate in payload.get("candidates") or []:
        print(f"  {json.dumps(candidate)}", file=sys.stderr)


def _print(payload: object) -> None:
    print(json.dumps(payload))


def _emit(payload: dict[str, Any], pretty: bool, line: Any) -> None:
    print(line(payload) if pretty else json.dumps(payload))


def _table(rows: list[dict[str, Any]], columns: list[tuple[str, Any]]) -> None:
    def cell(row: dict[str, Any], key: Any) -> str:
        value = key(row) if callable(key) else row.get(key)
        return "-" if value is None or value == "" else str(value)

    cells = [[cell(row, key) for _, key in columns] for row in rows]
    widths = [max([len(header), *(len(row[index]) for row in cells)]) for index, (header, _) in enumerate(columns)]
    print("  ".join(header.ljust(width) for (header, _), width in zip(columns, widths)).rstrip())
    for row in cells:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def _backend_line(body: dict[str, Any]) -> str:
    return f"{'* ' if body['current'] else ''}{body['name']}  {body['url']}  project {body['project']}"


def _agent_line(body: dict[str, Any]) -> str:
    detail = " ".join(part for part in (body.get("model") or "", body.get("url") or "") if part)
    return f"{body['name']}  {body['kind']}{'  ' + detail if detail else ''}{'  (built in)' if body.get('builtin') else ''}"


def _loop_summary(loop: dict[str, Any]) -> str:
    lines = [
        f"{loop['name']} ({loop['loopId']})  {loop['state']}"
        + (f": {loop['pauseReason']}" if loop.get("pauseReason") else ""),
        f"graph {loop['graph']} v{loop['version']} every {_duration(loop['everySeconds'])}, next {loop['nextRunAt'] or '-'}",
        f"pull requests {loop['prCount']}/{loop['limits']['maxPrs'] or '∞'}, spend {_spend(loop)}",
    ]
    if loop.get("activeRunId"):
        lines.append(f"active run {loop['activeRunId']}")
    latest = loop.get("latestOutput")
    if latest:
        lines.append(f"\nlatest output: {latest['node']} of {latest['runId']}, finished {latest['finishedAt']}")
        lines.append(_output_text(latest["value"]))
    elif "latestOutput" in loop:
        lines.append("latest output: none yet")
    return "\n".join(lines)


def _output_text(value: object) -> str:
    """A node's result for reading: text as written, anything else as indented JSON."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return json.dumps(value, indent=2)
    blocks = []
    for key, item in value.items():
        if key == "runner":
            continue
        text = item if isinstance(item, str) else json.dumps(item, indent=2)
        blocks.append(f"{key}:\n{text}" if "\n" in text else f"{key}: {text}")
    return "\n\n".join(blocks)


def _spend(loop: dict[str, Any]) -> str:
    spent = loop["spend"]
    cap = loop["limits"]["maxSpendUsd"]
    text = f"${spent['usd']:.2f}" + (f"/${cap:.2f}" if cap is not None else "")
    if not spent["complete"]:
        text += " (+unknown; cap not enforceable)" if cap is not None else " (+unknown)"
    return text


def _run_spend(run: dict[str, Any]) -> str | None:
    cost = run["usage"]["costUsd"]
    return None if cost is None else f"${cost:.2f}" + ("" if run["usage"]["complete"] else " (+unknown)")


def _duration(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


_AGENT_COLUMNS: list[tuple[str, Any]] = [
    ("NAME", "name"), ("KIND", "kind"), ("MODEL", "model"), ("URL", "url"),
    ("", lambda row: "built in" if row.get("builtin") else ""),
]
_NODE_COLUMNS: list[tuple[str, Any]] = [
    ("NODE", "node"), ("EXECUTION", "executionId"), ("ATTEMPT", "attempt"), ("STATUS", "status"),
    ("RUNNER", "runner"), ("STARTED", "startedAt"), ("FINISHED", "finishedAt"),
]
_STEERING_COLUMNS: list[tuple[str, Any]] = [
    ("#", "sequence"), ("STEERING", "steeringId"), ("STATUS", "status"), ("ACCEPTED", "acceptedAt"),
    ("DELIVERED", "deliveredAt"), ("APPLIED", "appliedAt"), ("MESSAGE", "message"),
]

_HANDLERS = {
    ("backend", "add"): backend_add,
    ("backend", "list"): backend_list,
    ("backend", "use"): backend_use,
    ("backend", "remove"): backend_remove,
    ("backends", "list"): backend_list,
    ("graph", "spec"): graph_spec,
    ("graph", "add"): graph_add,
    ("graph", "get"): graph_get,
    ("graph", "run"): graph_run,
    ("graphs", "list"): graphs_list,
    ("run", "get"): run_get,
    ("run", "wait"): run_wait,
    ("runs", "list"): runs_list,
    ("loop", "spec"): loop_spec,
    ("loop", "add"): loop_add,
    ("loop", "get"): loop_get,
    ("loop", "pause"): loop_pause,
    ("loop", "resume"): loop_resume,
    ("loops", "list"): loops_list,
    ("nodes", "list"): nodes_list,
    ("node", "get"): node_get,
    ("node", "steer"): node_steer,
    ("agent", "add"): agent_add,
    ("agent", "get"): agent_get,
    ("agent", "remove"): agent_remove,
    ("agent", "signin"): agent_signin,
    ("agents", "list"): agents_list,
    ("runner", "signin"): runner_signin,
}


__all__ = ["COMMANDS", "add_parsers", "main"]
