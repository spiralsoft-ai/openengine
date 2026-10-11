"""Web control interface entrypoint.

The Python process serves both the chat API and the built assistant-ui client.
``--check`` retains the cheap composition smoke test used in CI.

Composition is reachable twice: ``main`` runs it once and serves the result,
and ``build_app`` is the import string the development server's reloader names,
which constructs the same application again in every fresh child process.
"""

import argparse
import asyncio
import ipaddress
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import uvicorn
from dotenv import dotenv_values
from starlette.applications import Starlette

from engine.apps.web.api import create_app
from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_graph_runtime,
    build_graph_service,
    build_read_only_runners,
    build_runners,
    build_session,
    claude_session_config_for,
)
from engine.apps.web.github_auth import GitHubCredentialStore
from engine.apps.web.repositories import ensure_repository_checkouts
from engine.apps.web.github_login import GitHubLoginConfig, valid_service_token
from engine.apps.web.github_webhook import GitHubWebhookConfig, github_webhook_config
from engine.adapters.communications.slack import SlackCredentialStore
from engine.apps.web.source_control import SourceControlPreferences, gh_cli_status
from engine.runtime import (
    EngineConfigError,
    LoadedEngineConfig,
    WorkflowCatalog,
    describe_loaded_config,
    load_engine_config,
    load_workflow_catalog,
    WorkflowLoadError,
)
from engine.runtime.repositories import RepositoryRegistry

#: Vite's production output, served by the same process as the API.
STATIC_DIRECTORY = Path(__file__).resolve().parent / "static"
if not STATIC_DIRECTORY.is_dir():
    STATIC_DIRECTORY = Path(__file__).resolve().parents[4] / "dist"


def report_wiring(settings: Settings) -> None:
    """Print the composed capability graph, as the other two roots do."""
    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    read_only_runners = build_read_only_runners(settings)
    session = build_session(capabilities, runners, read_only_runners=read_only_runners)
    print(
        describe_loaded_config(
            LoadedEngineConfig(config=settings.engine_config, path=settings.config_path)
        )
    )
    print(f"engine-web -- http://{settings.host}:{settings.port}, capabilities wired:")
    for field in type(capabilities).__dataclass_fields__:
        print(f"  {field}: {type(getattr(capabilities, field)).__name__}")
    cli = gh_cli_status()
    print(
        "  source_control GitHub identity: gh auth; "
        f"authenticated={cli.authenticated} account={cli.account or 'unknown'}"
        + ("" if cli.authenticated else f" ({cli.message})")
    )
    print(f"agents: {', '.join(sorted(session.profiles))}")
    print(f"runners: {', '.join(f'{n} ({type(r).__name__})' for n, r in runners.items())}")
    print(
        "read-only runners (read-only agents): "
        + ", ".join(
            f"{name} ({type(runner).__name__})"
            for name, runner in read_only_runners.items()
        )
    )
    read_only_agents = sorted(
        agent_id for agent_id, profile in session.profiles.items() if profile.read_only
    )
    print(f"read-only agents: {', '.join(read_only_agents) or 'none'}")
    webhook = settings.github_webhook
    print(
        "github webhooks: "
        + (
            "not configured"
            if webhook is None
            else f"{', '.join(webhook.webhook_repositories) or 'no repository named'}, "
            + ("secret saved" if webhook.current_secret() else "secret missing")
        )
    )
    if webhook is not None:
        async def check_repository_access() -> None:
            for repository in webhook.webhook_repositories:
                url = f"https://github.com/{repository}"
                try:
                    async with asyncio.timeout(5):
                        login = await capabilities.source_control.authenticated_login(url)
                        writable = bool(login) and await capabilities.source_control.can_write_repository(
                            f"{url}/pull/1", login,
                        )
                    status = "write access" if writable else "missing write access"
                except Exception:  # noqa: BLE001 -- #779: startup access probe reports failed status
                    status = "access check failed; verify the posting account's repository access"
                print(f"  github {repository}: {status}")
        asyncio.run(check_repository_access())
    print(f"assistant-ui chat is live; conversations are stored in {settings.sqlite_path}.")


