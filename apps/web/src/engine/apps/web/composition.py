"""Composition root for the web control interface.

The one file in this app allowed to name concrete adapters, and a sibling of the
other two compositions rather than shared code with them -- for the same reason
the worker's is: these processes will diverge, and sharing now would couple
three deployables that should be free to move independently.

Three of the six capabilities here are real. `agent_runner` reaches Codex or
Claude over ACP, through the same `langgraph-acp` providers the graph workflows
use, `state_store` persists conversations in SQLite, and `workspace_provider`
gives every chat an isolated Git worktree. The other three remain wired for the
composition report but are not exposed by the chat API.

`Capabilities` holds one runner because a port has one implementation, and that
is the one anything non-interactive uses. The interface additionally offers a
*choice* of runner, which is `build_runners` -- a name-to-implementation mapping
of exactly the kind a composition root exists to own.

The state store is SQLite rather than Postgres: conversations survive a process
restart without requiring an external database service.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from pathlib import Path

from langgraph_acp.providers import (
    CLAUDE_ACP_COMMAND,
    CODEX_ACP_COMMAND,
    OPENCODE_ACP_COMMAND,
    ClaudeACPProvider,
    CodexACPProvider,
    OpenCodeACPProvider,
)

from engine.runtime.repositories import RepositoryRegistry
from engine.adapters.agent_runner.acp import (
    READ_ONLY_TOOLS,
    allowed_tools_for,
    claude_acp_runner,
    claude_session_config,
    codex_acp_runner,
    opencode_acp_runner,
)
from engine.adapters.communications.slack import (
    SlackCommunications,
    SlackCredentialStore,
)
from engine.adapters.sandbox.process import ProcessSandbox
from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.source_control.github.transports import (
    GitHubCliTransport,
    GitHubOAuthTransport,
)
from engine.adapters.source_control.gitlab import GitLabSourceControl
from engine.adapters.source_control.gitlab.transports import GitLabOAuthTransport
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.adapters.workflow_runtime.temporal import TemporalWorkflowRuntime
from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_ROOT_DIRECTORY,
    GitWorktreeWorkspaceProvider,
)
from engine.apps.web.gitlab_auth import (
    GitLabAuthError,
    GitLabCredentialStore,
    GitLabRefreshTokenInvalidError,
)
from engine.apps.web.gitlab_auth import (
    refresh_access_token as refresh_gitlab_access_token,
)
from engine.apps.web.github_auth import (
    GitHubAuthError,
    GitHubCredentialStore,
    GitHubRefreshTokenInvalidError,
)
from engine.apps.web.github_auth import (
    refresh_access_token as refresh_github_access_token,
)
from engine.apps.web.github_webhook import GitHubWebhookConfig
from engine.apps.web.oauth_credentials import OAuthCredentialStore, StoredCredentials
from engine.apps.web.source_control import (
    RoutingSourceControl,
    SourceControlPreferences,
)
from engine.graph_runtime import GraphRuntime, GraphWorkflow
from engine.graph_runtime_langgraph import LangGraphRuntime, answer_permission
from engine.graph_runtime_langgraph.workflows import RUNS, agent_registry, sqlite_runtime
from engine.graph_service import GraphService, StartRun
from engine.graph_service.service import opencode_config
from engine.graph_service.store import AgentRow
from engine.ports import AgentRunner, Communications, SourceControl
from engine.runtime import (
    AgentSession,
    Capabilities,
    EngineConfig,
)


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the interface needs from the environment.

    `host` and `port` are handed to Uvicorn by `__main__`; the rest are adapter
    arguments. `__main__` fills the bind address and state paths from
    `engine.toml`, overridden by `ENGINE_*` environment variables.

    Frozen so one immutable settings value can be shared by the server wiring.
    """

    host: str = "localhost"
    port: int = 4364
    codex_binary: str = "codex"
    """The Codex CLI milestone scoping runs. Chat's runners reach Codex through
    `codex_acp_command`, whose adapter brings its own."""
    codex_acp_command: tuple[str, ...] = CODEX_ACP_COMMAND
    """The ACP adapter chat's Codex runners launch."""
    codex_sandbox: str = "read-only"
    """What a turn nobody is watching may do: read, and nothing else.

    `build_capabilities` wires the one runner a non-interactive caller reaches
    for, so it gets the sandbox that needs no one present. Chat is the other
    case and takes `interactive_codex_sandbox`. codex-acp cannot be asked for a
    sandbox, so `codex_acp_runner` enforces it under the adapter.
    """
    codex_working_directory: str = "."
    codex_timeout_seconds: float | None = None
    """No ceiling: a turn runs until it is done or someone cancels it."""
    codex_model: str = ""
    claude_acp_command: tuple[str, ...] = CLAUDE_ACP_COMMAND
    """The ACP adapter chat's Claude runners launch."""
    claude_working_directory: str = "."
    claude_timeout_seconds: float | None = None
    """Same as `codex_timeout_seconds`."""
    claude_model: str = ""
    opencode_acp_command: tuple[str, ...] = OPENCODE_ACP_COMMAND
    """The OpenCode chat's OpenCode runners launch; it speaks ACP itself."""
    opencode_working_directory: str = "."
    opencode_timeout_seconds: float | None = None
    """Same as `codex_timeout_seconds`."""
    opencode_model: str = ""
    """`provider/model`, as OpenCode names them. Empty is OpenCode's own default."""
    interactive_codex_sandbox: str = "workspace-write"
    """An approved command has to be able to do the thing it was approved for.

    The read-only sandbox would refuse the write after the user allowed it, and
    on-request approval is what keeps that from meaning "unattended": Codex
    stops and asks before stepping outside the worktree.

    So this is not built from `approvals.allow`, and cannot be: a capability
    that policy leaves unruled is one a person may still allow mid-turn, and a
    sandbox narrowed before the turn started would refuse what they just
    allowed. Codex's policy is applied to its requests instead.
    """
    temporal_host: str = "localhost:7233"
    github_token: str = ""
    github_client_id: str = ""
    github_webhook: GitHubWebhookConfig | None = None
    """Which repository's webhook deliveries are answered, and their secret.

    ``None`` when the deployment named neither, which is what leaves the
    webhook route refusing deliveries instead of trusting unsigned ones.
    """
    source_control_preferences: SourceControlPreferences | None = None
    workspace_root: str = DEFAULT_ROOT_DIRECTORY
    sqlite_path: str = "conversations.sqlite3"
    graph_state_directory: str = "graph-state"
    """Where a graph workflow's saved progress is kept.

    Plain English: the new graph workflows remember where they got to by
    writing two small database files. This is the folder those files go in, so
    a run that was half finished when the process stopped is still there when
    it starts again. A folder rather than a file because there are two of them,
    and the graph engine names them itself.
    """
    engine_config: EngineConfig = EngineConfig()
    """Provider-neutral settings loaded from TOML.

    `approvals` is where chat's permissions are written down: it builds the
    interactive Claude runner's tool list, and answers both runners' approval
    requests. No field here holds a second copy of it.
    """
    config_path: Path | None = None
    """The single TOML source, or ``None`` when built-in defaults are active."""


