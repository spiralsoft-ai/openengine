"""HTTP surface for the assistant-ui client.

The engine owns conversations; assistant-ui owns their presentation.  This
module translates between those two vocabularies and keeps the small amount of
thread metadata that is UI-specific (title, archive status, selected runner).

Runs are streamed as newline-delimited JSON.  Their tasks are owned by the
service rather than by one response, so a refreshed browser can reconnect.
A lock per thread prevents two turns from reading the same stale transcript.

Approvals have their own replayable event feed. Their durable record is loaded
when a browser subscribes, while process-local notifications wake that feed for
later transitions without polling the transcript.
"""

from __future__ import annotations

from engine.github_concierge import Continuation, FeedbackRequest, GithubConcierge
from engine.github_concierge.github_egress import tool_permission as github_tool_permission
from engine.slack_concierge import SlackConcierge, SlackIngress
from engine.slack_concierge.slack_egress import tool_permission
from langgraph_acp.agent import ACPAgentProvider
from langgraph_acp.providers import CodexACPProvider

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from importlib.metadata import version
from html import escape
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from engine.apps.web import source_control as source_control_settings
from engine.apps.web.graph_progress import GraphProgress
from engine.apps.web.loop_runs import (
    Loop,
    LoopHost,
    LoopRunner,
    LoopStore,
    loop_defaults,
    parse_loop,
    tool_permission as loop_tool_permission,
)
from engine.apps.web.loops import LoopSettingsStore, parse_loop_settings
from engine.apps.web.github_activity import GithubActivityLog, activity_json
from engine.apps.web.github_communications import (
    GITHUB_CHANNEL_PREFIX,
    ChannelRoutedCommunications,
    GithubCommunications,
)
from engine.apps.web.github_ingress import (
    GithubAssignment, GithubComment, GithubIngress, GithubMerge, GithubReviewRequest,
    github_co_author, github_requester, mentions_other_accounts,
)
from engine.apps.web.github_login import STREAM_ACCESS, GitHubLogin, GitHubLoginConfig
from engine.apps.web.github_auth import (
    DeviceFlowComplete,
    DeviceFlowState,
    GitHubAuthError,
    GitHubCredentialStore,
    credentials_from_device_flow,
    poll_device_flow,
    start_device_flow,
)
from engine.apps.web.gitlab_auth import (
    DeviceFlowComplete as GitLabDeviceFlowComplete,
    GitLabAuthError,
    GitLabCredentialStore,
    credentials_from_device_flow as gitlab_credentials_from_device_flow,
    normalize_origin as normalize_gitlab_origin,
    poll_device_flow as poll_gitlab_device_flow,
    start_device_flow as start_gitlab_device_flow,
)
from engine.apps.web.source_control import (
    SourceControlPreferences,
)
from engine.apps.web.utilization import (
    UtilizationService,
    utilization_json,
)
from engine.adapters.communications.slack import (
    SlackAuthError,
    SlackCommunications,
    SlackCredentialStore,
    authorization_url as slack_authorization_url,
    exchange_code as exchange_slack_code,
    revoke_token as revoke_slack_token,
    verify_signature as verify_slack_signature,
)
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentRunId,
    AgentRunStatus,
    ApprovalDecision,
    ApprovalId,
    ApprovalKind,
    ApprovalRecord,
    ForgeMode,
    MODE_INPUT,
    Message,
    STATE_INPUT,
    WorkState,
    Role,
    RunId,
    RunOrigin,
    RunPhase,
    RunState,
    TaskId,
    WorkflowId,
    WorkspaceId,
    review_inputs,
)
from engine.graph_runtime.inputs import LEAST_UTILIZED, choose_runners, resolve_inputs
from engine.graph_runtime import (
    EventKind,
    EventLog,
    GraphCompilationError,
    GraphId,
    GraphRuntime,
    GraphRuntimeError,
    GraphWorkflow,
    NodeId,
    RunSnapshot,
    RunStatus,
    RuntimeEvent,
    UnknownGraphError,
)
from engine.graph_runtime import create_app as create_graph_app
from engine.graph_service import GraphService, StartRequest, StartRun
from engine.graph_service import create_app as create_graph_service_app
from engine.graph_runtime.usage import usage_rollup
from engine.graph_runtime_langgraph.components.human_review import (
    TOOL_NAME as HUMAN_REVIEW_TOOL,
)
from engine.graph_runtime_langgraph.store import PullRequestRecord
from engine.ports import (
    AgentRunner,
    ApprovalHandler,
    InteractiveAgentRunner,
    Message as CommunicationsMessage,
    MessageLink,
    StateStore,
    UserInputAnswer,
    WorkspaceState,
)
from engine.runtime.repositories import RepositoryRegistry
from engine.runtime.change_requests import change_request, pull_request_url, remote_project
from engine.runtime import (
    AgentSession,
    ApprovalBroker,
    ApprovalConfig,
    ApprovalDecisionNotAllowedError,
    ApprovalNotPendingError,
    RunNotifier,
    RunReader,
    UnknownApprovalError,
    UserInputNotAllowedError,
    WorkflowCatalog,
    WorkflowRunView,
    WorkOrdersConfig,
    load_engine_config,
    load_workflow_catalog,
)
import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send


@dataclass(slots=True)
class ChatThread:
    """UI metadata for one engine agent instance."""

    instance_id: AgentInstanceId
    agent_id: AgentId
    runner: str
    title: str = "New chat"
    archived: bool = False
    workspace_root: str | None = None
    workspace_id: WorkspaceId | None = None
    workspace_ref: str | None = None
    """What to check out to read this chat's work, checkout or no checkout."""


class ActiveRun:
    """One agent turn whose lifetime is independent of an HTTP connection.

    Subscribers receive complete content snapshots, so a browser that refreshes
    can reconnect without needing to know which individual events it missed. An
    approval is the same idea and for the same reason: the turn is paused on a
    question, and a subscriber that arrives after it was asked has to be told
    the question rather than left watching a stream that has gone quiet.
    """

    def __init__(
        self,
        agent_run_id: AgentRunId,
        known_tool_call_ids: Iterable[str] = (),
    ) -> None:
        self.agent_run_id = agent_run_id
        self.content: list[dict[str, object]] = []
        # assistant-ui registers tool calls as resources by id and throws when
        # one occurs twice. Providers can replay an already completed item, so
        # keep the ids from earlier turns as well as the parts in this one.
        self._tool_calls: dict[str, dict[str, object]] = {
            call_id: {} for call_id in known_tool_call_ids
        }
        self.approvals: dict[str, dict[str, object]] = {}
        """The latest snapshot of every request this run has raised, by id.

        A map rather than "the one the turn is on", because what a subscriber
        needs is each request's *transition*: a turn let go by a decision often
        asks its next question before anyone has been told about the answer, and
        a single slot would hand the new question over in place of it -- leaving
        a card waiting forever on a request that was decided.
        """
        self._approval_transitions: list[dict[str, object]] = []
        """Every distinct state the run stream must deliver, in order.

        The latest-state map makes reconnect snapshots cheap, but cannot serve
        as an event queue: a pending request may become decided before the
        stream task next runs. Keeping the transitions separately makes stream
        delivery independent of event-loop scheduling.
        """
        self.error: str | None = None
        self.done = False
        self._revision = 0
        self._changed = asyncio.Condition()
        self._task: asyncio.Task[None] | None = None

    def start(self, say: Awaitable[str]) -> None:
        self._task = asyncio.create_task(self._run(say))

    async def cancel(self) -> None:
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def stream(self) -> AsyncIterator[bytes]:
        revision = 0
        transition_index = 0
        sent: dict[str, dict[str, object]] = {}
        while True:
            async with self._changed:
                await self._changed.wait_for(
                    lambda: self._revision > revision or self.done
                )
                revision = self._revision
                content = [dict(part) for part in self.content]
                transitions = [
                    dict(value)
                    for value in self._approval_transitions[transition_index:]
                ]
                transition_index += len(transitions)
                error = self.error
                done = self.done

            for approval in transitions:
                approval_id = str(approval["id"])
                # Whole snapshots, including the resolved ones: a client that
                # missed the decision would otherwise go on showing a prompt
                # for a request that has already been answered. Every one that
                # has moved rather than only the newest, because several can
                # move between two wakes and the one being answered is exactly
                # the one that would be dropped. Emitted before the terminal
                # events so the last thing said about a request is never lost to
                # the run ending in the same breath.
                if sent.get(approval_id) == approval:
                    continue
                sent[approval_id] = approval
                yield _json_line({"type": "approval", "approval": approval})
            if error is not None:
                yield _json_line({"type": "error", "error": error})
                return
            if done:
                yield _json_line({"type": "done", "content": content})
                return
            yield _json_line({"type": "content", "content": content})

    async def _run(self, say: Awaitable[str]) -> None:
        try:
            answer = await say
            if answer and not any(
                part.get("type") == "text" and part.get("text") == answer
                for part in self.content
            ):
                self.content.append({"type": "text", "text": answer})
            await self._finish()
        except Exception as error:  # noqa: BLE001 -- #779: agent task publishes error in the completed run
            self.error = f"{type(error).__name__}: {error}"
            await self._finish()
        except asyncio.CancelledError:
            await self._finish()
            raise

    async def observe(self, message: Message) -> None:
        if not _merge_message(self.content, message, self._tool_calls):
            return
        async with self._changed:
            self._revision += 1
            self._changed.notify_all()

    async def present_approval(self, approval: ApprovalRecord) -> None:
        """Publish what the turn is waiting on, and wake the subscribers.

        For a pause: nothing else is going to happen on this run until somebody
        answers, so this is the only thing that will wake anyone.
        """
        snapshot = _approval_json(approval)
        async with self._changed:
            self.approvals[str(approval.approval_id)] = snapshot
            self._approval_transitions.append(snapshot)
            self._revision += 1
            self._changed.notify_all()

    def note_approval(self, approval: ApprovalRecord) -> None:
        """Update the snapshot without waking anyone, for a run that is ending.

        Synchronous on purpose. The wake that matters is the one the run's own
        ending sends a moment later, and awaiting a lock here would yield the
        event loop back to the very turn being torn down.
        """
        snapshot = _approval_json(approval)
        self.approvals[str(approval.approval_id)] = snapshot
        self._approval_transitions.append(snapshot)

    async def _finish(self) -> None:
        async with self._changed:
            self.done = True
            self._revision += 1
            self._changed.notify_all()


class ApprovalFeed:
    """Replay durable approval snapshots, then push each later transition.

    Persistence remains the source of truth. The condition is only a wake-up
    signal, so reconnecting after a lost HTTP connection cannot lose an event.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._revisions: dict[AgentInstanceId, int] = {}
        self._changed: dict[AgentInstanceId, asyncio.Condition] = {}

    async def publish(self, approval: ApprovalRecord) -> None:
        condition = self._changed.setdefault(approval.instance_id, asyncio.Condition())
        async with condition:
            self._revisions[approval.instance_id] = (
                self._revisions.get(approval.instance_id, 0) + 1
            )
            condition.notify_all()

    async def stream(self, instance_id: AgentInstanceId) -> AsyncIterator[bytes]:
        condition = self._changed.setdefault(instance_id, asyncio.Condition())
        sent: dict[str, dict[str, object]] = {}
        # Flush the response immediately even when this conversation has never
        # asked for approval. EventSource ignores comment frames.
        yield b": connected\n\n"
        while True:
            revision = self._revisions.get(instance_id, 0)
            approvals = await self._store.list_approvals(instance_id=instance_id)
            for record in approvals:
                approval = _approval_json(record)
                approval_id = str(record.approval_id)
                if sent.get(approval_id) == approval:
                    continue
                sent[approval_id] = approval
                yield _server_event(approval)

            async with condition:
                await condition.wait_for(
                    lambda: self._revisions.get(instance_id, 0) > revision
                )


class WebGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Starlette excludes SSE already. Older supported versions can buffer
        # NDJSON, so keep both live and resumed agent turns uncompressed too.
        path = scope.get("path", "")
        if path.startswith("/api/threads/") and path.endswith(
            ("/runs", "/runs/current")
        ):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


class BuiltClient(StaticFiles):
    """The Vite build, cached the way its filenames say it should be.

    Asset names carry a content hash, so those files are safe to keep forever
    and are never the reason a browser is out of date. The page that *names*
    them is the opposite: served without instructions, browsers cache it
    heuristically and go on asking for the hashed files of a build that no
    longer exists, which arrives as a blank page and a pair of 404s. So the
    entry point is revalidated every time and the hashed assets are not.
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        immutable = Path(full_path).parent.name == "assets"
        response.headers["cache-control"] = (
            "public, max-age=31536000, immutable" if immutable else "no-cache"
        )
        return response


class ThreadService:
    """Coordinates assistant-ui threads over an ``AgentSession``."""

    def __init__(
        self,
        session: AgentSession,
        runners: Mapping[str, AgentRunner],
        approval_policy: ApprovalConfig = ApprovalConfig(),
        *,
        approval_observer: Callable[[ApprovalRecord], Awaitable[None]] | None = None,
    ) -> None:
        self.session = session
        self.approvals = ApprovalBroker(
            session.state_store, approval_policy, observe=approval_observer
        )
        """Public alongside `session`: the same durable boundary, for pauses."""
        self._runners = runners
        self._threads: dict[AgentInstanceId, ChatThread] = {}
        self._locks: dict[AgentInstanceId, asyncio.Lock] = {}
        self._active_runs: dict[AgentInstanceId, ActiveRun] = {}
        self._restored = False
        self._restore_lock = asyncio.Lock()

    async def list(self) -> tuple[ChatThread, ...]:
        await self._restore()
        return tuple(reversed(self._threads.values()))

    async def get(self, instance_id: AgentInstanceId) -> ChatThread | None:
        await self._restore()
        thread = self._threads.get(instance_id)
        if thread is not None:
            return thread
        # A conversation may be created after this web process restored its
        # initial registry. Resolve direct links from the durable store instead
        # of requiring a server restart.
        instance = await self.session.instance(instance_id)
        if instance is None:
            return None
        thread = ChatThread(
            instance.instance_id,
            instance.agent_id,
            (
                instance.runner
                if instance.runner in self.session.runners
                else self.session.default_runner
            ),
            title=instance.title,
            archived=instance.archived,
        )
        self._threads[instance.instance_id] = await self._sync_workspace(thread)
        self._locks[instance.instance_id] = asyncio.Lock()
        return thread

    async def create(self, agent_id: AgentId, runner: str) -> ChatThread:
        await self._restore()
        if runner not in self.session.runners:
            raise ValueError(f"unknown runner {runner!r}")
        instance = await self.session.start(agent_id, runner=runner)
        thread = ChatThread(instance.instance_id, agent_id, runner)
        await self._sync_workspace(thread)
        self._threads[instance.instance_id] = thread
        self._locks[instance.instance_id] = asyncio.Lock()
        return thread

    async def attach_workspace(self, instance_id: AgentInstanceId, repository: str | None = None) -> ChatThread:
        """Give this chat a checkout again -- or a first one."""
        thread = await self._require_idle(instance_id)
        async with self._locks[instance_id]:
            state = await self.session.attach_workspace(instance_id, repository)
        return self._apply_workspace_state(thread, state)

    async def detach_workspace(self, instance_id: AgentInstanceId) -> ChatThread:
        """Release this chat's checkout, keeping its work on the branch."""
        thread = await self._require_idle(instance_id)
        async with self._locks[instance_id]:
            state = await self.session.detach_workspace(instance_id)
        return self._apply_workspace_state(thread, state)

    def _apply_workspace_state(
        self, thread: ChatThread, state: WorkspaceState | None
    ) -> ChatThread:
        """Refresh every loaded conversation sharing the changed workspace."""
        workspace_id = state.workspace_id if state is not None else thread.workspace_id
        if workspace_id is not None:
            for cached in self._threads.values():
                if cached.workspace_id == workspace_id:
                    _with_workspace(cached, state)
        return _with_workspace(thread, state)

    async def _require_idle(self, instance_id: AgentInstanceId) -> ChatThread:
        """A workspace is not the agent's to lose in the middle of using it.

        The turn lock alone would serialize this correctly but leave the
        request hanging for as long as the agent runs, which reads as a broken
        button rather than a busy one.
        """
        thread = await self._require(instance_id)
        if self.active_run(instance_id) is not None:
            raise RuntimeError("this chat has a run in progress")
        return thread

    async def delete(self, instance_id: AgentInstanceId) -> None:
        await self._restore()
        self._threads.pop(instance_id, None)
        self._locks.pop(instance_id, None)

    async def history(self, instance_id: AgentInstanceId) -> tuple[Message, ...]:
        await self._require(instance_id)
        return await self.session.history(instance_id)

    async def say(
        self,
        instance_id: AgentInstanceId,
        text: str,
        runner: str | None,
        observed: asyncio.Queue[Message],
        on_approval: ApprovalHandler | None = None,
        agent_run_id: AgentRunId | None = None,
    ) -> str:
        thread = await self._require(instance_id)
        selected_runner = runner or thread.runner
        if selected_runner not in self.session.runners:
            raise ValueError(f"unknown runner {selected_runner!r}")
        thread.runner = selected_runner
        await self._persist_metadata(thread)

        async with self._locks[instance_id]:
            turn = await self.session.say(
                instance_id,
                text,
                runner=selected_runner,
                on_message=observed.put_nowait,
                on_approval=on_approval,
                agent_run_id=agent_run_id,
            )
        return turn.message.content

    async def start_run(
        self, instance_id: AgentInstanceId, text: str, runner: str | None
    ) -> ActiveRun:
        thread = await self._require(instance_id)
        await self.require_somewhere_to_run(instance_id)
        history = await self.session.history(instance_id)
        initial_message_count = len(history)
        current = self.active_run(instance_id)
        if current is not None:
            raise RuntimeError("this chat already has a run in progress")

        observed: asyncio.Queue[Message] = asyncio.Queue()
        # Named before it starts, because the approvals it raises are brokered
        # against this run and a decision has to be able to name it too.
        agent_run_id = _new_agent_run_id()
        selected_runner = runner or thread.runner
        run = ActiveRun(agent_run_id, _tool_call_ids(history))
        self._active_runs[instance_id] = run
        on_approval = None
        # The runner the session will hand this turn to, not the one the name
        # alone would pick: an agent that only reads is answered by a different
        # object, and both whether it can pause and how it reads its own
        # requests are that object's to say. An unknown name or an agent this
        # process no longer composes leaves this unresolved, and the turn then
        # fails in the session exactly where it failed before.
        profile = self.session.profiles.get(thread.agent_id)
        selected = (
            self.session.runner_for(thread.agent_id, selected_runner)
            if profile is not None and selected_runner in self.session.runners
            else None
        )
        if profile is not None and isinstance(selected, InteractiveAgentRunner):
            on_approval = self.approvals.handler(
                agent_run_id=agent_run_id,
                instance_id=instance_id,
                runner=selected_runner,
                present=run.present_approval,
                # Where this turn will actually work, so consent it collects is
                # bounded by the same worktree the agent is standing in.
                workspace_id=thread.workspace_id,
                # How this provider's requests read as Engine capabilities, so
                # the configured policy has something to evaluate them against.
                translator=selected.permission_translator,
                # And what this agent is, which the policy does not get to
                # widen: a planner asking to edit is refused here, whatever the
                # deployment allows a coder.
                read_only=profile.read_only,
            )

        async def execute() -> str:
            task = asyncio.create_task(
                self.say(
                    instance_id,
                    text,
                    selected_runner,
                    observed,
                    on_approval,
                    agent_run_id,
                )
            )
            try:
                while not task.done() or not observed.empty():
                    try:
                        async with asyncio.timeout(0.1):
                            message = await observed.get()
                    except TimeoutError:
                        continue
                    await run.observe(message)
                return await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if on_approval is not None:
                    # However this turn ended, nothing is waiting on its
                    # requests any more -- a provider that died mid-question
                    # leaves one here. Every one of them, because a client is
                    # showing whichever it was last told about, and any of those
                    # has to stop saying "pending", whoever resolved it.
                    await self.approvals.interrupt_run(agent_run_id)
                    for asked in await self.session.state_store.list_approvals(
                        agent_run_id=agent_run_id
                    ):
                        run.note_approval(asked)

        run.start(execute())
        # Ensure a refresh can load the submitted question before this POST
        # starts returning streamed response bytes.
        while (
            len(await self.session.history(instance_id)) <= initial_message_count
            and not run.done
        ):
            await asyncio.sleep(0)
        return run

    def active_run(self, instance_id: AgentInstanceId) -> ActiveRun | None:
        run = self._active_runs.get(instance_id)
        return run if run is not None and not run.done else None

    def latest_run(self, instance_id: AgentInstanceId) -> ActiveRun | None:
        """The latest run, including a just-finished run needed by a racing resume."""
        return self._active_runs.get(instance_id)

    async def decide_approval(
        self,
        instance_id: AgentInstanceId,
        approval_id: ApprovalId,
        decision: str,
        agent_run_id: AgentRunId | None = None,
    ) -> ApprovalRecord:
        """Answer what this chat's current run is paused on.

        Scoped to the run rather than the conversation: an id from a turn that
        has already ended names a provider process nobody can resume, and
        applying its answer to whatever is running now would approve a command
        the user never saw.
        """
        await self._require(instance_id)
        run = self.active_run(instance_id)
        try:
            chosen = ApprovalDecision(decision)
        except ValueError:
            raise ApprovalDecisionNotAllowedError(
                f"unknown decision {decision!r}"
            ) from None
        record = await self.approvals.decide(
            approval_id,
            chosen,
            instance_id=instance_id,
            agent_run_id=run.agent_run_id if run is not None else agent_run_id,
        )
        if run is not None:
            await run.present_approval(record)
        return record

    async def answer_question(
        self,
        instance_id: AgentInstanceId,
        approval_id: ApprovalId,
        answers: tuple[UserInputAnswer, ...],
        agent_run_id: AgentRunId | None = None,
    ) -> ApprovalRecord:
        """Answer a structured prompt from this chat's current run."""

        await self._require(instance_id)
        run = self.active_run(instance_id)
        record = await self.approvals.answer(
            approval_id,
            answers,
            instance_id=instance_id,
            agent_run_id=run.agent_run_id if run is not None else agent_run_id,
        )
        if run is not None:
            await run.present_approval(record)
        return record

    async def stop_run(self, instance_id: AgentInstanceId) -> None:
        """Stop this chat's run, whether it is working or waiting on a person.

        What it was waiting on is resolved as a cancellation before the turn is
        torn down, so the answer to "was that command allowed?" is a recorded
        no rather than a row that stops mid-sentence. Tearing the turn down
        then does the rest: cancelling one request would not oblige the agent
        to stop asking, and stopping means stopping.
        """
        run = self.active_run(instance_id)
        if run is None:
            return
        for resolved in await self.approvals.cancel_run(run.agent_run_id):
            run.note_approval(resolved)
        await run.cancel()
        await self._record_cancelled(run.agent_run_id)

    async def _record_cancelled(self, agent_run_id: AgentRunId) -> None:
        """Record the stopped run as a cancellation, however the turn ended.

        A cancelled approval is a decision the provider can act on, so a
        well-behaved one answers it by tidying up and returning -- and a turn
        that returns is a turn the session records as a success. Left there,
        stopping a paused run would read afterwards as one that finished
        normally. Whatever the provider made of the last second, the user
        withdrew this turn.
        """
        store = self.session.state_store
        agent_run = await store.agent_run(agent_run_id)  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        if agent_run is None or agent_run.status is AgentRunStatus.CANCELLED:
            return
        await store.record_agent_run(
            replace(agent_run, status=AgentRunStatus.CANCELLED, summary="cancelled")
        )

    async def generate_title(
        self,
        instance_id: AgentInstanceId,
        opening_text: str | None = None,
        runner: str | None = None,
    ) -> str:
        """Ask the thread's agent for a title without changing its transcript."""
        thread = await self._require(instance_id)
        if thread.title != "New chat":
            return thread.title
        selected_runner = runner or thread.runner
        if selected_runner not in self.session.runners:
            raise ValueError(f"unknown runner {selected_runner!r}")
        async with self._locks[instance_id]:
            if thread.title != "New chat":
                return thread.title
            history = await self.session.history(instance_id)
            title_context = (
                (*history, Message.user(opening_text)) if opening_text else history
            )
            # The session's answer rather than the name's, so a chat with an
            # agent that only reads is named by a runner that only reads too.
            turn = await self.session.runner_for(
                thread.agent_id, selected_runner
            ).run_turn(
                _new_agent_run_id(),
                self.session.profiles[thread.agent_id],
                (*title_context, Message.user(_TITLE_PROMPT)),
                # Naming a chat reads the transcript, not the tree, so a
                # detached one is named where the process runs rather than
                # failing on a directory it does not need.
                workspace_id=thread.workspace_id if thread.workspace_root else None,
            )
        title = _clean_title(turn.message.content)
        if title:
            thread.title = title
            await self._persist_metadata(thread)
        return thread.title

    async def update_metadata(
        self,
        instance_id: AgentInstanceId,
        *,
        title: str | None = None,
        runner: str | None = None,
        archived: bool | None = None,
    ) -> ChatThread:
        thread = await self._require(instance_id)
        if runner is not None and runner not in self.session.runners:
            raise ValueError(f"unknown runner {runner!r}")
        if title is not None:
            thread.title = title
        if runner is not None:
            thread.runner = runner
        if archived is not None:
            thread.archived = archived
        await self._persist_metadata(thread)
        return thread

    async def _persist_metadata(self, thread: ChatThread) -> None:
        await self.session.update_instance_metadata(
            thread.instance_id,
            thread.title,
            thread.archived,
            thread.runner,
        )

    async def _require(self, instance_id: AgentInstanceId) -> ChatThread:
        thread = await self.get(instance_id)
        if thread is None:
            raise KeyError(f"no chat thread {instance_id!r}")
        return thread

    async def require_somewhere_to_run(self, instance_id: AgentInstanceId) -> None:
        """Refuse a turn a detached chat cannot run, in words the UI can act on.

        The runner would fail on the missing directory anyway, several layers
        down and phrased as a lookup error. A chat that never had a workspace
        is left alone: it runs where the process was told to.
        """
        try:
            workspace = await self.session.workspace(instance_id)
            detached = workspace is not None and not workspace.attached
        except KeyError:
            detached = True
        if detached:
            raise RuntimeError(
                "this chat's worktree is detached; reattach it to run the agent"
            )

    async def _sync_workspace(self, thread: ChatThread) -> ChatThread:
        """Record what the provider currently says about this chat's workspace.

        Conversations outlive their checkouts -- `git worktree remove`, a swept
        /tmp, a reboot -- so a chat is listed with whatever is left of its
        workspace rather than failing the request. A provider that disowns the
        id entirely is treated the same way: the chat is simply one without a
        workspace, and attaching offers it a new one.
        """
        try:
            return _with_workspace(
                thread, await self.session.workspace(thread.instance_id)
            )
        except KeyError:
            return _with_workspace(thread, None)

    async def _restore(self) -> None:
        """Populate the UI registry from the durable conversation store once."""
        if self._restored:
            return
        async with self._restore_lock:
            if self._restored:
                return
            instances = await self.session.instances()
            for instance in reversed(instances):
                thread = ChatThread(
                    instance.instance_id,
                    instance.agent_id,
                    (
                        instance.runner
                        if instance.runner in self.session.runners
                        else self.session.default_runner
                    ),
                    title=instance.title,
                    archived=instance.archived,
                )
                self._threads[instance.instance_id] = await self._sync_workspace(thread)
                self._locks[instance.instance_id] = asyncio.Lock()
            # A CLI subprocess does not survive the server that spawned it, so
            # a request still marked pending here was asked by a process that
            # no longer exists and can never be answered.
            await self.approvals.interrupt_orphans()
            self._restored = True


