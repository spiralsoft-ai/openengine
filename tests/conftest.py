"""Lazy, function-scoped factories for isolated test infrastructure.

Explicit paths allow restart/migration tests to reopen the same database.
Applications and clients are built only on demand; lifespan remains controlled
by the test's context manager, including tests that exercise startup failures.
"""

from pathlib import Path
import subprocess

import pytest

from engine.runtime.config import CONFIG_ENVIRONMENT_VARIABLE


@pytest.fixture(autouse=True)
def _engine_config_is_not_inherited(monkeypatch):
    """Keep an exported ``ENGINE_CONFIG`` out of every test.

    A developer with Engine running has this set to their own checkout's
    `engine.toml`, which names workflow and worktree directories outside this
    one; tests that load configuration without naming a file would then read
    that deployment's rather than the repository's. Tests wanting a particular
    configuration still set the variable themselves -- this only removes what
    nobody asked for.
    """

    monkeypatch.delenv(CONFIG_ENVIRONMENT_VARIABLE, raising=False)


@pytest.fixture
def sqlite_store(tmp_path):
    """Create independent stores, or reopen an explicit path; close all at teardown."""
    from engine.adapters.state_store.sqlite import SQLiteStateStore

    stores = []

    def build(path=None):
        path = path if path is not None else tmp_path / f"state-{len(stores)}.sqlite3"
        store = SQLiteStateStore(path)
        stores.append(store)
        return store

    yield build
    for store in reversed(stores):
        store.close()


@pytest.fixture
def git_repo(tmp_path):
    """Initialize a checkout (or bare remote), optionally with an initial commit."""
    count = 0

    def build(path=None, branch="main", *, bare=False, commit=False, origin=None):
        nonlocal count
        path = Path(path) if path is not None else tmp_path / f"repo-{count}"
        count += 1
        path.mkdir(parents=True, exist_ok=True)

        def git(*args):
            subprocess.run(
                ["git", "-C", str(path), *args], check=True, capture_output=True
            )

        git("init", "--quiet", "-b", branch, *(["--bare"] if bare else []))
        if commit:
            (path / "README.md").write_text("engine\n")
            git("add", "README.md")
            git(
                "-c",
                "user.name=Engine Tests",
                "-c",
                "user.email=engine@example.test",
                "commit",
                "--quiet",
                "-m",
                "initial",
            )
        if origin is not None:
            git("remote", "add", "origin", str(origin))
        return path

    return build


@pytest.fixture
def web_app():
    """Build the real web app with an explicit session or lightweight defaults.

    Session dependencies are keyword overrides; remaining options go directly
    to create_app. Each call gets a fresh in-memory store unless supplied.
    """
    from engine.adapters.state_store.memory import InMemoryStateStore
    from engine.apps.web import api
    from engine.runtime import AgentSession, Capabilities

    def build(
        session=None,
        runners=None,
        *args,
        state_store=None,
        runner=None,
        profiles=None,
        workspaces=None,
        communications=None,
        workspace_repository=None,
        **overrides,
    ):
        if session is None:
            stub = object()
            if runner is None:
                runner = next(iter(runners.values()), stub) if runners else stub
            runners = runners if runners is not None else {"test": runner}
            session = AgentSession(
                Capabilities(
                    workflow_runtime=stub,
                    source_control=stub,
                    agent_runner=runner,
                    communications=communications
                    if communications is not None
                    else stub,
                    workspace_provider=workspaces if workspaces is not None else stub,
                    state_store=state_store
                    if state_store is not None
                    else InMemoryStateStore(),
                ),
                profiles=profiles if profiles is not None else {},
                runners=runners,
                workspace_repository=workspace_repository,
            )
        if runners is None:
            runners = session.runners
        return api.create_app(session, runners, *args, **overrides)

    return build


@pytest.fixture
def client(web_app):
    """Synchronous client factory; use as a context manager to run app lifespan."""
    from starlette.testclient import TestClient

    def build(app=None, **options):
        return TestClient(app if app is not None else web_app(), **options)

    return build


@pytest.fixture
def async_client():
    """Async ASGI clients; the caller controls lifespan and async cleanup."""
    import httpx

    def build(app, *, base_url="http://test", **options):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=base_url, **options
        )

    return build


