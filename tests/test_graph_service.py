"""Registered graphs, runs, loops and steering, driven over real ACP sessions.

Every agent node here is a langgraph-acp session with `graph_service_agent.py`,
a child process, and every run goes through a `LangGraphRuntime` with durable
checkpoints -- the same path the daemon takes, minus HTTP and the WorkOrder row.
"""

import asyncio
import json
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from engine.domain import RunId, WorkspaceId
from engine.graph_runtime import RunStatus
from engine.graph_runtime_langgraph import (
    LangGraphRuntime,
    PullRequestRecord,
    SqliteGraphRuntimeStore,
    agent_registry,
)
from engine.cli import starters
from engine.graph_service import GraphError, GraphService, create_app, parse_graph
from engine.graph_service.service import Conflict, NotFound, ServiceError
from engine.ports import Workspace
from langgraph_acp import StdioACPProvider

AGENT = Path(__file__).parent / "graph_service_agent.py"
DATABASE = "graph-runs.sqlite3"

PAIR = """apiVersion: openengine.cc/v1
name: pair
description: Implement, then review.
inputs:
  tone: {default: plain}
implementation:
  implement:
    agent: stub
    prompt: do ${instruction} (${inputs.tone})
review:
  review:
    agent: stub
    prompt: review ${outputs.implement}
"""


def single(name: str, prompt: str) -> str:
    return yaml.safe_dump({
        "apiVersion": "openengine.cc/v1",
        "name": name,
        "implementation": {"work": {"agent": "stub", "prompt": prompt}},
    })


class Checkouts:
    """A workspace provider that hands every run the same empty directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def provision(self, repository: str, base_ref: str, *, co_author: str = "") -> Workspace:
        self.root.mkdir(exist_ok=True)
        return Workspace(
            workspace_id=WorkspaceId("ws-test"), root_path=str(self.root),
            repository=repository, base_ref=base_ref, ref="engine/ws-test",
        )


class Forge:
    """Just enough source control for a node to be served the repository tools."""

    async def run_git(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("not called")

    async def request_review(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("not called")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@asynccontextmanager
async def graph_service(
    tmp_path: Path, *, clock: Clock | None = None, cost: str = "0.5", **options: Any,
) -> AsyncIterator[GraphService]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    def stub(name: str) -> StdioACPProvider:
        return StdioACPProvider(name=name, command=[sys.executable, str(AGENT)], env={"STUB_COST": cost})

    registry = agent_registry([stub("stub")])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver:
        store = SqliteGraphRuntimeStore(tmp_path / DATABASE)
        runtime = LangGraphRuntime(store=store, checkpointer=saver, source_control=Forge())
        service = GraphService(
            runtime, tmp_path / DATABASE,
            workspace_provider=Checkouts(tmp_path / "checkout"),
            registry=registry,
            default_repository="example/repo",
            agent_factory=lambda row: stub(row.name),
            clock=clock,
            **options,
        )
        runtime.observe(service.observe)
        await service.open(schedule=False)
        try:
            yield service
        finally:
            await service.aclose()
            await runtime.aclose()
            store.close()


async def settled(service: GraphService, run_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        run = await service.run_json(run_id)
        if run["terminal"]:
            return run
        assert asyncio.get_running_loop().time() < deadline, run
        await asyncio.sleep(0.05)


async def paused(service: GraphService, loop_id: str, timeout: float = 30.0) -> dict[str, Any]:
    """The loop once paused; the service settles a loop just after its run turns terminal."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        loop = await service.loop_json(loop_id)
        if loop["state"] == "paused":
            return loop
        assert asyncio.get_running_loop().time() < deadline, loop
        await asyncio.sleep(0.05)


async def running_execution(service: GraphService, run_id: str, node: str) -> dict[str, Any]:
    """The node's attempt, once its agent is mid-turn.

    Steering that arrives before the first turn starts is held until that
    turn ends, so these tests wait for the turn the steering is meant to cut.
    """
    for _ in range(600):
        for execution in (await service.run_json(run_id))["nodes"]:
            if execution["node"] == node and execution["status"] == "running" and any(
                event.kind.value == "transcript"
                and event.payload.get("role") == "user"
                and event.execution_id == execution["executionId"]
                for event in service.runtime.store.events_since(RunId(run_id))
            ):
                return execution
        await asyncio.sleep(0.05)
    raise AssertionError(f"{node} never started")


