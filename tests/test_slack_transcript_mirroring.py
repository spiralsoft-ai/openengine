"""The UI transcript is the sole source of Slack agent progress/reports."""

import asyncio
import json
from contextlib import asynccontextmanager
from html import escape

import pytest
from langgraph.graph import END, START, StateGraph

from engine.domain import RunPhase
from engine.graph_runtime import EventKind, RunStatus
from engine.graph_runtime_langgraph import State, graph_workflow
from engine.graph_runtime_langgraph.executions import current_execution
from engine.graph_runtime_langgraph.workflows import sqlite_runtime
from engine.runtime import WorkflowCatalog, WorkOrdersConfig
from test_slack_work_orders import _signed
from provider_fakes import FakeACPProvider
from web_fakes import RecordingCommunications


@pytest.mark.parametrize("ending", ["finished", "failed"])
@pytest.mark.parametrize("origin", ["slack", "web"])
@pytest.mark.parametrize("fail_post", [False, True])
@pytest.mark.parametrize("before_row", [False, True])
@pytest.mark.parametrize("blank_report", [False, True])
def test_agent_transcript_is_mirrored(
    tmp_path, origin, fail_post, before_row, ending, blank_report, *, slack_app, client
):
    texts = (
        [" \n"] if blank_report
        else ["Inspecting the code.", "Checking <@UOTHER> & [REDACTED].",
              '{"name":"Update quickstart with oe installation instructions"}',
              "Final report: done."]
    )

    async def agent(state):
        execution = current_execution()
        await execution.say("hidden system prompt", role="system")
        await execution.say("user instructions", role="user")
        await execution.say(texts[0])
        await execution.tool("shell-1", "terminal", {"command": "private command"}, "private output")
        for text in texts[1:]:
            await execution.say(text)
        if ending == "failed":
            raise RuntimeError("unexpected service failure")
        return {}

    agent.graph_node_kind = "agent"
    builder = StateGraph(State)
    builder.add_node("agent", agent)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    graph = graph_workflow(builder, id="mirror-test", name="Mirror test")

    @asynccontextmanager
    async def running():
        async with sqlite_runtime((graph,), tmp_path / "graph") as runtime:
            start = runtime.start

            async def start_and_wait(*args, **kwargs):
                snapshot = await start(*args, **kwargs)
                async with asyncio.timeout(10):
                    while (await runtime.snapshot(snapshot.run_id)).status is RunStatus.RUNNING:
                        await asyncio.sleep(0.01)
                return await runtime.snapshot(snapshot.run_id)

            if before_row:
                runtime.start = start_and_wait
            yield runtime

    class Communications(RecordingCommunications):
        async def post(self, channel, message, run_id=None, thread_id=""):
            if fail_post and message.text == texts[0]:
                raise RuntimeError("secret-token private request body")
            return await super().post(channel, message, run_id, thread_id)

    communications = Communications()
    app, capabilities, _ = slack_app(
        communications,
        WorkOrdersConfig(repository="acme/api", workflow="mirror-test"),
        WorkflowCatalog.from_graphs((graph,)),
        provider=FakeACPProvider(create=True),
        graph_runtime=running(),
    )
    with client(app) as browser:
        if origin == "slack":
            body = json.dumps({"type": "event_callback", "event": {
                "type": "app_mention", "channel": "CSOURCE", "user": "UREQUESTER",
                "ts": "2", "thread_ts": "1", "text": "<@BOT> new workorder please",
            }}).encode()
            assert browser.post("/api/slack/events", content=body, headers=_signed(body)).status_code == 200
            browser.portal.call(app.state.slack_ingress.drain)
        else:
            response = browser.post("/api/runs", json={
                "workflowId": "mirror-test", "prompt": "Implement it", "repository": "acme/api",
            })
            assert response.status_code < 300, response.text
        runs = browser.portal.call(capabilities.state_store.list_runs)
        assert len(runs) == 1
        run_id = runs[0].run_id

        async def finished():
            async with asyncio.timeout(10):
                while True:
                    state = await capabilities.state_store.load(run_id)
                    if state.phase in (RunPhase.SUCCEEDED, RunPhase.FAILED):
                        return state
                    await asyncio.sleep(0.01)

        state = browser.portal.call(finished)
        assert state.phase is (RunPhase.SUCCEEDED if ending == "finished" else RunPhase.FAILED)
        events = browser.get(f"/api/runs/{run_id}/graph-events").json()["events"]
        diagnostics = [e for e in events if e["type"] == EventKind.NOTIFICATION_FAILED.value]
        assert len(diagnostics) == int(origin == "slack" and fail_post and not blank_report)
        assert "secret-token" not in str(events)
        assert "private request body" not in str(events)
        if diagnostics:
            assert diagnostics[0]["payload"] == {
                "error": "Slack notification could not be delivered.", "eventKind": "transcript",
            }
        visible = [e["payload"]["text"] for e in events
                   if e["type"] == "transcript" and e["payload"].get("role") == "assistant"]
        assert visible == texts

    if origin == "web":
        assert communications.posts == []
        return
    assert (state.origin.channel, state.origin.thread_id, state.origin.author) == (
        "CSOURCE", "1", "UREQUESTER",
    )
    posted = [m.text for _, m, _ in communications.posts]
    expected = [
        ("*name*\n\tUpdate quickstart with oe installation instructions"
         if text.startswith('{"name":') else escape(text, quote=False))
        for text in texts[1 if fail_post else 0:]
        if text.strip()
    ]
    assert [text for text in posted if text in expected] == expected
    assert all(not message.progress for _, message, _ in communications.posts
               if message.text in expected)
    assert all(not message.links for _, message, _ in communications.posts
               if message.text in expected or message.text == "*agent* started.")
    assert posted.count("Work order finished.") == int(ending == "finished")
    work_order_links = [
        (message, link)
        for _, message, _ in communications.posts
        for link in message.links
        if link.label == "View work order"
    ]
    assert len(work_order_links) == 1
    completion, link = work_order_links[0]
    assert completion.text == (
        "Work order finished." if ending == "finished"
        else "Work order failed: unexpected service failure"
    )
    assert link.url.endswith(f"/runs/{run_id}")
    assert posted.count("Work order failed: unexpected service failure") == int(ending == "failed")
    assert posted.count("*agent* started.") == 1
    assert not any(word in text for text in posted for word in (
        "private command", "private output", "hidden system prompt", "user instructions",
    ))
    assert all((channel, thread) == ("CSOURCE", "1") for channel, _, thread in communications.posts)


