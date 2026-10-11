"""Registered graphs, their runs, loops of them, and steering their nodes.

One object holds the four, because they share everything that matters: the
daemon's one `LangGraphRuntime`, the graph database, and the event feed every
run publishes to. `GraphService.observe` is installed on that feed, and is how
node executions, steering delivery and loop budgets are kept current without
polling anything.

The vocabulary, kept the same in every response:

* a **graph** is a name within a project, and its **versions** are immutable.
  A run pins a version: registering another never changes a run in flight;
* a **run** is one execution of a version, started through the backend's
  configured runners;
* a **loop** starts runs of one pinned version on a cadence, never two at once,
  until a limit or a person pauses it;
* a **node execution** is one attempt at one node within a run, and is what
  steering addresses.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import yaml
from engine.runtime.repositories import RepositoryRegistry
from engine.domain import MODE_INPUT, ApprovalDecision, ApprovalId, ForgeMode, RunId
from engine.graph_runtime import (
    AmbiguousExecutionError,
    RunNotSteerableError,
    RunStatus,
    UnknownGraphError,
    UnknownRunError,
)
from engine.graph_runtime.control import CANCELLED
from engine.graph_runtime.events import EventKind, RuntimeEvent
from engine.graph_runtime.identity import ExecutionId
from engine.graph_runtime.usage import usage_rollup
from engine.graph_runtime_langgraph import GraphWorkflow, LangGraphRuntime
from engine.ports import WorkspaceProvider
from langgraph_acp import ACPAgentRegistry

from engine.graph_runtime import GraphId
from engine.graph_runtime.inputs import RUNNER_POLICIES, resolve_inputs
from engine.graph_runtime.topology import GraphTopology
from engine.runtime.config import DEFAULT_SESSION_TOOLS, ApprovalConfig
from engine.runtime.workflows import WorkflowLoadError, load_workflow_file

from engine.graph_service.compile import compile_graph
from engine.graph_service.session import (
    BASE_INPUT,
    SESSION_AGENTS,
    SESSION_INPUT,
    Session,
    Sessions,
    session_workflow,
)
from engine.graph_service.language import (
    STAGE_GROUPS,
    STAGES,
    GraphError,
    GraphSpec,
    Problem,
    parse_duration,
    parse_graph,
)
from engine.graph_service.store import (
    OPEN_EXECUTION_STATUSES,
    AgentRow,
    ExecutionRow,
    GraphRow,
    GraphServiceStore,
    LoopRow,
    SteeringRow,
    SubmissionRow,
    VersionRow,
    now_iso,
    parse_iso,
)

log = logging.getLogger(__name__)

#: The harnesses an added agent can run on.
AGENT_KINDS = ("claude", "codex", "opencode")
_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: Query parameter names that carry a credential: an agent's url is stored and listed back, so none may ride in it.
_CREDENTIAL_PARAMETER = re.compile(r"(key|token|secret|password|signature|sig|auth|credential)s?$")

#: The provider an opencode agent with a url reaches its endpoint through.
OPENCODE_URL_PROVIDER = "engine"

AgentFactory = Callable[[AgentRow], Any]
"""Builds the ACP provider an added agent runs as: one registered under `row.name`."""

DEFAULT_PROJECT = "default"
TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})
DEFAULT_RUN_LIMIT = 20

#: What a runner says when it has no credentials, across the agents ACP drives.
#: Matched against a run's failure so the answer can be a sign-in instruction
#: rather than a stack trace.
_AUTH_FAILURE = re.compile(
    r"auth(?:entication|orization)?[ _-]?(?:is )?required|not (?:logged|signed) in|"
    r"(?:log|sign) ?in (?:first|again|required)|invalid (?:api[ _-]?key|credentials?)|"
    r"unauthori[sz]ed|\b401\b|credentials? (?:expired|missing|not found)|"
    r"please run [`'\"]?\S+ login",
    re.IGNORECASE,
)

#: What `max-spend` counts. Shown with every loop, so the cap is never read as
#: covering more than it does.
SPEND_SCOPE = (
    "Agent usage reported by each node's ACP session (the agent's own session "
    "cost, or tokens priced at list rates when it reports none), summed over "
    "every run of the loop. Excludes CI minutes, hosting, and usage billed "
    "through a subscription, which agents report without a price."
)


class ServiceError(Exception):
    """A refusal with an HTTP status and, sometimes, details to show."""

    status = 400

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.details = details


class NotFound(ServiceError):
    status = 404


class Conflict(ServiceError):
    status = 409


class Unavailable(ServiceError):
    status = 503


class Forbidden(ServiceError):
    status = 403


#: The two languages a graph can be written in.
FORMATS = ("yaml", "python")
_GROUP_STAGES = {group: stage for stage, group in STAGE_GROUPS.items()}
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


@dataclass(frozen=True)
class Loaded:
    """A registered version as this process offers it."""

    workflow: GraphWorkflow
    spec: GraphSpec | None
    """The YAML graph it was compiled from; `None` for a Python graph."""

    def parents(self) -> dict[str, str]:
        """LangGraph node key -> the YAML node it belongs to, for parallel branches."""
        if self.spec is None:
            return {}
        return {key: node.id for node in self.spec.nodes if node.parallel for key in node.keys}


@dataclass(frozen=True)
class StartRequest:
    """Everything a run of a registered graph is started with."""

    instruction: str
    repository: str
    inputs: Mapping[str, str]


StartRun = Callable[[GraphWorkflow, StartRequest], Awaitable[RunId]]
"""How the host starts a run. The daemon passes its own WorkOrder path, so a
run started here is listed, approved and notified like any other."""


@dataclass(frozen=True)
class Accounting:
    """A loop's consumption, recomputed from durable events on every read."""

    spend_usd: float
    complete: bool
    estimated: bool
    pull_requests: int
    runs: int

    def json(self) -> dict[str, object]:
        return {
            "usd": round(self.spend_usd, 6),
            "complete": self.complete,
            "estimated": self.estimated,
        }


