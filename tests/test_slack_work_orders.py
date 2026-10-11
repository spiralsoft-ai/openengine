"""Starting a work order by pinging the bot, and reporting it back.

Four things have to hold for the feature to be what it says it is: a mention
becomes a run, the run remembers where it came from, the agent can say
something mid-step, and the endings -- complete, fail, clarify, review ready --
arrive in the same thread.
"""

import asyncio
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

from provider_fakes import FakeACPProvider, call_mcp

from web_fakes import RecordingCommunications

import pytest

from engine.adapters.communications.slack import (
    mention_from_event,
    verify_signature,
)
from engine.domain import (
    AgentId,
    AgentRunId,
    RunId,
    RunOrigin,
    RunState,
    StepId,
    StepSpec,
    TaskId,
    WorkflowId,
)
from engine.ports import Message as CommunicationsMessage
from engine.runtime import RunNotifier, WorkOrdersConfig
from engine.runtime.terminal_mcp import TerminalMcpBroker, TerminalResultRegistry


SIGNING_SECRET = "shhh"


def _signed(body: bytes, secret: str = SIGNING_SECRET) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = "v0=" + hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
        "content-type": "application/json",
    }


# --- reading a delivery ------------------------------------------------------


def test_mention_becomes_a_request_without_the_bot_token() -> None:
    mention = mention_from_event(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT|openengine> please add a   health endpoint",
            },
        }
    )
    assert mention is not None
    assert mention.text == "please add a health endpoint"
    assert (mention.channel, mention.author) == ("C123", "U777")
    # No thread yet, so the mention itself is the thread to answer under.
    assert mention.thread_id == "1700.0001"


def test_mention_inside_a_thread_answers_that_thread() -> None:
    mention = mention_from_event(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0009",
                "thread_ts": "1700.0001",
                "text": "<@UBOT> do it",
            },
        }
    )
    assert mention is not None and mention.thread_id == "1700.0001"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "url_verification", "challenge": "abc"},
        {"type": "event_callback", "event": {"type": "message", "text": "hi"}},
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "ts": "1",
                "bot_id": "B1",
                "text": "<@UBOT> loop",
            },
        },
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "ts": "1",
                "text": "<@UBOT>",
            },
        },
    ],
    ids=["handshake", "other-event", "the-bot-itself", "nothing-asked"],
)
def test_deliveries_that_are_not_a_request(payload: dict) -> None:
    assert mention_from_event(payload) is None


def test_signature_accepts_slack_and_refuses_everything_else() -> None:
    body = b'{"type":"event_callback"}'
    headers = _signed(body)
    assert verify_signature(
        SIGNING_SECRET,
        headers["x-slack-request-timestamp"],
        headers["x-slack-signature"],
        body,
    )
    assert not verify_signature(
        "another-secret",
        headers["x-slack-request-timestamp"],
        headers["x-slack-signature"],
        body,
    )
    assert not verify_signature(
        SIGNING_SECRET,
        headers["x-slack-request-timestamp"],
        headers["x-slack-signature"],
        body + b" ",
    )
    # A capture replayed an hour later is refused even though it verifies.
    stale = str(int(time.time()) - 3600)
    replayed = "v0=" + hmac.new(
        SIGNING_SECRET.encode(),
        b"v0:" + stale.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    assert not verify_signature(SIGNING_SECRET, stale, replayed, body)


@pytest.mark.parametrize("timestamp", ["nan", "inf", "-inf", "not-a-number"])
def test_a_timestamp_that_is_not_a_time_is_refused(timestamp: str) -> None:
    """The age check has to reject these, not fall through to the digest.

    `float("nan")` parses, and every comparison against NaN is False -- so an
    age test written the obvious way round waves it past the only guard there
    is against a replay.
    """
    body = b'{"type":"event_callback"}'
    signature = "v0=" + hmac.new(
        SIGNING_SECRET.encode(),
        b"v0:" + timestamp.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    assert not verify_signature(SIGNING_SECRET, timestamp, signature, body)


# --- the endpoint ------------------------------------------------------------


def _mention_graph():
    """The workflow these mentions name, doing nothing in particular."""
    from engine.graph_runtime_langgraph import State, graph_workflow
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(State)
    builder.add_node("work", lambda state: {})
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    return graph_workflow(
        builder, id="implementation-review-v1", name="Implementation review"
    )

def _workflow_catalog():
    """A catalog holding the workflow these mentions name."""
    from engine.runtime import WorkflowCatalog

    return WorkflowCatalog.from_graphs((_mention_graph(),))


def test_handshake_is_answered_with_the_challenge(*, slack_app, client) -> None:
    app, _capabilities, slack_store = slack_app(RecordingCommunications(), WorkOrdersConfig())
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    with client(app) as browser:
        response = browser.post("/api/slack/events", content=body, headers=_signed(body))
        browser.portal.call(app.state.slack_ingress.drain)
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc"}


def test_an_unsigned_delivery_starts_nothing(*, slack_app, client) -> None:
    communications = RecordingCommunications()
    app, capabilities, slack_store = slack_app(
        communications,
        WorkOrdersConfig(repository="acme/api", workflow="implementation-review-v1"),
        _workflow_catalog(),
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "ts": "1",
                "text": "<@UBOT> do something",
            },
        }
    ).encode()
    with client(app) as browser:
        response = browser.post(
            "/api/slack/events",
            content=body,
            headers={
                "x-slack-request-timestamp": str(int(time.time())),
                "x-slack-signature": "v0=not-a-signature",
            },
        )
    assert response.status_code == 401
    assert communications.posts == []
    assert asyncio.run(capabilities.state_store.list_runs()) == ()


