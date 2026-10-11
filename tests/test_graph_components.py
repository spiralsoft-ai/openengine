"""The nodes a workflow assembles, and the API it assembles them with.

Two things arrive together and are tested together, because neither is worth
much alone:

* `engine.graph_runtime_langgraph.components` -- a checkout, a human decision,
  and the ACP agent turn re-exported beside them, so a workflow uses them rather
  than reimplementing them and two workflows cannot disagree about what a
  checkout or a human decision means;
* `engine.graph_runtime_langgraph.workflows` -- `graph_workflow`, which names a
  graph without compiling it, and `sqlite_runtime`, which is the deployment half
  that owns the files compiling needs.

Driven through the runtime rather than called directly. A node here is only
meaningful under an execution -- it publishes events, raises approvals and reads
steering off the execution it is running in -- so a test that invoked
`WorkspaceNode(...)` as a function would be asserting on a mock of the very
thing that could break. Every test below starts a real run of a real compiled
graph against real SQLite files.

No ACP agent, deliberately. What an agent node *does* is
`tests/test_graph_runtime_langgraph_acp.py`, against a real process on a real
pipe; what is being checked here is the surface a workflow author writes
against, and a subprocess would only make these slower.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from engine.domain import TRIAGE_TOOL, ApprovalDecision, ApprovalKind, RunId, WorkspaceId
from engine.graph_runtime import EventLog, GraphCompilationError, RuntimeEvent, create_app
from engine.graph_runtime_langgraph import (
    GraphWorkflow,
    LangGraphRuntime,
    State,
    agent_registry,
    answer_permission,
    graph_workflow,
    sqlite_runtime,
)
from engine.graph_runtime_langgraph.components import (
    ACPNode,
    HumanReviewNode,
    NameNode,
    NoWorkingDirectoryError,
    TriageNode,
    WorkspaceNode,
    checkout,
)
from engine.graph_runtime_langgraph.components.human_review import (
    DECISION,
    NOTE,
    TOOL_NAME,
)
from engine.graph_runtime_langgraph.components.workspace import CHECKOUT
from engine.ports import Workspace, WorkspaceProvider, WorkspaceState
from langgraph.graph import END, START, StateGraph
from langgraph_acp.providers import CodexACPProvider

#: Long enough that only a genuinely stuck run reaches it. A passing run never
#: waits, but two SQLite files make this slower than an in-memory suite.
PATIENCE = 30.0

REPOSITORY = "acme/api"
TASK = "Add a greeting file."
NOTE_TEXT = "The finding can wait; ship it."


# --- a provider, without a filesystem ---------------------------------------


@dataclass
class RecordingWorkspaceProvider:
    """A `WorkspaceProvider` that hands out paths and remembers being asked.

    Implements the port rather than being a mock of it, which is the point of
    `WorkspaceNode` taking the capability: what a checkout *is* -- a worktree, a
    container, a remote sandbox -- is the deployment's, so a component that only
    worked against git would not be reusable in the sense the name claims.
    """

    provisioned: list[tuple[str, str]] = field(default_factory=list)
    co_authors: list[str] = field(default_factory=list)

    async def provision(
        self, repository: str, base_ref: str, *, co_author: str = ""
    ) -> Workspace:
        self.provisioned.append((repository, base_ref))
        self.co_authors.append(co_author)
        workspace_id = WorkspaceId(f"ws-{uuid4().hex[:8]}")
        return Workspace(
            workspace_id=workspace_id,
            root_path=f"/checkouts/{workspace_id}",
            repository=repository,
            base_ref=base_ref,
            ref=f"engine/{workspace_id}",
        )

    async def root_path(self, workspace_id: WorkspaceId) -> str:
        return f"/checkouts/{workspace_id}"

    async def state(self, workspace_id: WorkspaceId) -> WorkspaceState:
        return WorkspaceState(
            workspace_id=workspace_id,
            ref=f"engine/{workspace_id}",
            root_path=f"/checkouts/{workspace_id}",
        )

    async def attach(
        self, workspace_id: WorkspaceId, repository: str, base_ref: str,
        *, co_author: str = "",
    ) -> Workspace:
        raise NotImplementedError

    async def detach(self, workspace_id: WorkspaceId) -> None:
        raise NotImplementedError

    async def dispose(self, workspace_id: WorkspaceId) -> None:
        raise NotImplementedError


# --- graphs the tests drive --------------------------------------------------


def working_node(seen: list[str]) -> Any:
    """A stand-in for an agent node: reports where it was told to work."""

    async def works(state: dict[str, Any]) -> dict[str, Any]:
        seen.append(checkout(state))
        return {"work": "done"}

    works.graph_node_name = "Work"  # type: ignore[attr-defined]
    works.graph_node_kind = "agent"  # type: ignore[attr-defined]
    works.graph_node_description = "Does the thing."  # type: ignore[attr-defined]
    return works


def assembled(provider: WorkspaceProvider, seen: list[str]) -> GraphWorkflow:
    """The shape a workflow using these components has: check out, work, decide."""

    @graph_workflow(id="assembled", name="Assembled")
    def workflow() -> StateGraph:
        builder: StateGraph = StateGraph(State)
        builder.add_node(
            "workspace", WorkspaceNode(provider=provider, base_ref="origin/main")
        )
        builder.add_node("work", working_node(seen))
        builder.add_node("decide", HumanReviewNode())
        builder.add_edge(START, "workspace")
        builder.add_edge("workspace", "work")
        builder.add_edge("work", "decide")
        builder.add_edge("decide", END)
        return builder

    return workflow


def trivial(node_id: str = "only") -> StateGraph:
    async def node(_state: dict[str, Any]) -> dict[str, Any]:
        return {}

    builder: StateGraph = StateGraph(State)
    builder.add_node(node_id, node)
    builder.add_edge(START, node_id)
    builder.add_edge(node_id, END)
    return builder


@asynccontextmanager
async def running(
    workflows: Sequence[GraphWorkflow], tmp_path: Path
) -> AsyncIterator[tuple[LangGraphRuntime, EventLog]]:
    async with sqlite_runtime(workflows, tmp_path / "state") as runtime:
        log = EventLog()
        runtime.observe(log.append)
        yield runtime, log


async def until(
    log: EventLog, run_id: RunId, kind: str, cursor: int = 0
) -> list[RuntimeEvent]:
    """Everything up to and including the first event of `kind`."""
    seen: list[RuntimeEvent] = []
    async with asyncio.timeout(PATIENCE):
        async for event in log.stream(run_id, cursor):
            seen.append(event)
            if event.kind.value == kind:
                return seen
    return seen


# --- naming a graph ----------------------------------------------------------


def test_graph_workspace_detaches_and_reattaches_across_restart(
    tmp_path: Path, *, async_client
) -> None:
    class Provider(RecordingWorkspaceProvider):
        detached: bool = False
        restored: tuple[WorkspaceId, str, str] | None = None

        async def state(self, workspace_id: WorkspaceId) -> WorkspaceState:
            state = await super().state(workspace_id)
            return replace(state, root_path=None) if self.detached else state

        async def detach(self, workspace_id: WorkspaceId) -> None:
            self.detached = True

        async def attach(
            self, workspace_id: WorkspaceId, repository: str, base_ref: str,
            *, co_author: str = "",
        ) -> Workspace:
            self.detached = False
            self.restored = (workspace_id, repository, base_ref)
            state = await self.state(workspace_id)
            return Workspace(workspace_id, state.root_path, repository, base_ref, state.ref)

    async def scenario() -> None:
        provider = Provider()
        seen: list[str] = []
        workflow = assembled(provider, seen)
        async with running([workflow], tmp_path) as (runtime, _log):
            app = create_app(runtime)
            async with async_client(app, base_url="http://test") as client:
                run = await runtime.start(workflow.graph_id, {"repository": REPOSITORY})
                async with asyncio.timeout(PATIENCE):
                    while not (await runtime.snapshot(run.run_id)).pending_approvals:
                        await asyncio.sleep(0.01)
                endpoint = f"/api/runs/{run.run_id}/workspace"
                attached = (await client.get(endpoint)).json()
                refused = await client.delete(endpoint)
                assert refused.status_code == 409
                assert "stop" in refused.json()["error"]
                assert not provider.detached
                await runtime.cancel(run.run_id)
                history = await runtime.history(run.run_id)
                checkpoint = next(p for p in history if "work" in p.next_nodes)
                detached = await client.delete(endpoint)
                assert detached.status_code == 200
                assert detached.json()["workspaceAttached"] is False
                assert detached.json()["workspaceRef"] == attached["workspaceRef"]
                assert (await client.delete(endpoint)).status_code == 200

        async with running([workflow], tmp_path) as (runtime, _log):
            app = create_app(runtime)
            async with async_client(app, base_url="http://test") as client:
                assert (await client.get(endpoint)).json()["workspaceAttached"] is False
                refused = await client.post(
                    f"/api/runs/{run.run_id}/transitions",
                    json={"checkpoint": str(checkpoint.checkpoint_id)},
                )
                assert refused.status_code == 409
                assert "reattach" in refused.json()["error"]
                restored = await client.post(endpoint)
                assert restored.status_code == 200
                assert restored.json() == attached
                assert provider.restored == (
                    WorkspaceId(checkpoint.values["workspaceId"]), REPOSITORY, "origin/main"
                )
                assert len(provider.provisioned) == 1
                await runtime.resume_from(run.run_id, checkpoint.checkpoint_id)
                async with asyncio.timeout(PATIENCE):
                    while len(seen) < 2:
                        await asyncio.sleep(0.01)
                assert seen == [attached["workspaceRoot"]] * 2
                assert (await client.get("/api/runs/missing/workspace")).status_code == 404

    asyncio.run(scenario())


def test_a_graph_is_named_the_same_way_either_way() -> None:
    """Two spellings, because workflow files come in two shapes.

    A file describing one graph reads best as a decorator on the function that
    builds it. A file describing a *family* -- the same pipeline on each of
    several agents -- has one builder and several names for it, and a decorator
    would force the body to be written out once per variant, which is the surest
    way to end up with two that have quietly drifted apart.
    """

    @graph_workflow(id="decorated", name="Decorated")
    def decorated() -> StateGraph:
        return trivial()

    called = graph_workflow(trivial(), id="called", name="Called")

    assert (str(decorated.graph_id), decorated.name) == ("decorated", "Decorated")
    assert (str(called.graph_id), called.name) == ("called", "Called")
    # Not a running thing: compiling needs a checkpointer, and a checkpointer is
    # a file somebody has to own and close.
    assert isinstance(decorated, GraphWorkflow)
    assert isinstance(called, GraphWorkflow)


def test_a_graph_workflow_refuses_what_it_cannot_run() -> None:
    """Refused where the workflow is written, not on the first request for it."""
    with pytest.raises(ValueError, match="needs an id"):
        graph_workflow(trivial(), id="  ", name="Named")
    with pytest.raises(ValueError, match="needs a name"):
        graph_workflow(trivial(), id="named", name="")
    with pytest.raises(TypeError, match="must be built from a StateGraph"):
        graph_workflow("not a graph", id="named", name="Named")


def test_agents_answer_permission_requests_rather_than_declining_them() -> None:
    """The wiring a workflow file would otherwise get silently wrong.

    A `session/request_permission` arrives on the ACP connection rather than on
    the graph, and a provider left with the default handler *declines every
    request* instead of asking anybody -- a run that would read as an agent
    changing its mind. `agent_registry` attaches the handler that routes the
    question to the execution holding the conversation.
    """
    registry = agent_registry([CodexACPProvider()])

    assert registry.resolve("codex").permissions is answer_permission


# --- what a deployment owns --------------------------------------------------


def test_a_runtime_compiles_every_workflow_and_closes_its_files(
    tmp_path: Path,
) -> None:
    """Both files, opened together and shut together.

    Two because they answer to two owners: LangGraph's idea of where a run
    stands, and the runtime's own record of outstanding approvals and agent
    sessions. Closed on the way out, which is the reason this is a context
    manager rather than a factory -- composing them without owning their
    shutdown would leave SQLite handles open past the process that made them.
    """
    workflows = [
        graph_workflow(trivial(), id="first", name="First"),
        graph_workflow(trivial(), id="second", name="Second"),
    ]
    state = tmp_path / "state"

    async def scenario() -> tuple[list[str], list[str], LangGraphRuntime]:
        async with sqlite_runtime(workflows, state) as runtime:
            named = [str(one.graph_id) for one in runtime.graphs()]
            return named, sorted(path.name for path in state.iterdir()), runtime

    named, written, runtime = asyncio.run(scenario())

    assert named == ["first", "second"]
    assert written == ["checkpoints.sqlite3", "graph-runs.sqlite3"]
    assert runtime.running() == ()
    with pytest.raises(sqlite3.ProgrammingError):
        asyncio.run(runtime.store.runs())


def test_a_graph_that_does_not_compile_says_which_one_it_was(tmp_path: Path) -> None:
    """The one fact LangGraph's own complaint leaves out.

    "Graph must have an entrypoint" is about a node and an edge and says nothing
    about which file it came from, which is useless in a workflow directory
    holding several graphs. Wrapped so the id travels with it, and typed so a
    caller can tell a broken *definition* -- someone has to fix a file -- from a
    machine that could not open the files a graph's progress is kept in.
    """
    unreachable: StateGraph = StateGraph(State)
    unreachable.add_node("only", lambda _state: {})
    workflows = [
        graph_workflow(trivial(), id="fine", name="Fine"),
        graph_workflow(unreachable, id="broken", name="Broken"),
    ]

    async def scenario() -> None:
        async with sqlite_runtime(workflows, tmp_path / "state"):
            pass  # pragma: no cover -- compiling never gets this far

    with pytest.raises(GraphCompilationError) as raised:
        asyncio.run(scenario())

    assert str(raised.value.graph_id) == "broken"
    assert "entrypoint" in raised.value.reason
    # And the original failure is still the cause, so a traceback points at the
    # workflow file rather than stopping here.
    assert isinstance(raised.value.__cause__, ValueError)


def test_a_runtime_needs_something_to_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with sqlite_runtime((), tmp_path / "state"):
            pass

    with pytest.raises(ValueError, match="at least one workflow"):
        asyncio.run(scenario())


# --- what a client is shown --------------------------------------------------


def test_a_node_names_itself_so_a_workflow_need_not(tmp_path: Path) -> None:
    """Read off the compiled graph, which is why a name cannot go stale.

    A reusable node knows what it is called -- a checkout step is "Workspace" in
    every graph that has one -- so a workflow that assembled three of them does
    not restate all three in a second table beside them.
    """
    seen: list[str] = []
    workflow = assembled(RecordingWorkspaceProvider(), seen)

    async def scenario() -> Any:
        async with running([workflow], tmp_path) as (runtime, _log):
            return runtime.graphs()[0]

    topology = asyncio.run(scenario())

    assert [str(node.node_id) for node in topology.nodes] == [
        "workspace",
        "work",
        "decide",
    ]
    assert [node.name for node in topology.nodes] == ["Workspace", "Work", "Human review"]
    # The kinds a client draws differently: a checkout, an agent, and the one
    # stage that is a person.
    assert [node.kind for node in topology.nodes] == ["workspace", "agent", "human"]
    # And which of them a client offers as one of the run's conversations: the
    # agent alone. A checkout has nobody to talk to, and a person's own verdict
    # is made where it is presented rather than read as a transcript.
    assert [node.show_in_sidebar for node in topology.nodes] == [False, True, False]
    assert str(topology.entry_point) == "workspace"


def test_a_graph_may_override_what_a_node_calls_itself(tmp_path: Path) -> None:
    """For somebody else's node, or one this graph uses differently."""
    workflow = graph_workflow(
        trivial("only"), id="renamed", name="Renamed", names={"only": "The one step"}
    )

    async def scenario() -> Any:
        async with running([workflow], tmp_path) as (runtime, _log):
            return runtime.graphs()[0]

    topology = asyncio.run(scenario())

    assert [node.name for node in topology.nodes] == ["The one step"]
    # A node that says nothing about itself is a plain node, which is the
    # truthful answer for a graph this package did not write. It is offered as
    # one of the run's conversations for the same reason: leaving somebody
    # else's node out of the one navigation a person has would hide the run.
    assert [node.kind for node in topology.nodes] == ["node"]
    assert [node.show_in_sidebar for node in topology.nodes] == [True]


