"""Engine's TOML configuration is singular and typed."""

from pathlib import Path

import pytest

import engine.apps.control_server.__main__ as control_server_main
import engine.apps.web.__main__ as web_main
import engine.apps.worker.__main__ as worker_main
from engine.runtime import (
    LoadedEngineConfig,
    ApprovalCapability,
    EngineConfigError,
    ResponseStyle,
    describe_loaded_config,
    load_engine_config,
    parse_engine_config,
)


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


def test_defaults_allow_reads_without_selecting_a_file(tmp_path: Path) -> None:
    loaded = load_engine_config(environ={}, cwd=tmp_path)

    assert loaded.path is None
    assert loaded.config.attribution is True
    assert loaded.config.default_branch == "main"
    assert loaded.config.public_url == ""
    assert loaded.config.github.repository == ""
    assert loaded.config.communications.provider == "slack"
    assert loaded.config.communications.channel == ""
    assert loaded.config.work_orders.repository == ""
    assert loaded.config.work_orders.workflow == ""
    assert loaded.config.work_orders.runner == ""
    assert loaded.config.orchestrator.host == "127.0.0.1:7233"
    assert loaded.config.orchestrator.database == ".engine/temporal.sqlite3"
    assert loaded.config.orchestrator.health_check_interval == 5.0
    assert loaded.config.claude.output_style is None
    assert loaded.config.approvals.allow == (ApprovalCapability.READ,)
    assert loaded.config.approvals.auto_approve is False
    assert loaded.config.approvals.bash.allow == ()