def test_a_mention_replies_through_the_concierge(*, slack_app, client) -> None:
    """A mention routes through the concierge agent and replies in thread."""

    communications = RecordingCommunications()
    app, capabilities, slack_store = slack_app(communications, WorkOrdersConfig(
            repository="acme/api",
            workflow="implementation-review-v1",
            runner="default",
        ), _workflow_catalog())
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT> add a health endpoint",
            },
        }
    ).encode()
    with client(app) as browser:
        response = browser.post("/api/slack/events", content=body, headers=_signed(body))
        browser.portal.call(app.state.slack_ingress.drain)

    assert response.status_code == 200
    # The concierge replies in the thread with its greeting.
    channel, message, thread_id = communications.posts[0]
    assert (channel, thread_id) == ("C123", "1700.0001")
    assert message.mention == "U777"
    assert "Hi, how can I help?" in message.text


def test_a_redelivery_is_ignored(*, slack_app, client) -> None:
    communications = RecordingCommunications()
    app, capabilities, slack_store = slack_app(communications, WorkOrdersConfig(
            repository="acme/api",
            workflow="implementation-review-v1",
            runner="default",
        ), _workflow_catalog())
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT> add a health endpoint",
            },
        }
    ).encode()
    with client(app) as browser:
        browser.post("/api/slack/events", content=body, headers=_signed(body))
        retry = browser.post(
            "/api/slack/events",
            content=body,
            headers={**_signed(body), "x-slack-retry-num": "1"},
        )

        browser.portal.call(app.state.slack_ingress.drain)

    assert retry.status_code == 200
    # Only one reply — the redelivery was ignored.
    assert len(communications.posts) == 1


def test_a_mention_without_config_still_greets(*, slack_app, client) -> None:
    """Even without work_orders config, the concierge greets the user."""

    communications = RecordingCommunications()
    app, capabilities, slack_store = slack_app(communications, WorkOrdersConfig(), _workflow_catalog())
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT> add a health endpoint",
            },
        }
    ).encode()
    with client(app) as browser:
        response = browser.post("/api/slack/events", content=body, headers=_signed(body))
        browser.portal.call(app.state.slack_ingress.drain)

    assert response.status_code == 200
    # The concierge greets regardless of work_orders config — it is the
    # create_workorder tool that checks repositories, not the greeting.
    _channel, message, thread_id = communications.posts[0]
    assert thread_id == "1700.0001"
    assert "Hi, how can I help?" in message.text


def test_a_mention_starts_nothing_while_slack_is_disconnected(
    *, slack_app, client
) -> None:
    """An app stays installed after this server disconnects, so mentions arrive.

    Starting one would provision a workspace and run a write-access agent to
    completion with every reply -- including a refusal -- dropped on the floor.
    """

    communications = RecordingCommunications()
    app, capabilities, slack_store = slack_app(communications, WorkOrdersConfig(
            repository="acme/api",
            workflow="implementation-review-v1",
            runner="default",
        ), _workflow_catalog())
    slack_store.token.return_value = None
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT> add a health endpoint",
            },
        }
    ).encode()
    with client(app) as browser:
        response = browser.post("/api/slack/events", content=body, headers=_signed(body))
        browser.portal.call(app.state.slack_ingress.drain)
        # And the panel does not claim otherwise while it is in that state.
        status = browser.get("/api/slack/status").json()

    assert response.status_code == 200
    assert asyncio.run(capabilities.state_store.list_runs()) == ()
    assert communications.posts == []
    assert status["connected"] is False
    assert status["events"] is False


# --- reporting back ----------------------------------------------------------


def test_update_status_is_served_only_to_a_run_with_somewhere_to_report() -> None:
    async def scenario() -> None:
        reported: list[str] = []

        async def report(status: str) -> None:
            reported.append(status)

        step = StepSpec(StepId("implementation"), AgentId("coder"))
        silent = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=step,
            registry=TerminalResultRegistry(),
        )
        async with silent:
            assert "--status-updates" not in silent.config.args
            refused = await silent._submit(
                {
                    "token": silent._token,
                    "request_id": 1,
                    "name": "update_status",
                    "arguments": {"status": "working on it"},
                }
            )
        assert refused["ok"] is False

        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-2"),
            step=step,
            registry=TerminalResultRegistry(),
        )
        broker.enable_status_updates(report)
        async with broker:
            assert "--status-updates" in broker.config.args
            accepted = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 2,
                    "name": "update_status",
                    "arguments": {"status": "reading the code"},
                }
            )
            blank = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 3,
                    "name": "update_status",
                    "arguments": {"status": "  "},
                }
            )
        assert accepted["ok"] is True
        assert blank["ok"] is False
        assert reported == ["reading the code"]

    asyncio.run(scenario())


def test_clarify_is_reported_because_no_event_carries_it() -> None:
    async def scenario() -> None:
        reported: list[str] = []

        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=StepSpec(StepId("implementation"), AgentId("coder")),
            registry=TerminalResultRegistry(),
        )
        broker.enable_status_updates(lambda status: _record(reported, status))
        async with broker:
            response = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 1,
                    "name": "clarify",
                    "arguments": {},
                }
            )
        assert response["acknowledgement"] == "clarified"
        assert reported == ["answered a question without changing the work order"]

    asyncio.run(scenario())


