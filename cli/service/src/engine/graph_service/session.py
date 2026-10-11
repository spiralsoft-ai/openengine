"""Interactive sessions: a person's own agent CLI as a graph run's implementation node.

`engine agent claude|codex|opencode` starts a run of one built-in graph: the workspace node
every graph has, then a node that does what an implementation node does --
binds the run's repository tools to the checkout -- and hands them to a CLI
the person drives in their own terminal, instead of opening an ACP session.
The run is a run like any other, so it is listed, its pull requests are
recorded, and its mode decides which tools it is served.

The node waits for the terminal to say the session ended. A session lives in
this process: if the daemon restarts while one is open, the node cannot reach
its terminal again, and the run fails saying so.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from engine.domain import (
    MODE_INPUT,
    ApprovalDecision,
    ForgeMode,
    RunFailed,
    StepCompleted,
)
from engine.graph_runtime.inputs import WorkflowInput
from engine.graph_runtime_langgraph import (
    GraphWorkflow,
    State,
    TerminalMcpServer,
    WorkspaceNode,
    current_execution,
    graph_workflow,
)
from engine.graph_runtime_langgraph.components.forge import PUBLISH_CHANGE
from engine.graph_service.language import STAGE_GROUPS
from engine.ports import ApprovalRequest, WorkspaceProvider
from engine.ports.permissions import ApprovalCapability, PermissionScope
from engine.runtime import ApprovalConfig, PolicyDecision, policy_decision_for
from langgraph.graph import END, START, StateGraph

SESSION_GRAPH_ID = "engine-session"
SESSION_INPUT = "session"
BASE_INPUT = "base"
CHECKOUT_NODE = "workspace"
SESSION_NODE = "implement"
#: The harnesses a session can be driven with.
SESSION_AGENTS = ("claude", "codex", "opencode")


@dataclass
class Session:
    """One open session, as the node and the HTTP surface both see it."""

    session_id: str
    agent: str
    run_id: str = ""
    ready: asyncio.Future[dict[str, Any]] = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    ended: asyncio.Future[str] = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    @property
    def status(self) -> str:
        if self.ready.done() and self.ready.exception() is not None:
            return "failed"
        if self.ended.done():
            return "ended"
        return "ready" if self.ready.done() else "starting"


class Sessions:
    def __init__(self) -> None:
        self._open: dict[str, Session] = {}

    def create(self, agent: str) -> Session:
        session = Session(f"s-{uuid.uuid4().hex[:12]}", agent)
        self._open[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._open.get(session_id)


@dataclass(frozen=True, slots=True)
class SessionNode:
    """An implementation node whose agent is a person's own CLI."""

    sessions: Sessions
    tools: tuple[str, ...]
    policy: ApprovalConfig

    graph_node_name: str = "Interactive session"
    graph_node_kind: str = "session"
    graph_node_description: str = "An agent CLI driven from a terminal, with this run's repository tools."
    graph_node_group: str = STAGE_GROUPS["implement"]
    graph_node_show_in_sidebar: bool = False

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        execution = current_execution()
        inputs = state.get("inputs")
        session_id = inputs.get(SESSION_INPUT) if isinstance(inputs, Mapping) else None
        session = self.sessions.get(str(session_id or ""))
        if session is None or session.ready.done():
            raise RuntimeError("this session's terminal is gone; the daemon restarted while it was open")
        binding = TerminalMcpServer(
            step_id=str(execution.node_id), agent_id=session.agent, repository_tools=self.tools,
        )
        try:
            async with binding(state, execution, self._approve) as bound:
                session.ready.set_result({
                    "workspace": {
                        "path": str(state.get("workspace") or ""),
                        "ref": str(state.get("workspaceRef") or ""),
                        "id": str(state.get("workspaceId") or ""),
                    },
                    "mcp": dict(bound.config),
                    "instructions": session_instructions(state),
                    "settings": agent_settings(session.agent, self.policy),
                })
                summary = await self._summary(session, bound.result)
        except BaseException as error:
            if not session.ready.done():
                session.ready.set_exception(RuntimeError(str(error) or type(error).__name__))
            raise
        return {SESSION_NODE: summary}

    async def _summary(self, session: Session, result: Any) -> str:
        """What the session did: what the agent reported with complete_step, else that it ended."""
        reported = asyncio.ensure_future(result()) if result is not None else None
        try:
            ended = await asyncio.shield(session.ended)
        finally:
            if reported is not None and not reported.done():
                reported.cancel()
        if reported is not None and reported.done() and not reported.cancelled():
            event = reported.result()
            if isinstance(event, RunFailed):
                raise RuntimeError(event.reason)
            if isinstance(event, StepCompleted) and event.summary:
                return event.summary
        return ended or "The interactive session ended."

    async def _approve(self, request: ApprovalRequest) -> ApprovalDecision:
        """The person approved this call in their CLI; Engine's policy can still refuse it."""
        decision = policy_decision_for(self.policy, PermissionScope(ApprovalCapability.MCP, request.tool_name))
        return ApprovalDecision.CANCEL if decision is PolicyDecision.DENY else ApprovalDecision.ACCEPT


