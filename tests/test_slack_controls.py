"""Slack controls the graph WorkOrder, including when no agent is running."""
import json
from dataclasses import replace

import pytest

from engine.domain import ApprovalDecision, ApprovalKind, RunId, RunOrigin, RunState, TaskId, WorkflowId
from engine.graph_runtime import RunStatus
from engine.runtime import WorkOrdersConfig
from test_github_concierge import _graph_runtime, _human_review
from test_slack_work_orders import _signed, _workflow_catalog
from provider_fakes import FakeACPProvider, call_mcp
from web_fakes import RecordingCommunications


class ControlProvider(FakeACPProvider):
    def __init__(self, tool, arguments):
        super().__init__()
        self.tool, self.arguments = tool, arguments

    async def connect(self):
        client = await super().connect()
        original = client.prompt

        async def prompt(text):
            client.result = await call_mcp(client.config, self.tool, arguments=self.arguments)
            async for event in original(text):
                yield event

        client.prompt = prompt
        return client


def send(client, app, text, author="U", ts="2"):
    body = json.dumps({"type": "event_callback", "event": {
        "type": "message", "channel": "C", "thread_ts": "1", "ts": ts,
        "user": author, "text": text,
    }}).encode()
    assert client.post("/api/slack/events", content=body, headers=_signed(body)).status_code == 200
    client.portal.call(app.state.slack_ingress.drain)


def saved_run(run_id="existing", author="U"):
    return RunState(
        run_id=RunId(run_id), task_id=TaskId("task"), workflow_id=WorkflowId("implementation-review-v1"),
        origin=RunOrigin(channel="C", thread_id="1", author=author), prompt="Fix the app",
    )


@pytest.mark.parametrize("action,status,pending,arguments", [
    ("steer_workorder", RunStatus.RUNNING, (), {"prompt": "Use the system theme"}),
    ("resume_workorder", RunStatus.COMPLETED, (), {"prompt": "Browser tests fail"}),
    ("resume_workorder", RunStatus.FAILED, (), {"prompt": "Try again"}),
    ("decide_workorder_review", RunStatus.RUNNING, (_human_review(),), {"approved": True}),
    ("decide_workorder_review", RunStatus.RUNNING, (_human_review(),),
     {"approved": False, "summary": "The login button is broken"}),
    ("answer_workorder_question", RunStatus.RUNNING,
     (replace(_human_review(tool_name="question"), reason="Which API?"),),
     {"approval_id": "approval-1", "answers": {"reply": ["Public API"]}}),
])
@pytest.mark.parametrize("author", ["U", "OPERATOR", "STRANGER"])
def test_slack_controls_graph_workorder(
    action, status, pending, arguments, author, *, slack_app, client
):
    runtime, opened = _graph_runtime(status=status, pending_approvals=pending)
    provider = ControlProvider(action, arguments)
    app, capabilities, _ = slack_app(
        RecordingCommunications(),
        WorkOrdersConfig(slack_operators=("OPERATOR",)),
        _workflow_catalog(),
        provider=provider,
        graph_runtime=opened,
    )
    with client(app) as browser:
        browser.portal.call(capabilities.state_store.save, saved_run())
        send(browser, app, "Please handle this", author)
        result = provider.clients[0].result
        if author == "STRANGER":
            assert result["isError"]
            runtime.steer.assert_not_awaited()
            runtime.decide.assert_not_awaited()
        else:
            assert not result.get("isError"), result
            assert result["structuredContent"]["run_id"] == "existing"
            if action == "decide_workorder_review" and arguments["approved"]:
                runtime.decide.assert_awaited_once_with(RunId("existing"), pending[0].approval_id, ApprovalDecision.ACCEPT)
            else:
                assert runtime.steer.await_args.args[0] == RunId("existing")
                if action == "answer_workorder_question":
                    assert runtime.steer.await_args.kwargs == {"execution_id": pending[0].execution_id}
                    assert "Public API" in runtime.steer.await_args.args[1]
                    runtime.decide.assert_awaited_once()
                else:
                    assert runtime.steer.await_args.kwargs == {"node_id": "implementation"}
            if action == "decide_workorder_review" and not arguments["approved"]:
                assert "requested changes" in runtime.steer.await_args.args[1]
                assert arguments["summary"] in runtime.steer.await_args.args[1]
                assert "implementation resumed" in result["content"][0]["text"]
            assert len(browser.portal.call(capabilities.state_store.list_runs)) == 1
        runtime.start.assert_not_awaited()


@pytest.mark.parametrize("action", ["steer_workorder", "resume_workorder"])
def test_pending_question_cannot_be_bypassed(action, *, slack_app, client):
    runtime, opened = _graph_runtime(pending_approvals=(_human_review(tool_name="question"),))
    provider = ControlProvider(action, {"prompt": "Do it"})
    app, capabilities, _ = slack_app(
        RecordingCommunications(),
        WorkOrdersConfig(),
        _workflow_catalog(),
        provider=provider,
        graph_runtime=opened,
    )
    with client(app) as browser:
        browser.portal.call(capabilities.state_store.save, saved_run())
        send(browser, app, "Do it")
        assert provider.clients[0].result["isError"]
        runtime.steer.assert_not_awaited()
        runtime.decide.assert_not_awaited()


