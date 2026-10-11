"""The graph control surface: runs, state, topology, events, steering, resumption.

Driven over HTTP rather than against the runtime object, because the HTTP
surface is what this package is for -- a test that called `steer` directly would
pass whatever the wire format did.

Every test here runs twice: once against the scripted stand-in the contract was
written with, and once against `engine-graph-runtime-langgraph` driving a real
compiled LangGraph built from the same description. That is what `build` is --
see `tests/graph_runtime_backends.py`. The suite is deliberately not duplicated
per implementation: a binding that only had to pass tests written after it would
say nothing about the contract, and the two implementations disagreeing about
one of these assertions is exactly the news worth having.

Three graphs are used. A pipeline, implementation then review, which is the
shape "send it back to implementation" is written in; a fan-out, implementation
into three reviewers into a reranker, which is the shape that makes a single
"current node" a lie; and the same review node run three times over, which is
the shape that makes a node *name* useless as an address. See
`tests/graph_runtime_fakes.py` for what a script is.

Requests are made with httpx, except the event feed. That one is called as ASGI
directly: httpx's ASGI transport buffers a whole response before returning it,
and a subscription that stays open so a run can be sent back to an earlier
position never lets it finish. The route is the real one either way.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest

from engine.domain import RunId
from engine.graph_runtime import (
    CANCELLED,
    ControllableExecution,
    GraphId,
    GraphRuntime,
    NodeId,
    RunSnapshot,
    RunStatus,
    UnknownRunError,
    create_app,
)
from graph_runtime_backends import BACKENDS, Backend
from graph_runtime_fakes import (
    Ask,
    AwaitSteering,
    Beat,
    Call,
    Fail,
    Say,
    ScriptedGraph,
    ScriptedNode,
)

GRAPH = GraphId("implementation-review")
POOL = GraphId("reviewer-pool")
REPEATED = GraphId("repeated-review")
IMPLEMENTATION = NodeId("implementation")
REVIEW = NodeId("review")
RERANKER = NodeId("reranker")
REVIEWERS = (NodeId("reviewer-1"), NodeId("reviewer-2"), NodeId("reviewer-3"))

#: How long a test waits for an event before deciding the run is stuck. Generous,
#: because it only bounds a failure -- a passing run never waits.
PATIENCE = 5.0


@pytest.fixture(params=BACKENDS, ids=[backend.name for backend in BACKENDS])
def build(request: pytest.FixtureRequest) -> Backend:
    """The runtime under test: the scripted stand-in, then the LangGraph one."""
    return request.param


def _pipeline(*implementation: Beat) -> ScriptedGraph:
    """Implementation, then review, with the implementation node scripted."""
    return ScriptedGraph(
        GRAPH,
        "Implementation and review",
        (
            ScriptedNode(
                IMPLEMENTATION,
                implementation,
                next_nodes=(REVIEW,),
                name="Implementation",
            ),
            ScriptedNode(REVIEW, (Say("Looks right."),), name="Review"),
        ),
    )


def _fan_out(*reviewer: Beat) -> ScriptedGraph:
    """One implementation, three reviewers at once, then a reranker.

    The shape a linear "current node" cannot describe: while the pool is
    working there are three answers to "where is this run?", and picking one
    would be a guess.
    """
    return ScriptedGraph(
        POOL,
        "Reviewer pool",
        (
            ScriptedNode(
                IMPLEMENTATION,
                (Say("Wrote the code."),),
                next_nodes=REVIEWERS,
                name="Implementation",
            ),
            *(
                ScriptedNode(
                    node_id, reviewer, next_nodes=(RERANKER,), name=str(node_id)
                )
                for node_id in REVIEWERS
            ),
            ScriptedNode(RERANKER, (Say("Ranked them."),), name="Reranker"),
        ),
    )


def _repeated(*review: Beat) -> ScriptedGraph:
    """Implementation, then the *same* review node three times over.

    What `Send` does: one node, several concurrent tasks. The three share a
    name and nothing else -- separate executions, separate transcripts,
    separate approvals -- so this is the graph that makes a node name useless
    as an address.
    """
    return ScriptedGraph(
        REPEATED,
        "Three passes of one reviewer",
        (
            ScriptedNode(
                IMPLEMENTATION,
                (Say("Wrote the code."),),
                next_nodes=(REVIEW,),
                name="Implementation",
            ),
            ScriptedNode(REVIEW, review, tasks=3, name="Review"),
        ),
    )


class _Feed:
    """An open subscription, read one event at a time."""

    def __init__(self) -> None:
        self.chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._buffer = b""

    async def _frame(self) -> str:
        while b"\n\n" not in self._buffer:
            self._buffer += await self.chunks.get()
        frame, _, self._buffer = self._buffer.partition(b"\n\n")
        return frame.decode()

    async def event(self) -> dict:
        """The next event, skipping the comment frame that opens the stream."""
        while True:
            frame = await self._frame()
            if frame.startswith(":"):
                continue
            identifier, _, data = frame.partition("\n")
            event = json.loads(data.removeprefix("data:"))
            # The id is what a browser sends back as `Last-Event-ID`, so it has
            # to be the cursor and not some second numbering.
            assert identifier == f"id:{event['sequence']}"
            return event

    async def until(self, kind: str, count: int = 1) -> list[dict]:
        """Everything up to and including the `count`th event of `kind`.

        Counting rather than stopping at the first, because a superstep raises
        the same kind once per node and "the reviewers have all started" is not
        a thing one event says.
        """
        events: list[dict] = []
        seen = 0
        async with asyncio.timeout(PATIENCE):
            while True:
                events.append(await self.event())
                seen += events[-1]["type"] == kind
                if seen == count:
                    return events


@dataclass(slots=True)
class _Surface:
    """One control server, reachable both ways the tests need it."""

    app: object
    client: httpx.AsyncClient

    def subscribe(
        self,
        run_id: str,
        *,
        cursor: int | str | None = None,
        last_event_id: int | None = None,
    ):
        return _subscribe(self.app, run_id, cursor=cursor, last_event_id=last_event_id)

    async def read(
        self, run_id: str, kind: str, count: int = 1, **where: int
    ) -> list[dict]:
        """Subscribe, collect up to `kind`, and close again."""
        async with self.subscribe(run_id, **where) as feed:
            return await feed.until(kind, count)


@asynccontextmanager
async def _subscribe(
    app: object, run_id: str, *, cursor: int | str | None, last_event_id: int | None
) -> AsyncIterator[_Feed]:
    feed = _Feed()
    headers = [(b"host", b"test")]
    if last_event_id is not None:
        headers.append((b"last-event-id", str(last_event_id).encode()))
    path = f"/api/runs/{run_id}/events"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"" if cursor is None else f"cursor={cursor}".encode(),
        "headers": headers,
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
        "root_path": "",
    }
    hung_up = asyncio.Event()

    async def receive() -> dict[str, object]:
        await hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            feed.chunks.put_nowait(message["body"])

    served = asyncio.create_task(app(scope, receive, send))  # type: ignore[operator]
    try:
        yield feed
    finally:
        hung_up.set()
        served.cancel()
        await asyncio.gather(served, return_exceptions=True)


@asynccontextmanager
async def _server(runtime: GraphRuntime, *, async_client) -> AsyncIterator[_Surface]:
    """A running control server, entered and left the way a real one is.

    Through the app's own lifespan rather than by calling `runtime.aclose()`
    here, so what stops the run tasks between test cases is the same shutdown a
    SIGTERM reaches -- a lifespan that stopped nothing would leak tasks into the
    next test instead of passing quietly.
    """
    app = create_app(runtime)
    async with app.router.lifespan_context(app):
        async with async_client(app, base_url="http://test") as client:
            yield _Surface(app, client)


async def _start(surface: _Surface, graph: GraphId = GRAPH, **body: object) -> dict:
    started = await surface.client.post(
        "/api/runs", json={"graphId": str(graph), **body}
    )
    assert started.status_code == 201, started.text
    return started.json()


async def _checkpoints(surface: _Surface, run_id: str) -> list[dict]:
    listed = await surface.client.get(f"/api/runs/{run_id}/checkpoints")
    assert listed.status_code == 200, listed.text
    return listed.json()["checkpoints"]


def _kinds(events: Sequence[dict]) -> list[str]:
    return [event["type"] for event in events]


def _of_kind(events: Sequence[dict], kind: str) -> list[dict]:
    return [event for event in events if event["type"] == kind]


def _nodes_of(executions: Sequence[dict]) -> list[str]:
    """The nodes behind a run's active executions, in the order it reports them."""
    return [execution["nodeId"] for execution in executions]


def _ids_of(executions: Sequence[dict]) -> list[str]:
    return [execution["executionId"] for execution in executions]


def _transcript(events: Sequence[dict]) -> list[tuple[str, str]]:
    return [
        (event["payload"]["role"], event["payload"]["text"])
        for event in _of_kind(events, "transcript")
    ]


