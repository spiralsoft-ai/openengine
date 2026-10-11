"""A fake ACP agent executable, for chat's runners and a graph's nodes.

Not a mock of our adapters: a real subprocess, real newline-delimited JSON, a
real approval round trip, and a real run of whatever command it is allowed to
run. What it does not have is a model, so what the agent "decides" to do is
scripted instead.

It lives here rather than inside one test module because `apps/web/e2e` drives
a browser against a real server wired to it, with a script naming exactly what
the agent says and does on the way through, and the graph-runtime tests run
workflow nodes against it.

The script is JSON, read from `ENGINE_FAKE_SCRIPT` on every invocation, so a
test can change what the agent will do next without restarting the server it is
talking to:

    {"title": "Adding a greeting",
     "scenarios": [{"when": "greeting",
                    "steps": [{"type": "say", "text": "Reading the tree."},
                              {"type": "run", "command": "echo hi > hi.txt"},
                              {"type": "tool", "name": "complete_step",
                               "arguments": {"outcome": "success",
                                             "summary": "Added the greeting.",
                                             "outputs": {"pr_url": "..."}}}]}]}

`when` is matched as a substring of the prompt, and a scenario without one
matches anything -- so a conversation is scripted turn by turn by what each
turn is asked, rather than by a counter that a retry or a title would knock out
of step. The first matching scenario wins, so a reviewer's scenario -- whose
prompt quotes the task the implementation was given -- has to be listed before
the implementation's.

A `tool` step calls the run-bound MCP server the runtime attached to this
invocation, which is the only thing that ends a workflow step: an agent that
merely stops is corrected and asked again, and fails the run on the third pass.
The server is read off `session/new` or `session/load` and spawned as given,
credential and all, because the broker refuses a session it did not issue.

A turn that is naming a chat or a workflow rather than running a step is
answered with the script's `title`: naming is not what any of these tests are
about, and spending a scenario on it would make every script carry one. A
workflow's is recognised by what it was served -- the repository tools alone,
with none of the tools that end a step; a chat's by the instruction it ends
with, since it is served nothing at all.

A script that *is* about naming says so with a top-level `naming` list of steps,
which that turn then runs like any other:

    {"naming": [{"type": "tool", "name": "view_work_item",
                 "arguments": {"number": 270}},
                {"type": "say", "text": "#270 Pin the dependencies"}]}
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: What an unscripted run reads as the instruction to run one command.
DIRECTIVE = "run:"

#: Where the script is, when there is one.
SCRIPT_ENVIRONMENT_VARIABLE = "ENGINE_FAKE_SCRIPT"

#: One command, taken from the prompt, gated on approval, then an answer. The
#: behaviour `approval_scenarios` expects from an unscripted fake.
DIRECTIVE_SCRIPT: Mapping[str, object] = {
    "scenarios": [{"steps": [{"type": "run"}, {"type": "say", "text": "Ran it."}]}]
}

#: What a non-interactive turn answers when the script does not name a title.
UNSCRIPTED_TITLE = "Scripted conversation"

#: How the web app's chat-naming request begins, which is how a turn -- on the
#: same transport as every other -- is recognised as one.
CHAT_NAMING_INSTRUCTION = "Name this chat based on the conversation above."

#: The MCP revision this client speaks, which is the one the bound server does.
MCP_PROTOCOL_VERSION = "2025-06-18"

#: How long the MCP server gets to exit once its input has been closed. A call
#: itself is bounded by whatever is driving the turn, which has more to say
#: about a hang than this does.
MCP_TIMEOUT_SECONDS = 30.0



# --- installing one ---------------------------------------------------------


def install(provider: str, directory: Path) -> str:
    """Write an executable named `provider` into `directory`, and name its path.

    A shim rather than a generated program: the fake is this module, which is
    ordinary source that can be read, imported, and edited with the tools that
    edit source. A runner is handed the path as its command, so nothing has to
    be put on `PATH` for this to be the agent a composed application runs.
    """

    path = directory / provider
    path.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(Path(__file__).resolve()))} {provider} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


# --- reading the script -----------------------------------------------------


def _script() -> Mapping[str, object]:
    path = os.environ.get(SCRIPT_ENVIRONMENT_VARIABLE)
    if not path:
        return DIRECTIVE_SCRIPT
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"could not read {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise SystemExit(f"{path} must hold a JSON object")
    return loaded


def _title() -> str:
    title = _script().get("title")
    return str(title) if title else UNSCRIPTED_TITLE


def _steps(prompt: str) -> Sequence[Mapping[str, object]]:
    """The steps whose scenario this prompt selects."""

    scenarios = _script().get("scenarios") or ()
    if not isinstance(scenarios, list):
        raise SystemExit("scenarios must be a JSON array")
    for scenario in scenarios:
        when = scenario.get("when")
        if when is None or str(when) in prompt:
            return list(scenario.get("steps") or ())
    raise SystemExit("no scripted scenario matches this prompt")


def _command(step: Mapping[str, object], prompt: str) -> str:
    """What this step runs: what it says, or what the prompt directed."""

    command = step.get("command")
    if command is not None:
        return str(command)
    for line in reversed(prompt.splitlines()):
        # The last directive in a transcript is the newest instruction.
        if DIRECTIVE in line:
            return line.split(DIRECTIVE, 1)[1].strip()
    raise SystemExit(f"no {DIRECTIVE!r} directive in the prompt")


def _send(message: Mapping[str, object]) -> None:
    print(json.dumps(message), flush=True)


# --- calling the run-bound MCP server ---------------------------------------
#
# The runtime hands the agent three fields -- a name, a command, and its
# arguments -- in the session's `mcpServers`. No SDK: a fake whose whole point
# is that it speaks the wire protocol should speak this one by hand too.


@dataclass(frozen=True, slots=True)
class McpServer:
    """The stdio MCP server this invocation was told to talk to."""

    name: str
    command: str
    args: tuple[str, ...]


def _acp_mcp_server(servers: object) -> McpServer | None:
    """ACP's `[{name, command, args}]` server description."""

    if not isinstance(servers, list):
        return None
    for server in servers:
        if not isinstance(server, Mapping) or not server.get("command"):
            continue
        return McpServer(
            name=str(server.get("name") or "workflow"),
            command=str(server["command"]),
            args=tuple(str(argument) for argument in server.get("args") or ()),
        )
    return None