def test_loads_provider_neutral_approval_configuration(tmp_path: Path) -> None:
    path = tmp_path / "permissions.toml"
    path.write_text(
        """
[approvals]
auto_approve = true
allow = ["read", "edit"]

[approvals.bash]
allow = ["uv run pytest **", "git status **"]
ask = ["git push **"]
deny = ["sudo **"]
""".strip()
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.path == path.resolve()
    assert loaded.config.attribution is True
    assert loaded.config.approvals.auto_approve is True
    assert loaded.config.approvals.allow == (
        ApprovalCapability.READ,
        ApprovalCapability.EDIT,
    )
    assert loaded.config.approvals.bash.allow == (
        "uv run pytest **",
        "git status **",
    )
    assert loaded.config.approvals.bash.ask == ("git push **",)
    assert loaded.config.approvals.bash.deny == ("sudo **",)


def test_loads_a_configured_default_branch(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('default_branch = "master"\n')

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.default_branch == "master"


def test_loads_communications_notification_configuration(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'public_url = "https://engine.example/"\n'
        '[communications]\nprovider = "slack"\nchannel = "C12345678"\n'
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.public_url == "https://engine.example"
    assert loaded.config.communications.channel == "C12345678"


def test_rejects_legacy_slack_channel_configuration(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[slack]\nchannel_id = "C12345678"\n')

    with pytest.raises(EngineConfigError, match="unknown key in configuration: slack"):
        load_engine_config(path, environ={}, cwd=tmp_path)


def test_loads_communications_provider(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[communications]\nprovider = "buzz"\nchannel = "engineering"\n')

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.communications.provider == "buzz"
    assert loaded.config.communications.channel == "engineering"


def test_loads_what_a_mention_should_start(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        "[work_orders]\n"
        'repository = "acme/api"\n'
        'workflow = "implementation-review-v1"\n'
        'runner = "claude"\n'
        'slack_operators = ["U-operator", "U-release"]\n'
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.work_orders.repository == "acme/api"
    assert loaded.config.work_orders.workflow == "implementation-review-v1"
    assert loaded.config.work_orders.runner == "claude"
    assert loaded.config.work_orders.slack_operators == ("U-operator", "U-release")


def test_rejects_an_unknown_work_order_key(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[work_orders]\nrepo = "acme/api"\n')

    with pytest.raises(EngineConfigError, match="unknown key in work_orders: repo"):
        load_engine_config(path, environ={}, cwd=tmp_path)


def test_loads_the_repository_webhook_deliveries_are_accepted_from(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[github]\nrepository = "owner/name"\n[repos]\n"owner/name" = "."\n')

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.github.repository == "owner/name"


def test_loads_github_deployment_credentials(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'github_client_id = "client-from-toml"\n'
        'github_token = "token-from-toml"\n'
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.github_client_id == "client-from-toml"
    assert loaded.config.github_token == "token-from-toml"


def test_selection_is_explicit_then_environment_then_working_directory(
    tmp_path: Path,
) -> None:
    implicit = tmp_path / "engine.toml"
    environment = tmp_path / "environment.toml"
    explicit = tmp_path / "explicit.toml"
    implicit.write_text('[approvals]\nallow = ["read"]\n')
    environment.write_text('[approvals]\nallow = ["edit"]\n')
    explicit.write_text('[approvals]\nallow = ["web"]\n')

    from_default = load_engine_config(environ={}, cwd=tmp_path)
    local = tmp_path / ".engine/config.toml"
    local.parent.mkdir()
    local.write_text('default_branch = "local"\n')
    from_local = load_engine_config(environ={}, cwd=tmp_path)
    assert from_local.path == local.resolve()
    assert from_local.config.default_branch == "local"
    assert from_local.config.approvals.allow == (ApprovalCapability.READ,)

    from_environment = load_engine_config(
        environ={"ENGINE_CONFIG": environment.name}, cwd=tmp_path
    )
    from_explicit = load_engine_config(
        explicit.name,
        environ={"ENGINE_CONFIG": environment.name},
        cwd=tmp_path,
    )

    assert from_default.path == implicit.resolve()
    assert from_default.config.approvals.allow == (ApprovalCapability.READ,)
    assert from_environment.path == environment.resolve()
    assert from_environment.config.approvals.allow == (ApprovalCapability.EDIT,)
    assert from_explicit.path == explicit.resolve()
    assert from_explicit.config.approvals.allow == (ApprovalCapability.WEB,)


@pytest.mark.parametrize(
    "document,message",
    [
        ({"approval": {}}, "unknown key in configuration: approval"),
        ({"repo_modes": {"api": "disconnected"}}, "repo_modes.api names no repository"),
        (
            {"repos": {"api": "/api"}, "repo_modes": {"api": "offline"}},
            "repo_modes.api must be one of: connected, disconnected",
        ),
        ({"trusted_repos": {"api": True}}, "trusted_repos.api names no repository"),
        (
            {"repos": {"api": "/api"}, "trusted_repos": {"api": "yes"}},
            "trusted_repos.api must be a boolean",
        ),
        ({"show_projects": False}, "unknown key in configuration: show_projects"),
        ({"approvals": {"automatic": True}}, "unknown key in approvals: automatic"),
        ({"approvals": {"auto_approve": "yes"}}, "must be a boolean"),
        ({"attribution": "no"}, "attribution must be a boolean"),
        ({"default_branch": ""}, "default_branch must be a non-empty string"),
        ({"default_branch": 1}, "default_branch must be a non-empty string"),
        ({"github_client_id": 1}, "github_client_id must be a string"),
        ({"github_token": " "}, "github_token must not be blank"),
        ({"github": {"repo": "owner/name"}}, "unknown key in github: repo"),
        ({"github": {"repository": "name"}}, 'github.repository must be "owner/name"'),
        (
            {"github": {"repository": "owner/name/extra"}},
            'github.repository must be "owner/name"',
        ),
        (
            {"github": {"repository": "owner /name"}},
            'github.repository must be "owner/name"',
        ),
        ({"github": {"repository": " "}}, "github.repository must not be blank"),
        ({"orchestrator": {"host": ""}}, "orchestrator.host must not be blank"),
        (
            {"orchestrator": {"health_check_interval": 0}},
            "orchestrator.health_check_interval must be a positive number",
        ),
        ({"output_style": "concise"}, "unknown key in configuration: output_style"),
        ({"claude": {"style": "concise"}}, "unknown key in claude: style"),
        ({"claude": {"output_style": True}}, "claude.output_style must be a string"),
        (
            {"claude": {"output_style": "Concise"}},
            "claude.output_style is unknown: 'Concise'",
        ),
        (
            {"claude": {"output_style": "terse"}},
            "expected one of: concise, explanatory, learning",
        ),
        ({"approvals": {"allow": "read"}}, "must be an array of strings"),
        ({"approvals": {"allow": ["Read"]}}, "unknown capability 'Read'"),
        ({"approvals": {"allow": ["read", "read"]}}, "must not contain duplicates"),
        (
            {"approvals": {"bash": {"allow": [" "]}}},
            "must not contain empty patterns",
        ),
        (
            {"approvals": {"bash": {"allowed": ["pytest **"]}}},
            "unknown key in approvals.bash: allowed",
        ),
    ],
)
def test_rejects_mistyped_or_unknown_settings(
    document: dict[str, object], message: str
) -> None:
    with pytest.raises(EngineConfigError, match=message):
        parse_engine_config(document)


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EngineConfigError, match="configuration file does not exist"):
        load_engine_config("missing.toml", environ={}, cwd=tmp_path)


def test_attribution_can_be_disabled(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text("attribution = false\n")

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.attribution is False


def test_output_style_is_engine_vocabulary_scoped_to_the_runner_that_honours_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[claude]\noutput_style = "concise"\n')

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.claude.output_style is ResponseStyle.CONCISE


def test_invalid_toml_names_its_source(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("[approvals\n")

    with pytest.raises(EngineConfigError, match=f"invalid TOML in {path}"):
        load_engine_config(path, environ={}, cwd=tmp_path)


def test_startup_description_reports_the_policy_being_enforced(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        '[approvals]\nauto_approve = true\nallow = ["read", "mcp"]\n'
        '[approvals.bash]\nallow = ["pytest **"]\nask = ["git push **"]\n'
    )

    description = describe_loaded_config(load_engine_config(path, environ={}))

    assert str(path) in description
    assert "auto_approve=on" in description
    assert "allow=read, mcp" in description
    assert "bash_rules=2" in description
    assert "approvals enforced" in description
    assert "claude.output_style=provider default" in description
    assert "default_branch=main" in description


def test_web_entrypoint_puts_explicit_config_in_composition_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "permissions.toml"
    path.write_text('[approvals]\nauto_approve = true\nallow = ["read", "bash"]\n')
    seen = []
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == 0

    assert len(seen) == 1
    assert seen[0].config_path == path.resolve()
    assert seen[0].engine_config.approvals.auto_approve is True
    assert seen[0].engine_config.approvals.allow == (
        ApprovalCapability.READ,
        ApprovalCapability.BASH,
    )


def test_web_entrypoint_uses_toml_github_credentials_when_environment_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'github_client_id = "client-from-toml"\n'
        'github_token = "token-from-toml"\n'
    )
    seen = []
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == 0

    assert seen[0].github_client_id == "client-from-toml"
    assert seen[0].github_token == "token-from-toml"


def test_web_entrypoint_prefers_environment_github_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'github_client_id = "client-from-toml"\n'
        'github_token = "token-from-toml"\n'
    )
    seen = []
    monkeypatch.setenv("GITHUB_CLIENT_ID", "client-from-environment")
    monkeypatch.setenv("GITHUB_TOKEN", "token-from-environment")
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == 0

    assert seen[0].github_client_id == "client-from-environment"
    assert seen[0].github_token == "token-from-environment"


@pytest.mark.parametrize(
    "entrypoint,arguments",
    [
        (web_main.main, ["--check"]),
        (worker_main.main, []),
        (control_server_main.main, []),
    ],
)
def test_every_entrypoint_rejects_an_explicit_missing_config(
    entrypoint, arguments: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.toml"

    assert entrypoint([*arguments, "--config", str(missing)]) == 2

    assert "configuration file does not exist" in capsys.readouterr().err


def test_github_login_toml_and_secret_rotation(tmp_path, monkeypatch):
    for name in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI"):
        monkeypatch.delenv(f"ENGINE_GITHUB_LOGIN_{name}", raising=False)
    (tmp_path / "engine.toml").write_text(
        'github_login_client_id = "login-client"\n'
        'github_login_redirect_uri = "https://engine.test/api/auth/github/callback"\n'
        '[github]\nrepository = "acme/api"\n[repos]\n"acme/api" = "."\n'
    )
    secret_file = tmp_path / ".env"
    secret_file.write_text('ENGINE_GITHUB_LOGIN_CLIENT_SECRET="first-${LITERAL}"\n')
    loaded = load_engine_config(environ={}, cwd=tmp_path)
    config = web_main._github_login_config(loaded)
    assert config.client_id == "login-client"
    assert config.current_secret() == "first-${LITERAL}"
    secret_file.write_text('ENGINE_GITHUB_LOGIN_CLIENT_SECRET=rotated\n')
    assert config.current_secret() == "rotated"
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "environment-secret")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "environment-client")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_REDIRECT_URI", "https://override.test/api/auth/github/callback")
    override = web_main._github_login_config(loaded)
    assert override.client_id == "environment-client"
    assert override.redirect_uri == "https://override.test/api/auth/github/callback"
    assert override.current_secret() == "environment-secret"
    monkeypatch.delenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET")
    secret_file.unlink()
    with pytest.raises(ValueError, match="secret is not configured"):
        config.current_secret()


def test_github_login_disabled_and_partial_configuration(tmp_path, monkeypatch):
    for name in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI"):
        monkeypatch.delenv(f"ENGINE_GITHUB_LOGIN_{name}", raising=False)
    monkeypatch.chdir(tmp_path)
    loaded = load_engine_config(environ={}, cwd=tmp_path)
    assert web_main._github_login_config(loaded) is None
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "partial")
    with pytest.raises(EngineConfigError, match="requires credentials"):
        web_main._github_login_config(loaded)


@pytest.mark.parametrize("args", [[], ["--check"]])
@pytest.mark.parametrize("redirect_uri", ["", "http://public.test/api/auth/github/callback"])
def test_web_reports_invalid_login_configuration(tmp_path, monkeypatch, capsys, args, redirect_uri):
    path = tmp_path / "engine.toml"
    path.write_text("")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "client")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "private-secret")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_REDIRECT_URI", redirect_uri)

    def unexpected_start(*args, **kwargs):
        pytest.fail("Invalid login configuration must stop startup")

    monkeypatch.setattr(web_main.uvicorn, "run", unexpected_start)
    monkeypatch.setattr(web_main, "report_wiring", unexpected_start)
    assert web_main.main(["--config", str(path), *args]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("configuration error: GitHub login requires credentials")
    assert "Traceback" not in captured.err
    assert "private-secret" not in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("args", [[], ["--check"]])
def test_web_refuses_to_start_login_without_a_repository(tmp_path, monkeypatch, capsys, args):
    """Login admits only accounts that can write to `[github] repository`, so
    without one every login would be refused: that is a setup error, reported
    at startup rather than to each user as missing access."""
    path = tmp_path / "engine.toml"
    path.write_text("")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "client")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "private-secret")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_REDIRECT_URI", "https://engine.test/api/auth/github/callback")

    def unexpected_start(*args, **kwargs):
        pytest.fail("Login without a repository must stop startup")

    monkeypatch.setattr(web_main.uvicorn, "run", unexpected_start)
    monkeypatch.setattr(web_main, "report_wiring", unexpected_start)
    assert web_main.main(["--config", str(path), *args]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("configuration error: GitHub login requires [github] repository")
    assert "Traceback" not in captured.err
    assert "private-secret" not in captured.err


@pytest.mark.parametrize("key", ["github_login_client_id", "github_login_redirect_uri"])
def test_github_login_config_requires_strings(key):
    with pytest.raises(EngineConfigError, match=f"{key} must be a string"):
        parse_engine_config({key: 42})


def test_github_login_secret_not_accepted_in_toml():
    with pytest.raises(EngineConfigError):
        parse_engine_config({"github_login_client_secret": "secret"})


def test_github_host_aliases_configuration():
    assert parse_engine_config({}).github.host_aliases == {}
    assert parse_engine_config({
        "github": {"host_aliases": {"Alias.Example": "Forge.Example"}}
    }).github.host_aliases == {"alias.example": "forge.example"}


@pytest.mark.parametrize("aliases", [
    "forge.example", ["forge.example"], {"alias.example": 1},
    {"": "forge.example"}, {"alias.example": " "},
])
def test_github_host_aliases_require_nonblank_string_mapping(aliases):
    with pytest.raises(EngineConfigError, match="github.host_aliases"):
        parse_engine_config({"github": {"host_aliases": aliases}})


def test_github_unbound_hosts_are_not_accepted():
    with pytest.raises(EngineConfigError, match="hosts"):
        parse_engine_config({"github": {"hosts": ["forge.example"]}})


def test_repository_choices_load_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[repos]\n"OpenEngine/OpenEngine" = "~/code/OpenEngine"\nn8n = "~/code/n8n"\n')
    assert load_engine_config(path, environ={}).config.repos == {
        "OpenEngine/OpenEngine": "~/code/OpenEngine", "n8n": "~/code/n8n",
    }
    assert parse_engine_config({}).repos == {}


@pytest.mark.parametrize("repos", [[], {"repo": ""}, {"repo": 123}, {" ": "/tmp/repo"}])
def test_invalid_repository_choices_are_rejected(repos) -> None:
    with pytest.raises(EngineConfigError, match="repos"):
        parse_engine_config({"repos": repos})


def test_access_operators_are_github_user_ids() -> None:
    assert parse_engine_config({"access": {"operators": [583231, 42]}}).access.operators == (583231, 42)
    assert parse_engine_config({}).access.operators == ()


@pytest.mark.parametrize("operators", [["octocat"], [0], [True], [1, 1], "42"])
def test_access_operators_reject_logins_and_bad_ids(operators) -> None:
    with pytest.raises(EngineConfigError, match="access.operators"):
        parse_engine_config({"access": {"operators": operators}})


def test_repository_projects_are_read_from_the_checkouts_remotes(
    tmp_path, *, git_repo
) -> None:
    """`[repos]` names local paths; login asks GitHub about the repository
    each one pushes to, and skips what GitHub cannot answer for."""
    import subprocess

    remotes = {
        "api": "git@github.com:Acme/API.git",
        "web": "https://github.com/acme/web.git",
        "same": "https://github.com/acme/web",
        "gitlab": "https://gitlab.example/acme/tools.git",
        "enterprise": "https://github-web.example/acme/core.git",
    }
    repos = {}
    for name, remote in remotes.items():
        git_repo(tmp_path / name)
        subprocess.run(["git", "-C", str(tmp_path / name), "remote", "add", "origin", remote], check=True)
        repos[name] = str(tmp_path / name)
    repos["missing"] = str(tmp_path / "missing")
    loaded = LoadedEngineConfig(config=parse_engine_config({
        "repos": repos,
        "github": {"host_aliases": {"github-web.example": "github-api.example"}},
    }))

    projects = web_main._repository_projects(loaded)
    assert {name: projects[name] for name in repos if name in projects} == {
        "api": "acme/api", "web": "acme/web", "same": "acme/web",
        "enterprise": "github-web.example/acme/core",
    }
    assert web_main._login_repositories(loaded, projects) == (
        "acme/api", "acme/web", "github-web.example/acme/core",
    )


def test_the_servers_own_checkout_is_named_dot(
    tmp_path, monkeypatch, *, git_repo
) -> None:
    """A run in `.` is in the server's own checkout, which does not by itself
    let anyone sign in."""
    import subprocess

    git_repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "https://github.com/acme/server.git"], check=True)
    monkeypatch.chdir(tmp_path)
    loaded = LoadedEngineConfig(config=parse_engine_config({}))

    projects = web_main._repository_projects(loaded)
    assert projects == {".": "acme/server"}
    assert web_main._login_repositories(loaded, projects) == ()


def test_web_starts_login_with_only_operators(tmp_path, monkeypatch):
    path = tmp_path / "engine.toml"
    path.write_text("[access]\noperators = [42]\n")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "client")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "private-secret")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_REDIRECT_URI", "https://engine.test/api/auth/github/callback")

    assert web_main._github_login_config(load_engine_config(path)) is not None