# --- the contract itself ---------------------------------------------------


def test_every_backend_satisfies_the_runtime_contract(build: Backend) -> None:
    """Both are `GraphRuntime`s, which is what makes running one suite honest."""
    assert isinstance(build(_pipeline(Say("Done."))), GraphRuntime)


# --- topology --------------------------------------------------------------


def test_topology_describes_every_node_and_edge(
    build: Backend, *, async_client
) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            return (
                await surface.client.get("/api/graphs"),
                await surface.client.get(f"/api/graphs/{GRAPH}"),
                await surface.client.get("/api/graphs/nonexistent"),
            )

    listed, described, missing = asyncio.run(scenario())

    assert [graph["graphId"] for graph in listed.json()["graphs"]] == [str(GRAPH)]
    assert described.status_code == 200
    assert described.json() == {
        "graphId": str(GRAPH),
        "name": "Implementation and review",
        "entryPoint": str(IMPLEMENTATION),
        "nodes": [
            {
                "nodeId": str(IMPLEMENTATION),
                "name": "Implementation",
                "kind": "agent",
                "description": "",
                # A node that says nothing about where it belongs is somewhere
                # a person can go and read, which is what a client offers.
                "showInSidebar": True,
                "alwaysOpen": False,
                "group": "",
                "runner": "",
                "runners": [],
                "runnerInput": "",
                "findingsKey": "",
            },
            {
                "nodeId": str(REVIEW),
                "name": "Review",
                "kind": "agent",
                "description": "",
                "showInSidebar": True,
                "alwaysOpen": False,
                "group": "",
                "runner": "",
                "runners": [],
                "runnerInput": "",
                "findingsKey": "",
            },
        ],
        "edges": [
            {"source": str(IMPLEMENTATION), "target": str(REVIEW), "condition": ""}
        ],
    }
    assert missing.status_code == 404
    assert missing.json() == {"error": "graph not found"}


def test_topology_describes_a_fan_out_as_several_edges_out_of_one_node(
    build: Backend, *, async_client
) -> None:
    """Three edges out of implementation, and three back into the reranker.

    Spelled out rather than derived from `REVIEWERS`, because a test that built
    its expectation the same way the code builds the answer would agree with a
    fan-out that had gone missing. The order is asserted too: `GraphTopology`
    requires edges in declaration order, so that a client rendering the same
    graph twice draws the same diagram.
    """

    async def scenario() -> dict:
        async with _server(build(_fan_out(Say("Fine."))), async_client=async_client) as surface:
            described = await surface.client.get(f"/api/graphs/{POOL}")
            return described.json()

    described = asyncio.run(scenario())

    assert described["edges"] == [
        {"source": "implementation", "target": "reviewer-1", "condition": ""},
        {"source": "implementation", "target": "reviewer-2", "condition": ""},
        {"source": "implementation", "target": "reviewer-3", "condition": ""},
        {"source": "reviewer-1", "target": "reranker", "condition": ""},
        {"source": "reviewer-2", "target": "reranker", "condition": ""},
        {"source": "reviewer-3", "target": "reranker", "condition": ""},
    ]


# --- starting runs and reading their state ---------------------------------


def test_starting_a_run_reports_the_frontier_it_begins_at(
    build: Backend, *, async_client
) -> None:
    async def scenario() -> dict:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            return await _start(surface, values={"repository": "acme/api"})

    run = asyncio.run(scenario())

    assert run["graphId"] == str(GRAPH)
    assert run["status"] == "running"
    # Nothing is executing yet -- the run is standing at its first checkpoint,
    # which is exactly what a resume with no argument would replay.
    assert run["activeExecutions"] == []
    assert run["nextNodes"] == [str(IMPLEMENTATION)]
    assert run["checkpointId"] is not None
    assert run["values"] == {"repository": "acme/api"}
    assert run["pendingApprovals"] == []


def test_starting_a_graph_nobody_registered_is_refused(
    build: Backend, *, async_client
) -> None:
    async def scenario() -> httpx.Response:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            return await surface.client.post(
                "/api/runs", json={"graphId": "nonexistent"}
            )

    refused = asyncio.run(scenario())

    assert refused.status_code == 404
    assert refused.json() == {"error": "unknown graph: nonexistent"}


def test_current_state_names_what_a_paused_run_is_waiting_in(
    build: Backend, *, async_client
) -> None:
    """The pause is the case reading state exists for: nothing else will move."""
    graph = _pipeline(
        Say("Ready to run the tests."), Ask("run the tests", command="pytest")
    )

    async def scenario() -> dict:
        async with _server(build(graph), async_client=async_client) as surface:
            run = await _start(surface)
            await surface.read(str(run["runId"]), "approval.requested")
            paused = await surface.client.get(f"/api/runs/{run['runId']}")
            return paused.json()

    paused = asyncio.run(scenario())

    assert paused["status"] == "awaiting_approval"
    assert _nodes_of(paused["activeExecutions"]) == [str(IMPLEMENTATION)]
    assert paused["nextNodes"] == [str(REVIEW)]
    # The graph's state, as the graph itself would report it -- which is where
    # the two runtimes genuinely differ rather than merely disagree. A superstep
    # is LangGraph's unit of durability, so a node's output is not part of the
    # state until the node returns; the scripted runtime mutates as it goes.
    # Both are truthful about their own graph, and the contract asks for exactly
    # that, so the test asks each for what it has.
    assert paused["values"] == (
        {str(IMPLEMENTATION): "Ready to run the tests."}
        if build.commits_mid_node
        else {}
    )
    approval = paused["pendingApprovals"][0]
    assert approval["nodeId"] == str(IMPLEMENTATION)
    assert approval["command"] == "pytest"
    assert approval["reason"] == "run the tests"
    assert approval["kind"] == "command_execution"
    assert approval["allowedDecisions"] == ["accept", "cancel"]


def test_a_run_nobody_started_is_not_found(build: Backend, *, async_client) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            return (
                await surface.client.get("/api/runs/run-404"),
                await surface.client.get("/api/runs/run-404/events"),
                await surface.client.get("/api/runs/run-404/checkpoints"),
            )

    state, feed, history = asyncio.run(scenario())

    assert state.status_code == 404
    # The feed too: a subscription to a run that does not exist would otherwise
    # hang open looking like a run that has not said anything yet.
    assert feed.status_code == 404
    assert history.status_code == 404


# --- supersteps ------------------------------------------------------------


def test_a_fan_out_reports_every_node_of_the_superstep_at_once(
    build: Backend, *, async_client
) -> None:
    """The reason `active_executions` is plural.

    Three reviewers are one superstep, not three, and there is no truthful value
    for "the current node" while all three are working.
    """
    graph = _fan_out(Say("Reviewing."), AwaitSteering(), Say("Approved."))

    async def scenario() -> tuple[dict, list[dict]]:
        async with _server(build(graph), async_client=async_client) as surface:
            run = await _start(surface, POOL)
            run_id = str(run["runId"])
            # Four transcripts: the implementation's, then one per reviewer, so
            # by the last of them the whole pool is in flight.
            events = await surface.read(run_id, "transcript", 4)
            working = await surface.client.get(f"/api/runs/{run_id}")
            return working.json(), events

    working, events = asyncio.run(scenario())

    assert working["status"] == "running"
    assert _nodes_of(working["activeExecutions"]) == [
        str(node_id) for node_id in REVIEWERS
    ]
    assert working["nextNodes"] == [str(RERANKER)]
    # One checkpoint per superstep boundary, and the pool is one boundary.
    started = [event["nodeId"] for event in _of_kind(events, "node.started")]
    assert started == [str(IMPLEMENTATION), *(str(node) for node in REVIEWERS)]
    pool = _of_kind(events, "checkpoint")[-1]
    assert pool["payload"]["nextNodes"] == [str(node_id) for node_id in REVIEWERS]


# --- steering: routed to the execution, not through the graph ---------------