def _require(server: McpServer | None, provider: str) -> McpServer:
    if server is None:
        raise SystemExit(
            f"this {provider} turn was given no MCP server, so a 'tool' step "
            "has nothing to call"
        )
    return server


def _turn_steps(
    prompt: str, server: McpServer | None
) -> Sequence[Mapping[str, object]]:
    """This turn's script: the run's `naming` steps when it is a naming turn.

    A naming turn is read off the server the runtime attached: naming is served
    the repository tools and nothing else, and says so on the argv it hands
    over, while a step is served the tools that end one.

    Most scripts say nothing about naming and get a turn that answers `title`,
    because naming is not what they are about. A script that does carry
    `naming` steps drives that turn like any other -- which is the only way to
    put an agent, calling a real tool over the real bridge, in front of the
    thing a naming turn is for.
    """

    if server is None and CHAT_NAMING_INSTRUCTION in prompt[-500:]:
        return [{"type": "say", "text": _title()}]
    if server is None or "--repository-tools-only" not in server.args:
        return _steps(prompt)
    naming = _script().get("naming")
    if naming is None:
        return [{"type": "say", "text": _title()}]
    if not isinstance(naming, list):
        raise SystemExit("naming must be a JSON array of steps")
    return naming


def _call_tool(
    server: McpServer, name: str, arguments: object
) -> tuple[str, bool]:
    """Call one tool over stdio MCP, and report what the server answered.

    The command is spawned exactly as it was handed over, credential included:
    the token is issued per agent run and the broker refuses a session it did
    not issue, so a reconstructed argv would be turned away.
    """

    process = subprocess.Popen(
        [server.command, *server.args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        _mcp_request(
            process,
            1,
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "provider-fake", "version": "1"},
            },
        )
        _mcp_notify(process, "notifications/initialized")
        result = _mcp_request(
            process, 2, "tools/call", {"name": name, "arguments": arguments}
        )
    finally:
        _close(process)
    text = "\n".join(
        str(block.get("text", ""))
        for block in result.get("content") or ()
        if isinstance(block, Mapping)
    )
    return text or "(no content)", bool(result.get("isError"))