class GraphService:
    def __init__(
        self,
        runtime: LangGraphRuntime,
        database: str | Path,
        *,
        workspace_provider: WorkspaceProvider,
        registry: ACPAgentRegistry,
        start: StartRun | None = None,
        session_config: Mapping[str, object] | None = None,
        default_repository: str = "",
        repositories: Mapping[str, str] | RepositoryRegistry | None = None,
        default_base_ref: str = "origin/HEAD",
        model_tiers: Mapping[str, Mapping[str, str]] | None = None,
        allow_python: bool = True,
        agent_factory: AgentFactory | None = None,
        session_tools: Sequence[str] = DEFAULT_SESSION_TOOLS,
        approval_policy: ApprovalConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        tick_seconds: float = 15.0,
    ) -> None:
        if runtime.checkpointer is None:
            raise ValueError("graphs can only be registered on a runtime that has a checkpointer")
        self.runtime = runtime
        self.store = GraphServiceStore(database)
        self._workspace_provider = workspace_provider
        self._registry = registry
        self._start = start or self._start_directly
        self._session_config = session_config
        self._default_repository = default_repository
        self._repositories = (
            repositories if isinstance(repositories, RepositoryRegistry)
            else RepositoryRegistry(repositories, projects={})
        )
        self._default_base_ref = default_base_ref
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._tick_seconds = tick_seconds
        self._model_tiers = model_tiers
        self._allow_python = allow_python
        self._agent_factory = agent_factory
        #: What the registry held before any added agent: these cannot be replaced or removed.
        self._builtin_agents = tuple(registry.names)
        #: Added agents' models, by name. Compiled graphs hold this dict, not a copy,
        #: so a model changed here reaches graphs registered before the change.
        self._agent_models: dict[str, str] = {}
        self._sessions = Sessions()
        self._session_workflow = session_workflow(
            workspace_provider, self._sessions,
            tools=session_tools, policy=approval_policy or ApprovalConfig(),
            default_base_ref=default_base_ref,
        )
        self._sources = Path(database).parent / "registered-graphs"
        self._loaded: dict[str, Loaded] = {}
        self._lock = asyncio.Lock()
        self._scheduler: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()

    # --- lifetime -----------------------------------------------------------

    async def open(self, *, schedule: bool = True) -> None:
        """Offer every stored version again, and settle what the last process left open.

        Called before the host restores unfinished runs, because a run can only
        be picked back up if its graph is registered.

        Nothing that was executing survives a process, so open executions are
        recorded as interrupted -- a resumed run starts fresh attempts with new
        ids -- and steering still queued in memory is reported undelivered
        rather than left looking like it might still arrive.
        """
        for agent in self.store.agents():
            try:
                self._offer_agent(agent)
            except Exception:
                log.exception("added agent %s could not be offered", agent.name)
        self.runtime.register(self._session_workflow.compiled(self.runtime.checkpointer))
        for version in self.store.versions():
            try:
                self._register(version)
            except Exception:
                log.exception("registered graph version %s could not be loaded", version.version_id)
        at = self._now_iso()
        self.store.close_open_executions("interrupted", at, error="the daemon restarted")
        self.store.abandon_undelivered_steering("the daemon restarted before it was delivered")
        if schedule:
            self._scheduler = asyncio.create_task(self._schedule())

    async def aclose(self) -> None:
        if self._scheduler is not None:
            self._scheduler.cancel()
            await asyncio.gather(self._scheduler, return_exceptions=True)
        await asyncio.gather(*self._background, return_exceptions=True)
        self.store.close()

    def runners(self) -> tuple[str, ...]:
        return tuple(self._registry.names)

    # --- agents -------------------------------------------------------------

    def agents_json(self) -> list[dict[str, Any]]:
        added = {row.name: row for row in self.store.agents()}
        return [self.agent_json(name, added.get(name)) for name in self._registry.names]

    def agent_json(self, name: str, row: AgentRow | None = None) -> dict[str, Any]:
        row = row or self.store.agent(name)
        if row is not None:
            return {
                "name": row.name, "kind": row.kind, "model": row.model, "url": row.url,
                "builtin": False, "createdAt": row.created_at, "updatedAt": row.updated_at,
            }
        if name in self._builtin_agents:
            return {"name": name, "kind": name, "model": "", "url": "", "builtin": True}
        raise NotFound(f"no agent named {name!r}", agent=name)

    async def add_agent(
        self, kind: str, *, name: str = "", model: str = "", url: str = "", overwrite: bool = False,
    ) -> dict[str, Any]:
        """Offer a harness under a name graphs can use, with its own model or endpoint."""
        if self._agent_factory is None:
            raise Unavailable("this backend does not support adding agents")
        name = name or kind
        if kind not in AGENT_KINDS:
            raise ServiceError(f"kind must be one of {', '.join(AGENT_KINDS)}", kind=kind)
        if not _AGENT_NAME.match(name) or name in RUNNER_POLICIES:
            raise ServiceError(
                "an agent name is lowercase letters, digits, - and _, and is not a runner policy", name=name,
            )
        if url and kind != "opencode":
            raise ServiceError("only an opencode agent takes a url", kind=kind)
        if url and not model:
            raise ServiceError("an agent with a url needs a model to ask it for")
        if url and not re.match(r"^https?://", url):
            raise ServiceError("url must be http(s)", url=url)
        if url and _carries_credential(url):
            # Not echoed: the refusal would otherwise repeat the secret into responses and logs.
            raise ServiceError("url must not carry an api key in its query string")
        if name in self._builtin_agents:
            raise Conflict(f"{name} is built in; give this agent another name with --name", agent=name)
        async with self._lock:
            existing = self.store.agent(name)
            if existing is not None and not overwrite:
                raise Conflict(f"an agent named {name} already exists; pass replace to change it", agent=name)
            at = self._now_iso()
            row = AgentRow(name, kind, model, url, existing.created_at if existing else at, at)
            self._offer_agent(row)
            self.store.upsert_agent(row)
        return self.agent_json(name, row)

    async def remove_agent(self, name: str) -> dict[str, Any]:
        if name in self._builtin_agents:
            raise Conflict(f"{name} is built in and cannot be removed", agent=name)
        async with self._lock:
            row = self.store.agent(name)
            if row is None:
                raise NotFound(f"no agent named {name!r}", agent=name)
            self.store.delete_agent(name)
            self._registry.unregister(name)
            self._agent_models.pop(name, None)
        return {**self.agent_json(name, row), "removed": True}

    def _offer_agent(self, row: AgentRow) -> None:
        assert self._agent_factory is not None
        self._registry.register(self._agent_factory(row), replace=True)
        if row.model:
            self._agent_models[row.name] = session_model(row)
        else:
            self._agent_models.pop(row.name, None)

    # --- sessions -----------------------------------------------------------

    async def start_session(self, *, agent: str, repository: str, base_ref: str = "") -> dict[str, Any]:
        """Start a run whose implementation node is a CLI the caller drives itself."""
        if agent not in SESSION_AGENTS:
            raise ServiceError(f"agent must be one of {', '.join(SESSION_AGENTS)}", agent=agent)
        repository = self.resolve_repository((repository or self._default_repository).strip())
        if not repository:
            raise ServiceError("no repository: run from inside one, pass --repo, or configure a default on the backend")
        session = self._sessions.create(agent)
        inputs = {
            SESSION_INPUT: session.session_id, MODE_INPUT: str(ForgeMode.CONNECTED),
            **({BASE_INPUT: base_ref} if base_ref else {}),
        }
        session.run_id = str(await self._start(
            self._session_workflow, StartRequest(f"Interactive {agent} session", repository, inputs),
        ))
        return self.session_json(session.session_id)

    def session_json(self, session_id: str) -> dict[str, Any]:
        session = self._session(session_id)
        body: dict[str, Any] = {
            "sessionId": session.session_id, "agent": session.agent,
            "runId": session.run_id, "status": session.status,
        }
        if session.status == "failed":
            body["error"] = str(session.ready.exception())
        elif session.ready.done():
            body.update(session.ready.result())
        return body

    async def end_session(self, session_id: str, summary: str = "") -> dict[str, Any]:
        session = self._session(session_id)
        if not session.ended.done():
            session.ended.set_result(summary.strip())
        return self.session_json(session_id)

    def _session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFound(f"no session {session_id}")
        return session

    # --- graphs -------------------------------------------------------------

    async def add_graph(
        self, project: str, *, source: str, format: str = "yaml", name: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Register a YAML or Python graph; a changed definition becomes the next version.

        Registering an identical definition again is not a new version: the
        existing one is answered, so `graph add` can be rerun safely.
        """
        project = _project(project)
        format = (format or "yaml").strip().lower()
        if format not in FORMATS:
            raise ServiceError(f"format must be one of {', '.join(FORMATS)}")
        if not source.strip():
            raise ServiceError("the graph source is empty")
        if format == "yaml":
            spec = _parse_yaml(source, runners=self.runners())
            graph_name, definition, digest = spec.name, {"format": "yaml", **spec.json()}, spec.digest
        else:
            if not self._allow_python:
                raise Forbidden("this backend does not accept Python graphs; send YAML, or set [graphs] allow_python")
            graph_name = name.strip()
            if graph_name and not _NAME.match(graph_name):
                raise ServiceError("a graph name is lowercase letters, digits, '.', '_' and '-'")
            definition, digest = {"format": "python"}, hashlib.sha256(source.encode()).hexdigest()
        async with self._lock:
            at = self._now_iso()
            version_id = f"gv-{uuid.uuid4().hex[:12]}"
            if format == "python":
                # Loaded once to learn its name and to refuse it early if it is broken.
                workflow = self._python_workflow(version_id, source, graph_name)
                graph_name = graph_name or str(workflow.graph_id)
                if not _NAME.match(graph_name):
                    raise ServiceError(f"{graph_name!r} is not a usable graph name; pass --name")
                definition["workflowId"] = str(workflow.graph_id)
                definition["title"] = workflow.name
                digest = hashlib.sha256(f"{graph_name}\n{source}".encode()).hexdigest()
            existing = self.store.graphs_named(graph_name, project)
            if existing:
                graph = existing[0]
                latest = self.store.version(graph.latest_version_id)
                if latest is not None and latest.digest == digest:
                    self._forget_source(version_id)
                    return self._graph_json(graph, latest, definition=True), False
                number = max(version.number for version in self.store.versions(graph.graph_id)) + 1
            else:
                graph = GraphRow(f"g-{uuid.uuid4().hex[:12]}", project, graph_name, "", at, at)
                number = 1
            version = VersionRow(
                version_id, graph.graph_id, number, definition, digest, at, format=format, source=source,
            )
            # Compiled before anything is written: a graph LangGraph refuses is
            # a validation error, and leaves no version behind.
            try:
                self._register(version, name=graph_name)
            except GraphError:
                self._forget_source(version_id)
                raise
            except Exception as broken:
                self._forget_source(version_id)
                raise GraphError([Problem("$", f"does not compile: {broken}")]) from broken
            with self.store.transaction():
                if existing:
                    self.store.set_latest_version(graph.graph_id, version.version_id, at)
                    graph = replace(graph, latest_version_id=version.version_id, updated_at=at)
                else:
                    graph = replace(graph, latest_version_id=version.version_id)
                    self.store.insert_graph(graph)
                self.store.insert_version(version)
            return self._graph_json(graph, version, definition=True), True

    def list_graphs(self, project: str | None) -> list[dict[str, Any]]:
        return [
            self._graph_json(graph, self.store.version(graph.latest_version_id))
            for graph in self.store.graphs(_project(project) if project else None)
        ]

    def get_graph(self, project: str | None, reference: str) -> dict[str, Any]:
        graph, version = self.resolve(project, reference)
        return self._graph_json(graph, version, definition=True, versions=True)

    def resolve(self, project: str | None, reference: str) -> tuple[GraphRow, VersionRow]:
        """A graph id, a version id, a name, or `name@N`, within the project.

        A name with no project is looked up everywhere, and refused rather than
        guessed when two projects use it.
        """
        reference = reference.strip()
        if not reference:
            raise ServiceError("name a graph")
        if reference.startswith("gv-"):
            version = self.store.version(reference)
            graph = self.store.graph(version.graph_id) if version else None
            if version is None or graph is None:
                raise NotFound(f"no graph version {reference}")
            return graph, version
        if reference.startswith("g-"):
            graph = self.store.graph(reference)
            if graph is None:
                raise NotFound(f"no graph {reference}")
            return graph, self._version(graph.latest_version_id)
        name, _, number = reference.partition("@")
        matches = self.store.graphs_named(name, _project(project) if project else None)
        if not matches:
            where = f" in project {_project(project)!r}" if project else ""
            raise NotFound(f"no graph named {name!r}{where}")
        if len(matches) > 1:
            raise Conflict(
                f"graph name {name!r} is ambiguous; name a project or use a graph id",
                candidates=[{"graphId": graph.graph_id, "project": graph.project} for graph in matches],
            )
        graph = matches[0]  # pyright: ignore[reportGeneralTypeIssues]  # Baseline: see docs/pyright.md
        if not number:
            return graph, self._version(graph.latest_version_id)
        if not number.isdigit():
            raise ServiceError(f"{reference!r}: a version is a number, as in {name}@2")
        version = self.store.version_numbered(graph.graph_id, int(number))
        if version is None:
            raise NotFound(f"graph {name!r} has no version {number}")
        return graph, version

    # --- runs ---------------------------------------------------------------

    async def submit_run(
        self,
        *,
        project: str | None,
        graph: str,
        instruction: str,
        inputs: Mapping[str, object] | None = None,
        repository: str = "",
        idempotency_key: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Start a run of the graph's pinned version, at most once per key.

        Answers `(run, created)`. A retry carrying the key of a run already
        started answers that run instead of starting another; the same key with
        a different request is refused, since it cannot be both.
        """
        graph_row, version = self.resolve(project, graph)
        request = self._start_request(version, instruction, inputs, repository)
        key = idempotency_key.strip() or f"anonymous-{uuid.uuid4().hex}"
        digest = _digest({
            "versionId": version.version_id,
            "instruction": request.instruction,
            "inputs": dict(request.inputs),
            "repository": request.repository,
        })
        async with self._lock:
            previous = self.store.submission(key)
            if previous is not None:
                if previous.request_digest != digest:
                    raise Conflict("this idempotency key was already used for a different run request")
                return await self.run_json(previous.run_id), False
            run_id = await self._start_version(version, request)
            self.store.insert_submission(
                SubmissionRow(key, digest, str(run_id), version.version_id, self._now_iso())
            )
        return await self.run_json(str(run_id)), True

    async def run_json(self, run_id: str) -> dict[str, Any]:
        try:
            snapshot = await self.runtime.snapshot(RunId(run_id))
        except UnknownGraphError as unloadable:
            raise NotFound(str(unloadable)) from unloadable
        except UnknownRunError:
            snapshot = None
        if snapshot is None:
            raise NotFound(f"no run {run_id}")
        version = self.store.version(str(snapshot.graph_id))
        graph = self.store.graph(version.graph_id) if version else None
        loaded = self._loaded.get(str(snapshot.graph_id))
        topology = self.runtime.topology(snapshot.graph_id)
        executions = self.store.executions(run_id)
        record = await self.runtime.store.run(RunId(run_id))
        overrides = record.runner_overrides if record else {}
        values = snapshot.values or {}
        results = _results(topology, values)
        events = self.runtime.store.events_since(RunId(run_id))
        usage = usage_rollup(events)
        activity = _latest_activity(events)
        failure: dict[str, Any] | None = None
        if snapshot.status is RunStatus.FAILED:
            failed = next((row for row in reversed(executions) if row.status == "failed"), None)
            node_id = failed.node_id if failed else ""
            failure = {"error": snapshot.error, "node": node_id or None}
            runner = _runner_of(node_id, values, overrides, topology)  # pyright: ignore[reportAssignmentType]  # Baseline: see docs/pyright.md
            if auth := auth_required(snapshot.error, runner):  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md
                failure["authRequired"] = auth

        def runner(node_id: str) -> str:
            return _runner_of(node_id, values, overrides, topology)

        return {
            "runId": run_id,
            "graphId": graph.graph_id if graph else None,
            "versionId": str(snapshot.graph_id),
            "graph": graph.name if graph else str(snapshot.graph_id),
            "project": graph.project if graph else None,
            "version": version.number if version else None,
            "status": snapshot.status.value,
            "terminal": snapshot.status in TERMINAL_RUN_STATUSES,
            "loopId": self.store.loop_for_run(run_id),
            "format": version.format if version else None,
            "nodes": [_execution_json(row, topology, runner) for row in executions],
            **_progress(topology, loaded.parents() if loaded else {}, executions, activity, runner),
            "workspace": {
                "path": values.get("workspace"),
                "ref": values.get("workspaceRef"),
            } if values.get("workspace") else None,
            "pendingApprovals": [
                {
                    "approvalId": str(item.approval_id),
                    "executionId": str(item.execution_id),
                    "node": str(item.node_id),
                    "kind": str(getattr(item.kind, "value", item.kind)),
                    "reason": item.reason,
                    "command": item.command,
                }
                for item in snapshot.pending_approvals
            ],
            "results": results,
            "output": _output(executions, results) if snapshot.status is RunStatus.COMPLETED else None,
            "failure": failure,
            "usage": usage.total.json(),
            "pullRequests": [
                {"repository": repository, "number": number}
                for repository, number in await self.runtime.store.pull_requests(RunId(run_id))
            ],
        }

    async def list_runs(
        self,
        project: str | None,
        *,
        graph: str = "",
        loop: str = "",
        status: str = "",
        limit: int = DEFAULT_RUN_LIMIT,
    ) -> list[dict[str, Any]]:
        """The runs this service started, newest first: a summary of each, not its nodes."""
        if status and status not in {item.value for item in RunStatus}:
            raise ServiceError(
                f"unknown status {status!r}; one of {', '.join(item.value for item in RunStatus)}"
            )
        if limit < 1:
            raise ServiceError("limit must be at least 1")
        rows = self.store.started_runs(
            project=_project(project) if project else None,
            graph_id=self.resolve(project, graph)[0].graph_id if graph else None,
            loop_id=self.resolve_loop(project, loop).loop_id if loop else None,
        )
        runs: list[dict[str, Any]] = []
        for row in rows:
            if len(runs) >= limit:
                break
            try:
                snapshot = await self.runtime.snapshot(RunId(row.run_id))
            except (UnknownRunError, UnknownGraphError):
                snapshot = None
            if snapshot is None or (status and snapshot.status.value != status):
                continue
            version = self.store.version(row.version_id)
            graph_row = self.store.graph(version.graph_id) if version else None
            loop_row = self.store.loop(row.loop_id) if row.loop_id else None
            runs.append({
                "runId": row.run_id,
                "graphId": graph_row.graph_id if graph_row else None,
                "versionId": row.version_id,
                "graph": graph_row.name if graph_row else row.version_id,
                "project": graph_row.project if graph_row else None,
                "version": version.number if version else None,
                "status": snapshot.status.value,
                "terminal": snapshot.status in TERMINAL_RUN_STATUSES,
                "loopId": row.loop_id,
                "loop": loop_row.name if loop_row else None,
                "startedAt": row.started_at,
                "usage": usage_rollup(self.runtime.store.events_since(RunId(row.run_id))).total.json(),
            })
        return runs

    # --- nodes --------------------------------------------------------------

    async def list_nodes(self, run_id: str) -> list[dict[str, Any]]:
        run = await self.run_json(run_id)
        return run["nodes"]

    async def decide(self, run_id: str, approval_id: str, decision: str) -> dict[str, Any]:
        """Answer a question a run's agent stopped on: `accept` or `cancel`."""
        try:
            chosen = ApprovalDecision(decision)
        except ValueError:
            allowed = ", ".join(sorted(choice.value for choice in ApprovalDecision))
            raise ServiceError(f"decision must be one of: {allowed}") from None
        await self.run_json(run_id)
        try:
            await self.runtime.decide(RunId(run_id), ApprovalId(approval_id), chosen)
        except Exception as refused:
            raise Conflict(f"cannot answer approval {approval_id}: {refused}") from refused
        return await self.run_json(run_id)

    async def cancel(self, run_id: str) -> dict[str, Any]:
        await self.run_json(run_id)
        await self.runtime.cancel(RunId(run_id))
        return await self.run_json(run_id)

    async def get_node(self, execution_id: str) -> dict[str, Any]:
        row = self._execution(execution_id)
        topology, values, overrides = await self._run_context(row.run_id)
        return {
            **_execution_json(row, topology, lambda node: _runner_of(node, values, overrides, topology)),
            "steering": [_steering_json(item) for item in self.store.steering(execution_id)],
        }

    async def steer(self, execution_id: str, message: str, idempotency_key: str) -> tuple[dict[str, Any], bool]:
        """Queue a message for one running attempt, at most once per key.

        Refused for an attempt that has finished -- steering is not a retry,
        and a finished attempt has no session to speak in -- and for a node
        that is not an agent session. A message the runtime accepts is
        `accepted`; it becomes `delivered` when the agent is sent it and
        `applied` only when the agent finishes that turn.
        """
        message = message.strip()
        if not message:
            raise ServiceError("a steering message cannot be empty")
        key = idempotency_key.strip()
        if not key:
            raise ServiceError("steering needs an idempotency key")
        async with self._lock:
            previous = self.store.steering_by_key(key)
            if previous is not None:
                if previous.execution_id != execution_id or previous.message != message:
                    raise Conflict("this idempotency key was already used for a different steering message")
                return _steering_json(previous), False
            row = self._execution(execution_id)
            if row.status not in OPEN_EXECUTION_STATUSES:
                raise Conflict(
                    f"node execution {execution_id} ({row.node_id}, attempt {row.attempt}) is "
                    f"{row.status}; only a running attempt can be steered",
                    status=row.status,
                )
            topology, _, _ = await self._run_context(row.run_id)
            node = topology.node(row.node_id) if topology else None  # type: ignore[arg-type]
            if node is None or node.kind != "agent":
                raise Conflict(f"node {row.node_id!r} is not an agent session, so it cannot be steered")
            steering_id = f"st-{uuid.uuid4().hex[:12]}"
            self.store.insert_steering(steering_id, key, row.run_id, execution_id, message, self._now_iso())
        try:
            await self.runtime.steer(
                RunId(row.run_id), message, ExecutionId(execution_id), steering_id=steering_id,
            )
        except (RunNotSteerableError, AmbiguousExecutionError, UnknownRunError) as refused:
            reason = (
                "this backend has no live session for that execution, so it cannot be "
                f"steered: {refused}"
            )
            self.store.mark_steering(steering_id, "rejected", self._now_iso(), reason)
            stored = self.store.steering_by_id(steering_id)
            assert stored is not None
            raise Conflict(reason, steering=_steering_json(stored)) from refused
        stored = self.store.steering_by_id(steering_id)
        assert stored is not None
        return _steering_json(stored), True

    # --- loops --------------------------------------------------------------

    async def add_loop(
        self,
        *,
        project: str | None,
        graph: str,
        name: str = "",
        instruction: str = "",
        every: object = None,
        max_prs: object = None,
        max_spend_usd: object = None,
        repository: str = "",
        inputs: Mapping[str, object] | None = None,
        start_now: bool = True,
    ) -> dict[str, Any]:
        """Register a recurring run of one pinned version.

        Instruction and cadence come from the graph's `loop` section unless
        given here. Neither is invented: a loop with no cadence is refused.
        """
        graph_row, version = self.resolve(project, graph)
        loaded = self._loaded.get(version.version_id)
        defaults = loaded.spec.loop if loaded and loaded.spec else None
        instruction = (instruction or (defaults.instruction if defaults else "")).strip()
        if not instruction:
            raise ServiceError("no instruction: pass one, or set loop.instruction in the graph")
        if every in (None, ""):
            if defaults is None or defaults.interval_seconds is None:
                raise ServiceError("no cadence: pass --every (such as 6h), or set loop.every in the graph")
            interval = defaults.interval_seconds
        else:
            try:
                interval = parse_duration(every)
            except ValueError as error:
                raise ServiceError(f"every: {error}") from None
        limits = _limits(max_prs, max_spend_usd)
        request = self._start_request(version, instruction, inputs, repository)
        name = (name or graph_row.name).strip()
        loop_project = graph_row.project
        at = self._clock()
        row = LoopRow(
            loop_id=f"loop-{uuid.uuid4().hex[:12]}",
            project=loop_project,
            name=name,
            graph_id=graph_row.graph_id,
            version_id=version.version_id,
            instruction=request.instruction,
            repository=request.repository,
            inputs=dict(request.inputs),
            interval_seconds=interval,
            max_prs=limits[0],
            max_spend_usd=limits[1],
            state="active",
            pause_reason="",
            next_run_at=now_iso(at if start_now else at + timedelta(seconds=interval)),
            active_run_id=None,
            created_at=now_iso(at),
            updated_at=now_iso(at),
        )
        async with self._lock:
            if self.store.loops_named(name, loop_project):
                raise Conflict(f"a loop named {name!r} already exists in project {loop_project!r}")
            self.store.insert_loop(row)
        return await self.loop_json(row.loop_id)

    async def list_loops(self, project: str | None) -> list[dict[str, Any]]:
        return [
            await self.loop_json(row.loop_id)
            for row in self.store.loops(_project(project) if project else None)
        ]

    def resolve_loop(self, project: str | None, reference: str) -> LoopRow:
        reference = reference.strip()
        if reference.startswith("loop-"):
            row = self.store.loop(reference)
            if row is None:
                raise NotFound(f"no loop {reference}")
            return row
        matches = self.store.loops_named(reference, _project(project) if project else None)
        if not matches:
            raise NotFound(f"no loop named {reference!r}")
        if len(matches) > 1:
            raise Conflict(
                f"loop name {reference!r} is ambiguous; name a project or use a loop id",
                candidates=[{"loopId": row.loop_id, "project": row.project} for row in matches],
            )
        return matches[0]  # pyright: ignore[reportGeneralTypeIssues]  # Baseline: see docs/pyright.md

    async def loop_json(self, loop_id: str) -> dict[str, Any]:
        row = self.store.loop(loop_id)
        if row is None:
            raise NotFound(f"no loop {loop_id}")
        graph = self.store.graph(row.graph_id)
        version = self.store.version(row.version_id)
        accounting = await self.accounting(row)
        remaining = (
            max(row.max_spend_usd - accounting.spend_usd, 0.0)
            if row.max_spend_usd is not None else None
        )
        latest: dict[str, Any] | None = None
        for _tick, run_id in reversed(self.store.loop_runs(row.loop_id)):
            if latest := await self._completed_output(run_id):
                break
        return {
            "loopId": row.loop_id,
            "name": row.name,
            "project": row.project,
            "graph": graph.name if graph else row.graph_id,
            "graphId": row.graph_id,
            "versionId": row.version_id,
            "version": version.number if version else None,
            "instruction": row.instruction,
            "repository": row.repository,
            "inputs": row.inputs,
            "everySeconds": row.interval_seconds,
            "state": row.state,
            "pauseReason": row.pause_reason or None,
            "nextRunAt": row.next_run_at if row.state == "active" else None,
            "activeRunId": row.active_run_id,
            "runs": accounting.runs,
            "prCount": accounting.pull_requests,
            "spend": accounting.json(),
            "limits": {
                "maxPrs": row.max_prs,
                "maxSpendUsd": row.max_spend_usd,
                # While a run is active it may spend what is left; nothing else may.
                "reservedUsd": (
                    round(remaining, 6) if remaining is not None and row.active_run_id else None
                ),
                "spendEnforceable": (
                    None if row.max_spend_usd is None else accounting.complete
                ),
                "spendScope": SPEND_SCOPE,
            },
            "latestOutput": latest,
            "createdAt": row.created_at,
            "updatedAt": row.updated_at,
        }

    async def _completed_output(self, run_id: str) -> dict[str, Any] | None:
        """What a run produced, with its id, once it has completed; otherwise nothing."""
        try:
            snapshot = await self.runtime.snapshot(RunId(run_id))
        except (UnknownRunError, UnknownGraphError):
            return None
        if snapshot is None or snapshot.status is not RunStatus.COMPLETED:
            return None
        results = _results(self.runtime.topology(snapshot.graph_id), snapshot.values or {})
        output = _output(self.store.executions(run_id), results)
        return {"runId": run_id, **output} if output else None

    async def pause_loop(self, project: str | None, reference: str, reason: str = "") -> dict[str, Any]:
        row = self.resolve_loop(project, reference)
        async with self._lock:
            self.store.update_loop(
                row.loop_id, state="paused", pause_reason=reason.strip() or "paused by a person",
                updated_at=self._now_iso(),
            )
        return await self.loop_json(row.loop_id)

    async def resume_loop(
        self,
        project: str | None,
        reference: str,
        *,
        max_prs: object = None,
        max_spend_usd: object = None,
    ) -> dict[str, Any]:
        """Resume, optionally with new limits; refused while a limit is still reached."""
        row = self.resolve_loop(project, reference)
        limits = _limits(
            row.max_prs if max_prs is None else max_prs,
            row.max_spend_usd if max_spend_usd is None else max_spend_usd,
        )
        candidate = replace(row, max_prs=limits[0], max_spend_usd=limits[1])
        if reached := self._limit_reached(candidate, await self.accounting(candidate)):
            raise Conflict(f"cannot resume: {reached}; raise the limit to resume")
        async with self._lock:
            self.store.update_loop(
                row.loop_id, state="active", pause_reason="", max_prs=limits[0],
                max_spend_usd=limits[1], next_run_at=self._now_iso(), updated_at=self._now_iso(),
            )
        return await self.loop_json(row.loop_id)

    async def accounting(self, row: LoopRow) -> Accounting:
        spend, complete, estimated = 0.0, True, False
        pull_requests: set[tuple[str, int]] = set()
        runs = 0
        for _tick, run_id in self.store.loop_runs(row.loop_id):
            if not run_id:
                continue
            runs += 1
            total = usage_rollup(self.runtime.store.events_since(RunId(run_id))).total
            if total.cost_usd is not None:
                spend += total.cost_usd
            complete = complete and total.complete
            estimated = estimated or total.estimated
            pull_requests.update(await self.runtime.store.pull_requests(RunId(run_id)))
        return Accounting(spend, complete, estimated, len(pull_requests), runs)

    async def tick(self) -> None:
        """Start every loop that is due, once.

        A tick is identified by the time it was due, and recorded before its
        run starts, so a duplicate tick -- a second scheduler, or a retry after
        a crash between the two -- finds it taken and starts nothing. A loop
        whose run is still going keeps its due tick until that run ends, which
        is what coalesces every tick missed meanwhile into one.
        """
        now = self._clock()
        for row in self.store.loops():
            if row.state != "active" or parse_iso(row.next_run_at) > now:
                continue
            try:
                await self._tick_loop(row, now)
            except Exception:
                log.exception("loop %s could not be ticked", row.loop_id)

    async def _tick_loop(self, row: LoopRow, now: datetime) -> None:
        if row.active_run_id:
            try:
                snapshot = await self.runtime.snapshot(RunId(row.active_run_id))
            except (UnknownRunError, UnknownGraphError):
                snapshot = None
            if snapshot is not None and snapshot.status not in TERMINAL_RUN_STATUSES:
                return
            self.store.update_loop(row.loop_id, active_run_id=None, updated_at=now_iso(now))
            row = replace(row, active_run_id=None)
        if reached := self._limit_reached(row, await self.accounting(row)):
            await self._pause(row.loop_id, reached)
            return
        tick = row.next_run_at
        following = _following(parse_iso(tick), row.interval_seconds, now)
        with self.store.transaction() as connection:
            fresh = self.store.loop(row.loop_id)
            if fresh is None or fresh.state != "active" or fresh.next_run_at != tick:
                return
            if not self.store.claim_tick(connection, row.loop_id, tick, now_iso(now)):
                connection.execute(
                    "UPDATE cli_loops SET next_run_at = ? WHERE loop_id = ?",
                    (now_iso(following), row.loop_id),
                )
                return
            connection.execute(
                "UPDATE cli_loops SET next_run_at = ?, updated_at = ? WHERE loop_id = ?",
                (now_iso(following), now_iso(now), row.loop_id),
            )
        version = self._version(row.version_id)
        try:
            run_id = await self._start_version(
                version, StartRequest(row.instruction, row.repository, row.inputs)
            )
        except Exception as failed:
            self.store.release_tick(row.loop_id, tick)
            if auth := auth_required(str(failed), ""):
                await self._pause(row.loop_id, _auth_reason(auth))
            log.exception("loop %s could not start a run", row.loop_id)
            return
        self.store.set_tick_run(row.loop_id, tick, str(run_id))
        self.store.update_loop(row.loop_id, active_run_id=str(run_id), updated_at=self._now_iso())

    async def _schedule(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("the loop scheduler failed a pass")
            await asyncio.sleep(self._tick_seconds)

    def _limit_reached(self, row: LoopRow, accounting: Accounting) -> str:
        if row.max_prs is not None and accounting.pull_requests >= row.max_prs:
            return f"max-prs reached ({accounting.pull_requests} of {row.max_prs} pull requests opened)"
        if row.max_spend_usd is not None and accounting.spend_usd >= row.max_spend_usd:
            return f"max-spend reached (${accounting.spend_usd:.2f} of ${row.max_spend_usd:.2f})"
        return ""

    async def _pause(self, loop_id: str, reason: str) -> None:
        self.store.update_loop(loop_id, state="paused", pause_reason=reason, updated_at=self._now_iso())
        log.info("loop %s paused: %s", loop_id, reason)

    # --- the event feed -----------------------------------------------------

    async def observe(self, event: RuntimeEvent) -> None:
        """Keep executions, steering and loop budgets current from the run feed."""
        try:
            await self._observe(event)
        except Exception:
            log.exception("graph service could not record %s for %s", event.kind.value, event.run_id)

    async def _observe(self, event: RuntimeEvent) -> None:
        run_id, at = str(event.run_id), self._now_iso()
        kind, execution = event.kind, str(event.execution_id or "")
        if kind is EventKind.NODE_STARTED and execution and event.node_id:
            self.store.start_execution(run_id, str(event.node_id), execution, at)
        elif kind is EventKind.NODE_FINISHED and execution:
            self.store.set_execution_status(execution, "completed", at)
            self.store.abandon_undelivered_steering("the node finished first", execution_id=execution)
        elif kind is EventKind.APPROVAL_REQUESTED and execution:
            if not event.payload.get("autoApproved"):
                self.store.set_execution_status(execution, "awaiting_approval")
        elif kind is EventKind.APPROVAL_RESOLVED and execution:
            row = self.store.execution(execution)
            if row is not None and row.status == "awaiting_approval":
                self.store.set_execution_status(execution, "running")
        elif kind is EventKind.STEERING_DELIVERED:
            self.store.mark_steering(str(event.payload.get("steeringId", "")), "delivered", at)
        elif kind is EventKind.STEERING_APPLIED:
            self.store.mark_steering(str(event.payload.get("steeringId", "")), "applied", at)
        elif kind is EventKind.RUN_FORKED:
            self.store.close_open_executions(
                "interrupted", at, run_id=run_id, error="the run was sent back to an earlier checkpoint"
            )
            self.store.abandon_undelivered_steering("the run was sent back first", run_id=run_id)
        elif kind is EventKind.RUN_FINISHED:
            self.store.close_open_executions("completed", at, run_id=run_id)
            self.store.abandon_undelivered_steering("the run finished first", run_id=run_id)
            await self._settle_loop(run_id)
        elif kind is EventKind.RUN_FAILED:
            error = str(event.payload.get("error", ""))
            if error == CANCELLED:
                self.store.close_open_executions("cancelled", at, run_id=run_id, error=error)
            else:
                if event.node_id:
                    self.store.close_open_executions(
                        "failed", at, run_id=run_id, node_id=str(event.node_id), error=error
                    )
                self.store.close_open_executions("interrupted", at, run_id=run_id, error=error)
            self.store.abandon_undelivered_steering("the run stopped first", run_id=run_id)
            await self._settle_loop(run_id, error=error, node_id=str(event.node_id or ""))
        elif kind is EventKind.USAGE_UPDATED:
            await self._enforce_spend(run_id)

    async def _settle_loop(self, run_id: str, *, error: str = "", node_id: str = "") -> None:
        loop_id = self.store.loop_for_run(run_id)
        if loop_id is None:
            return
        row = self.store.loop(loop_id)
        if row is None:
            return
        if error and error != CANCELLED:
            topology, values, overrides = await self._run_context(run_id)
            if auth := auth_required(error, _runner_of(node_id, values, overrides, topology)):
                await self._pause(loop_id, _auth_reason(auth))
                return
        if row.state == "active" and (reached := self._limit_reached(row, await self.accounting(row))):
            await self._pause(loop_id, reached)

    async def _enforce_spend(self, run_id: str) -> None:
        """Stop a loop's run as soon as the loop's spend reaches its cap."""
        loop_id = self.store.loop_for_run(run_id)
        row = self.store.loop(loop_id) if loop_id else None
        if row is None or row.max_spend_usd is None:
            return
        accounting = await self.accounting(row)
        if accounting.spend_usd < row.max_spend_usd:
            return
        if row.state == "active":
            await self._pause(row.loop_id, self._limit_reached(row, accounting))
        snapshot = await self.runtime.snapshot(RunId(run_id))
        if snapshot is not None and snapshot.status not in TERMINAL_RUN_STATUSES:
            # Not awaited here: this runs inside the run's own event delivery,
            # and cancelling a run waits for the very task that is delivering.
            task = asyncio.create_task(self.runtime.cancel(RunId(run_id)))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    # --- helpers ------------------------------------------------------------

    def _register(self, version: VersionRow, *, name: str = "") -> GraphWorkflow:
        """Load a stored version and offer it on the runtime under its version id."""
        if version.format == "python":
            graph = self.store.graph(version.graph_id)
            loaded = Loaded(
                self._python_workflow(
                    version.version_id, version.source,
                    name or (graph.name if graph else ""),
                    version.manifest.get("workflowId", ""),
                    number=version.number,
                ),
                None,
            )
        else:
            spec = _parse_yaml(version.source, runners=None)
            loaded = Loaded(
                compile_graph(
                    spec,
                    version_id=version.version_id,
                    number=version.number,
                    workspace_provider=self._workspace_provider,
                    registry=self._registry,
                    session_config=self._session_config,
                    model_tiers=self._model_tiers,
                    agent_models=self._agent_models,
                    default_base_ref=self._default_base_ref,
                ),
                spec,
            )
        definition = loaded.workflow.compiled(self.runtime.checkpointer)
        self.runtime.register(definition)
        self._loaded[version.version_id] = loaded
        return loaded.workflow

    def _python_workflow(
        self, version_id: str, source: str, name: str, workflow_id: str = "", *, number: int = 0,
    ) -> GraphWorkflow:
        """Import an uploaded Python graph, as a workflow directory's file would be."""
        self._sources.mkdir(parents=True, exist_ok=True)
        path = self._sources / f"{version_id.replace('-', '_')}.py"
        path.write_text(source, encoding="utf-8")
        try:
            exported = load_workflow_file(path, session_config=self._session_config)
        except WorkflowLoadError as broken:
            raise GraphError([Problem("$", str(broken).replace(str(path), "the graph"))]) from broken
        candidates = [item for item in exported if isinstance(item, GraphWorkflow)]
        if not candidates:
            raise GraphError([Problem("workflow", "must be built with graph_workflow from engine.graph_runtime_langgraph")])
        wanted = workflow_id or name
        chosen = [item for item in candidates if str(item.graph_id) == wanted] if wanted else []
        if not chosen:
            if len(candidates) > 1:
                ids = ", ".join(str(item.graph_id) for item in candidates)
                raise GraphError([Problem("workflow", f"exports several graphs ({ids}); pass --name to choose one")])
            chosen = candidates
        workflow = chosen[0]
        return replace(
            workflow,
            graph_id=GraphId(version_id),
            name=f"{name or workflow.graph_id} v{number}" if number else workflow.name,
            previous_ids=(),
        )

    def _forget_source(self, version_id: str) -> None:
        (self._sources / f"{version_id.replace('-', '_')}.py").unlink(missing_ok=True)

    async def _start_version(self, version: VersionRow, request: StartRequest) -> RunId:
        loaded = self._loaded.get(version.version_id)
        if loaded is None:
            raise Unavailable(f"graph version {version.version_id} is not loaded on this backend")
        return await self._start(loaded.workflow, request)

    async def _start_directly(self, workflow: GraphWorkflow, request: StartRequest) -> RunId:
        snapshot = await self.runtime.start(
            workflow.graph_id,
            {
                "task": request.instruction,
                "repository": request.repository,
                **({"inputs": dict(request.inputs)} if request.inputs else {}),
            },
        )
        return snapshot.run_id

    def _start_request(
        self,
        version: VersionRow,
        instruction: str,
        inputs: Mapping[str, object] | None,
        repository: str,
    ) -> StartRequest:
        loaded = self._loaded.get(version.version_id)
        if loaded is None:
            raise Unavailable(f"graph version {version.version_id} is not loaded on this backend")
        instruction = (instruction or "").strip()
        if not instruction:
            raise ServiceError("an instruction is required")
        given = {key: "" if value is None else str(value) for key, value in (inputs or {}).items()}
        try:
            resolved = resolve_inputs(tuple(loaded.workflow.inputs), given)
        except ValueError as error:
            raise ServiceError(str(error)) from None
        if loaded.spec is not None:
            self._check_runner_inputs(loaded.spec, resolved)
        repository = self.resolve_repository(
            (repository or (loaded.spec.repository if loaded.spec else "") or self._default_repository).strip()
        )
        if not repository:
            raise ServiceError(
                "no repository: run from inside one, pass --repo, set repository in the graph, "
                "or configure a default on the backend"
            )
        return StartRequest(instruction, repository, resolved)

    def _check_runner_inputs(self, spec: GraphSpec, inputs: Mapping[str, str]) -> None:
        """Refuse a run whose input names an agent this backend does not offer.

        Registration leaves these unchecked, so a graph whose default is an
        agent this backend lacks still registers and runs with another one.
        """
        available = self.runners()
        names = {node.runner.value for node in spec.nodes if node.runner is not None and node.runner.kind == "input"}
        for name in sorted(names):
            chosen = inputs.get(name, "")
            if chosen and chosen not in RUNNER_POLICIES and chosen not in available:
                how = "--agent NAME" if name == "agent" else f"--input {name}=NAME"
                raise ServiceError(
                    f"agent {chosen!r} is not available on this backend "
                    f"(available: {', '.join(available) or 'none'}); choose one with {how}",
                    available=list(available),
                )

    def resolve_repository(self, repository: str) -> str:
        """A `[repos]` name or `owner/repo` as the checkout it names; a path as itself.

        What lets a CLI on another machine say which repository it is standing
        in: it cannot know this host's paths, but it knows the project.
        """
        if not repository or repository == ".":
            return repository
        repos = self._repositories.snapshot.repos
        if repository in repos:
            return str(Path(repos[repository]).expanduser())
        folded = repository.lower().removesuffix(".git")
        for name, path in repos.items():
            if name.lower() == folded:
                return str(Path(path).expanduser())
        return repository

    async def _run_context(
        self, run_id: str,
    ) -> tuple[GraphTopology | None, Mapping[str, Any], Mapping[Any, str]]:
        snapshot = await self.runtime.snapshot(RunId(run_id))
        if snapshot is None:
            raise NotFound(f"no run {run_id}")
        record = await self.runtime.store.run(RunId(run_id))
        return (
            self.runtime.topology(snapshot.graph_id),
            snapshot.values or {},
            record.runner_overrides if record else {},
        )

    def _execution(self, execution_id: str) -> ExecutionRow:
        row = self.store.execution(execution_id)
        if row is None:
            raise NotFound(f"no node execution {execution_id}")
        return row

    def _graph_json(
        self,
        graph: GraphRow,
        version: VersionRow | None,
        *,
        definition: bool = False,
        versions: bool = False,
    ) -> dict[str, Any]:
        manifest = version.manifest if version else {}
        body: dict[str, Any] = {
            "graphId": graph.graph_id,
            "project": graph.project,
            "name": graph.name,
            "format": version.format if version else None,
            "description": manifest.get("description", ""),
            "versionId": version.version_id if version else None,
            "version": version.number if version else None,
            "latestVersionId": graph.latest_version_id,
            "createdAt": graph.created_at,
            "updatedAt": graph.updated_at,
        }
        if definition:
            body["definition"] = manifest
            body["source"] = version.source if version else None
            body["digest"] = version.digest if version else None
        if versions:
            body["versions"] = [
                {"versionId": item.version_id, "version": item.number, "createdAt": item.created_at}
                for item in self.store.versions(graph.graph_id)
            ]
        return body

    def _version(self, version_id: str) -> VersionRow:
        version = self.store.version(version_id)
        if version is None:
            raise NotFound(f"no graph version {version_id}")
        return version

    def _now_iso(self) -> str:
        return now_iso(self._clock())


def _results(topology: Any, values: Mapping[str, Any]) -> dict[str, Any]:
    """Each node's result in a run's state, leaving out checkouts and joins."""
    return {
        str(node.node_id): values[node.node_id]
        for node in (topology.nodes if topology else ())
        if node.kind not in ("workspace", "join") and node.node_id in values
    }


def _output(executions: Sequence[ExecutionRow], results: Mapping[str, Any]) -> dict[str, Any] | None:
    """A run's output: the result of the node that completed last.

    That is the node the run ended on, so a graph that ends in a report has the
    report as its output, whichever route it took to get there.
    """
    finished = [row for row in executions if row.status == "completed" and row.node_id in results]
    if not finished:
        return None
    last = max(reversed(finished), key=lambda row: row.finished_at or "")
    return {"node": last.node_id, "finishedAt": last.finished_at, "value": results[last.node_id]}


def _latest_activity(events: Sequence[RuntimeEvent]) -> dict[str, dict[str, str]]:
    """Per execution: the agent's last message, and the last thing it did."""
    latest: dict[str, dict[str, str]] = {}
    for event in events:
        if not event.execution_id:
            continue
        entry = latest.setdefault(str(event.execution_id), {"message": "", "activity": ""})
        if event.kind is EventKind.TRANSCRIPT and event.payload.get("role") == "assistant":
            entry["message"] = str(event.payload.get("text", "")).strip() or entry["message"]
            entry["activity"] = ""
        elif event.kind is EventKind.TOOL_CALL:
            entry["activity"] = str(event.payload.get("name", ""))
        elif event.kind is EventKind.APPROVAL_REQUESTED and not event.payload.get("autoApproved"):
            entry["activity"] = f"waiting for approval: {event.payload.get('reason', '')}".strip()
    return latest


def _runner_of(
    node_id: str, values: Mapping[str, Any], overrides: Mapping[Any, str], topology: GraphTopology | None,
) -> str:
    """The runner a node's latest execution used, else the one it would use."""
    if not node_id:
        return ""
    used = values.get(f"_runner.{node_id}")
    if isinstance(used, str) and used:
        return used
    if node_id in overrides:
        return overrides[node_id]
    node = topology.node(node_id) if topology else None  # type: ignore[arg-type]
    return node.runner if node else ""


def _execution_json(
    row: ExecutionRow, topology: GraphTopology | None, runner: Callable[[str], str],
) -> dict[str, Any]:
    node = topology.node(row.node_id) if topology else None  # type: ignore[arg-type]
    return {
        "executionId": row.execution_id,
        "runId": row.run_id,
        "node": row.node_id,
        "name": node.name if node else row.node_id,
        "attempt": row.attempt,
        "status": row.status,
        "runner": runner(row.node_id) or None,
        "startedAt": row.started_at,
        "finishedAt": row.finished_at,
        "error": row.error or None,
        "steerable": node is not None and node.kind == "agent" and row.status in OPEN_EXECUTION_STATUSES,
    }


def _progress(
    topology: GraphTopology | None,
    parents: Mapping[str, str],
    executions: Sequence[ExecutionRow],
    activity: Mapping[str, Mapping[str, str]],
    runner: Callable[[str], str],
) -> dict[str, Any]:
    """Where the run is in its graph: every node, the three stages, and what is current.

    Read off the compiled graph, so YAML and Python graphs are drawn alike.
    Nodes come from the graph rather than from what has run, so a client can
    draw the steps still to come. A node's stage is its group (Planning,
    Implementation, Review); the checkout belongs to none.
    """
    if topology is None:
        return {"graphNodes": [], "edges": [], "stages": [], "current": []}
    latest: dict[str, ExecutionRow] = {}
    for row in executions:
        latest[row.node_id] = row
    joins = {str(node.node_id) for node in topology.nodes if node.kind == "join"}
    nodes: list[dict[str, Any]] = []
    for node in topology.nodes:
        node_id = str(node.node_id)
        if node_id in joins:
            continue
        row = latest.get(node_id)
        seen = activity.get(row.execution_id, {}) if row else {}
        nodes.append({
            "id": node_id,
            "name": node.name,
            "kind": node.kind,
            "stage": None if node.kind == "workspace" else _GROUP_STAGES.get(node.group),
            "parent": parents.get(node_id),
            "runner": runner(node_id) or None,
            "status": row.status if row else "pending",
            "executionId": row.execution_id if row else None,
            "attempt": row.attempt if row else 0,
            "latestMessage": seen.get("message") or None,
            "latestActivity": seen.get("activity") or None,
            "error": (row.error or None) if row else None,
        })
    into = {join: [str(e.source) for e in topology.edges if str(e.target) == join] for join in joins}
    edges: list[list[str]] = []
    for edge in topology.edges:
        source, target = str(edge.source), str(edge.target)
        if target in joins:
            continue
        for origin in into.get(source, [source]):
            if [origin, target] not in edges:
                edges.append([origin, target])
    stages = []
    for stage in STAGES:
        members = [node for node in nodes if node["stage"] == stage]
        statuses = {node["status"] for node in members}
        if not members:
            state = "skipped"
        elif statuses & {"running", "awaiting_approval"}:
            state = "active"
        elif "failed" in statuses:
            state = "failed"
        elif "completed" in statuses:
            # Nodes still pending here are branches this run did not take.
            state = "done"
        elif statuses & {"cancelled", "interrupted"}:
            state = "stopped"
        else:
            state = "pending"
        stages.append({"stage": stage, "status": state, "nodes": [node["id"] for node in members]})
    current = [node["id"] for node in nodes if node["status"] in OPEN_EXECUTION_STATUSES]
    return {"graphNodes": nodes, "edges": edges, "stages": stages, "current": current}


def _carries_credential(url: str) -> bool:
    """Whether `url`'s query string names a key, token or other secret, e.g. `?api_key=...`."""
    return any(
        _CREDENTIAL_PARAMETER.search(re.sub(r"[^a-z]", "", name.lower()))
        for name, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)
    )


def session_model(row: AgentRow) -> str:
    """The model an added agent's sessions ask for: on its own endpoint, OpenCode's `provider/model`."""
    return f"{OPENCODE_URL_PROVIDER}/{row.model}" if row.url else row.model


def opencode_config(row: AgentRow) -> dict[str, Any]:
    """The `OPENCODE_CONFIG_CONTENT` an opencode agent runs with: its model, and its endpoint if any."""
    config: dict[str, Any] = {"model": session_model(row)} if row.model else {}
    if row.url:
        config["provider"] = {
            OPENCODE_URL_PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": row.name,
                "options": {"baseURL": row.url},
                "models": {row.model: {"name": row.model}},
            },
        }
    return config