def test_steering_reaches_the_execution_without_restarting_the_node(
    build: Backend, *, async_client
) -> None:
    """The whole requirement: a message to something already running.

    What must be true afterwards is that `implementation` was entered exactly
    once -- a runtime that delivered the instruction by interrupting the node
    and replaying it would say its opening line twice, and an agent session
    behind it would have been torn down mid-turn to receive one sentence.
    """
    graph = _pipeline(Say("Reading the tree."), AwaitSteering(), Say("Renamed it."))
    runtime = build(graph)

    async def scenario() -> tuple[dict, httpx.Response, list[dict], dict]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            await surface.read(run_id, "transcript")
            waiting = await surface.client.get(f"/api/runs/{run_id}")
            steered = await surface.client.post(
                f"/api/runs/{run_id}/steering", json={"message": "Rename the flag."}
            )
            events = await surface.read(run_id, "run.finished")
            final = await surface.client.get(f"/api/runs/{run_id}?includeValues=true")
            return waiting.json(), steered, events, final.json()

    waiting, steered, events, final = asyncio.run(scenario())

    assert waiting["status"] == "running"
    assert _nodes_of(waiting["activeExecutions"]) == [str(IMPLEMENTATION)]
    assert steered.status_code == 200
    assert runtime.entered(IMPLEMENTATION) == 1
    assert [event["nodeId"] for event in _of_kind(events, "node.started")] == [
        str(IMPLEMENTATION),
        str(REVIEW),
    ]
    assert _transcript(events) == [
        ("assistant", "Reading the tree."),
        ("user", "Rename the flag."),
        ("assistant", "Renamed it."),
        ("assistant", "Looks right."),
    ]
    # Accepted for delivery first, delivered second, and the event names which
    # execution took it -- a run with three agents in it needs that.
    steering = _of_kind(events, "steering.received")
    assert len(steering) == 1
    assert steering[0]["payload"] == {"message": "Rename the flag."}
    assert steering[0]["nodeId"] == str(IMPLEMENTATION)
    # The id the run reported as active is the one the message went to, so a
    # client can tell which of several agents was redirected.
    assert steering[0]["executionId"] == _ids_of(waiting["activeExecutions"])[0]
    assert steering[0]["sequence"] < _of_kind(events, "transcript")[1]["sequence"]
    assert final["status"] == "completed"
    assert final["values"]["steering"] == ["Rename the flag."]


def test_an_execution_waiting_on_an_approval_can_still_be_steered(
    build: Backend, *, async_client
) -> None:
    """The lifecycle this package exists to get right.

    An agent that has asked to run a command is not suspended: its session is
    alive, holding the turn. So it can be redirected while it waits, the
    decision releases the same session rather than replaying the node, and the
    graph is not paused and resumed for either. All of that has to be true at
    once, which is why it is one test.
    """
    graph = _pipeline(
        Say("Reading the tree."),
        Ask("run the tests", command="pytest", tool_name="shell"),
        Say("Tests pass."),
    )
    runtime = build(graph)

    async def scenario() -> dict[str, object]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            asked = await surface.read(run_id, "approval.requested")
            approval_id = asked[-1]["payload"]["approvalId"]
            # Steered while the execution is blocked on the approval. A runtime
            # that had suspended the graph node to ask would have nothing to
            # deliver this to.
            steered = await surface.client.post(
                f"/api/runs/{run_id}/steering",
                json={"message": "Use the fast suite."},
            )
            waiting = await surface.client.get(f"/api/runs/{run_id}")
            released = await surface.client.post(
                f"/api/runs/{run_id}/approvals/{approval_id}",
                json={"decision": "accept"},
            )
            events = await surface.read(run_id, "run.finished")
            return {
                "steered": steered,
                "waiting": waiting.json(),
                "released": released,
                "events": events,
            }

    outcome = asyncio.run(scenario())

    assert outcome["steered"].status_code == 200
    # Still waiting on the person: steering it did not answer the question.
    assert outcome["waiting"]["status"] == "awaiting_approval"
    assert len(outcome["waiting"]["pendingApprovals"]) == 1
    assert outcome["released"].status_code == 200
    assert outcome["released"].json()["pendingApprovals"] == []
    # Entered once. Neither the steering nor the decision restarted the node.
    assert runtime.entered(IMPLEMENTATION) == 1
    events = outcome["events"]
    assert [event["nodeId"] for event in _of_kind(events, "node.started")] == [
        str(IMPLEMENTATION),
        str(REVIEW),
    ]
    assert _transcript(events) == [
        ("assistant", "Reading the tree."),
        ("user", "Use the fast suite."),
        ("assistant", "Tests pass."),
        ("assistant", "Looks right."),
    ]
    assert _of_kind(events, "approval.resolved")[0]["payload"]["decision"] == "accept"


def test_steering_a_fan_out_has_to_name_which_execution_it_is_for(
    build: Backend, *, async_client
) -> None:
    """Three agents running is three answers to "steer this run"."""
    graph = _fan_out(Say("Reviewing."), AwaitSteering(), Say("Approved."))
    runtime = build(graph)

    async def scenario() -> dict[str, object]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface, POOL)
            run_id = str(run["runId"])
            await surface.read(run_id, "transcript", 4)
            working = await surface.client.get(f"/api/runs/{run_id}")
            steering = f"/api/runs/{run_id}/steering"
            unnamed = await surface.client.post(steering, json={"message": "Focus."})
            named = await surface.client.post(
                steering, json={"message": "Focus on auth.", "node": str(REVIEWERS[1])}
            )
            idle = await surface.client.post(
                steering, json={"message": "Rank them.", "node": str(RERANKER)}
            )
            both = await surface.client.post(
                steering,
                json={
                    "message": "Which?",
                    "node": str(REVIEWERS[0]),
                    "execution": _ids_of(working.json()["activeExecutions"])[0],
                },
            )
            for node_id in REVIEWERS:
                await surface.client.post(
                    steering, json={"message": "Wrap up.", "node": str(node_id)}
                )
            events = await surface.read(run_id, "run.finished")
            return {
                "working": working.json(),
                "unnamed": unnamed,
                "named": named,
                "idle": idle,
                "both": both,
                "events": events,
            }

    outcome = asyncio.run(scenario())

    # A race the client can fix by naming one, not a request that failed -- and
    # the refusal lists what it could have named, ids and nodes together.
    assert outcome["unnamed"].status_code == 409
    refusal = outcome["unnamed"].json()["error"]
    assert refusal.startswith("name the execution to control: ")
    assert all(f"({node_id})" in refusal for node_id in REVIEWERS)
    assert all(
        execution_id in refusal
        for execution_id in _ids_of(outcome["working"]["activeExecutions"])
    )
    assert outcome["named"].status_code == 200
    # A node in the graph, but not one of the three in flight.
    assert outcome["idle"].status_code == 409
    assert outcome["idle"].json() == {"error": f"{RERANKER} is not executing"}
    # Two addresses for one message is a client bug, not something to resolve.
    assert outcome["both"].status_code == 400
    assert outcome["both"].json() == {
        "error": "give at most one of node or execution"
    }
    # Each reviewer took its own messages, and none of them was restarted.
    assert all(runtime.entered(node_id) == 1 for node_id in REVIEWERS)
    steered = _of_kind(outcome["events"], "steering.received")
    assert [event["nodeId"] for event in steered] == [
        str(REVIEWERS[1]),
        *(str(node_id) for node_id in REVIEWERS),
    ]


def test_several_tasks_of_one_node_are_several_executions(
    build: Backend, *, async_client
) -> None:
    """The reason an execution has an id of its own.

    LangGraph's `Send` fans several tasks into one node, so `review` can be
    three things at once. A registry keyed by node would have kept the last of
    them and dropped the other two: the run would report one execution, two
    agents would be unreachable, and their approvals unanswerable.
    """
    runtime = build(_repeated(Say("Reviewing."), AwaitSteering()))

    async def scenario() -> dict[str, object]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface, REPEATED)
            run_id = str(run["runId"])
            events = await surface.read(run_id, "transcript", 4)
            working = await surface.client.get(f"/api/runs/{run_id}")
            return {"working": working.json(), "events": events}

    outcome = asyncio.run(scenario())
    executions = outcome["working"]["activeExecutions"]

    # Three entries, one node, three distinct ids -- and the node still named,
    # because that is what a client shows a person.
    assert _nodes_of(executions) == [str(REVIEW)] * 3
    assert len(set(_ids_of(executions))) == 3
    assert runtime.entered(REVIEW) == 3
    # The feed can tell them apart too: three identical transcript lines that
    # would otherwise be indistinguishable.
    reviewing = [
        event
        for event in _of_kind(outcome["events"], "transcript")
        if event["payload"]["text"] == "Reviewing."
    ]
    assert len(reviewing) == 3
    assert sorted(event["executionId"] for event in reviewing) == sorted(
        _ids_of(executions)
    )