async def _record(sink: list[str], status: str) -> None:
    sink.append(status)


def test_a_run_from_the_web_is_never_announced() -> None:
    communications = RecordingCommunications()
    notifier = RunNotifier(communications, "https://engine.example")
    state = RunState(
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        workflow_id=WorkflowId("implementation-review-v1"),
    )
    asyncio.run(notifier.announce(state, "half way there"))
    assert communications.posts == []


def test_the_signing_secret_can_be_added_without_reconnecting(
    *, slack_app, client
) -> None:
    """Enabling mentions must not cost an operator their Slack connection.

    Saving the OAuth pair revokes the token and starts the flow over, which is
    right when the app changes and wrong as the price of one extra secret.
    """

    app, _capabilities, slack_store = slack_app(RecordingCommunications(), WorkOrdersConfig())
    store = slack_store
    store.signing_secret.return_value = None

    with (
        patch(
            "engine.apps.web.api.revoke_slack_token", new=AsyncMock()
        ) as revoke,
        client(app) as browser,
    ):
        response = browser.post(
            "/api/slack/credentials", json={"signingSecret": "shhh"}
        )

    assert response.status_code == 204
    store.set_signing_secret.assert_called_once_with("shhh")
    store.set_credentials.assert_not_called()
    store.disconnect.assert_not_called()
    revoke.assert_not_awaited()


def test_the_signing_secret_alone_needs_credentials_already_saved(
    *, slack_app, client
) -> None:
    app, _capabilities, slack_store = slack_app(RecordingCommunications(), WorkOrdersConfig())
    store = slack_store
    store.credentials.return_value = None

    with client(app) as browser:
        response = browser.post(
            "/api/slack/credentials", json={"signingSecret": "shhh"}
        )

    assert response.status_code == 409
    store.set_signing_secret.assert_not_called()


class BrokenCommunications:
    async def post(self, *_args, **_kwargs) -> str:
        raise RuntimeError("Slack is unavailable")

    async def reply(self, *_args) -> str:  # pragma: no cover
        raise NotImplementedError


def _origin_state() -> RunState:
    return RunState(
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        workflow_id=WorkflowId("implementation-review-v1"),
        origin=RunOrigin(channel="C1", thread_id="1700.0001", author="U1"),
    )


def test_a_provider_that_is_down_does_not_break_the_run() -> None:
    notifier = RunNotifier(BrokenCommunications(), "https://engine.example")
    asyncio.run(notifier.announce(_origin_state(), "half way there"))


def test_an_agent_is_told_when_its_status_did_not_reach_anyone() -> None:
    """The acknowledgement has to be true, or it is worse than no answer.

    An agent told "status posted" by a step whose status went nowhere will not
    mention the gap or say it again, so the one path with somebody waiting on
    the answer reports the failure instead of swallowing it.
    """

    async def scenario() -> None:
        notifier = RunNotifier(BrokenCommunications(), "https://engine.example")
        state = _origin_state()

        async def report(status: str) -> None:
            await notifier.deliver(state, f"*Implementation*: {status}")

        broker = TerminalMcpBroker(
            run_id=state.run_id,
            agent_run_id=AgentRunId("agent-run-1"),
            step=StepSpec(StepId("implementation"), AgentId("coder")),
            registry=TerminalResultRegistry(),
        )
        broker.enable_status_updates(report)
        async with broker:
            answer = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 1,
                    "name": "update_status",
                    "arguments": {"status": "reading the code"},
                }
            )
            # The step is not ended by it: the run is the thing that matters,
            # and a `clarify` in the same session still answers normally.
            clarified = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 2,
                    "name": "clarify",
                    "arguments": {},
                }
            )
        assert answer["ok"] is False
        assert "Slack is unavailable" in answer["error"]
        assert clarified == {"ok": True, "acknowledgement": "clarified"}

    asyncio.run(scenario())


# --- concierge broker ---------------------------------------------------------


def test_concierge_broker_creates_a_work_order() -> None:
    """The create_workorder tool calls the factory callback and returns the URL."""
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        created: list[tuple[str, str]] = []

        async def create(repository: str, prompt: str) -> tuple[str, str]:
            created.append((repository, prompt))
            return "https://engine.example/runs/run-abc", "run-abc"

        broker = ConciergeBroker(
            create_workorder=create,
            default_repository="acme/api",
        )
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "create_workorder",
                    "arguments": {"prompt": "add a health endpoint"},
                }
            )
        assert result["ok"] is True
        assert "run-abc" in result["text"]
        assert created == [("acme/api", "add a health endpoint")]

    asyncio.run(scenario())


def test_concierge_broker_falls_back_to_dot_when_no_default() -> None:
    """Without a configured default the broker uses '.' (current directory)."""
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        created: list[tuple[str, str]] = []

        async def create(repository: str, prompt: str) -> tuple[str, str]:
            created.append((repository, prompt))
            return "https://engine.example/runs/run-1", "run-1"

        broker = ConciergeBroker(create_workorder=create, default_repository="")
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "create_workorder",
                    "arguments": {"prompt": "do something"},
                }
            )
        assert result["ok"] is True
        assert created == [(".", "do something")]

    asyncio.run(scenario())