# --- the components, under a run ---------------------------------------------


def test_a_workspace_node_checks_out_where_the_next_node_works(
    tmp_path: Path,
) -> None:
    """Provisioning is a node, so it is a position a run stands at.

    Which is the whole reason it is one: checking out clones, fetches and writes
    to disk, so it fails the way real work fails. As a node that failure is
    reported on the run's own feed; done before a run existed it would be a
    request that hung and then 500'd, with nothing left to look at.

    The path goes into the graph's state rather than into the node, so one
    compiled graph serves every run -- and `checkout` is how the node
    downstream reads it.
    """
    provider = RecordingWorkspaceProvider()
    seen: list[str] = []
    workflow = assembled(provider, seen)

    async def scenario() -> tuple[Any, list[RuntimeEvent]]:
        async with running([workflow], tmp_path) as (runtime, log):
            run = await runtime.start(
                workflow.graph_id,
                {"task": TASK, "repository": REPOSITORY, "coAuthor": "Ada <ada@example.test>"},
            )
            events = await until(log, run.run_id, "approval.requested")
            return await runtime.snapshot(run.run_id), events

    snapshot, events = asyncio.run(scenario())

    assert provider.provisioned == [(REPOSITORY, "origin/main")]
    assert provider.co_authors == ["Ada <ada@example.test>"]
    assert snapshot.values[CHECKOUT].startswith("/checkouts/ws-")
    assert snapshot.values["workspaceRef"].startswith("engine/ws-")
    # The node downstream worked in the checkout this run was given, not in one
    # named when the graph was written.
    assert seen == [snapshot.values[CHECKOUT]]
    # Reported as a call rather than only as prose: a checkout is a thing that
    # happened to the world, and a client should be able to render it beside an
    # agent's own calls rather than parse a sentence.
    calls = [event for event in events if event.kind.value == "tool.call"]
    assert [event.payload["name"] for event in calls] == ["provision_workspace"]
    assert calls[0].payload["arguments"] == {
        "repository": REPOSITORY,
        "baseRef": "origin/main",
    }