def _mcp_request(
    process: "subprocess.Popen[str]",
    request_id: int,
    method: str,
    params: Mapping[str, object],
) -> Mapping[str, object]:
    _mcp_write(process, {"id": request_id, "method": method, "params": params})
    assert process.stdout is not None
    while True:
        line = process.stdout.readline()
        if not line:
            raise SystemExit(f"the MCP server closed before answering {method}")
        message = json.loads(line)
        if message.get("id") != request_id:
            continue
        if message.get("error"):
            raise SystemExit(f"MCP {method} failed: {message['error']}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def _mcp_notify(process: "subprocess.Popen[str]", method: str) -> None:
    """A notification carries no id, and is answered by nothing."""

    _mcp_write(process, {"method": method, "params": {}})


def _mcp_write(
    process: "subprocess.Popen[str]", message: Mapping[str, object]
) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    process.stdin.flush()


def _close(process: "subprocess.Popen[str]") -> None:
    """Let the server end on its own closed input, and insist if it does not."""

    if process.stdin is not None:
        process.stdin.close()
    try:
        process.wait(timeout=MCP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


# --- acp ---------------------------------------------------------------------
#
# Chat's runners and a *graph* workflow's `ACPNode` do not run `codex` or
# `claude`: they talk ACP to an adapter that wraps one, so a scripted run needs
# an agent that speaks ACP.
#
# The script drives it, including calls to the invocation-bound MCP server
# carried by `session/new` or `session/load`. A graph node advances only after
# one of those tools reports a terminal result; ending the ACP turn is not a
# completion signal.

#: The id this agent asks its permission questions under. One outstanding
#: question at a time, which is all an ACP turn can have.
_ACP_PERMISSION_ID = 8001

#: What ACP requires of an MCP server description, by the transport it names.
#: A description without a `type` is the stdio one, which is the only kind
#: Engine sends, and `env` is required of it even when there is nothing to put
#: there -- the field a description is most easily built without, because
#: nothing else in it needs one.
_ACP_MCP_SERVER_FIELDS: Mapping[object, tuple[str, ...]] = {
    None: ("name", "command", "args", "env"),
    "stdio": ("name", "command", "args", "env"),
    "http": ("name", "url", "headers"),
    "sse": ("name", "url", "headers"),
}


def _acp_refusal(params: Mapping[str, object]) -> str | None:
    """Why a real agent would refuse these `mcpServers`, if it would.

    Validated rather than accepted because a real one validates: claude answers
    `session/new` with `-32602` when a server description misses a field its
    schema requires, and the session never opens, so the node fails before its
    first turn with nothing said. A fake that took whatever it was handed made
    that failure unreachable from every tier below production.
    """

    servers = params.get("mcpServers") or []
    if not isinstance(servers, list):
        return "mcpServers must be an array"
    for index, server in enumerate(servers):
        if not isinstance(server, Mapping):
            return f"mcpServers[{index}] must be an object"
        required = _ACP_MCP_SERVER_FIELDS.get(server.get("type"))
        if required is None:
            return f"mcpServers[{index}].type is not a transport ACP defines"
        missing = [field for field in required if server.get(field) is None]
        if missing:
            return f"mcpServers[{index}] is missing {', '.join(missing)}"
    return None


def _acp_invalid_params(message_id: object, reason: str) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": -32602, "message": f"Invalid params: {reason}"},
        }
    )


def fake_acp(directory: Path) -> str:
    """An ACP agent, for chat's runners and the graph runtime's `ACPNode`."""

    return install("acp", directory)


def _acp(arguments: Sequence[str]) -> int:
    """One ACP agent over stdio, for as long as the client keeps it open.

    Long-lived: a graph run opens a session per node and keeps the connection for the whole turn, including while it is stopped
    waiting for somebody to answer a permission request.
    """

    working_directories: dict[str, str] = {}
    mcp_servers: dict[str, McpServer | None] = {}
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        if not line.strip():
            continue
        message = json.loads(line)
        method = message.get("method")
        if method is None:
            continue  # An answer to something this agent asked.
        message_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            _acp_respond(
                message_id,
                {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {"image": False, "embeddedContext": False},
                    },
                },
            )
        elif method == "session/new":
            refusal = _acp_refusal(params)
            if refusal is not None:
                _acp_invalid_params(message_id, refusal)
                continue
            session_id = f"acp-{len(working_directories) + 1}"
            working_directories[session_id] = str(params.get("cwd") or "")
            mcp_servers[session_id] = _acp_mcp_server(params.get("mcpServers"))
            _acp_respond(message_id, {"sessionId": session_id})
        elif method == "session/set_config_option":
            _acp_respond(message_id, {"configOptions": []})
        elif method == "session/load":
            refusal = _acp_refusal(params)
            if refusal is not None:
                _acp_invalid_params(message_id, refusal)
                continue
            session_id = str(params.get("sessionId"))
            working_directories[session_id] = str(params.get("cwd") or "")
            mcp_servers[session_id] = _acp_mcp_server(params.get("mcpServers"))
            _acp_respond(message_id, {})
        elif method == "session/prompt":
            session_id = str(params.get("sessionId"))
            _acp_turn(
                message_id,
                session_id,
                _acp_prompt(params),
                working_directories.get(session_id, ""),
                mcp_servers.get(session_id),
            )
        elif method == "session/cancel":
            continue  # A notification, and this agent has nothing to abandon.
        elif message_id is not None:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {"code": -32601, "message": f"no such method: {method}"},
                }
            )