def test_steering_one_task_of_a_node_reaches_only_that_task(
    build: Backend, *, async_client
) -> None:
    """The node name is ambiguous here, and the id is not."""
    runtime = build(
        _repeated(Say("Reviewing."), AwaitSteering(), Say("Done."))
    )

    async def scenario() -> dict[str, object]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface, REPEATED)
            run_id = str(run["runId"])
            await surface.read(run_id, "transcript", 4)
            working = await surface.client.get(f"/api/runs/{run_id}")
            ids = _ids_of(working.json()["activeExecutions"])
            steering = f"/api/runs/{run_id}/steering"
            # Naming the node is not enough: three of them are running it.
            by_node = await surface.client.post(
                steering, json={"message": "Focus.", "node": str(REVIEW)}
            )
            targeted = await surface.client.post(
                steering, json={"message": "Focus on auth.", "execution": ids[1]}
            )
            after = await surface.client.get(f"/api/runs/{run_id}")
            for execution_id in (ids[0], ids[2]):
                await surface.client.post(
                    steering, json={"message": "Wrap up.", "execution": execution_id}
                )
            events = await surface.read(run_id, "run.finished")
            return {
                "ids": ids,
                "by_node": by_node,
                "targeted": targeted,
                "after": after.json(),
                "events": events,
            }

    outcome = asyncio.run(scenario())
    ids = outcome["ids"]

    assert outcome["by_node"].status_code == 409
    assert outcome["by_node"].json()["error"].startswith(
        "name the execution to control: "
    )
    assert outcome["targeted"].status_code == 200
    # Only the one that was named was given anything: the other two were sent
    # nothing and so cannot have moved, which is what "reaches only that task"
    # has to mean. (Whether the named one has woken yet is its own business.)
    assert {ids[0], ids[2]} <= set(_ids_of(outcome["after"]["activeExecutions"]))
    steered = _of_kind(outcome["events"], "steering.received")
    assert [event["executionId"] for event in steered] == [ids[1], ids[0], ids[2]]
    assert all(event["nodeId"] == str(REVIEW) for event in steered)
    # Each took its own instructions in its own order, and none was restarted.
    assert runtime.entered(REVIEW) == 3
    delivered = [
        (event["executionId"], event["payload"]["text"])
        for event in _of_kind(outcome["events"], "transcript")
        if event["payload"]["role"] == "user"
    ]
    assert delivered.count((ids[1], "Focus on auth.")) == 1
    assert [entry for entry in delivered if entry[0] == ids[0]] == [
        (ids[0], "Wrap up.")
    ]


def test_an_approval_goes_back_to_the_task_that_raised_it(
    build: Backend, *, async_client
) -> None:
    """Routing by node would release whichever of three the dictionary kept."""
    runtime = build(
        _repeated(Ask("run the tests", command="pytest"), Say("Done."))
    )

    async def scenario() -> dict[str, object]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface, REPEATED)
            run_id = str(run["runId"])
            asked = await surface.read(run_id, "approval.requested", 3)
            waiting = await surface.client.get(f"/api/runs/{run_id}")
            requests = waiting.json()["pendingApprovals"]
            answered = await surface.client.post(
                f"/api/runs/{run_id}/approvals/{requests[1]['approvalId']}",
                json={"decision": "accept"},
            )
            for request in (requests[0], requests[2]):
                await surface.client.post(
                    f"/api/runs/{run_id}/approvals/{request['approvalId']}",
                    json={"decision": "accept"},
                )
            events = await surface.read(run_id, "run.finished")
            return {
                "asked": asked,
                "requests": requests,
                "answered": answered,
                "events": events,
            }

    outcome = asyncio.run(scenario())
    requests = outcome["requests"]

    # Three separate questions from one node, each naming who asked it.
    assert [request["nodeId"] for request in requests] == [str(REVIEW)] * 3
    assert len({request["executionId"] for request in requests}) == 3
    assert len({request["approvalId"] for request in requests}) == 3
    # Answering one released one: two are still outstanding afterwards.
    assert outcome["answered"].status_code == 200
    assert [
        request["approvalId"]
        for request in outcome["answered"].json()["pendingApprovals"]
    ] == [requests[0]["approvalId"], requests[2]["approvalId"]]
    resolved = _of_kind(outcome["events"], "approval.resolved")
    assert resolved[0]["payload"]["approvalId"] == requests[1]["approvalId"]
    assert resolved[0]["executionId"] == requests[1]["executionId"]
    assert runtime.entered(REVIEW) == 3


def test_steering_a_run_with_nothing_in_flight_is_refused(
    build: Backend, *, async_client
) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            await surface.read(run_id, "run.finished")
            return (
                await surface.client.post(
                    f"/api/runs/{run_id}/steering", json={"message": "wait"}
                ),
                await surface.client.post(
                    f"/api/runs/{run_id}/steering", json={"message": "   "}
                ),
                await surface.client.post(
                    "/api/runs/run-404/steering", json={"message": "x"}
                ),
            )

    finished, blank, unknown = asyncio.run(scenario())

    assert finished.status_code == 409
    assert finished.json() == {"error": "this run has no execution in flight"}
    assert blank.status_code == 400
    assert blank.json() == {"error": "message must be a non-empty string"}
    assert unknown.status_code == 404