# --- graph validation ---------------------------------------------------------


def test_a_graph_reports_every_problem_at_once() -> None:
    with pytest.raises(GraphError) as raised:
        parse_graph(
            {
                "apiVersion": "openengine.cc/v1",
                "name": "Bad Name",
                "implementation": {
                    "a": {"agent": "stub", "prompt": "go"},
                    "b": {"agent": "other", "prompt": "go"},
                    "workspace": {"agent": "stub", "prompt": "x"},
                },
                "flow": ["a -> nowhere"],
                "loop": {"every": "5s"},
            },
            runners=["stub"],
        )
    paths = {problem.path for problem in raised.value.problems}
    assert {"name", "implementation.b.agent", "implementation.workspace", "flow[0]", "loop.every"} <= paths

    # Structure and references are checked after the individual fields are valid.
    cyclic = yaml.safe_load(PAIR)
    cyclic["flow"] = ["start -> implement", "implement -> review", "review -> implement"]
    with pytest.raises(GraphError, match="loops with no route out"):
        parse_graph(cyclic)
    with pytest.raises(GraphError, match="inputs.missing: not a declared input"):
        parse_graph(yaml.safe_load(single("solo", "${inputs.missing}")))


def test_the_example_graphs_parse() -> None:
    examples = sorted((Path(__file__).parents[1] / "docs/examples/graphs").glob("*.yaml"))
    assert examples
    for example in examples:
        parse_graph(yaml.safe_load(example.read_text()), runners=["claude", "codex"])


def test_a_prompt_may_only_read_nodes_that_can_run_before_it() -> None:
    graph = {
        "apiVersion": "openengine.cc/v1",
        "name": "fan",
        "implementation": {
            "a": {"agent": "stub", "prompt": "x"},
            "b": {"agent": "stub", "prompt": "${outputs.c}"},
            "c": {"agent": "stub", "prompt": "${outputs.a}"},
        },
        "flow": ["a -> [b, c]", "b -> end", "c -> end"],
    }
    with pytest.raises(GraphError, match="c cannot have run before b"):
        parse_graph(graph)


def test_the_instruction_is_builtin_and_runners_must_exist() -> None:
    parsed = parse_graph(yaml.safe_load(single("solo", "${instruction}")))
    assert parsed.inputs == ()
    assert parsed.node("work").prompt == "${instruction}"
    with pytest.raises(GraphError, match="not configured on this backend"):
        parse_graph(yaml.safe_load(single("solo", "x")), runners=["codex"])


def test_a_runner_input_is_not_checked_at_registration() -> None:
    parse_graph(yaml.safe_load(starters.source("adversarial-review")), runners=["codex"])


# --- graphs and runs ----------------------------------------------------------