def test_web_bind_address_and_state_paths_default_to_the_source_checkout_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "ENGINE_HOST", "ENGINE_PORT", "ENGINE_STATE_DIRECTORY",
        "ENGINE_SQLITE_PATH", "ENGINE_GRAPH_STATE_DIRECTORY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    settings = web_main._settings(load_engine_config(environ={}, cwd=tmp_path))

    assert (settings.host, settings.port) == ("localhost", 4364)
    assert Path(settings.sqlite_path) == tmp_path / "conversations.sqlite3"
    assert Path(settings.graph_state_directory) == tmp_path / "graph-state"


def test_web_state_paths_resolve_against_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "ENGINE_HOST", "ENGINE_PORT", "ENGINE_STATE_DIRECTORY",
        "ENGINE_SQLITE_PATH", "ENGINE_GRAPH_STATE_DIRECTORY",
    ):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "etc" / "engine.toml"
    config.parent.mkdir()
    config.write_text(
        '[server]\nhost = "127.0.0.1"\nport = 5000\n'
        '[state]\ndirectory = "state"\nsqlite_path = "chat.db"\n'
        'graph_state_directory = "graphs"\n'
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    settings = web_main._settings(load_engine_config(config))

    assert (settings.host, settings.port) == ("127.0.0.1", 5000)
    assert Path(settings.sqlite_path) == config.parent / "state" / "chat.db"
    assert Path(settings.graph_state_directory) == config.parent / "state" / "graphs"

    monkeypatch.setenv("ENGINE_HOST", "0.0.0.0")
    monkeypatch.setenv("ENGINE_PORT", "6000")
    monkeypatch.setenv("ENGINE_STATE_DIRECTORY", str(tmp_path / "var"))
    monkeypatch.setenv("ENGINE_SQLITE_PATH", "other.db")
    settings = web_main._settings(load_engine_config(config))

    assert (settings.host, settings.port) == ("0.0.0.0", 6000)
    assert Path(settings.sqlite_path) == tmp_path / "var" / "other.db"
    assert Path(settings.graph_state_directory) == tmp_path / "var" / "graphs"

    monkeypatch.setenv("ENGINE_PORT", "http")
    with pytest.raises(EngineConfigError, match="ENGINE_PORT"):
        web_main._settings(load_engine_config(config))


@pytest.mark.parametrize(
    ("host", "refused"),
    [
        ("localhost", False),
        ("127.0.0.1", False),
        ("::1", False),
        ("0.0.0.0", True),
        ("::", True),
        ("engine.example.com", True),
    ],
)
def test_web_refuses_a_non_loopback_host_without_github_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, refused: bool
) -> None:
    for name in (
        "ENGINE_PORT", "ENGINE_STATE_DIRECTORY", "ENGINE_SQLITE_PATH",
        "ENGINE_GRAPH_STATE_DIRECTORY", "ENGINE_GITHUB_LOGIN_CLIENT_ID",
        "ENGINE_GITHUB_LOGIN_REDIRECT_URI", "ENGINE_GITHUB_LOGIN_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENGINE_HOST", host)
    # A service token does not switch the session middleware on by itself.
    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "s" * 32)
    config = tmp_path / "engine.toml"
    config.write_text("")
    loaded = load_engine_config(config)
    settings = web_main._settings(loaded)

    if refused:
        with pytest.raises(EngineConfigError, match="GitHub login"):
            web_main._require_login_off_loopback(settings, None)
        assert web_main.main(["--config", str(config), "--check"]) == 2
    else:
        web_main._require_login_off_loopback(settings, None)
    web_main._require_login_off_loopback(settings, object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"server": {"port": "4364"}}, "server.port"),
        ({"server": {"port": 70000}}, "server.port"),
        ({"server": {"host": " "}}, "server.host"),
        ({"server": {"address": "x"}}, "unknown key in server"),
        ({"state": {"directory": ""}}, "state.directory"),
        ({"state": {"path": "x"}}, "unknown key in state"),
    ],
)
def test_server_and_state_settings_are_validated(document, message) -> None:
    with pytest.raises(EngineConfigError, match=message):
        parse_engine_config(document)