#: Where the graph runtime's sub-application is served from, so its addresses
#: are `/graph/api/runs/...` and cannot collide with this app's own `/api`.
GRAPH_PREFIX = "/graph"

#: Graph lifecycle events that change what a WorkOrder row should read.
GRAPH_EVENT_PHASES: Mapping[EventKind, RunPhase] = {
    EventKind.RUN_FORKED: RunPhase.RUNNING_AGENT,
    EventKind.RUN_FINISHED: RunPhase.SUCCEEDED,
    EventKind.RUN_FAILED: RunPhase.FAILED,
}

#: The same three answers, as the graph engine reports them when asked rather
#: than when it announces them. A run waiting on a person is still working as
#: far as a WorkOrder row is concerned: what it is waiting for is a question
#: only the graph engine's own API can show today.
GRAPH_PHASES: Mapping[RunStatus, RunPhase] = {
    RunStatus.RUNNING: RunPhase.RUNNING_AGENT,
    RunStatus.AWAITING_APPROVAL: RunPhase.RUNNING_AGENT,
    RunStatus.COMPLETED: RunPhase.SUCCEEDED,
    RunStatus.FAILED: RunPhase.FAILED,
}


def _slack_task_report(text: str) -> str:
    """Present structured transcript outputs without exposing Slack mentions."""
    try:
        outputs = json.loads(text)
    except ValueError:
        outputs = None
    if isinstance(outputs, dict) and outputs:
        text = "\n".join(
            f"*{key}*\n\t{value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}"
            for key, value in outputs.items()
        )
    return escape(text, quote=False)


def _graph_workorder_name(values: object) -> str:
    """The concise name a graph's naming node left in its state."""
    if not isinstance(values, Mapping):
        return ""
    value = values.get("name")
    if not isinstance(value, str) or not value.strip():
        return ""
    first_line = value.strip().splitlines()[0]
    return first_line.strip(" \t\"'`).:;!?")[:120]


#: Where this module says what went wrong with something nobody asked it about
#: -- a graph engine that would not open, a stranded run it could not pick back
#: up. Those go to the log rather than to a person, because the person who
#: would read them is not in the room when a server starts.
log = logging.getLogger(__name__)

#: The only graph events a WorkOrder's originating GitHub issue hears about,
#: keyed by kind and node id ("" for run-level events), with what it is told.
#: Node ids are the implementation-review-rerank workflow's. A run finishes
#: once its human review is answered, which the pull request's merge does.
GITHUB_ISSUE_MILESTONES: Mapping[tuple[EventKind, str], str] = {
    (EventKind.NODE_STARTED, "implementation"): "Implementation started.",
    (EventKind.NODE_FINISHED, "reranker"): "Review finished.",
    (EventKind.RUN_FINISHED, ""): "Work order finished.",
}

#: How long the forge lookups that authorize a GitHub comment may take before
#: the comment is abandoned. The ingress behind them has one worker, so this is
#: not only that comment's latency: whatever it waits, every comment queued
#: after it waits too. Long enough to cover a slow-but-working forge, short
#: enough that a hung one costs a redelivery rather than the queue.
GITHUB_AUTHORIZATION_TIMEOUT_SECONDS = 45

#: How long a least-utilized WorkOrder waits on a fresh utilization scrape
#: before it starts from whatever was last cached instead.
UTILIZATION_REFRESH_TIMEOUT_SECONDS = 10

#: How old utilization may be before a least-utilized WorkOrder scrapes again;
#: every scrape reads the runners' stored credentials, so starts share one.
UTILIZATION_MAX_AGE_SECONDS = 60 * 60

#: How long a browser sign-in, or a signed-in request's access recheck, waits
#: on GitHub. Someone is watching a page load, so a hung lookup should fail
#: quickly and be retried rather than hold them for the webhook budget above.
GITHUB_LOGIN_TIMEOUT_SECONDS = 10

#: How long the configured checkouts together get to name their `origin` when
#: a webhook is matched to one. A local `git remote get-url` answers at once;
#: one that does not is on a stalled mount, and is skipped rather than waited on.
GITHUB_CHECKOUT_TIMEOUT_SECONDS = 5


@dataclass(slots=True)
class _GraphSurface:
    """The graph engine, once the server has started it.

    Both fields are empty until the application starts, because opening the
    engine means opening files and that is something a running server owns
    rather than something building one does. A request that arrives before then
    is told the graph engine is not running rather than being given half of it.

    They stay empty when the engine could not be opened for a reason outside
    the graphs themselves, which is what keeps that kind of failure to the
    graph feature: no engine, no graph entries in the dropdown, and the
    rest of the application carries on. A graph that does not *compile* never
    gets this far -- it stops the server, because it is a definition somebody
    has to fix.
    """

    runtime: GraphRuntime | None = None
    app: Starlette | None = None
    service: GraphService | None = None
    """Registered graphs, loops and node steering, when the engine supports them."""
    service_app: Starlette | None = None


class GithubProvenance(Protocol):
    """The half of the runtime's store that says who owns a pull request.

    Named as the pair it is used as: a comment is routed by reading the claim,
    and a work order started for a comment is findable only if it writes one.
    Stated as a protocol because this module reaches the store by duck-typing
    -- the control surface is deliberately forge-agnostic -- and a runtime that
    keeps no provenance answers ``None`` here rather than growing a method
    shaped like a pull request.
    """

    async def run_for_pull_request(self, repository: str, number: int) -> RunId | None: ...

    async def pull_request_for_run(self, run_id: RunId) -> tuple[str, int] | None: ...

    async def claim_pull_request(
        self, record: PullRequestRecord, *, replacing: RunId | None = None
    ) -> RunId: ...


#: The statuses a run can still be steered in. A completed or failed run has
#: no execution listening, so feedback for the pull request it opened is a new
#: request rather than a continuation of that one.
STEERABLE_RUN_STATUSES = frozenset({RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL})