def session_workflow(
    workspace_provider: WorkspaceProvider,
    sessions: Sessions,
    *,
    tools: Sequence[str],
    policy: ApprovalConfig,
    default_base_ref: str = "origin/HEAD",
) -> GraphWorkflow:
    builder: Any = StateGraph(State)  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md
    builder.add_node(CHECKOUT_NODE, WorkspaceNode(workspace_provider, base_ref=default_base_ref, ref_input=BASE_INPUT))
    builder.add_node(SESSION_NODE, SessionNode(sessions, tuple(tools), policy))
    builder.add_edge(START, CHECKOUT_NODE)
    builder.add_edge(CHECKOUT_NODE, SESSION_NODE)
    builder.add_edge(SESSION_NODE, END)
    return graph_workflow(
        builder,
        id=SESSION_GRAPH_ID,
        name="Interactive session",
        inputs=(
            WorkflowInput(SESSION_INPUT, "Session"),
            WorkflowInput(BASE_INPUT, "Base ref"),
            # Declared so the daemon replaces it with the repository's own mode.
            WorkflowInput(MODE_INPUT, "Mode", ForgeMode.CONNECTED, choices=tuple(ForgeMode)),
        ),
    )


def session_instructions(state: Mapping[str, object]) -> str:
    """What the CLI's agent is told, beside the person's own prompts: an implementation node's publishing rules."""
    return (
        "You are working in an OpenEngine workspace with a person at the keyboard. "
        "Follow their requests. " + PUBLISH_CHANGE(state)
    )


def agent_settings(agent: str, policy: ApprovalConfig) -> dict[str, Any]:
    """The agent's own settings carrying the shell rules Engine enforces on its own agents.

    Engine's patterns are globs over a whole command. Only the shape the deny
    list is written in -- a literal command, or one whose only wildcard is a
    trailing ` **` -- can be said the same way to every agent; any other
    pattern is left out rather than approximated. Codex has no command rules
    its configuration can carry, so it is given none; the repository tools
    still refuse what Engine refuses.
    """
    rules = [rule for rule in (_command_rule(pattern) for pattern in policy.bash.deny) if rule]
    if not rules:
        return {}
    if agent == "claude":
        return {"permissions": {"deny": [f"Bash({head}:*)" if prefix else f"Bash({head})" for head, prefix in rules]}}
    if agent == "opencode":
        # OpenCode takes the last bash rule that matches, so these, merged
        # after the operator's own, win; `head *` alone would miss `head`.
        bash: dict[str, str] = {}
        for head, prefix in rules:
            bash[head] = "deny"
            if prefix:
                bash[f"{head} *"] = "deny"
        return {"permission": {"bash": bash}}
    return {}


def claude_settings(policy: ApprovalConfig) -> dict[str, Any]:
    """Claude Code settings carrying Engine's shell rules; see `agent_settings`."""
    return agent_settings("claude", policy)


def _command_rule(pattern: str) -> tuple[str, bool] | None:
    """`(command, whether anything may follow it)`, or None for a pattern no agent can be told."""
    head = pattern.removesuffix(" **")
    if any(character in head for character in "*?["):
        return None
    return head, head != pattern


__all__ = [
    "SESSION_AGENTS",
    "SESSION_GRAPH_ID",
    "Session",
    "SessionNode",
    "Sessions",
    "agent_settings",
    "claude_settings",
    "session_workflow",
]
