"""Small session doubles shared by web fixtures and endpoint tests."""

import asyncio
from collections.abc import Sequence
from engine.domain import AgentProfile, AgentRunId, Message
from engine.ports import AgentTurn, Workspace, WorkspaceState
from engine.ports import Message as CommunicationsMessage
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR


class ConcurrentRunner:
    """A controllably slow runner that records how much work overlaps."""

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    def __init__(self, replies: Sequence[str] = ("ok",)) -> None:
        self.replies = list(replies)
        self.seen: list[tuple[Message, ...]] = []
        self.workspace_ids: list[str | None] = []
        self.active = 0
        self.most_active = 0

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        self.seen.append(tuple(messages))
        self.workspace_ids.append(workspace_id)
        self.active += 1
        self.most_active = max(self.most_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        reply = self.replies.pop(0) if self.replies else "ok"
        return AgentTurn(Message.assistant(reply))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        pass


class ConversationWorkspaces:
    """A provider whose checkouts come and go, as real ones do."""

    def __init__(self) -> None:
        self.count = 0
        self.detached: set[str] = set()
        self.attachments: list[tuple[str, str, str]] = []

    async def provision(
        self, repository: str, base_ref: str, *, co_author: str = ""
    ) -> Workspace:
        self.count += 1
        return self._workspace(f"ws-{self.count}", repository, base_ref)

    async def root_path(self, workspace_id: str) -> str:
        if workspace_id in self.detached:
            message = f"no workspace {workspace_id!r}"
            raise KeyError(message)
        return f"/worktrees/{workspace_id}"

    async def state(self, workspace_id: str) -> WorkspaceState:
        return WorkspaceState(
            workspace_id=workspace_id,
            ref=f"engine/{workspace_id}",
            root_path=(
                None if workspace_id in self.detached else f"/worktrees/{workspace_id}"
            ),
        )

    async def attach(
        self, workspace_id: str, repository: str, base_ref: str, *, co_author: str = ""
    ) -> Workspace:
        self.attachments.append((workspace_id, repository, base_ref))
        self.detached.discard(workspace_id)
        return self._workspace(workspace_id, repository, base_ref)

    async def detach(self, workspace_id: str) -> None:
        self.detached.add(workspace_id)

    async def dispose(self, workspace_id: str) -> None:
        self.detached.add(workspace_id)

    def _workspace(
        self, workspace_id: str, repository: str, base_ref: str
    ) -> Workspace:
        return Workspace(
            workspace_id=workspace_id,
            root_path=f"/worktrees/{workspace_id}",
            repository=repository,
            base_ref=base_ref,
            ref=f"engine/{workspace_id}",
        )


class RecordingCommunications:
    """A chat provider that remembers what was said and where."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, CommunicationsMessage | str, str]] = []

    async def post(self, channel, message, run_id=None, thread_id="") -> str:
        self.posts.append((channel, message, thread_id))
        return "1700.0002"

    async def reply(self, message_id: str, message: str) -> str:  # pragma: no cover
        raise NotImplementedError


class GreetingRunner:
    """A minimal runner that satisfies ``McpAgentRunner`` for tests.

    Returns a canned greeting from the concierge without calling any tools.
    """

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    async def run_turn(
        self, agent_run_id, profile, messages, tools=(), workspace_id=None
    ):
        return AgentTurn(message=Message.assistant("Hi, how can I help?"))

    async def run_turn_with_mcp(
        self, agent_run_id, profile, messages, mcp_server, workspace_id=None
    ):
        return AgentTurn(message=Message.assistant("Hi, how can I help?"))

    async def cancel(self, agent_run_id):
        pass