def _port(loaded: LoadedEngineConfig) -> int:
    configured = os.environ.get("ENGINE_PORT")
    if configured is None:
        return loaded.config.server.port
    try:
        port = int(configured)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise EngineConfigError("ENGINE_PORT must be an integer from 0 to 65535")
    return port


def _state_paths(loaded: LoadedEngineConfig) -> tuple[str, str]:
    """The conversation database and graph state folder, as absolute paths.

    The state directory resolves against the configuration file's directory,
    like every other path in it, and both locations resolve inside that.
    """

    state = loaded.config.state
    base = loaded.path.parent if loaded.path else Path.cwd()
    directory = base / os.environ.get("ENGINE_STATE_DIRECTORY", state.directory)
    sqlite_path = directory / os.environ.get("ENGINE_SQLITE_PATH", state.sqlite_path)
    graph_state = directory / os.environ.get(
        "ENGINE_GRAPH_STATE_DIRECTORY", state.graph_state_directory
    )
    return str(sqlite_path), str(graph_state)


def _settings(loaded: LoadedEngineConfig) -> Settings:
    """Apply deployment overrides to the immutable TOML configuration."""

    sqlite_path, graph_state_directory = _state_paths(loaded)
    return Settings(
        engine_config=loaded.config,
        config_path=loaded.path,
        host=os.environ.get("ENGINE_HOST", loaded.config.server.host),
        port=_port(loaded),
        sqlite_path=sqlite_path,
        graph_state_directory=graph_state_directory,
        github_client_id=os.environ.get(
            "GITHUB_CLIENT_ID", loaded.config.github_client_id
        ),
        github_token=os.environ.get("GITHUB_TOKEN", loaded.config.github_token),
        github_webhook=github_webhook_config(loaded),
        source_control_preferences=SourceControlPreferences(),
    )


def _webhook_secret_reader(webhook: GitHubWebhookConfig | None) -> Callable[[], str]:
    """How the route reads the webhook secret, rather than the secret itself.

    A reader instead of a string so that rotating the secret on disk takes
    effect on the next delivery: the route asks per delivery, and the process
    outlives any one value of it.
    """

    if webhook is None:
        return lambda: ""
    return webhook.current_secret


def _github_login_config(loaded: LoadedEngineConfig, registry: RepositoryRegistry | None = None) -> GitHubLoginConfig | None:
    secret_file = (loaded.path.parent if loaded.path else Path.cwd()) / ".env"
    values = dotenv_values(secret_file, interpolate=False)
    client_id = os.environ.get(
        "ENGINE_GITHUB_LOGIN_CLIENT_ID", loaded.config.github_login_client_id
    )
    redirect_uri = os.environ.get(
        "ENGINE_GITHUB_LOGIN_REDIRECT_URI", loaded.config.github_login_redirect_uri
    )
    secret = os.environ.get(
        "ENGINE_GITHUB_LOGIN_CLIENT_SECRET",
        values.get("ENGINE_GITHUB_LOGIN_CLIENT_SECRET") or "",
    )
    if not any((client_id, redirect_uri, secret)):
        return None
    try:
        config = GitHubLoginConfig(client_id, secret, redirect_uri, secret_file)
    except ValueError as error:
        raise EngineConfigError(str(error)) from error
    if not (
        loaded.config.github.webhook_repositories
        or (registry.snapshot.login_repositories if registry is not None
            else _login_repositories(loaded, _repository_projects(loaded)))
        or loaded.config.access.operators
    ):
        # Sessions go only to operators and accounts that can write to one of
        # these repositories, so without any every login would be refused as
        # if the user lacked access.
        raise EngineConfigError(
            "GitHub login requires [github] repository, a GitHub checkout in [repos], "
            "or [access] operators to decide who may sign in"
        )
    return config