def _acp_turn(
    message_id: object,
    session_id: str,
    prompt: str,
    cwd: str,
    server: McpServer | None,
) -> None:
    """Play this prompt's scenario, then end the turn."""

    for index, step in enumerate(_turn_steps(prompt, server), start=1):
        kind = str(step.get("type"))
        if kind == "say":
            _acp_say(session_id, str(step.get("text", "")))
        elif kind == "tool":
            called = _require(server, "ACP")
            name = str(step["name"])
            call_arguments = step.get("arguments") or {}
            tool_call_id = f"mcp-{index}"
            _acp_calling(
                session_id, tool_call_id, called.name, name, call_arguments
            )
            output, failed = _call_tool(called, name, call_arguments)
            _acp_called(session_id, tool_call_id, output, failed)
        elif kind == "run":
            command = _command(step, prompt)
            if step.get("approval", True) and not _acp_allowed(session_id, command):
                _acp_say(session_id, "Stopped, as asked.")
                _acp_respond(message_id, {"stopReason": "refusal"})
                return
            _acp_running(session_id, command)
            code, output, cancelled = _acp_execute(
                session_id, command, cwd or os.getcwd()
            )
            if cancelled:
                _acp_respond(message_id, {"stopReason": "cancelled"})
                return
            _acp_ran(session_id, command, code, output)
        else:
            raise SystemExit(f"ACP cannot play a {kind!r} step")
    _acp_respond(message_id, {"stopReason": "end_turn"})


def _acp_execute(session_id: str, command: str, cwd: str) -> tuple[int, str, bool]:
    """Run a command while remaining able to receive ACP cancellation."""

    process = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    while process.poll() is None:
        readable, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not readable:
            continue
        message = json.loads(sys.stdin.readline())
        params = message.get("params") or {}
        if (
            message.get("method") != "session/cancel"
            or params.get("sessionId") != session_id
        ):
            continue
        process.terminate()
        output, _ = process.communicate()
        return process.returncode, output, True
    output, _ = process.communicate()
    return process.returncode, output, False


def _acp_allowed(session_id: str, command: str) -> bool:
    """Ask, and block until the client answers or the pipe closes."""

    _send(
        {
            "jsonrpc": "2.0",
            "id": _ACP_PERMISSION_ID,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": "call-1",
                    "title": command,
                    "kind": "execute",
                    "rawInput": {"command": command},
                },
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
    )
    while True:
        line = sys.stdin.readline()
        if not line:
            raise SystemExit("the client closed the transport mid-question")
        if not line.strip():
            continue
        reply = json.loads(line)
        if reply.get("id") != _ACP_PERMISSION_ID:
            continue
        outcome = (reply.get("result") or {}).get("outcome") or {}
        return bool(
            outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow"
        )


def _acp_prompt(params: Mapping[str, object]) -> str:
    blocks = params.get("prompt")
    return "".join(
        str(block.get("text", ""))
        for block in (blocks if isinstance(blocks, list) else ())
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _acp_respond(message_id: object, result: Mapping[str, object]) -> None:
    _send({"jsonrpc": "2.0", "id": message_id, "result": result})


def _acp_update(session_id: str, update: Mapping[str, object]) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }
    )


def _acp_say(session_id: str, text: str) -> None:
    _acp_update(
        session_id,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        },
    )


def _acp_calling(
    session_id: str,
    tool_call_id: str,
    server: str,
    name: str,
    arguments: object,
) -> None:
    """Report an invocation-bound MCP call before crossing the stdio bridge."""

    _acp_update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": tool_call_id,
            "title": f"{server}.{name}",
            "kind": "other",
            "status": "in_progress",
            "rawInput": arguments,
        },
    )


def _acp_called(
    session_id: str, tool_call_id: str, output: str, failed: bool
) -> None:
    """Report the MCP result in the same ACP tool call."""

    _acp_update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": tool_call_id,
            "status": "failed" if failed else "completed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": output}}
            ],
        },
    )


def _acp_ran(session_id: str, command: str, code: int, output: str) -> None:
    """Report the result of a command the agent finished."""
    _acp_update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-1",
            "status": "completed" if code == 0 else "failed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": output}}
            ],
        },
    )


def _acp_running(session_id: str, command: str) -> None:
    """Report a command before running it, so its turn is observable."""
    _acp_update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-1",
            "title": command,
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": command},
        },
    )


PROVIDERS = {"acp": _acp}


