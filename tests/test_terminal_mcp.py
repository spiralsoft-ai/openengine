"""Run-bound MCP terminal tools and their single-result invariants."""

import asyncio
from unittest.mock import AsyncMock
import json
import logging
from collections.abc import Sequence

import pytest

from engine.ports.source_control import ChangeRequest, CommentResult
from engine.runtime.change_requests import change_request, remote_project

from engine.domain import (
    AgentId,
    AgentRunId,
    ApprovalDecision,
    ApprovalKind,
    RunFailed,
    RunId,
    StepCompleted,
    StepId,
    StepSpec,
    WorkspaceId,
)
from engine.ports import ApprovalRequest, GitResult
from engine.runtime.terminal_mcp import (
    DEFAULT_BASE_REF,
    OpenedPullRequest,
    PostedComment,
    TerminalMcpBroker,
    TerminalResultRegistry,
    _mcp_response,
)


STEP = StepSpec(StepId("implementation"), AgentId("coder"), ("revision",))


def _request(
    broker: TerminalMcpBroker,
    request_id: str | int,
    name: str,
    arguments: object,
) -> dict[str, object]:
    # This helper stands in for the stdio bridge. The model never receives the
    # opaque token or any workflow identifier.
    config = broker.config
    token = config.args[config.args.index("--token") + 1]
    return {
        "token": token,
        "request_id": request_id,
        "name": name,
        "arguments": arguments,
    }


def _direct_request(
    broker: TerminalMcpBroker,
    request_id: str | int,
    name: str,
    arguments: object,
) -> dict[str, object]:
    """Call the in-process broker without starting its loopback bridge."""

    return {
        "token": broker._token,
        "request_id": request_id,
        "name": name,
        "arguments": arguments,
    }


def test_completion_uses_bound_ids_and_records_the_mcp_request() -> None:
    async def scenario() -> None:
        delivered: list[StepCompleted | RunFailed] = []

        async def deliver(event: StepCompleted | RunFailed) -> None:
            delivered.append(event)

        broker = TerminalMcpBroker(
            run_id=RunId("bound-run"),
            agent_run_id=AgentRunId("bound-agent-run"),
            step=STEP,
            registry=TerminalResultRegistry(),
            deliver=deliver,
        )
        async with broker:
            response = await broker._submit(
                _request(
                    broker,
                    "mcp-42",
                    "complete_step",
                    {
                        "outcome": "success",
                        "summary": "Done.",
                        "outputs": {"revision": "abc123"},
                    },
                )
            )
            event = await broker.result()

        assert response == {"ok": True, "acknowledgement": "accepted"}
        assert delivered == [event], "delivery must finish before acknowledgement"
        assert isinstance(event, StepCompleted)
        assert event.run_id == "bound-run"
        assert event.agent_run_id == "bound-agent-run"
        assert event.step_id == STEP.step_id
        assert event.mcp_request_id == "mcp-42"

    asyncio.run(scenario())