def test_concierge_broker_uses_configured_default_repository() -> None:
    """The configured default repository is always used."""
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        created: list[tuple[str, str]] = []

        async def create(repository: str, prompt: str) -> tuple[str, str]:
            created.append((repository, prompt))
            return "https://engine.example/runs/run-1", "run-1"

        broker = ConciergeBroker(
            create_workorder=create,
            default_repository="acme/api",
        )
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "create_workorder",
                    "arguments": {
                        "prompt": "fix the bug",
                    },
                }
            )
        assert result["ok"] is True
        assert created == [("acme/api", "fix the bug")]

    asyncio.run(scenario())


def test_concierge_broker_rejects_unknown_arguments() -> None:
    """Extra arguments (like repository) are rejected."""
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        async def create(repository: str, prompt: str) -> tuple[str, str]:
            raise AssertionError("should not be called")

        broker = ConciergeBroker(
            create_workorder=create,
            default_repository="",
        )
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "create_workorder",
                    "arguments": {
                        "prompt": "fix the bug",
                        "repository": "acme/frontend",
                    },
                }
            )
        assert result["ok"] is False
        assert "unknown" in result["error"]

    asyncio.run(scenario())


def test_concierge_broker_rejects_empty_prompt() -> None:
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        async def create(repository: str, prompt: str) -> tuple[str, str]:
            raise AssertionError("should not be called")

        broker = ConciergeBroker(
            create_workorder=create, default_repository="acme/api"
        )
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "create_workorder",
                    "arguments": {"prompt": "  "},
                }
            )
        assert result["ok"] is False
        assert "prompt" in result["error"]

    asyncio.run(scenario())


def test_concierge_broker_rejects_unknown_tool() -> None:
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        async def create(repository: str, prompt: str) -> tuple[str, str]:
            raise AssertionError("should not be called")

        broker = ConciergeBroker(
            create_workorder=create, default_repository="acme/api"
        )
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "complete_step",
                    "arguments": {},
                }
            )
        assert result["ok"] is False
        assert "unknown" in result["error"]

    asyncio.run(scenario())


# --- concierge MCP protocol --------------------------------------------------
#
# What a Slack agent's CLI sees when it connects. The transport answering these
# is shared and tested once in `test_single_tool_mcp.py`; kept here as well
# because the answers are this surface's, and a shared implementation is
# exactly where a change made for the other surface could quietly alter them.


def _slack_mcp_answer(request: object) -> dict[str, object] | None:
    from engine.single_tool_mcp import mcp_response
    from engine.slack_concierge import slack_egress

    async def scenario() -> dict[str, object] | None:
        # Port 0 connects to nothing: none of these reach the host, which is
        # part of what they assert.
        return await mcp_response(
            "127.0.0.1", 0, "tok", request,
            tool_spec=slack_egress._TOOL_SPEC,
            server_info_name=slack_egress._SERVER_INFO_NAME,
        )

    return asyncio.run(scenario())