def build_capabilities(
    settings: Settings,
    slack_credential_store: SlackCredentialStore | None = None,
    gitlab_credential_store: GitLabCredentialStore | None = None,
    github_credential_store: GitHubCredentialStore | None = None,
) -> Capabilities:
    """Wire every port to its concrete implementation."""
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    # GH CLI acts as the host's `gh auth` login. GitHub OAuth acts as the
    # device-flow token `engine connect github` (or Settings) saved, from the
    # store the web interface shares so the keychain is read once. Browser
    # login identifies the UI user and never reaches here; neither does
    # GITHUB_TOKEN.
    logging.getLogger(__name__).info(
        "source_control composition=web github_identity=gh-cli credential=gh auth"
    )
    github = GitHubSourceControl(
        "",
        resolve_addressed_threads=settings.engine_config.github.resolve_addressed_threads,
        host_aliases=settings.engine_config.github.host_aliases,
        workspace_provider=workspace_provider,
        transport=GitHubCliTransport(),
    )
    github_store = github_credential_store or GitHubCredentialStore(cached=True)
    github_refresh_lock = asyncio.Lock()

    def _github_token() -> str:
        credentials = github_store.get_credentials()
        return credentials.access_token if credentials else ""

    async def _refresh_github_after_unauthorized(failed_token: str) -> bool:
        async with github_refresh_lock:
            return await _refresh_after_unauthorized(
                github_store,
                failed_token,
                lambda: settings.github_client_id or github_store.get_client_id(),
                refresh_github_access_token,
                GitHubRefreshTokenInvalidError,
                GitHubAuthError,
            )

    github_oauth = GitHubSourceControl(
        _github_token,
        resolve_addressed_threads=settings.engine_config.github.resolve_addressed_threads,
        host_aliases=settings.engine_config.github.host_aliases,
        workspace_provider=workspace_provider,
        transport=GitHubOAuthTransport(
            _github_token, on_token_unauthorized=_refresh_github_after_unauthorized
        ),
    )

    def _gitlab_origin() -> str:
        return (  # pyright: ignore[reportReturnType]  # Baseline: see docs/pyright.md
            settings.source_control_preferences.gitlab_origin()
            if settings.source_control_preferences is not None
            and settings.source_control_preferences.gitlab_origin()
            else "https://gitlab.com"
        )

    _gitlab_stores: dict[str, GitLabCredentialStore] = {}
    _gitlab_refresh_persistence_failed: set[str] = set()

    def _gitlab_store() -> GitLabCredentialStore:
        origin = _gitlab_origin()
        if gitlab_credential_store is not None:
            return gitlab_credential_store
        return _gitlab_stores.setdefault(origin, GitLabCredentialStore(origin))

    def _gitlab_token() -> str:
        store = _gitlab_store()
        credentials = store.get_credentials()
        return credentials.access_token if credentials else ""

    async def _refresh_gitlab_after_unauthorized(failed_token: str) -> bool:
        store = _gitlab_store()
        if store.origin in _gitlab_refresh_persistence_failed:
            return False
        async with store.refresh_lock():
            return await _refresh_after_unauthorized(
                store,
                failed_token,
                store.get_client_id,
                lambda client_id, refresh_token: refresh_gitlab_access_token(
                    store.origin, client_id, refresh_token
                ),
                GitLabRefreshTokenInvalidError,
                GitLabAuthError,
                on_persist_failed=lambda: _gitlab_refresh_persistence_failed.add(store.origin),
            )

    gitlab = GitLabSourceControl(
        _gitlab_token,
        origin=_gitlab_origin,
        workspace_provider=workspace_provider,
        transport=GitLabOAuthTransport(
            _gitlab_token, _gitlab_origin, _refresh_gitlab_after_unauthorized
        ),
    )
    if settings.source_control_preferences is None:
        source_control = github
    else:
        source_control = RoutingSourceControl(
            settings.source_control_preferences,
            github,
            github_oauth,
            gitlab,
        )
    Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    if settings.engine_config.sandbox.backend != "process":
        raise NotImplementedError("smolvm sandbox backend is not installed")
    return Capabilities(
        sandbox=ProcessSandbox(),
        workflow_runtime=TemporalWorkflowRuntime(settings.temporal_host),
        source_control=source_control,  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md
        agent_runner=codex_acp_runner(
            command=settings.codex_acp_command,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
        communications=build_communications(settings, slack_credential_store),
        workspace_provider=workspace_provider,
        state_store=SQLiteStateStore(settings.sqlite_path),
    )


async def _refresh_after_unauthorized(
    store: OAuthCredentialStore,
    failed_token: str,
    client_id: Callable[[], str | None],
    refresh: Callable[[str, str], Awaitable[StoredCredentials]],
    invalid_refresh_token: type[Exception],
    auth_error: type[Exception],
    *,
    on_persist_failed: Callable[[], None] = lambda: None,
) -> bool:
    """Replace `store`'s rejected `failed_token` by refreshing it; whether to retry.

    Called with the store's refresh lock held. A token another caller already
    replaced is retried as is; a refresh token the provider refuses is deleted,
    so the next attempt asks to connect again instead of refreshing forever.
    """
    credentials = store.get_credentials()
    if credentials is None or not credentials.refresh_token:
        return False
    if credentials.access_token != failed_token:
        return True
    identifier = client_id()
    if not identifier:
        return False
    try:
        refreshed = await refresh(identifier, credentials.refresh_token)
    except invalid_refresh_token:
        store.delete()
        return False
    except auth_error:
        return False
    try:
        store.set_credentials(refreshed)
    except auth_error:
        on_persist_failed()
        return False
    return True


def build_communications(
    settings: Settings,
    slack_credential_store: SlackCredentialStore | None = None,
) -> Communications:
    """Build the configured communications provider."""
    provider = settings.engine_config.communications.provider
    if provider == "buzz":
        raise RuntimeError(
            "communications provider 'buzz' is not available yet; "
            "configure communications.provider = 'slack'"
        )
    return SlackCommunications(slack_credential_store or SlackCredentialStore())


def build_graph_runtime(
    settings: Settings,
    graphs: Sequence[GraphWorkflow],
    source_control: SourceControl | None = None,
) -> AbstractAsyncContextManager[GraphRuntime] | None:
    """The engine that runs graph workflows, or nothing when there are none.

    It hands back an *unopened* context manager rather than a running engine,
    because starting one opens database files that somebody then has to close.
    The web application opens it when the server starts and closes it when the
    server stops, which is the only lifetime that gets that right.

    `None` when this deployment's workflow directory holds no graphs: there is
    nothing to run, so there is no reason to open the files. The interface then
    offers no graph entries, which is what keeps a person from picking one
    that nothing here could start.
    """
    if not graphs:
        return None
    return sqlite_runtime(
        tuple(graphs),  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md
        settings.graph_state_directory,
        source_control=source_control,
    )


def build_graph_service(
    settings: Settings,
    *,
    default_repository: str = "",
    repositories: Mapping[str, str] | RepositoryRegistry | None = None,
) -> Callable[[LangGraphRuntime, StartRun], GraphService]:
    config = settings.engine_config
    """How the daemon offers registered graphs, once its graph engine is open.

    A factory rather than a service, because the service registers graphs on
    the runtime and the runtime does not exist until the server starts. Its
    agents are langgraph-acp sessions on the same providers the workflow
    directory's graphs use; its tables live in the graph engine's own file.
    """

    def build(runtime: LangGraphRuntime, start: StartRun) -> GraphService:
        return GraphService(
            runtime,
            Path(settings.graph_state_directory) / RUNS,
            workspace_provider=GitWorktreeWorkspaceProvider(settings.workspace_root),
            registry=agent_registry([CodexACPProvider(), ClaudeACPProvider(), OpenCodeACPProvider()]),
            agent_factory=added_agent,
            start=start,
            session_config=claude_session_config_for(settings),
            default_repository=default_repository,
            repositories=repositories,
            model_tiers=config.model_tiers,
            allow_python=config.graphs.allow_python,
            session_tools=config.sessions.tools,
            approval_policy=config.approvals,
        )

    return build


def added_agent(row: AgentRow) -> object:
    """The provider an agent added with `engine agent add` runs as, answering permissions like the built-ins."""
    if row.kind == "claude":
        provider: object = ClaudeACPProvider(name=row.name)
    elif row.kind == "codex":
        provider = CodexACPProvider(name=row.name)
    else:
        config = opencode_config(row)
        env = {"OPENCODE_CONFIG_CONTENT": json.dumps(config)} if config else None
        provider = OpenCodeACPProvider(name=row.name, env=env)
    return replace(provider, permissions=answer_permission)


def claude_session_config_for(settings: Settings) -> dict[str, object] | None:
    """The ACP session metadata that wires Engine's TOML settings to Claude.

    Translates the deployment's ``attribution`` and ``[claude] output_style``
    into the dict the ``claude-agent-acp`` adapter reads from
    ``session/new`` under ``_meta``. Returns ``None`` when every setting is at its default.
    """
    return claude_session_config(
        attribution=settings.engine_config.attribution,
        output_style=settings.engine_config.claude.output_style,
    )



def build_runners(settings: Settings) -> Mapping[str, AgentRunner]:
    """Every agent runner this process offers, by the name the interface shows.

    The one place a runner name is bound to an implementation -- below this file
    "codex", "claude" and "opencode" are opaque strings, exactly like tool grants. The first
    entry is the default, so it is also what a conversation gets when nobody
    picks.

    One entry per agent: the dropdown names the agent you are talking to, not
    the transport it is driven over -- ACP, for all of them. All pause for approval, because a runner that
    could only run unattended is not a second choice worth offering -- what it
    would do without asking, these do after asking.

    What Claude may do without asking is `approvals.allow` from `engine.toml`:
    a preapproved tool is one whose requests never reach the callback, and one
    left off the list is still allowed if a person allows it. Codex has no such
    list -- its pre-turn knob is a sandbox, which is a ceiling rather than a
    preapproval -- so its whole policy is applied to its requests instead. Shell
    is on neither list on purpose: a shell rule is written per command rather
    than per capability, so `Bash` reaches the callback either way and
    `approvals.bash` is applied there. OpenCode is Codex's case: it is set to
    ask before every change, and the policy answers those requests.
    """
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    return {
        "codex": codex_acp_runner(
            command=settings.codex_acp_command,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.interactive_codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
        "claude": claude_acp_runner(
            command=settings.claude_acp_command,
            timeout_seconds=settings.claude_timeout_seconds,
            allowed_tools=allowed_tools_for(settings.engine_config.approvals.allow),
            working_directory=settings.claude_working_directory,
            model=settings.claude_model,
            attribution=settings.engine_config.attribution,
            output_style=settings.engine_config.claude.output_style,
            workspace_provider=workspace_provider,
        ),
        "opencode": _opencode_runner(settings, workspace_provider, read_only=False),
    }


def _opencode_runner(
    settings: Settings, workspace_provider: GitWorktreeWorkspaceProvider, *, read_only: bool
) -> AgentRunner:
    return opencode_acp_runner(
        command=settings.opencode_acp_command,
        read_only=read_only,
        timeout_seconds=settings.opencode_timeout_seconds,
        working_directory=settings.opencode_working_directory,
        model=settings.opencode_model,
        attribution=settings.engine_config.attribution,
        workspace_provider=workspace_provider,
    )


def build_read_only_runners(settings: Settings) -> Mapping[str, AgentRunner]:
    """The runners without the tools to change anything, by provider name.

    Two callers, one property: a workflow review step, and any profile that says
    it only reads. Named for what the runners are rather than for the first
    thing that wanted them, because the planner wants them for the same reason
    the reviewer does.

    Not built from `approvals.allow`, and deliberately: being unable to write is
    a property of the work rather than a permission the deployment gets to
    widen. A policy granting `edit` is a statement about what an agent may do
    when somebody asks it to change something, not about the one asked to read.

    Withholding the tools is half of it. The other half is that a `read_only`
    profile's approvals are refused by the broker, so a policy cannot hand back
    at the pause what this withheld before the turn. For Codex the withholding
    is its read-only sandbox, which `codex_acp_runner` holds every turn to; for
    OpenCode it is denying edit, shell and fetch outright.
    """
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    return {
        "codex": codex_acp_runner(
            command=settings.codex_acp_command,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
        "claude": claude_acp_runner(
            command=settings.claude_acp_command,
            timeout_seconds=settings.claude_timeout_seconds,
            allowed_tools=READ_ONLY_TOOLS,
            tools=READ_ONLY_TOOLS,
            working_directory=settings.claude_working_directory,
            model=settings.claude_model,
            attribution=settings.engine_config.attribution,
            output_style=settings.engine_config.claude.output_style,
            workspace_provider=workspace_provider,
        ),
        "opencode": _opencode_runner(settings, workspace_provider, read_only=True),
    }


def build_session(
    capabilities: Capabilities,
    runners: Mapping[str, AgentRunner],
    repository: str = ".",
    read_only_runners: Mapping[str, AgentRunner] | None = None,
) -> AgentSession:
    """Conversations, over the capabilities this process composed.

    Takes the capability set rather than settings so the interface and the chat
    share one store -- two `build_capabilities` calls would open independent
    connections rather than sharing the session's store object.

    A conversation may be continued by any of `runners`, including one that did
    not start it: we hold the transcript, so whichever answers next is handed
    everything the other one said and did.

    `read_only_runners` answers the agents that only read, by the same provider
    names -- so a planning conversation is the CLI the user picked, without the
    tools to change the tree it is reading.
    """
    return AgentSession(
        capabilities,
        runners=runners,
        workspace_repository=repository,
        read_only_runners=read_only_runners,
    )


__all__ = [
    "Settings",
    "build_capabilities",
    "build_read_only_runners",
    "build_runners",
    "build_session",
    "claude_session_config_for",
]
