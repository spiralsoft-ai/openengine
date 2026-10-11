"""A scripted stand-in for the graph the control surface drives.

Not a mock of the control surface: a real `GraphRuntime`, with real asyncio
tasks, real supersteps that fan out, a real execution that waits for permission
nobody has given yet, and a real execution that takes an instruction while it is
still running. What it does not have is LangGraph, so what each node "decides"
to do is a script instead.

That is the point of the split. The HTTP surface, its wire format, and its
behaviour under steering and resumption are all decided here and checked in
`tests/test_graph_runtime.py`; when the LangGraph binding lands it satisfies the
same protocol and the same tests run against it unchanged.

The important thing this fake models -- and the reason it is not simply a stub
-- is where control goes. Each task in flight registers a `ControllableExecution`
under its own `ExecutionId` and keeps it registered for the whole task: while it
works, while it waits on an approval, and while somebody redirects it. `steer()`
and `decide()` are routed to that object and the node's coroutine never stops or
restarts, which is exactly the lifecycle an `ACPNode` holding an `ACPSession`
has. A fake that implemented approvals by tearing the node down and running it
again would pass a weaker suite and prove nothing about the binding.

`ScriptedNode.tasks` is how the same node runs more than once at a time, which
is what LangGraph's `Send` does. Two tasks of one node are two executions with
two queues and two sets of open questions, and the fake would be misleading if
they shared either.

A node is a tuple of beats. `Say` and `Call` are things it does; `Ask` is a
pause on consent, taken by the execution rather than by the graph;
`AwaitSteering` is a point where it waits for an instruction and then carries
on; `Fail` raises, which is the one thing a real node does that none of the
others can.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import count

from engine.graph_runtime.inputs import WorkflowInput, mode_input

from engine.domain import ApprovalDecision, ApprovalId, ApprovalKind, RunId
from engine.graph_runtime import (
    CANCELLED,
    ActiveExecution,
    AmbiguousExecutionError,
    ApprovalNotPendingError,
    Checkpoint,
    CheckpointId,
    EventKind,
    EventObserver,
    ExecutionId,
    ExecutionRegistry,
    GraphEdge,
    GraphId,
    GraphNode,
    GraphTopology,
    NoSuchPositionError,
    NodeId,
    UnknownNodeError,
    PendingApproval,
    RunNotSteerableError,
    RunSnapshot,
    RunStatus,
    RuntimeEvent,
    UnknownApprovalError,
    UnknownCheckpointError,
    UnknownGraphError,
    UnknownRunError,
)


@dataclass(frozen=True, slots=True)
class Say:
    """The node adds one message to its transcript."""

    text: str
    role: str = "assistant"


@dataclass(frozen=True, slots=True)
class Call:
    """The node calls a tool and is given `result` back."""

    name: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    result: str = "ok"


@dataclass(frozen=True, slots=True)
class Ask:
    """The execution stops and waits for consent, staying alive to be told."""

    reason: str
    command: str = ""
    tool_name: str = ""
    kind: ApprovalKind = ApprovalKind.COMMAND_EXECUTION


@dataclass(frozen=True, slots=True)
class AwaitSteering:
    """The execution stops until somebody sends it a message."""


@dataclass(frozen=True, slots=True)
class Fail:
    """The node raises.

    The likeliest thing a real node does that none of the other beats can: an
    agent whose provider is out of quota, a tool that is not installed, a bug.
    Scripted so the contract has to say what a run does about it, rather than
    leaving the answer to whichever binding hits it first.
    """

    message: str


class ScriptedFailure(RuntimeError):
    """What a `Fail` beat raises. Nothing catches it by type."""


Beat = Say | Call | Ask | AwaitSteering | Fail


@dataclass(frozen=True, slots=True)
class ScriptedNode:
    node_id: NodeId
    beats: tuple[Beat, ...] = ()
    next_nodes: tuple[NodeId, ...] = ()
    """Every successor, run together as one superstep. Plural on purpose."""
    tasks: int = 1
    """How many concurrent executions of this node one superstep starts.

    LangGraph's `Send` fans several tasks into one node -- the same reviewer
    prompt over three candidate diffs, say. Each is its own execution with its
    own id, its own approvals and its own transcript, and none of them can be
    addressed by the node name they share.
    """
    name: str = ""
    kind: str = "agent"
    output_key: str = ""
    """State key a spoken answer is written to; the node id when empty."""
    always_open: bool = False
    """Whether steering to this node resets the graph when it is not executing."""


@dataclass(frozen=True, slots=True)
class ScriptedGraph:
    """A set of nodes. The first one is where a run begins."""

    graph_id: GraphId
    name: str
    nodes: tuple[ScriptedNode, ...]

    def node(self, node_id: NodeId) -> ScriptedNode | None:
        return next((node for node in self.nodes if node.node_id == node_id), None)

    def successors(self, frontier: Sequence[NodeId]) -> tuple[NodeId, ...]:
        """The next superstep: everything the frontier leads to, once each."""
        following: dict[NodeId, None] = {}
        for node_id in frontier:
            node = self.node(node_id)
            if node is not None:
                following.update(dict.fromkeys(node.next_nodes))
        return tuple(following)

    def topology(self) -> GraphTopology:
        return GraphTopology(
            graph_id=self.graph_id,
            name=self.name,
            entry_point=self.nodes[0].node_id,
            nodes=tuple(
                GraphNode(
                    node.node_id,
                    node.name or str(node.node_id),
                    node.kind,
                    always_open=node.always_open,
                )
                for node in self.nodes
            ),
            edges=tuple(
                GraphEdge(node.node_id, target)
                for node in self.nodes
                for target in node.next_nodes
            ),
        )


@dataclass(frozen=True, slots=True)
class _Outcome:
    """How one node in a superstep ended."""

    node_id: NodeId
    error: str = ""
    refused: bool = False
    """An approval was answered "no", which ends the run rather than the node."""


class _Run:
    """One execution of one scripted graph."""

    def __init__(self, run_id: RunId, graph: ScriptedGraph) -> None:
        self.run_id = run_id
        self.graph = graph
        self.status = RunStatus.RUNNING
        self.values: dict[str, object] = {}
        self.frontier: tuple[NodeId, ...] = ()
        """The superstep in flight, which is what `next_nodes` looks past."""
        self.checkpoints: list[Checkpoint] = []
        self.by_id: dict[CheckpointId, Checkpoint] = {}
        self.pending: dict[ApprovalId, PendingApproval] = {}
        self.answered: set[ApprovalId] = set()
        """Requests that have been resolved, so a repeat is a 409 and not a 404."""
        self.auto_approve_nodes: tuple[NodeId, ...] = ()
        self.runner_overrides: dict[NodeId, str] = {}
        self.error = ""
        self.executors: set[asyncio.Task[None]] = set()
        """Every task driving this run, which must never be more than one.

        A set rather than one slot, because a leaked executor has to be visible
        to be stopped: a second one started while the first was being cancelled
        would otherwise be reachable from nothing at all, and would go on
        writing into the checkpoints and values below.
        """
        self.control = asyncio.Lock()
        """Serialises the operations that stop and restart this run.

        Held for the whole of `resume_from`, because stopping is asynchronous:
        the guard is not a check but a stretch of time, and a request that
        arrived in the middle of it used to walk straight past.
        """

    @property
    def position(self) -> Checkpoint | None:
        return self.checkpoints[-1] if self.checkpoints else None

    def record(
        self,
        next_nodes: tuple[NodeId, ...],
        parent: CheckpointId | None,
        source: str,
        counter: count[int],
    ) -> Checkpoint:
        """Save a position. Appends -- a fork never replaces what it came from."""
        checkpoint = Checkpoint(
            checkpoint_id=CheckpointId(f"checkpoint-{next(counter)}"),
            parent_id=parent,
            next_nodes=next_nodes,
            values=dict(self.values),
            source=source,
        )
        self.checkpoints.append(checkpoint)
        self.by_id[checkpoint.checkpoint_id] = checkpoint
        return checkpoint

    def snapshot(self, active: tuple[ActiveExecution, ...]) -> RunSnapshot:
        position = self.position
        return RunSnapshot(
            run_id=self.run_id,
            graph_id=self.graph.graph_id,
            status=self._status(),
            active_executions=active,
            next_nodes=(
                self.graph.successors(self.frontier)
                if self.frontier
                else (position.next_nodes if position is not None else ())
            ),
            checkpoint_id=position.checkpoint_id if position is not None else None,
            values=dict(self.values),
            pending_approvals=tuple(self.pending.values()),
            error=self.error,
            auto_approve_nodes=self.auto_approve_nodes,
            runner_overrides=self.runner_overrides,
        )

    def _status(self) -> RunStatus:
        """Waiting on a person is a summary of the approvals, not a fourth state.

        Derived rather than assigned so a fan-out cannot contradict itself: with
        one reviewer waiting and two working, the run is both, and the honest
        answer is the one a list view can show plus the approvals underneath it.
        """
        if self.status is RunStatus.RUNNING and self.pending:
            return RunStatus.AWAITING_APPROVAL
        return self.status


class _Execution:
    """One task in flight, and the only thing external control reaches.

    Stands in for the session an agent node would be holding. It outlives every
    approval and every steering message: the node's coroutine is inside
    `ScriptedGraphRuntime._run_node` the whole time, and nothing here suspends
    or restarts it.

    One per task rather than per node: two tasks fanned into the same node are
    two of these, with two queues and two sets of open questions, so a message
    for one cannot be taken by the other.
    """

    def __init__(
        self,
        runtime: ScriptedGraphRuntime,
        run: _Run,
        execution_id: ExecutionId,
        node_id: NodeId,
    ) -> None:
        self._runtime = runtime
        self._run = run
        self.execution_id = execution_id
        self.node_id = node_id
        self._steering: asyncio.Queue[str] = asyncio.Queue()
        self._waiting: dict[ApprovalId, asyncio.Future[ApprovalDecision]] = {}

    # --- what the runtime routes to us -------------------------------------

    async def steer(self, message: str) -> None:
        self._steering.put_nowait(message)

    async def decide(
        self, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> None:
        waiting = self._waiting.get(approval_id)
        if waiting is None or waiting.done():
            raise ApprovalNotPendingError(
                f"approval is no longer pending: {approval_id}"
            )
        waiting.set_result(decision)

    # --- what the script does ----------------------------------------------

    async def ask(self, beat: Ask) -> ApprovalDecision:
        """Raise a request and wait, without going back to the graph."""
        approval = PendingApproval(
            approval_id=ApprovalId(f"approval-{next(self._runtime.ids)}"),
            execution_id=self.execution_id,
            node_id=self.node_id,
            kind=beat.kind,
            reason=beat.reason,
            command=beat.command,
            tool_name=beat.tool_name,
        )
        waiting: asyncio.Future[ApprovalDecision] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiting[approval.approval_id] = waiting
        self._run.pending[approval.approval_id] = approval
        automatic = self.node_id in self._run.auto_approve_nodes and beat.kind in (
            ApprovalKind.COMMAND_EXECUTION, ApprovalKind.FILE_CHANGE, ApprovalKind.TOOL_USE
        )
        await self._runtime.emit(
            self._run,
            EventKind.APPROVAL_REQUESTED,
            {
                "approvalId": str(approval.approval_id),
                "kind": approval.kind.value,
                "reason": approval.reason,
                "command": approval.command,
                "toolName": approval.tool_name,
                "autoApproved": automatic,
            },
            node_id=self.node_id,
            execution_id=self.execution_id,
        )
        try:
            if automatic:
                await self._runtime.decide(self._run.run_id, approval.approval_id, ApprovalDecision.ACCEPT)
            decision = await waiting
        finally:
            # `decide` clears the run's copy on the way in; this is for the run
            # being stopped or forked while the question is still open. Recorded
            # as answered either way, so a client still showing an abandoned
            # request is told it is no longer pending rather than that it never
            # existed.
            self._waiting.pop(approval.approval_id, None)
            self._run.pending.pop(approval.approval_id, None)
            self._run.answered.add(approval.approval_id)
        await self._runtime.emit(
            self._run,
            EventKind.APPROVAL_RESOLVED,
            {"approvalId": str(approval.approval_id), "decision": decision.value},
            node_id=self.node_id,
            execution_id=self.execution_id,
        )
        return decision

    async def next_message(self) -> str:
        return await self._steering.get()

    async def drain(self) -> None:
        """Take whatever has arrived without waiting for more."""
        while not self._steering.empty():
            await self.receive(self._steering.get_nowait())

    async def receive(self, message: str) -> None:
        received = self._run.values.get("steering")
        self._run.values["steering"] = [
            *(received if isinstance(received, list) else ()),
            message,
        ]
        await self._runtime.emit(
            self._run,
            EventKind.TRANSCRIPT,
            {"role": "user", "text": message},
            node_id=self.node_id,
            execution_id=self.execution_id,
        )


class ScriptedGraphRuntime:
    """A `GraphRuntime` whose nodes follow a script instead of a model."""

    def __init__(self, *graphs: ScriptedGraph) -> None:
        self._graphs = {graph.graph_id: graph for graph in graphs}
        self._runs: dict[RunId, _Run] = {}
        self._observer: EventObserver | None = None
        self._executions = ExecutionRegistry()
        self._entries: dict[NodeId, int] = {}
        self._pending_steers: dict[RunId, dict[NodeId, list[str]]] = {}
        self.ids = count(1)

    # --- the control surface's contract ------------------------------------

    def observe(self, observer: EventObserver) -> None:
        self._observer = observer

    def graphs(self) -> tuple[GraphTopology, ...]:
        return tuple(graph.topology() for graph in self._graphs.values())

    def topology(self, graph_id: GraphId) -> GraphTopology | None:
        graph = self._graphs.get(graph_id)
        return graph.topology() if graph is not None else None

    async def start(
        self, graph_id: GraphId, values: Mapping[str, object], *, run_id: RunId | None = None
    ) -> RunSnapshot:
        graph = self._graphs.get(graph_id)
        if graph is None:
            raise UnknownGraphError(f"unknown graph: {graph_id}")
        run_id = run_id or RunId(f"run-{next(self.ids)}")
        if run_id in self._runs:
            raise ValueError(f"run already exists: {run_id}")
        run = _Run(run_id, graph)
        run.values.update(values)
        self._runs[run.run_id] = run
        await self.emit(run, EventKind.RUN_STARTED, {"values": dict(run.values)})
        opening = run.record(
            (graph.nodes[0].node_id,), parent=None, source="start", counter=self.ids
        )
        await self._emit_checkpoint(run, opening)
        self._drive(run, opening)
        return self._snapshot(run)

    async def snapshot(self, run_id: RunId) -> RunSnapshot | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        if run.graph.graph_id not in self._graphs:
            raise UnknownGraphError(
                f"run {run_id} is of graph {run.graph.graph_id}, which this "
                "runtime does not have"
            )
        return self._snapshot(run)

    def withdraw(self, graph_id: GraphId) -> None:
        """Stop defining a graph, keeping the runs already made of it.

        What a deployment does when a workflow leaves its directory, or is
        renamed without retiring the id it had: the runs are in the store and
        the graph is not, so nothing can say where they got to. Modelled here
        because it is the state an old WorkOrder is in, and every reader of one
        has to survive it.
        """
        self._graphs.pop(graph_id, None)

    async def history(self, run_id: RunId) -> tuple[Checkpoint, ...]:
        return tuple(self._require(run_id).checkpoints)

    async def resume_from(
        self, run_id: RunId, checkpoint_id: CheckpointId,
        *, node_id: NodeId | None = None, message: str | None = None,
    ) -> RunSnapshot:
        run = self._require(run_id)
        origin = run.by_id.get(checkpoint_id)
        if origin is None:
            raise UnknownCheckpointError(f"unknown checkpoint: {checkpoint_id}")
        # Held across the stop *and* the restart. `_stop` awaits a cancellation,
        # so everything between here and the new executor is a window a second
        # request would otherwise arrive in -- and it would find nothing in
        # flight to stop, because the first caller had already let go of it.
        async with run.control:
            if (node_id is None) != (message is None):
                raise ValueError("give both node_id and message")
            if message is not None and (not message.strip() or node_id not in origin.next_nodes):
                raise ValueError("message requires a node in the checkpoint frontier")
            await self._stop(run)
            # The state that position held, not whatever a later node concluded
            # -- and appended as a child of it, so the attempt being replaced is
            # still in `history()` for anyone auditing how the run got here.
            run.values = dict(origin.values)
            run.error = ""
            run.status = RunStatus.RUNNING
            run.frontier = ()
            forked = run.record(
                origin.next_nodes,
                parent=checkpoint_id,
                source="fork",
                counter=self.ids,
            )
            await self.emit(
                run,
                EventKind.RUN_FORKED,
                {
                    "from": str(checkpoint_id),
                    "checkpointId": str(forked.checkpoint_id),
                    "nodes": [str(node_id) for node_id in forked.next_nodes],
                    "values": dict(run.values),
                },
            )
            self._pending_steers[run_id] = {}
            if message is not None:
                await self.emit(
                    run, EventKind.STEERING_RECEIVED, {"message": message}, node_id=node_id,
                )
                self._pending_steers[run_id][node_id] = [message]
            # No await between here and the return, so the snapshot the caller
            # is answered with is the forked position rather than whatever the
            # restarted superstep has already got to.
            self._drive(run, forked)
            return self._snapshot(run)

    async def steer(
        self,
        run_id: RunId,
        message: str,
        execution_id: ExecutionId | None = None,
        node_id: NodeId | None = None,
    ) -> RunSnapshot:
        run = self._require(run_id)
        try:
            target, execution = self._executions.resolve(run_id, execution_id, node_id)
        except (RunNotSteerableError, AmbiguousExecutionError):
            if node_id is not None and self._is_always_open(run, node_id):
                return await self._steer_always_open(run, node_id, message)
            raise
        await execution.steer(message)
        # Accepted for delivery, not yet delivered: the execution picks it up at
        # its next interruption point and says so itself, with a transcript
        # entry. Blocking this call until then would mean an agent waiting on an
        # approval could never be sent an instruction.
        await self.emit(
            run,
            EventKind.STEERING_RECEIVED,
            {"message": message},
            node_id=target.node_id,
            execution_id=target.execution_id,
        )
        return self._snapshot(run)

    async def workspace(self, run_id: RunId):
        self._require(run_id)
        raise NoSuchPositionError("this run has no workspace to attach or detach")

    async def set_workspace_attached(self, run_id: RunId, attached: bool):
        return await self.workspace(run_id)

    async def set_runner(
        self, run_id: RunId, node_id: NodeId, runner: str
    ) -> RunSnapshot:
        run = self._require(run_id)
        node = self.topology(run.graph.graph_id).node(node_id)
        if node is None:
            raise UnknownNodeError(f"unknown node: {node_id}")
        if not node.runner or runner not in (*node.runners, node.runner):
            raise ValueError(f"unsupported runner for {node_id}: {runner}")
        if runner == node.runner:
            run.runner_overrides.pop(node_id, None)
        elif runner != run.runner_overrides.get(node_id, node.runner):
            run.runner_overrides[node_id] = runner
        return self._snapshot(run)

    async def set_auto_approve(
        self, run_id: RunId, node_id: NodeId, enabled: bool
    ) -> RunSnapshot:
        run = self._require(run_id)
        if self.topology(run.graph.graph_id).node(node_id) is None:
            raise UnknownNodeError(f"unknown node: {node_id}")
        run.auto_approve_nodes = tuple(n for n in run.auto_approve_nodes if n != node_id)
        if enabled:
            run.auto_approve_nodes += (node_id,)
            for approval in tuple(run.pending.values()):
                if approval.node_id == node_id and approval.kind in (
                    ApprovalKind.COMMAND_EXECUTION, ApprovalKind.FILE_CHANGE, ApprovalKind.TOOL_USE
                ):
                    await self.decide(run_id, approval.approval_id, ApprovalDecision.ACCEPT)
        return self._snapshot(run)

    async def decide(
        self, run_id: RunId, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> RunSnapshot:
        run = self._require(run_id)
        approval = run.pending.get(approval_id)
        if approval is None:
            if approval_id in run.answered:
                raise ApprovalNotPendingError(
                    f"approval is no longer pending: {approval_id}"
                )
            raise UnknownApprovalError(f"unknown approval: {approval_id}")
        # Resolved before anything is mutated, so a request that cannot be
        # delivered does not leave the run half-answered. By execution rather
        # than by node: the other two reviewers did not ask this question.
        _, execution = self._executions.resolve(run_id, approval.execution_id)
        run.pending.pop(approval_id)
        run.answered.add(approval_id)
        if decision is not ApprovalDecision.CANCEL:
            await execution.decide(approval_id, decision)
            return self._snapshot(run)
        # A refusal is the end of the run rather than a release, so it waits for
        # the run to actually be over. The reason it has to: the two siblings of
        # this request are blocked on questions nobody is going to answer now,
        # and a superstep that sat waiting for them would leave the run saying
        # `failed` while never publishing a terminal event -- and while those
        # two went on taking steering. `_walk` unwinds the superstep on the
        # refusal, which is what makes this wait terminate.
        run.error = f"{approval.reason} was not allowed"
        await execution.decide(approval_id, decision)
        await asyncio.gather(*run.executors, return_exceptions=True)
        return self._snapshot(run)

    async def cancel(self, run_id: RunId) -> RunSnapshot:
        run = self._require(run_id)
        async with run.control:
            # `run.status` rather than the derived one: a run parked on a
            # question is a run that is still going, and stopping it is exactly
            # what this is for.
            ending = run.status
            await self._stop(run)
            if ending is RunStatus.RUNNING:
                await self._fail(run, CANCELLED, None)
            return self._snapshot(run)

    async def aclose(self) -> None:
        """Stop every run, as the server's shutdown does."""
        for run in self._runs.values():
            async with run.control:
                await self._stop(run)

    # --- what a test can ask that a client cannot --------------------------

    def running(self) -> tuple[RunId, ...]:
        """The runs still executing, which shutdown has to leave empty.

        Not part of `GraphRuntime`: "is a task still alive" is an
        implementation's own question, and the HTTP surface has no way to ask
        it -- a leaked task looks exactly like a node that is thinking. Spelled
        the same way in every implementation, so one test can ask both.
        """
        return tuple(
            run.run_id for run in self._runs.values() if self.executors(run.run_id)
        )

    def executors(self, run_id: RunId) -> tuple[asyncio.Task[None], ...]:
        """The tasks driving this run, which must never be more than one.

        The same reasoning as `running`, and the same blind spot it exists to
        cover: two executors over one run are invisible from outside, because
        everything they do is something one of them could plausibly have done
        alone -- until their checkpoints and their state start interleaving.
        """
        run = self._runs.get(run_id)
        if run is None:
            return ()
        return tuple(executor for executor in run.executors if not executor.done())

    def entered(self, node_id: NodeId) -> int:
        """How many times a node has been started, across every attempt.

        The question "did that restart the node?" reduces to, and a fake that
        could not answer it would let a binding satisfy the steering contract by
        replaying the node from the top.
        """
        return self._entries.get(node_id, 0)

    # --- execution ---------------------------------------------------------

    def _drive(self, run: _Run, checkpoint: Checkpoint) -> None:
        """Start the one executor this run is allowed, and keep hold of it."""
        executor = asyncio.create_task(self._execute(run, checkpoint))
        run.executors.add(executor)
        executor.add_done_callback(run.executors.discard)

    async def _execute(self, run: _Run, checkpoint: Checkpoint) -> None:
        """Drive the run, and make sure a node that raises is reported as one.

        Without this, the task would die with its exception unretrieved and the
        run would still claim to be running -- forever, to every client asking.
        """
        try:
            await self._walk(run, checkpoint)
        except asyncio.CancelledError:
            raise
        except Exception as failure:  # noqa: BLE001 -- #779: fake executor reports a failed run; pragma: no cover - a bug in the fake
            await self._fail(run, str(failure), None)

    async def _walk(self, run: _Run, checkpoint: Checkpoint) -> None:
        while checkpoint.next_nodes:
            run.frontier = checkpoint.next_nodes
            # One superstep: every node in the frontier at once, and every task
            # of each of them, which is what makes a reviewer pool one step
            # rather than three -- and one node's three tasks one step too.
            tasks = [
                asyncio.create_task(self._run_node(run, node_id))
                for node_id in checkpoint.next_nodes
                for _ in range(self._tasks(run, node_id))
            ]
            try:
                ended = await self._superstep(tasks)
            finally:
                # Whatever ends the step takes the rest of it: a refusal, a
                # raise, or the cancellation a fork does. Siblings waiting on
                # approvals nobody will answer now would otherwise hold the step
                # -- and the run -- open for good.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            run.frontier = ()
            if ended is not None:
                # `run.error` for a refusal: the reason belongs to the request
                # that was turned down, and `decide` recorded it there.
                await self._fail(run, ended.error or run.error, ended.node_id)
                return
            checkpoint = run.record(
                run.graph.successors(checkpoint.next_nodes),
                parent=checkpoint.checkpoint_id,
                source="superstep",
                counter=self.ids,
            )
            await self._emit_checkpoint(run, checkpoint)
        run.status = RunStatus.COMPLETED
        await self.emit(run, EventKind.RUN_FINISHED, {"values": dict(run.values)})

    async def _superstep(
        self, tasks: Sequence[asyncio.Task[_Outcome]]
    ) -> _Outcome | None:
        """Wait for the step, and stop waiting as soon as one task ends the run.

        `wait` rather than `gather`, because `gather` only answers once every
        task has: three reviewers, one of them refused and the other two blocked
        on their own questions, is a step that never completes and a run that
        never publishes an ending.
        """
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            ended = next(
                (
                    outcome
                    for outcome in (task.result() for task in done)
                    if outcome.error or outcome.refused
                ),
                None,
            )
            if ended is not None:
                return ended
        return None

    def _tasks(self, run: _Run, node_id: NodeId) -> int:
        node = run.graph.node(node_id)
        return node.tasks if node is not None else 1

    async def _run_node(self, run: _Run, node_id: NodeId) -> _Outcome:
        node = run.graph.node(node_id)
        assert node is not None
        execution = _Execution(
            self, run, ExecutionId(f"execution-{next(self.ids)}"), node_id
        )
        self._entries[node_id] = self._entries.get(node_id, 0) + 1
        # Deliver any messages queued by a steer that triggered this resume.
        run_steers = self._pending_steers.get(run.run_id, {})
        for msg in run_steers.pop(node_id, []):
            execution._steering.put_nowait(msg)
        # Registered for the whole execution, so steering and approvals reach it
        # wherever it has got to -- and released however it ends, including by
        # the cancellation a fork does.
        with self._executions.in_flight(
            run.run_id, execution.execution_id, node_id, execution
        ):
            await self.emit(
                run,
                EventKind.NODE_STARTED,
                node_id=node_id,
                execution_id=execution.execution_id,
            )
            try:
                for beat in node.beats:
                    await execution.drain()
                    if not await self._play(run, execution, beat):
                        return _Outcome(node_id, refused=True)
            except asyncio.CancelledError:
                raise
            except Exception as failure:  # noqa: BLE001 -- #779: fake node reports error in its outcome
                return _Outcome(node_id, error=str(failure))
            await self.emit(
                run,
                EventKind.NODE_FINISHED,
                {"values": dict(run.values)},
                node_id=node_id,
                execution_id=execution.execution_id,
            )
        return _Outcome(node_id)

    async def _play(self, run: _Run, execution: _Execution, beat: Beat) -> bool:
        """Play one beat. False means the run ended here."""
        node_id = execution.node_id
        execution_id = execution.execution_id
        match beat:
            case Say(text=text, role=role):
                node = run.graph.node(node_id)
                assert node is not None
                run.values[node.output_key or str(node_id)] = text
                await self.emit(
                    run,
                    EventKind.TRANSCRIPT,
                    {"role": role, "text": text},
                    node_id=node_id,
                    execution_id=execution_id,
                )
            case Call(name=name, arguments=arguments, result=result):
                call_id = f"call-{next(self.ids)}"
                await self.emit(
                    run,
                    EventKind.TOOL_CALL,
                    {"callId": call_id, "name": name, "arguments": dict(arguments)},
                    node_id=node_id,
                    execution_id=execution_id,
                )
                await self.emit(
                    run,
                    EventKind.TOOL_RESULT,
                    {"callId": call_id, "name": name, "result": result},
                    node_id=node_id,
                    execution_id=execution_id,
                )
            case Ask():
                return await execution.ask(beat) is not ApprovalDecision.CANCEL
            case AwaitSteering():
                await execution.receive(await execution.next_message())
            case Fail(message=message):
                raise ScriptedFailure(message)
        return True

    async def _fail(self, run: _Run, error: str, node_id: NodeId | None) -> None:
        run.status = RunStatus.FAILED
        run.error = error
        await self.emit(run, EventKind.RUN_FAILED, {"error": error}, node_id=node_id)

    async def _stop(self, run: _Run) -> None:
        run.pending.clear()
        # Every executor, not the latest one: if a leak ever does happen, this
        # is what stops it being permanent. Callers hold `run.control`, which is
        # what stops one happening in the first place.
        executors = tuple(run.executors)
        run.executors.clear()
        for executor in executors:
            executor.cancel()
        if executors:
            await asyncio.gather(*executors, return_exceptions=True)
        for active in self._executions.active(run.run_id):
            self._executions.release(run.run_id, active.execution_id)
        run.frontier = ()

    # --- always-open helpers ------------------------------------------------

    def _is_always_open(self, run: _Run, node_id: NodeId) -> bool:
        graph_node = run.graph.topology().node(node_id)
        return graph_node is not None and graph_node.always_open

    async def _steer_always_open(
        self, run: _Run, node_id: NodeId, message: str
    ) -> RunSnapshot:
        """Resume the graph at an always-open node and queue the message."""
        checkpoint_id: CheckpointId | None = None
        for checkpoint in reversed(run.checkpoints):
            if node_id in checkpoint.next_nodes:
                checkpoint_id = checkpoint.checkpoint_id
                break
        if checkpoint_id is None:
            raise NoSuchPositionError(
                f"this run has never been about to run {node_id}"
            )
        return await self.resume_from(
            run.run_id, checkpoint_id, node_id=node_id, message=message,
        )

    # --- internal ----------------------------------------------------------

    def _require(self, run_id: RunId) -> _Run:
        run = self._runs.get(run_id)
        if run is None:
            raise UnknownRunError(f"unknown run: {run_id}")
        return run

    def _snapshot(self, run: _Run) -> RunSnapshot:
        # The registry is the source of truth for what is executing: it is the
        # same thing steering resolves against, so a snapshot cannot promise a
        # node a message could not actually reach.
        return run.snapshot(self._executions.active(run.run_id))

    async def _emit_checkpoint(self, run: _Run, checkpoint: Checkpoint) -> None:
        await self.emit(
            run,
            EventKind.CHECKPOINT,
            {
                "checkpointId": str(checkpoint.checkpoint_id),
                "parentId": (
                    str(checkpoint.parent_id) if checkpoint.parent_id else None
                ),
                "nextNodes": [str(node_id) for node_id in checkpoint.next_nodes],
                "source": checkpoint.source,
                "values": dict(checkpoint.values),
            },
        )

    async def emit(
        self,
        run: _Run,
        kind: EventKind,
        payload: Mapping[str, object] | None = None,
        node_id: NodeId | None = None,
        execution_id: ExecutionId | None = None,
    ) -> None:
        if self._observer is None:
            return
        await self._observer(
            RuntimeEvent(
                run_id=run.run_id,
                kind=kind,
                payload=dict(payload or {}),
                node_id=node_id,
                execution_id=execution_id,
            )
        )


__all__ = [
    "Ask",
    "AwaitSteering",
    "Beat",
    "Call",
    "Fail",
    "Say",
    "ScriptedFailure",
    "ScriptedGraph",
    "InputGraph",
    "ModeGraph",
    "ScriptedGraphRuntime",
    "ScriptedNode",
]


@dataclass(frozen=True)
class ModeGraph(ScriptedGraph):
    """A scripted workflow exposing the standard connected/disconnected input."""

    inputs: tuple[WorkflowInput, ...] = (mode_input(),)


@dataclass(frozen=True)
class InputGraph(ScriptedGraph):
    """A scripted workflow with test-selected input declarations."""

    inputs: tuple[WorkflowInput, ...] = ()