def test_model_supplied_identifiers_are_rejected_not_trusted() -> None:
    async def scenario() -> None:
        broker = TerminalMcpBroker(
            run_id=RunId("bound-run"),
            agent_run_id=AgentRunId("bound-agent-run"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        async with broker:
            response = await broker._submit(
                _request(
                    broker,
                    1,
                    "complete_step",
                    {
                        "run_id": "other-run",
                        "step_id": "other-step",
                        "outcome": "success",
                        "summary": "Done.",
                    },
                )
            )
        assert response["ok"] is False
        assert "exactly outcome" in str(response["error"])

    asyncio.run(scenario())


def test_duplicate_and_conflicting_terminal_calls_are_rejected() -> None:
    async def scenario() -> None:
        registry = TerminalResultRegistry()
        first = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=registry,
        )
        async with first:
            accepted = await first._submit(
                _request(
                    first,
                    1,
                    "complete_step",
                    {
                        "outcome": "success",
                        "summary": "Done.",
                        "outputs": {"revision": "abc123"},
                    },
                )
            )
            duplicate = await first._submit(
                _request(first, 1, "fail_step", {"summary": "Changed my mind."})
            )

        other_run = TerminalMcpBroker(
            run_id=RunId("run-2"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=registry,
        )
        async with other_run:
            cross_run = await other_run._submit(
                _request(other_run, 2, "fail_step", {"summary": "No."})
            )

        assert accepted["ok"] is True
        assert duplicate["ok"] is False
        assert cross_run["ok"] is False
        assert "already accepted" in str(cross_run["error"])

    asyncio.run(scenario())


def test_fail_step_is_bound_and_auditable() -> None:
    async def scenario() -> None:
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        async with broker:
            response = await broker._submit(
                _request(broker, 99, "fail_step", {"summary": "Tests cannot pass."})
            )
            event = await broker.result()

        assert response["ok"] is True
        assert event == RunFailed(
            run_id=RunId("run-1"),
            reason="Tests cannot pass.",
            agent_run_id=AgentRunId("agent-run-1"),
            mcp_request_id=99,
        )

    asyncio.run(scenario())


def test_stdio_mcp_surface_includes_non_terminal_clarify_tool() -> None:
    response = asyncio.run(
        _mcp_response(
            "127.0.0.1",
            1,
            "unused",
            {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"},
        )
    )

    assert response is not None
    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "complete_step",
        "fail_step",
        "clarify",
    ]
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)


def test_clarify_acknowledges_without_submitting_a_terminal_result() -> None:
    async def scenario() -> None:
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        async with broker:
            clarified = await broker._submit(
                _request(broker, "clarify-1", "clarify", {})
            )
            await broker.clarification()
            assert broker._result is not None
            assert not broker._result.done()
            completed = await broker._submit(
                _request(
                    broker,
                    "complete-1",
                    "complete_step",
                    {
                        "outcome": "success",
                        "summary": "Done after clarifying.",
                        "outputs": {"revision": "abc123"},
                    },
                )
            )

        assert clarified == {"ok": True, "acknowledgement": "clarified"}
        assert completed == {"ok": True, "acknowledgement": "accepted"}

    asyncio.run(scenario())


def test_reviewer_mcp_surface_includes_repo_comment_tool() -> None:
    response = asyncio.run(
        _mcp_response(
            "127.0.0.1",
            1,
            "unused",
            {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"},
            repository_tools=("add_comment",),
        )
    )

    assert response is not None
    tools = response["result"]["tools"]
    add_comment = next(tool for tool in tools if tool["name"] == "add_comment")
    assert add_comment["inputSchema"]["required"] == ["pr_url", "comment"]
    assert add_comment["inputSchema"]["dependentRequired"] == {
        "file": ["line"],
        "line": ["file"],
    }


def test_a_session_with_no_step_lists_only_the_repository_tools() -> None:
    """The listing a naming session shows, read from the real subprocess.

    Asserted through `config` and the server's own argument parser rather than
    against `_tools` directly, because what decides whether the naming agent is
    shown a tool it cannot use is the whole path: the flag `config` appends,
    the name `main` parses it under, and the sense it is passed on in. Each of
    those could break on its own and leave a listing assertion green.
    """

    async def scenario() -> list[str]:
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=None,
            registry=TerminalResultRegistry(),
        )
        async with broker:
            broker.enable_repository_tools(
                object(),  # type: ignore[arg-type]
                ("view_work_item",),
            )
            config = broker.config
            assert "--repository-tools-only" in config.args
            server = await asyncio.create_subprocess_exec(
                config.command,
                *config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert server.stdin is not None
            assert server.stdout is not None
            assert server.stderr is not None
            try:
                request = {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"}
                server.stdin.write(json.dumps(request).encode() + b"\n")
                await server.stdin.drain()
                # Bounded, so a server that never answers fails this test
                # rather than hanging the suite; the `finally` then reports
                # what it said on stderr, so one that died on its arguments
                # says so instead of surfacing as unparseable emptiness.
                line = await asyncio.wait_for(server.stdout.readline(), timeout=5)
                assert line, "the stdio MCP server exited without a response"
                answer = json.loads(line)
            finally:
                server.stdin.close()
                await server.stdin.wait_closed()
                return_code = await asyncio.wait_for(server.wait(), timeout=5)
                assert return_code == 0, (await server.stderr.read()).decode()
        return [tool["name"] for tool in answer["result"]["tools"]]

    assert asyncio.run(scenario()) == ["view_work_item"]


@pytest.mark.parametrize("reply_id", [None, 99])
def test_repo_comment_is_forwarded(reply_id: int | None) -> None:
    class RecordingSourceControl:
        def __init__(self) -> None:
            self.comments: list[tuple[object, ...]] = []

        async def add_comment(self, *arguments: object, **options: object) -> CommentResult:
            self.comments.append(arguments)
            return CommentResult(123, "https://example.com/comment/123")

    async def scenario() -> None:
        source_control = RecordingSourceControl()
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        broker.enable_repository_tools(source_control, ("add_comment",), git_approval=AsyncMock(return_value=ApprovalDecision.ACCEPT))  # type: ignore[arg-type]
        broker._result = asyncio.get_running_loop().create_future()
        request = {
            "token": broker._token,
            "request_id": "comment-1",
            "name": "add_comment",
            "arguments": {
                "pr_url": "https://github.com/acme/api/pull/42",
                "comment": "This can race.",
                "file": "src/worker.py",
                "line": 17,
            },
        }
        if reply_id is not None:
            request["arguments"].pop("file")
            request["arguments"].pop("line")
            request["arguments"].update(in_reply_to_id=reply_id, thread_id="PRRT_1", resolve=False)
        accepted = await broker._submit(request)

        assert accepted["ok"] is True
        assert accepted["acknowledgement"] == "comment added"
        assert json.loads(accepted["output"]) == {"id": 123, "url": "https://example.com/comment/123"}
        assert source_control.comments == [
            (
                "https://github.com/acme/api/pull/42",
                "This can race.",
                "src/worker.py" if reply_id is None else None,
                17 if reply_id is None else None,
                reply_id,
            )
        ]

    asyncio.run(scenario())


def test_step_granted_add_comment_can_complete_without_commenting() -> None:
    class SourceControl:
        async def add_comment(self, *_arguments: object) -> CommentResult:
            raise AssertionError("no comment should be posted")

    async def scenario() -> None:
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        broker.enable_repository_tools(SourceControl(), ("add_comment",))  # type: ignore[arg-type]
        broker._result = asyncio.get_running_loop().create_future()

        completed = await broker._submit({
            "token": broker._token,
            "request_id": "complete-1",
            "name": "complete_step",
            "arguments": {
                "outcome": "success",
                "summary": "Nothing survived review.",
                "outputs": {"revision": "abc123"},
            },
        })

        assert completed["ok"] is True
        assert broker._result.done()

    asyncio.run(scenario())


class RecordingRepository:
    """A source control that answers instead of touching a repository."""

    def __init__(self, exit_code: int = 0, stdout: str = "") -> None:
        self.git_calls: list[tuple[object, tuple[str, ...]]] = []
        self.reviews: list[tuple[object, ...]] = []
        self._exit_code = exit_code
        self._stdout = stdout

    async def run_git(self, workspace_id, arguments) -> GitResult:
        self.git_calls.append((workspace_id, tuple(arguments)))
        return GitResult(
            exit_code=self._exit_code,
            stdout=self._stdout,
            stderr="" if not self._exit_code else "fatal: no",
        )

    async def request_review(self, workspace_id, branch, base_ref, title, body) -> str:
        self.reviews.append((workspace_id, branch, base_ref, title, body))
        return "https://github.com/acme/api/pull/7"


async def _approve_git(_request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision.ACCEPT


def _repository_broker(
    source_control: object,
    *names: str,
    git_approval=_approve_git,
    tool_call_ids=None,
) -> TerminalMcpBroker:
    broker = TerminalMcpBroker(
        run_id=RunId("run-1"),
        agent_run_id=AgentRunId("agent-run-1"),
        step=STEP,
        registry=TerminalResultRegistry(),
    )
    broker.enable_repository_tools(
        source_control,  # type: ignore[arg-type]
        names,
        WorkspaceId("ws-under-test"),
        git_approval,
        tool_call_ids,
    )
    return broker


def test_repository_tools_are_listed_only_when_the_step_holds_them() -> None:
    response = asyncio.run(
        _mcp_response(
            "127.0.0.1",
            1,
            "unused",
            {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"},
            repository_tools=("git_subcommand", "open_pull_request"),
        )
    )

    assert response is not None
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    assert "add_comment" not in tools
    git = tools["git_subcommand"]
    assert git["inputSchema"]["properties"]["arguments"] == {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
    }
    assert tools["open_pull_request"]["inputSchema"]["required"] == [
        "branch",
        "title",
        "body",
    ]


def test_git_runs_against_the_step_workspace_and_returns_what_it_printed() -> None:
    """The model names the command; the broker names the workspace.

    A model that could pass a workspace id could name somebody else's, so the
    one the step is running in is bound when the tool is enabled and never
    appears in the tool's schema.
    """

    async def scenario() -> None:
        source_control = RecordingRepository(stdout="On branch agent/greeting")
        broker = _repository_broker(source_control, "git_subcommand")
        async with broker:
            response = await broker._submit(
                _request(
                    broker,
                    "git-1",
                    "git_subcommand",
                    {"arguments": ["status", "--short", "--branch"]},
                )
            )

        assert response == {
            "ok": True,
            "acknowledgement": "git ran",
            "output": "On branch agent/greeting",
        }
        assert source_control.git_calls == [
            (WorkspaceId("ws-under-test"), ("status", "--short", "--branch"))
        ]

    asyncio.run(scenario())


def test_git_is_approved_by_engine_even_when_the_mcp_tool_was_preapproved() -> None:
    """Provider permission only admits the call to this server.

    Git may launch helpers, hooks and aliases outside the provider sandbox, so
    the server independently submits the exact argument vector to Engine's
    approval broker before invoking source control.
    """

    async def scenario() -> None:
        requests: list[ApprovalRequest] = []

        async def approve(request: ApprovalRequest) -> ApprovalDecision:
            requests.append(request)
            return ApprovalDecision.ACCEPT

        source_control = RecordingRepository(stdout="clean")
        broker = _repository_broker(
            source_control, "git_subcommand", git_approval=approve
        )
        response = await broker._submit(
            _direct_request(
                broker,
                "git-sensitive",
                "git_subcommand",
                {"arguments": ["-c", "alias.x=!sh", "x"]},
            )
        )

        assert response["ok"] is True
        assert len(requests) == 1
        request = requests[0]
        assert request.kind is ApprovalKind.TOOL_USE
        assert request.tool_name == "mcp__workflow__git_subcommand"
        # Nothing told this broker what the provider called the call, and the
        # request id is this server's own numbering -- so the request names no
        # call rather than one nothing in the transcript answers to.
        assert request.tool_call_id is None
        assert request.command == "git -c 'alias.x=!sh' x"
        assert request.arguments == '{"arguments": ["-c", "alias.x=!sh", "x"]}'
        assert request.allowed_decisions == (
            ApprovalDecision.ACCEPT,
            ApprovalDecision.CANCEL,
        )
        assert source_control.git_calls == [
            (WorkspaceId("ws-under-test"), ("-c", "alias.x=!sh", "x"))
        ]

    asyncio.run(scenario())


def test_git_approval_names_the_call_the_provider_reported() -> None:
    """The pause has to be readable beside the call it interrupted.

    A client pairs the two by the provider's id for the call, and this server
    is reached over a transport of its own -- so the id is looked up from what
    the provider streamed rather than taken from the MCP request carrying it.
    """

    async def scenario() -> None:
        requests: list[ApprovalRequest] = []
        asked: list[tuple[str, str]] = []

        async def approve(request: ApprovalRequest) -> ApprovalDecision:
            requests.append(request)
            return ApprovalDecision.ACCEPT

        def tool_call_ids(name: str, arguments: str) -> str | None:
            asked.append((name, arguments))
            return "thread-1:item-7"

        source_control = RecordingRepository(stdout="clean")
        broker = _repository_broker(
            source_control,
            "git_subcommand",
            git_approval=approve,
            tool_call_ids=tool_call_ids,
        )
        response = await broker._submit(
            _direct_request(
                broker,
                "git-9",
                "git_subcommand",
                {"arguments": ["status", "--short"]},
            )
        )

        assert response["ok"] is True
        assert requests[0].tool_call_id == "thread-1:item-7"
        # Asked by what the call is, so the answer can be found among the calls
        # the provider has reported rather than among this server's requests.
        assert asked == [
            (
                "mcp__workflow__git_subcommand",
                '{"arguments": ["status", "--short"]}',
            )
        ]

    asyncio.run(scenario())


def test_git_fails_closed_when_the_step_has_no_approval_handler() -> None:
    async def scenario() -> None:
        source_control = RecordingRepository()
        broker = _repository_broker(
            source_control, "git_subcommand", git_approval=None
        )
        response = await broker._submit(
            _direct_request(
                broker,
                "git-1",
                "git_subcommand",
                {"arguments": ["status"]},
            )
        )

        assert response["ok"] is False
        assert "requires approval handling" in str(response["error"])
        assert source_control.git_calls == []

    asyncio.run(scenario())


def test_git_does_not_run_when_approval_is_cancelled() -> None:
    async def cancel(_request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.CANCEL

    async def scenario() -> None:
        source_control = RecordingRepository()
        broker = _repository_broker(
            source_control, "git_subcommand", git_approval=cancel
        )
        response = await broker._submit(
            _direct_request(
                broker,
                "git-1",
                "git_subcommand",
                {"arguments": ["push", "origin", "agent/topic"]},
            )
        )

        assert response == {"ok": False, "error": "git_subcommand was not approved"}
        assert source_control.git_calls == []

    asyncio.run(scenario())


def test_a_failing_git_command_is_reported_with_what_git_said() -> None:
    async def scenario() -> None:
        broker = _repository_broker(
            RecordingRepository(exit_code=128), "git_subcommand"
        )
        async with broker:
            response = await broker._submit(
                _request(broker, "git-1", "git_subcommand", {"arguments": ["push", "origin", "agent/topic"]})
            )

        assert response["ok"] is False
        assert "git exited 128" in str(response["error"])
        assert "fatal: no" in str(response["error"])

    asyncio.run(scenario())


def test_a_repository_tool_the_step_was_not_granted_is_refused() -> None:
    """Serving is per step, so holding one tool is not holding the rest."""

    async def scenario() -> None:
        source_control = RecordingRepository()
        broker = _repository_broker(source_control, "git_subcommand")
        async with broker:
            response = await broker._submit(
                _request(
                    broker,
                    "pr-1",
                    "open_pull_request",
                    {
                        "branch": "agent/greeting",
                        "title": "feat: greet",
                        "body": "Body.",
                    },
                )
            )

        assert response["ok"] is False
        assert "not enabled" in str(response["error"])
        assert source_control.reviews == []

    asyncio.run(scenario())


def test_a_pull_request_url_reaches_the_model_through_the_bridge() -> None:
    """A bare "accepted" would leave the step guessing at its own output."""

    async def scenario() -> None:
        source_control = RecordingRepository()
        broker = _repository_broker(source_control, "open_pull_request")
        async with broker:
            config = broker.config
            host = config.args[config.args.index("--host") + 1]
            port = int(config.args[config.args.index("--port") + 1])
            token = config.args[config.args.index("--token") + 1]
            response = await _mcp_response(
                host,
                port,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": "call-1",
                    "method": "tools/call",
                    "params": {
                        "name": "open_pull_request",
                        "arguments": {
                            "branch": "agent/greeting",
                            "title": "feat: greet",
                            "body": "Body.",
                        },
                    },
                },
                repository_tools=("open_pull_request",),
            )

        assert response is not None
        url = "https://github.com/acme/api/pull/7"
        assert response["result"]["content"] == [{"type": "text", "text": url}]
        assert response["result"]["structuredContent"] == {
            "accepted": True,
            "output": url,
        }
        # The base nobody named, rather than nothing at all.
        assert source_control.reviews == [
            (
                WorkspaceId("ws-under-test"),
                "agent/greeting",
                DEFAULT_BASE_REF,
                "feat: greet",
                "Body.",
            )
        ]
        assert config.args.count("--repository-tool") == 1

    asyncio.run(scenario())


def test_the_bridge_credential_can_never_read_as_a_command_line_flag() -> None:
    """The token is an argv element, so its alphabet is a correctness property.

    `token_urlsafe` draws from an alphabet that includes `-`, and a token that
    began with one was read by the server's own parser as an option: it exited
    on `--token: expected one argument` before answering `initialize`. About
    one agent run in sixty-four, and nothing in the aftermath named the cause
    -- the step's agent simply had no tools, and the run failed two
    corrections later.

    Sampled rather than asserted once, because a one-in-sixty-four fault is
    not something a single draw catches.
    """

    async def scenario() -> list[str]:
        tokens: list[str] = []
        for index in range(200):
            broker = TerminalMcpBroker(
                run_id=RunId(f"run-{index}"),
                agent_run_id=AgentRunId(f"agent-run-{index}"),
                step=STEP,
                registry=TerminalResultRegistry(),
            )
            async with broker:
                config = broker.config
                tokens.append(config.args[config.args.index("--token") + 1])
        return tokens

    tokens = asyncio.run(scenario())

    assert all(token.isalnum() for token in tokens)
    # And it is a credential, so no two sessions share one.
    assert len(set(tokens)) == len(tokens)


def test_stdio_bridge_returns_a_small_acknowledgement() -> None:
    async def scenario() -> None:
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        async with broker:
            config = broker.config
            host = config.args[config.args.index("--host") + 1]
            port = int(config.args[config.args.index("--port") + 1])
            token = config.args[config.args.index("--token") + 1]
            response = await _mcp_response(
                host,
                port,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": "call-1",
                    "method": "tools/call",
                    "params": {
                        "name": "complete_step",
                        "arguments": {
                            "outcome": "success",
                            "summary": "Done.",
                            "outputs": {"revision": "abc123"},
                        },
                    },
                },
            )

        assert response == {
            "jsonrpc": "2.0",
            "id": "call-1",
            "result": {
                "content": [{"type": "text", "text": "accepted"}],
                "structuredContent": {"accepted": True},
            },
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [
    {"in_reply_to_id": 0}, {"in_reply_to_id": -1}, {"in_reply_to_id": True},
    {"in_reply_to_id": "123"}, {"in_reply_to_id": 1.5},
    {"in_reply_to_id": 123, "file": "src/app.py", "line": 1},
])
def test_comment_arguments_reject_invalid_replies(arguments: dict) -> None:
    from engine.runtime.terminal_mcp import _comment_arguments

    with pytest.raises(ValueError):
        _comment_arguments({"pr_url": "https://github.com/acme/api/pull/42", "comment": "Fixed.", **arguments})


def test_comment_provenance_reaches_mcp_client() -> None:
    from unittest.mock import AsyncMock

    async def scenario() -> None:
        source = AsyncMock()
        source.add_comment.return_value = CommentResult(124, "https://example.com/comment/124")
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"), agent_run_id=AgentRunId("agent-run-1"),
            step=STEP, registry=TerminalResultRegistry(),
        )
        broker.enable_repository_tools(source, ("add_comment",), git_approval=AsyncMock(return_value=ApprovalDecision.ACCEPT))
        async with broker:
            config = broker.config
            response = await _mcp_response(
                config.args[config.args.index("--host") + 1],
                int(config.args[config.args.index("--port") + 1]),
                config.args[config.args.index("--token") + 1],
                {"jsonrpc": "2.0", "id": "reply-1", "method": "tools/call", "params": {
                    "name": "add_comment", "arguments": {
                        "pr_url": "https://github.com/acme/api/pull/42",
                        "comment": "Fixed", "in_reply_to_id": 123, "thread_id": "PRRT_1", "resolve": False,
                    },
                }},
                repository_tools=("add_comment",),
            )
        result = response["result"]
        assert json.loads(result["content"][0]["text"]) == {"id": 124, "url": "https://example.com/comment/124"}
        assert result["structuredContent"]["output"] == result["content"][0]["text"]
        source.add_comment.assert_awaited_once_with("https://github.com/acme/api/pull/42", "Fixed", None, None, 123, thread_id="PRRT_1", resolve=False, commit_sha=None)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "pr_url, expected",
    [
        ("https://github.com/acme/api/pull/42", ("acme/api", 42)),
        ("https://github.com/acme/api/pull/42/files", ("acme/api", 42)),
        ("https://github.com/acme/api/pull/42#issuecomment-9", ("acme/api", 42)),
        ("https://github.example.com/acme/api/pull/42", ("github.example.com/acme/api", 42)),
        ("https://GITHUB.COM/Acme/API/pull/42", ("acme/api", 42)),
        ("https://github.example.com:8443/acme/api/pull/42", ("github.example.com:8443/acme/api", 42)),
        ("/acme/api/pull/42", None),
        # A repository, or an owner, named `pull`. The marker that names the
        # pull request is the one after it, so this is the one it looks like.
        ("https://github.com/wei/pull/pull/123", ("wei/pull", 123)),
        ("https://github.com/pull/repo/pull/7", ("pull/repo", 7)),
        # Merge requests are read too, and namespaced by the host they live
        # on: two forges number from counters of their own. Nesting is to any
        # depth, because a GitLab project is.
        ("https://gitlab.com/acme/api/-/merge_requests/7", ("gitlab.com/acme/api", 7)),
        (
            "https://gitlab.com/group/sub/project/-/merge_requests/5#note_9",
            ("gitlab.com/group/sub/project", 5),
        ),
        ("https://gitlab.com/pull/project/-/merge_requests/3", ("gitlab.com/pull/project", 3)),
        ("https://gitlab.example.com/x/y/pull/7/-/merge_requests/1#note_123", None),
        # GitLab's views of one merge request, read past as `/files` is on a
        # pull request.
        ("https://gitlab.com/acme/api/-/merge_requests/7/diffs", ("gitlab.com/acme/api", 7)),
        ("https://gitlab.com/acme/api/-/merge_requests/7/commits", ("gitlab.com/acme/api", 7)),
        (
            "https://gitlab.com/acme/api/-/merge_requests/7/diffs#note_1",
            ("gitlab.com/acme/api", 7),
        ),
        ("https://gitlab.com/acme/api/-/merge_requests/7/", ("gitlab.com/acme/api", 7)),
        ("https://github.com/acme/api/issues/42", None),
        ("https://github.com/acme/api", None),
        # Two change requests in one path. Read from the front this is
        # acme/app#12 and read from the back it is #99, so it names neither.
        ("https://github.com/acme/app/pull/12/x/victim/repo/pull/99", None),
        ("https://github.com/acme/app/pull/12/pull/99", None),
        ("https://gitlab.com/a/b/-/merge_requests/1/-/merge_requests/2", None),
        ("https://gitlab.com/a/b/-/merge_requests/1/x/-/merge_requests/2", None),
        ("https://gitlab.com/a/b/-/merge_requests/1/merge_requests/2", None),
        ("https://gitlab.com/a/b/-/merge_requests/1/x/y/pull/9", None),
        # A number is spelled one way: `str.isdigit` is true of arabic-indic
        # digits, `int` raises on `²`, and a leading zero is a number to
        # a reader matching digits and not to one matching `[1-9][0-9]*`.
        ("https://github.com/acme/api/pull/١٢", None),
        ("https://github.com/acme/api/pull/²", None),
        ("https://github.com/acme/api/pull/042", None),
        ("https://github.com/acme/api/pull/0", None),
        # A run of digits longer than `int` converts is refused like any other
        # spelling that is not a number, rather than raised out of the reader.
        ("https://github.com/acme/api/pull/" + "1" * 5000, None),
        ("https://github.com/acme/api/pull/" + "9" * 20, None),
        ("https://github.com/acme/api/pull/" + "9" * 19, ("acme/api", int("9" * 19))),
        # A dot segment is a move, not a name.
        ("https://github.com/../x/pull/1", None),
        ("https://github.com/a/../pull/1", None),
        ("https://gitlab.com/../-/merge_requests/1", None),
        ("https://gitlab.com/a/../../x/-/merge_requests/1", None),
        # A dot inside a step is ordinary; only a whole segment moves.
        ("https://github.com/a.b/c.d/pull/7", ("a.b/c.d", 7)),
        # An authority the standard reader refuses is refused here too, rather
        # than raised out into CICheck and the adapters.
        ("https://[invalid/acme/api/pull/1", None),
        ("https://[::1/acme/api/pull/1", None),
        ("https://ghe.acme.com:notaport/acme/api/pull/1", None),
        ("https://ghe.acme.com:99999/acme/api/pull/1", None),
        # A bracketed host that is well spelled still reads, keyed by the
        # address the reader read out of the brackets.
        ("https://[::1]:8443/acme/api/pull/7", ("::1:8443/acme/api", 7)),
    ],
)
def test_a_change_request_is_read_off_its_url_one_way(
    pr_url: str, expected: tuple[str, int] | None
) -> None:
    from engine.runtime.change_requests import change_request

    found = change_request(pr_url)
    assert (None if found is None else (found.project, found.number)) == expected


@pytest.mark.parametrize(
    "project",
    ["acme/api", "ghe.acme.com/acme/api", "ghe.acme.com:8443/acme/api"],
)
def test_a_pull_request_key_spelled_back_reads_as_that_key(project: str) -> None:
    from engine.runtime.change_requests import ChangeRequest, change_request, pull_request_url

    assert change_request(pull_request_url(project, 7)) == ChangeRequest(project, 7)


def test_an_opened_merge_request_is_written_down_like_a_pull_request() -> None:
    from unittest.mock import AsyncMock

    recorded: list[OpenedPullRequest] = []

    async def record(opened: OpenedPullRequest) -> None:
        recorded.append(opened)

    url = "https://gitlab.com/group/sub/project/-/merge_requests/7"
    source = AsyncMock()
    source.request_review.return_value = url

    async def scenario() -> dict[str, object]:
        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=StepSpec(StepId("implementation"), AgentId("coder"), ("pr_url",)),
            registry=TerminalResultRegistry(),
        )
        broker.enable_pull_request_records(record)
        broker.enable_repository_tools(
            source, ("open_pull_request",), WorkspaceId("ws")
        )
        broker._result = asyncio.get_running_loop().create_future()
        return await broker._submit(
            _direct_request(
                broker,
                "open-1",
                "open_pull_request",
                {"branch": "feature", "title": "Add a thing"},
            )
        )

    assert asyncio.run(scenario())["ok"] is True
    assert recorded == [OpenedPullRequest("gitlab.com/group/sub/project", 7, url)]


@pytest.mark.parametrize(
    "host, repository",
    [
        ("github.com", "acme/renamed"),
        ("github.example.com", "github.example.com/acme/renamed"),
    ],
)
def test_posted_comments_are_recorded_against_the_change_request(
    host: str, repository: str,
) -> None:
    comment_url = f"https://{host}/Acme/Renamed/pull/42#issuecomment-123"

    class RecordingSourceControl:
        async def add_comment(self, *_arguments: object) -> CommentResult:
            return CommentResult(123, comment_url)

    async def scenario() -> list[PostedComment]:
        recorded: list[PostedComment] = []

        async def record(posted: PostedComment) -> None:
            recorded.append(posted)

        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        broker.enable_repository_tools(RecordingSourceControl(), ("add_comment",))  # type: ignore[arg-type]
        broker.enable_comment_records(record)
        answer = await broker._submit(
            {
                "token": broker._token,
                "request_id": "comment-1",
                "name": "add_comment",
                "arguments": {
                    "pr_url": "https://github.com/acme/api/pull/42",
                    "comment": "Looks good.",
                },
            }
        )
        assert answer["ok"] is True
        return recorded

    assert asyncio.run(scenario()) == [
        PostedComment(
            repository, 42, "issue", CommentResult(123, comment_url)
        )
    ]


@pytest.mark.parametrize(
    "url, repository, number",
    [
        ("https://github.com/Acme/Renamed/pull/42", "acme/renamed", 42),
        ("https://github.com/acme/api/pull/7", "acme/api", 7),
    ],
)
def test_an_opened_pull_request_is_claimed_by_the_run_that_opened_it(
    url: str, repository: str, number: int,
) -> None:
    """Opening is the act that makes a run the pull request's owner.

    Recorded here rather than read back off the comments on the pull request,
    because commenting is something any run may do to one it does not own: a
    review run's note would otherwise make it the owner of somebody else's work.
    """

    class OpeningSourceControl:
        async def request_review(self, *_arguments: object) -> str:
            return url

    async def scenario() -> list[OpenedPullRequest]:
        claimed: list[OpenedPullRequest] = []

        async def claim(opened: OpenedPullRequest) -> None:
            claimed.append(opened)

        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        broker.enable_repository_tools(
            OpeningSourceControl(),  # type: ignore[arg-type]
            ("open_pull_request",),
            WorkspaceId("workspace"),
        )
        broker.enable_pull_request_records(claim)
        answer = await broker._submit(
            {
                "token": broker._token,
                "request_id": "open-1",
                "name": "open_pull_request",
                "arguments": {"branch": "feature", "title": "Add a thing"},
            }
        )
        assert answer["ok"] is True
        assert answer["output"] == url
        return claimed

    assert asyncio.run(scenario()) == [OpenedPullRequest(repository, number, url)]


@pytest.mark.parametrize("failure", ["unrecordable", "unrecognisable"])
def test_a_pull_request_that_cannot_be_claimed_is_still_reported_as_opened(
    caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    """It is open on the forge by now; retrying would open a second one."""
    url = (
        "https://gitlab.example/acme/api/-/merge_requests/42"
        if failure == "unrecognisable"
        else "https://github.com/acme/api/pull/42"
    )

    class OpeningSourceControl:
        async def request_review(self, *_arguments: object) -> str:
            return url

    async def scenario() -> dict[str, object]:
        async def claim(_opened: OpenedPullRequest) -> None:
            raise RuntimeError("the store is gone")

        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        broker.enable_repository_tools(
            OpeningSourceControl(),  # type: ignore[arg-type]
            ("open_pull_request",),
            WorkspaceId("workspace"),
        )
        broker.enable_pull_request_records(claim)
        return await broker._submit(
            {
                "token": broker._token,
                "request_id": "open-1",
                "name": "open_pull_request",
                "arguments": {"branch": "feature", "title": "Add a thing"},
            }
        )

    with caplog.at_level(logging.WARNING):
        answer = asyncio.run(scenario())

    assert answer["ok"] is True
    assert answer["output"] == url
    assert caplog.text


def test_a_comment_that_cannot_be_recorded_is_still_reported_as_posted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The comment is on the forge by now; retrying would post it twice."""

    class RecordingSourceControl:
        async def add_comment(self, *_arguments: object) -> CommentResult:
            return CommentResult(123, "https://github.com/Acme/Renamed/pull/42#issuecomment-123")

    async def scenario() -> dict[str, object]:
        async def record(_posted: PostedComment) -> None:
            raise RuntimeError("the store is gone")

        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=STEP,
            registry=TerminalResultRegistry(),
        )
        broker.enable_repository_tools(RecordingSourceControl(), ("add_comment",))  # type: ignore[arg-type]
        broker.enable_comment_records(record)
        broker._result = asyncio.get_running_loop().create_future()
        answer = await broker._submit(
            {
                "token": broker._token,
                "request_id": "comment-1",
                "name": "add_comment",
                "arguments": {
                    "pr_url": "https://github.com/acme/api/pull/42",
                    "comment": "Looks good.",
                },
            }
        )
        # And the comment still counts towards finishing the review.
        completed = await broker._submit(
            {
                "token": broker._token,
                "request_id": "complete-1",
                "name": "complete_step",
                "arguments": {
                    "outcome": "success",
                    "summary": "Done.",
                    "outputs": {"revision": "abc123"},
                },
            }
        )
        assert completed["ok"] is True
        return answer

    answer = asyncio.run(scenario())
    assert answer["ok"] is True
    assert answer["acknowledgement"] == "comment added"

    assert "Could not record posted comment 123" in caplog.text
    assert "the store is gone" in caplog.text
    assert "https://github.com/Acme/Renamed/pull/42#issuecomment-123" in caplog.text


@pytest.mark.parametrize("dependency", [None, "prerequisite"])
def test_create_workorder_is_opt_in_and_returns_created_run(dependency) -> None:
    calls = []

    async def create(parent, prompt, dependency):
        calls.append((parent, prompt, dependency))
        return "/runs/child", "child"

    async def scenario():
        broker = TerminalMcpBroker(
            run_id=RunId("parent"), agent_run_id=AgentRunId("agent"),
            step=STEP, registry=TerminalResultRegistry(),
        )
        async with broker:
            arguments = {"prompt": " Next task "}
            if dependency:
                arguments["depends_on_run_id"] = dependency
            request = _request(broker, 1, "create_workorder", arguments)
            assert (await broker._submit(request))["ok"] is False
            broker.enable_workorder_creation(create)
            assert "--create-workorder" in broker.config.args
            for invalid in ({}, {"prompt": ""}, {"prompt": 1},
                              {"prompt": "task", "depends_on_run_id": ""},
                              {"prompt": "task", "depends_on_run_id": 1},
                              {"prompt": "task", "parent_run_id": "spoofed"}):
                assert (await broker._submit({**request, "arguments": invalid}))["ok"] is False
            args = broker.config.args
            response = await _mcp_response(
                args[args.index("--host") + 1], int(args[args.index("--port") + 1]),
                args[args.index("--token") + 1],
                {"id": 2, "method": "tools/call", "params": {
                    "name": "create_workorder", "arguments": arguments,
                }}, create_workorder=True,
            )
            assert json.loads(response["result"]["content"][0]["text"]) == {
                "url": "/runs/child", "run_id": "child",
            }
            listing = await _mcp_response("", 0, "", {
                "id": 3, "method": "tools/list",
            }, create_workorder=True)
            assert "create_workorder" in [t["name"] for t in listing["result"]["tools"]]
        assert calls == [(RunId("parent"), "Next task", dependency)]

    asyncio.run(scenario())


class OwningSourceControl:
    """Opens `acme/api#7` and posts wherever it is told to."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    async def request_review(self, *_arguments: object) -> str:
        return "https://github.com/acme/api/pull/7"

    async def add_comment(self, pr_url: str, *_arguments: object) -> CommentResult:
        self.posted.append(pr_url)
        return CommentResult(1, f"{pr_url}#issuecomment-1")


async def _owning_broker(
    source_control: OwningSourceControl, *, opened: bool, recorded: object,
) -> TerminalMcpBroker:
    """A step that may have opened `acme/api#7`, over a store answering `recorded`.

    `recorded` is the store's answer, an exception it raises instead, or
    `None` for a broker with no store at all.
    """
    broker = TerminalMcpBroker(
        run_id=RunId("run-1"),
        agent_run_id=AgentRunId("agent-run-1"),
        step=StepSpec(StepId("implementation"), AgentId("coder"), ("pr_url",)),
        registry=TerminalResultRegistry(),
    )
    broker.enable_repository_tools(
        source_control,  # type: ignore[arg-type]
        ("open_pull_request", "add_comment"),
        WorkspaceId("workspace"),
    )
    if recorded is not None:
        async def lookup() -> object:
            if isinstance(recorded, Exception):
                raise recorded
            return recorded

        broker.enable_pull_request_ownership(lookup)  # type: ignore[arg-type]
    broker._result = asyncio.get_running_loop().create_future()
    if opened:
        answer = await broker._submit(_direct_request(
            broker, "open-1", "open_pull_request", {"branch": "feature", "title": "A thing"},
        ))
        assert answer["ok"] is True
    return broker


@pytest.mark.parametrize(
    ("opened", "recorded", "accepted", "refused"),
    [
        # What the step opened, before anything is recorded.
        (True, None, [7], [406]),
        # What the store recorded, for a step that opened nothing -- CI fixes
        # and review rounds on a pull request an earlier step opened.
        (False, [("acme/api", 406)], [406], [407]),
        # Both at once.
        (True, [("acme/api", 406)], [7, 406], [407]),
        # A store that has not caught up with the open still counts it.
        (True, [], [7], [406]),
        # Unknown ownership fails closed: nothing recorded, or a store that
        # cannot be read, leaves only what the step itself opened.
        (False, [], [], [406, 407]),
        (True, RuntimeError("the store is gone"), [7], [406]),
        (False, RuntimeError("the store is gone"), [], [406, 407]),
        # Ownership not enabled and nothing opened: nothing to hold it to.
        (False, None, [406, 407], []),
    ],
    ids=["opened", "recorded", "union", "store-lagging", "nothing-recorded",
         "store-unreachable", "store-unreachable-nothing-opened", "no-store"],
)
def test_a_step_reports_and_comments_only_on_its_runs_pull_requests(
    opened: bool, recorded: object, accepted: list[int], refused: list[int],
) -> None:
    """A number read off an issue, a diff or CI output is not this run's PR.

    The review step bound to the merged #406 while its worktree was #407 took
    exactly that path: an unchecked `pr_url` travelled downstream and every
    comment went to the wrong pull request.
    """

    async def outcome(number: int) -> tuple[bool, bool, list[str]]:
        url = f"https://github.com/acme/api/pull/{number}"
        source_control = OwningSourceControl()
        broker = await _owning_broker(source_control, opened=opened, recorded=recorded)
        commented = await broker._submit(_direct_request(
            broker, "comment-1", "add_comment", {"pr_url": url, "comment": "Note."},
        ))
        completed = await broker._submit(_direct_request(
            broker, "complete-1", "complete_step",
            {"outcome": "success", "summary": "Done.", "outputs": {"pr_url": url}},
        ))
        return bool(commented["ok"]), bool(completed["ok"]), source_control.posted

    for number in accepted:
        url = f"https://github.com/acme/api/pull/{number}"
        assert asyncio.run(outcome(number)) == (True, True, [url])
    for number in refused:
        commented, completed, posted = asyncio.run(outcome(number))
        assert (commented, completed, posted) == (False, False, [])


def test_a_misdirected_comment_is_refused_and_says_where_to_post() -> None:
    async def scenario() -> tuple[dict[str, object], dict[str, object], list[str]]:
        source_control = OwningSourceControl()
        broker = await _owning_broker(
            source_control, opened=False, recorded=[("acme/api", 407)]
        )
        misdirected = await broker._submit(_direct_request(
            broker, "comment-1", "add_comment",
            {"pr_url": "https://github.com/acme/api/pull/406", "comment": "Note."},
        ))
        correct = await broker._submit(_direct_request(
            broker, "comment-2", "add_comment",
            # A view of the same pull request is the same pull request.
            {"pr_url": "https://github.com/ACME/api/pull/407/files", "comment": "Note."},
        ))
        return misdirected, correct, source_control.posted

    misdirected, correct, posted = asyncio.run(scenario())

    assert misdirected["ok"] is False
    assert "acme/api#407" in str(misdirected["error"])
    assert correct["ok"] is True
    assert posted == ["https://github.com/ACME/api/pull/407/files"]


class ReportingSourceControl:
    """Independent remote tips and forge metadata for a reported PR."""

    def __init__(
        self, *, shown_url="https://github.com/acme/api/pull/7",
        commit=GitResult(0, "abc123\n", ""), author="engine-bot",
        login="engine-bot", pushed=GitResult(0, "", ""),
        remote="https://github.com/acme/api.git", before="", same_repository=True,
        moves=True,
    ) -> None:
        self.pushed, self.shown_url, self.commit = pushed, shown_url, commit
        self.author, self.login, self.remote = author, login, remote
        self.before, self.same_repository, self.moves = before, same_repository, moves
        self.did_push = False

    async def view_change_request(self, _workspace_id, number):
        return ChangeRequest(
            number=number, title="A thing", state="open", body="", author=self.author,
            url=self.shown_url, head_ref="feature", head_sha="abc123", base_ref="main",
            head_is_same_repository=self.same_repository,
        )

    async def run_git(self, _workspace_id, arguments):
        if "push" in arguments:
            self.did_push = self.moves
            return self.pushed
        if arguments[0] == "rev-parse":
            return self.commit
        if arguments[0] == "remote":
            return GitResult(0, self.remote + "\n", "")
        raise AssertionError(f"unexpected Git transport read: {arguments}")

    async def branch_tips(self, project, destinations):
        if not self.commit.ok:
            raise RuntimeError("forge unavailable")
        tip = self.commit.stdout.strip() if self.did_push else self.before
        return {"feature": tip} if tip else {}

    async def authenticated_login(self, _repository_url):
        return self.login


_REPORT = {
    "outcome": "success",
    "summary": "Done.",
    "outputs": {"pr_url": "https://github.com/acme/api/pull/7"},
}

_PUSH = ("push", "-u", "origin", "feature")


async def _reporting_broker(
    source_control: ReportingSourceControl,
    claim: object,
    *,
    workspace: str | None = "workspace",
    push: Sequence[str] | None = _PUSH,
) -> TerminalMcpBroker:
    """A step with nothing recorded, that ran `push` unless it is nothing."""

    async def lookup() -> list[tuple[str, int]]:
        return []

    broker = TerminalMcpBroker(
        run_id=RunId("run-1"),
        agent_run_id=AgentRunId("agent-run-1"),
        step=StepSpec(StepId("implementation"), AgentId("coder"), ("pr_url",)),
        registry=TerminalResultRegistry(),
    )
    broker.enable_repository_tools(
        source_control,  # type: ignore[arg-type]
        ("git_subcommand",),
        None if workspace is None else WorkspaceId(workspace),
        git_approval=_approve_git,
    )
    broker.enable_pull_request_ownership(lookup)
    broker.enable_pull_request_claims(claim)  # type: ignore[arg-type]
    broker._result = asyncio.get_running_loop().create_future()
    if push is not None:
        await broker._submit(_direct_request(
            broker, "push-1", "git_subcommand", {"arguments": list(push)},
        ))
    return broker


def _push_output(text: str) -> GitResult:
    return GitResult(0, "", text)


@pytest.mark.parametrize(
    ("source_control", "workspace", "push", "recorded"),
    [
        (ReportingSourceControl(), "workspace", _PUSH, True),
        (ReportingSourceControl(), "workspace", ("--no-pager", *_PUSH), True),
        (ReportingSourceControl(before="old123"), "workspace", _PUSH, True),
        (ReportingSourceControl(before="abc123"), "workspace", _PUSH, False),
        (ReportingSourceControl(remote="https://github.com/acme/other.git"), "workspace", _PUSH, False),
        (ReportingSourceControl(remote="/tmp/mirror.git"), "workspace", _PUSH, False),
        (ReportingSourceControl(remote="https://github.com:2222/acme/api.git"), "workspace", _PUSH, False),
        (ReportingSourceControl(commit=GitResult(0, "def456\n", "")), "workspace", _PUSH, False),
        (ReportingSourceControl(commit=GitResult(1, "", "unavailable")), "workspace", _PUSH, False),
        (ReportingSourceControl(shown_url="https://github.com/acme/other/pull/7"), "workspace", _PUSH, False),
        (ReportingSourceControl(author="somebody-else"), "workspace", _PUSH, False),
        (ReportingSourceControl(author="", login=""), "workspace", _PUSH, False),
        (ReportingSourceControl(), None, _PUSH, False),
        (ReportingSourceControl(), "workspace", None, False),
        (ReportingSourceControl(same_repository=False), "workspace", _PUSH, False),
        (ReportingSourceControl(moves=False, before="abc123", pushed=_push_output(
            "To github.com:acme/api.git\n * [new branch] feature -> feature\n"
        )), "workspace", _PUSH, False),
        *[(ReportingSourceControl(), "workspace", (*_PUSH, flag), False)
          for flag in ("--dry-run", "-n", "-un", "--dry-r")],
    ],
)
def test_a_reported_pull_request_is_recorded_only_when_the_forge_shows_it_is_the_runs(
    source_control: ReportingSourceControl,
    workspace: str | None,
    push: Sequence[str] | None,
    recorded: bool,
) -> None:
    """A step that opened its pull request in the shell has nothing recorded.

    Its report is claimed when the forge agrees it is this step's work: the
    login is shared by every run and the checkout is the agent's to arrange, so
    what has to match is this step's own push -- of that commit, to that
    branch, in the repository the pull request lives in.
    """

    async def scenario() -> tuple[dict[str, object], list[OpenedPullRequest]]:
        claimed: list[OpenedPullRequest] = []

        async def claim(reported: OpenedPullRequest) -> bool:
            claimed.append(reported)
            return True

        broker = await _reporting_broker(
            source_control, claim, workspace=workspace, push=push
        )
        answer = await broker._submit(
            _direct_request(broker, "complete-1", "complete_step", _REPORT)
        )
        return answer, claimed

    answer, claimed = asyncio.run(scenario())

    assert answer["ok"] is recorded
    expected = [OpenedPullRequest("acme/api", 7, "https://github.com/acme/api/pull/7")]
    assert claimed == (expected if recorded else [])


def test_a_reported_pull_request_another_run_holds_is_refused() -> None:
    async def scenario() -> tuple[dict[str, object], bool]:
        async def claim(_reported: OpenedPullRequest) -> bool:
            return False

        broker = await _reporting_broker(ReportingSourceControl(), claim)
        answer = await broker._submit(
            _direct_request(broker, "complete-1", "complete_step", _REPORT)
        )
        return answer, broker._result.done()  # type: ignore[union-attr]

    answer, done = asyncio.run(scenario())

    assert answer["ok"] is False
    assert "not a pull request this run opened" in str(answer["error"])
    assert done is False


def test_a_completion_turned_away_as_a_duplicate_claims_nothing() -> None:
    """The claim is made for the one accepted result, so a second (or
    concurrent) `complete_step` cannot take another pull request with it."""

    async def scenario() -> tuple[list[dict[str, object]], list[OpenedPullRequest]]:
        claimed: list[OpenedPullRequest] = []

        async def claim(reported: OpenedPullRequest) -> bool:
            claimed.append(reported)
            return True

        broker = await _reporting_broker(ReportingSourceControl(), claim)
        broker._registry._accepted[broker._agent_run_id] = object()  # type: ignore[assignment]
        answers = list(await asyncio.gather(
            broker._submit(_direct_request(broker, "complete-1", "complete_step", _REPORT)),
            broker._submit(_direct_request(broker, "complete-2", "complete_step", _REPORT)),
        ))
        return answers, claimed

    answers, claimed = asyncio.run(scenario())

    assert [answer["ok"] for answer in answers] == [False, False]
    assert all("already accepted" in str(answer["error"]) for answer in answers)
    assert claimed == []


def test_concurrent_completions_claim_for_only_the_accepted_one() -> None:
    async def scenario() -> tuple[list[dict[str, object]], list[OpenedPullRequest]]:
        claimed: list[OpenedPullRequest] = []

        async def claim(reported: OpenedPullRequest) -> bool:
            claimed.append(reported)
            return True

        broker = await _reporting_broker(ReportingSourceControl(), claim)
        answers = list(await asyncio.gather(
            broker._submit(_direct_request(broker, "complete-1", "complete_step", _REPORT)),
            broker._submit(_direct_request(broker, "complete-2", "complete_step", _REPORT)),
        ))
        return answers, claimed

    answers, claimed = asyncio.run(scenario())

    assert sorted(answer["ok"] for answer in answers) == [False, True]
    assert len(claimed) == 1


@pytest.mark.parametrize(
    ("remote_url", "change_request_url"),
    [
        ("git@github.com:acme/api.git", "https://github.com/acme/api/pull/7"),
        ("https://github.com/Acme/API.git", "https://github.com/acme/api/pull/7"),
        ("ssh://git@github.com/acme/api", "https://github.com/acme/api/pull/7"),
        ("git@github.com:acme/api", "https://github.com/acme/api/pull/7"),
        (
            "git@gitlab.example.com:group/sub/project.git",
            "https://gitlab.example.com/group/sub/project/-/merge_requests/3",
        ),
    ],
)
def test_a_push_names_the_project_its_change_requests_are_keyed_by(
    remote_url: str, change_request_url: str,
) -> None:
    """The two readings have to agree, or a push confirms no pull request."""
    requested = change_request(change_request_url)
    assert requested is not None
    assert remote_project(remote_url) == requested.project


@pytest.mark.parametrize(
    "remote_url",
    ["/tmp/mirror.git", "../mirror", "git@github.com:api.git", "", "https:///api.git",
     "https://gitlab.example.com:2222/group/repo",
     "https://github.com:443/acme/api", "ssh://git@github.com:22/acme/api",
     "https://github.com:invalid/acme/api"],
)
def test_a_remote_naming_no_forge_project_names_none(remote_url: str) -> None:
    assert remote_project(remote_url) is None


@pytest.mark.parametrize("rewrite", ["insteadOf", "pushInsteadOf"])
def test_redirected_literal_url_push_cannot_claim_an_existing_pr(
    tmp_path, rewrite, *, git_repo
):
    """A real redirected push changes a mirror, while the forge PR stays put."""
    import dataclasses
    import subprocess
    from unittest.mock import AsyncMock

    from engine.adapters.source_control.github import GitHubSourceControl

    def git(*args):
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    checkout, mirror = tmp_path / "checkout", tmp_path / "mirror.git"
    git_repo(mirror, bare=True)
    git_repo(checkout, branch="feature")
    git("-C", str(checkout), "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "--allow-empty", "-m", "initial")
    sha = git("-C", str(checkout), "rev-parse", "HEAD")
    url = "https://github.com/acme/api.git"
    git("-C", str(checkout), "config", f"url.{mirror}.{rewrite}", url)

    provider = AsyncMock()
    provider.root_path.return_value = str(checkout)
    adapter = GitHubSourceControl("", workspace_provider=provider)
    adapter._api = AsyncMock(return_value=[{"ref": "refs/heads/feature", "object": {"sha": sha}}])

    class RedirectedSourceControl(ReportingSourceControl):
        async def run_git(self, workspace, arguments):
            return await adapter.run_git(workspace, arguments)

        async def branch_tips(self, project, destinations):
            return await adapter.branch_tips(project, destinations)

        async def view_change_request(self, workspace, number):
            return dataclasses.replace(
                await super().view_change_request(workspace, number), head_sha=sha
            )

    async def scenario():
        claim = AsyncMock(return_value=True)
        broker = await _reporting_broker(
            RedirectedSourceControl(), claim, push=("push", url, "feature")
        )
        answer = await broker._submit(
            _direct_request(broker, "complete-1", "complete_step", _REPORT)
        )
        assert answer["ok"] is False
        claim.assert_not_awaited()
        assert not broker._pushed

    asyncio.run(scenario())
    assert git("--git-dir", str(mirror), "rev-parse", "refs/heads/feature") == sha
    assert adapter._api.await_count == 2
    adapter._api.assert_called_with(
        "GET", "/repos/acme/api/git/matching-refs/heads/feature"
    )


def test_concurrent_update_is_not_credited_to_noop_push():
    from unittest.mock import AsyncMock

    class ConcurrentSourceControl(ReportingSourceControl):
        async def run_git(self, workspace, arguments):
            if arguments[0] == "rev-parse":
                return GitResult(0, "old123", "")
            return await super().run_git(workspace, arguments)

    async def scenario():
        claim = AsyncMock(return_value=True)
        broker = await _reporting_broker(ConcurrentSourceControl(before="old123"), claim)
        answer = await broker._submit(_direct_request(broker, "complete-1", "complete_step", _REPORT))
        assert answer["ok"] is False
        assert not broker._pushed
        claim.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("accepted", [True, False])
def test_issue_publication_requires_explicit_resolution_and_approval(accepted):
    async def scenario():
        source = AsyncMock()
        source.request_review.return_value = "https://github.com/acme/api/pull/8"
        approval = AsyncMock(return_value=ApprovalDecision.ACCEPT if accepted else ApprovalDecision.CANCEL)
        broker = TerminalMcpBroker(run_id=RunId("run"), agent_run_id=AgentRunId("agent"), step=STEP, registry=TerminalResultRegistry())
        broker.enable_repository_tools(source, ("open_pull_request",), WorkspaceId("ws"), approval)
        broker.enable_issue({"repository": "acme/api", "number": 7})
        lookup = AsyncMock(side_effect=RuntimeError("no existing ownership"))
        broker.enable_pull_request_ownership(lookup)
        record = AsyncMock()
        broker.enable_pull_request_records(record)
        args = {"branch": "agent/issue", "title": "feat: fix", "body": "Change"}
        result = await broker._submit(_direct_request(broker, "1", "open_pull_request", args))
        assert not result["ok"] and "issue_resolution" in result["error"]
        source.request_review.assert_not_awaited()
        args["issue_resolution"] = "resolves"
        result = await broker._submit(_direct_request(broker, "2", "open_pull_request", args))
        assert result["ok"] is accepted
        assert source.request_review.await_count == int(accepted)
        lookup.assert_not_awaited()
        assert record.await_count == int(accepted)
        if accepted:
            assert source.request_review.await_args.kwargs == {"issue": {"repository": "acme/api", "number": 7}, "issue_resolution": "resolves"}
        approval.assert_awaited_once()
        request = approval.await_args.args[0]
        assert "append an empty metadata commit" in request.reason
        assert "push it normally" in request.reason
        assert "repository hooks and signing helpers are disabled" in request.reason
        assert "helpers still run on the host" in request.reason
        assert json.loads(request.arguments) == args
    asyncio.run(scenario())


@pytest.mark.parametrize("accepted", [True, False])
def test_review_resolution_is_approval_gated(accepted):
    async def scenario():
        source = AsyncMock()
        source.add_comment.return_value = CommentResult(2, "https://github.com/acme/api/pull/7#discussion_r2")
        approval = AsyncMock(return_value=ApprovalDecision.ACCEPT if accepted else ApprovalDecision.CANCEL)
        broker = TerminalMcpBroker(run_id=RunId("run"), agent_run_id=AgentRunId("agent"), step=STEP, registry=TerminalResultRegistry())
        broker.enable_repository_tools(source, ("add_comment",), WorkspaceId("ws"), approval)
        args = {"pr_url": "https://github.com/acme/api/pull/7", "comment": "Fixed the race", "in_reply_to_id": 1, "thread_id": "PRRT_1", "resolve": True, "commit_sha": "abcdef0"}
        result = await broker._submit(_direct_request(broker, "1", "add_comment", args))
        assert result["ok"] is accepted
        assert source.add_comment.await_count == int(accepted)
        assert approval.await_args.args[0].tool_name == "mcp__workflow__add_comment"
        if accepted:
            assert source.add_comment.await_args.kwargs == {"thread_id": "PRRT_1", "resolve": True, "commit_sha": "abcdef0"}
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["https://github.com/acme/api/pull/7", "https://gitlab.com/acme/api/-/merge_requests/7"])
def test_plain_reply_accepts_omitted_resolution_metadata(url):
    async def scenario():
        source = AsyncMock()
        source.add_comment.return_value = CommentResult(2, url + "#reply")
        approval = AsyncMock(return_value=ApprovalDecision.ACCEPT)
        broker = TerminalMcpBroker(run_id=RunId("run"), agent_run_id=AgentRunId("agent"), step=STEP, registry=TerminalResultRegistry())
        broker.enable_repository_tools(source, ("add_comment",), WorkspaceId("ws"))
        result = await broker._submit(_direct_request(broker, "1", "add_comment", {"pr_url": url, "comment": "Needs a decision", "in_reply_to_id": 1}))
        assert result["ok"]
        source.add_comment.assert_awaited_once_with(url, "Needs a decision", None, None, 1)
        approval.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("recorded", [(), (("acme/api", 7),)])
def test_force_push_ownership_is_bound_to_work_order(recorded):
    async def scenario():
        from engine.ports import GitResult
        source = AsyncMock()
        source.run_git.return_value = GitResult(exit_code=0, stdout="", stderr="")
        broker = TerminalMcpBroker(run_id=RunId("run"), agent_run_id=AgentRunId("agent"), step=STEP, registry=TerminalResultRegistry())
        broker.enable_repository_tools(source, ("git_subcommand",), WorkspaceId("ws"), AsyncMock(return_value=ApprovalDecision.ACCEPT))
        broker.enable_pull_request_ownership(AsyncMock(return_value=recorded))
        result = await broker._submit(_direct_request(broker, "1", "git_subcommand", {"arguments": ["push", "--force-with-lease", "origin", "agent/change"]}))
        assert result["ok"] == bool(recorded)
        if recorded:
            assert source.run_git.await_args.kwargs == {"owned_pull_requests": recorded}
        else:
            source.run_git.assert_not_awaited()
    asyncio.run(scenario())


def test_force_push_refused_when_ownership_lookup_fails():
    async def scenario():
        source = AsyncMock()
        broker = TerminalMcpBroker(run_id=RunId("run"), agent_run_id=AgentRunId("agent"), step=STEP, registry=TerminalResultRegistry())
        broker.enable_repository_tools(source, ("git_subcommand",), WorkspaceId("ws"), AsyncMock(return_value=ApprovalDecision.ACCEPT))
        broker.enable_pull_request_ownership(AsyncMock(side_effect=RuntimeError("store offline")))
        result = await broker._submit(_direct_request(broker, "1", "git_subcommand", {"arguments": ["push", "origin", "+HEAD:agent/change"]}))
        assert not result["ok"]
        source.run_git.assert_not_awaited()
    asyncio.run(scenario())