def _repository_projects(loaded: LoadedEngineConfig) -> dict[str, str]:
    """The GitHub repository behind each `[repos]` checkout, keyed by name.

    Read from each checkout's `origin` remote, because `[repos]` names a local
    path. A checkout on another forge, or one whose remote cannot be read, is
    left out: its permissions are not something GitHub can answer. The server's
    own directory is `.`, what a run gets when no `[repos]` entry is named.
    """
    return dict(RepositoryRegistry(
        loaded.config.repos, host_aliases=loaded.config.github.host_aliases
    ).snapshot.projects)


def _login_repositories(loaded: LoadedEngineConfig, projects: Mapping[str, str]) -> tuple[str, ...]:
    """The GitHub repositories whose writers may sign in: those behind `[repos]`."""
    return tuple(dict.fromkeys(
        projects[name] for name in loaded.config.repos if name in projects
    ))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _require_login_off_loopback(
    settings: Settings, github_login_config: GitHubLoginConfig | None
) -> None:
    """Refuse to serve an unauthenticated interface beyond this machine.

    Without GitHub login the session middleware admits every request, and the
    service token alone does not switch it on, so binding anywhere but loopback
    would let anyone who reaches the port run agents and change credentials.
    """

    if github_login_config is None and not _is_loopback(settings.host):
        raise EngineConfigError(
            f"server host {settings.host!r} is not loopback, and GitHub login is "
            "not configured; configure GitHub login or bind to 127.0.0.1"
        )


def _service_token_reader(loaded: LoadedEngineConfig) -> Callable[[], str]:
    """How the login middleware reads `ENGINE_SERVICE_TOKEN`, per request.

    Read like the login client secret: the process environment first, then the
    server-local `.env` beside `engine.toml`, never TOML. A reader so rotating
    the file takes effect without a restart; a value set now but invalid fails
    startup rather than silently admitting nothing.
    """

    secret_file = (loaded.path.parent if loaded.path else Path.cwd()) / ".env"

    def read() -> str:
        if "ENGINE_SERVICE_TOKEN" in os.environ:
            return os.environ["ENGINE_SERVICE_TOKEN"]
        values = dotenv_values(secret_file, interpolate=False)
        return values.get("ENGINE_SERVICE_TOKEN") or ""

    token = read()
    if token and not valid_service_token(token):
        raise EngineConfigError(
            "ENGINE_SERVICE_TOKEN must contain at least 32 non-whitespace characters"
        )
    return read


def _github_client_id_source() -> str:
    return "environment" if "GITHUB_CLIENT_ID" in os.environ else "configuration"


def read_configuration(
    config_path: str | os.PathLike[str] | None = None,
) -> tuple[LoadedEngineConfig, WorkflowCatalog | None]:
    """The two files this process reads once, at startup, and never again.

    Together because they fail together -- neither is worth starting without --
    and because "what a restart is for" has to be one list: the development
    server watches exactly what this function reads.
    """
    loaded = load_engine_config(config_path)
    # Workflow providers may omit env entirely; all Claude children inherit this.
    if (directory := loaded.claude_config_dir) is not None:
        os.environ["CLAUDE_CONFIG_DIR"] = str(directory)
    settings = _settings(loaded)
    catalog = (
        load_workflow_catalog(
            loaded.workflows_directory,
            session_config=claude_session_config_for(settings),
        )
        if loaded.workflows_directory is not None
        else None
    )
    ensure_repository_checkouts(loaded.config.repos)
    return loaded, catalog