@pytest.mark.parametrize("decision", ["accept", "cancel"])
@pytest.mark.parametrize("agent_report", [False, True])
def test_human_review_slack_sequence(
    tmp_path, decision, agent_report, *, slack_app, client
):
    from engine.graph_runtime_langgraph.components import HumanReviewNode

    class Agent:
        graph_node_kind = "agent"

        async def __call__(self, state):
            if agent_report:
                await current_execution().say("Final report: ready for review.")
            return {}

    builder = StateGraph(State)
    builder.add_node("agent", Agent())
    builder.add_node("decision", HumanReviewNode())
    builder.add_edge(START, "agent")
    builder.add_edge("agent", "decision")
    builder.add_edge("decision", END)
    graph = graph_workflow(builder, id="review-mirror-test", name="Review mirror test")
    communications = RecordingCommunications()
    provider = FakeACPProvider(create=True, text="Created the work order.")
    app, capabilities, _ = slack_app(
        communications,
        WorkOrdersConfig(repository="acme/api", workflow=graph.graph_id),
        WorkflowCatalog.from_graphs((graph,)),
        provider=provider,
        graph_runtime=sqlite_runtime((graph,), tmp_path / "graph"),
    )
    with client(app) as browser:
        body = json.dumps({"type": "event_callback", "event": {
            "type": "app_mention", "channel": "CSOURCE", "user": "UREQUESTER",
            "ts": "2", "thread_ts": "1", "text": "<@BOT> new workorder please",
        }}).encode()
        assert browser.post("/api/slack/events", content=body, headers=_signed(body)).status_code == 200
        browser.portal.call(app.state.slack_ingress.drain)
        runs = browser.portal.call(capabilities.state_store.list_runs)
        assert len(runs) == 1
        run_id = runs[0].run_id

        async def wait_for_phase(phase):
            async with asyncio.timeout(10):
                while (await capabilities.state_store.load(run_id)).phase is not phase:
                    await asyncio.sleep(0.01)

        async def wait_for_review():
            async with asyncio.timeout(10):
                while not any(m.text == "Review complete and ready for your decision."
                              for _, m, _ in communications.posts):
                    await asyncio.sleep(0.01)

        browser.portal.call(wait_for_review)
        events = browser.get(f"/api/runs/{run_id}/graph-events").json()["events"]
        approval = next(e for e in events if e["type"] == "approval.requested")
        response = browser.post(
            f"/graph/api/runs/{run_id}/approvals/{approval['payload']['approvalId']}",
            json={"decision": decision},
        )
        assert response.status_code < 300, response.text
        browser.portal.call(wait_for_phase, RunPhase.SUCCEEDED if decision == "accept" else RunPhase.FAILED)
        events = browser.get(f"/api/runs/{run_id}/graph-events").json()["events"]
        # Internal narration remains available in OE but cannot count as a report.
        transcripts = [e["payload"]["text"] for e in events if e["type"] == "transcript"]
        assert HumanReviewNode().prompt in transcripts
        assert f"Recorded: {'approved' if decision == 'accept' else 'rejected'}." in transcripts

    expected = ["Created the work order.", "*agent* started."]
    if agent_report:
        expected.append("Final report: ready for review.")
    expected.extend(["*Human review* started.", "Review complete and ready for your decision."])
    if decision == "cancel":
        expected.append("Work order failed: approval of this run was not allowed")
    else:
        expected.append("Work order finished.")
    assert [message.text for _, message, _ in communications.posts] == expected
    assert all((channel, thread) == ("CSOURCE", "1") for channel, _, thread in communications.posts)
    review_message = next(m for _, m, _ in communications.posts
                          if m.text == "Review complete and ready for your decision.")
    assert review_message.mention == "UREQUESTER"

    assert any(link.label == "View work order" for link in review_message.links)


@pytest.mark.parametrize(("text", "expected"), [
    ('{"name":"Quickstart","summary":"Install oe"}',
     "*name*\n\tQuickstart\n*summary*\n\tInstall oe"),
    ('{"result":{"ok":true},"count":2,"empty":null}',
     '*result*\n\t{"ok": true}\n*count*\n\t2\n*empty*\n\tnull'),
    ('{"<key>":"<@UOTHER> & [REDACTED]"}',
     "*&lt;key&gt;*\n\t&lt;@UOTHER&gt; &amp; [REDACTED]"),
    ("Plain text <example>", "Plain text &lt;example&gt;"),
    ('{"incomplete":', '{"incomplete":'),
    ('["one", "two"]', '["one", "two"]'),
    ('{}', '{}'),
])
def test_slack_task_report(text, expected):
    from engine.apps.web.api import _slack_task_report

    assert _slack_task_report(text) == expected