def test_registering_discovering_and_running_a_graph(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            graph, created = await service.add_graph("default", source=PAIR)
            assert created and graph["version"] == 1 and graph["graphId"].startswith("g-")
            again, created = await service.add_graph("default", source=PAIR)
            assert not created and again["versionId"] == graph["versionId"]
            assert [item["name"] for item in service.list_graphs("default")] == ["pair"]
            assert service.get_graph("default", "pair")["source"] == PAIR

            run, created = await service.submit_run(
                project="default", graph="pair", instruction="the thing", idempotency_key="k1",
            )
            assert created and run["versionId"] == graph["versionId"]
            replayed, created = await service.submit_run(
                project="default", graph="pair", instruction="the thing", idempotency_key="k1",
            )
            assert not created and replayed["runId"] == run["runId"]
            with pytest.raises(Conflict, match="different run request"):
                await service.submit_run(
                    project="default", graph="pair", instruction="else", idempotency_key="k1",
                )

            done = await settled(service, run["runId"])
            assert done["status"] == "completed"
            assert done["results"] == {
                "implement": "echo: do the thing (plain)",
                "review": "echo: review echo: do the thing (plain)",
            }
            nodes = {node["node"]: node for node in done["nodes"]}
            assert nodes["implement"]["status"] == "completed"
            assert nodes["implement"]["runner"] == "stub" and nodes["implement"]["attempt"] == 1
            assert done["usage"]["costUsd"] == pytest.approx(1.0)
            assert await service.list_nodes(run["runId"]) == done["nodes"]

    asyncio.run(scenario())


def test_a_built_in_graph_registers_without_its_default_agent(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            _, created = await service.add_graph("default", source=starters.source("adversarial-review"))
            assert created
            with pytest.raises(ServiceError, match=r"agent 'claude' is not available .*available: stub.*--agent"):
                await service.submit_run(
                    project="default", graph="adversarial-review", instruction="x",
                    inputs={"branch": "feat/x"},
                )
            run, created = await service.submit_run(
                project="default", graph="adversarial-review", instruction="x",
                inputs={"branch": "feat/x", "agent": "stub"},
            )
            assert created and run["runId"]

    asyncio.run(scenario())


def test_a_new_version_does_not_change_a_run_already_pinned(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            first, _ = await service.add_graph("default", source=single("solo", "one ${instruction}"))
            run, _ = await service.submit_run(project="default", graph="solo", instruction="x")
            second, created = await service.add_graph("default", source=single("solo", "two ${instruction}"))
            assert created and second["version"] == 2 and second["graphId"] == first["graphId"]
            assert (await settled(service, run["runId"]))["results"] == {"work": "echo: one x"}
            assert service.get_graph("default", "solo@1")["versionId"] == first["versionId"]
            later, _ = await service.submit_run(project="default", graph="solo", instruction="x")
            assert (await settled(service, later["runId"]))["results"] == {"work": "echo: two x"}

    asyncio.run(scenario())


def test_names_resolve_within_a_project_and_ambiguity_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("alpha", source=single("solo", "x"))
            await service.add_graph("beta", source=single("solo", "y"))
            assert service.get_graph("alpha", "solo")["project"] == "alpha"
            with pytest.raises(Conflict, match="ambiguous"):
                service.get_graph(None, "solo")
            with pytest.raises(NotFound):
                service.get_graph("gamma", "solo")

    asyncio.run(scenario())


def test_registered_graphs_survive_a_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("solo", "${instruction}"))
        async with graph_service(tmp_path) as service:
            run, _ = await service.submit_run(project="default", graph="solo", instruction="hi")
            assert (await settled(service, run["runId"]))["results"] == {"work": "echo: hi"}

    asyncio.run(scenario())


def test_a_runner_without_credentials_fails_with_a_signin_instruction(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("locked", "AUTH ${instruction}"))
            run, _ = await service.submit_run(project="default", graph="locked", instruction="x")
            failed = await settled(service, run["runId"])
            assert failed["status"] == "failed"
            assert failed["failure"]["node"] == "work"
            assert failed["failure"]["authRequired"]["command"] == "engine agent signin stub"

    asyncio.run(scenario())


def test_runs_list_newest_first_whoever_started_them(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with graph_service(tmp_path, clock=clock) as service:
            await service.add_graph("default", source=single("solo", "${instruction}"))
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            await service.add_graph("other", source=single("solo", "${instruction}"))
            first, _ = await service.submit_run(project="default", graph="solo", instruction="one")
            await settled(service, first["runId"])
            clock.advance(minutes=1)
            loop = await service.add_loop(project="default", graph="slow", instruction="x", every="1h")
            await service.tick()
            looped = (await service.loop_json(loop["loopId"]))["activeRunId"]
            clock.advance(minutes=1)
            elsewhere, _ = await service.submit_run(project="other", graph="solo", instruction="two")

            runs = await service.list_runs("default")
            assert [run["runId"] for run in runs] == [looped, first["runId"]]
            assert runs[0]["loop"] == "slow" and runs[0]["status"] == "running"
            assert runs[1]["graph"] == "solo" and runs[1]["loopId"] is None
            assert runs[1]["status"] == "completed" and runs[1]["usage"]["costUsd"] == pytest.approx(0.5)
            assert [run["runId"] for run in await service.list_runs(None)] == [
                elsewhere["runId"], looped, first["runId"],
            ]
            assert [run["runId"] for run in await service.list_runs(None, limit=1)] == [elsewhere["runId"]]
            assert [run["runId"] for run in await service.list_runs("default", graph="solo")] == [first["runId"]]
            assert [run["runId"] for run in await service.list_runs("default", loop="slow")] == [looped]
            assert [run["runId"] for run in await service.list_runs("default", status="completed")] == [
                first["runId"],
            ]
            with pytest.raises(ServiceError, match="unknown status"):
                await service.list_runs("default", status="done")
            with pytest.raises(Conflict, match="ambiguous"):
                await service.list_runs(None, graph="solo")
            await service.runtime.cancel(RunId(looped))
            await settled(service, looped)

    asyncio.run(scenario())


def test_runs_of_a_version_that_no_longer_loads_are_left_out(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("solo", "${instruction}"))
            await service.add_graph("default", source=single("kept", "${instruction}"))
            stale, _ = await service.submit_run(project="default", graph="solo", instruction="one")
            await settled(service, stale["runId"])
            kept, _ = await service.submit_run(project="default", graph="kept", instruction="two")
            await settled(service, kept["runId"])
        # A version stored under a graph language the daemon no longer parses.
        with sqlite3.connect(tmp_path / DATABASE) as connection:
            connection.execute(
                "UPDATE cli_graph_versions SET source = replace(source, 'openengine.cc/v1', 'openengine.dev/v1') "
                "WHERE version_id = ?", (stale["versionId"],),
            )
        async with graph_service(tmp_path) as service:
            assert [run["runId"] for run in await service.list_runs("default")] == [kept["runId"]]
            with pytest.raises(NotFound, match="which this runtime does not have"):
                await service.run_json(stale["runId"])

    asyncio.run(scenario())


def test_runs_are_refused_without_their_required_inputs(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=PAIR)
            with pytest.raises(ServiceError, match="unknown workflow inputs"):
                await service.submit_run(project="default", graph="pair", instruction="x", inputs={"nope": "1"})
            with pytest.raises(ServiceError, match="instruction is required"):
                await service.submit_run(project="default", graph="pair", instruction=" ")

    asyncio.run(scenario())


# --- steering -----------------------------------------------------------------


def test_steering_reaches_the_running_attempt_and_is_applied(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            run, _ = await service.submit_run(project="default", graph="slow", instruction="x")
            execution = await running_execution(service, run["runId"], "work")
            steering, created = await service.steer(execution["executionId"], "finish now", "s1")
            assert created and steering["status"] in ("accepted", "delivered", "applied")
            again, created = await service.steer(execution["executionId"], "finish now", "s1")
            assert not created and again["steeringId"] == steering["steeringId"]

            done = await settled(service, run["runId"])
            assert done["results"] == {"work": "echo: finish now"}
            node = await service.get_node(execution["executionId"])
            [record] = node["steering"]
            assert record["status"] == "applied"
            assert record["deliveredAt"] and record["appliedAt"]
            with pytest.raises(Conflict, match="only a running attempt"):
                await service.steer(execution["executionId"], "too late", "s2")

    asyncio.run(scenario())


def test_steering_is_not_applied_by_merely_being_queued(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            run, _ = await service.submit_run(project="default", graph="slow", instruction="x")
            execution = await running_execution(service, run["runId"], "work")
            # A steered turn that is itself cancelled was delivered, never applied.
            await service.steer(execution["executionId"], "WAIT more", "s1")
            for _ in range(200):
                [record] = (await service.get_node(execution["executionId"]))["steering"]
                if record["status"] == "delivered":
                    break
                await asyncio.sleep(0.05)
            assert record["status"] == "delivered" and record["appliedAt"] is None
            await service.runtime.cancel(RunId(run["runId"]))
            await settled(service, run["runId"])
            [record] = (await service.get_node(execution["executionId"]))["steering"]
            assert record["status"] == "delivered"
            assert (await service.get_node(execution["executionId"]))["status"] == "cancelled"

    asyncio.run(scenario())


# --- loops --------------------------------------------------------------------


def test_a_loop_needs_a_cadence_and_never_overlaps_its_runs(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with graph_service(tmp_path, clock=clock) as service:
            await service.add_graph("default", source=single("slow", "WAIT ${instruction}"))
            with pytest.raises(ServiceError, match="no cadence"):
                await service.add_loop(project="default", graph="slow", instruction="x")
            loop = await service.add_loop(project="default", graph="slow", instruction="x", every="1h")
            assert loop["nextRunAt"] == clock.now.isoformat(timespec="seconds")

            await asyncio.gather(service.tick(), service.tick())
            loop = await service.loop_json(loop["loopId"])
            first = loop["activeRunId"]
            assert first and loop["runs"] == 1

            clock.advance(hours=3)
            await service.tick()
            loop = await service.loop_json(loop["loopId"])
            assert loop["activeRunId"] == first and loop["runs"] == 1

            await service.runtime.cancel(RunId(first))
            await settled(service, first)
            await service.tick()
            loop = await service.loop_json(loop["loopId"])
            assert loop["runs"] == 2 and loop["activeRunId"] != first
            # The three ticks missed while the first run worked became one.
            assert loop["nextRunAt"] == (clock.now + timedelta(hours=1)).isoformat(timespec="seconds")
            await service.runtime.cancel(RunId(loop["activeRunId"]))

    asyncio.run(scenario())


def test_a_loop_shows_the_output_of_its_latest_completed_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with graph_service(tmp_path, clock=clock) as service:
            await service.add_graph("default", source=PAIR)
            # Each run spends $1.00, so the cap lets the first finish and stops the second.
            loop = await service.add_loop(
                project="default", graph="pair", instruction="it", every="1h", max_spend_usd=1.4,
            )
            assert (await service.loop_json(loop["loopId"]))["latestOutput"] is None

            await service.tick()
            first = (await service.loop_json(loop["loopId"]))["activeRunId"]
            run = await settled(service, first)
            assert run["output"]["node"] == "review"
            assert run["output"]["value"] == "echo: review echo: do it (plain)"

            # A run that does not complete leaves the last completed output in place.
            clock.advance(hours=1)
            await service.tick()
            second = (await service.loop_json(loop["loopId"]))["activeRunId"]
            assert second != first
            stopped = await settled(service, second)
            assert stopped["status"] == "failed" and stopped["output"] is None

            latest = (await service.loop_json(loop["loopId"]))["latestOutput"]
            assert latest == {"runId": first, **run["output"]}

    asyncio.run(scenario())


def test_reaching_max_spend_stops_the_run_and_survives_a_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with graph_service(tmp_path, clock=clock) as service:
            await service.add_graph("default", source=PAIR)
            loop = await service.add_loop(
                project="default", graph="pair", instruction="x", every="1h", max_spend_usd=0.4,
            )
            assert loop["limits"]["spendScope"]
            await service.tick()
            run_id = (await service.loop_json(loop["loopId"]))["activeRunId"]
            run = await settled(service, run_id)
            assert run["status"] == "failed"  # cancelled once the cap was reached
            assert "review" not in run["results"]
        async with graph_service(tmp_path, clock=clock) as service:
            loop = await service.loop_json(loop["loopId"])
            assert loop["state"] == "paused" and loop["pauseReason"].startswith("max-spend reached")
            assert loop["spend"]["usd"] == pytest.approx(0.5)
            clock.advance(hours=2)
            await service.tick()
            assert (await service.loop_json(loop["loopId"]))["runs"] == 1
            with pytest.raises(Conflict, match="raise the limit"):
                await service.resume_loop("default", "pair")
            resumed = await service.resume_loop("default", "pair", max_spend_usd=10)
            assert resumed["state"] == "active" and resumed["limits"]["maxSpendUsd"] == 10

    asyncio.run(scenario())


def test_reaching_max_prs_pauses_the_loop(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path, clock=Clock()) as service:
            await service.add_graph("default", source=single("solo", "${instruction}"))
            loop = await service.add_loop(
                project="default", graph="solo", instruction="x", every="1h", max_prs=1,
            )
            await service.tick()
            run_id = (await service.loop_json(loop["loopId"]))["activeRunId"]
            await service.runtime.store.remember_pull_request(
                PullRequestRecord("example/repo", 7, RunId(run_id), "2026-10-05T12:00:00+00:00")
            )
            await settled(service, run_id)
            loop = await paused(service, loop["loopId"])
            assert loop["prCount"] == 1
            assert loop["pauseReason"].startswith("max-prs reached")

    asyncio.run(scenario())


def test_a_loop_pauses_when_its_runner_needs_signing_in(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path, clock=Clock()) as service:
            await service.add_graph("default", source=single("locked", "AUTH ${instruction}"))
            loop = await service.add_loop(project="default", graph="locked", instruction="x", every="1h")
            await service.tick()
            await settled(service, (await service.loop_json(loop["loopId"]))["activeRunId"])
            loop = await paused(service, loop["loopId"])
            assert "engine agent signin stub" in loop["pauseReason"]

    asyncio.run(scenario())


# --- sessions -----------------------------------------------------------------


async def mcp_tools(server: dict[str, Any]) -> list[str]:
    """The tools a client launching `server` the way claude does is offered."""
    process = await asyncio.create_subprocess_exec(
        server["command"], *server["args"],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        for number, method in enumerate(("initialize", "tools/list"), start=1):
            params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
            message = {"jsonrpc": "2.0", "id": number, "method": method, "params": params if number == 1 else {}}
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()
            response = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        return [tool["name"] for tool in response["result"]["tools"]]
    finally:
        process.kill()
        await process.wait()


def test_a_session_is_a_run_whose_implementation_node_is_the_callers_cli(tmp_path: Path) -> None:
    from engine.runtime.config import ApprovalConfig, BashApprovalConfig

    policy = ApprovalConfig(bash=BashApprovalConfig(deny=("git push **", "rm *.txt")))

    async def scenario() -> None:
        async with graph_service(tmp_path, session_tools=("git_subcommand",), approval_policy=policy) as service:
            started = await service.start_session(agent="claude", repository="example/repo", base_ref="origin/dev")
            assert started["status"] == "starting" and started["runId"]
            deadline = asyncio.get_running_loop().time() + 30
            while (session := service.session_json(started["sessionId"]))["status"] == "starting":
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.05)
            assert session["status"] == "ready", session
            assert session["workspace"]["path"] == str(tmp_path / "checkout")
            arguments = session["mcp"]["args"]
            assert arguments[arguments.index("--repository-tool") + 1] == "git_subcommand"
            assert "open_pull_request" not in arguments
            assert session["settings"] == {"permissions": {"deny": ["Bash(git push:*)"]}}
            assert "git_subcommand" in session["instructions"]
            assert "git_subcommand" in await mcp_tools(session["mcp"])

            ended = await service.end_session(started["sessionId"], "added the thing")
            assert ended["status"] == "ended"
            run = await settled(service, started["runId"])
            assert run["status"] == "completed" and run["results"]["implement"] == "added the thing"
            with pytest.raises(NotFound):
                service.session_json("s-missing")
            with pytest.raises(ServiceError, match="agent must be"):
                await service.start_session(agent="gemini", repository="example/repo")

    asyncio.run(scenario())


def test_each_session_agent_is_told_engines_shell_rules_in_its_own_terms() -> None:
    from engine.graph_service.session import agent_settings
    from engine.runtime.config import ApprovalConfig, BashApprovalConfig

    policy = ApprovalConfig(bash=BashApprovalConfig(deny=("git push **", "gh pr create", "rm *.txt")))
    assert agent_settings("claude", policy) == {"permissions": {"deny": ["Bash(git push:*)", "Bash(gh pr create)"]}}
    assert agent_settings("opencode", policy) == {
        "permission": {"bash": {"git push": "deny", "git push *": "deny", "gh pr create": "deny"}},
    }
    assert agent_settings("codex", policy) == {}
    assert agent_settings("opencode", ApprovalConfig(bash=BashApprovalConfig(deny=()))) == {}


# --- agents -------------------------------------------------------------------


def test_an_added_agent_runs_graphs_and_outlives_the_daemon(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            added = await service.add_agent("claude", name="reviewer", model="opus")
            assert added["kind"] == "claude" and added["model"] == "opus" and not added["builtin"]
            assert [agent["name"] for agent in service.agents_json()] == ["reviewer", "stub"]
            assert service.agent_json("stub")["builtin"]
            with pytest.raises(Conflict, match="already exists"):
                await service.add_agent("claude", name="reviewer")
            changed = await service.add_agent("codex", name="reviewer", model="", overwrite=True)
            assert changed["kind"] == "codex" and changed["createdAt"] == added["createdAt"]

            await service.add_graph("default", source=single("solo", "check ${instruction}").replace("agent: stub", "agent: reviewer"))
            run, _ = await service.submit_run(project="default", graph="solo", instruction="it", idempotency_key="r1")
            done = await settled(service, run["runId"])
            runners = {node["node"]: node["runner"] for node in done["nodes"]}
            assert done["status"] == "completed" and runners["work"] == "reviewer"

        async with graph_service(tmp_path) as service:
            assert service.agent_json("reviewer")["kind"] == "codex"
            assert "reviewer" in service.runners()
            removed = await service.remove_agent("reviewer")
            assert removed["removed"] and "reviewer" not in service.runners()
            with pytest.raises(NotFound):
                service.agent_json("reviewer")
            with pytest.raises(Conflict, match="built in"):
                await service.remove_agent("stub")

    asyncio.run(scenario())


@pytest.mark.parametrize(("arguments", "refusal"), [
    ({"kind": "gemini"}, "kind must be one of"),
    ({"kind": "claude", "name": "Bad Name"}, "lowercase"),
    ({"kind": "claude", "name": "round-robin"}, "runner policy"),
    ({"kind": "claude", "name": "q", "url": "http://localhost:11434/v1", "model": "m"}, "only an opencode"),
    ({"kind": "opencode", "name": "q", "url": "http://localhost:11434/v1"}, "needs a model"),
    ({"kind": "opencode", "name": "q", "url": "ftp://box", "model": "m"}, "http"),
    ({"kind": "opencode", "name": "q", "url": "http://gpu:8000/v1?api_key=sk-1", "model": "m"}, "api key"),
    ({"kind": "opencode", "name": "q", "url": "http://gpu:8000/v1?x=1&API-Key=sk-1", "model": "m"}, "api key"),
    ({"kind": "opencode", "name": "q", "url": "https://gpu/v1?access_token=t", "model": "m"}, "api key"),
    ({"kind": "claude", "name": "stub"}, "built in"),
])
def test_an_agent_that_cannot_run_is_refused(tmp_path: Path, arguments: dict[str, str], refusal: str) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            kind = arguments.pop("kind")
            with pytest.raises(ServiceError, match=refusal):
                await service.add_agent(kind, **arguments)
            assert [agent["name"] for agent in service.agents_json()] == ["stub"]

    asyncio.run(scenario())


def test_a_refused_query_string_key_is_not_repeated(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            with pytest.raises(ServiceError) as refused:
                await service.add_agent("opencode", name="q", model="m", url="http://gpu:8000/v1?key=sk-secret")
            assert "sk-secret" not in repr(refused.value) and "sk-secret" not in str(vars(refused.value))

    asyncio.run(scenario())


def test_a_query_string_without_a_key_is_accepted(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            added = await service.add_agent("opencode", name="q", model="m", url="http://gpu:8000/v1?region=us")
            assert added["url"] == "http://gpu:8000/v1?region=us"

    asyncio.run(scenario())


def test_an_opencode_agent_on_a_model_server_gets_its_own_provider() -> None:
    from engine.graph_service.service import opencode_config, session_model
    from engine.graph_service.store import AgentRow

    row = AgentRow("qwen", "opencode", "qwen3-coder", "http://gpu.local:8000/v1", "", "")
    assert session_model(row) == "engine/qwen3-coder"
    assert opencode_config(row) == {
        "model": "engine/qwen3-coder",
        "provider": {"engine": {
            "npm": "@ai-sdk/openai-compatible", "name": "qwen",
            "options": {"baseURL": "http://gpu.local:8000/v1"},
            "models": {"qwen3-coder": {"name": "qwen3-coder"}},
        }},
    }
    assert opencode_config(AgentRow("o", "opencode", "", "", "", "")) == {}


def test_an_added_agents_model_applies_when_a_node_names_none_or_an_unknown_tier() -> None:
    from engine.graph_service.compile import GraphAgentNode
    from engine.graph_service.language import RunnerRule

    def node(model: str) -> GraphAgentNode:
        return GraphAgentNode(
            agent="qwen", prompt="p", cwd=".", state_key="k", rule=RunnerRule("literal", "qwen"), model_template=model,
            tiers={"claude": {"default": "sonnet"}}, agent_models={"qwen": "engine/qwen3"},
        )

    assert node("").model_for({}, "qwen") == "engine/qwen3"
    assert node("default").model_for({}, "qwen") == "engine/qwen3"
    assert node("other-model").model_for({}, "qwen") == "other-model"
    assert node("default").model_for({}, "claude") == "sonnet"


# --- HTTP ---------------------------------------------------------------------


def test_the_http_surface_reports_validation_problems_and_conflicts(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with graph_service(tmp_path) as service:
            transport = httpx.ASGITransport(app=create_app(service))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                refused = await client.post("/graphs", json={"source": "apiVersion: openengine.cc/v1\nname: x\nimplementation: {}\n"})
                assert refused.status_code == 400
                assert refused.json()["problems"][0]["path"] == "implementation"
                added = await client.post("/graphs", json={"project": "p", "source": PAIR})
                assert added.status_code == 201
                assert (await client.post("/graphs", json={"project": "p", "source": PAIR})).status_code == 200
                listed = await client.get("/graphs", params={"project": "p"})
                assert [graph["name"] for graph in listed.json()["graphs"]] == ["pair"]
                assert (await client.get("/graphs/pair@1", params={"project": "p"})).json()["version"] == 1
                assert (await client.get("/runs/run-missing")).status_code == 404
                assert (await client.get("/runs", params={"project": "p"})).json() == {"runs": []}
                assert (await client.get("/runs", params={"limit": "many"})).status_code == 400
                backend = (await client.get("/backend")).json()
                assert backend["runners"] == ["stub"] and backend["execution"] == "langgraph-acp"
                added = await client.post("/agents", json={"kind": "claude", "name": "second", "model": "opus"})
                assert added.status_code == 201 and added.json()["name"] == "second"
                assert (await client.post("/agents", json={"kind": "claude", "name": "second"})).status_code == 409
                assert (await client.post("/agents", json={"kind": "claude", "replace": "yes"})).status_code == 400
                assert [agent["name"] for agent in (await client.get("/agents")).json()["agents"]] == ["second", "stub"]
                assert (await client.get("/agents/second")).json()["model"] == "opus"
                assert (await client.delete("/agents/second")).json()["removed"]
                assert (await client.get("/agents/second")).status_code == 404

    asyncio.run(scenario())


# --- through the daemon -------------------------------------------------------


def test_the_daemon_serves_the_graph_api_and_lists_its_runs_as_work_orders(tmp_path: Path) -> None:
    """`engine-web` mounts the service at /api/v1 and starts runs as WorkOrders."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from engine.adapters.state_store.memory import InMemoryStateStore
    from test_web_app import ConcurrentRunner, _workflow_app

    async def scenario() -> None:
        registry = agent_registry([
            StdioACPProvider(name="stub", command=[sys.executable, str(AGENT)]),
        ])
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver:
            store = SqliteGraphRuntimeStore(tmp_path / DATABASE)
            runtime = LangGraphRuntime(store=store, checkpointer=saver)

            @asynccontextmanager
            async def running() -> AsyncIterator[LangGraphRuntime]:
                try:
                    yield runtime
                finally:
                    await runtime.aclose()

            def factory(runtime: LangGraphRuntime, start: Any) -> GraphService:
                return GraphService(
                    runtime, tmp_path / DATABASE,
                    workspace_provider=Checkouts(tmp_path / "checkout"),
                    registry=registry, start=start, default_repository="example/repo",
                )

            app = _workflow_app(
                InMemoryStateStore(), ConcurrentRunner(), graph_runtime=running(), graph_service=factory,
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                async with app.router.lifespan_context(app):
                    added = await client.post("/api/v1/graphs", json={"source": PAIR})
                    assert added.status_code == 201, added.text
                    run = await client.post("/api/v1/runs", json={
                        "graph": "pair", "instruction": "it", "idempotencyKey": "k",
                    })
                    assert run.status_code == 201, run.text
                    run_id = run.json()["runId"]
                    for _ in range(600):
                        body = (await client.get(f"/api/v1/runs/{run_id}")).json()
                        if body["terminal"]:
                            break
                        await asyncio.sleep(0.05)
                    assert body["status"] == "completed", body
                    assert body["results"]["review"] == "echo: review echo: do it (plain)"
                    listed = (await client.get("/api/runs")).json()["runs"]
                    assert [item["runId"] for item in listed] == [run_id]
            store.close()

    asyncio.run(scenario())