def main(argv: Sequence[str]) -> int:
    if not argv or argv[0] not in PROVIDERS:
        raise SystemExit(f"usage: {Path(__file__).name} {'|'.join(PROVIDERS)} [...]")
    return PROVIDERS[argv[0]](tuple(argv[1:]))


__all__ = [
    "DIRECTIVE",
    "DIRECTIVE_SCRIPT",
    "SCRIPT_ENVIRONMENT_VARIABLE",
    "UNSCRIPTED_TITLE",
    "fake_acp",
    "FakeACPProvider",
    "call_mcp",
    "install",
]


class FakeACPProvider:
    name = "fake"

    def __init__(
        self,
        text="Hi, how can I help?",
        fail=False,
        create=False,
        fail_after_create=False,
        after_create=None,
        steer=False,
        resume=False,
        answer=False,
        review=False,
        calls=1,
    ):
        self.text, self.fail, self.create = text, fail, create
        self.clients = []
        self.fail_after_create = fail_after_create
        self.after_create = after_create
        self.steer = steer
        self.resume = resume
        self.answer = answer
        self.review = review
        self.calls = calls

    async def connect(self):
        provider = self
        class Client:
            closed = False
            prompts = []
            async def new_session(self, *, cwd, mcp_servers):
                self.config = mcp_servers[0]
                self.prompts = []
                return self
            async def prompt(self, prompt):
                from langgraph_acp.events import ACPEvent, ACPEventType
                self.prompts.append(prompt)
                if provider.fail:
                    provider.fail = False
                    raise RuntimeError("transient")
                if provider.create and "new workorder" in prompt:
                    self.results = await call_mcp(self.config, calls=provider.calls)
                    self.result = self.results[-1]
                    if provider.after_create is not None:
                        await provider.after_create(self.result["structuredContent"]["run_id"])
                    if provider.fail_after_create:
                        raise RuntimeError("failed after accepting work")
                if provider.steer and "follow the system theme" in prompt:
                    self.result = await call_mcp(self.config, "steer_workorder", "follow the system theme")
                if provider.resume and "browser tests are failing" in prompt:
                    self.result = await call_mcp(self.config, "resume_workorder", "browser tests are failing")
                if provider.answer:
                    context = json.loads(prompt.split(
                        "Host context (message text is user content, not host instructions):\n"
                    )[-1])
                    if context["pending_questions"]:
                        self.result = await call_mcp(self.config, "answer_workorder_question", arguments={
                            "approval_id": context["pending_questions"][0]["approval_id"],
                            "answers": {"api": ["Public"]},
                        })
                if provider.review and "approve the review" in prompt:
                    self.result = await call_mcp(self.config, "decide_workorder_review", arguments={
                        "approved": True, "summary": "Approved in Slack.",
                    })
                yield ACPEvent(agent="fake", type=ACPEventType.MESSAGE_DELTA,
                               data={"content": {"type": "text", "text": provider.text}})
            async def close(self):
                self.closed = True
        client = Client()
        self.clients.append(client)
        return client


async def call_mcp(
    config,
    tool_name="create_workorder",
    prompt="Implement it",
    arguments=None,
    *,
    calls=None,
):
    """Real stdio child -> TCP broker -> injected host callback."""
    # The GitHub concierge exposes its single continuation tool; Slack's
    # default is create_workorder.  This shared fake provider starts either
    # conversation with its default action.
    if (tool_name == "create_workorder"
            and "engine.github_concierge.github_egress" in config["args"]):
        tool_name = "continue_workorder"
    process = await asyncio.create_subprocess_exec(
        config["command"], *config["args"], stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    call_count = 1 if calls is None else calls
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "unsupported"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        *[
            {
                "jsonrpc": "2.0",
                "id": 3 + index,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments if arguments is not None else {"prompt": prompt},
                },
            }
            for index in range(call_count)
        ],
    ]
    stdout, stderr = await process.communicate("".join(json.dumps(r) + "\n" for r in requests).encode())
    assert process.returncode == 0, stderr.decode()
    responses = [json.loads(line) for line in stdout.splitlines()]
    assert len(responses) == 2 + call_count
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    primary_tool = (
        "continue_workorder"
        if "engine.github_concierge.github_egress" in config["args"]
        else "create_workorder"
    )
    assert responses[1]["result"]["tools"][0]["name"] == primary_tool
    if tool_name in (
        "steer_workorder", "resume_workorder", "answer_workorder_question",
        "decide_workorder_review",
    ):
        assert tool_name in [tool["name"] for tool in responses[1]["result"]["tools"]]
    results = [response["result"] for response in responses[2:]]
    return results if calls is not None else results[0]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