def _parse_yaml(source: str, *, runners: Sequence[str] | None) -> GraphSpec:
    try:
        raw = yaml.safe_load(source)
    except yaml.YAMLError as error:
        raise GraphError([Problem("$", f"not valid YAML: {error}")]) from None
    return parse_graph(raw, runners=runners)


def auth_required(error: str, runner: str) -> dict[str, str] | None:
    """A sign-in instruction, when a failure reads as missing runner credentials."""
    if not error or not _AUTH_FAILURE.search(error):
        return None
    target = runner or "<runner>"
    return {
        "runner": runner or "",
        "command": f"engine agent signin {target}",
        "message": (
            f"agent {target} is not signed in on this backend; run "
            f"`engine agent signin {target}` and retry"
        ),
    }


def _auth_reason(auth: Mapping[str, str]) -> str:
    return f"authentication required: {auth['message']}"


def _steering_json(row: SteeringRow) -> dict[str, Any]:
    return {
        "steeringId": row.steering_id,
        "sequence": row.sequence,
        "executionId": row.execution_id,
        "runId": row.run_id,
        "message": row.message,
        "status": row.status,
        "error": row.error or None,
        "acceptedAt": row.accepted_at,
        "deliveredAt": row.delivered_at,
        "appliedAt": row.applied_at,
    }