def test_state_keeps_what_earlier_nodes_reported(tmp_path: Path) -> None:
    """A node reporting one key does not erase the checkout the rest need."""
    provider = RecordingWorkspaceProvider()
    seen: list[str] = []
    workflow = assembled(provider, seen)

    async def scenario() -> Any:
        async with running([workflow], tmp_path) as (runtime, log):
            run = await runtime.start(workflow.graph_id, {"task": TASK})
            await until(log, run.run_id, "approval.requested")
            return await runtime.snapshot(run.run_id)

    values = asyncio.run(scenario()).values

    assert values["task"] == TASK
    assert values["work"] == "done"
    assert CHECKOUT in values


@pytest.mark.parametrize(
    ("response", "expected_name"),
    [
        (None, None),
        (
            '{"name": "  #301 Add GitHub auth sessions  "}',
            "#301 Add GitHub auth sessions",
        ),
        (
            'I’ll read issue #301 to identify the name.\n'
            '{"name": "#301 Add GitHub auth sessions"}',
            "#301 Add GitHub auth sessions",
        ),
        ('{"name": "Fix {auth} sessions"}', "Fix {auth} sessions"),
        ("I’ll read issue #301 to identify the name.", None),
        ('{"name": "unfinished', None),
        ('{"title": "Wrong field"}', None),
        ('{"name": 301}', None),
        ('{"name": null}', None),
        ('{"name": "  "}', None),
        ('[{"name": "Not an object"}]', None),
        ('{"name": "A name"}\nTrailing narration', None),
    ],
)
def test_naming_reads_a_json_name_and_always_allows_implementation_to_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: str | None,
    expected_name: str | None,
) -> None:
    """Narration is not a name, and invalid metadata cannot fail the work."""
    started: list[str] = []

    async def cannot_open(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("naming provider unavailable")

    async def answer(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"name": response}

    async def implement(_state: dict[str, Any]) -> dict[str, Any]:
        started.append("implementation")
        return {"implementation": "done"}

    if response is None:
        monkeypatch.setattr(ACPNode, "_open", cannot_open)
    else:
        monkeypatch.setattr(ACPNode, "__call__", answer)
    builder: StateGraph = StateGraph(State)
    builder.add_node("naming", NameNode(agent="codex", cwd=str(tmp_path)))
    builder.add_node("implementation", implement)
    builder.add_edge(START, "naming")
    builder.add_edge("naming", "implementation")
    builder.add_edge("implementation", END)
    workflow = graph_workflow(builder, id="best-effort-name", name="Best effort")

    async def scenario() -> Any:
        async with running([workflow], tmp_path) as (runtime, log):
            run = await runtime.start(workflow.graph_id, {"task": TASK})
            await until(log, run.run_id, "run.finished")
            return await runtime.snapshot(run.run_id)

    finished = asyncio.run(scenario())

    assert finished.status.value == "completed"
    assert finished.error == ""
    assert finished.values["implementation"] == "done"
    if expected_name is None:
        assert "name" not in finished.values
    else:
        assert finished.values["name"] == expected_name
    assert started == ["implementation"]


def test_a_human_review_node_waits_for_a_person_and_records_the_verdict(
    tmp_path: Path,
) -> None:
    """Stop, and wait, without interrupting the graph.

    An approval rather than an interrupt, so the node that raised it is still
    running while a person thinks -- and the question is written down before the
    wait, so a process that died here would leave one somebody could still
    answer.

    The note arrives as *steering*, which is the channel for saying something to
    an execution rather than about a graph's shape. The decision endpoint
    carries a decision and nothing else, so a client says the words first and
    the verdict second.
    """
    provider = RecordingWorkspaceProvider()
    seen: list[str] = []
    workflow = assembled(provider, seen)

    async def scenario() -> tuple[Any, Any, list[RuntimeEvent]]:
        async with running([workflow], tmp_path) as (runtime, log):
            run = await runtime.start(workflow.graph_id, {"task": TASK})
            events = await until(log, run.run_id, "approval.requested")
            waiting = await runtime.snapshot(run.run_id)
            asked = waiting.pending_approvals[0]
            await runtime.steer(run.run_id, NOTE_TEXT, execution_id=asked.execution_id)
            await runtime.decide(run.run_id, asked.approval_id, ApprovalDecision.ACCEPT)
            await until(log, run.run_id, "run.finished", cursor=len(events))
            return waiting, await runtime.snapshot(run.run_id), events

    waiting, finished, _events = asyncio.run(scenario())

    asked = waiting.pending_approvals[0]
    assert str(asked.node_id) == "decide"
    assert asked.kind is ApprovalKind.USER_INPUT
    # Raised under its own tool name, so a client can tell the one approval that
    # is a person's verdict from the ones that are an agent asking permission.
    assert asked.tool_name == TOOL_NAME
    assert asked.reason == "approval of this run"

    assert finished.status.value == "completed"
    assert finished.values[DECISION] == "approved"
    assert finished.values[NOTE] == NOTE_TEXT


def test_a_rejected_run_ends(tmp_path: Path) -> None:
    """Refusing ends the run the way refusing any other request does."""
    provider = RecordingWorkspaceProvider()
    seen: list[str] = []
    workflow = assembled(provider, seen)

    async def scenario() -> Any:
        async with running([workflow], tmp_path) as (runtime, log):
            run = await runtime.start(workflow.graph_id, {"task": TASK})
            events = await until(log, run.run_id, "approval.requested")
            asked = (await runtime.snapshot(run.run_id)).pending_approvals[0]
            await runtime.decide(run.run_id, asked.approval_id, ApprovalDecision.CANCEL)
            await until(log, run.run_id, "run.failed", cursor=len(events))
            return await runtime.snapshot(run.run_id)

    refused = asyncio.run(scenario())

    assert refused.status.value == "failed"
    # Worded from the request, so the refusal a client shows is a sentence.
    assert refused.error == "approval of this run was not allowed"


FINDINGS = [
    {"tagline": "The greeting is misspelled", "description": "It says helo.", "agent": "codex", "facet": "bugs"},
    {"tagline": "The file has no newline", "description": "Editors complain.", "agent": "codex", "facet": "conciseness"},
]


def triaged(tmp_path: Path, answer) -> Any:
    """Run a checkout and a triage over `FINDINGS`, answering it with `answer`."""
    provider = RecordingWorkspaceProvider()

    @graph_workflow(id="triaged", name="Triaged")
    def workflow() -> StateGraph:
        builder: StateGraph = StateGraph(State)
        builder.add_node(
            "workspace",
            WorkspaceNode(provider=provider, base_ref="origin/main", ref_input="ref"),
        )
        builder.add_node("triage", TriageNode(findings_key="review"))
        builder.add_edge(START, "workspace")
        builder.add_edge("workspace", "triage")
        builder.add_edge("triage", END)
        return builder

    async def scenario() -> tuple[Any, Any]:
        async with running([workflow], tmp_path) as (runtime, log):
            run = await runtime.start(workflow.graph_id, {
                "task": TASK, "review": FINDINGS, "inputs": {"ref": "origin/feature"},
            })
            events = await until(log, run.run_id, "approval.requested")
            asked = (await runtime.snapshot(run.run_id)).pending_approvals[0]
            await answer(runtime, run.run_id, asked)
            await until(log, run.run_id, "run.finished", cursor=len(events))
            return asked, await runtime.snapshot(run.run_id)

    asked, finished = asyncio.run(scenario())
    assert provider.provisioned == [(".", "origin/feature")]
    assert asked.kind is ApprovalKind.USER_INPUT
    assert asked.tool_name == TRIAGE_TOOL
    # Finishing without a fix is a finished review, not a refused run.
    assert finished.status.value == "completed"
    return finished.values["fix"]


def test_triage_fixes_the_findings_a_person_steers_to_it(tmp_path: Path) -> None:
    async def choose_second(runtime, run_id, asked):
        await runtime.steer(run_id, json.dumps([FINDINGS[1]]), execution_id=asked.execution_id)
        await runtime.decide(run_id, asked.approval_id, ApprovalDecision.ACCEPT)

    assert triaged(tmp_path, choose_second) == [FINDINGS[1]]


def test_triage_accepted_with_nothing_chosen_fixes_every_finding(tmp_path: Path) -> None:
    async def accept(runtime, run_id, asked):
        await runtime.decide(run_id, asked.approval_id, ApprovalDecision.ACCEPT)

    assert triaged(tmp_path, accept) == FINDINGS


def test_triage_cancelled_fixes_nothing_and_finishes(tmp_path: Path) -> None:
    async def finish(runtime, run_id, asked):
        await runtime.decide(run_id, asked.approval_id, ApprovalDecision.CANCEL)

    assert triaged(tmp_path, finish) == []


# --- never the server's own directory ----------------------------------------
#
# One hazard, three ways in, and the reason all of them are refusals rather than
# defaults: ACP resolves an absent working directory against the *client's*
# process, so a node that reached a session with nothing would open one in the
# server's own checkout. In a normal deployment that is the operator's live
# repository, and an agent with permission to edit would start editing it --
# with nothing in the run saying so, because there is no event for "started
# somewhere unintended" and the transcript reads the same either way.


def test_a_node_cannot_be_written_without_saying_where_it_works() -> None:
    """The loudest of the three: `cwd` has no default, so there is nothing
    to forget -- caught by a type checker as well as at the `add_node` line."""
    with pytest.raises(TypeError, match="cwd"):
        ACPNode(agent="codex", prompt="...")  # type: ignore[call-arg]


def test_an_empty_working_directory_is_refused_where_it_is_written() -> None:
    """`cwd=""` is the same accident spelled differently.

    Worth its own refusal because it is what a half-finished interpolation
    produces -- `cwd=os.environ.get("CHECKOUT", "")` -- and because ACP would
    resolve it against the process exactly as it resolves an absent one.
    """
    with pytest.raises(NoWorkingDirectoryError, match="empty working directory"):
        ACPNode(agent="codex", prompt="...", cwd="   ")


def test_a_run_with_no_checkout_fails_rather_than_naming_one(tmp_path: Path) -> None:
    """`checkout` refuses; it has no `None` to hand anybody.

    Driven through a graph whose `WorkspaceNode` is missing, because that is how
    this arrives in practice: a workflow somebody else assembled, a node ordered
    before the one that provisions, or a fork re-entering from a position taken
    before anything had been checked out. The run fails, and the failure says
    which node was supposed to have run.
    """
    seen: list[str] = []

    @graph_workflow(id="unprovisioned", name="Unprovisioned")
    def workflow() -> StateGraph:
        async def works(state: dict[str, Any]) -> dict[str, Any]:
            seen.append(checkout(state))
            return {}

        builder: StateGraph = StateGraph(State)
        builder.add_node("work", works)
        builder.add_edge(START, "work")
        builder.add_edge("work", END)
        return builder

    async def scenario() -> Any:
        async with running([workflow], tmp_path) as (runtime, log):
            run = await runtime.start(workflow.graph_id, {"task": TASK})
            await until(log, run.run_id, "run.failed")
            return await runtime.snapshot(run.run_id)

    failed = asyncio.run(scenario())

    assert failed.status.value == "failed"
    assert "no checkout to work in" in failed.error
    assert "WorkspaceNode" in failed.error
    # Nothing was handed a directory, correct or otherwise.
    assert seen == []


def test_a_provider_that_answers_with_no_path_is_refused(tmp_path: Path) -> None:
    """Caught at the provider rather than three nodes later.

    A `WorkspaceProvider` is somebody else's implementation and an empty path is
    a plausible thing for one to return. Left unchecked it would reach state,
    and the complaint would come from whichever node could not work anywhere
    rather than from the thing that actually went wrong.
    """

    class Pathless(RecordingWorkspaceProvider):
        async def provision(
            self, repository: str, base_ref: str, *, co_author: str = ""
        ) -> Workspace:
            given = await super().provision(repository, base_ref, co_author=co_author)
            return replace(given, root_path="")

    seen: list[str] = []
    workflow = assembled(Pathless(), seen)

    async def scenario() -> Any:
        async with running([workflow], tmp_path) as (runtime, log):
            run = await runtime.start(workflow.graph_id, {"task": TASK})
            await until(log, run.run_id, "run.failed")
            return await runtime.snapshot(run.run_id)

    failed = asyncio.run(scenario())

    assert failed.status.value == "failed"
    assert "Pathless provisioned a workspace with no path" in failed.error
    assert CHECKOUT not in failed.values