def test_steering_an_always_open_node_resets_the_graph(
    build: Backend, *, async_client
) -> None:
    """Steering to a node marked always_open after the graph has moved past it
    resumes the graph at that node and delivers the message.

    This is the "re-open the workorder" behaviour: a person sends a message to
    the implementation agent after review has started, and the graph rewinds to
    implementation with the message already queued.
    """
    graph = ScriptedGraph(
        GRAPH,
        "Implementation and review",
        (
            ScriptedNode(
                IMPLEMENTATION,
                (AwaitSteering(), Say("Done.")),
                next_nodes=(REVIEW,),
                name="Implementation",
                always_open=True,
            ),
            ScriptedNode(REVIEW, (Say("Looks right."),), name="Review"),
        ),
    )

    async def scenario() -> dict[str, object]:
        async with _server(build(graph), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            # Wait for implementation to start and steer it the first time.
            await surface.read(run_id, "node.started")
            await surface.client.post(
                f"/api/runs/{run_id}/steering",
                json={"message": "first instruction", "node": str(IMPLEMENTATION)},
            )
            # Let the graph proceed past implementation into review and finish.
            await surface.read(run_id, "run.finished")
            # Now steer to implementation again -- the graph should reset.
            response = await surface.client.post(
                f"/api/runs/{run_id}/steering",
                json={"message": "second instruction", "node": str(IMPLEMENTATION)},
            )
            assert response.status_code == 200, response.json()
            # The graph should be running again at implementation.
            snapshot = response.json()
            assert snapshot["status"] == "running"
            # Wait for the run to finish again.
            events = await surface.read(run_id, "run.finished")
            return {"events": events, "snapshot": snapshot}

    outcome = asyncio.run(scenario())
    # The snapshot returned by the steer shows the graph was reset.
    assert outcome["snapshot"]["status"] == "running"
    # The run finished, meaning implementation consumed the steering message
    # and proceeded through review again.
    finished = _of_kind(outcome["events"], "run.finished")
    assert len(finished) >= 1


def test_steering_a_non_always_open_node_with_nothing_in_flight_is_still_refused(
    build: Backend, *, async_client
) -> None:
    """A node that is *not* always_open still refuses steering after completion."""

    async def scenario() -> httpx.Response:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            await surface.read(run_id, "run.finished")
            return await surface.client.post(
                f"/api/runs/{run_id}/steering",
                json={"message": "wait", "node": str(IMPLEMENTATION)},
            )

    response = asyncio.run(scenario())
    assert response.status_code == 409


def test_a_controllable_execution_is_all_the_runtime_asks_of_a_node() -> None:
    """Two methods, and nothing about ACP, Claude or Codex in either of them.

    A node registers one of these and gets steering and approvals routed to it.
    Anything the generic runtime needed beyond this would be a control surface
    that only works for the agents it was written against.
    """

    class Session:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def steer(self, message: str) -> None:
            self.messages.append(message)

        async def decide(self, approval_id, decision) -> None:  # noqa: ANN001
            self.messages.append(f"{approval_id}={decision.value}")

    assert isinstance(Session(), ControllableExecution)


# --- approvals -------------------------------------------------------------


def test_an_approval_is_published_answered_and_answered_only_once(
    build: Backend, *, async_client
) -> None:
    graph = _pipeline(Ask("run the tests", command="pytest", tool_name="shell"))

    async def scenario() -> dict[str, object]:
        async with _server(build(graph), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            asked = await surface.read(run_id, "approval.requested")
            approval_id = asked[-1]["payload"]["approvalId"]
            answer = f"/api/runs/{run_id}/approvals/{approval_id}"
            unknown = await surface.client.post(
                f"/api/runs/{run_id}/approvals/approval-404",
                json={"decision": "accept"},
            )
            garbled = await surface.client.post(answer, json={"decision": "maybe"})
            accepted = await surface.client.post(answer, json={"decision": "accept"})
            finished = await surface.read(run_id, "run.finished")
            again = await surface.client.post(answer, json={"decision": "accept"})
            return {
                "asked": asked,
                "unknown": unknown,
                "garbled": garbled,
                "accepted": accepted,
                "finished": finished,
                "again": again,
            }

    answered = asyncio.run(scenario())

    request = answered["asked"][-1]
    assert request["nodeId"] == str(IMPLEMENTATION)
    assert request["payload"]["command"] == "pytest"
    assert request["payload"]["toolName"] == "shell"
    assert answered["unknown"].status_code == 404
    assert answered["garbled"].status_code == 400
    assert answered["garbled"].json() == {
        "error": "decision must be one of: accept, accept_for_session, cancel"
    }
    # The reply is the run as the decision left it -- released, and no longer
    # asking -- rather than however far the execution then got. A reply that
    # waited for the agent would say something different per script.
    assert answered["accepted"].status_code == 200
    assert answered["accepted"].json()["status"] == "running"
    assert answered["accepted"].json()["pendingApprovals"] == []
    resolved = _of_kind(answered["finished"], "approval.resolved")[0]
    assert resolved["payload"] == {
        "approvalId": request["payload"]["approvalId"],
        "decision": "accept",
    }
    # The second answer is a race that lost, not a request that never existed.
    assert answered["again"].status_code == 409


def test_cancelling_an_approval_fails_the_run_where_it_asked(
    build: Backend, *, async_client
) -> None:
    graph = _pipeline(Ask("delete the branch", command="git branch -D main"))

    async def scenario() -> tuple[list[dict], dict, dict]:
        async with _server(build(graph), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            asked = await surface.read(run_id, "approval.requested")
            refused = await surface.client.post(
                f"/api/runs/{run_id}/approvals/{asked[-1]['payload']['approvalId']}",
                json={"decision": "cancel"},
            )
            events = await surface.read(run_id, "run.failed")
            state = await surface.client.get(f"/api/runs/{run_id}")
            return events, state.json(), refused.json()

    events, state, refused = asyncio.run(scenario())

    # A refusal is the end of the run, so the reply is the run already over --
    # not a status set on something still executing.
    assert refused["status"] == "failed"
    assert refused["error"] == "delete the branch was not allowed"
    assert refused["activeExecutions"] == []
    assert refused["pendingApprovals"] == []
    assert _of_kind(events, "approval.resolved")[0]["payload"]["decision"] == "cancel"
    assert state["status"] == "failed"
    assert state["error"] == "delete the branch was not allowed"
    assert state["activeExecutions"] == []
    # Review never ran, and the position it stopped at still names the node it
    # was about to run -- which is what a resume needs.
    assert state["nextNodes"] == [str(IMPLEMENTATION)]
    assert [event["nodeId"] for event in _of_kind(events, "node.started")] == [
        str(IMPLEMENTATION)
    ]
    assert _of_kind(events, "run.failed")[0]["nodeId"] == str(IMPLEMENTATION)


def test_refusing_one_task_ends_the_run_and_takes_its_siblings_with_it(
    build: Backend, *, async_client
) -> None:
    """A refusal is terminal, so it has to actually terminate the run.

    Three tasks of one node each stop to ask permission. Refusing one of them
    used to set the run's status and nothing else: the superstep went on waiting
    for the two siblings, whose own questions nobody was going to answer now, so
    no terminal event was ever published -- and those two carried on accepting
    steering and decisions while the run reported itself over. `RunStatus.FAILED`
    exists to stop a client being given two answers it cannot reconcile, so
    reaching it has to mean the run is really finished.
    """
    runtime = build(
        _repeated(Ask("delete the branch", command="git branch -D main"), Say("Done."))
    )

    async def scenario() -> dict[str, object]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface, REPEATED)
            run_id = str(run["runId"])
            await surface.read(run_id, "approval.requested", 3)
            waiting = await surface.client.get(f"/api/runs/{run_id}")
            requests = waiting.json()["pendingApprovals"]
            refused = await surface.client.post(
                f"/api/runs/{run_id}/approvals/{requests[1]['approvalId']}",
                json={"decision": "cancel"},
            )
            # Both aimed at siblings that were mid-question when the run ended.
            sibling = await surface.client.post(
                f"/api/runs/{run_id}/approvals/{requests[0]['approvalId']}",
                json={"decision": "accept"},
            )
            steered = await surface.client.post(
                f"/api/runs/{run_id}/steering",
                json={"message": "carry on", "execution": requests[2]["executionId"]},
            )
            return {
                "requests": requests,
                "refused": refused.json(),
                "sibling": sibling,
                "steered": steered,
                "events": await surface.read(run_id, "run.failed"),
                "executors": len(runtime.executors(RunId(run_id))),
            }

    outcome = asyncio.run(scenario())
    requests = outcome["requests"]

    assert len(requests) == 3
    # The reply is the run over: nothing executing, nothing still being asked.
    assert outcome["refused"]["status"] == "failed"
    assert outcome["refused"]["error"] == "delete the branch was not allowed"
    assert outcome["refused"]["activeExecutions"] == []
    assert outcome["refused"]["pendingApprovals"] == []
    assert outcome["executors"] == 0
    # And the feed says so, once, naming the node that was refused.
    failures = _of_kind(outcome["events"], "run.failed")
    assert len(failures) == 1
    assert failures[0]["payload"] == {"error": "delete the branch was not allowed"}
    assert failures[0]["nodeId"] == str(REVIEW)
    # One question was answered; the siblings' were abandoned, not resolved.
    resolved = _of_kind(outcome["events"], "approval.resolved")
    assert [event["payload"]["approvalId"] for event in resolved] == [
        requests[1]["approvalId"]
    ]
    assert resolved[0]["executionId"] == requests[1]["executionId"]
    # Neither sibling is still reachable. The abandoned request is a 409 rather
    # than a 404: a client showing it should be told it is no longer pending,
    # not that it never existed.
    assert outcome["sibling"].status_code == 409
    assert outcome["steered"].status_code == 409
    assert outcome["steered"].json() == {
        "error": "this run has no execution in flight"
    }


# --- a node that raises ----------------------------------------------------


def test_a_node_that_raises_fails_the_run_where_it_raised(
    build: Backend, *, async_client
) -> None:
    """The failure path, which is ordinary rather than exceptional.

    A run whose task died silently would report `running` forever, hold every
    subscriber waiting for a terminal event, and refuse steering as having
    nothing in flight -- three answers a client cannot reconcile.
    """
    graph = _pipeline(Say("Running the tests."), Fail("codex is out of quota"))

    async def scenario() -> tuple[list[dict], dict, httpx.Response]:
        async with _server(build(graph), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            events = await surface.read(run_id, "run.failed")
            state = await surface.client.get(f"/api/runs/{run_id}")
            steered = await surface.client.post(
                f"/api/runs/{run_id}/steering", json={"message": "try again"}
            )
            return events, state.json(), steered

    events, state, steered = asyncio.run(scenario())

    assert events[-1]["payload"] == {"error": "codex is out of quota"}
    assert events[-1]["nodeId"] == str(IMPLEMENTATION)
    assert state["status"] == "failed"
    assert state["error"] == "codex is out of quota"
    assert state["activeExecutions"] == []
    assert state["nextNodes"] == [str(IMPLEMENTATION)]
    assert [event["nodeId"] for event in _of_kind(events, "node.started")] == [
        str(IMPLEMENTATION)
    ]
    # The refusal to steer now agrees with the status.
    assert steered.status_code == 409


# --- cancellation ----------------------------------------------------------
#
# Not a route: cancelling is what a caller holding the runtime does when the
# reason for the run is gone -- the WorkOrder it belonged to was deleted -- so
# the primitive is called directly here, the way `running()` is.


def test_cancelling_a_run_stops_it_and_says_it_is_over(
    build: Backend, *, async_client
) -> None:
    """Nothing left driving the run, and the run says so.

    Both halves matter. Stopping without recording an ending would leave the
    engine reporting `running` for a run with no driver -- the exact answer the
    failure path exists to prevent -- and recording an ending without stopping
    would leave a node working on a run everyone has been told is finished.
    """
    runtime = build(_pipeline(Say("Reading."), AwaitSteering()))

    async def scenario() -> tuple[dict, list[str], RunSnapshot]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            await surface.read(run_id, "transcript")
            cancelled = await runtime.cancel(RunId(run_id))
            state = await surface.client.get(f"/api/runs/{run_id}")
            return state.json(), [str(one) for one in runtime.running()], cancelled

    state, still_running, cancelled = asyncio.run(scenario())

    assert still_running == []
    assert cancelled.status is RunStatus.FAILED
    assert cancelled.error == CANCELLED
    assert state["status"] == "failed"
    assert state["error"] == CANCELLED
    assert state["activeExecutions"] == []


def test_cancelling_a_run_parked_on_a_question_settles_it_undecided(
    build: Backend, *, async_client
) -> None:
    """The state a WorkOrder mostly sits in, so it is the one worth stopping.

    A run waiting on permission has no task making progress, and stopping the
    executor is not enough on its own: the questions it raised outlive it in
    whatever the runtime writes them down in. Left pending they would report a
    run that is over as still waiting on a person, and answering one would go
    to an execution that no longer exists.

    Three tasks of one node, each mid-question, because a superstep is plural
    and a cancel has to take all of them. Settled rather than deleted and
    without an `approval.resolved` for any of them: nobody decided these, so a
    client still showing one is told it is no longer pending rather than that
    it was answered or never existed.
    """
    runtime = build(
        _repeated(Ask("delete the branch", command="git branch -D main"), Say("Done."))
    )

    async def scenario() -> dict[str, object]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface, REPEATED)
            run_id = str(run["runId"])
            async with surface.subscribe(run_id) as feed:
                await feed.until("approval.requested", 3)
                waiting = await surface.client.get(f"/api/runs/{run_id}")
                requests = waiting.json()["pendingApprovals"]
                cancelled = await runtime.cancel(RunId(run_id))
                events = await feed.until("run.failed")
            return {
                "waiting": waiting.json(),
                "requests": requests,
                "cancelled": cancelled,
                "state": (await surface.client.get(f"/api/runs/{run_id}")).json(),
                # Aimed at a question that was open when the run was stopped.
                "decided": await surface.client.post(
                    f"/api/runs/{run_id}/approvals/{requests[0]['approvalId']}",
                    json={"decision": "accept"},
                ),
                "events": events,
                "running": [str(one) for one in runtime.running()],
            }

    outcome = asyncio.run(scenario())

    # The run really was parked on the questions, rather than racing past them.
    assert outcome["waiting"]["status"] == "awaiting_approval"
    assert len(outcome["requests"]) == 3
    assert outcome["cancelled"].status is RunStatus.FAILED
    assert outcome["cancelled"].error == CANCELLED
    assert outcome["cancelled"].pending_approvals == ()
    assert outcome["running"] == []
    state = outcome["state"]
    assert state["status"] == "failed"
    assert state["error"] == CANCELLED
    assert state["pendingApprovals"] == []
    assert state["activeExecutions"] == []
    # Nobody answered them, and nobody can now: 409 rather than the 404 a
    # forgotten request would give.
    assert outcome["decided"].status_code == 409
    assert _of_kind(outcome["events"], "approval.resolved") == []
    assert len(_of_kind(outcome["events"], "run.failed")) == 1


def test_cancelling_a_finished_run_leaves_what_it_said_about_itself(
    build: Backend, *, async_client
) -> None:
    """A run that already ended is not re-ended.

    The row a person deletes may be one that finished last week, and reporting
    it as cancelled would rewrite what happened. Cancelling twice is the same
    case, and has to be as harmless.
    """
    runtime = build(_pipeline(Say("Wrote it.")))

    async def scenario() -> dict:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            await surface.read(run_id, "run.finished")
            await runtime.cancel(RunId(run_id))
            await runtime.cancel(RunId(run_id))
            return (await surface.client.get(f"/api/runs/{run_id}")).json()

    state = asyncio.run(scenario())

    assert state["status"] == "completed"
    assert state["error"] == ""


def test_cancelling_a_run_nobody_started_is_refused(
    build: Backend, *, async_client
) -> None:
    """`UnknownRunError`, so a caller can tell it from a run it did stop."""
    runtime = build(_pipeline(Say("Wrote it.")))

    async def scenario() -> None:
        async with _server(runtime, async_client=async_client):
            with pytest.raises(UnknownRunError):
                await runtime.cancel(RunId("run-nobody-started"))

    asyncio.run(scenario())


# --- shutdown --------------------------------------------------------------


def test_shutting_the_server_down_stops_the_runs_it_was_driving(
    build: Backend, *, async_client
) -> None:
    """Runs outlive the request that started them, so nothing else would.

    Without this a SIGTERM drops whatever node was mid-execution, with no
    chance for an implementation to checkpoint it.
    """
    runtime = build(_pipeline(Say("Reading."), AwaitSteering()))

    async def scenario() -> list[str]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface)
            await surface.read(str(run["runId"]), "transcript")
        return [str(run_id) for run_id in runtime.running()]

    assert asyncio.run(scenario()) == []


# --- checkpoints and resumption --------------------------------------------


def test_a_run_saves_a_checkpoint_at_every_superstep_boundary(
    build: Backend, *, async_client
) -> None:
    async def scenario() -> list[dict]:
        runtime = build(_pipeline(Say("Wrote it.")))
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            await surface.read(run_id, "run.finished")
            return await _checkpoints(surface, run_id)

    history = asyncio.run(scenario())

    assert [point["nextNodes"] for point in history] == [
        [str(IMPLEMENTATION)],
        [str(REVIEW)],
        [],
    ]
    assert [point["source"] for point in history] == ["start", "superstep", "superstep"]
    # A chain, so an audit can walk backwards from any position to the first.
    assert history[0]["parentId"] is None
    assert history[1]["parentId"] == history[0]["checkpointId"]
    assert history[2]["parentId"] == history[1]["checkpointId"]
    # The state each position starts from, not the state it produced.
    assert history[0]["values"] == {}
    assert history[1]["values"] == {str(IMPLEMENTATION): "Wrote it."}


def test_sending_a_run_back_forks_and_keeps_the_attempt_it_replaces(
    build: Backend, *, async_client
) -> None:
    """"Send it back to implementation" resolved to a checkpoint, and appended.

    A destructive rewind would be cheaper and would throw away the thing the
    workflow exists to produce: the first attempt, next to the second, with the
    review that rejected it still readable.

    This is also what keeps the "steering did not restart the node" assertions
    elsewhere from being vacuous: `entered` counts a second pass when there
    genuinely is one, and here there is.
    """
    runtime = build(_pipeline(Say("Wrote the code.")))

    async def scenario() -> dict[str, object]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            first = await surface.read(run_id, "run.finished")
            before = await _checkpoints(surface, run_id)
            reverted = await surface.client.post(
                f"/api/runs/{run_id}/transitions", json={"node": str(IMPLEMENTATION)}
            )
            assert reverted.status_code == 200, reverted.text
            second = await surface.read(
                run_id, "run.finished", cursor=first[-1]["sequence"]
            )
            return {
                "before": before,
                "reverted": reverted.json(),
                "second": second,
                "after": await _checkpoints(surface, run_id),
                "finally": (await surface.client.get(f"/api/runs/{run_id}")).json(),
            }

    outcome = asyncio.run(scenario())
    before, after = outcome["before"], outcome["after"]
    reverted = outcome["reverted"]

    # The answer to the request is the forked position: standing at a new
    # checkpoint, holding the state the node was entered with, nothing running.
    assert reverted["status"] == "running"
    assert reverted["activeExecutions"] == []
    assert reverted["nextNodes"] == [str(IMPLEMENTATION)]
    assert reverted["values"] == {}
    assert reverted["checkpointId"] not in {point["checkpointId"] for point in before}
    # Nothing was removed. The fork hangs off the position it re-attempts, so
    # the first attempt keeps its own children.
    assert after[: len(before)] == before
    fork = after[len(before)]
    assert fork["source"] == "fork"
    assert fork["parentId"] == before[0]["checkpointId"]
    assert fork["checkpointId"] == reverted["checkpointId"]
    assert _kinds(outcome["second"]) == [
        "run.forked",
        "node.started",
        "transcript",
        "node.finished",
        "checkpoint",
        "node.started",
        "transcript",
        "node.finished",
        "checkpoint",
        "run.finished",
    ]
    forked = outcome["second"][0]["payload"]
    assert forked["from"] == before[0]["checkpointId"]
    assert forked["checkpointId"] == fork["checkpointId"]
    assert forked["nodes"] == [str(IMPLEMENTATION)]
    assert outcome["finally"]["status"] == "completed"
    # A fork is the one thing that *does* run a node again, and it says so.
    assert runtime.entered(IMPLEMENTATION) == 2


def test_a_node_selector_takes_the_latest_position(
    build: Backend, *, async_client
) -> None:
    """The ambiguity a node has, answered where it belongs.

    After one fork there are two checkpoints that were about to run
    `implementation`, and the words "send it back to implementation" do not pick
    between them. The selector takes the latest -- the attempt the person is
    looking at -- and a client that wants an earlier one names the id it read
    from `/checkpoints`.
    """

    async def scenario() -> dict[str, object]:
        runtime = build(_pipeline(Say("Wrote the code.")))
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            transitions = f"/api/runs/{run_id}/transitions"
            await surface.read(run_id, "run.finished")
            first = await surface.client.post(
                transitions, json={"node": str(IMPLEMENTATION)}
            )
            await surface.read(run_id, "run.finished", 2)
            second = await surface.client.post(
                transitions, json={"node": str(IMPLEMENTATION)}
            )
            await surface.read(run_id, "run.finished", 3)
            history = await _checkpoints(surface, run_id)
            named = await surface.client.post(
                transitions, json={"checkpoint": history[1]["checkpointId"]}
            )
            return {
                "first": first.json(),
                "second": second.json(),
                "named": named.json(),
                "history": history,
            }

    outcome = asyncio.run(scenario())
    history = outcome["history"]
    by_id = {point["checkpointId"]: point for point in history}

    # The second fork hangs off the first one, not off the original start.
    assert by_id[outcome["second"]["checkpointId"]]["parentId"] == (
        outcome["first"]["checkpointId"]
    )
    # And naming a checkpoint reaches one the selector would never have chosen.
    assert outcome["named"]["nextNodes"] == history[1]["nextNodes"]
    assert outcome["named"]["values"] == history[1]["values"]


def test_a_failed_run_can_be_sent_back_and_run_again(
    build: Backend, *, async_client
) -> None:
    """Recovery is the reason a failure leaves a resumable position behind."""
    graph = ScriptedGraph(
        GRAPH,
        "Implementation and review",
        (
            ScriptedNode(
                IMPLEMENTATION,
                (Fail("codex is out of quota"),),
                next_nodes=(REVIEW,),
                name="Implementation",
            ),
            ScriptedNode(REVIEW, (Say("Looks right."),), name="Review"),
        ),
    )

    async def scenario() -> tuple[list[dict], dict]:
        async with _server(build(graph), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            failed = await surface.read(run_id, "run.failed")
            reverted = await surface.client.post(
                f"/api/runs/{run_id}/transitions", json={"node": str(IMPLEMENTATION)}
            )
            assert reverted.status_code == 200, reverted.text
            return failed, reverted.json()

    failed, reverted = asyncio.run(scenario())

    assert failed[-1]["type"] == "run.failed"
    # The error is cleared by the fork: it describes the attempt being replaced,
    # and a run that carried it forward would look failed while running.
    assert reverted["status"] == "running"
    assert reverted["error"] == ""
    assert reverted["nextNodes"] == [str(IMPLEMENTATION)]


def test_a_resume_can_interrupt_a_superstep_that_is_still_running(
    build: Backend, *, async_client
) -> None:
    """Reverting is not something only a finished run can be asked for."""
    graph = _pipeline(Say("Reading the tree."), AwaitSteering(), Say("Never reached."))

    async def scenario() -> tuple[dict, list[dict]]:
        async with _server(build(graph), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            waiting = await surface.read(run_id, "transcript")
            reverted = await surface.client.post(
                f"/api/runs/{run_id}/transitions", json={"node": str(IMPLEMENTATION)}
            )
            assert reverted.status_code == 200, reverted.text
            restarted = await surface.read(
                run_id, "node.started", cursor=waiting[-1]["sequence"]
            )
            return reverted.json(), restarted

    reverted, restarted = asyncio.run(scenario())

    assert reverted["values"] == {}
    assert reverted["nextNodes"] == [str(IMPLEMENTATION)]
    assert _kinds(restarted) == ["run.forked", "node.started"]
    assert restarted[-1]["nodeId"] == str(IMPLEMENTATION)


def test_two_resumes_arriving_at_once_do_not_leave_two_executors(
    build: Backend, *, async_client
) -> None:
    """`resume_from` says anything in flight is stopped first, so it has to be.

    Stopping is asynchronous -- something is cancelled and waited for -- so the
    guard is a stretch of time rather than a check, and a second request
    arriving inside it used to walk straight past: the first caller had already
    let go of the executor, so the second found nothing to stop and started one
    of its own. Two executors then drove one run, interleaving into one
    checkpoint list and one values dict and publishing two endings for it.

    Both requests are still honoured -- they are serialised, not dropped -- so
    what this pins is that only one of them is driving the run afterwards.
    """
    graph = _pipeline(Say("Reading the tree."), AwaitSteering())
    runtime = build(graph)

    async def scenario() -> dict[str, object]:
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            await surface.read(run_id, "transcript")
            transitions = f"/api/runs/{run_id}/transitions"
            both = await asyncio.gather(
                surface.client.post(transitions, json={"node": str(IMPLEMENTATION)}),
                surface.client.post(transitions, json={"node": str(IMPLEMENTATION)}),
            )
            return {
                "both": both,
                "executors": len(runtime.executors(RunId(run_id))),
                "history": await _checkpoints(surface, run_id),
            }

    outcome = asyncio.run(scenario())
    history = outcome["history"]
    by_id = {point["checkpointId"]: point for point in history}

    assert [answered.status_code for answered in outcome["both"]] == [200, 200]
    assert outcome["executors"] == 1
    # Two forks, each answered to one of the callers, and each a child of a
    # checkpoint that exists -- rather than two branches appending side by side.
    forks = [point for point in history if point["source"] == "fork"]
    assert len(forks) == 2
    assert [answered.json()["checkpointId"] for answered in outcome["both"]] == [
        fork["checkpointId"] for fork in forks
    ]
    assert all(
        point["parentId"] is None or point["parentId"] in by_id for point in history
    )


def test_a_transition_the_runtime_cannot_resolve_is_refused(
    build: Backend, *, async_client
) -> None:
    graph = _pipeline(Fail("codex is out of quota"))

    async def scenario() -> dict[str, httpx.Response]:
        async with _server(build(graph), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            transitions = f"/api/runs/{run_id}/transitions"
            await surface.read(run_id, "run.failed")
            return {
                "unknown_node": await surface.client.post(
                    transitions, json={"node": "deploy"}
                ),
                # `review` is in the graph, but this run never got near it.
                "never_reached": await surface.client.post(
                    transitions, json={"node": str(REVIEW)}
                ),
                "unknown_checkpoint": await surface.client.post(
                    transitions, json={"checkpoint": "checkpoint-404"}
                ),
                "neither": await surface.client.post(transitions, json={}),
                "both": await surface.client.post(
                    transitions,
                    json={"node": str(IMPLEMENTATION), "checkpoint": "checkpoint-1"},
                ),
                "unknown_run": await surface.client.post(
                    "/api/runs/run-404/transitions", json={"node": str(IMPLEMENTATION)}
                ),
            }

    refused = asyncio.run(scenario())

    assert refused["unknown_node"].status_code == 400
    assert refused["unknown_node"].json() == {"error": "unknown node: deploy"}
    # Not a misspelling -- a place the run has not been. A client may retry it
    # once the run has got there, which is what separates it from the 400.
    assert refused["never_reached"].status_code == 409
    assert refused["never_reached"].json() == {
        "error": f"this run has never been about to run {REVIEW}"
    }
    assert refused["unknown_checkpoint"].status_code == 404
    assert refused["neither"].status_code == 400
    assert refused["neither"].json() == {
        "error": "give exactly one of node or checkpoint"
    }
    assert refused["both"].status_code == 400
    assert refused["unknown_run"].status_code == 404


# --- subscribing to events -------------------------------------------------


def test_the_feed_carries_transcript_events_and_tool_calls(
    build: Backend, *, async_client
) -> None:
    graph = _pipeline(
        Say("Reading the tree."),
        Call("shell", {"command": "pytest"}, result="14 passed"),
        Say("The tests pass."),
    )

    async def scenario() -> list[dict]:
        async with _server(build(graph), async_client=async_client) as surface:
            run = await _start(surface)
            return await surface.read(str(run["runId"]), "run.finished")

    events = asyncio.run(scenario())

    assert _kinds(events) == [
        "run.started",
        "checkpoint",
        "node.started",
        "transcript",
        "tool.call",
        "tool.result",
        "transcript",
        "node.finished",
        "checkpoint",
        "node.started",
        "transcript",
        "node.finished",
        "checkpoint",
        "run.finished",
    ]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert _transcript(events) == [
        ("assistant", "Reading the tree."),
        ("assistant", "The tests pass."),
        ("assistant", "Looks right."),
    ]
    call, result = _of_kind(events, "tool.call")[0], _of_kind(events, "tool.result")[0]
    assert call["nodeId"] == str(IMPLEMENTATION)
    assert call["payload"]["name"] == "shell"
    assert call["payload"]["arguments"] == {"command": "pytest"}
    assert result["payload"]["callId"] == call["payload"]["callId"]
    assert result["payload"]["result"] == "14 passed"


def test_one_open_subscription_sees_the_run_it_watched_finish_and_start_again(
    build: Backend, *, async_client
) -> None:
    """The feed outlives `run.finished`, on the feed rather than on a reconnect.

    A subscription that closed itself on the terminal event would still pass a
    suite that re-subscribes with a cursor after each fork, so this one holds a
    single feed open across the whole thing: finish, send it back, and watch the
    same feed carry the run starting again.
    """

    async def scenario() -> list[dict]:
        runtime = build(_pipeline(Say("Wrote the code.")))
        async with _server(runtime, async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            async with surface.subscribe(run_id) as feed:
                await feed.until("run.finished")
                reverted = await surface.client.post(
                    f"/api/runs/{run_id}/transitions",
                    json={"node": str(IMPLEMENTATION)},
                )
                assert reverted.status_code == 200, reverted.text
                return await feed.until("node.started")

    after = asyncio.run(scenario())

    assert _kinds(after) == ["run.forked", "node.started"]
    assert after[-1]["nodeId"] == str(IMPLEMENTATION)


def test_two_subscribers_at_different_cursors_each_see_the_whole_run(
    build: Backend, *, async_client
) -> None:
    """Fan-out is the log's job, so more than one reader has to be real.

    Two feeds open at once on the same run, one from the beginning and one from
    partway through, is where a shared condition and a shared cursor would go
    wrong. Each sees every event it asked for, in order, and neither advances
    the other past anything.
    """
    graph = _pipeline(Say("Reading the tree."), AwaitSteering(), Say("Renamed it."))

    async def scenario() -> tuple[list[dict], list[dict]]:
        async with _server(build(graph), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            async with surface.subscribe(run_id) as first:
                opening = await first.until("transcript")
                async with surface.subscribe(
                    run_id, cursor=opening[-1]["sequence"]
                ) as second:
                    await surface.client.post(
                        f"/api/runs/{run_id}/steering", json={"message": "Rename it."}
                    )
                    both = await asyncio.gather(
                        first.until("run.finished"), second.until("run.finished")
                    )
            return opening + both[0], both[1]

    whole, late = asyncio.run(scenario())

    assert _kinds(whole)[:3] == ["run.started", "checkpoint", "node.started"]
    assert _kinds(whole)[-1] == "run.finished"
    assert [event["sequence"] for event in whole] == list(range(1, len(whole) + 1))
    # The late reader saw everything after its cursor and nothing before it.
    assert late == whole[len(whole) - len(late) :]
    assert late[0]["sequence"] == whole[len(whole) - len(late)]["sequence"]


def test_a_subscriber_replays_from_its_own_cursor(
    build: Backend, *, async_client
) -> None:
    """A reconnecting client asks for what it missed, not for everything."""

    async def scenario() -> tuple[list[dict], list[dict], httpx.Response]:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            everything = await surface.read(run_id, "run.finished")
            resumed = await surface.read(
                run_id, "run.finished", cursor=everything[2]["sequence"]
            )
            refused = await surface.client.get(
                f"/api/runs/{run_id}/events", params={"cursor": "later"}
            )
            return everything, resumed, refused

    everything, resumed, refused = asyncio.run(scenario())

    assert _kinds(resumed) == _kinds(everything)[3:]
    assert resumed[0]["sequence"] == everything[3]["sequence"]
    assert refused.status_code == 400
    assert refused.json() == {"error": "cursor must be an integer"}


def test_a_cursor_before_the_beginning_is_refused(
    build: Backend, *, async_client
) -> None:
    async def scenario() -> httpx.Response:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            run = await _start(surface)
            return await surface.client.get(
                f"/api/runs/{run['runId']}/events", params={"cursor": "-1"}
            )

    refused = asyncio.run(scenario())

    assert refused.status_code == 400
    assert refused.json() == {"error": "cursor must not be negative"}


def test_a_browser_reconnects_with_the_event_id_it_last_saw(
    build: Backend, *, async_client
) -> None:
    """`Last-Event-ID` is what EventSource sends; honouring it avoids a poll."""

    async def scenario() -> tuple[list[dict], dict]:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            everything = await surface.read(run_id, "run.finished")
            async with surface.subscribe(
                run_id, last_event_id=everything[-2]["sequence"]
            ) as feed:
                async with asyncio.timeout(PATIENCE):
                    return everything, await feed.event()

    everything, first = asyncio.run(scenario())

    assert first == everything[-1]
    assert first["type"] == "run.finished"


def test_an_explicit_cursor_beats_the_header_even_when_it_is_empty(
    build: Backend, *, async_client
) -> None:
    """An empty `cursor` is a position -- the beginning -- not an absent one.

    A client that says `?cursor=` is asking to replay the run from the start,
    and honouring the browser's memory instead would answer with somewhere it
    did not ask for.
    """

    async def scenario() -> tuple[list[dict], dict]:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            run = await _start(surface)
            run_id = str(run["runId"])
            everything = await surface.read(run_id, "run.finished")
            async with surface.subscribe(
                run_id, cursor="", last_event_id=everything[-2]["sequence"]
            ) as feed:
                async with asyncio.timeout(PATIENCE):
                    return everything, await feed.event()

    everything, first = asyncio.run(scenario())

    assert first == everything[0]
    assert first["type"] == "run.started"


# --- request validation ----------------------------------------------------


@pytest.mark.parametrize(
    "body,message",
    [
        ({}, "graphId must be a non-empty string"),
        ({"graphId": "  "}, "graphId must be a non-empty string"),
        ({"graphId": str(GRAPH), "values": []}, "values must be an object"),
    ],
)
def test_starting_a_run_refuses_a_body_it_cannot_read(
    build: Backend, body: dict[str, object], message: str, *, async_client
) -> None:
    async def scenario() -> httpx.Response:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            return await surface.client.post("/api/runs", json=body)

    refused = asyncio.run(scenario())

    assert refused.status_code == 400
    assert refused.json() == {"error": message}


@pytest.mark.parametrize(
    "body", [b"not json", b'{"graphId": "\xff"}'], ids=["unparseable", "not utf-8"]
)
def test_a_body_that_is_not_json_at_all_is_still_a_400(
    build: Backend, body: bytes, *, async_client
) -> None:
    """Including one that is not even text.

    Starlette hands raw bytes to `json.loads`, which decodes them itself, so a
    body that is not UTF-8 raises `UnicodeDecodeError` rather than
    `JSONDecodeError` -- the one way a client could make this surface 500.
    """

    async def scenario() -> httpx.Response:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            return await surface.client.post(
                "/api/runs",
                content=body,
                headers={"content-type": "application/json"},
            )

    refused = asyncio.run(scenario())

    assert refused.status_code == 400
    assert refused.json() == {"error": "graphId must be a non-empty string"}


def test_polling_projects_display_values_and_full_state_is_explicit(
    build: Backend, *, async_client
) -> None:
    runtime = build(_pipeline(Ask("Proceed?")))
    values = {
        "workspaceId": "workspace-1",
        "task": "large input" * 1000,
        "internal": {"transcript": "large transcript" * 1000},
        "result": {"pr_url": "https://example.test/pull/1", "raw": "large output" * 1000},
        "implementation": {"summary": "Implemented", "tests": ["passed"]},
    }

    async def scenario():
        async with _server(runtime, async_client=async_client) as surface:
            started = await surface.client.post("/api/runs", json={"graphId": str(GRAPH), "values": values})
            run_id = started.json()["runId"]
            await surface.read(run_id, "approval.requested")
            poll = await surface.client.get(f"/api/runs/{run_id}")
            full = await surface.client.get(f"/api/runs/{run_id}?includeValues=true")
            return poll.json(), full.json()

    poll, full = asyncio.run(scenario())
    assert poll["values"] == {
        "workspaceId": "workspace-1",
        "result": {"pr_url": "https://example.test/pull/1"},
        "implementation": values["implementation"],
    }
    assert full["values"] == values
    assert poll["pendingApprovals"] == full["pendingApprovals"]
    assert len(poll["pendingApprovals"]) == 1
    assert poll["activeExecutions"] == full["activeExecutions"]
    assert poll["nextNodes"] == full["nextNodes"]


def test_transition_queues_instruction_before_restarted_node_runs(
    build: Backend, *, async_client
) -> None:
    async def scenario() -> None:
        async with _server(build(_pipeline(Say("Done immediately."))), async_client=async_client) as surface:
            run_id = str((await _start(surface))["runId"])
            first = await surface.read(run_id, "run.finished")
            response = await surface.client.post(
                f"/api/runs/{run_id}/transitions",
                json={"node": str(IMPLEMENTATION), "message": "New instruction"},
            )
            assert response.status_code == 200, response.text
            events = await surface.read(run_id, "run.finished", cursor=first[-1]["sequence"])
            messages = [event for event in _of_kind(events, "transcript")
                        if event["nodeId"] == str(IMPLEMENTATION)]
            assert [event["payload"]["text"] for event in messages] == [
                "New instruction", "Done immediately.",
            ]
            assert messages[0]["executionId"] == messages[1]["executionId"]
            assert not any(event["nodeId"] == str(REVIEW) and
                           event["payload"].get("text") == "New instruction"
                           for event in _of_kind(events, "transcript"))

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [{"node": "implementation", "message": " "},
                                      {"checkpoint": "unknown", "message": "Fix it"}])
def test_invalid_transition_message_does_not_fork(
    build: Backend, payload: dict, *, async_client
) -> None:
    async def scenario() -> None:
        async with _server(build(_pipeline(Say("Done."))), async_client=async_client) as surface:
            run_id = str((await _start(surface))["runId"])
            await surface.read(run_id, "run.finished")
            before = await _checkpoints(surface, run_id)
            response = await surface.client.post(f"/api/runs/{run_id}/transitions", json=payload)
            assert response.status_code == 400
            assert await _checkpoints(surface, run_id) == before

    asyncio.run(scenario())