def test_multiple_workorders_are_selected_in_slack_per_sender(*, slack_app, client):
    runtime, opened = _graph_runtime()
    provider = ControlProvider("steer_workorder", {"prompt": "Fix it"})
    communications = RecordingCommunications()
    app, capabilities, _ = slack_app(
        communications,
        WorkOrdersConfig(),
        _workflow_catalog(),
        provider=provider,
        graph_runtime=opened,
    )
    with client(app) as browser:
        for run in (
            replace(saved_run("first"), name='Ask <@U123> & keep "quotes"'),
            replace(saved_run("second"), prompt="Fix <!channel> & <@U456>"),
        ):
            browser.portal.call(capabilities.state_store.save, run)
        send(browser, app, "Fix it")
        assert not provider.clients
        assert "Which WorkOrder" in communications.posts[-1][1].text
        assert '`first` (Ask &lt;@U123&gt; &amp; keep "quotes")' in communications.posts[-1][1].text
        assert "`second` (Fix &lt;!channel&gt; &amp; &lt;@U456&gt;)" in communications.posts[-1][1].text
        send(browser, app, "second", ts="3")
        assert "Selected WorkOrder" in communications.posts[-1][1].text
        send(browser, app, "Fix it", ts="4")
        assert runtime.steer.await_args.args[0] == "second"
        send(browser, app, "Fix it", author="STRANGER", ts="5")
        assert "Which WorkOrder" in communications.posts[-1][1].text
        assert runtime.steer.await_count == 1


@pytest.mark.parametrize("ending", ["completed", "failed", "cancelled", "review"])
def test_followup_reenters_same_real_graph(tmp_path, ending, *, slack_app, client):
    import asyncio
    from contextlib import asynccontextmanager
    from engine.graph_runtime import GraphId
    from engine.graph_runtime_langgraph import State, graph_workflow
    from engine.graph_runtime_langgraph.components import HumanReviewNode
    from engine.graph_runtime_langgraph.executions import current_execution
    from engine.graph_runtime_langgraph.workflows import sqlite_runtime
    from engine.runtime import WorkflowCatalog
    from langgraph.graph import END, START, StateGraph

    received = []

    class Implementation:
        graph_node_always_open = True

        async def __call__(self, state):
            received.append(current_execution().pending_messages())
            if ending == "failed" and len(received) == 1:
                raise RuntimeError("Failed the first attempt")
            if ending == "cancelled" and len(received) == 1:
                await asyncio.Event().wait()
            return {}

    builder = StateGraph(State)
    builder.add_node("implementation", Implementation())
    builder.add_edge(START, "implementation")
    if ending == "review":
        builder.add_node("review", HumanReviewNode())
        builder.add_edge("implementation", "review")
        builder.add_edge("review", END)
    else:
        builder.add_edge("implementation", END)
    graph = graph_workflow(builder, id="implementation-review-v1", name="Test")
    runtimes = []

    @asynccontextmanager
    async def opened():
        async with sqlite_runtime((graph,), tmp_path / "graph") as runtime:
            runtimes.append(runtime)
            yield runtime

    async def settle(runtime, predicate):
        async with asyncio.timeout(10):
            while not predicate(await runtime.snapshot(RunId("existing"))):
                await asyncio.sleep(.01)

    provider = ControlProvider(
        "decide_workorder_review" if ending == "review" else "resume_workorder",
        {"approved": False, "summary": "Fix the broken login"} if ending == "review"
        else {"prompt": "Fix the broken login"},
    )
    app, capabilities, _ = slack_app(
        RecordingCommunications(),
        WorkOrdersConfig(),
        WorkflowCatalog.from_graphs((graph,)),
        provider=provider,
        graph_runtime=opened(),
    )
    with client(app) as browser:
        runtime = runtimes[0]
        browser.portal.call(capabilities.state_store.save, saved_run())

        async def start():
            await runtime.start(GraphId("implementation-review-v1"), {}, run_id=RunId("existing"))
            if ending == "cancelled":
                async with asyncio.timeout(10):
                    while not received:
                        await asyncio.sleep(.01)
                await runtime.cancel(RunId("existing"))
            await settle(runtime, lambda s: bool(s.pending_approvals) if ending == "review"
                         else s.status in (RunStatus.COMPLETED, RunStatus.FAILED))

        browser.portal.call(start)
        send(browser, app, "Fix the broken login")
        assert not provider.clients[0].result.get("isError"), provider.clients[0].result
        browser.portal.call(settle, runtime, lambda s: len(received) == 2)
        assert "Fix the broken login" in received[1][0]
        assert len(browser.portal.call(capabilities.state_store.list_runs)) == 1


@pytest.mark.parametrize("pending,approval_id", [
    ((), "stale"),
    ((_human_review(),), "approval-1"),
    ((replace(_human_review(tool_name="bash"), kind=ApprovalKind.COMMAND_EXECUTION),), "approval-1"),
])
def test_answers_cannot_approve_reviews_permissions_or_stale_questions(
    pending, approval_id, *, slack_app, client
):
    runtime, opened = _graph_runtime(pending_approvals=pending)
    provider = ControlProvider("answer_workorder_question", {
        "approval_id": approval_id, "answers": {"reply": ["yes"]},
    })
    app, capabilities, _ = slack_app(
        RecordingCommunications(),
        WorkOrdersConfig(),
        _workflow_catalog(),
        provider=provider,
        graph_runtime=opened,
    )
    with client(app) as browser:
        browser.portal.call(capabilities.state_store.save, saved_run())
        send(browser, app, "yes")
        assert provider.clients[0].result["isError"]
        runtime.steer.assert_not_awaited()
        runtime.decide.assert_not_awaited()