def test_mcp_initialize_returns_server_protocol_version() -> None:
    """The server always returns its own version, not the client's."""
    from engine.single_tool_mcp import PROTOCOL_VERSION

    result = _slack_mcp_answer(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "1999-01-01",
            "clientInfo": {"name": "test", "version": "1"},
        }},
    )
    assert result is not None
    assert result["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_mcp_tools_list_returns_create_workorder() -> None:
    result = _slack_mcp_answer({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert result is not None
    tools = result["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "create_workorder"


def test_mcp_notifications_are_swallowed() -> None:
    assert _slack_mcp_answer(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ) is None


def test_mcp_unknown_method_returns_error() -> None:
    result = _slack_mcp_answer(
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}
    )
    assert result is not None
    assert result["error"]["code"] == -32601


def test_review_decision_is_available_over_the_real_concierge_mcp_server():
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario():
        decide = AsyncMock(return_value=("https://engine.example/runs/run-1", "run-1"))
        broker = ConciergeBroker(create_workorder=AsyncMock(), decide_review=decide)
        async with broker:
            assert "--enable-review-decisions" in broker.config["args"]
            result = await call_mcp(
                broker.config, "decide_workorder_review",
                arguments={"approved": False, "summary": "Please add coverage."},
            )
        assert not result.get("isError"), result
        assert result["structuredContent"]["approved"] is False
        decide.assert_awaited_once_with(False, "Please add coverage.")

    asyncio.run(scenario())


def test_reused_concierge_session_submits_review_as_current_sender():
    from engine.slack_concierge import IncomingMessage, SlackConcierge

    async def scenario():
        provider = FakeACPProvider(review=True)
        decide = AsyncMock(return_value=("url", "run-one"))
        agent = SlackConcierge(
            provider=provider, reply=AsyncMock(), create_workorder=AsyncMock(),
            decide_review=decide,
        )
        try:
            origin = RunOrigin(channel="C", thread_id="1", author="REVIEWER")
            await agent.handle(IncomingMessage(origin, "approve the review"))
            decide.assert_awaited_once_with(origin, True, "Approved in Slack.")
        finally:
            await agent.close()

    asyncio.run(scenario())


def test_concierge_graph_reuse_eviction_failure_and_empty_reply():
    from engine.slack_concierge import IncomingMessage, SlackConcierge

    async def scenario():
        provider = FakeACPProvider(text=" ")
        replies = []
        async def reply(origin, text):
            replies.append(text)
        async def create(origin, repository, prompt):
            return "url", "id"
        agent = SlackConcierge(provider=provider, reply=reply, create_workorder=create, max_threads=1)
        def message(thread):
            return IncomingMessage(RunOrigin(channel="C", thread_id=thread, author="U"), "hello")
        await agent.handle(message("1"))
        await agent.handle(message("1"))
        assert len(provider.clients) == 1
        assert len(provider.clients[0].prompts) == 2
        assert replies == ["I'm working on that."] * 2
        await agent.handle(message("2"))
        assert provider.clients[0].closed
        assert not agent.has_thread("C", "1")
        provider.fail = True
        import pytest
        with pytest.raises(RuntimeError, match="transient"):
            await agent.handle(message("2"))
        assert not agent.has_thread("C", "2")
        await agent.handle(message("2"))
        assert len(provider.clients) == 3
        await agent.close()
        assert all(c.closed for c in provider.clients)
    asyncio.run(scenario())


def test_reused_concierge_session_steers_as_current_sender():
    """A follow-up in a thread is attributed to its actual author."""
    from engine.slack_concierge import IncomingMessage, SlackConcierge

    async def scenario() -> None:
        provider = FakeACPProvider(steer=True)
        steer = AsyncMock(return_value=("url", "run-one"))
        agent = SlackConcierge(
            provider=provider,
            reply=AsyncMock(),
            create_workorder=AsyncMock(),
            steer_workorder=steer,
        )
        try:
            first = RunOrigin(channel="C", thread_id="1", author="FIRST")
            second = RunOrigin(channel="C", thread_id="1", author="SECOND")
            await agent.handle(IncomingMessage(first, "hello"))
            await agent.handle(
                IncomingMessage(second, "follow the system theme")
            )
            steer.assert_awaited_once_with(second, "follow the system theme")
        finally:
            await agent.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_after_create", [False, True])
def test_thread_reply_creates_workorder_through_stdio_mcp(
    tmp_path, fail_after_create, *, slack_app, client
):
    from engine.graph_runtime_langgraph.workflows import sqlite_runtime

    graph = _mention_graph()
    provider = FakeACPProvider(create=True, fail_after_create=fail_after_create)
    communications = RecordingCommunications()
    app, capabilities, _ = slack_app(
        communications,
        WorkOrdersConfig(
            repository="acme/api", workflow="implementation-review-v1", runner="default"
        ),
        _workflow_catalog(),
        provider=provider,
        graph_runtime=sqlite_runtime((graph,), tmp_path / "graph"),
    )
    def body(kind, ts, text, **extra):
        return json.dumps({"type": "event_callback", "event": dict(
            type=kind, channel="C", user="U", ts=ts, text=text, **extra)}).encode()
    with client(app) as browser:
        greeting = body("app_mention", "1", "<@BOT>")
        assert browser.post("/api/slack/events", content=greeting, headers=_signed(greeting)).status_code == 200
        browser.portal.call(app.state.slack_ingress.drain)
        assert not browser.portal.call(capabilities.state_store.list_runs)
        request = body("message", "2", "new workorder please", thread_ts="1")
        browser.post("/api/slack/events", content=request, headers=_signed(request))
        browser.portal.call(app.state.slack_ingress.drain)
        # Both Slack event kinds describe the same message; execute only once.
        duplicate = body("app_mention", "2", "new workorder please", thread_ts="1")
        browser.post("/api/slack/events", content=duplicate, headers=_signed(duplicate))
        browser.portal.call(app.state.slack_ingress.drain)
        runs = browser.portal.call(capabilities.state_store.list_runs)
        assert len(runs) == 1
        assert runs[0].origin.thread_id == "1"
        result = provider.clients[0].result
        assert not result.get("isError"), result
        assert result["structuredContent"]["url"].startswith("https://engine.example")
        assert len(provider.clients[0].prompts) == 2
        # Progress arrives from the graph run in the background.
        async def wait_for_progress():
            async with asyncio.timeout(10):
                while not any(m.progress and m.links for _, m, _ in communications.posts):
                    await asyncio.sleep(0.01)
        browser.portal.call(wait_for_progress)
    announcements = [
        m for _, m, _ in communications.posts
        if m.text.startswith("Started a work order")
    ]
    assert not announcements
    assert any(m.progress and m.links for _, m, _ in communications.posts)
    assert all(thread == "1" for _, _, thread in communications.posts)
    if not fail_after_create:
        messages = [m for _, m, _ in communications.posts]
        # The greeting uses the same reply text; compare with the creation reply.
        replies = [i for i, m in enumerate(messages) if m.text == provider.text]
        assert len(replies) == 2
        assert replies[-1] < next(i for i, m in enumerate(messages) if m.progress)


def test_concierge_uses_real_langgraph_acp_session(tmp_path):
    from pathlib import Path
    import sys
    from langgraph_acp.agent import StdioACPProvider
    from engine.slack_concierge import IncomingMessage, SlackConcierge

    async def scenario():
        log = tmp_path / "acp.jsonl"
        provider = StdioACPProvider(name="fake", command=[sys.executable,
            str(Path(__file__).resolve().parents[1] / "langgraph-acp/tests/fake_agent.py")],
            env={"FAKE_AGENT_LOG": str(log)})
        replies = []
        async def reply(origin, text):
            replies.append(text)
        async def create(origin, repository, prompt):
            raise AssertionError("greetings must not start work")
        agent = SlackConcierge(provider=provider, reply=reply, create_workorder=create)
        message = IncomingMessage(
            RunOrigin(channel="C", thread_id="1", author="U"), "hello")
        try:
            await agent.handle(message)
            await agent.handle(message)
        finally:
            await agent.close()
        requests = [json.loads(line) for line in log.read_text().splitlines()]
        new = [r for r in requests if r.get("method") == "session/new"]
        assert len(new) == 1
        config = new[0]["params"]["mcpServers"][0]
        assert config["name"] == "concierge"
        assert "--token" not in config["args"]
        assert not Path(config["args"][-1]).exists()
        assert len([r for r in requests if r.get("method") == "session/prompt"]) == 2
        assert len(replies) == 2
    asyncio.run(scenario())


def test_ingress_filters_messages_and_bounds_queue():
    from engine.slack_concierge import SlackIngress

    async def scenario():
        gate = asyncio.Event()
        messages = []
        class Concierge:
            def has_thread(self, channel, thread_id):
                return False
            async def handle(self, message):
                messages.append(message)
                await gate.wait()
            async def close(self):
                pass
        ingress = SlackIngress(Concierge(), capacity=1)
        def payload(kind, ts, **extra):
            return {"type": "event_callback", "event": dict(type=kind,
                channel="C", user="U", ts=ts, text="hello", **extra)}
        assert ingress.accept(payload("message", "0", thread_ts="unknown"))
        assert ingress.accept(payload("app_mention", "0", bot_id="bot"))
        assert ingress.accept(payload("message", "0", subtype="message_changed"))
        assert not messages
        assert ingress.accept(payload("app_mention", "1"))
        await asyncio.sleep(0)
        assert len(messages) == 1  # turn is blocked; accept already returned
        assert ingress.accept(payload("message", "2", thread_ts="1"))
        assert not ingress.accept(payload("app_mention", "3"))
        gate.set()
        await ingress.drain()
        assert ingress.accept(payload("app_mention", "3"))  # rejected event can retry
        await ingress.drain()
        assert [m.origin.thread_id for m in messages] == ["1", "1", "3"]
        await ingress.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("bot_marker", [{"bot_id": "B"}, {"bot_profile": {"id": "B"}}])
def test_ingress_does_not_query_workorders_for_a_bot_message(bot_marker, *, client):
    """Progress posts come back through Slack Events and must be cheap to ignore."""
    from engine.slack_concierge import SlackIngress
    from starlette.applications import Starlette
    from starlette.routing import Route

    class Concierge:
        linked_workorders = AsyncMock()

        def has_thread(self, channel, thread_id):
            return False

        async def handle(self, message):  # pragma: no cover - bot messages are filtered
            raise AssertionError("bot messages must not reach the concierge")

        async def close(self):
            pass

    concierge = Concierge()
    ingress = SlackIngress(
        concierge, signing_secret=lambda: "secret",
        verify_signature=lambda *_args: True,
    )
    app = Starlette(routes=[Route("/events", ingress.webhook, methods=["POST"])])
    payload = {"type": "event_callback", "event": {
        "type": "message", "channel": "C", "thread_ts": "1", "ts": "2",
        "user": "BOT", "text": "progress update", **bot_marker,
    }}
    with client(app) as browser:
        response = browser.post("/events", json=payload)
    assert response.status_code == 200
    concierge.linked_workorders.assert_not_awaited()


def test_ingress_reacts_with_eyes_before_handling():
    from engine.slack_concierge import SlackIngress

    async def scenario():
        order: list[str] = []
        messages = []

        class Concierge:
            def has_thread(self, channel, thread_id):
                return False
            async def handle(self, message):
                order.append("handle")
                messages.append(message)
            async def close(self):
                pass

        reacted: list[tuple[str, str, str]] = []

        async def react(channel, ts, emoji):
            order.append("react")
            reacted.append((channel, ts, emoji))

        ingress = SlackIngress(Concierge(), capacity=1, react=react)

        def payload(kind, ts, **extra):
            return {"type": "event_callback", "event": dict(
                type=kind, channel="C1", user="U1", ts=ts, text="hello", **extra)}

        ingress.accept(payload("app_mention", "1700.0001"))
        await ingress.drain()

        assert reacted == [("C1", "1700.0001", "eyes")]
        assert len(messages) == 1
        assert order == ["react", "handle"]

    asyncio.run(scenario())


def test_ingress_failing_react_does_not_prevent_handle():
    from engine.slack_concierge import SlackIngress

    async def scenario():
        messages = []

        class Concierge:
            def has_thread(self, channel, thread_id):
                return False
            async def handle(self, message):
                messages.append(message)
            async def close(self):
                pass

        async def failing_react(channel, ts, emoji):
            raise RuntimeError("Slack API down")

        ingress = SlackIngress(Concierge(), capacity=1, react=failing_react)

        def payload(kind, ts, **extra):
            return {"type": "event_callback", "event": dict(
                type=kind, channel="C1", user="U1", ts=ts, text="hello", **extra)}

        ingress.accept(payload("app_mention", "1700.0001"))
        await ingress.drain()

        # handle was still called despite the react failure
        assert len(messages) == 1

    asyncio.run(scenario())


def test_concierge_permissions_only_allow_the_granted_tool():
    from engine.slack_concierge.slack_egress import tool_permission
    from langgraph_acp.permissions import ACPPermissionRequest, ACPPermissionOption

    async def scenario():
        for name, allowed in [("mcp__concierge__create_workorder", True), ("Bash", False), ({}, False)]:
            result = await tool_permission(ACPPermissionRequest(agent="codex",
                tool_call={"name": name}, options=(ACPPermissionOption("yes", kind="allow_once"),)))
            assert result.granted == allowed
    asyncio.run(scenario())
@pytest.mark.parametrize("valid_signature", [True, False])
def test_slack_signature_auth_with_github_login_enabled(
    valid_signature, *, slack_app, client
):
    from engine.apps.web.github_login import GitHubLoginConfig

    app, _, _ = slack_app(RecordingCommunications(), WorkOrdersConfig(), github_login_config=GitHubLoginConfig(
            "client", "secret", "https://engine.example/api/auth/github/callback"
        ))
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    headers = _signed(body)
    if not valid_signature:
        headers["x-slack-signature"] = "v0=invalid"
    with client(app) as browser:
        assert browser.get("/api/config").status_code == 401
        response = browser.post("/api/slack/events", content=body, headers=headers)
    if valid_signature:
        assert response.status_code == 200
        assert response.json() == {"challenge": "abc"}
    else:
        assert response.status_code == 401


def test_checked_in_slack_repository_is_current_checkout():
    from pathlib import Path
    import tomllib

    config = tomllib.loads((Path(__file__).resolve().parents[1] / "engine.toml").read_text())
    assert config["work_orders"]["repository"] == "."


@pytest.mark.parametrize("ending", ("finished", "human_review", "failed"))
@pytest.mark.parametrize("before_row", (False, True))
@pytest.mark.parametrize("pr_url", ("https://github.com/example/repo/pull/42", None))
def test_slack_starts_configured_graph_with_input_defaults(
    tmp_path, ending, before_row, pr_url, *, slack_app, client
):
    from engine.graph_runtime_langgraph import State, WorkflowInput, graph_workflow
    from engine.graph_runtime_langgraph.workflows import sqlite_runtime
    from engine.runtime import WorkflowCatalog
    from engine.runtime.config import load_engine_config
    from langgraph.graph import START, END, StateGraph
    from pathlib import Path

    configured = load_engine_config(Path(__file__).resolve().parents[1] / "engine.toml")
    assert configured.config.work_orders.workflow == "implementation-review-rerank"
    builder = StateGraph(State)
    builder.add_node("work", lambda state: {"received": state["inputs"], "pr_url": pr_url})
    builder.add_edge(START, "work")
    if ending == "human_review":
        from engine.graph_runtime_langgraph.components import HumanReviewNode
        builder.add_node("decision", HumanReviewNode())
        builder.add_edge("work", "decision")
        builder.add_edge("decision", END)
    elif ending == "failed":
        def fail(state):
            raise RuntimeError("review service unavailable")
        builder.add_node("failure", fail)
        builder.add_edge("work", "failure")
        builder.add_edge("failure", END)
    else:
        builder.add_edge("work", END)
    graph = graph_workflow(
        builder, id="implementation-review-rerank", name="Implementation review rerank",
        inputs=(WorkflowInput("implementation_runner", "Implementation runner", "codex"),
                WorkflowInput("review_runner", "Review runner", "claude")),
    )
    # Force the graph to reach its ending before start() returns: notifications
    # must survive events arriving before the WorkOrder row/origin is saved.
    from contextlib import asynccontextmanager
    from engine.graph_runtime import RunStatus

    @asynccontextmanager
    async def runtime_before_row():
        async with sqlite_runtime((graph,), tmp_path / "graph") as runtime:
            start = runtime.start
            async def wait_for_ending(run_id):
                async with asyncio.timeout(10):
                    while (await runtime.snapshot(RunId(run_id))).status is RunStatus.RUNNING:
                        await asyncio.sleep(0.01)
            async def start_and_wait(*args, **kwargs):
                run = await start(*args, **kwargs)
                await wait_for_ending(run.run_id)
                return await runtime.snapshot(run.run_id)
            if before_row:
                runtime.start = start_and_wait
            # Also force events after the row exists but before the reply.
            provider.after_create = wait_for_ending
            yield runtime

    provider = FakeACPProvider(create=True, text="Created the work order.")
    communications = RecordingCommunications()
    app, capabilities, _ = slack_app(
        communications,
        configured.config.work_orders,
        WorkflowCatalog.from_graphs((graph,)),
        provider=provider,
        graph_runtime=runtime_before_row(),
    )
    body = json.dumps({"type": "event_callback", "event": {
        "type": "app_mention", "channel": "C", "user": "U", "ts": "1",
        "text": "<@BOT> new workorder please",
    }}).encode()
    with client(app) as browser:
        assert browser.post("/api/slack/events", content=body, headers=_signed(body)).status_code == 200
        browser.portal.call(app.state.slack_ingress.drain)
        result = provider.clients[0].result
        assert not result.get("isError"), result
        runs = browser.portal.call(capabilities.state_store.list_runs)
        assert len(runs) == 1
        assert str(runs[0].workflow_id) == graph.graph_id
        assert runs[0].origin.thread_id == "1"
        snapshot = browser.get(f"/graph/api/runs/{runs[0].run_id}?includeValues=true").json()
        assert snapshot["values"]["inputs"] == {
            "implementation_runner": "codex", "review_runner": "claude",
        }
        expected = {
            "finished": "Work order finished.",
            "human_review": "Review complete and ready for your decision.",
            "failed": "Work order failed: review service unavailable",
        }[ending]
        async def wait_for_notification():
            async with asyncio.timeout(10):
                while not any(message.text == expected for _, message, _ in communications.posts):
                    await asyncio.sleep(0.01)
        browser.portal.call(wait_for_notification)
        notifications = [
            (channel, message, thread) for channel, message, thread in communications.posts
            if message.text == expected
        ]
        assert len(notifications) == 1
        channel, message, thread = notifications[0]
        assert (channel, thread) == ("C", "1")
        assert message.mention == ("" if ending == "finished" else "U")
        assert message.progress == (ending == "finished")
        assert any(str(runs[0].run_id) in link.url for link in message.links)
        pr_links = [link.url for link in message.links if link.label == "View pull request"]
        assert pr_links == ([pr_url] if ending == "human_review" and pr_url else [])
        assert any(message.text == "*work* started." and message.progress
                   for _, message, _ in communications.posts)
    messages = [message for _, message, _ in communications.posts]
    assert messages[0].text == provider.text
    assert messages[1].text == "*work* started."
    assert messages[1].progress
    assert not messages[1].links
    assert not any(m.text.startswith("Started a work order") for m in messages)


def test_an_auto_approved_request_is_not_announced(*, slack_app, client) -> None:
    """A question the run answers itself is not reported to the thread.

    One work order asks to run dozens of commands, and with `auto_approve` on
    every one of them is settled by the run. Announcing each as "needs your
    approval" would bury the requests that really are somebody's -- the plan
    below, which is still announced.
    """
    from contextlib import asynccontextmanager


    from engine.domain import ApprovalKind
    from engine.graph_runtime import GraphId, NodeId
    from engine.runtime import WorkflowCatalog
    from engine.runtime.config import ApprovalConfig
    from graph_runtime_fakes import (
        Ask,
        AwaitSteering,
        ScriptedGraph,
        ScriptedGraphRuntime,
        ScriptedNode,
    )

    node = NodeId("implementation")
    graph = ScriptedGraph(
        GraphId("implementation-review-v1"),
        "Implementation review",
        (
            ScriptedNode(
                node,
                (
                    # Held here until the preference the app sets after starting
                    # the run has landed, so this is about what gets announced
                    # rather than a race with when auto-approve arrives.
                    AwaitSteering(),
                    Ask("Run git in the step's bound workspace"),
                    Ask("Approve the plan", kind=ApprovalKind.PLAN_APPROVAL),
                ),
            ),
        ),
    )
    runtime = ScriptedGraphRuntime(graph)

    @asynccontextmanager
    async def running(_app=None):
        yield runtime

    communications = RecordingCommunications()
    app, capabilities, _ = slack_app(
        communications,
        WorkOrdersConfig(repository="acme/api", workflow="implementation-review-v1"),
        WorkflowCatalog.from_graphs((graph,)),
        provider=FakeACPProvider(create=True),
        graph_runtime=running(),
        approval_policy=ApprovalConfig(auto_approve=True),
    )
    body = json.dumps({"type": "event_callback", "event": {
        "type": "app_mention", "channel": "C", "user": "U", "ts": "1",
        "text": "<@BOT> new workorder please",
    }}).encode()
    with client(app) as browser:
        assert browser.post("/api/slack/events", content=body, headers=_signed(body)).status_code == 200
        browser.portal.call(app.state.slack_ingress.drain)
        runs = browser.portal.call(capabilities.state_store.list_runs)
        assert len(runs) == 1
        run_id = runs[0].run_id

        def said() -> list[str]:
            return [
                message.text for _, message, _ in communications.posts
                if isinstance(message, CommunicationsMessage)
            ]

        async def reach_the_plan() -> None:
            async with asyncio.timeout(10):
                while node not in (await runtime.snapshot(run_id)).auto_approve_nodes:
                    await asyncio.sleep(0.01)
                await runtime.steer(run_id, "carry on")
                while not any("Approve the plan" in text for text in said()):
                    await asyncio.sleep(0.01)

        browser.portal.call(reach_the_plan)
        announced = said()

    assert "*implementation* needs your approval: Approve the plan" in announced
    assert not [text for text in announced if "Run git" in text]


def test_concierge_bridge_can_read_credential_and_list_tools() -> None:
    """Launch the real MCP child so Windows file-sharing failures are visible."""
    from pathlib import Path
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        async def create(repository: str, prompt: str) -> tuple[str, str]:
            raise AssertionError("listing tools must not create work")

        async with ConciergeBroker(create_workorder=create) as broker:
            config = broker.config
            args = config["args"]
            credential = Path(args[args.index("--token-file") + 1])
            child = await asyncio.create_subprocess_exec(
                config["command"], *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(
                    b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'
                ), timeout=20)
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            assert child.returncode == 0, stderr.decode()
            response = json.loads(stdout)
            assert "create_workorder" in [tool["name"] for tool in response["result"]["tools"]]
        assert not credential.exists()

    asyncio.run(scenario())

def test_ingress_qualifies_the_author_by_workspace():
    from engine.slack_concierge import SlackIngress

    async def scenario():
        messages = []

        class Concierge:
            def has_thread(self, channel, thread_id):
                return False

            async def handle(self, message):
                messages.append(message)

            async def close(self):
                pass

        ingress = SlackIngress(Concierge(), capacity=1)
        ingress.accept({"type": "event_callback", "team_id": "T1", "event": dict(
            type="app_mention", channel="C1", user="U1", ts="1700.0001", text="hi")})
        await ingress.drain()
        await ingress.close()
        return messages

    (message,) = asyncio.run(scenario())
    assert (message.origin.author, message.origin.requester) == ("U1", "slack:T1:U1")