def _limits(max_prs: object, max_spend_usd: object) -> tuple[int | None, float | None]:
    prs: int | None = None
    if max_prs is not None:
        if isinstance(max_prs, bool) or not isinstance(max_prs, int) or max_prs < 1:
            raise ServiceError("max-prs must be a whole number of at least 1")
        prs = max_prs
    spend: float | None = None
    if max_spend_usd is not None:
        if (
            isinstance(max_spend_usd, bool)
            or not isinstance(max_spend_usd, (int, float))
            or not max_spend_usd > 0
            or max_spend_usd == float("inf")
        ):
            raise ServiceError("max-spend must be a positive number of US dollars")
        spend = float(max_spend_usd)
    return prs, spend


def _following(due: datetime, interval: int, now: datetime) -> datetime:
    """The first tick after `now`: ticks missed meanwhile collapse into this one."""
    step = timedelta(seconds=interval)
    if due + step > now:
        return due + step
    missed = int((now - due) / step)
    return due + step * (missed + 1)


def _project(project: str | None) -> str:
    return (project or DEFAULT_PROJECT).strip() or DEFAULT_PROJECT


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


__all__ = [
    "AGENT_KINDS",
    "Accounting",
    "AgentFactory",
    "Conflict",
    "DEFAULT_PROJECT",
    "GraphService",
    "NotFound",
    "SPEND_SCOPE",
    "ServiceError",
    "StartRequest",
    "StartRun",
    "Unavailable",
    "auth_required",
    "opencode_config",
    "session_model",
]