def test_the_distributable_default_config_is_loopback_and_machine_neutral() -> None:
    from engine.runtime.config import DEFAULT_CONFIG_TEMPLATE

    loaded = load_engine_config(DEFAULT_CONFIG_TEMPLATE)
    config = loaded.config

    assert config.server.host == "127.0.0.1"
    assert config.public_url == ""
    assert config.repos == {}
    assert config.github.repository == ""
    assert config.communications.channel == ""
    assert config.communications.provider == "slack"
    assert config.workflows.directory == "workflows"
    assert "[orchestrator]" not in DEFAULT_CONFIG_TEMPLATE.read_text()


def test_repo_modes_name_the_mode_of_onboarded_repositories() -> None:
    config = parse_engine_config(
        {"repos": {"api": "/api", "web": "/web"}, "repo_modes": {"api": "disconnected"}}
    )

    assert config.repo_modes == {"api": "disconnected"}


def test_trusted_repos_name_the_repositories_whose_work_orders_are_auto_approved() -> None:
    config = parse_engine_config(
        {"repos": {"api": "/api", "web": "/web"}, "trusted_repos": {"api": True, "web": False}}
    )

    assert config.trusted_repos == frozenset({"api"})


@pytest.mark.parametrize("value", ["account", "~/account"])
def test_claude_config_directory_resolves_at_load(tmp_path, monkeypatch, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / "account"
    directory.mkdir()
    config = tmp_path / "engine.toml"
    config.write_text(f'[claude]\nconfig_dir = "{value}"\n')
    monkeypatch.chdir(tmp_path.parent)

    loaded = load_engine_config(config)

    assert loaded.config.claude.config_dir == value
    assert loaded.claude_config_dir == directory
    assert f"claude.config_dir={directory}" in describe_loaded_config(loaded)


@pytest.mark.parametrize("value", ["missing", "file"])
def test_claude_config_directory_must_exist(tmp_path, value):
    (tmp_path / "file").write_text("")
    config = tmp_path / "engine.toml"
    config.write_text(f'[claude]\nconfig_dir = "{value}"\n')
    with pytest.raises(EngineConfigError, match="claude.config_dir"):
        load_engine_config(config)


@pytest.mark.parametrize("claude", [
    {"config_dir": 1}, {"config_dir": ""}, {"config_dir": " "}, {"unknown": "x"},
])
def test_claude_directory_settings_are_strict(claude):
    with pytest.raises(EngineConfigError):
        parse_engine_config({"claude": claude})


@pytest.mark.parametrize("inherited", [None, "/existing/account"])
def test_web_preserves_claude_environment_without_configuration(tmp_path, monkeypatch, inherited):
    import os

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    if inherited is not None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", inherited)
    config = tmp_path / "engine.toml"
    config.write_text("")
    loaded, _ = web_main.read_configuration(config)
    assert loaded.claude_config_dir is None
    assert os.environ.get("CLAUDE_CONFIG_DIR") == inherited


def test_web_claude_provider_inherits_configured_login(tmp_path, monkeypatch):
    import asyncio
    import sys

    from langgraph_acp import ClaudeACPProvider

    directory = tmp_path / "account"
    directory.mkdir()
    config = tmp_path / "engine.toml"
    config.write_text('[claude]\nconfig_dir = "account"\n')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/other/account")
    # Use the protocol fake, but have it echo the child's inherited account.
    fake = Path(__file__).parents[1] / "langgraph-acp/tests/fake_agent.py"
    wrapper = tmp_path / "echo_account.py"
    wrapper.write_text(
        "import os, runpy\n"
        "os.environ['FAKE_AGENT_RESPONSE'] = os.environ['CLAUDE_CONFIG_DIR']\n"
        f"runpy.run_path({str(fake)!r}, run_name='__main__')\n"
    )
    web_main.read_configuration(config)

    async def prompt():
        client = await ClaudeACPProvider(command=(sys.executable, str(wrapper))).connect()
        try:
            session = await client.new_session(cwd=tmp_path)
            return [event async for event in session.prompt("Which account?")]
        finally:
            await client.close()

    events = asyncio.run(prompt())
    assert any(event.data.get("content") == {"type": "text", "text": str(directory)} for event in events)


@pytest.mark.parametrize("backend", ["process", "smolvm"])
def test_sandbox_configuration(backend):
    assert parse_engine_config({}).sandbox.backend == "process"
    assert parse_engine_config({"sandbox": {"backend": backend}}).sandbox.backend == backend


@pytest.mark.parametrize("sandbox", [{"backend": "docker"}, {"backend": None}, {"unknown": True}, "process"])
def test_invalid_sandbox_configuration(sandbox):
    with pytest.raises(EngineConfigError, match="sandbox"):
        parse_engine_config({"sandbox": sandbox})


@pytest.mark.parametrize("app", [web_main, worker_main, control_server_main])
def test_sandbox_backend_composition(app, tmp_path):
    from dataclasses import replace
    from engine.adapters.sandbox.process import ProcessSandbox

    settings = app.Settings()
    if hasattr(settings, "sqlite_path"):
        settings = replace(settings, sqlite_path=str(tmp_path / "state.sqlite"))
    assert isinstance(app.build_capabilities(settings).sandbox, ProcessSandbox)
    settings = replace(settings, engine_config=parse_engine_config({"sandbox": {"backend": "smolvm"}}))
    with pytest.raises(NotImplementedError, match="smolvm"):
        app.build_capabilities(settings)


def test_github_repositories_combine_with_legacy_repository():
    config = parse_engine_config({
        "github": {"repository": "Acme/API", "repositories": ["acme/api", "other/web"]},
        "repos": {"acme/api": "/api", "Other/Web": "/web"},
    })
    assert config.github.webhook_repositories == ("acme/api", "other/web")


@pytest.mark.parametrize("repositories", ["acme/api", [""], ["invalid"], [12]])
def test_github_repositories_reject_invalid_lists(repositories):
    with pytest.raises(EngineConfigError, match="github.repositories"):
        parse_engine_config({"github": {"repositories": repositories}})


@pytest.mark.parametrize("github", [
    {"repository": "acme/api"}, {"repositories": ["acme/api"]},
])
def test_github_repositories_require_checkout_mappings(github):
    with pytest.raises(EngineConfigError, match="acme/api.*checkout path under"):
        parse_engine_config({"github": github, "repos": {"other/repo": "/other"}})


@pytest.mark.parametrize("xdg", [None, "absolute", "relative"])
def test_machine_configuration_precedes_checkout_without_merging(tmp_path, xdg):
    config_home = tmp_path / "custom" if xdg == "absolute" else Path.home() / ".config"
    machine = config_home / "openengine" / "engine.toml"
    machine.parent.mkdir(parents=True)
    machine.write_text(
        '[github]\nrepositories = ["other/web"]\n'
        '[repos]\n"other/web" = "/srv/web"\n'
    )
    (tmp_path / "engine.toml").write_text('[repos]\n"checkout/repo" = "/checkout"\n')
    local = tmp_path / ".engine/config.toml"
    local.parent.mkdir()
    local.write_text('[repos]\n"local/repo" = "/local"\n')
    environment = {} if xdg is None else {
        "XDG_CONFIG_HOME": str(config_home) if xdg == "absolute" else "relative"
    }
    loaded = load_engine_config(environ=environment, cwd=tmp_path)
    assert loaded.path == machine.resolve()
    assert loaded.config.repos == {"other/web": "/srv/web"}
    assert loaded.config.github.webhook_repositories == ("other/web",)

    override = tmp_path / "override.toml"
    override.write_text('[repos]\n"explicit/repo" = "/explicit"\n')
    environment["ENGINE_CONFIG"] = str(override)
    assert load_engine_config(environ=environment, cwd=tmp_path).path == override
    assert load_engine_config(machine, environ=environment, cwd=tmp_path).path == machine
    assert load_engine_config(local, environ=environment, cwd=tmp_path).path == local


def test_invalid_machine_config_does_not_fall_back_to_checkout(tmp_path):
    machine = Path.home() / ".config" / "openengine" / "engine.toml"
    machine.parent.mkdir(parents=True)
    machine.write_text('[github]\nrepositories = ["missing/checkout"]\n')
    (tmp_path / "engine.toml").write_text("")
    with pytest.raises(EngineConfigError, match="missing/checkout"):
        load_engine_config(environ={}, cwd=tmp_path)


def test_invalid_local_config_does_not_fall_back_to_root(tmp_path):
    (tmp_path / "engine.toml").write_text("[repos]\n")
    local = tmp_path / ".engine/config.toml"
    local.parent.mkdir()
    local.write_text("[repos")
    with pytest.raises(EngineConfigError, match="invalid TOML"):
        load_engine_config(environ={}, cwd=tmp_path)


def test_local_config_works_without_root_config(tmp_path):
    local = tmp_path / ".engine/config.toml"
    local.parent.mkdir()
    local.write_text(
        '[repos]\n"owner/repo" = "~/code/repo"\n'
        '[workflows]\ndirectory = "../workflows"\n'
        '[approvals.bash]\nallow = ["uv run pytest"]\n'
    )
    loaded = load_engine_config(environ={}, cwd=tmp_path)
    assert loaded.path == local.resolve()
    assert loaded.config.repos == {"owner/repo": "~/code/repo"}
    assert loaded.workflows_directory == (tmp_path / "workflows").resolve()
    assert loaded.config.approvals.bash.allow == ("uv run pytest",)

def test_review_resolution_configuration():
    assert parse_engine_config({}).github.resolve_addressed_threads is True
    assert parse_engine_config({"github": {"resolve_addressed_threads": False}}).github.resolve_addressed_threads is False
    with pytest.raises(EngineConfigError, match="must be a boolean"):
        parse_engine_config({"github": {"resolve_addressed_threads": "false"}})