@pytest.fixture
def slack_app(web_app):
    """App, capabilities and credentials for Slack and GitHub concierge tests."""
    from unittest.mock import MagicMock
    from engine.adapters.communications.slack import (
        SlackCredentials,
        SlackCredentialStore,
    )
    from engine.adapters.state_store.memory import InMemoryStateStore
    from engine.runtime import AgentSession, Capabilities, WorkflowCatalog
    from provider_fakes import FakeACPProvider
    from web_fakes import GreetingRunner

    SIGNING_SECRET = "shhh"

    def build(
        communications,
        work_orders,
        catalog=None,
        provider=None,
        github_login_config=None,
        runner=None,
        workspaces=None,
        graph_runtime=None,
        github_comment_handler=None,
        github_webhook_secret="",
        github_repositories=(),
        approval_policy=None,
        repos=None,
    ):

        stub = object()
        runner = runner or GreetingRunner()
        capabilities = Capabilities(
            workflow_runtime=stub,
            source_control=stub,
            agent_runner=runner,
            communications=communications,
            workspace_provider=workspaces or stub,
            state_store=InMemoryStateStore(),
        )
        runners = {"default": runner}
        session = AgentSession(capabilities, profiles={}, runners=runners)
        slack_store = MagicMock(spec=SlackCredentialStore)
        slack_store.credentials.return_value = SlackCredentials("client", "secret")
        slack_store.token.return_value = "xoxb-token"
        slack_store.signing_secret.return_value = SIGNING_SECRET
        return (
            web_app(
                session,
                runners,
                workflow_catalog=(
                    catalog if catalog is not None else WorkflowCatalog.from_graphs(())
                ),
                **(
                    {}
                    if approval_policy is None
                    else {"approval_policy": approval_policy}
                ),
                slack_credential_store=slack_store,
                github_login_config=github_login_config,
                public_url="https://engine.example",
                work_orders=work_orders,
                repos=repos,
                credential_store=MagicMock(),
                concierge_provider=provider or FakeACPProvider(),
                graph_runtime=graph_runtime,
                github_comment_handler=github_comment_handler,
                github_webhook_secret=lambda: github_webhook_secret,
                github_repository="acme/api",
                github_repositories=github_repositories,
            ),
            capabilities,
            slack_store,
        )

    return build


@pytest.fixture
def github_app(slack_app, git_repo, tmp_path):
    """Concierge app with the configured GitHub repository checked out locally."""

    def build(*arguments, repos=None, **options):
        if repos is None:
            repos = {
                "acme/api": str(
                    git_repo(
                        tmp_path / "acme-api", origin="https://github.com/acme/api.git"
                    )
                )
            }
        return slack_app(*arguments, repos=repos, **options)

    return build


@pytest.fixture
def workflow_app(web_app):
    """Web app wired with chat workspaces and a workflow catalog."""
    from engine.domain import AgentId, AgentProfile
    from engine.runtime import WorkflowCatalog
    from web_fakes import ConversationWorkspaces

    coder = AgentId("coder")
    profiles = {
        coder: AgentProfile(
            agent_id=coder, instructions="Be terse.", description="Reads code."
        )
    }

    def build(
        store,
        runner,
        workspaces=None,
        communications=None,
        runners=None,
        workflow_catalog=None,
        workspace_repository=None,
        **options,
    ):
        """Wire the app the way the composition root does."""
        return web_app(
            state_store=store,
            runner=runner,
            runners=dict(runners or {"test": runner}),
            profiles=profiles,
            workspaces=workspaces or ConversationWorkspaces(),
            communications=communications,
            workspace_repository=workspace_repository,
            workflow_catalog=(
                workflow_catalog
                if workflow_catalog is not None
                else WorkflowCatalog.from_graphs(())
            ),
            **options,
        )

    return build


@pytest.fixture
def github_auth_app(web_app, sqlite_store, tmp_path):
    """App configured for GitHub OAuth and CSRF endpoint tests."""
    from engine.apps.web.github_auth import GitHubCredentialStore
    from engine.apps.web.source_control import SourceControlPreferences

    def build(client_id="test-client-id", login_config=None, credential_store=None):
        return web_app(
            runners={"default": object()},
            state_store=sqlite_store(),
            credential_store=credential_store or GitHubCredentialStore(),
            github_client_id=client_id,
            github_login_config=login_config,
            source_control_preferences=SourceControlPreferences(
                tmp_path / "settings.json"
            ),
        )

    return build