async def _repository_identity(path: Path) -> Path:
    """The git repository `path` is in, shared by all its subfolders and worktrees.

    That is its common git directory; a path outside any repository is its own.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return path
    try:
        async with asyncio.timeout(10):
            output, _ = await process.communicate()
    except TimeoutError:
        process.kill()
        return path
    if process.returncode != 0 or not output.strip():
        return path
    return Path(output.decode().strip()).resolve()


def _reentry_node(runtime: GraphRuntime, snapshot: RunSnapshot) -> NodeId | None:
    """Where feedback re-enters this run, or ``None`` to steer whatever runs.

    Untargeted steering reaches the execution in flight, and there is none once
    a run is parked at human review -- which is exactly when review feedback
    arrives. An always-open node is the graph's own statement of where it may
    be sent back to, so naming it is what makes the ordinary post-pull-request
    case work instead of raising.

    ``None`` when the graph names no such node, or names more than one: a graph
    that has not said where to re-enter has not asked to be reset, and guessing
    between two candidates would reset it somewhere arbitrary.
    """
    topology = runtime.topology(snapshot.graph_id)
    open_nodes = [node for node in topology.nodes if node.always_open] if topology else []
    return open_nodes[0].node_id if len(open_nodes) == 1 else None


def create_app(
    session: AgentSession,
    runners: Mapping[str, AgentRunner],
    static_directory: Path | None = None,
    *,
    workflow_catalog: WorkflowCatalog | None = None,
    graph_runtime: AbstractAsyncContextManager[GraphRuntime] | None = None,
    approval_policy: ApprovalConfig = ApprovalConfig(),
    credential_store: GitHubCredentialStore | None = None,
    github_client_id: str = "",
    github_client_id_source: str = "configuration",
    github_login_config: GitHubLoginConfig | None = None,
    service_token: Callable[[], str] = lambda: "",
    source_control_preferences: SourceControlPreferences | None = None,
    loop_settings: LoopSettingsStore | None = None,
    loop_store: LoopStore | None = None,
    loop_provider: ACPAgentProvider | None = None,
    loop_tick_seconds: float = 60,
    slack_credential_store: SlackCredentialStore | None = None,
    github_webhook_secret: Callable[[], str] = lambda: "",
    github_repository: str = "",
    github_repositories: tuple[str, ...] = (),
    github_comment_handler: Callable[[GithubComment], Awaitable[None]] | None = None,
    communications_channel: str = "",
    public_url: str = "",
    work_orders: WorkOrdersConfig = WorkOrdersConfig(),
    repos: Mapping[str, str] | RepositoryRegistry | None = None,
    repo_modes: Mapping[str, str] | None = None,
    trusted_repos: Collection[str] = (),
    login_repositories: Sequence[str] = (),
    login_operators: Collection[int] = (),
    repository_projects: Mapping[str, str] | None = None,
    utilization: UtilizationService | None = None,
    concierge_provider: ACPAgentProvider | None = None,
    graph_service: Callable[[Any, StartRun], GraphService] | None = None,
) -> Starlette:
    """Build the web application around already-composed capabilities."""
    if workflow_catalog is None:
        loaded_config = load_engine_config()
        catalog = (
            load_workflow_catalog(loaded_config.workflows_directory)
            if loaded_config.workflows_directory is not None
            else WorkflowCatalog.from_graphs(())
        )
    else:
        catalog = workflow_catalog
    # The graph workflows this deployment could run, looked up by the id the
    # dropdown sends back.
    #
    # "Could", not "does": whether they are actually offered is `offered_graphs`
    # below, which additionally asks whether the engine is running.
    graph_workflows: Mapping[str, GraphWorkflow] = (
        {str(graph.graph_id): graph for graph in catalog.graphs}
        if graph_runtime is not None
        else {}
    )
    surface = _GraphSurface()
    repository_registry = repos if isinstance(repos, RepositoryRegistry) else RepositoryRegistry(
        repos, repo_modes, trusted_repos,
        projects=repository_projects or {}, login_repositories=login_repositories,
    )

    async def in_repositories(repository: str, checkouts: frozenset[Path]) -> bool:
        """Whether `repository` is in the same git repository as one of `checkouts`.

        Compared by the git repository the path belongs to rather than by the
        literal path, so a subfolder or another worktree of a checkout counts too.
        """
        if not repository or not checkouts:
            return False
        identity = await _repository_identity(Path(repository).expanduser().resolve())
        for path in checkouts:
            if await _repository_identity(path) == identity:
                return True
        return False

    async def repository_mode(repository: str) -> ForgeMode | None:
        """The mode `[repo_modes]` fixes for WorkOrders on `repository`, if any."""
        if await in_repositories(repository, repository_registry.snapshot.disconnected):
            return ForgeMode.DISCONNECTED
        return None

    # Filled by the graph engine while a run is going, and read by the feed the
    # graph's own sub-application serves. Built here rather than when the
    # server starts so that the observer below can be written once.
    graph_events = EventLog()
    graph_progress: dict[RunId, GraphProgress] = {}

    def seed_graph_progress(runtime: GraphRuntime, snapshot: RunSnapshot) -> None:
        topology = runtime.topology(snapshot.graph_id)
        if topology is not None:
            graph_progress[snapshot.run_id] = GraphProgress(snapshot, topology)

    def offered_graphs() -> Mapping[str, GraphWorkflow]:
        """The graph entries a person may pick, right now.

        Two things have to be true, and the second one is only knowable once
        the server is up: this deployment has graph workflows, and the engine
        that runs them opened. If it did not -- an unwritable state directory,
        a graph that no longer compiles -- there are no graph entries at
        all, rather than entries that fail the moment somebody picks one.
        """
        return graph_workflows if surface.runtime is not None else {}

    approval_feed = ApprovalFeed(session.state_store)
    service = ThreadService(
        session,
        runners,
        approval_policy,
        approval_observer=approval_feed.publish,
    )
    run_reader = RunReader(session.state_store, catalog)

    pending_graph_notifications: dict[RunId, list[RuntimeEvent]] = {}
    deferred_graph_notifications: dict[RunId, RunOrigin] = {}
    # A pull request merged while its work order was still working towards its
    # human review. GitHub sends the merge once, so it is kept until the run
    # asks for that review rather than dropped for having arrived early.
    merges_awaiting_review: dict[RunId, GithubMerge] = {}
    graph_notification_lock = asyncio.Lock()
    dependencies_changed = asyncio.Event()
    dependency_lock = asyncio.Lock()
    deleting_runs: set[RunId] = set()

    async def notify_graph_event(state: RunState, event: RuntimeEvent) -> None:
        """Transcript owns agent text; lifecycle owns starts, actions and errors.

        Graph agents are not offered update_status: the visible transcript is
        the single progress/report path. Completion notices carry the work-order link.
        Never reconstruct agent text from tool payloads or raw provider output.
        """
        text = ""
        links: list[MessageLink] = []
        mention = False
        if state.origin is None:
            return
        topology = (
            surface.runtime.topology(GraphId(str(state.workflow_id)))
            if surface.runtime else None
        )
        node = topology.node(event.node_id) if topology and event.node_id else None
        label = node.name if node else str(event.node_id or "Workflow")
        # A GitHub issue is public, and watched by people who want milestones
        # rather than a running log: the work starting, its review settling,
        # and the run finishing once the merge answers its human review.
        # Everything else -- including errors, approval reasons and agent
        # text, which can hold paths, output or secrets -- stays behind the
        # work order link.
        public = state.origin.channel.startswith(GITHUB_CHANNEL_PREFIX)
        if public:
            milestone = GITHUB_ISSUE_MILESTONES.get((event.kind, str(event.node_id or "")))
            if milestone is None:
                return
            text = milestone
        elif event.kind is EventKind.RUN_FORKED:
            # Chat surfaces answer the resume request themselves.
            return
        elif event.kind is EventKind.TRANSCRIPT:
            # Assistant role alone is not authorship: human/tool nodes also
            # narrate their work in the UI. Their notifications are lifecycle-owned.
            if node is None or node.kind != "agent":
                return
            if event.payload.get("role", "assistant") != "assistant":
                return
            report = event.payload.get("text")
            if not isinstance(report, str) or not report.strip():
                return
            # Preserve UI redactions and escape Slack mention/link syntax.
            text = _slack_task_report(report)
        elif event.kind is EventKind.NODE_STARTED:
            text = f"*{label}* started."
        elif event.kind is EventKind.APPROVAL_REQUESTED:
            if event.payload.get("autoApproved"):
                # The run answers this one itself, so there is nobody to ask.
                # A single work order asks to run dozens of commands, and
                # announcing each would bury the requests that are real.
                return
            if event.payload.get("toolName") == HUMAN_REVIEW_TOOL:
                text = "Review complete and ready for your decision."
                snapshot = await surface.runtime.snapshot(state.run_id) if surface.runtime else None
                pr_url = snapshot.values.get("pr_url") if snapshot else None
                if isinstance(pr_url, str) and pr_url.strip():
                    links.append(MessageLink("View pull request", pr_url))
            else:
                text = f"*{label}* needs your approval: {event.payload.get('reason', '')}"
            mention = True
        elif event.kind is EventKind.RUN_FAILED:
            text = f"Work order failed: {event.payload.get('error', 'Unknown error')}"
            mention = True
        elif event.kind is EventKind.RUN_FINISHED:
            text = "Work order finished."
        if text:
            link = run_notifier.work_order_link(state)
            if link and (public or event.kind in (
                EventKind.APPROVAL_REQUESTED, EventKind.RUN_FAILED, EventKind.RUN_FINISHED,
            )):
                links.append(link)
            if public and not any(
                existing.label == "View pull request" for existing in links
            ):
                # The issue timeline is the run's history, so every update
                # carries the pull request once the run has opened one. The
                # link is optional: a failed lookup must not fail the run or
                # drop the update, since this observer runs inside the graph.
                try:
                    opened = await github_pull_request_for_run(str(state.run_id))
                except Exception:
                    log.exception("could not look up the pull request for run %s", state.run_id)
                    opened = None
                if opened is not None:
                    links.append(MessageLink("View pull request", pull_request_url(*opened)))
            try:
                await run_notifier.deliver(
                    state, text, links=links, mention=mention,
                    progress=event.kind in (
                        EventKind.NODE_STARTED, EventKind.NODE_FINISHED, EventKind.RUN_FINISHED,
                    ),
                )
            except Exception:  # noqa: BLE001 -- #779: notification boundary reports NOTIFICATION_FAILED
                # Exceptions may contain credentials or request bodies. Record
                # only a fixed diagnostic in OE's replayable event feed.
                await graph_events.append(RuntimeEvent(
                    run_id=state.run_id, kind=EventKind.NOTIFICATION_FAILED,
                    node_id=event.node_id, execution_id=event.execution_id,
                    payload={"error": "GitHub issue comment could not be posted." if public
                             else "Slack notification could not be delivered.",
                             "eventKind": event.kind.value},
                ))

    async def graph_notifications(event: RuntimeEvent) -> None:
        if event.kind not in (
            EventKind.NODE_STARTED, EventKind.NODE_FINISHED, EventKind.APPROVAL_REQUESTED,
            EventKind.RUN_FAILED, EventKind.RUN_FINISHED,
            EventKind.TRANSCRIPT, EventKind.RUN_FORKED,
        ):
            return
        async with graph_notification_lock:
            state = await session.state_store.load(event.run_id)
            if state is None or event.run_id in deferred_graph_notifications:
                pending_graph_notifications.setdefault(event.run_id, []).append(event)
                return
            for pending in pending_graph_notifications.pop(event.run_id, []):
                await notify_graph_event(state, pending)
            await notify_graph_event(state, event)

    async def graph_event(event: RuntimeEvent) -> None:
        """Everything the graph engine says, kept where two readers can see it.

        The feed is one reader: a browser or a script watching a graph run gets
        these back in order from the sub-application below.

        The WorkOrder row is the other. A graph run keeps its real progress in
        the graph engine's own files, and this app only holds a row for it, so
        without this the row would say "an agent is working" long after the run
        had finished or fallen over. Restarts, endings, and the name produced by
        a naming node are copied across. Node and approval events also maintain
        the small frontier projection read by the runs list.
        """
        if event.kind is EventKind.RUN_FORKED and event.run_id not in graph_progress:
            # Terminal rows are not restored at startup, but can be resumed later.
            state = await session.state_store.load(event.run_id)
            if state is not None and surface.runtime is not None:
                seed_graph_progress(surface.runtime, RunSnapshot(
                    run_id=event.run_id,
                    graph_id=GraphId(str(state.workflow_id)),
                    status=RunStatus.RUNNING,
                ))
        if progress := graph_progress.get(event.run_id):
            progress.apply(event)
        await graph_events.append(event)
        if surface.service is not None:
            await surface.service.observe(event)
        await graph_notifications(event)
        if (
            event.kind is EventKind.APPROVAL_REQUESTED
            and event.payload.get("toolName") == HUMAN_REVIEW_TOOL
            and event.run_id in merges_awaiting_review
        ):
            # After the notification, so the review step is still shown before
            # the merge that already answered it is recorded against it.
            try:
                await github_accept_merged_review(event.run_id)
            except Exception:
                log.exception(
                    "could not accept the human review of work order %s from its merge",
                    event.run_id,
                )
        phase = GRAPH_EVENT_PHASES.get(event.kind)
        name = _graph_workorder_name(event.payload.get("values"))
        if phase is None and not name:
            return
        state = await session.state_store.load(event.run_id)
        if state is None:
            return
        updated = replace(
            state,
            name=name or state.name,
            phase=phase or state.phase,
            failure_reason=(
                "" if event.kind is EventKind.RUN_FORKED
                else str(event.payload.get("error", "")) or state.failure_reason
            ),
        )
        if updated != state:
            await session.state_store.save(updated)
        if updated.phase is RunPhase.SUCCEEDED:
            dependencies_changed.set()

    async def open_graph_service(runtime: GraphRuntime, opened: AsyncExitStack) -> None:
        """Serve `/api/v1`, for `engine graph|loop|node`, on this engine.

        Contained like the engine itself: a failure here takes registered
        graphs and loops offline and leaves everything else serving.
        """
        if graph_service is None or getattr(runtime, "checkpointer", None) is None:
            return

        async def start(workflow: GraphWorkflow, request: StartRequest) -> RunId:
            # The WorkOrder path, so a CLI run is listed, approved under this
            # deployment's policy and reported like any other.
            state = await start_graph_run(
                runtime,
                workflow,
                inputs=dict(request.inputs),
                prompt=request.instruction,
                repository=request.repository,
            )
            return state.run_id

        try:
            service = graph_service(runtime, start)
            await service.open()
        except Exception:
            log.exception("registered graphs are not being offered in this process")
            return
        opened.push_async_callback(service.aclose)
        surface.service = service
        surface.service_app = create_graph_service_app(service)

    async def graph_service_surface(scope: Scope, receive: Receive, send: Send) -> None:
        """Pass `/api/v1` to the registered-graph service, for operators only.

        Its runs, loops and steering span every repository, so a signed-in
        user who sees only some of them is refused rather than shown all.
        """
        if surface.service_app is None:
            await JSONResponse(
                {"error": "this process is not running graph workflows"}, status_code=503,
            )(scope, receive, send)
            return
        if scope["type"] == "http":
            if await github_login.visible_repositories(Request(scope)) is not None:
                await _error("the graph API is for operators", 403)(scope, receive, send)
                return
        await surface.service_app(scope, receive, send)

    async def restore_graph_runs(runtime: GraphRuntime) -> None:
        """Pick every unfinished graph WorkOrder back up, or say why it cannot be.

        A run's progress lives in the graph engine's files, but the *driver* --
        the thing actually working through the graph -- is a task in a process,
        and a process that stops takes its drivers with it. Nothing rebuilds
        them on its own, so without this a run that was mid-agent when the
        server was restarted would sit at "working" forever, saying nothing and
        doing nothing.

        Three answers, one per thing the engine can say about a run:

        * **working** -- there is no driver for it in this fresh process, so it
          is sent back to the last position it saved and carried on from there.
          Whatever the interrupted agent had done since that position is lost,
          which is the honest cost of the process having died mid-sentence;
        * **waiting on a person** -- left exactly as it is. Answering the
          question is what starts it again, and that already works;
        * **finished or failed** -- the row missed the ending because the
          process was gone when it was announced, so it is copied over now.

        A run the engine has never heard of is one whose state was deleted from
        under it. It cannot be recovered and cannot be waited for, so the row is
        failed with a reason rather than left claiming to be working.

        So is a run of a workflow this deployment no longer has -- withdrawn,
        or renamed without retiring the id it had. Nothing can pick that one up
        either, and a row left mid-flight would say an agent is working on it
        forever, so it is failed saying which workflow went missing.
        """
        for state in await session.state_store.list_runs():
            if state.is_terminal or state.phase is RunPhase.SCHEDULED:
                continue
            try:
                try:
                    snapshot = await runtime.snapshot(state.run_id)
                except UnknownGraphError:
                    await session.state_store.save(
                        replace(
                            state,
                            phase=RunPhase.FAILED,
                            failure_reason=(
                                "the workflow this WorkOrder ran, "
                                f"{state.workflow_id}, is no longer available"
                            ),
                        )
                    )
                    continue
                if snapshot is None:
                    await session.state_store.save(
                        replace(
                            state,
                            phase=RunPhase.FAILED,
                            failure_reason=(
                                "the graph engine has no record of this run"
                            ),
                        )
                    )
                    continue
                seed_graph_progress(runtime, snapshot)
                if snapshot.status is RunStatus.RUNNING:
                    # A restart is the only way to be here: a run that is
                    # working has a driver, and this runs before any request
                    # could have started one.
                    if snapshot.checkpoint_id is None:
                        continue
                    await runtime.resume_from(state.run_id, snapshot.checkpoint_id)
                    continue
                phase = GRAPH_PHASES[snapshot.status]
                name = _graph_workorder_name(snapshot.values)
                if (
                    phase is not state.phase
                    or snapshot.error != state.failure_reason
                    or (name and name != state.name)
                ):
                    await session.state_store.save(
                        replace(
                            state,
                            name=name or state.name,
                            phase=phase,
                            failure_reason=snapshot.error or state.failure_reason,
                        )
                    )
            except Exception:
                # One unrecoverable run must not stop the others from being
                # recovered, and none of them may stop the server from serving.
                log.exception("could not restore graph WorkOrder %s", state.run_id)

    ready = False
    service_version = version("engine-web")

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"service": "openengine", "version": service_version,
             "ready": ready, "api_version": 1},
            status_code=200 if ready else 503,
            headers={"Cache-Control": "no-store"},
        )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        nonlocal ready
        async with AsyncExitStack() as opened:
            opened.push_async_callback(slack_ingress.close)
            opened.push_async_callback(github_concierge.close)
            opened.push_async_callback(github_ingress.close)
            if graph_runtime is not None:
                # Opening the graph engine is what makes a graph WorkOrder
                # startable: it compiles every graph in the workflow directory
                # and opens the files they remember their progress in. The exit
                # stack closes it again when the server stops, which is the
                # only thing that closes those files.
                #
                # It can fail two ways, and they are not the same kind of news.
                #
                # A graph that does not compile is a broken definition: a file
                # in this deployment's workflow directory says something that is
                # not a graph. Nothing about it improves by carrying on, and a
                # server that quietly dropped it would be running a deployment
                # nobody configured. So the graph is named, the reason is logged
                # in full, and startup fails -- loudly, at the moment somebody
                # is looking, rather than the first time a person picks it.
                #
                # Anything else is the environment around the graphs rather than
                # the graphs themselves: a state directory this process cannot
                # write, a checkpoint file another process is holding. That is
                # not a reason for chats, projects and the step WorkOrders to go
                # down with it, so it is logged and contained -- no engine, and
                # therefore no graph entries offered anywhere.
                try:
                    surface.runtime = await opened.enter_async_context(graph_runtime)
                    bind_creator = getattr(surface.runtime, "bind_workorder_creator", None)
                    if bind_creator is not None:
                        bind_creator(agent_create_workorder)
                except GraphCompilationError as broken:
                    log.error(
                        "graph workflow %r does not compile, so this server "
                        "will not start: %s",
                        str(broken.graph_id),
                        broken.reason,
                        exc_info=True,
                    )
                    raise
                except Exception:
                    log.exception(
                        "the graph engine did not start; graph WorkOrders are "
                        "not being offered in this process"
                    )
                else:
                    # The graph engine's own control surface, so a run started
                    # here can be watched and answered. Built first because it
                    # installs a listener of its own, and this app wants that
                    # listener *and* the WorkOrder row kept up to date -- so
                    # ours is installed afterwards and does both.
                    surface.app = create_graph_app(surface.runtime, graph_events)
                    surface.runtime.observe(graph_event)
                    # Registered graphs are offered again before runs are
                    # restored, since a run resumes only if its graph exists.
                    await open_graph_service(surface.runtime, opened)
                    await restore_graph_runs(surface.runtime)
                    dependency_task = asyncio.create_task(dispatch_dependencies())
                    dependencies_changed.set()

                    async def stop_dependencies() -> None:
                        dependency_task.cancel()
                        await asyncio.gather(dependency_task, return_exceptions=True)

                    opened.push_async_callback(stop_dependencies)
                    loops_task = asyncio.create_task(dispatch_loops())

                    async def stop_loops() -> None:
                        loops_task.cancel()
                        await asyncio.gather(loops_task, return_exceptions=True)

                    opened.push_async_callback(stop_loops)
            ready = graph_runtime is None or surface.runtime is not None
            try:
                yield
            finally:
                ready = False

    def workflow_is_active(thread: ChatThread) -> bool:
        return (
            thread.workflow_run_id is not None  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            and thread.workflow_run_id in workflow_tasks  # pyright: ignore[reportAttributeAccessIssue, reportUndefinedVariable]  # Baseline: see docs/pyright.md
        )

    async def interrupt_workflow(thread: ChatThread) -> None:
        """Stop the active process for an editable step without failing its run."""

        if not thread.editable or thread.workflow_run_id is None:  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            raise RuntimeError("this workflow conversation is read-only")
        state = await session.state_store.load(thread.workflow_run_id)  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        if (
            state is None
            or state.phase is not RunPhase.RUNNING_AGENT
            or state.current_step_id != thread.workflow_step_id  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        ):
            raise RuntimeError("this workflow step is no longer active")
        if state.current_agent_run_id is not None:  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            await service.approvals.cancel_run(state.current_agent_run_id)  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        task = workflow_tasks.get(thread.workflow_run_id)  # pyright: ignore[reportAttributeAccessIssue, reportUndefinedVariable]  # Baseline: see docs/pyright.md
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert thread.workflow_step_id is not None  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        await workflow_executor.pause_agent_step(  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
            thread.workflow_run_id, thread.workflow_step_id  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        )

    async def switch_workflow_runner(thread: ChatThread) -> None:
        """Restart an active workflow turn on its conversation's new runner."""

        assert thread.workflow_run_id is not None  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        lock = workflow_restart_locks.setdefault(thread.workflow_run_id, asyncio.Lock())  # pyright: ignore[reportAttributeAccessIssue, reportUndefinedVariable]  # Baseline: see docs/pyright.md
        async with lock:
            task = workflow_tasks.get(thread.workflow_run_id)  # pyright: ignore[reportAttributeAccessIssue, reportUndefinedVariable]  # Baseline: see docs/pyright.md
            if task is None or task.done():
                return
            state = await session.state_store.load(thread.workflow_run_id)  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            if (
                state is None
                or state.phase is not RunPhase.RUNNING_AGENT
                or state.current_step_id != thread.workflow_step_id  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            ):
                return
            if state.current_agent_run_id is not None:  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
                await service.approvals.cancel_run(state.current_agent_run_id)  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            # The completed turn may have advanced to another agent between the
            # first state read and cancellation. Resume whichever conversation is
            # now current, without applying this conversation's choice to another.
            state = await session.state_store.load(thread.workflow_run_id)  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            if (
                state is None
                or state.phase is not RunPhase.RUNNING_AGENT
                or state.agent_paused  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            ):
                return
            runner_name = await workflow_runner_for(state)  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
            track_workflow(  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
                state.run_id,
                asyncio.create_task(
                    workflow_executor.resume_agent_step(  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
                        state.run_id, runner_name=runner_name
                    )
                ),
            )

    async def continue_workflow(
        thread: ChatThread, text: str, *, active_only: bool = False, resume_only: bool = False,
    ) -> None:
        """Serialize web and Slack continuations for the same WorkOrder."""
        assert thread.workflow_run_id is not None  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        lock = workflow_restart_locks.setdefault(thread.workflow_run_id, asyncio.Lock())  # pyright: ignore[reportAttributeAccessIssue, reportUndefinedVariable]  # Baseline: see docs/pyright.md
        async with lock:
            await continue_workflow_locked(thread, text, active_only=active_only, resume_only=resume_only)

    async def continue_workflow_locked(
        thread: ChatThread, text: str, *, active_only: bool = False, resume_only: bool = False,
    ) -> None:
        """Interrupt, append a human message, and resume the same workflow step."""

        assert thread.workflow_run_id is not None  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        if not thread.editable:  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            raise RuntimeError("this workflow conversation is read-only")
        await service.require_somewhere_to_run(thread.instance_id)
        before = len(await service.history(thread.instance_id))
        state = await session.state_store.load(thread.workflow_run_id)  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        if state is None:
            raise RuntimeError("this workflow step is no longer active")
        if resume_only:
            executing = workflow_tasks.get(thread.workflow_run_id)  # pyright: ignore[reportAttributeAccessIssue, reportUndefinedVariable]  # Baseline: see docs/pyright.md
            if executing is not None and not executing.done():
                raise RuntimeError("this work order already has an execution in progress; use steering")
            if state.phase not in (RunPhase.SUCCEEDED, RunPhase.FAILED, RunPhase.AWAITING_HUMAN_REVIEW):  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
                raise RuntimeError("this work order has not finished; use steering for running work or the WorkOrder page for paused work")
            pending = await session.state_store.list_approvals(instance_id=thread.instance_id)
            if any(record.status.value == "pending" for record in pending):
                raise RuntimeError("answer the pending decision on the WorkOrder page first")
        if active_only:
            if (
                state.phase is not RunPhase.RUNNING_AGENT
                or state.current_step_id != thread.workflow_step_id  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
                or state.agent_paused  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            ):
                raise RuntimeError("this work order is not running an agent; continue it on the WorkOrder page")
            pending = await session.state_store.list_approvals(instance_id=thread.instance_id)
            if any(record.status.value == "pending" for record in pending):
                raise RuntimeError("this agent needs a decision on the WorkOrder page before Slack steering")
        if state.current_agent_run_id is not None:  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            await service.approvals.cancel_run(state.current_agent_run_id)  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        task = workflow_tasks.get(thread.workflow_run_id)  # pyright: ignore[reportAttributeAccessIssue, reportUndefinedVariable]  # Baseline: see docs/pyright.md
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        latest = await session.state_store.load(thread.workflow_run_id)  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        if active_only and (
            latest is None or latest.phase is not RunPhase.RUNNING_AGENT
            or latest.current_step_id != thread.workflow_step_id  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        ):
            # The run may advance while cancellation is in progress. Do not
            # reopen the old step, or strand a newer one we interrupted.
            if latest is not None and latest.phase is RunPhase.RUNNING_AGENT and not latest.agent_paused:  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
                track_workflow(latest.run_id, asyncio.create_task(  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
                    workflow_executor.resume_agent_step(latest.run_id)  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
                ))
            raise RuntimeError("the workflow moved to another step before the instruction could be delivered; try again")
        if (
            state.phase is RunPhase.RUNNING_AGENT
            and state.current_step_id == thread.workflow_step_id  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        ):
            await workflow_executor.pause_agent_step(  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
                thread.workflow_run_id, thread.workflow_step_id  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            )
        task = asyncio.create_task(
            workflow_executor.resume_agent_step(  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
                thread.workflow_run_id,  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
                text,
                thread.runner,
                step_id=thread.workflow_step_id,  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            )
        )
        track_workflow(thread.workflow_run_id, task)  # pyright: ignore[reportAttributeAccessIssue, reportUndefinedVariable]  # Baseline: see docs/pyright.md
        if active_only or resume_only:
            while True:
                history = await service.history(thread.instance_id)
                if any(message.role is Role.USER and message.content == text for message in history[before:]):
                    return
                if task.done():
                    raise RuntimeError("the agent stopped before the instruction was recorded; check the WorkOrder page")
                # The agent task records the message independently. Yielding with
                # zero delay turns this acknowledgement wait into a hot loop.
                await asyncio.sleep(0.01)
        while (
            len(await service.history(thread.instance_id)) <= before and not task.done()
        ):
            await asyncio.sleep(0.01)
        if task.done() and not task.cancelled() and task.exception() is not None:
            raise RuntimeError(str(task.exception()))

    async def stream_workflow_conversation(
        instance_id: AgentInstanceId, run_id: RunId
    ) -> AsyncIterator[bytes]:
        """Poll durable workflow progress into the chat client's snapshot stream."""
        previous: list[dict[str, object]] | None = None
        previous_approvals: dict[str, dict[str, object]] = {}
        while True:
            history = await service.history(instance_id)
            content = _latest_assistant_content(history)  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
            approvals = await session.state_store.list_approvals(
                instance_id=instance_id
            )
            active = run_id in workflow_tasks  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
            for record in approvals:
                approval = _approval_json(record)
                approval_id = str(record.approval_id)
                if approval != previous_approvals.get(approval_id):
                    previous_approvals[approval_id] = approval
                    yield _json_line({"type": "approval", "approval": approval})
            if not active:
                yield _json_line({"type": "done", "content": content})
                return
            if content != previous:
                previous = content
                yield _json_line({"type": "content", "content": content})
            await asyncio.sleep(0.25)

    async def config(request: Request) -> JSONResponse:
        visible = await github_login.visible_repositories(request)
        configured = repository_registry.snapshot
        repository_choices = [
            {
                "name": name,
                "path": str(Path(path).expanduser().resolve()),
                **({"mode": mode} if (mode := configured.repo_modes.get(name)) else {}),
            }
            for name, path in configured.repos.items()
        ] or [{"name": f". ({Path.cwd()})", "path": "."}]
        return JSONResponse(
            {
                "agents": [
                    {
                        "id": str(agent_id),
                        "description": profile.description,
                        "instructions": profile.instructions,
                    }
                    for agent_id, profile in sorted(session.profiles.items())
                ],
                "runners": [
                    {"id": name, "implementation": type(runner).__name__}
                    for name, runner in runners.items()
                ],
                "defaultAgent": str(next(iter(sorted(session.profiles)))),
                "defaultRunner": session.default_runner,
                "repositories": [
                    choice for choice in repository_choices
                    if repository_visible(visible, choice["path"])
                ],
                # Only the graphs this process can actually start are here --
                # see `offered_graphs` -- because an entry nobody could run
                # would be a choice that fails after it was made. Their
                # creation fields come from the workflow's own declarations.
                "workflows": [
                    {
                        "id": str(graph.graph_id),
                        "name": graph.name,
                        **(
                            {"inputs": [asdict(item) for item in graph.inputs]}  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
                            if getattr(graph, "inputs", ()) else {}
                        ),
                    }
                    for graph in offered_graphs().values()
                ],
            }
        )

    async def list_threads(request: Request) -> JSONResponse:
        return JSONResponse({"threads": [
            _thread_json(t) for t in await service.list()
            if not await thread_hidden(request, t)
        ]})

    async def list_runs(request: Request) -> JSONResponse:
        visible = await github_login.visible_repositories(request)
        available = (
            {graph.graph_id for graph in surface.runtime.graphs()}
            if surface.runtime else set()
        )
        runs = []
        for run in await run_reader.list():
            if not repository_visible(visible, run.repository):
                continue
            row = _run_json(run, listing=True)
            if run.phase not in {"scheduled", "succeeded", "failed"}:
                progress = graph_progress.get(run.run_id)
                if progress is not None and progress.topology.graph_id in available:
                    row["graphProgress"] = progress.json()
            runs.append(row)
        return JSONResponse({"runs": runs})

    round_robin_turns: dict[str, int] = {}

    async def start_graph_run(
        runtime: GraphRuntime,
        graph: GraphWorkflow,
        *,
        inputs: dict[str, str],
        prompt: str,
        repository: str,
        origin: RunOrigin | None = None,
        scheduled: RunState | None = None,
        defer_notifications: bool = False,
        parent_run_id: RunId | None = None,
        depends_on_run_id: RunId | None = None,
        requester: str | None = None,
    ) -> RunState:
        """Hand a graph WorkOrder to the graph engine and keep a row for it.

        What actually starts the work is one call: the graph engine is given
        the graph's id and the two things every one of these graphs asks for --
        the task to do, and the repository to do it in. It provisions the
        checkout, runs the agents and stops for a person by itself, and it
        remembers all of that in its own files.

        The row saved afterwards is this app's, and it is a record rather than
        a driver: it is what puts the WorkOrder in the list, on the sidebar and
        at a URL. It carries the graph engine's own run id, so the two halves
        are talking about the same run and nothing has to translate between two
        sets of ids.

        Declared inputs are validated before starting and carried in graph state.

        ``requester`` is who asked; a scheduled row keeps its own when none is
        given.

        The engine is an argument rather than something read here, because
        having one is what made this graph offerable in the first place: a
        caller that got a graph out of `offered_graphs` has already established
        that the engine is running, and passing it on says so.
        """
        if requester is None and scheduled is not None:
            requester = scheduled.requester
        # A repository onboarded as disconnected is never reached from, however
        # the WorkOrder was asked for.
        if MODE_INPUT in inputs and (mode := await repository_mode(repository)) is not None:
            inputs = {**inputs, MODE_INPUT: str(mode)}
        async def runner_usage() -> dict[str, float]:
            # Scraped here rather than trusting the cache, which otherwise only
            # fills when someone opens the Utilization page, but at most hourly.
            # A slow or failing scrape falls back to the cache instead of
            # holding up the run.
            try:
                async with asyncio.timeout(UTILIZATION_REFRESH_TIMEOUT_SECONDS):
                    readings = await _utilization.recent(
                        tuple(runners), UTILIZATION_MAX_AGE_SECONDS
                    )
            except Exception:  # Placement must not fail the run; log before falling back.
                log.warning("utilization refresh failed; using cached readings", exc_info=True)
                readings = _utilization.cached()
            return {
                reading.runner: max(window.used_percent for window in reading.windows)
                for reading in readings if reading.windows
            }

        # Read before taking the lock, so a slow scrape does not hold up every
        # other WorkOrder being created, deleted or scoped meanwhile.
        usage = await runner_usage() if LEAST_UTILIZED in (inputs or {}).values() else {}
        # Resolved before the lock: a Slack requester costs a Slack request.
        co_author = await _co_author(requester)
        async with dependency_lock:
            if depends_on_run_id is not None:
                prerequisite = await session.state_store.load(depends_on_run_id)
                if prerequisite is None or depends_on_run_id in deleting_runs:
                    raise ValueError(f"unknown prerequisite workorder: {depends_on_run_id}")
                if prerequisite.phase is not RunPhase.SUCCEEDED:
                    state = RunState(
                        run_id=RunId(f"run-{uuid4().hex[:12]}"),
                        task_id=TaskId(f"task-{uuid4().hex[:12]}"),
                        workflow_id=WorkflowId(str(graph.graph_id)),
                        phase=RunPhase.SCHEDULED,
                        prompt=prompt, repository=repository, origin=origin,
                        parent_run_id=parent_run_id, depends_on_run_id=depends_on_run_id,
                        inputs=inputs, requester=requester,
                    )
                    await session.state_store.save(state)
                    # Recheck after saving to cover completion racing with creation.
                    dependencies_changed.set()
                    return state
            # Policies resolve at start, not at scheduling, so a dependent
            # WorkOrder is placed by utilization when it actually runs.
            inputs = choose_runners(
                getattr(graph, "inputs", ()), inputs,
                usage=lambda: usage,
                turns=round_robin_turns,
            )
            # Taken before start(), which may already run nodes.
            started_at = datetime.now(UTC)
            snapshot = await runtime.start(
                GraphId(str(graph.graph_id)),
                {
                    "task": prompt,
                    "repository": repository,
                    **({"inputs": inputs} if inputs else {}),
                    **({"coAuthor": co_author} if co_author else {}),
                    **({"issue": {"repository": origin.issue_repository, "number": origin.issue_number}}
                       if origin and origin.issue_number else {}),
                },
                run_id=scheduled.run_id if scheduled else None,
            )
            # Register before saving the row: earlier events already wait for
            # the row, and later ones must wait for the concierge reply too.
            if defer_notifications and origin is not None:
                deferred_graph_notifications[snapshot.run_id] = origin
            seed_graph_progress(runtime, snapshot)
            if approval_policy.auto_approve or await in_repositories(repository, repository_registry.snapshot.trusted):
                topology = runtime.topology(GraphId(str(graph.graph_id)))
                if topology is not None:
                    for node in topology.nodes:
                        await runtime.set_auto_approve(snapshot.run_id, node.node_id, True)
            state = RunState(
                run_id=snapshot.run_id,
                task_id=scheduled.task_id if scheduled else TaskId(f"task-{uuid4().hex[:12]}"),
                name=scheduled.name if scheduled else "",
                workflow_id=WorkflowId(str(graph.graph_id)),
                # Working, as the engine has just reported it. `graph_event` above
                # moves this when the run ends.
                phase=GRAPH_PHASES[snapshot.status],
                prompt=prompt,
                repository=repository,
                origin=origin,
                parent_run_id=scheduled.parent_run_id if scheduled else parent_run_id,
                depends_on_run_id=scheduled.depends_on_run_id if scheduled else depends_on_run_id,
                inputs=inputs,
                requester=requester,
                started_at=started_at,
            )
            await session.state_store.save(state)
        # Nodes may publish before start() returns and before the origin exists.
        async with graph_notification_lock:
            if state.run_id not in deferred_graph_notifications:
                for event in pending_graph_notifications.pop(state.run_id, []):
                    await notify_graph_event(state, event)
        # A very short run can be over before the row above exists, and the
        # ending it announced would then have had nothing to land on -- leaving
        # a WorkOrder that claims to be working forever. So the engine is asked
        # once more, now that there is a row for its answer.
        latest = await runtime.snapshot(state.run_id)
        latest_name = (
            _graph_workorder_name(latest.values) if latest is not None else ""
        )
        if latest is not None and (
            GRAPH_PHASES[latest.status] is not state.phase
            or (latest_name and latest_name != state.name)
        ):
            state = replace(
                state,
                name=latest_name or state.name,
                phase=GRAPH_PHASES[latest.status],
                failure_reason=latest.error,
            )
            await session.state_store.save(state)
        if state.phase is RunPhase.SUCCEEDED:
            dependencies_changed.set()
        return state

    async def agent_create_workorder(
        parent_run_id: RunId, prompt: str, depends_on_run_id: RunId | None = None,
    ) -> tuple[str, str]:
        parent = await session.state_store.load(parent_run_id)
        if parent is None:
            raise ValueError("the creating workorder does not exist")
        graph = _mentioned_workflow()
        if graph is None:
            raise RuntimeError("no workflow is configured under `work_orders.workflow`")
        if depends_on_run_id is not None and github_login_config is not None:
            # An agent acts for its own WorkOrder's repository, so another
            # repository's run is as unknown to it as one that does not exist.
            prerequisite = await session.state_store.load(depends_on_run_id)
            if prerequisite is None or not same_repository(
                prerequisite.repository, parent.repository
            ):
                raise ValueError(f"unknown prerequisite workorder: {depends_on_run_id}")
        assert surface.runtime is not None
        state = await start_graph_run(
            surface.runtime, graph,
            inputs=resolve_inputs(getattr(graph, "inputs", ()), {}),
            prompt=prompt, repository=parent.repository,
            parent_run_id=parent.run_id,
            depends_on_run_id=depends_on_run_id, requester=parent.requester,
        )
        link = run_notifier.work_order_link(state)
        return link.url if link else f"/runs/{state.run_id}", str(state.run_id)

    scheduled_start_lock = asyncio.Lock()

    async def dependency_ready(state: RunState) -> bool:
        if state.depends_on_run_id is None:
            return True
        prerequisite = await session.state_store.load(state.depends_on_run_id)
        return prerequisite is not None and prerequisite.phase is RunPhase.SUCCEEDED

    async def dispatch_dependencies() -> None:
        # Iterative dispatch keeps arbitrarily deep chains off the call stack.
        while True:
            await dependencies_changed.wait()
            dependencies_changed.clear()
            async with scheduled_start_lock:
                for state in await session.state_store.list_runs():
                    if (
                        state.phase is not RunPhase.SCHEDULED
                        or state.depends_on_run_id is None
                        or not await dependency_ready(state)
                    ):
                        continue
                    try:
                        graph = offered_graphs()[str(state.workflow_id)]
                        assert surface.runtime is not None
                        await start_graph_run(
                            surface.runtime, graph,
                            inputs=resolve_inputs(getattr(graph, "inputs", ()), state.inputs),
                            prompt=state.prompt,
                            repository=state.repository or work_orders.repository,
                            origin=state.origin,
                            scheduled=state,
                        )
                    except Exception:
                        log.exception("could not start dependent workorder %s", state.run_id)

    async def start_scheduled_run(request: Request) -> JSONResponse:
        run_id = RunId(request.path_params["run_id"])
        if await run_hidden(request, run_id):
            return _error("run not found", 404)
        async with scheduled_start_lock:
            state = await session.state_store.load(run_id)
            if state is None:
                return _error("run not found", 404)
            if state.phase is not RunPhase.SCHEDULED:
                return _error("workorder is already started", 409)
            if not await dependency_ready(state):
                return _error("prerequisite workorder is not complete", 409)
            workflow_id = state.workflow_id or WorkflowId(work_orders.workflow)
            graph = offered_graphs().get(str(workflow_id)) if workflow_id else _mentioned_workflow()
            if graph is None:
                return _error("configure work_orders.workflow before starting this workorder", 400)
            repository = state.repository or work_orders.repository
            if not repository:
                return _error("configure work_orders.repository before starting this workorder", 400)
            assert surface.runtime is not None
            try:
                inputs = resolve_inputs(getattr(graph, "inputs", ()), state.inputs)
            except ValueError as error:
                return _error(str(error), 400)
            state = await start_graph_run(
                surface.runtime, graph, inputs=inputs, prompt=state.prompt,
                repository=repository,
                scheduled=state, origin=state.origin,
                # The proposer stays the requester; whoever clicks Start only
                # names a row that recorded nobody.
                requester=state.requester or _web_requester(request),
            )
            run = await run_reader.get(state.run_id)
            assert run is not None
            return JSONResponse(_run_json(run))

    async def create_run(request: Request) -> JSONResponse:
        """Persist a workflow request and start its supported local execution."""
        body = await _json_body(request)
        try:
            prompt = _required_string(body, "prompt")
            repository = _required_string(body, "repository")
            workflow_id = WorkflowId(_required_string(body, "workflowId"))
            dependency_value = _optional_string(body, "dependsOnRunId")
        except ValueError as error:
            return _error(str(error), 400)
        if not repository_visible(await github_login.visible_repositories(request), repository):
            return _error("you cannot write to this repository", 403)
        # A prerequisite the requester may not see is refused as if it did not
        # exist, so neither its existence nor its state is revealed.
        if dependency_value and await run_hidden(request, RunId(dependency_value)):
            return _error(f"unknown prerequisite workorder: {dependency_value}", 400)
        graph = offered_graphs().get(str(workflow_id))
        if graph is None:
            return _error(f"unknown workflow definition: {workflow_id}", 400)

        # `offered_graphs` only answers with a graph while the engine is
        # running, so this cannot be `None` here.
        assert surface.runtime is not None
        try:
            inputs = resolve_inputs(
                getattr(graph, "inputs", ()), body.get("inputs", {})
            )
        except ValueError as error:
            return _error(str(error), 400)
        try:
            state = await start_graph_run(
                surface.runtime,
                graph,
                inputs=inputs,
                prompt=prompt,
                repository=repository,
                depends_on_run_id=RunId(dependency_value) if dependency_value else None,
                requester=_web_requester(request),
            )
        except ValueError as error:
            return _error(str(error), 400)
        run = await run_reader.get(state.run_id)
        assert run is not None
        return JSONResponse(_run_json(run), status_code=201)

    async def get_run(request: Request) -> JSONResponse:
        run_id = RunId(request.path_params["run_id"])
        run = await run_reader.get(run_id)
        if run is None or await run_hidden(request, run_id):
            return _error("run not found", 404)
        # The WorkOrder's usage is its run's: summed from what each node's
        # agent reported, so it is read here rather than stored on the row.
        return JSONResponse({
            **_run_json(run), "usage": usage_rollup(graph_events.since(run_id)).json(),
        })

    async def delete_run(request: Request) -> Response:
        """Throw a WorkOrder away unless scheduled work still depends on it.

        A run still being worked on is the graph engine's, and its driver is a
        task in the engine rather than anything this app holds. Deleting the
        row without telling the engine would take the WorkOrder off the rail
        and leave the run working -- agents still going in the repository, with
        nothing left on screen to stop them by. So the engine is asked to
        cancel the run, and only then is the row forgotten.
        """
        run_id = RunId(request.path_params["run_id"])
        if await run_hidden(request, run_id):
            return _error("run not found", 404)
        async with dependency_lock:
            if run_id in deleting_runs:
                return _error("workorder deletion is already in progress", 409)
            state = await session.state_store.load(run_id)
            if state is None:
                return _error("run not found", 404)
            dependents = [
                str(run.run_id)
                for run in await session.state_store.list_runs()
                if run.phase is RunPhase.SCHEDULED and run.depends_on_run_id == run_id
            ]
            if dependents:
                return _error(
                    "cannot delete workorder required by scheduled workorders: "
                    + ", ".join(dependents),
                    409,
                )
            # Reserve deletion before cancellation yields to an active agent.
            # Creators check this reservation under the same persistence lock.
            deleting_runs.add(run_id)
        try:
            if state.phase is not RunPhase.SCHEDULED:
                await cancel_graph_run(run_id)
            async with dependency_lock:
                await session.state_store.delete_run(run_id)
                graph_progress.pop(run_id, None)
        finally:
            async with dependency_lock:
                deleting_runs.discard(run_id)
        return Response(status_code=204)

    async def cancel_graph_run(run_id: RunId) -> None:
        """Stop a graph WorkOrder in the engine, if there is one to stop.

        Two ways there is nothing to do, and neither is a reason to refuse the
        delete. The engine may not be running at all -- it failed to open, or
        this process never had one -- in which case nothing here is driving the
        run either, because a driver is a task in a process. And the engine may
        not know the run: a row whose graph state was deleted from under it,
        which `restore_graph_runs` fails on startup for the same reason.

        Either way the row is the reader's to throw away, so the reason is
        logged and the delete goes on. Refusing would leave a WorkOrder nobody
        can remove and nothing is working on.
        """
        runtime = surface.runtime
        if runtime is None:
            log.warning(
                "the graph engine is not running, so graph WorkOrder %s was "
                "deleted without being cancelled",
                run_id,
            )
            return
        try:
            await runtime.cancel(run_id)
        except GraphRuntimeError:
            log.warning(
                "the graph engine has no record of graph WorkOrder %s, so "
                "there was nothing to cancel",
                run_id,
            )

    async def graph_run_events(request: Request) -> JSONResponse:
        """Replay the graph transcript for the WorkOrder UI.

        The graph control surface deliberately exposes a live event stream. The
        WorkOrder page also needs a finite snapshot when it opens after an
        agent has finished, so serve the same recorded events as JSON here.

        Answered for any WorkOrder, including one whose workflow this
        deployment no longer has: what a run said is recorded against the run,
        not against the graph, so a withdrawn or renamed workflow takes away
        the ability to draw the graph and not the transcripts underneath it.
        """
        run_id = RunId(request.path_params["run_id"])
        if await session.state_store.load(run_id) is None or await run_hidden(request, run_id):
            return _error("run not found", 404)
        raw_cursor = request.query_params.get("cursor")
        if raw_cursor is None:
            raw_cursor = request.headers.get("last-event-id")
        try:
            cursor = int(raw_cursor) if raw_cursor and raw_cursor.strip() else 0
        except ValueError:
            return _error("cursor must be an integer", 400)
        if cursor < 0:
            return _error("cursor must not be negative", 400)
        return JSONResponse(
            {
                "events": [
                    {
                        "sequence": event.sequence,
                        "type": event.kind.value,
                        "nodeId": str(event.node_id) if event.node_id else None,
                        "executionId": str(event.execution_id) if event.execution_id else None,
                        "payload": dict(event.payload),
                    }
                    for event in graph_events.since(run_id, cursor)
                ]
            }
        )

    async def complete_human_review(request: Request) -> JSONResponse:
        run_id = RunId(request.path_params["run_id"])
        body = await _json_body(request)
        approved = body.get("approved")
        if not isinstance(approved, bool):
            return _error("approved must be a boolean", 400)
        summary = str(body.get("summary", "")).strip()
        lock = workflow_restart_locks.setdefault(run_id, asyncio.Lock())  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
        async with lock:
            state = await session.state_store.load(run_id)
            if state is None:
                return _error("run not found", 404)
            if (
                state.phase is not RunPhase.AWAITING_HUMAN_REVIEW  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
                or state.current_step_id is None  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
            ):
                return _error("run is not awaiting human review", 409)
            try:
                next_state = await workflow_executor.complete_human_review(  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
                    HumanReviewCompleted(  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
                        run_id=run_id,
                        step_id=state.current_step_id,  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
                        approved=approved,
                        summary=summary,
                    )
                )
                if next_state.phase is RunPhase.RUNNING_AGENT:
                    track_workflow(  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
                        run_id,
                        asyncio.create_task(workflow_executor.resume_agent_step(run_id)),  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
                    )
            except WorkflowExecutionError as error:  # pyright: ignore[reportUndefinedVariable]  # Baseline: see docs/pyright.md
                return _error(str(error), 409)
        run = await run_reader.get(run_id)
        assert run is not None
        return JSONResponse(_run_json(run))

    async def create_thread(request: Request) -> JSONResponse:
        body = await _json_body(request)
        # A new chat is given the default checkout at once, so it is only for
        # those who can write to that repository.
        default = session.workspace_repository
        if default is not None and not repository_visible(
            await github_login.visible_repositories(request), default
        ):
            return _error("you cannot write to this repository", 403)
        try:
            thread = await service.create(
                AgentId(_required_string(body, "agentId")),
                _required_string(body, "runner"),
            )
        except (KeyError, ValueError) as error:
            return _error(str(error), 400)
        return JSONResponse(_thread_json(thread), status_code=201)

    async def get_thread(request: Request) -> JSONResponse:
        thread = await service.get(_thread_id(request))
        if thread is None:
            return _error("thread not found", 404)
        result = _thread_json(thread)
        current = service.latest_run(thread.instance_id)
        result["phase"] = (
            "running" if current is not None and not current.done
            else "failed" if current is not None and current.error is not None
            else "idle"
        )
        result["currentRun"] = (
            {
                "id": str(current.agent_run_id),
                "phase": result["phase"],
            }
            if current is not None else None
        )
        # Agent-turn records are intentionally ephemeral: conversation history
        # is durable, but it is not an audit of every provider turn. Keep the
        # field explicit so terminal clients never infer a history from a live
        # process-local snapshot.
        result["previousRuns"] = []
        approvals = await session.state_store.list_approvals(instance_id=thread.instance_id)
        result["pendingApproval"] = any(record.status.value == "pending" for record in approvals)
        return JSONResponse(result)

    async def update_thread(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        title = None
        if "title" in body:
            title = str(body["title"]).strip()
            if title:
                title = title[:80]
            else:
                title = None
        runner = str(body["runner"]) if "runner" in body else None
        try:
            thread = await service.update_metadata(
                instance_id, title=title, runner=runner
            )
        except ValueError as error:
            return _error(str(error), 400)
        return JSONResponse(_thread_json(thread))

    async def archive_thread(request: Request) -> JSONResponse:
        thread = await service.get(_thread_id(request))
        if thread is None:
            return _error("thread not found", 404)
        thread = await service.update_metadata(
            thread.instance_id,
            archived=request.url.path.rsplit("/", 1)[-1] == "archive",
        )
        return JSONResponse(_thread_json(thread))

    async def delete_thread(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        await service.delete(instance_id)
        return Response(status_code=204)

    async def messages(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        history = await service.history(instance_id)
        active = service.active_run(instance_id)
        # What a conversation was asked to allow is part of the transcript, and
        # is loaded with it. The run stream replays these too, but it is only
        # opened while this process is still executing the turn -- so a chat
        # whose run has since finished used to come back from a page load with
        # its approvals missing entirely.
        approvals = await session.state_store.list_approvals(instance_id=instance_id)
        return JSONResponse(
            {
                "messages": _messages_json(history),
                "approvals": [_approval_json(record) for record in approvals],
                # A complete assistant transcript can become durable just
                # before ActiveRun flips to done. In that window replaying it
                # would duplicate the assistant message in the client.
                "unstable_resume": (
                    active is not None
                    and bool(history)
                    and history[-1].role is Role.USER
                ),
            }
        )

    async def approval_events(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        return StreamingResponse(
            approval_feed.stream(instance_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def title_thread(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        opening_text = str(body["text"]).strip() if body.get("text") else None
        runner = str(body["runner"]) if body.get("runner") else None
        try:
            title = await service.generate_title(instance_id, opening_text, runner)
        except ValueError as error:
            return _error(str(error), 400)
        except Exception as failure:  # noqa: BLE001 -- #779: title endpoint returns the provider error to the client
            # A provider that cannot name the chat has not cost anybody
            # anything yet, and must not be allowed to. The client asks for a
            # name *before* sending the message being named, so a failure
            # answered with a 500 here would take the user's turn with it --
            # a CLI that is out of quota would stop the chat working rather
            # than leave it called "New chat".
            #
            # The reason travels in the body instead of the status, because
            # something did go wrong and the placeholder name is not evidence
            # of which provider failed or why.
            return JSONResponse({"title": thread.title, "error": str(failure)})
        return JSONResponse({"title": title})

    async def attach_workspace(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        repository = body.get("repository")
        if repository is not None and (not isinstance(repository, str) or not repository.strip()):
            return _error("repository must be a non-empty string", 400)
        # A checkout, named or the default, is only for those who can write to
        # its repository: the chat's agent may read and change it.
        target = repository or session.workspace_repository
        if target is not None and not repository_visible(
            await github_login.visible_repositories(request), target
        ):
            return _error("you cannot write to this repository", 403)
        try:
            thread = await service.attach_workspace(instance_id, repository)
        except RuntimeError as error:
            # A repository that cannot produce a checkout -- unwired, or git
            # refusing -- is the server's problem to explain, not a 404.
            return _error(str(error), 409)
        return JSONResponse(_thread_json(thread))

    async def detach_workspace(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        try:
            thread = await service.detach_workspace(instance_id)
        except RuntimeError as error:
            return _error(str(error), 409)
        return JSONResponse(_thread_json(thread))

    async def run_thread(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        try:
            text = _required_string(body, "text")
        except ValueError as error:
            return _error(str(error), 400)
        runner = str(body["runner"]) if body.get("runner") else None

        try:
            run = await service.start_run(instance_id, text, runner)
            return StreamingResponse(run.stream(), media_type="application/x-ndjson")
        except RuntimeError as error:
            return _error(str(error), 409)

    async def resume_run(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        # Keep a completed snapshot available for the small race where history
        # observed an active run immediately before it finished.
        run = service.latest_run(instance_id)
        if run is not None:
            return StreamingResponse(run.stream(), media_type="application/x-ndjson")
        return Response(status_code=204)

    async def cancel_run(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        try:
            await service.stop_run(instance_id)
        except RuntimeError as error:
            return _error(str(error), 409)
        return Response(status_code=204)

    async def decide_approval(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        try:
            approval_id = ApprovalId(request.path_params["approval_id"])
            if "answers" in body:
                raw_answers = body["answers"]
                if not isinstance(raw_answers, dict):
                    raise ValueError("answers must be an object")
                answers = tuple(
                    UserInputAnswer(
                        question_id=str(question_id),
                        answers=tuple(values) if isinstance(values, list) else (),
                    )
                    for question_id, values in raw_answers.items()
                    if isinstance(question_id, str)
                    and isinstance(values, list)
                    and all(isinstance(value, str) for value in values)
                )
                if len(answers) != len(raw_answers):
                    raise ValueError("each answer must be an array of strings")
                approval = await service.answer_question(
                    instance_id, approval_id, answers
                )
            else:
                decision = _required_string(body, "decision")
                approval = await service.decide_approval(
                    instance_id, approval_id, decision
                )
        except ValueError as error:
            return _error(str(error), 400)
        except UnknownApprovalError as error:
            return _error(str(error), 404)
        except ApprovalDecisionNotAllowedError as error:
            return _error(str(error), 400)
        except UserInputNotAllowedError as error:
            return _error(str(error), 400)
        except ApprovalNotPendingError as error:
            # The request outlived whatever was waiting for it. Not the
            # client's mistake to fix by retrying, so not a 400.
            return _error(str(error), 409)
        return JSONResponse({"approval": _approval_json(approval)})

    # --- GitHub connection endpoints -----------------------------------------

    _credential_store = credential_store or GitHubCredentialStore()
    _source_control_preferences = (
        source_control_preferences or SourceControlPreferences()
    )

    # Scope connections and pending flows to the verified browser identity.
    # No authenticated user inherits the legacy local account's credentials.
    _github_flows: dict[tuple[str, str], tuple[DeviceFlowState, int, str]] = {}

    def _github_store(request: Request) -> GitHubCredentialStore:
        if not github_login.configured:
            return _credential_store
        user = github_login._read_session(request)
        if user is None:
            # Session middleware normally rejects this before routing.
            raise RuntimeError("GitHub connection requires a browser session")
        return GitHubCredentialStore(user_id=int(user["id"]))  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md

    def _web_requester(request: Request) -> str | None:
        """The signed-in GitHub account, or ``None`` without one to name."""
        user = github_login._read_session(request) if github_login.configured else None
        return github_requester(int(user["id"]), str(user["login"])) if user else None  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md

    async def _co_author(requester: str | None) -> str:
        """Who the WorkOrder's commits credit as co-author, or empty for nobody.

        A Slack requester is credited only when their profile email can be
        read; GitHub shows the commit as theirs only if they verified it.
        """
        provider, _, rest = (requester or "").partition(":")
        if provider != "slack":
            return github_co_author(requester)
        identity = await _slack_comms.user_identity(rest.partition(":")[2])
        if identity is None:
            return ""
        name, email = (re.sub(r"[<>\s]+", " ", part).strip() for part in identity)
        return f"{name} <{email}>" if name and email and " " not in email else ""

    def _is_local_request(request: Request) -> bool:
        """True when the request originates from the UI served by this process.

        The GitHub auth endpoints are mutating and must not be triggerable by
        arbitrary pages. Checking the Origin header against localhost is a
        lightweight CSRF guard appropriate for a local tool; it stops a
        cross-origin page from silently disconnecting the user's token or
        initiating a new device flow. GET /api/github/status is read-only and
        exempt.
        """
        origin = request.headers.get("origin", "")
        if not origin:
            # No Origin means a same-origin request (form submit, etc.) or a
            # curl call from localhost. Both are fine for a local tool.
            return True
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        # Browser Origin values have no path and lowercase hostnames.  Compare
        # parsed hosts rather than prefixes: localhost.evil.example is not
        # localhost.
        return parsed.hostname.lower() in {
            "localhost",
            "127.0.0.1",
            "::1",
            (request.url.hostname or "").lower(),
        }

    def _hint(value: str) -> str:
        """Return first 4 chars + bullets so the UI can confirm which ID is set."""
        return value[:4] + "••••••••" if len(value) > 4 else "••••••••"

    def _effective_client_id(request: Request) -> str:
        """Env-var takes precedence; keychain is the fallback for UI-configured IDs."""
        return github_client_id or _github_store(request).get_client_id() or ""

    async def github_status(_request: Request) -> JSONResponse:
        credentials = _github_store(_request).get_credentials()
        now = time.time()
        connected = bool(credentials and credentials.is_usable(now))
        return JSONResponse(
            {
                "connected": connected,
                "clientIdConfigured": bool(_effective_client_id(_request)),
                # Agents act as the host's `engine connect github` connection.
                # With sign-in on, the one made here is the user's own and is
                # not it.
                "agentsUseConnection": not github_login.configured,
            }
        )

    async def source_control_status(_request: Request) -> JSONResponse:
        provider, auto_selected = source_control_settings.selected_or_detected_provider(
            _source_control_preferences
        )
        cli = source_control_settings.gh_cli_status()
        return JSONResponse(
            {
                "provider": provider,
                "autoSelected": auto_selected,
                "ghCli": {
                    "installed": cli.installed,
                    "authenticated": cli.authenticated,
                    "account": cli.account,
                    "message": cli.message,
                },
            }
        )

    async def source_control_provider_status(_request: Request) -> JSONResponse:
        """Return the chosen provider without probing an unrelated CLI.

        A saved OAuth choice is a local settings-file read.  Do not make the
        Settings panel wait for ``gh auth status`` merely to render that choice.
        First-run auto-selection still performs its one required CLI probe.
        """
        provider, auto_selected = source_control_settings.selected_or_detected_provider(
            _source_control_preferences
        )
        return JSONResponse(
            {"provider": provider, "autoSelected": auto_selected}
        )

    async def set_source_control_provider(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        provider = body.get("provider")
        if provider not in {"gh-cli", "github-oauth", "gitlab-oauth"}:
            return _error("provider must be 'gh-cli', 'github-oauth', or 'gitlab-oauth'", 400)
        origin = body.get("origin") if isinstance(body.get("origin"), str) else None
        if provider == "gitlab-oauth":
            try:
                origin = normalize_gitlab_origin(origin or "https://gitlab.com")
            except ValueError as error:
                return _error(str(error), 400)
        _source_control_preferences.set(provider, origin if provider == "gitlab-oauth" else None)
        return Response(status_code=204)

    _loop_settings = loop_settings or LoopSettingsStore()

    async def get_loop_settings(_request: Request) -> JSONResponse:
        return JSONResponse(_loop_settings.get().json())

    async def set_loop_settings(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        try:
            settings = parse_loop_settings(await request.json(), runners)
        except ValueError as error:
            return _error(str(error), 400)
        _loop_settings.set(settings)
        return JSONResponse(settings.json())

    async def loop_create_workorder(loop: Loop, prompt: str) -> str:
        graph = _mentioned_workflow()
        if graph is None:
            raise RuntimeError("no workflow is configured under `work_orders.workflow`")
        assert surface.runtime is not None
        state = await start_graph_run(
            surface.runtime, graph,
            inputs=resolve_inputs(getattr(graph, "inputs", ()), {}),
            prompt=prompt, repository=loop.repository, requester=loop.requester,
        )
        return str(state.run_id)

    async def loop_list_runs(repository: str) -> list[RunState]:
        return [
            state for state in await session.state_store.list_runs()
            if same_repository(state.repository, repository)
        ]

    async def loop_direct(run_id: str, prompt: str, *, resume: bool) -> None:
        runtime = surface.runtime
        if runtime is None:
            raise RuntimeError("graph WorkOrders are not running in this process")
        snapshot = await runtime.snapshot(RunId(run_id))
        if snapshot is None:
            raise RuntimeError("the WorkOrder is unavailable")
        stopped = snapshot.status in (RunStatus.COMPLETED, RunStatus.FAILED)
        if stopped != resume:
            raise RuntimeError(
                "this WorkOrder is still running; use steer_workorder" if resume
                else "this WorkOrder has stopped; use resume_workorder"
            )
        node = _reentry_node(runtime, snapshot)
        if resume and node is None:
            raise RuntimeError("the workflow has no unique implementation to resume")
        await runtime.steer(RunId(run_id), f"Loop instruction:\n{prompt}", node_id=node)

    async def loop_steer(run_id: str, prompt: str) -> None:
        await loop_direct(run_id, prompt, resume=False)

    async def loop_resume(run_id: str, prompt: str) -> None:
        await loop_direct(run_id, prompt, resume=True)

    async def loop_load(run_id: str) -> RunState | None:
        return await session.state_store.load(RunId(run_id))

    def loop_spend(run_id: str) -> float:
        return usage_rollup(graph_events.since(RunId(run_id))).cost_usd or 0.0  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md

    async def loop_may_act(loop: Loop) -> bool:
        """Whether the loop's creator can still write to its repository, asked
        again before each run and WorkOrder as their own request would be."""
        if loop.requester is None or github_login.config is None or github_login.authorize is None:
            # Created by someone who sees everything: login was off, or the
            # service credential made it.
            return True
        provider, _, rest = loop.requester.partition(":")
        account, _, login = rest.partition(":")
        if provider != "github" or not account.isdigit() or not login:
            return False
        user: dict[str, object] = {"id": int(account), "login": login}
        if user["id"] in github_login.operators:
            return True
        writable = await github_login.writable_repositories(user)
        return writable is not None and repository_visible(writable, loop.repository)

    _loops = loop_store or LoopStore()
    loop_runner = LoopRunner(
        _loops,
        LoopHost(
            create=loop_create_workorder, load=loop_load, list_runs=loop_list_runs,
            steer=loop_steer, resume=loop_resume, spend=loop_spend,
            # Defined further down, so looked up when called.
            same_repository=lambda one, other: same_repository(one, other),
            may_act=loop_may_act,
        ),
        loop_provider or CodexACPProvider(permissions=loop_tool_permission),
    )
    loop_runs: set[asyncio.Task[None]] = set()

    async def dispatch_loops() -> None:
        while True:
            try:
                for task in await loop_runner.tick():
                    loop_runs.add(task)
                    task.add_done_callback(loop_runs.discard)
            except Exception:
                log.exception("could not start due loops")
            await asyncio.sleep(loop_tick_seconds)

    async def loop_json(loop: Loop) -> dict[str, object]:
        # The newest first, read together rather than one after another.
        states = await asyncio.gather(
            *(loop_load(one.run_id) for one in reversed(loop.workorders)))
        return loop_runner.json(loop, [state for state in states if state is not None])

    async def list_loops(request: Request) -> JSONResponse:
        visible = await github_login.visible_repositories(request)
        return JSONResponse({"loops": [
            await loop_json(loop) for loop in _loops.list()
            if repository_visible(visible, loop.repository)
        ]})

    async def new_loop_defaults(_request: Request) -> JSONResponse:
        return JSONResponse(loop_defaults(_loop_settings.get()))

    async def create_loop(request: Request) -> JSONResponse:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        repositories = [
            str(Path(path).expanduser().resolve()) for path in repository_registry.snapshot.repos.values()
        ] or ["."]
        try:
            loop = parse_loop(
                await request.json(), repositories, datetime.now().astimezone(),
            )
        except ValueError as error:
            return _error(str(error), 400)
        if not repository_visible(await github_login.visible_repositories(request), loop.repository):
            return _error("you cannot write to this repository", 403)
        loop = replace(loop, requester=_web_requester(request))
        _loops.save(loop)
        return JSONResponse(await loop_json(loop), status_code=201)

    async def visible_loop(request: Request) -> Loop | None:
        loop = _loops.get(request.path_params["loop_id"])
        visible = await github_login.visible_repositories(request)
        return loop if loop is not None and repository_visible(visible, loop.repository) else None

    async def get_loop(request: Request) -> JSONResponse:
        loop = await visible_loop(request)
        return JSONResponse(await loop_json(loop)) if loop else _error("loop not found", 404)

    async def delete_loop(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        loop = await visible_loop(request)
        if loop is None:
            return _error("loop not found", 404)
        _loops.delete(loop.loop_id)
        return Response(status_code=204)

    async def github_get_client_id(_request: Request) -> JSONResponse:
        # Never return the actual value — only whether one is set and its hint.
        stored = _github_store(_request).get_client_id()
        if github_client_id:
            return JSONResponse(
                {"source": github_client_id_source, "hint": _hint(github_client_id)}
            )
        if stored:
            return JSONResponse({"source": "keychain", "hint": _hint(stored)})
        return JSONResponse({"source": "none", "hint": ""})

    async def github_set_client_id(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        client_id = (body.get("clientId") or "").strip()
        if not client_id:
            return _error("clientId is required", 400)
        try:
            _github_store(request).set_client_id(client_id)
        except GitHubAuthError as error:
            return _error(str(error), 500)
        return Response(status_code=204)

    async def github_connect(request: Request) -> JSONResponse:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        effective_client_id = _effective_client_id(request)
        if not effective_client_id:
            return _error(
                "GitHub client ID is not configured. Enter it in Settings.", 503
            )
        identity = _github_store(request).credential_identity
        active = _github_flows.get(identity)
        if active is None:
            try:
                flow = await start_device_flow(effective_client_id)
            except GitHubAuthError as error:
                return _error(str(error), 502)
            # A concurrent tab may have started a flow while we awaited GitHub.
            active = _github_flows.setdefault(
                identity, (flow, flow.interval, effective_client_id)
            )
        flow, interval, _ = active
        return JSONResponse(
            {
                "userCode": flow.user_code,
                "verificationUri": flow.verification_uri,
                "expiresIn": flow.expires_in,
                "interval": interval,
            }
        )

    async def github_connect_poll(request: Request) -> JSONResponse:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        store = _github_store(request)
        identity = store.credential_identity
        active = _github_flows.get(identity)
        if active is None:
            return _error(
                "no active device flow; call POST /api/github/connect first", 409
            )
        flow, interval, client_id = active
        try:
            result = await poll_device_flow(client_id, flow.device_code, interval)
        except GitHubAuthError as error:
            if _github_flows.get(identity) is active:
                _github_flows.pop(identity)
            return _error(str(error), 502)
        if _github_flows.get(identity) is not active:
            return _error("device flow was disconnected or replaced", 409)
        if isinstance(result, DeviceFlowComplete):
            _github_flows.pop(identity)
            try:
                store.set_credentials(credentials_from_device_flow(result))
            except GitHubAuthError as error:
                return _error(str(error), 500)
            return JSONResponse({"status": "complete"})
        _github_flows[identity] = (flow, result.next_interval, client_id)
        return JSONResponse({"status": "pending", "nextInterval": result.next_interval})

    async def github_disconnect(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        store = _github_store(request)
        _github_flows.pop(store.credential_identity, None)
        store.delete()
        return Response(status_code=204)

    # GitLab credentials are per OAuth issuer, unlike GitHub's single public
    # issuer.  Keep one in-flight device flow per canonical instance so tabs
    # cannot race an authorization code for the same account.
    _gitlab_flows: dict[str, tuple[object, int]] = {}

    def _gitlab_origin(request: Request | None = None, body: Mapping[str, object] | None = None) -> str:
        value = (
            body.get("origin") if body is not None else request.query_params.get("origin") if request is not None else None
        )
        try:
            return normalize_gitlab_origin(value if isinstance(value, str) else "https://gitlab.com")
        except ValueError as error:
            raise GitLabAuthError(str(error)) from error

    def _gitlab_connected(store: GitLabCredentialStore) -> bool:
        credentials = store.get_credentials()
        now = time.time()
        return bool(credentials and credentials.is_usable(now))

    async def gitlab_status(request: Request) -> JSONResponse:
        try:
            origin = _gitlab_origin(request)
        except GitLabAuthError as error:
            return _error(str(error), 400)
        store = GitLabCredentialStore(origin)
        return JSONResponse({"origin": origin, "connected": _gitlab_connected(store), "clientIdConfigured": bool(store.get_client_id())})

    async def gitlab_set_client_id(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        try:
            origin = _gitlab_origin(body=body)
        except GitLabAuthError as error:
            return _error(str(error), 400)
        client_id = body.get("clientId")
        if not isinstance(client_id, str) or not client_id.strip():
            return _error("clientId is required", 400)
        try:
            GitLabCredentialStore(origin).set_client_id(client_id.strip())
        except GitLabAuthError as error:
            return _error(str(error), 500)
        return Response(status_code=204)

    async def gitlab_connect(request: Request) -> JSONResponse:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        try:
            origin = _gitlab_origin(body=body)
        except GitLabAuthError as error:
            return _error(str(error), 400)
        client_id = GitLabCredentialStore(origin).get_client_id()
        if not client_id:
            return _error("GitLab client ID is not configured for this instance.", 503)
        active = _gitlab_flows.get(origin)
        if active is None:
            try:
                flow = await start_gitlab_device_flow(origin, client_id)
            except GitLabAuthError as error:
                return _error(str(error), 502)
            active = (flow, flow.interval)
            _gitlab_flows[origin] = active
        flow, interval = active
        return JSONResponse({"origin": origin, "userCode": flow.user_code, "verificationUri": flow.verification_uri, "expiresIn": flow.expires_in, "interval": interval})  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md

    async def gitlab_connect_poll(request: Request) -> JSONResponse:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        try:
            origin = _gitlab_origin(body=body)
        except GitLabAuthError as error:
            return _error(str(error), 400)
        active = _gitlab_flows.get(origin)
        if active is None:
            return _error("no active GitLab device flow; call POST /api/gitlab/connect first", 409)
        flow, interval = active
        client_id = GitLabCredentialStore(origin).get_client_id()
        if not client_id:
            _gitlab_flows.pop(origin, None)
            return _error("GitLab client ID is not configured for this instance.", 503)
        try:
            result = await poll_gitlab_device_flow(origin, client_id, flow.device_code, interval)  # pyright: ignore[reportAttributeAccessIssue]  # Baseline: see docs/pyright.md
        except GitLabAuthError as error:
            _gitlab_flows.pop(origin, None)
            return _error(str(error), 502)
        if isinstance(result, GitLabDeviceFlowComplete):
            try:
                GitLabCredentialStore(origin).set_credentials(gitlab_credentials_from_device_flow(result))
            except GitLabAuthError as error:
                _gitlab_flows.pop(origin, None)
                return _error(str(error), 500)
            _gitlab_flows.pop(origin, None)
            return JSONResponse({"status": "complete"})
        _gitlab_flows[origin] = (flow, result.next_interval)
        return JSONResponse({"status": "pending", "nextInterval": result.next_interval})

    async def gitlab_disconnect(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        try:
            origin = _gitlab_origin(body=body)
        except GitLabAuthError as error:
            return _error(str(error), 400)
        _gitlab_flows.pop(origin, None)
        GitLabCredentialStore(origin).delete()
        return Response(status_code=204)

    async def graph_surface(scope: Scope, receive: Receive, send: Send) -> None:
        """Pass anything under `/graph` to the graph engine's own server.

        The graph engine ships a small API of its own -- what a run is doing,
        what it has raised, and the two things a person can send back: a
        message for whichever agent is working, and an answer to a question it
        stopped on. That is how a graph run gets approved today, and this
        app's pages cannot do it yet.

        A hop rather than a re-implementation, and behind a prefix of its own
        because both servers call their runs `/api/runs`. It has to be a
        forwarder rather than a plain mount because the engine on the far side
        does not exist until the server starts.
        """
        if surface.app is None:
            await JSONResponse(
                {"error": "this process is not running graph workflows"},
                status_code=503,
            )(scope, receive, send)
            return
        if scope["type"] == "http":
            # The same runs as `/api/runs`, so scoped the same way: someone
            # else's run is not found, and starting one without a row to scope
            # it by is left to those who see everything.
            request = Request(scope)
            path = scope["path"].removeprefix(scope.get("root_path", ""))
            parts = path.strip("/").split("/")
            refusal: JSONResponse | None = None
            if parts[:2] == ["api", "runs"] and len(parts) > 2:
                run_id = RunId(parts[2])
                if await run_hidden(request, run_id):
                    refusal = _error("run not found", 404)
                else:
                    # A run's event stream stays open; it ends once the run's
                    # repository is no longer one this user can see.
                    async def still_visible() -> bool:
                        return not await run_hidden(request, run_id)

                    scope[STREAM_ACCESS] = still_visible
            elif parts == ["api", "runs"] and request.method == "POST":
                if await github_login.visible_repositories(request) is not None:
                    refusal = _error("only operators may start graph runs directly", 403)
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        await surface.app(scope, receive, send)

    # --- Slack connection endpoints ------------------------------------------

    _slack_store = slack_credential_store or SlackCredentialStore()
    _slack_state: str | None = None
    _slack_redirect_uri: str | None = None
    # Concierge replies and graph progress share the same Slack transport;
    # runs started from a GitHub issue report back there as comments instead.
    run_notifier = RunNotifier(
        ChannelRoutedCommunications(
            session.capabilities.communications,
            GithubCommunications(session.capabilities.source_control),
        ),
        public_url,
    )

    def _signing_secret() -> str:
        return _slack_store.signing_secret() or ""

    async def slack_status(_request: Request) -> JSONResponse:
        credentials = _slack_store.credentials()
        signing_secret = bool(_signing_secret())
        connected = bool(_slack_store.token())
        return JSONResponse(
            {
                "configured": credentials is not None,
                "connected": connected,
                # Whether a mention could actually start something, and which
                # of its parts is missing -- so the settings panel can offer
                # the one this deployment still needs rather than a paragraph
                # listing everything it might. Being connected counts: a work
                # order this server cannot reply to is one nobody would see.
                "events": (
                    connected and signing_secret and bool(work_orders.repository)
                ),
                "signingSecret": signing_secret,
            }
        )

    async def slack_set_credentials(request: Request) -> Response:
        nonlocal _slack_state, _slack_redirect_uri
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        client_id = (body.get("clientId") or "").strip()
        client_secret = (body.get("clientSecret") or "").strip()
        signing_secret = (body.get("signingSecret") or "").strip()
        if signing_secret and not client_id and not client_secret:
            # Adding only the signing secret, to a deployment that connected
            # before it was asked for. It belongs to the app already
            # configured, so this must not walk the path below: revoking the
            # token and re-saving the same OAuth pair would cost a working
            # connection to enable mentions on it.
            if _slack_store.credentials() is None:
                return _error("Slack OAuth credentials are not configured", 409)
            try:
                _slack_store.set_signing_secret(signing_secret)
            except SlackAuthError as error:
                return _error(str(error), 500)
            return Response(status_code=204)
        if not client_id or not client_secret:
            return _error("clientId and clientSecret are required", 400)
        token = _slack_store.token()
        if token:
            try:
                await revoke_slack_token(token)
            except SlackAuthError as error:
                return _error(str(error), 502)
            _slack_store.disconnect()
        try:
            _slack_store.set_credentials(client_id, client_secret)
            if signing_secret:
                # After the credentials, never before: saving them forgets the
                # previous app's signing secret, which would take this one too.
                _slack_store.set_signing_secret(signing_secret)
        except SlackAuthError as error:
            return _error(str(error), 500)
        _slack_state = None
        _slack_redirect_uri = None
        return Response(status_code=204)

    async def slack_connect(request: Request) -> JSONResponse:
        nonlocal _slack_state, _slack_redirect_uri
        if not _is_local_request(request):
            return _error("forbidden", 403)
        credentials = _slack_store.credentials()
        if credentials is None:
            return _error("Slack OAuth credentials are not configured", 503)
        _slack_state = uuid4().hex
        _slack_redirect_uri = str(request.url_for("slack_callback"))
        return JSONResponse(
            {"authorizationUrl": slack_authorization_url(credentials.client_id, _slack_redirect_uri, _slack_state)}
        )

    async def slack_callback(request: Request) -> Response:
        nonlocal _slack_state, _slack_redirect_uri
        if not _slack_state or request.query_params.get("state") != _slack_state:
            return _error("invalid OAuth state", 400)
        code = request.query_params.get("code")
        credentials = _slack_store.credentials()
        if not code or credentials is None or _slack_redirect_uri is None:
            return _error(request.query_params.get("error", "authorization was not completed"), 400)
        try:
            token = await exchange_slack_code(credentials, code, _slack_redirect_uri)
            _slack_store.set_token(token)
        except SlackAuthError as error:
            return _error(str(error), 502)
        finally:
            _slack_state = None
            _slack_redirect_uri = None
        return Response(
            "<html><body><p>Slack connected. You can close this window.</p>"
            "<script>window.close()</script></body></html>",
            media_type="text/html",
        )

    async def slack_disconnect(request: Request) -> Response:
        nonlocal _slack_state, _slack_redirect_uri
        if not _is_local_request(request):
            return _error("forbidden", 403)
        token = _slack_store.token()
        if token:
            try:
                await revoke_slack_token(token)
            except SlackAuthError as error:
                return _error(str(error), 502)
        _slack_store.disconnect()
        _slack_state = None
        _slack_redirect_uri = None
        return Response(status_code=204)

    async def concierge_reply(origin: RunOrigin, text: str) -> None:
        await run_notifier.post(origin, CommunicationsMessage(text, mention=origin.author))

    async def concierge_turn_finished(origin: RunOrigin) -> None:
        # Release after the concierge reply, or when the turn fails so an
        # accepted work order can still report progress.
        # Serialize the flush with live events so new progress cannot overtake it.
        async with graph_notification_lock:
            for run_id, run_origin in list(deferred_graph_notifications.items()):
                if (run_origin.channel, run_origin.thread_id) != (origin.channel, origin.thread_id):
                    continue
                del deferred_graph_notifications[run_id]
                state = await session.state_store.load(run_id)
                for event in pending_graph_notifications.pop(run_id, []):
                    if state is not None:
                        await notify_graph_event(state, event)

    async def concierge_create_workorder(
        origin: RunOrigin, repository: str, prompt: str,
    ) -> tuple[str, str]:
        graph = _mentioned_workflow()
        if graph is None:
            raise RuntimeError("no workflow is configured under `work_orders.workflow`")
        assert surface.runtime is not None
        state = await start_graph_run(
            surface.runtime, graph,
            inputs=resolve_inputs(getattr(graph, "inputs", ()), {}),
            prompt=prompt, repository=repository,
            origin=origin, defer_notifications=True,
            requester=origin.requester or None,
        )
        link = run_notifier.work_order_link(state)
        return link.url if link else "", str(state.run_id)

    slack_selections: dict[tuple[str, str, str], str] = {}

    async def concierge_select_workorder(origin: RunOrigin, run_id: str) -> None:
        slack_selections[(origin.channel, origin.thread_id, origin.author)] = run_id

    async def concierge_find_workorders(origin: RunOrigin) -> list[RunState]:
        linked = list(await session.state_store.list_runs_for_origin(
            origin.channel, origin.thread_id
        ))

        selected = slack_selections.get((origin.channel, origin.thread_id, origin.author))
        matches = [state for state in linked if str(state.run_id) == selected]
        return matches or linked

    async def concierge_controlled_workorder(origin: RunOrigin) -> RunState:
        linked = await concierge_find_workorders(origin)
        if len(linked) != 1:
            raise RuntimeError(
                "this thread has no work order" if not linked else
                "this thread has multiple work orders; ask which WorkOrder ID the message applies to in Slack"
            )
        state = linked[0]
        assert state.origin is not None
        if (
            origin.author != state.origin.author
            and origin.author not in work_orders.slack_operators
        ):
            raise RuntimeError(
                "only the person who started this WorkOrder or a configured Slack operator can control it"
            )
        return state

    async def concierge_snapshot(origin: RunOrigin) -> tuple[RunState, GraphRuntime, RunSnapshot]:
        state = await concierge_controlled_workorder(origin)
        runtime = surface.runtime
        if runtime is None:
            raise RuntimeError("graph WorkOrders are not running in this process")
        snapshot = await runtime.snapshot(state.run_id)
        if snapshot is None:
            raise RuntimeError("the WorkOrder is unavailable")
        return state, runtime, snapshot

    def concierge_result(state: RunState) -> tuple[str, str]:
        link = run_notifier.work_order_link(state)
        return link.url if link else "", str(state.run_id)

    async def concierge_steer_workorder(origin: RunOrigin, prompt: str) -> tuple[str, str]:
        state, runtime, snapshot = await concierge_snapshot(origin)
        if snapshot.status in (RunStatus.COMPLETED, RunStatus.FAILED):
            raise RuntimeError("this WorkOrder has stopped; use resume_workorder for follow-up work")
        if any(one.kind is ApprovalKind.USER_INPUT for one in snapshot.pending_approvals):
            raise RuntimeError("answer the pending question or review in Slack first")
        await runtime.steer(
            state.run_id, f"Slack instruction from <@{origin.author}>:\n{prompt}",
            node_id=_reentry_node(runtime, snapshot),
        )
        return concierge_result(state)

    async def concierge_resume_workorder(origin: RunOrigin, prompt: str) -> tuple[str, str]:
        state, runtime, snapshot = await concierge_snapshot(origin)
        if snapshot.pending_approvals:
            raise RuntimeError("answer the pending question or review in Slack first")
        if snapshot.status not in (RunStatus.COMPLETED, RunStatus.FAILED):
            raise RuntimeError("this WorkOrder is still running; use steer_workorder")
        node = _reentry_node(runtime, snapshot)
        if node is None:
            raise RuntimeError("the workflow has no unique implementation to resume; please clarify the target")
        await runtime.steer(
            state.run_id, f"Slack follow-up from <@{origin.author}>:\n{prompt}", node_id=node,
        )
        return concierge_result(state)

    async def concierge_find_questions(origin: RunOrigin) -> list[dict]:
        linked = await concierge_find_workorders(origin)
        if len(linked) != 1 or surface.runtime is None:
            return []
        snapshot = await surface.runtime.snapshot(linked[0].run_id)
        if snapshot is None:
            return []
        return [
            {"approval_id": str(record.approval_id), "tool_name": record.tool_name,
             "questions": [{"id": "reply", "question": record.reason}],
             "command": record.command}
            for record in snapshot.pending_approvals
            if record.kind is ApprovalKind.USER_INPUT
        ]

    async def concierge_answer_question(
        origin: RunOrigin, approval_id: str, answers: dict[str, list[str]],
    ) -> tuple[str, str]:
        state, runtime, snapshot = await concierge_snapshot(origin)
        pending = next((one for one in snapshot.pending_approvals
                        if str(one.approval_id) == approval_id
                        and one.kind is ApprovalKind.USER_INPUT
                        and one.tool_name != HUMAN_REVIEW_TOOL), None)
        if pending is None or set(answers) != {"reply"}:
            raise RuntimeError("this question is not pending for the WorkOrder")
        await runtime.steer(
            state.run_id,
            f"Slack answer from <@{origin.author}>:\n" + "\n".join(answers["reply"]),
            execution_id=pending.execution_id,
        )
        await runtime.decide(state.run_id, pending.approval_id, ApprovalDecision.ACCEPT)
        return concierge_result(state)

    async def concierge_decide_review(
        origin: RunOrigin, approved: bool, summary: str,
    ) -> tuple[str, str]:
        state, runtime, snapshot = await concierge_snapshot(origin)
        pending = [one for one in snapshot.pending_approvals
                   if one.kind is ApprovalKind.USER_INPUT and one.tool_name == HUMAN_REVIEW_TOOL]
        if len(pending) != 1:
            raise RuntimeError("this WorkOrder is not awaiting a unique review decision")
        if approved:
            if summary:
                await runtime.steer(state.run_id, summary, execution_id=pending[0].execution_id)
            await runtime.decide(state.run_id, pending[0].approval_id, ApprovalDecision.ACCEPT)
        else:
            node = _reentry_node(runtime, snapshot)
            if node is None:
                raise RuntimeError("the workflow has no unique implementation to resume; please clarify the target")
            # Targeted steering records the feedback and forks implementation,
            # settling the old review without cancelling the WorkOrder.
            await runtime.steer(
                state.run_id,
                f"Slack requested changes from <@{origin.author}>:\n{summary}", node_id=node,
            )
        return concierge_result(state)

    slack_concierge = SlackConcierge(
        provider=concierge_provider or CodexACPProvider(permissions=tool_permission),
        create_workorder=concierge_create_workorder,
        reply=concierge_reply, default_repository=work_orders.repository,
        turn_finished=concierge_turn_finished,
        find_workorders=concierge_find_workorders,
        steer_workorder=concierge_steer_workorder,
        resume_workorder=concierge_resume_workorder,
        select_workorder=concierge_select_workorder,
        find_questions=concierge_find_questions,
        answer_question=concierge_answer_question,
        decide_review=concierge_decide_review,
    )
    _slack_comms = SlackCommunications(_slack_store)
    slack_ingress = SlackIngress(
        slack_concierge, signing_secret=_signing_secret,
        verify_signature=verify_slack_signature, connected=lambda: bool(_slack_store.token()),
        react=_slack_comms.add_reaction,
    )
    # What each delivered comment has led to, for the panel that shows it. The
    # steps are recorded where they happen -- the ingress queues and picks up,
    # the callbacks below forward and answer -- so the panel says what this
    # process did rather than what it was about to try.
    github_activity = GithubActivityLog()

    async def github_reply(origin: RunOrigin, text: str) -> None:
        number, _, review_id = origin.thread_id.partition("/review/")
        await session.capabilities.source_control.add_comment(
            pull_request_url(origin.channel.removeprefix("github:"), int(number)),
            text,
            in_reply_to_id=int(review_id) if review_id else None,
        )
        # After the comment lands, so a row reading "replied" means a reader
        # will find the reply on the pull request.
        github_activity.replied(text)

    async def github_run_for_pull_request(repository: str, number: int) -> RunId | None:
        """Which work order opened this pull request, if anything recorded one.

        Read through the binding that owns the provenance table rather than
        through the control surface, which is deliberately forge-agnostic and
        has no business growing a method shaped like a pull request.
        """
        store = getattr(surface.runtime, "store", None)
        if store is None:
            return None
        return await store.run_for_pull_request(repository.lower(), number)

    async def github_active_run(repository: str, number: int) -> tuple[RunId | None, bool]:
        """The work order recorded for this pull request, and whether it can still be steered."""
        run_id = await github_run_for_pull_request(repository, number)
        runtime = surface.runtime
        if run_id is None or runtime is None:
            return run_id, False
        try:
            snapshot = await runtime.snapshot(run_id)
        except UnknownGraphError:
            # A saved work order can outlive the graph it was started from.
            return run_id, False
        return run_id, snapshot.status in STEERABLE_RUN_STATUSES  # pyright: ignore[reportOptionalMemberAccess]  # Baseline: see docs/pyright.md

    async def github_checkout(project: str) -> str:
        """The local checkout a forge `project` key is worked on in.

        A matching `[repos]` slug selects its configured path directly. For
        checkouts named with a local alias, ask which has `project` as its
        `origin`. A forge on a non-default web port keys its
        projects by that port, which a remote does not carry, so the comparison
        ignores it.
        """
        for name, path in repository_registry.snapshot.repos.items():
            if name.lower() == project.lower():
                return str(Path(path).expanduser())
        authority, _, rest = project.partition("/")
        wanted = f"{authority.partition(':')[0]}/{rest}" if "/" in rest else project

        async def origin(path: str) -> str | None:
            try:
                process = await asyncio.create_subprocess_exec(
                    "git", "-C", path, "remote", "get-url", "origin",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError:
                return None
            try:
                stdout, _ = await process.communicate()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            return remote_project(stdout.decode(errors="replace")) if process.returncode == 0 else None

        # Expanded here, not only for the lookup: the path returned is handed
        # to `git -C` by the worktree provider, which does not expand `~`.
        paths = dict.fromkeys(
            str(Path(path).expanduser())
            for path in (*repository_registry.snapshot.repos.values(), work_orders.repository) if path
        )
        lookups = {path: asyncio.create_task(origin(path)) for path in paths}
        # The checkouts are asked at once and share one deadline, so stalled
        # mounts cost a webhook that deadline once rather than once each, and
        # never hold the one ingress worker, and every delivery queued behind
        # it, indefinitely. Neither setting is required, and `asyncio.wait`
        # refuses an empty set, so a deployment with no checkout skips it.
        stalled = set()
        if lookups:
            _, stalled = await asyncio.wait(
                lookups.values(), timeout=GITHUB_CHECKOUT_TIMEOUT_SECONDS)
        for lookup in stalled:
            lookup.cancel()
        await asyncio.gather(*stalled, return_exceptions=True)
        for path, lookup in lookups.items():
            if lookup not in stalled and not lookup.exception() and lookup.result() == wanted:
                return path
        raise RuntimeError(
            f"could not start a work order: no checkout of {project} is configured "
            "under [repos] or work_orders.repository"
        )

    async def github_start_workorder(
        store: GithubProvenance | None, repository: str, number: int, prompt: str,
        *, replacing: RunId | None, requester: str | None = None,
        inputs: Mapping[str, str] | None = None,
    ) -> Continuation:
        """Start a work order for a pull request that has no run in flight.

        Deliberately without an origin, unlike the Slack concierge's: the
        commenter is answered on the pull request by the concierge's own fixed
        reply, and a ``github:`` channel is not somewhere the chat provider can
        post -- a run carrying one would send every progress update to a Slack
        channel that does not exist.

        The run claims the pull request as it starts. Provenance is otherwise
        written by opening one, which a run started here never does -- it
        pushes to the pull request the comment arrived on -- so without the
        claim nothing would own it and the next comment would start another
        work order, leaving several agents on one branch and letting anyone who
        can comment create runs without limit. Claiming needs somewhere to
        write, so a deployment whose runtime keeps no provenance starts nothing
        rather than starting what it cannot find again.

        Starting and claiming cannot be one operation -- the run id to claim
        with is the engine's answer to starting -- so the claim decides which
        run keeps the pull request and this cancels the one that did not. Two
        comments arriving together both find nothing in flight and both start,
        and a claim that replaced the earlier one would leave the run it
        displaced alive and unreachable, since every later comment is routed by
        that row. `replacing` is the finished run this pull request was last
        taken on by, if it had one, which is the only claim a start may take
        over: it is what the caller established has stopped working. A claim
        that cannot be written at all leaves nothing behind either -- the run
        is cancelled before the failure is raised, so the redelivery that
        follows starts one run rather than adding one.
        """
        graph = _mentioned_workflow()
        if graph is None:
            raise RuntimeError("no workflow is configured under `work_orders.workflow`")
        if store is None:
            raise RuntimeError(
                "could not start a work order: this runtime records no pull requests"
            )
        runtime = surface.runtime
        assert runtime is not None  # only reached with a runtime in hand
        state = await start_graph_run(
            runtime, graph,
            inputs=resolve_inputs(getattr(graph, "inputs", ()), inputs or {}),
            prompt=prompt, repository=await github_checkout(repository),
            requester=requester,
        )
        url = pull_request_url(repository, number)
        try:
            holder = await store.claim_pull_request(
                PullRequestRecord(
                    repository=repository.lower(), number=number, run_id=state.run_id,
                    opened_at=datetime.now(UTC).isoformat(), url=url,
                ),
                replacing=replacing,
            )
        except Exception:
            await _github_cancel_unclaimed(state.run_id)
            raise
        if holder != state.run_id:
            # Lost the race: the pull request is someone else's work order, so
            # the feedback goes there and this run is undone rather than left
            # working a branch nothing can reach.
            await _github_cancel_unclaimed(state.run_id)
            return await github_steer_workorder(holder, prompt)
        link = run_notifier.work_order_link(state)
        return Continuation(
            url=link.url if link else "", run_id=str(state.run_id), started=True,
        )

    async def _github_cancel_unclaimed(run_id: RunId) -> None:
        """Stop a run that did not end up owning its pull request.

        Best effort, and deliberately quiet: whatever is being reported when
        this is called -- the race lost, or the claim write that failed -- is
        the more useful thing to report, and a cancel that fails leaves a run
        visible in the list rather than a silent one.
        """
        runtime = surface.runtime
        if runtime is None:
            return
        try:
            await runtime.cancel(run_id)
        except Exception:
            log.exception("could not cancel unclaimed work order %s", run_id)

    async def github_pull_request_for_run(run_id: str) -> tuple[str, int] | None:
        """Which pull request this work order opened, if it opened one.

        The same claim `github_run_for_pull_request` reads, asked from the run
        instead: a page showing one work order's comments wants its pull
        request once, not the owner of every pull request somebody has
        commented on.
        """
        store = getattr(surface.runtime, "store", None)
        read = getattr(store, "pull_request_for_run", None)
        if read is None:
            return None
        return await read(RunId(run_id))

    async def github_steer_workorder(run_id: RunId, prompt: str) -> Continuation:
        """Deliver feedback to the work order that holds this pull request."""
        runtime = surface.runtime
        assert runtime is not None  # only reached with a runtime in hand
        snapshot = await runtime.snapshot(run_id)
        if snapshot is None:
            raise RuntimeError("could not reach the work order for this pull request")
        await runtime.steer(run_id, prompt, node_id=_reentry_node(runtime, snapshot))
        state = await session.state_store.load(run_id)
        link = run_notifier.work_order_link(state) if state is not None else None
        return Continuation(url=link.url if link else "", run_id=str(run_id))

    async def github_continue_workorder(
        origin: RunOrigin, prompt: str, allow_start: bool,
    ) -> Continuation:
        """Reach this pull request's work order, and write down what happened.

        The recording is here rather than inside the two branches below
        because this is the boundary the concierge calls: past it the broker
        answers the agent instead of raising, so a dispatch that failed would
        otherwise reach nothing that could record it, and the row would settle
        at "replied" -- carrying the undelivered notice, with no reason and no
        sign anything went wrong.
        """
        try:
            reached = await _github_reach_workorder(origin, prompt, allow_start)
        except Exception as failure:
            github_activity.dispatch_failed(str(failure) or type(failure).__name__)
            raise
        github_activity.dispatched(reached.run_id, started_run=reached.started)
        return reached

    async def _github_reach_workorder(
        origin: RunOrigin, prompt: str, allow_start: bool,
    ) -> Continuation:
        """Steer the work order this pull request already has, or start one.

        Which of the two happens is the host's to decide, not the agent's: it
        turns on what this process recorded when a run took the pull request
        on and on what the graph engine says that run is doing now, neither of
        which a commenter can influence. A pull request nobody is working on --
        opened by hand, or by a run that has since finished or lost its graph
        -- has no execution to steer, and steering one would either raise or
        reach nothing; a comment asking for a change there is a request for
        work, so it gets a work order only if the comment mentioned Engine.
        """
        repository = origin.channel.removeprefix("github:")
        number = int(origin.thread_id.partition("/review/")[0])
        if origin.review_comment_id:
            prompt += (f"\n\nRequested review thread: {origin.review_thread_id or 'lookup unavailable'}; "
                       f"root comment: {origin.review_comment_id}; PR: {pull_request_url(repository, number)}. "
                       "Reply using add_comment with in_reply_to_id. If thread_id is unavailable, "
                       "look it up with view_change_request; a plain reply can omit it. "
                       "For addressed work supply resolve=true and commit_sha; "
                       "for disagreement or a needed decision supply resolve=false.")
        runtime = surface.runtime
        if runtime is None:
            raise RuntimeError("could not reach a work order: graph runtime unavailable")
        run_id, steerable = await github_active_run(repository, number)
        if not steerable:
            if not allow_start:
                raise RuntimeError(
                    "no active work order and Engine was not @mentioned"
                )
            reached = await github_start_workorder(
                getattr(runtime, "store", None), repository, number, prompt, replacing=run_id,
                requester=origin.requester or None,
            )
        else:
            reached = await github_steer_workorder(run_id, prompt)  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md
        return reached

    async def github_react(request: FeedbackRequest, content: str) -> None:
        repository = request.origin.channel.removeprefix("github:")
        if repository_registry.snapshot.disconnected:
            checkout = await github_checkout(repository)
            if await repository_mode(checkout) == ForgeMode.DISCONNECTED:
                return
        number, _, review_id = request.origin.thread_id.partition("/review/")
        await session.capabilities.source_control.add_reaction(
            pull_request_url(repository, int(number)), int(request.comment_id), content,
            review_comment=bool(review_id),
        )

    github_concierge = GithubConcierge(
        provider=concierge_provider or CodexACPProvider(permissions=github_tool_permission),
        continue_workorder=github_continue_workorder, reply=github_reply, react=github_react,
    )

    posting_login: dict[str, str] = {}

    async def github_posting_login(repository: str) -> str:
        """The account Engine replies as, asked once per repository.

        A resolved login is a property of the credentials on that repository,
        so each repository keeps its own answer.

        A token held by a machine user posts comments that look like anybody
        else's: without knowing who this process posts as, the concierge answers
        its own reply and then answers that, forever. The credentials themselves are the
        authority on this, so they are asked rather than configured. A failure
        to answer propagates: the turn is retried on redelivery instead of
        replying into a loop this process cannot recognise.
        """
        if repository not in posting_login:
            posting_login[repository] = await session.capabilities.source_control.authenticated_login(
                pull_request_url(repository, 1).rsplit("/pull/", 1)[0]
            )
        return posting_login[repository]

    async def github_create_workorder(assignment: GithubAssignment) -> None:
        """An assignment is an explicit request to implement the issue."""
        delivery = urlsplit(assignment.url)
        found = change_request(delivery._replace(
            path=f"/{assignment.repository}/pull/{assignment.number}", query="", fragment="",
        ).geturl())
        if found is None:
            return
        repository = found.project
        graph = _mentioned_workflow()
        if graph is None:
            raise RuntimeError("no workflow is configured under `work_orders.workflow`")
        if surface.runtime is None:
            raise RuntimeError("could not start a work order: graph runtime unavailable")
        # Progress is reported back to the issue, mentioning whoever assigned it.
        await start_graph_run(
            surface.runtime, graph,
            inputs=resolve_inputs(getattr(graph, "inputs", ()), {}),
            prompt=(f"Implement issue #{assignment.number}: {assignment.title}\n\n"
                    f"{assignment.body}\n\nIssue: {assignment.url}\n"
                    "Issue references are added to commits automatically. When opening "
                    "the pull request, declare issue_resolution as resolves for complete "
                    "work or refs for partial work."),
            repository=await github_checkout(repository),
            origin=RunOrigin(
                channel=f"{GITHUB_CHANNEL_PREFIX}{repository}",
                thread_id=f"issue/{assignment.number}",
                issue_repository=assignment.repository,
                issue_number=assignment.number,
                author=assignment.sender,
                requester=github_requester(assignment.sender_id, assignment.sender) or "",
            ),
            requester=github_requester(assignment.sender_id, assignment.sender),
        )

    async def github_review_pull_request(requested: GithubReviewRequest) -> None:
        """A review requested from Engine starts a review of the pull request.

        The same run `engine review <PR URL>` starts, in connected mode and
        checked out at the pull request's branch, except that nobody waits at
        triage: whoever asked reads the review on the pull request, so the
        surviving findings and the impact analysis are posted there. A
        pull request a work order is still working on is left to it: that run
        reviews its own change.
        """
        delivery = urlsplit(requested.url)
        found = change_request(delivery._replace(
            path=f"/{requested.repository}/pull/{requested.number}", query="", fragment="",
        ).geturl())
        if found is None:
            return
        repository = found.project
        url = pull_request_url(repository, requested.number)
        # Whether the requester can write to the repository was asked by the
        # ingress before this was called; see `github_sender_may_act`.
        graph = _mentioned_workflow()
        if graph is None:
            raise RuntimeError("no workflow is configured under `work_orders.workflow`")
        declared = {item.name: item for item in getattr(graph, "inputs", ())}
        state = declared.get(STATE_INPUT)
        if state is None or WorkState.REVIEW not in state.choices:
            raise RuntimeError("the configured workflow cannot start in review")
        runtime = surface.runtime
        if runtime is None:
            raise RuntimeError("could not start a review: graph runtime unavailable")
        run_id, steerable = await github_active_run(repository, requested.number)
        if steerable:
            log.info(
                "a review of %s#%s was requested, but work order %s is still on it",
                repository, requested.number, run_id,
            )
            return
        await github_start_workorder(
            getattr(runtime, "store", None), repository, requested.number,
            f"Review pull request {url}: {requested.title}",
            replacing=run_id,
            requester=github_requester(requested.sender_id, requested.sender),
            inputs=review_inputs(
                declared, ref=f"origin/{requested.branch}", pr_url=url, branch=requested.branch,
                publish=True,
            ),
        )

    async def github_refuse_comment(comment: GithubComment) -> None:
        """Best-effort acknowledgement for mentions refused before a model turn."""
        try:
            async with asyncio.timeout(GITHUB_AUTHORIZATION_TIMEOUT_SECONDS):
                found = change_request(urlsplit(comment.url)._replace(
                    path=f"/{comment.repository}/pull/{comment.number}", query="", fragment="",
                ).geturl())
                if found is None:
                    return
                login = await github_posting_login(found.project)
                if not login or comment.author.lower() == login.lower() or not re.search(
                    rf"(?<![\w@-])@{re.escape(login)}(?![\w-])", comment.body, re.IGNORECASE,
                ):
                    return
                log.error("Engine refused GitHub mention %s in %s",
                          comment.comment_id, comment.repository)
                thread = str(comment.number)
                if comment.event == "pull_request_review_comment":
                    thread += f"/review/{comment.in_reply_to_id or comment.comment_id}"
                await github_react(FeedbackRequest(
                    origin=RunOrigin(channel=f"github:{found.project}", thread_id=thread,
                                     author=comment.author),
                    text=comment.body, comment_id=comment.comment_id, allow_start=True,
                ), "-1")
        except Exception:
            log.exception("Could not react to refused GitHub comment %s", comment.comment_id)

    async def github_concierge_turn(comment: GithubComment) -> None:
        # Issue comments do not start work; only assignment events do.
        if not comment.is_pull_request:
            await github_refuse_comment(comment)
            github_activity.ignored("not a pull request")
            return
        # The delivery names the forge as well as the repository. Preserve its
        # port in the shared key so claims and replies stay on that forge.
        delivery = urlsplit(comment.url)
        found = change_request(delivery._replace(
            path=f"/{comment.repository}/pull/{comment.number}", query="", fragment="",
        ).geturl())
        if found is None:
            github_activity.ignored("not a pull-request URL")
            return
        comment = replace(comment, repository=found.project)
        # Both lookups reach the forge, and the queue behind this has one
        # worker: a comment that waits here is every later comment waiting too,
        # so they are bounded together rather than left to whatever the
        # configured transport happens to bound. Timing out raises, which the
        # ingress treats like any other failure -- the comment is forgotten and
        # can be redelivered -- so a slow forge costs a retry, not the queue.
        async with asyncio.timeout(GITHUB_AUTHORIZATION_TIMEOUT_SECONDS):
            login = await github_posting_login(comment.repository)
            if comment.author.lower() == login.lower():
                # GitHub logins are case-insensitive, so the comparison is too.
                github_activity.ignored("posted by Engine itself")
                return
        if mentions_other_accounts(comment.body, login):
            github_activity.ignored("addressed to another account")
            return
        # Whether the author can write to the repository was asked by the
        # ingress before this was called; see `github_sender_may_act`.
        mentioned = bool(login and re.search(
            rf"(?<![\w@-])@{re.escape(login)}(?![\w-])", comment.body, re.IGNORECASE,
        ))
        if not mentioned:
            _, steerable = await github_active_run(comment.repository, comment.number)
            if not steerable:
                github_activity.ignored("no active work order and Engine was not @mentioned")
                return
        thread_id = str(comment.number)
        root_comment = None
        lookup_unavailable = False
        if comment.event == "pull_request_review_comment":
            root_comment = int(comment.in_reply_to_id or comment.comment_id)
            if not comment.thread_id:
                for attempt in range(2):
                    try:
                        async with asyncio.timeout(GITHUB_AUTHORIZATION_TIMEOUT_SECONDS / 2):
                            thread = await session.capabilities.source_control.review_thread(pull_request_url(found.project, found.number), root_comment)
                        comment = replace(comment, thread_id=thread.thread_id or "")
                        break
                    except NotImplementedError:
                        # An unsupported capability will not recover on retry.
                        break
                    except Exception:  # noqa: BLE001 -- #779: webhook boundary retries then logs and reports unavailable lookup
                        if attempt == 0:
                            await asyncio.sleep(0.25)
                            continue
                        lookup_unavailable = True
                        log.warning("Review thread lookup failed twice; forwarding feedback with root comment ID %s", root_comment)
            thread_id += f"/review/{root_comment}"
        await github_concierge.handle(FeedbackRequest(
            origin=RunOrigin(
                channel=f"github:{comment.repository}", thread_id=thread_id,
                author=comment.author,
                review_thread_id=comment.thread_id,
                review_comment_id=root_comment,
                requester=github_requester(comment.author_id, comment.author) or "",
            ),
            text=comment.body, comment_id=comment.comment_id, allow_start=mentioned,
        ))
        if lookup_unavailable:
            try:
                async with asyncio.timeout(GITHUB_AUTHORIZATION_TIMEOUT_SECONDS):
                    await github_reply(
                        RunOrigin(channel=f"github:{comment.repository}", thread_id=thread_id),
                        "Review-thread lookup is temporarily unavailable after two attempts. "
                        "Your comment was received, but automatic thread resolution may be unavailable. "
                        "Please retry later.",
                    )
            except Exception:  # noqa: BLE001 -- #779: webhook boundary logs and records failed retry advice
                # Do not replay already-forwarded work if the reply service is
                # unavailable too. Keep the retry advice visible in activity.
                github_activity.ignored("Review-thread service unavailable; please retry later.")
                log.warning("Could not post review-thread retry advice")

    async def github_merge_approves_workorder(merged: GithubMerge) -> None:
        """Merging a pull request is a person accepting its work order.

        The same decision as the Accept button on the WorkOrder page, made
        where the reviewer already is: merging is the one event that closes out
        a work order's pull request, and somebody who has read the diff and
        merged it has reviewed the run. Asking them to say so a second time in
        another tab is asking for a click that says nothing new. Rejecting
        stays the web UI's: closing a pull request without merging says the
        work was abandoned, not that it was judged.

        `merge_from_payload` has already refused a bot's merge. Engine's own is
        refused here, against the login its credentials resolve to: a machine
        user's token merges as an ordinary `User`. Anybody else who merged is
        a person GitHub let write to the repository -- the same permission the
        comment path calls
        `can_write_repository` to establish, here proven by the merge itself.

        A merge that decides nothing is not a failure: a pull request opened by
        hand and one whose work order has stopped arrive here with no verdict
        to record. A work order still working towards its review keeps the
        merge until it asks for one. Anything that does go wrong raises, so the
        delivery can be redelivered rather than silently losing the approval.
        """
        runtime = surface.runtime
        if runtime is None:
            return
        run_id = await github_run_for_pull_request(merged.repository, merged.number)
        if run_id is None:
            log.info(
                "%s#%s was merged, but no work order opened it",
                merged.repository, merged.number,
            )
            return
        try:
            snapshot = await runtime.snapshot(run_id)
        except UnknownGraphError:
            # A saved work order can outlive the graph it was started from.
            log.info(
                "%s#%s was merged, but work order %s can no longer be run",
                merged.repository, merged.number, run_id,
            )
            return
        if snapshot.status in (RunStatus.COMPLETED, RunStatus.FAILED):  # pyright: ignore[reportOptionalMemberAccess]  # Baseline: see docs/pyright.md
            log.info(
                "%s#%s was merged, but work order %s has already stopped",
                merged.repository, merged.number, run_id,
            )
            return
        # Asked only for a run the merge can still decide: a pull request
        # opened by hand or a finished work order must not wait on, or fail
        # with, a credential lookup.
        async with asyncio.timeout(GITHUB_AUTHORIZATION_TIMEOUT_SECONDS):
            engine_login = await github_posting_login(merged.repository)
        if merged.merged_by.lower() == engine_login.lower():
            log.info(
                "%s#%s was merged by Engine itself, which is not a review",
                merged.repository, merged.number,
            )
            return
        # Kept before looking for the review, so one requested while this looks
        # still finds the merge waiting for it.
        merges_awaiting_review[run_id] = merged
        await github_accept_merged_review(run_id)

    async def github_accept_merged_review(run_id: RunId) -> None:
        """Answer a work order's human review with the merge kept for it.

        Called when the merge arrives and again when the run asks for its
        review, whichever comes second finding both halves: a pull request can
        be merged while the run is still finishing the steps before the review,
        and GitHub will not send the merge a second time.
        """
        merged = merges_awaiting_review.get(run_id)
        runtime = surface.runtime
        if merged is None or runtime is None:
            return
        try:
            snapshot = await runtime.snapshot(run_id)
        except UnknownGraphError:
            merges_awaiting_review.pop(run_id, None)
            return
        if snapshot.status in (RunStatus.COMPLETED, RunStatus.FAILED):  # pyright: ignore[reportOptionalMemberAccess]  # Baseline: see docs/pyright.md
            merges_awaiting_review.pop(run_id, None)
            return
        pending = next(
            (
                approval
                for approval in snapshot.pending_approvals  # pyright: ignore[reportOptionalMemberAccess]  # Baseline: see docs/pyright.md
                if approval.tool_name == HUMAN_REVIEW_TOOL
            ),
            None,
        )
        if pending is None:
            log.info(
                "%s#%s was merged before work order %s asked for its human review; "
                "the merge will answer it when it does",
                merged.repository, merged.number, run_id,
            )
            return
        merges_awaiting_review.pop(run_id, None)
        try:
            await runtime.decide(run_id, pending.approval_id, ApprovalDecision.ACCEPT)
        except (UnknownApprovalError, ApprovalNotPendingError):
            # Somebody decided it between the snapshot and here -- the web UI,
            # or a cancellation. The verdict is already recorded; a second one
            # is not owed.
            log.info(
                "%s#%s was merged, but work order %s had already been decided",
                merged.repository, merged.number, run_id,
            )
            return
        log.info(
            "%s merging %s#%s accepted the human review of work order %s",
            merged.merged_by, merged.repository, merged.number, run_id,
        )

    async def github_sender_may_act(
        delivery: GithubComment | GithubAssignment | GithubReviewRequest,
    ) -> bool:
        """Whether whoever sent `delivery` can write to its repository.

        Asked by the ingress before any handler runs: before a comment becomes
        a prompt or is forwarded to a work order, and before an assignment or
        a review request starts one. A comment is untrusted text and the agent that reads it can
        read the host it runs on, so whoever writes one is choosing what this
        process reads and what it says back in public. `author_association`
        does not bound that -- a COLLABORATOR may hold read access alone. Write
        access is the line: it is already the authority to change this
        repository, so it is no escalation to reach the agent working on it.

        Bounded, since the queue behind this has one worker; a timeout raises,
        which the ingress treats like any other failure, so the delivery can be
        redelivered.
        """
        if isinstance(delivery, GithubComment):
            sender, sender_id = delivery.author, delivery.author_id
        else:
            sender, sender_id = delivery.sender, delivery.sender_id
        found = change_request(urlsplit(delivery.url)._replace(
            path=f"/{delivery.repository}/pull/{delivery.number}", query="", fragment="",
        ).geturl())
        repository = found.project if found is not None else delivery.repository
        may_write: bool | None = None
        if sender_id and repository.lower() in access_repositories():
            # The same per-user answer, and cache, that scopes the web app:
            # a sender who was just checked there, or here, is not asked again.
            async with asyncio.timeout(GITHUB_AUTHORIZATION_TIMEOUT_SECONDS):
                writable = await github_login.writable_repositories(
                    {"id": sender_id, "login": sender}
                )
            if writable is not None and repository.lower() in writable:
                may_write = True
            elif github_login.access_known(sender_id):
                may_write = False
        if may_write is None:
            # Not one of this deployment's repositories, or its answer is
            # unknown: ask GitHub about this one directly.
            async with asyncio.timeout(GITHUB_AUTHORIZATION_TIMEOUT_SECONDS):
                may_write = await session.capabilities.source_control.can_write_repository(
                    pull_request_url(repository, delivery.number), sender,
                )
        if not may_write:
            # Ignored rather than answered: a refusal posted back is both noise
            # and a way to make this process talk to somebody it will not act for.
            log.info("ignored a GitHub delivery on %s#%s from %s, who cannot write to it",
                     repository, delivery.number, sender)
            github_activity.ignored(f"{sender} cannot write to {repository}")
            if isinstance(delivery, GithubComment):
                await github_refuse_comment(delivery)
        return may_write

    github_ingress = GithubIngress(
        webhook_secret=github_webhook_secret,
        repository=github_repository,
        repositories=github_repositories,
        authenticated_login=github_posting_login,
        may_act=github_sender_may_act,
        handle=github_comment_handler or github_concierge_turn,
        handle_merge=github_merge_approves_workorder,
        handle_assignment=github_create_workorder,
        handle_review_request=github_review_pull_request,
        activity=github_activity,
    )

    async def run_github_comments(request: Request) -> JSONResponse:
        """The GitHub comments left on this WorkOrder's pull request.

        Scoped by path rather than filtered by query because a comment only
        means anything next to the work it steered: read on its own it is a
        line from a conversation with no subject. Hanging it under the run
        also keeps the one listing of every comment this process ever saw --
        including comments about work that is none of this WorkOrder's
        business -- off the API entirely.

        Which comments those are is resolved here rather than remembered with
        each one: a pull request's owner is written down when it is opened,
        which can be after a comment on it was recorded, so joining at read
        time is what lets a row reach the right page at all. Asked from this
        run -- one seek, whatever the log holds -- rather than by asking who
        owns each remembered pull request and discarding every answer naming
        somebody else.
        """
        run_id = request.path_params["run_id"]
        if await run_hidden(request, RunId(run_id)):
            return _error("run not found", 404)
        pull_request = await github_pull_request_for_run(run_id)
        if pull_request:
            repository = pull_request[0]
        else:
            state = await session.state_store.load(RunId(run_id))
            repository = (
                run_project(state.repository or work_orders.repository) or ""
                if state is not None else github_repository
            )
        return JSONResponse(activity_json(
            github_activity.recent(),
            run_id=run_id,
            pull_request=pull_request,
            repository=repository,
            # Local readiness only: GitHub's webhook setup and delivery are
            # independent of whether Engine accepts this repository.
            configured=bool(
                repository.lower() in {
                    repo.lower() for repo in (github_repository, *github_repositories) if repo
                }
                and github_webhook_secret()
            ),
        ))

    def _mentioned_workflow() -> GraphWorkflow | None:
        """Which workflow a mention runs: the configured one, or the only one.

        Answered from the graphs actually on offer rather than from the
        catalog, because a mention that resolved to a workflow this process
        could not start would be accepted and then go nowhere.
        """
        offered = offered_graphs()
        if work_orders.workflow:
            return offered.get(work_orders.workflow)
        return next(iter(offered.values())) if len(offered) == 1 else None

    # --- runner utilization ---------------------------------------------------

    _utilization = utilization or UtilizationService()

    async def read_utilization(_request: Request) -> JSONResponse:
        """What was true the last time anybody looked, answered without looking.

        Deliberately offline: this is what the page draws while the scrape
        below is still in flight, so it must not wait on the same providers.
        """
        return JSONResponse(utilization_json(_utilization.cached()))

    async def refresh_utilization(request: Request) -> Response:
        # Reads the tokens the runners signed in with, so it is held to the same
        # origin check as the other endpoints that touch a stored credential.
        if not _is_local_request(request):
            return _error("forbidden", 403)
        readings = await _utilization.refresh(tuple(runners))
        return JSONResponse(utilization_json(readings))

    # Anyone who can push to one of the repositories this deployment works on
    # may sign in: the webhook repository and the configured checkouts. What
    # they see is the WorkOrders of the repositories they can push to.
    def access_repositories() -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            project.lower() for project in (
                github_repository, *github_repositories, *repository_registry.snapshot.login_repositories
            ) if project
        ))

    async def github_repository_access(user_id: int, login: str) -> dict[str, bool | None]:
        """Whether the user can push to each of this deployment's repositories.

        None marks a lookup that failed or timed out: that repository's answer
        is unknown, and is not read as a no.
        """
        async def check(project: str) -> bool | None:
            try:
                # The check reads the repository from a pull request URL; the
                # number names no particular one.
                async with asyncio.timeout(GITHUB_LOGIN_TIMEOUT_SECONDS):
                    return await session.capabilities.source_control.can_write_repository(
                        pull_request_url(project, 1), login, user_id=user_id,
                    )
            except Exception:
                log.exception("could not check whether %s can write to %s", login, project)
                return None

        projects = access_repositories()
        answers = await asyncio.gather(*(check(project) for project in projects))
        return dict(zip(projects, answers, strict=True))

    # The GitHub repository behind each value a run's `repository` takes: a
    # `[repos]` name, the checkout path the web form sends, or `.`. A run
    # started from GitHub already names its `owner/repo`.
    def run_project(repository: str) -> str | None:
        project = repository_registry.snapshot.run_projects.get(repository)
        if project is None and "/" in repository and not repository.startswith(("/", ".", "~")):
            project = repository.lower()
        return project

    def repository_visible(visible: frozenset[str] | None, repository: str) -> bool:
        """Whether a run in `repository` is among `visible`; None sees everything.

        A run whose repository maps to no GitHub repository is only for
        operators, who see everything.
        """
        if visible is None:
            return True
        project = run_project(repository or work_orders.repository)
        return project is not None and project in visible

    def same_repository(repository: str, other: str) -> bool:
        """Whether two runs' repositories are the same GitHub repository, or the same checkout."""
        repository, other = repository or work_orders.repository, other or work_orders.repository
        project = run_project(repository)
        return project == run_project(other) if project is not None else repository == other

    async def run_hidden(request: Request, run_id: RunId) -> bool:
        """Whether `run_id` is outside what `request` may see.

        Answered as a missing run, 404 rather than 403, so that a run's
        existence is not revealed to someone who may not see it.
        """
        visible = await github_login.visible_repositories(request)
        if visible is None:
            return False
        state = await session.state_store.load(run_id)
        return state is None or not repository_visible(visible, state.repository)

    # The GitHub repository whose checkout holds each chat's workspace branch,
    # once found: the branch stays in the repository it was made in, attached
    # or not, so it is what a chat's transcript and agent are about.
    branch_projects: dict[str, str] = {}

    async def thread_project(thread: ChatThread) -> str | None:
        """The GitHub repository a chat's workspace branch lives in, if one holds it."""
        ref = thread.workspace_ref
        if ref is None:
            return None
        if ref not in branch_projects:
            for name, path in {".": ".", **repository_registry.snapshot.repos}.items():
                project = run_project(name)
                if project is None:
                    continue
                try:
                    process = await asyncio.create_subprocess_exec(
                        "git", "-C", str(Path(path).expanduser()),
                        "rev-parse", "--verify", "--quiet", f"refs/heads/{ref}",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                except OSError:
                    continue
                if await process.wait() == 0:
                    branch_projects[ref] = project
                    break
        return branch_projects.get(ref)

    async def thread_hidden(request: Request, thread: ChatThread) -> bool:
        """Whether this chat belongs to a repository `request` may not see.

        A chat that never had a workspace has no repository to be scoped by. One
        whose branch no configured checkout holds is, like such a run, only for
        those who see everything.
        """
        visible = await github_login.visible_repositories(request)
        if visible is None or thread.workspace_ref is None:
            return False
        project = await thread_project(thread)
        return project is None or project not in visible

    def thread_scoped(handler: Callable[[Request], Awaitable[Response]]):
        """`handler`, answering a chat the requester may not see as missing.

        404 rather than 403, as for runs, so its existence is not revealed. A
        stream it opens is ended once the chat's repository is out of reach.
        """
        async def scoped(request: Request) -> Response:
            thread = await service.get(_thread_id(request))
            if thread is not None:
                if await thread_hidden(request, thread):
                    return _error("thread not found", 404)

                async def still_visible() -> bool:
                    return not await thread_hidden(request, thread)

                request.scope[STREAM_ACCESS] = still_visible
            return await handler(request)

        return scoped

    async def github_user_repository_access(token: str) -> dict[str, bool | None]:
        """Whether GitHub says the account holding sign-in `token` can push to each of these repositories.

        Asked with the user's own token, so it answers even when the server's
        connection does not. The token has only `read:user` scope, so GitHub
        answers for public github.com repositories; the rest read as unknown
        (None), as does any lookup that fails.
        """
        projects = [project for project in access_repositories() if project.count("/") == 1]
        if not projects:
            return {}

        async def check(client: httpx.AsyncClient, project: str) -> bool:
            response = await client.get(
                f"https://api.github.com/repos/{project}",
                headers={"Accept": "application/vnd.github+json",
                         "Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            permissions = response.json().get("permissions")
            return isinstance(permissions, dict) and any(
                permissions.get(role) is True for role in ("admin", "maintain", "push")
            )

        async with (
            asyncio.timeout(GITHUB_LOGIN_TIMEOUT_SECONDS),
            httpx.AsyncClient(timeout=GITHUB_LOGIN_TIMEOUT_SECONDS) as client,
        ):
            answers = await asyncio.gather(
                *(check(client, project) for project in projects), return_exceptions=True
            )
        return {
            project: answer if isinstance(answer, bool) else None
            for project, answer in zip(projects, answers, strict=True)
        }

    github_login = GitHubLogin(
        github_login_config, service_token, github_repository_access,
        operators=frozenset(login_operators),
        authorize_user=github_user_repository_access,
        access_timeout=GITHUB_LOGIN_TIMEOUT_SECONDS,
    )
    routes = [
        Route("/api/health", health),
        *github_login.routes(),
        Route("/api/config", config),
        Route("/api/github/status", github_status),
        Route("/api/source-control/status", source_control_status),
        Route("/api/source-control/provider", source_control_provider_status),
        Route(
            "/api/source-control/provider",
            source_control_provider_status,
            methods=["GET"],
        ),
        Route(
            "/api/source-control/provider",
            set_source_control_provider,
            methods=["POST"],
        ),
        Route("/api/github/client-id", github_get_client_id),
        Route("/api/github/client-id", github_set_client_id, methods=["POST"]),
        Route("/api/github/connect", github_connect, methods=["POST"]),
        Route("/api/github/connect/poll", github_connect_poll, methods=["POST"]),
        Route("/api/github/disconnect", github_disconnect, methods=["POST"]),
        Route("/api/gitlab/status", gitlab_status),
        Route("/api/gitlab/client-id", gitlab_set_client_id, methods=["POST"]),
        Route("/api/gitlab/connect", gitlab_connect, methods=["POST"]),
        Route("/api/gitlab/connect/poll", gitlab_connect_poll, methods=["POST"]),
        Route("/api/gitlab/disconnect", gitlab_disconnect, methods=["POST"]),
        Route("/api/slack/status", slack_status),
        Route("/api/slack/credentials", slack_set_credentials, methods=["POST"]),
        Route("/api/slack/connect", slack_connect, methods=["POST"]),
        Route("/api/slack/callback", slack_callback, name="slack_callback"),
        Route("/api/slack/disconnect", slack_disconnect, methods=["POST"]),
        Route("/api/slack/events", slack_ingress.webhook, methods=["POST"]),
        Route("/api/loops/settings", get_loop_settings),
        Route("/api/loops/settings", set_loop_settings, methods=["PUT"]),
        Route("/api/loops/defaults", new_loop_defaults),
        Route("/api/loops", list_loops),
        Route("/api/loops", create_loop, methods=["POST"]),
        Route("/api/loops/{loop_id}", get_loop),
        Route("/api/loops/{loop_id}", delete_loop, methods=["DELETE"]),
        Route("/api/utilization", read_utilization),
        Route("/api/utilization/refresh", refresh_utilization, methods=["POST"]),
        Route("/api/runs", list_runs),
        Route("/api/runs", create_run, methods=["POST"]),
        Route("/api/runs/{run_id}", get_run),
        Route("/api/runs/{run_id}/start", start_scheduled_run, methods=["POST"]),
        Route("/api/runs/{run_id}", delete_run, methods=["DELETE"]),
        Route("/api/runs/{run_id}/graph-events", graph_run_events),
        Route("/api/runs/{run_id}/github-comments", run_github_comments),
        # The graph half of the runs above, served by the engine that runs
        # them rather than by this file.
        Mount(GRAPH_PREFIX, app=graph_surface),
        # Registered graphs, runs of them, loops and node steering, for the
        # `engine graph|loop|node` commands.
        Mount("/api/v1", app=graph_service_surface),
        Route("/api/threads", list_threads),
        Route("/api/threads", create_thread, methods=["POST"]),
        Route("/api/threads/{thread_id}", thread_scoped(get_thread)),
        Route("/api/threads/{thread_id}", thread_scoped(update_thread), methods=["PATCH"]),
        Route("/api/threads/{thread_id}", thread_scoped(delete_thread), methods=["DELETE"]),
        Route(
            "/api/threads/{thread_id}/archive",
            thread_scoped(archive_thread),
            methods=["POST"],
            name="archive",
        ),
        Route(
            "/api/threads/{thread_id}/unarchive",
            thread_scoped(archive_thread),
            methods=["POST"],
            name="unarchive",
        ),
        Route("/api/threads/{thread_id}/messages", thread_scoped(messages)),
        Route("/api/threads/{thread_id}/approval-events", thread_scoped(approval_events)),
        Route(
            "/api/threads/{thread_id}/workspace",
            thread_scoped(attach_workspace),
            methods=["POST"],
        ),
        Route(
            "/api/threads/{thread_id}/workspace",
            thread_scoped(detach_workspace),
            methods=["DELETE"],
        ),
        Route("/api/threads/{thread_id}/title", thread_scoped(title_thread), methods=["POST"]),
        Route("/api/threads/{thread_id}/runs", thread_scoped(run_thread), methods=["POST"]),
        Route("/api/threads/{thread_id}/runs/current", thread_scoped(resume_run)),
        Route("/api/threads/{thread_id}/runs/current", thread_scoped(cancel_run), methods=["DELETE"]),
        Route(
            "/api/threads/{thread_id}/runs/current/approvals/{approval_id}",
            thread_scoped(decide_approval),
            methods=["POST"],
        ),
    ]
    routes.append(Route("/api/github/events", github_ingress.webhook, methods=["POST"]))
    if static_directory is not None and (static_directory / "index.html").is_file():

        async def spa_page(_request: Request) -> Response:
            return FileResponse(
                static_directory / "index.html",
                headers={"cache-control": "no-cache"},
            )

        routes.extend(
            [
                Route("/login", spa_page),
                Route("/runs", spa_page),
                Route("/runs/new", spa_page),
                Route("/runs/{run_id}/conversations/{thread_id}", spa_page),
                Route("/runs/{run_id}", spa_page),
                Route("/conversations", spa_page),
                Route("/conversations/{thread_id}", spa_page),
                Route("/utilization", spa_page),
            ]
        )
        routes.append(Mount("/", BuiltClient(directory=static_directory, html=True)))
    else:
        routes.append(Route("/", _missing_frontend))
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(WebGZipMiddleware, minimum_size=1024, compresslevel=5)],
    )
    app.state.thread_service = service
    app.state.slack_ingress = slack_ingress
    app.state.github_ingress = github_ingress
    # Enforce session auth on API routes when GitHub login is configured.
    app = github_login.middleware(app)
    return app  # pyright: ignore[reportReturnType]  # Baseline: see docs/pyright.md


def _with_workspace(thread: ChatThread, state: WorkspaceState | None) -> ChatThread:
    """Fold a provider's answer into the thread the UI is shown."""
    thread.workspace_id = state.workspace_id if state is not None else None
    thread.workspace_ref = state.ref if state is not None else None
    thread.workspace_root = state.root_path if state is not None else None
    return thread


def _thread_json(thread: ChatThread) -> dict[str, object]:
    result: dict[str, object] = {
        "id": str(thread.instance_id),
        "title": thread.title,
        "archived": thread.archived,
        "agentId": str(thread.agent_id),
        "runner": thread.runner,
        # Present but detached is a state of its own: the work is still there,
        # on the ref, and attaching brings a checkout back to it.
        "workspaceAttached": thread.workspace_root is not None,
    }
    if thread.workspace_root is not None:
        result["workspaceRoot"] = thread.workspace_root
    if thread.workspace_ref is not None:
        result["workspaceRef"] = thread.workspace_ref
    return result


def _run_json(run: WorkflowRunView, *, listing: bool = False) -> dict[str, object]:
    """One WorkOrder, as a client is shown it.

    A listing leaves out the prose: the task prompt and a failure's reason.
    Every screen polls `/api/runs` once a second to keep the rail current, so
    what that list carries is what every screen pays for, on a payload that
    grows with every run ever started. The pages that draw the prose read the
    one run they are about from `/api/runs/{run_id}`.
    """
    result: dict[str, object] = {
        "runId": str(run.run_id),
        "name": run.name,
        "workflowId": run.workflow_id,
        "workflowName": run.workflow_name,
        "taskId": run.task_id,
        "repository": run.repository,
        "repositoryContext": {"repository": run.repository},
        "parentRunId": str(run.parent_run_id) if run.parent_run_id else None,
        "dependsOnRunId": str(run.depends_on_run_id) if run.depends_on_run_id else None,
        "phase": run.phase,
        "terminalOutcome": run.terminal_outcome,
        "startedAt": run.started_at.isoformat() if run.started_at else None,
    }
    if listing:
        return result
    result["taskPrompt"] = run.task_prompt
    result["failureReason"] = run.failure_reason
    result["requester"] = run.requester
    return result


def _approval_json(approval: ApprovalRecord) -> dict[str, object]:
    """One complete request, as the client is shown it.

    Whole rather than incremental, like the content snapshots beside it: a
    client that reconnected mid-pause has no way to reconstruct a request from
    the parts of it that were emitted before it arrived.
    """
    result = {
        "id": str(approval.approval_id),
        "status": approval.status.value,
        "kind": approval.kind.value,
        "reason": approval.reason,
        "command": approval.command,
        "cwd": approval.cwd,
        "toolName": approval.tool_name,
        # The call this was asked about, so the client can show the request
        # beside it rather than collecting every request at the end of a turn.
        "toolCallId": approval.tool_call_id,
        "arguments": approval.arguments,
        "allowedDecisions": [decision.value for decision in approval.allowed_decisions],
        "decision": approval.decision.value if approval.decision else None,
        # Who decided, so the client can tell an answer the user gave from one
        # a grant gave on their behalf. A request nobody was shown still has to
        # read as something that happened, not as something that was skipped.
        "decisionSource": (
            approval.decision_source.value if approval.decision_source else None
        ),
    }
    if approval.questions is not None:
        result["questions"] = _json_value(approval.questions, [])
    if approval.answers is not None:
        result["answers"] = _json_value(approval.answers, None)
    return result


def _json_value(value: str | None, default: object) -> object:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _messages_json(messages: tuple[Message, ...]) -> list[dict[str, object]]:
    """Group the engine's turn transcript into assistant-ui messages."""
    result: list[dict[str, object]] = []
    assistant_content: list[dict[str, object]] = []
    assistant_id = ""
    tool_calls: dict[str, dict[str, object]] = {}

    def flush_assistant() -> None:
        nonlocal assistant_content, assistant_id
        if assistant_content:
            result.append(
                {
                    "id": assistant_id or f"assistant-{len(result)}",
                    "role": Role.ASSISTANT.value,
                    "content": assistant_content,
                }
            )
        assistant_content = []
        assistant_id = ""

    for index, message in enumerate(messages):
        if message.role is Role.USER:
            flush_assistant()
            if message.content:
                result.append(
                    {
                        "id": str(message.message_id or f"user-{index}"),
                        "role": Role.USER.value,
                        "content": [{"type": "text", "text": message.content}],
                    }
                )
            continue
        if not assistant_id and message.message_id:
            assistant_id = str(message.message_id)
        _merge_message(assistant_content, message, tool_calls)
    flush_assistant()
    return result


def _tool_call_ids(messages: Iterable[Message]) -> set[str]:
    """Every provider call id already present in a transcript."""
    return {call.call_id for message in messages for call in message.tool_calls}


def _merge_message(
    content: list[dict[str, object]],
    message: Message,
    tool_calls: dict[str, dict[str, object]] | None = None,
) -> bool:
    """Fold one engine message into one assistant-ui assistant response."""
    if tool_calls is None:
        tool_calls = {
            str(part["toolCallId"]): part
            for part in content
            if part.get("type") == "tool-call" and "toolCallId" in part
        }
    changed = False
    if message.role is Role.ASSISTANT:
        if message.content:
            content.append({"type": "text", "text": message.content})
            changed = True
        for call in message.tool_calls:
            # Provider streams and resumed sessions can replay the same item.
            # It is one call semantically, and assistant-ui requires it to be
            # one resource structurally, so retain the first occurrence.
            if call.call_id in tool_calls:
                continue
            try:
                arguments = json.loads(call.arguments)
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            part: dict[str, object] = {
                "type": "tool-call",
                "toolCallId": call.call_id,
                "toolName": call.name,
                "args": arguments,
                "argsText": call.arguments,
            }
            content.append(part)
            tool_calls[call.call_id] = part
            clarification = _clarification_context(call.name, arguments)
            if clarification:
                content.append({"type": "text", "text": clarification})
            changed = True
    elif message.role is Role.TOOL and message.tool_call_id:
        part = tool_calls.get(message.tool_call_id)  # pyright: ignore[reportAssignmentType]  # Baseline: see docs/pyright.md
        if part is not None:
            part["result"] = message.content
            changed = any(candidate is part for candidate in content)
    return changed


_CLARIFICATION_TOOLS = frozenset(
    {
        "askuserquestion",
        "escalate",
        "escalatetohuman",
        "requestclarification",
        "requesthumanreview",
        "requestuserinput",
    }
)


def _clarification_context(tool_name: str, arguments: object) -> str | None:
    """Extract the question text from a provider's clarification tool call."""

    leaf_name = tool_name.rsplit("__", 1)[-1].rsplit(".", 1)[-1]
    normalized = "".join(
        character for character in leaf_name.lower() if character.isalnum()
    )
    if normalized not in _CLARIFICATION_TOOLS or not isinstance(arguments, dict):
        return None

    questions = arguments.get("questions")
    candidates = questions if isinstance(questions, list) else (arguments,)
    context: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            text = candidate.strip()
        elif isinstance(candidate, dict):
            text = next(
                (
                    value.strip()
                    for key in ("question", "prompt", "message")
                    if isinstance((value := candidate.get(key)), str) and value.strip()
                ),
                "",
            )
        else:
            text = ""
        if text and text not in context:
            context.append(text)
    return "\n\n".join(context) or None


_TITLE_PROMPT = (
    "Name this chat based on the conversation above. Reply with only a concise "
    "title of at most eight words, with no quotes or ending punctuation."
)


def _clean_title(value: str) -> str:
    first_line = value.strip().splitlines()[0] if value.strip() else ""
    return first_line.strip(" \t\"'`).:;!?")[:80]


async def _json_body(request: Request) -> dict[str, object]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def _required_string(body: dict[str, object], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(body: dict[str, object], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _thread_id(request: Request) -> AgentInstanceId:
    return AgentInstanceId(request.path_params["thread_id"])


def _new_agent_run_id() -> AgentRunId:
    return AgentRunId(f"ar-{uuid4().hex[:12]}")


def _json_line(value: dict[str, object]) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def _server_event(value: dict[str, object]) -> bytes:
    return f"data:{json.dumps(value, separators=(',', ':'))}\n\n".encode()


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _missing_frontend(_request: Request) -> Response:
    return Response(
        "The assistant-ui client has not been built. Run `npm --prefix apps/web run build`.",
        status_code=503,
        media_type="text/plain",
    )


__all__ = ["ChatThread", "ThreadService", "create_app"]