def compose_app(
    loaded: LoadedEngineConfig, workflow_catalog: WorkflowCatalog | None
) -> Starlette:
    """Wire the capability graph and hand it to the HTTP surface."""
    settings = _settings(loaded)
    registry = RepositoryRegistry(
        loaded.config.repos, loaded.config.repo_modes, loaded.config.trusted_repos,
        host_aliases=loaded.config.github.host_aliases,
    )
    github_login_config = _github_login_config(loaded, registry)
    _require_login_off_loopback(settings, github_login_config)
    # One cached store for Settings and agent actions alike, so the token
    # `engine connect github` saved is used without reading the keychain again.
    credential_store = GitHubCredentialStore(cached=True)
    slack_credential_store = SlackCredentialStore()
    capabilities = build_capabilities(
        settings,
        slack_credential_store=slack_credential_store,
        github_credential_store=credential_store,
    )
    runners = build_runners(settings)
    read_only_runners = build_read_only_runners(settings)
    session = build_session(capabilities, runners, read_only_runners=read_only_runners)
    # The runtime for the workflows in the configured directory. It
    # is `None` when that directory holds no graphs, and then the interface
    # offers none of them.
    graph_runtime = build_graph_runtime(
        settings,
        workflow_catalog.graphs if workflow_catalog is not None else (),
        source_control=capabilities.source_control,
    )
    return create_app(
        session,
        runners,
        STATIC_DIRECTORY,
        workflow_catalog=workflow_catalog,
        graph_runtime=graph_runtime,
        approval_policy=loaded.config.approvals,
        credential_store=credential_store,
        github_client_id=settings.github_client_id,
        github_client_id_source=_github_client_id_source(),
        github_login_config=github_login_config,
        service_token=_service_token_reader(loaded),
        source_control_preferences=settings.source_control_preferences,
        slack_credential_store=slack_credential_store,
        github_webhook_secret=_webhook_secret_reader(settings.github_webhook),
        github_repository=settings.github_webhook.repository if settings.github_webhook else "",
        github_repositories=settings.github_webhook.webhook_repositories if settings.github_webhook else (),
        communications_channel=loaded.config.communications.channel,
        public_url=loaded.config.public_url,
        work_orders=loaded.config.work_orders,
        repos=registry,
        login_operators=loaded.config.access.operators,
        graph_service=build_graph_service(
            settings,
            default_repository=loaded.config.work_orders.repository,
            repositories=registry,
        ),
    )


#: Timestamped, and named by logger, because the log is read after the fact:
#: "what happened to that webhook an hour ago" is answered by the time and the
#: module that said it, and a line without either is one nobody can place.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging() -> None:
    """Show Engine's own INFO lines; keep libraries at WARNING.

    Without a handler Python prints only warnings, so every decision Engine
    logs at INFO -- a webhook ignored and why, a merge accepted -- was written
    to nowhere. Libraries stay at WARNING because some log every HTTP request
    at INFO, which would bury the lines this is for.
    """
    logging.basicConfig(level=logging.WARNING, format=LOG_FORMAT)
    logging.getLogger("engine").setLevel(logging.INFO)


def build_app(config_path: str | os.PathLike[str] | None = None) -> Starlette:
    """Read the configuration and compose the application from it.

    The reloader names this as an import string and calls it with no arguments
    in each child process it starts, so the configuration file is selected by
    ``ENGINE_CONFIG`` there rather than by a command line the child never saw.
    """
    configure_logging()
    return compose_app(*read_configuration(config_path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the OpenEngine web interface.")
    parser.add_argument("--config", help="read Engine settings from this TOML file")
    parser.add_argument("--check", action="store_true", help="report wiring and exit")
    args = parser.parse_args(argv)
    try:
        loaded, workflow_catalog = read_configuration(args.config)
        settings = _settings(loaded)
        if args.check:
            _require_login_off_loopback(settings, _github_login_config(loaded))
            _service_token_reader(loaded)
            report_wiring(settings)
            return 0
        configure_logging()
        app = compose_app(loaded, workflow_catalog)
    except (EngineConfigError, WorkflowLoadError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    print(describe_loaded_config(loaded))
    uvicorn.run(app, host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
