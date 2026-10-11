"""GitHub source control: git in the workspace, ``gh`` for the rest."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from engine.adapters.source_control.github import (
    GitGlobalOptionError,
    GitHubSourceControl,
    GitHubSourceControlError,
    GitOutsideWorkspaceError,
    InternalBranchPublicationError,
)
from engine.domain.ids import WorkspaceId


WORKSPACE = WorkspaceId("ws-under-test")
_IDENTITY = ("-c", "user.name=Engine Tests", "-c", "user.email=engine@example.test")


class _OneWorkspace:
    """Enough of `WorkspaceProvider` to resolve a single checkout."""

    def __init__(self, root_path: Path) -> None:
        self._root_path = str(root_path)

    async def root_path(self, workspace_id: WorkspaceId) -> str:
        assert workspace_id == WORKSPACE
        return self._root_path


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _checkout(path: Path, branch: str = "main", *, git_repo) -> GitHubSourceControl:
    """A real repository on `branch`, and a source control pointed at it."""
    git_repo(path, branch, commit=True)
    _git(path, "config", "user.name", "Engine Tests")
    _git(path, "config", "user.email", "engine@example.test")
    # A remote that exists but is never reachable: the guards under test have
    # to refuse before anything is dialled, so a push that gets past one fails
    # loudly rather than quietly talking to something.
    _git(path, "remote", "add", "origin", str(path / "nowhere.git"))
    return GitHubSourceControl("", workspace_provider=_OneWorkspace(path))


# --- git runs in the workspace, and any subcommand is reachable -------------


def test_any_subcommand_runs_in_the_workspace(tmp_path: Path, *, git_repo) -> None:
    """The point of the passthrough: no menu, and one bounded directory."""
    source_control = _checkout(tmp_path / "checkout", git_repo=git_repo)

    branches = asyncio.run(
        source_control.run_git(WORKSPACE, ["rev-parse", "--abbrev-ref", "HEAD"])
    )
    # A subcommand no named port method would ever have thought to expose.
    searched = asyncio.run(
        source_control.run_git(WORKSPACE, ["log", "--format=%s", "-S", "engine"])
    )

    assert branches.ok
    assert branches.stdout == "main"
    assert searched.stdout == "initial"


def test_a_multi_line_commit_message_survives_being_an_argument(
    tmp_path: Path, *, git_repo
) -> None:
    """An argument vector, not a command line: nothing is split or quoted."""
    checkout = tmp_path / "checkout"
    source_control = _checkout(checkout, git_repo=git_repo)
    (checkout / "greeting.txt").write_text("hello\n")
    message = "feat: add a greeting\n\nWith a body that has its own lines."

    asyncio.run(source_control.run_git(WORKSPACE, ["add", "greeting.txt"]))
    committed = asyncio.run(
        source_control.run_git(WORKSPACE, ["commit", "--message", message])
    )

    assert committed.ok, committed.stderr
    assert _git(checkout, "log", "-1", "--format=%B").strip() == message


def test_a_failing_command_is_reported_rather_than_raised(
    tmp_path: Path, *, git_repo
) -> None:
    """Half of git answers questions with its exit code.

    `diff --exit-code` says "there are changes" that way, so raising on every
    non-zero exit would make a whole class of git unusable through the tool.
    """
    checkout = tmp_path / "checkout"
    source_control = _checkout(checkout, git_repo=git_repo)
    (checkout / "README.md").write_text("changed\n")

    result = asyncio.run(source_control.run_git(WORKSPACE, ["diff", "--exit-code"]))
    unknown = asyncio.run(source_control.run_git(WORKSPACE, ["frobnicate"]))

    assert not result.ok
    assert result.exit_code == 1
    assert "README.md" in result.stdout
    assert not unknown.ok
    assert "frobnicate" in unknown.stderr


def test_git_needs_at_least_one_argument(tmp_path: Path, *, git_repo) -> None:
    source_control = _checkout(tmp_path / "checkout", git_repo=git_repo)

    with pytest.raises(ValueError):
        asyncio.run(source_control.run_git(WORKSPACE, []))


# --- and the one thing it will not do ---------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        ["push", "--set-upstream", "origin", "engine/ws-under-test"],
        ["push", "origin", "HEAD:refs/heads/engine/ws-under-test"],
        ["push", "origin", "+engine/ws-under-test:engine/ws-under-test"],
    ],
)
def test_an_internal_branch_is_never_published(
    tmp_path: Path, arguments: list[str], *, git_repo
) -> None:
    """`engine/<workspace>` is Engine's bookkeeping, not a proposed change.

    Enforced here rather than asked for in a prompt, which is the difference
    between a rule and a suggestion.
    """
    source_control = _checkout(tmp_path / "checkout", git_repo=git_repo)

    with pytest.raises(InternalBranchPublicationError):
        asyncio.run(source_control.run_git(WORKSPACE, arguments))


def test_a_push_naming_no_refspec_is_refused(tmp_path: Path, *, git_repo) -> None:
    """The refusable case with nothing in argv to refuse.

    `git push` says nothing about which source or destination its configuration
    will choose, so there is no branch name in argv for the guard to validate.
    """
    source_control = _checkout(tmp_path / "checkout", branch="engine/ws-under-test", git_repo=git_repo)

    with pytest.raises(InternalBranchPublicationError):
        asyncio.run(source_control.run_git(WORKSPACE, ["push", "origin"]))


@pytest.mark.parametrize(
    "arguments",
    [
        ["push", "origin", "HEAD"],
        ["push", "origin", "@"],
        ["push", "--all", "origin"],
        ["push", "--branches", "origin"],
        ["push", "--mirror", "origin"],
        ["push", "origin", ":"],
        ["push", "origin", "refs/heads/*:refs/heads/*"],
    ],
)
def test_ambiguous_and_bulk_pushes_are_refused(
    tmp_path: Path, arguments: list[str], *, git_repo
) -> None:
    """Every allowed branch push identifies its remote destination in argv."""
    source_control = _checkout(tmp_path / "checkout", branch="engine/ws-under-test", git_repo=git_repo)

    with pytest.raises(InternalBranchPublicationError):
        asyncio.run(source_control.run_git(WORKSPACE, arguments))


def test_a_descriptive_branch_is_not_refused(tmp_path: Path, *, git_repo) -> None:
    """The guard is about one prefix, and must not read as "no pushing"."""
    checkout = tmp_path / "checkout"
    source_control = _checkout(checkout, git_repo=git_repo)
    _git(checkout, "branch", "agent/add-a-greeting")

    # Reaching git at all is the assertion: the push then fails on the remote
    # that was never meant to answer, which is a report rather than a refusal.
    result = asyncio.run(
        source_control.run_git(WORKSPACE, ["push", "origin", "agent/add-a-greeting"])
    )

    assert not result.ok
    assert "nowhere.git" in f"{result.stderr}\n{result.stdout}"


def test_head_is_safe_when_its_destination_is_explicit(
    tmp_path: Path, *, git_repo
) -> None:
    source_control = _checkout(tmp_path / "checkout", git_repo=git_repo)

    result = asyncio.run(
        source_control.run_git(
            WORKSPACE, ["push", "origin", "HEAD:refs/heads/agent/add-a-greeting"]
        )
    )

    assert not result.ok
    assert "nowhere.git" in f"{result.stderr}\n{result.stdout}"


@pytest.mark.parametrize(
    "arguments",
    [
        ["-C", "/etc", "status"],
        ["--git-dir=/elsewhere/.git", "log"],
        ["--work-tree", "/elsewhere", "status"],
        ["-c", "alias.x=!sh", "x"],
        ["--exec-path=/tmp", "x"],
        ["--config-env", "alias.x=PAYLOAD", "x"],
    ],
)
def test_git_global_options_cannot_select_config_or_executables(
    tmp_path: Path, arguments: list[str], *, git_repo
) -> None:
    """Global git options can be process launchers in disguise.

    `-c alias.x=!sh` and `--exec-path` are direct code-execution paths. An
    allowlist also covers the next global option git adds without requiring a
    security reviewer to hear about it first.
    """
    source_control = _checkout(tmp_path / "checkout", git_repo=git_repo)

    with pytest.raises((GitGlobalOptionError, GitOutsideWorkspaceError)):
        asyncio.run(source_control.run_git(WORKSPACE, arguments))


def test_value_free_safe_global_options_still_pass(tmp_path: Path, *, git_repo) -> None:
    source_control = _checkout(tmp_path / "checkout", git_repo=git_repo)

    result = asyncio.run(
        source_control.run_git(WORKSPACE, ["--no-pager", "rev-parse", "HEAD"])
    )

    assert result.ok, result.stderr


def test_git_never_receives_the_forge_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_control = GitHubSourceControl("adapter-secret", workspace_provider=_OneWorkspace(tmp_path))
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "other-host-secret")

    environment = source_control._git_environment()

    assert "GH_TOKEN" not in environment
    assert "GITHUB_TOKEN" not in environment


def test_git_without_a_workspace_provider_says_so() -> None:
    with pytest.raises(RuntimeError, match="workspace provider"):
        asyncio.run(GitHubSourceControl("").run_git(WORKSPACE, ["status"]))


# --- opening the review ------------------------------------------------------


def test_opening_a_review_proposes_against_the_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, git_repo
) -> None:
    """A workflow's base is written `origin/main`; GitHub wants `main`."""
    checkout = tmp_path / "checkout"
    source_control = _checkout(checkout, git_repo=git_repo)
    api_calls: list[tuple[str, str, dict]] = []

    async def fake_api(self_inner, method: str, path: str, **kwargs: object) -> dict:
        api_calls.append((method, path, kwargs.get("json", {})))
        return {"html_url": "https://github.com/acme/api/pull/42"}

    monkeypatch.setattr(type(source_control), "_api", fake_api)

    url = asyncio.run(
        source_control.request_review(
            WORKSPACE, "agent/add-a-greeting", "origin/main", "feat: greet", "Body."
        )
    )

    assert url == "https://github.com/acme/api/pull/42"
    assert len(api_calls) == 1
    method, path, payload = api_calls[0]
    assert method == "POST"
    assert path.endswith("/pulls")
    assert payload["head"] == "agent/add-a-greeting"
    assert payload["base"] == "main"
    assert payload["title"] == "feat: greet"
    assert payload["body"] == "Body."


def test_a_base_branch_with_a_slash_in_it_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, git_repo
) -> None:
    """`release/2.0` is a branch name, not a remote and a branch."""
    source_control = _checkout(tmp_path / "checkout", git_repo=git_repo)
    api_calls: list[tuple[str, str, dict]] = []

    async def fake_api(self_inner, method: str, path: str, **kwargs: object) -> dict:
        api_calls.append((method, path, kwargs.get("json", {})))
        return {"html_url": "https://github.com/acme/api/pull/42"}

    monkeypatch.setattr(type(source_control), "_api", fake_api)

    asyncio.run(
        source_control.request_review(
            WORKSPACE, "agent/fix", "release/2.0", "fix: it", ""
        )
    )

    assert api_calls[0][2]["base"] == "release/2.0"


def test_a_review_is_never_opened_for_an_internal_branch(
    tmp_path: Path, *, git_repo
) -> None:
    source_control = _checkout(tmp_path / "checkout", git_repo=git_repo)

    with pytest.raises(InternalBranchPublicationError):
        asyncio.run(
            source_control.request_review(
                WORKSPACE, "engine/ws-under-test", "main", "feat: leak", ""
            )
        )


# --- comments ----------------------------------------------------------------


def test_general_comment_posts_to_the_issues_comments_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_control = GitHubSourceControl("")
    api_calls: list[tuple[str, str, dict]] = []

    async def fake_api(self_inner, method: str, path: str, **kwargs: object) -> dict:
        api_calls.append((method, path, kwargs.get("json", {})))
        return {"id": 123, "html_url": "https://github.com/acme/api/pull/42#discussion_r123"}

    monkeypatch.setattr(type(source_control), "_api", fake_api)

    result = asyncio.run(
        source_control.add_comment(
            "https://github.com/acme/api/pull/42", "Looks good."
        )
    )

    assert result.id == 123
    assert result.url == "https://github.com/acme/api/pull/42#discussion_r123"
    assert len(api_calls) == 1
    method, path, payload = api_calls[0]
    assert method == "POST"
    assert path == "/repos/acme/api/issues/42/comments"
    assert payload == {"body": "Looks good."}


def test_inline_comment_resolves_head_and_posts_review_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_control = GitHubSourceControl("")
    api_calls: list[tuple[str, str, dict]] = []

    async def fake_api(self_inner, method: str, path: str, **kwargs: object) -> dict:
        api_calls.append((method, path, kwargs.get("json", {})))
        if method == "GET" and path.endswith("/pulls/42"):
            return {"head": {"sha": "abc123"}}
        return {"id": 123, "html_url": "https://github.com/acme/api/pull/42#discussion_r123"}

    monkeypatch.setattr(type(source_control), "_api", fake_api)

    result = asyncio.run(
        source_control.add_comment(
            "https://github.com/acme/api/pull/42",
            "This can race.",
            "src/worker.py",
            17,
        )
    )

    assert result.id == 123
    assert result.url == "https://github.com/acme/api/pull/42#discussion_r123"
    assert len(api_calls) == 2
    # First call fetches the PR to get the head SHA.
    get_method, get_path, _ = api_calls[0]
    assert get_method == "GET"
    assert get_path == "/repos/acme/api/pulls/42"
    # Second call posts the inline review comment.
    post_method, post_path, post_payload = api_calls[1]
    assert post_method == "POST"
    assert post_path == "/repos/acme/api/pulls/42/comments"
    assert post_payload["body"] == "This can race."
    assert post_payload["commit_id"] == "abc123"
    assert post_payload["path"] == "src/worker.py"
    assert post_payload["line"] == 17
    assert post_payload["side"] == "RIGHT"


@pytest.mark.parametrize(
    ("file", "line"), [("src/worker.py", None), (None, 17), ("src/worker.py", 0)]
)
def test_inline_comment_requires_a_valid_file_and_line(
    file: str | None, line: int | None
) -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            GitHubSourceControl("").add_comment(
                "https://github.com/acme/api/pull/42", "Finding.", file, line
            )
        )


def test_reply_posts_to_existing_review_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    api = AsyncMock(return_value={"id": 124, "html_url": "https://github.com/acme/api/pull/42#discussion_r124"})
    source = GitHubSourceControl("")
    monkeypatch.setattr(source, "_api", api)
    result = asyncio.run(source.add_comment(
        "https://github.com/acme/api/pull/42", "Fixed.", in_reply_to_id=123
    ))
    api.assert_awaited_once_with(
        "POST", "/repos/acme/api/pulls/42/comments/123/replies", json={"body": "Fixed."}
    )
    assert result.id == 124
    assert result.url == "https://github.com/acme/api/pull/42#discussion_r124"


@pytest.mark.parametrize("arguments", [
    {"in_reply_to_id": 0}, {"in_reply_to_id": -1}, {"in_reply_to_id": True},
    {"in_reply_to_id": "123"}, {"in_reply_to_id": 1.5},
    {"in_reply_to_id": 123, "file": "src/app.py", "line": 1},
])
def test_invalid_reply_is_rejected_before_api_call(monkeypatch: pytest.MonkeyPatch, arguments: dict) -> None:
    from unittest.mock import AsyncMock

    api = AsyncMock()
    source = GitHubSourceControl("")
    monkeypatch.setattr(source, "_api", api)
    with pytest.raises(ValueError):
        asyncio.run(source.add_comment("https://github.com/acme/api/pull/42", "Fixed.", **arguments))
    api.assert_not_awaited()


@pytest.mark.parametrize("permission, allowed", [
    ("write", True), ("admin", True), ("read", False), ("none", False),
    (None, False), ("unexpected", False),
])
def test_effective_repository_write_permission(monkeypatch, permission, allowed):
    from unittest.mock import AsyncMock

    source = GitHubSourceControl("")
    api = AsyncMock(return_value={"permission": permission, "role_name": "custom-role"})
    monkeypatch.setattr(source, "_api", api)
    assert asyncio.run(source.can_write_repository(
        "https://github.com/acme/api/pull/42", "someone")) is allowed
    api.assert_awaited_once_with("GET", "/repos/acme/api/collaborators/someone/permission")


@pytest.mark.parametrize("user, allowed", [
    ({"id": 42, "login": "someone"}, True),
    ({"id": 7, "login": "someone"}, False),
    ({"id": "42"}, False),
    (None, False),
])
def test_repository_permission_is_bound_to_the_user_id(monkeypatch, user, allowed):
    """A login may have been renamed and claimed by someone else since it was
    verified, so a user ID is checked against the account GitHub answered for."""
    from unittest.mock import AsyncMock

    source = GitHubSourceControl("")
    monkeypatch.setattr(source, "_api", AsyncMock(return_value={"permission": "write", "user": user}))
    assert asyncio.run(source.can_write_repository(
        "https://github.com/acme/api/pull/42", "someone", user_id=42)) is allowed


def test_repository_permission_lookup_failure_propagates(monkeypatch):
    from unittest.mock import AsyncMock

    source = GitHubSourceControl("")
    monkeypatch.setattr(source, "_api", AsyncMock(side_effect=RuntimeError("HTTP 403")))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        asyncio.run(source.can_write_repository("https://github.com/acme/api/pull/42", "someone"))


def test_authenticated_login_identifies_the_posting_account(monkeypatch):
    from unittest.mock import AsyncMock

    source = GitHubSourceControl("")
    api = AsyncMock(return_value={"login": "OpenEngine-worker", "type": "User"})
    monkeypatch.setattr(source, "_api", api)
    assert asyncio.run(
        source.authenticated_login("https://github.com/acme/api")
    ) == "OpenEngine-worker"
    api.assert_awaited_once_with("GET", "/user")


@pytest.mark.parametrize("response", [{}, {"login": ""}, {"login": 7}, []])
def test_authenticated_login_refuses_an_unusable_answer(monkeypatch, response):
    from unittest.mock import AsyncMock

    source = GitHubSourceControl("")
    monkeypatch.setattr(source, "_api", AsyncMock(return_value=response))
    with pytest.raises(GitHubSourceControlError):
        asyncio.run(source.authenticated_login("https://github.com/acme/api"))


@pytest.mark.parametrize(
    "pr_url",
    [
        # Refused by the shared reader, so refused here too: two change
        # requests in one path, numbers spelled other than one way, and a
        # dot segment that httpx would resolve before the request leaves.
        "https://github.com/acme/api/pull/42/x/victim/repo/pull/99",
        "https://github.com/acme/api/pull/\u0661\u0662",
        "https://github.com/acme/api/pull/\u00b2",
        "https://github.com/acme/api/pull/042",
        "https://github.com/../x/pull/1",
        "https://github.com/a/../pull/1",
        "https://github.com/./x/pull/1",
        "https://github.com/acme/api/-/merge_requests/1",
        # Nothing GitHub would not write in a name, either.
        "https://github.com/acme/api%2F../pull/1",
        "https://github.com/ac me/api/pull/1",
    ],
)
def test_a_url_the_shared_reader_refuses_is_refused_here_too(
    monkeypatch: pytest.MonkeyPatch, pr_url: str
) -> None:
    from unittest.mock import AsyncMock

    api = AsyncMock()
    source = GitHubSourceControl("")
    monkeypatch.setattr(source, "_api", api)
    with pytest.raises(ValueError, match="pull-request URL"):
        asyncio.run(source.add_comment(pr_url, "Finding."))
    api.assert_not_awaited()


@pytest.mark.parametrize(
    "pr_url",
    [
        # The tab a reviewer is looking at when it copies the address. CICheck
        # and the recorders read past these, so the adapter must as well.
        "https://github.com/acme/api/pull/42/files",
        "https://github.com/acme/api/pull/42/commits",
        "https://github.com/acme/api/pull/42/files#diff-abc",
        "https://github.com/acme/api/pull/42/",
    ],
)
def test_a_tab_on_a_pull_request_is_the_same_pull_request(
    monkeypatch: pytest.MonkeyPatch, pr_url: str
) -> None:
    from unittest.mock import AsyncMock

    api = AsyncMock(return_value={"id": 1, "html_url": f"{pr_url}#c1"})
    source = GitHubSourceControl("")
    monkeypatch.setattr(source, "_api", api)
    asyncio.run(source.add_comment(pr_url, "Finding."))
    assert "/repos/acme/api/issues/42/comments" in api.await_args.args[1]


@pytest.mark.parametrize(
    "pr_url, parts",
    [
        ("https://github.com/acme/api/pull/42", ("acme", "api", "42")),
        # A dot inside a name is ordinary; only a whole dot segment moves.
        ("https://github.com/a.b/c.d/pull/7", ("a.b", "c.d", "7")),
        ("https://github.com/wei/pull/pull/123", ("wei", "pull", "123")),
        ("https://github.com/my-org/my_repo/pull/1", ("my-org", "my_repo", "1")),
        # The reader's key is lowercased; the names sent to the API are what
        # the URL wrote.
        ("https://github.com/Acme/API/pull/42/files", ("Acme", "API", "42")),
    ],
)
def test_the_parts_sent_are_the_ones_the_shared_reader_read(
    pr_url: str, parts: tuple[str, str, str]
) -> None:
    from engine.adapters.source_control.github import _pull_request_parts

    assert _pull_request_parts(pr_url) == parts


def test_a_remote_url_is_held_to_the_same_names() -> None:
    from engine.adapters.source_control.github import _parse_repo_coords

    assert _parse_repo_coords("git@github.com:acme/api.git") == ("acme", "api")
    with pytest.raises(GitHubSourceControlError, match="cannot determine owner/repo"):
        _parse_repo_coords("https://github.com/../x.git")


@pytest.mark.parametrize("arguments", [{}, {"file": "app.py", "line": 1}, {"in_reply_to_id": 2}])
def test_foreign_pull_request_hosts_are_refused_before_any_api_call(monkeypatch, arguments):
    from unittest.mock import AsyncMock

    source = GitHubSourceControl("")
    api = AsyncMock()
    monkeypatch.setattr(source, "_api", api)
    with pytest.raises(ValueError, match="pull-request URL"):
        asyncio.run(source.add_comment("https://evil.example/acme/api/pull/1", "Finding", **arguments))
    with pytest.raises(ValueError, match="pull-request URL"):
        asyncio.run(source.can_write_repository("https://evil.example/acme/api/pull/1", "alice"))
    api.assert_not_awaited()


@pytest.mark.parametrize("host", ["forge.example", "alias.example"])
def test_explicit_aliases_of_the_transport_are_accepted(monkeypatch, host):
    from unittest.mock import AsyncMock

    source = GitHubSourceControl("", api_url="https://forge.example/api/v3", host_aliases={"ALIAS.EXAMPLE": "FORGE.EXAMPLE"})
    api = AsyncMock(return_value={"id": 1, "html_url": f"https://{host}/acme/api/pull/1#c"})
    monkeypatch.setattr(source, "_api", api)
    asyncio.run(source.add_comment(f"https://{host}/acme/api/pull/1", "Finding"))
    api.assert_awaited_once_with("POST", "/repos/acme/api/issues/1/comments", json={"body": "Finding"})
    with pytest.raises(ValueError):
        asyncio.run(source.add_comment("https://github.com/acme/api/pull/1", "Finding"))


@pytest.mark.parametrize("arguments", [{}, {"file": "app.py", "line": 1}, {"in_reply_to_id": 2}])
@pytest.mark.parametrize("api_url, foreign_url, aliases", [
    (
        "https://api.github.com",
        "https://forge.example/acme/api/pull/1",
        {"forge.example": "forge.example"},
    ),
    (
        "https://api.github.com",
        "https://alias.example/acme/api/pull/1",
        {"alias.example": "forge.example"},
    ),
    (
        "https://forge.example/api/v3",
        "https://github.com/acme/api/pull/1",
        {"github.com": "github.com"},
    ),
])
def test_aliases_for_another_transport_cannot_authorize_requests(
    monkeypatch, arguments, api_url, foreign_url, aliases
):
    from unittest.mock import AsyncMock

    source = GitHubSourceControl("", api_url=api_url, host_aliases=aliases)
    api = AsyncMock()
    monkeypatch.setattr(source, "_api", api)
    with pytest.raises(ValueError, match="pull-request URL"):
        asyncio.run(source.add_comment(foreign_url, "Private finding", **arguments))
    with pytest.raises(ValueError, match="pull-request URL"):
        asyncio.run(source.can_write_repository(foreign_url, "alice"))
    api.assert_not_awaited()


@pytest.mark.parametrize("cli", [False, True])
@pytest.mark.parametrize("authority", ["forge.example", "forge.example:8443"])
@pytest.mark.parametrize("arguments", [{}, {"file": "app.py", "line": 1}, {"in_reply_to_id": 2}])
def test_pull_request_ports_must_match_transport(monkeypatch, cli, authority, arguments):
    from unittest.mock import AsyncMock
    from engine.adapters.source_control.github.transports import GitHubCliTransport

    source = GitHubSourceControl(
        "", api_url=f"https://{authority}/api/v3",
        transport=GitHubCliTransport(host=authority) if cli else None,
    )
    api = AsyncMock(return_value={"id": 1, "html_url": "https://forge.example/o/r/pull/5#c"})
    monkeypatch.setattr(source, "_api", api)
    for target in ["forge.example:9999", "forge.example:80"]:
        with pytest.raises(ValueError, match="pull-request URL"):
            asyncio.run(source.add_comment(f"https://{target}/o/r/pull/5", "Private", **arguments))
        with pytest.raises(ValueError, match="pull-request URL"):
            asyncio.run(source.can_write_repository(f"https://{target}/o/r/pull/5", "alice"))
    api.assert_not_awaited()
    accepted = authority if ":" in authority else authority + ":443"
    asyncio.run(source.add_comment(f"https://{accepted}/o/r/pull/5", "Finding"))


@pytest.mark.parametrize("target, accepted", [
    ("forge.example:443", True),
    ("forge.example:8443", False),
])
def test_alias_ports_are_bound_to_transport(monkeypatch, target, accepted):
    from unittest.mock import AsyncMock

    source = GitHubSourceControl(
        "", api_url="https://forge.example",
        host_aliases={"alias.example:8443": target},
    )
    api = AsyncMock(return_value={"id": 1, "html_url": "https://forge.example/o/r/pull/5#c"})
    monkeypatch.setattr(source, "_api", api)
    with pytest.raises(ValueError):
        asyncio.run(source.add_comment("https://alias.example/o/r/pull/5", "Private"))
    url = "https://alias.example:8443/o/r/pull/5"
    if accepted:
        asyncio.run(source.add_comment(url, "Finding"))
        api.assert_awaited_once()
    else:
        with pytest.raises(ValueError):
            asyncio.run(source.add_comment(url, "Private"))
        api.assert_not_awaited()


@pytest.mark.parametrize("head_repo,base_repo,expected", [
    ({"id": 1}, {"id": 1}, True),
    ({"id": 2}, {"id": 1}, False),
    (None, {"id": 1}, False),
    ({}, {}, False),
])
def test_pull_request_head_repository(head_repo, base_repo, expected) -> None:
    from unittest.mock import AsyncMock

    source = GitHubSourceControl("")
    source._workspace_repo = AsyncMock(return_value=("acme", "api"))
    source._api = AsyncMock(return_value={"head": {"repo": head_repo}, "base": {"repo": base_repo}})
    source._paginated_objects = AsyncMock(return_value=[])
    shown = asyncio.run(source.view_change_request(WORKSPACE, 7))
    assert shown.head_is_same_repository is expected


def test_branch_tips_reads_only_requested_refs(monkeypatch):
    from unittest.mock import AsyncMock

    source = GitHubSourceControl("")
    api = AsyncMock(side_effect=[[
        {"ref": "refs/heads/feature/x", "object": {"sha": "head"}},
        {"ref": "refs/heads/feature/xyz", "object": {"sha": "other"}},
    ], []])
    monkeypatch.setattr(source, "_api", api)
    assert asyncio.run(source.branch_tips("acme/api", ("feature/x", "missing"))) == {"feature/x": "head"}
    assert api.await_count == 2
    assert api.call_args_list[0].args == ("GET", "/repos/acme/api/git/matching-refs/heads/feature%2Fx")


def test_branch_tips_refuses_another_forge():
    with pytest.raises(ValueError):
        asyncio.run(GitHubSourceControl("").branch_tips("other.example/acme/api", ("feature",)))


@pytest.mark.parametrize("response", [[{"ref": "refs/heads/feature"}], {"message": "unavailable"}])
def test_branch_tips_refuses_invalid_snapshot(monkeypatch, response):
    from unittest.mock import AsyncMock

    source = GitHubSourceControl("")
    monkeypatch.setattr(source, "_api", AsyncMock(return_value=response))
    with pytest.raises(GitHubSourceControlError):
        asyncio.run(source.branch_tips("acme/api", ("feature",)))


@pytest.mark.parametrize("review_comment,kind", [(False, "issues"), (True, "pulls")])
@pytest.mark.parametrize("content", ["+1", "-1", "eyes"])
def test_add_reaction_uses_comment_endpoint_and_replaces_only_own_opposite(review_comment, kind, content):
    from unittest.mock import AsyncMock, MagicMock, call

    opposite = "-1" if content == "+1" else "+1"
    responses = [
        {"login": "Engine"},
        [{"id": 3, "content": opposite, "user": {"login": "ENGINE"}},
         {"id": 4, "content": opposite, "user": {"login": "someone"}}],
        {}, {},
    ] if content != "eyes" else [{}]
    transport = MagicMock(host="github.com", request=AsyncMock(side_effect=responses))
    source = GitHubSourceControl("", transport=transport)
    asyncio.run(source.add_reaction("https://github.com/acme/api/pull/7", 42, content,
                                    review_comment=review_comment))
    endpoint = f"/repos/acme/api/{kind}/comments/42/reactions"
    expected = [] if content == "eyes" else [
        call("GET", "/user"),
        call("GET", endpoint, params={"content": opposite, "per_page": 100, "page": 1}),
        call("DELETE", endpoint + "/3"),
    ]
    assert transport.request.await_args_list == expected + [
        call("POST", endpoint, json={"content": content})]


def test_reactions_are_idempotent_across_outcome_changes():
    from unittest.mock import MagicMock

    reactions = {}
    async def request(method, path, **kwargs):
        if path == "/user":
            return {"login": "Engine"}
        if method == "GET":
            return [r for r in reactions.values() if r["content"] == kwargs["params"]["content"]]
        if method == "DELETE":
            del reactions[int(path.rsplit("/", 1)[1])]
        if method == "POST":
            content = kwargs["json"]["content"]
            reaction_id = 1 if content == "+1" else 2
            reactions[reaction_id] = {"id": reaction_id, "content": content, "user": {"login": "Engine"}}
        return {}
    source = GitHubSourceControl("", transport=MagicMock(host="github.com", request=request))
    for content in ("-1", "-1", "+1", "+1"):
        asyncio.run(source.add_reaction("https://github.com/acme/api/pull/7", 42, content))
        assert [r["content"] for r in reactions.values()] == [content]


def test_reaction_transport_errors_are_surfaced():
    from unittest.mock import AsyncMock, MagicMock
    from engine.adapters.source_control.github.transports import GitHubTransportError

    transport = MagicMock(host="github.com", request=AsyncMock(side_effect=GitHubTransportError("offline")))
    source = GitHubSourceControl("", transport=transport)
    with pytest.raises(GitHubSourceControlError, match="offline"):
        asyncio.run(source.add_reaction("https://github.com/acme/api/pull/7", 42, "eyes"))


@pytest.mark.parametrize("resolution,keyword", [("resolves", "Resolves"), ("refs", "Refs")])
@pytest.mark.parametrize("issue_repo,reference", [("acme/api", "#7"), ("acme/other", "acme/other#7")])
def test_issue_publication_normalizes_body_and_head(
    monkeypatch, tmp_path, resolution, keyword, issue_repo, reference, *, git_repo
):
    from unittest.mock import AsyncMock
    source = _checkout(tmp_path / "checkout", "agent/issue", git_repo=git_repo)
    calls = []
    async def git(root, arguments):
        calls.append(arguments)
        return {
            ("branch", "--show-current"): "agent/issue",
            ("status", "--porcelain"): "",
            ("rev-parse", "HEAD"): "abc1234",
            ("log", "-1", "--format=%B"): f"feat: change\n\nRefs {reference}",
            ("ls-remote", "origin", "refs/heads/agent/issue"): "abc1234\trefs/heads/agent/issue",
        }.get(arguments, "")
    source._git_checked = git
    source._repo_coords = AsyncMock(return_value=("acme", "api"))
    source._api = AsyncMock(return_value={"html_url": "https://github.com/acme/api/pull/8"})
    result = asyncio.run(source.request_review(WORKSPACE, "agent/issue", "main", "feat: change", f"Description\n\nFixes {reference}", issue={"repository": issue_repo, "number": 7}, issue_resolution=resolution))
    assert result.endswith("/8")
    assert source._api.await_args.kwargs["json"]["body"] == f"Description\n\n{keyword} {reference}"
    amends = [call for call in calls if "commit" in call]
    if resolution == "resolves":
        assert len(amends) == 1
        assert f"Resolves {reference}" in amends[0][-1]
        assert f"Refs {reference}" in amends[0][-1]
        assert ("-c", "core.hooksPath=/dev/null", "push", "--no-mirror", "origin", "HEAD:refs/heads/agent/issue") in calls
    else:
        assert not amends


def _thread_page(resolved=False, cursor=None):
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "nodes": [{"id": "PRRT_1", "isResolved": resolved, "comments": {"nodes": [{
            "databaseId": 41, "body": "Fix it", "url": "https://github.com/acme/api/pull/7#discussion_r41", "author": {"login": "alice"}, "path": "app.py", "line": 3,
        }]}}], "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
    }}}}}


def _thread_node(resolved=False, replies=(), cursor=None):
    node = _thread_page(resolved)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]
    node["pullRequest"] = {"number": 7, "repository": {"nameWithOwner": "acme/api"}}
    node["comments"]["nodes"].extend(replies)
    node["comments"]["pageInfo"] = {"hasNextPage": bool(cursor), "endCursor": cursor}
    return {"data": {"node": node}}


@pytest.mark.parametrize("resolve,enabled", [(True, True), (False, True), (True, False)])
def test_addressed_review_reply_resolves_only_when_enabled(resolve, enabled):
    from unittest.mock import AsyncMock
    source = GitHubSourceControl("", resolve_addressed_threads=enabled)
    source._paginated_objects = AsyncMock(return_value=[])
    requests = []
    async def api(method, path, **options):
        requests.append((method, path, options))
        if path == "/graphql":
            if "mutation" in options["json"]["query"]:
                return {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_1", "isResolved": True}}}}
            return _thread_node()
        return {"id": 42, "html_url": "https://github.com/acme/api/pull/7#discussion_r42"}
    source._api = api
    reply = asyncio.run(source.add_comment("https://github.com/acme/api/pull/7", "Guard the empty input", in_reply_to_id=41, thread_id="PRRT_1", resolve=resolve, commit_sha="abcdef0" if resolve else None))
    assert reply.id == 42
    posted = [request for request in requests if request[1].endswith("/replies")]
    assert posted[0][2]["json"]["body"] == ("Addressed in abcdef0: " if resolve else "") + "Guard the empty input"
    mutations = [request for request in requests if "mutation" in request[2].get("json", {}).get("query", "")]
    assert len(mutations) == int(resolve and enabled)
    queries = [request for request in requests if "query" in request[2].get("json", {}).get("query", "")]
    assert len(queries) == 1
    if mutations:
        assert mutations[0][2]["json"]["variables"] == {"thread": "PRRT_1"}


def test_view_change_request_exposes_review_ids_and_state():
    from unittest.mock import AsyncMock
    source = GitHubSourceControl("")
    source._workspace_repo = AsyncMock(return_value=("acme", "api"))
    source._api = AsyncMock(side_effect=[{"html_url": "https://github.com/acme/api/pull/7"}, _thread_page(True)])
    source._paginated_objects = AsyncMock(side_effect=[[], [], [
        {"id": 41, "body": "Fix it", "user": {"login": "alice"}},
        {"id": 42, "in_reply_to_id": 41, "body": "Reply", "user": {"login": "bob"}},
    ]])
    shown = asyncio.run(source.view_change_request(WORKSPACE, 7))
    assert [(c.comment_id, c.thread_id, c.is_resolved) for c in shown.comments] == [(41, "PRRT_1", True), (42, "PRRT_1", True)]


def test_resolving_foreign_thread_or_graphql_failure_is_refused():
    from unittest.mock import AsyncMock
    source = GitHubSourceControl("")
    source._api = AsyncMock(return_value=_thread_node())
    with pytest.raises(ValueError, match="does not belong"):
        asyncio.run(source.resolve_review_thread("https://github.com/acme/api/pull/7", "PRRT_other"))
    assert source._api.await_count == 1
    source._api = AsyncMock(return_value={"errors": [{"message": "denied"}]})
    with pytest.raises(GitHubSourceControlError, match="denied"):
        asyncio.run(source.resolve_review_thread("https://github.com/acme/api/pull/7", "PRRT_1"))


def test_first_issue_pr_appends_metadata_and_keeps_credit_once(tmp_path, *, git_repo):
    from unittest.mock import AsyncMock
    from engine.adapters.workspace_provider.git_worktree import _credit
    root = tmp_path / "checkout"
    source = _checkout(root, "main", git_repo=git_repo)
    remote = tmp_path / "remote.git"
    git_repo(remote, bare=True)
    _git(root, "remote", "set-url", "origin", str(remote))
    _git(root, "push", "origin", "main")
    _git(root, "checkout", "-b", "agent/issue")
    asyncio.run(_credit(root, "Alice <alice@example.test>", "#7"))
    (root / "fix.txt").write_text("fixed")
    _git(root, "add", "fix.txt")
    _git(root, "commit", "-m", "feat: fix the issue")
    _git(root, "push", "origin", "agent/issue")
    old = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    hooks = tmp_path / "untrusted-hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-executed"
    for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-rewrite", "pre-push"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        hook.chmod(0o755)
    _git(root, "config", "core.hooksPath", str(hooks))
    helper = tmp_path / "untrusted-helper"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    helper.chmod(0o755)
    _git(root, "config", "core.fsmonitor", str(helper))
    _git(root, "config", "commit.gpgsign", "true")
    _git(root, "config", "gpg.program", str(helper))
    source._repo_coords = AsyncMock(return_value=("acme", "api"))
    source._api = AsyncMock(return_value={"html_url": "https://github.com/acme/api/pull/8"})
    asyncio.run(source.request_review(WORKSPACE, "agent/issue", "main", "feat: fix", "Fixes #7", issue={"repository": "acme/api", "number": 7}, issue_resolution="resolves"))
    new = _git(root, "rev-parse", "HEAD")
    message = _git(root, "log", "-1", "--format=%B")
    assert old != new
    assert _git(root, "rev-parse", "HEAD^") == old
    assert not marker.exists()
    assert _git(root, "rev-parse", "HEAD^{tree}") == tree
    assert message.count("Resolves #7") == message.count("Refs #7") == 1
    assert message.count("Co-authored-by: Alice <alice@example.test>") == 1
    assert _git(root, "ls-remote", "origin", "refs/heads/agent/issue").split()[0] == new
    asyncio.run(source._issue_head(str(root), "agent/issue", "main", "#7", "resolves"))
    assert _git(root, "rev-parse", "HEAD") == new


def test_resolution_retry_reuses_own_posted_reply():
    from unittest.mock import AsyncMock
    from engine.ports.source_control import Discussion
    source = GitHubSourceControl("")
    thread = Discussion("alice", "Fix", "", comment_id=41, thread_id="PRRT_1")
    source._resolve_validated_thread = AsyncMock(side_effect=[RuntimeError("unavailable"), True])
    source.authenticated_login = AsyncMock(return_value="engine")
    posted = {"id": 42, "html_url": "https://github.com/acme/api/pull/7#discussion_r42", "body": "Addressed in abcdef0: Fixed", "in_reply_to_id": 41, "user": {"login": "engine"}}
    source._review_thread_by_id = AsyncMock(side_effect=[(thread, []), (thread, [
        {"databaseId": 42, "url": posted["html_url"], "body": posted["body"], "author": {"login": "engine"}}
    ])])
    source._api = AsyncMock(return_value=posted)
    async def reply():
        return await source.add_comment("https://github.com/acme/api/pull/7", "Fixed", in_reply_to_id=41, thread_id="PRRT_1", resolve=True, commit_sha="abcdef0")
    with pytest.raises(GitHubSourceControlError, match="repeat the same"):
        asyncio.run(reply())
    assert asyncio.run(reply()).id == 42
    source._api.assert_awaited_once()


def test_partial_body_removes_all_closing_aliases_without_changing_other_issues():
    from engine.runtime.issue_links import issue_body
    body = "Fixes #7\nResolves acme/api#7\nFixes #70\nRefs other/repo#7"
    normalized = issue_body(body, "#7", "refs", qualified_reference="acme/api#7")
    assert "Fixes #7\n" not in normalized
    assert "Resolves acme/api#7" not in normalized
    assert "Fixes #70" in normalized
    assert "Refs other/repo#7" in normalized
    assert normalized.splitlines().count("Refs #7") == 1


def test_review_threads_follow_graphql_pagination():
    from unittest.mock import AsyncMock
    source = GitHubSourceControl("")
    second = _thread_page()
    node = second["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]
    node["id"] = "PRRT_2"
    node["comments"]["nodes"][0]["databaseId"] = 99
    source._api = AsyncMock(side_effect=[_thread_page(cursor="next"), second])
    found = asyncio.run(source.review_thread("https://github.com/acme/api/pull/7", 99))
    assert found.thread_id == "PRRT_2"
    assert source._api.await_args.kwargs["json"]["variables"]["cursor"] == "next"


@pytest.mark.parametrize("foreign", ["repository", "number", "missing", "root"])
def test_targeted_reply_rejects_foreign_thread_before_posting(foreign):
    from unittest.mock import AsyncMock
    page = _thread_node()
    node = page["data"]["node"]
    if foreign == "repository":
        node["pullRequest"]["repository"]["nameWithOwner"] = "other/api"
    elif foreign == "number":
        node["pullRequest"]["number"] = 8
    elif foreign == "missing":
        page["data"]["node"] = None
    else:
        node["comments"]["nodes"][0]["databaseId"] = 99
    source = GitHubSourceControl("")
    source._api = AsyncMock(return_value=page)
    with pytest.raises(ValueError):
        asyncio.run(source.add_comment(
            "https://github.com/acme/api/pull/7", "Fixed", in_reply_to_id=41,
            thread_id="PRRT_1", resolve=True, commit_sha="abcdef0",
        ))
    assert source._api.await_count == 1


def test_targeted_retry_paginates_only_thread_comments():
    from unittest.mock import AsyncMock
    body = "Addressed in abcdef0: Fixed"
    other = {"databaseId": 42, "body": body, "url": "other", "author": {"login": "alice"}}
    own = {"databaseId": 43, "body": body, "url": "own", "author": {"login": "engine"}}
    first = _thread_node(True, [other], cursor="next")
    second = _thread_node(True)
    second["data"]["node"]["comments"]["nodes"] = [own]
    source = GitHubSourceControl("")
    source._api = AsyncMock(side_effect=[first, second])
    source.authenticated_login = AsyncMock(return_value="engine")
    source._paginated_objects = AsyncMock()
    result = asyncio.run(source.add_comment(
        "https://github.com/acme/api/pull/7", "Fixed", in_reply_to_id=41,
        thread_id="PRRT_1", resolve=True, commit_sha="abcdef0",
    ))
    assert result.id == 43
    assert source._api.await_count == 2
    source._paginated_objects.assert_not_awaited()
    for call in source._api.await_args_list:
        assert call.args == ("POST", "/graphql")
        assert "reviewThreads(" not in call.kwargs["json"]["query"]
        assert call.kwargs["json"]["variables"]["thread"] == "PRRT_1"
    assert source._api.await_args.kwargs["json"]["variables"]["cursor"] == "next"


def test_targeted_reply_does_not_reuse_another_authors_comment():
    from unittest.mock import AsyncMock
    source = GitHubSourceControl("")
    source.authenticated_login = AsyncMock(return_value="engine")
    source._api = AsyncMock(side_effect=[
        _thread_node(True, [{"databaseId": 42, "body": "Addressed in abcdef0: Fixed",
                            "url": "other", "author": {"login": "alice"}}]),
        {"id": 43, "html_url": "own"},
    ])
    result = asyncio.run(source.add_comment(
        "https://github.com/acme/api/pull/7", "Fixed", in_reply_to_id=41,
        thread_id="PRRT_1", resolve=True, commit_sha="abcdef0",
    ))
    assert result.id == 43
    assert source._api.await_args.args[1].endswith("/41/replies")


def test_issue_head_fast_forward_preserves_a_concurrent_remote_push(
    tmp_path, *, git_repo
):
    root = tmp_path / "checkout"
    source = _checkout(root, git_repo=git_repo)
    remote = tmp_path / "remote.git"
    git_repo(remote, bare=True)
    _git(root, "remote", "set-url", "origin", str(remote))
    _git(root, "push", "origin", "main")
    _git(root, "checkout", "-b", "agent/issue")
    _git(root, "commit", "--allow-empty", "-m", "feat: fix")
    _git(root, "push", "origin", "agent/issue")
    head = _git(root, "rev-parse", "HEAD")
    concurrent = _git(root, "commit-tree", "HEAD^{tree}", "-p", head, "-m", "concurrent change")
    checked = source._git_checked

    async def racing(root_path, arguments):
        if "commit" in arguments:
            _git(root, "push", "origin", f"{concurrent}:refs/heads/agent/issue")
        return await checked(root_path, arguments)

    source._git_checked = racing
    with pytest.raises(GitHubSourceControlError):
        asyncio.run(source._issue_head(str(root), "agent/issue", "main", "#7", "resolves"))
    assert _git(root, "ls-remote", "origin", "refs/heads/agent/issue").split()[0] == concurrent
    assert _git(root, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("branch", ["main", "trunk", "release/stable"])
@pytest.mark.parametrize("prefix", ["", "origin/"])
def test_issue_publication_refuses_base_even_without_remote_protection(
    tmp_path, branch, prefix, *, git_repo
):
    from unittest.mock import AsyncMock
    root = tmp_path / "checkout"
    source = _checkout(root, branch, git_repo=git_repo)
    remote = tmp_path / "remote.git"
    git_repo(remote, bare=True)
    _git(root, "remote", "set-url", "origin", str(remote))
    _git(root, "push", "origin", branch)
    old = _git(root, "rev-parse", "HEAD")
    source._repo_coords = AsyncMock(return_value=("acme", "api"))
    source._api = AsyncMock()

    with pytest.raises(ValueError, match="must not rewrite.*base branch"):
        asyncio.run(source.request_review(
            WORKSPACE, branch, prefix + branch, "feat: change", "Body",
            issue={"repository": "acme/api", "number": 7}, issue_resolution="resolves",
        ))

    assert _git(root, "rev-parse", "HEAD") == old
    assert _git(root, "ls-remote", "origin", f"refs/heads/{branch}").split()[0] == old
    source._api.assert_not_awaited()


@pytest.mark.parametrize("keyword", ["Fixes", "Closes", "Resolves", "Refs"])
@pytest.mark.parametrize("separator", ["", " ", ": ", " : ", ":", " :", "\t:\n"])
@pytest.mark.parametrize("resolution", ["refs", "resolves"])
@pytest.mark.parametrize("reference", ["#7", "acme/api#7"])
def test_issue_body_normalizes_colon_keywords(keyword, separator, resolution, reference):
    from engine.runtime.issue_links import issue_body

    body = f"Description\n\n{keyword}{separator}{reference}"
    assert issue_body(body, "#7", resolution, qualified_reference="acme/api#7") == (
        f"Description\n\n{resolution.capitalize()} #7"
    )


@pytest.mark.parametrize("accepted", [False, True])
def test_issue_head_failed_push_can_be_retried(tmp_path, accepted, *, git_repo):
    root = tmp_path / "checkout"
    source = _checkout(root, "agent/issue", git_repo=git_repo)
    remote = tmp_path / "remote.git"
    git_repo(remote, bare=True)
    _git(root, "remote", "set-url", "origin", str(remote))
    _git(root, "push", "origin", "agent/issue")
    original = _git(root, "rev-parse", "HEAD")
    checked = source._git_checked

    async def failing(root_path, arguments):
        if "push" in arguments:
            if accepted:
                await checked(root_path, arguments)
            raise GitHubSourceControlError("connection lost")
        return await checked(root_path, arguments)

    source._git_checked = failing
    with pytest.raises(GitHubSourceControlError, match="connection lost"):
        asyncio.run(source._issue_head(str(root), "agent/issue", "main", "#7", "resolves"))
    local = _git(root, "rev-parse", "HEAD")
    assert (local != original) == accepted
    assert _git(root, "ls-remote", "origin", "refs/heads/agent/issue").split()[0] == local
    source._git_checked = checked
    asyncio.run(source._issue_head(str(root), "agent/issue", "main", "#7", "resolves"))
    assert "Resolves #7" in _git(root, "log", "-1", "--format=%B")
    assert _git(root, "ls-remote", "origin", "refs/heads/agent/issue").split()[0] == _git(root, "rev-parse", "HEAD")


def test_review_thread_stops_after_matching_page():
    from unittest.mock import AsyncMock
    source = GitHubSourceControl("")
    page = _thread_page(cursor="next")
    comment_id = page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]["comments"]["nodes"][0]["databaseId"]
    source._api = AsyncMock(side_effect=[page, AssertionError("unnecessary page")])
    assert asyncio.run(source.review_thread("https://github.com/acme/api/pull/7", comment_id)).comment_id == comment_id
    source._api.assert_awaited_once()


@pytest.mark.parametrize("failure", ["remote_unavailable", "local_move"])
def test_issue_head_recovery_preserves_uncertain_or_concurrent_state(
    tmp_path, failure, *, git_repo
):
    root = tmp_path / "checkout"
    source = _checkout(root, "agent/issue", git_repo=git_repo)
    remote = tmp_path / "remote.git"
    git_repo(remote, bare=True)
    _git(root, "remote", "set-url", "origin", str(remote))
    _git(root, "push", "origin", "agent/issue")
    checked = source._git_checked
    preserved = []

    async def failing(root_path, arguments):
        if "push" in arguments:
            if failure == "local_move":
                _git(root, "commit", "--allow-empty", "-m", "feat: concurrent local work")
            preserved.append(_git(root, "rev-parse", "HEAD"))
            raise GitHubSourceControlError("push failed")
        if "ls-remote" in arguments and preserved and failure == "remote_unavailable":
            raise GitHubSourceControlError("remote unavailable")
        return await checked(root_path, arguments)

    source._git_checked = failing
    with pytest.raises(GitHubSourceControlError):
        asyncio.run(source._issue_head(str(root), "agent/issue", "main", "#7", "resolves"))
    assert _git(root, "rev-parse", "HEAD") == preserved[0]


def test_issue_body_padded_nonmatches_finish_without_backtracking():
    import sys

    # Isolate the time limit so a regex regression cannot hang the test runner.
    subprocess.run(
        [sys.executable, "-c", """
from engine.runtime.issue_links import issue_body
for separator in (" " * 100_000, " " * 100_000 + ":" + " " * 100_000):
    body = "Fixes" + separator + "#8"
    assert issue_body(body, "#7", "refs") == body + "\\n\\nRefs #7"
    assert issue_body("Fixes" + separator + "#7", "#7", "refs") == "Refs #7"
"""],
        check=True, capture_output=True, timeout=5,
    )


@pytest.mark.parametrize("rewrite", ["insteadOf", "pushInsteadOf"])
@pytest.mark.parametrize("remote", ["https://github.com/acme/api.git", "git@github.com:acme/api.git", "origin"])
def test_force_push_checks_rewritten_destination(
    tmp_path, rewrite, remote, *, git_repo
):
    from unittest.mock import AsyncMock

    root = tmp_path / "checkout"
    source = _checkout(root, "agent/change", git_repo=git_repo)
    literal = remote if remote != "origin" else "https://github.com/acme/api.git"
    _git(root, "remote", "set-url", "origin", literal)
    _git(root, "config", f"url.https://github.com/acme/other.git.{rewrite}", literal)
    source._api = AsyncMock(side_effect=AssertionError("must refuse before API calls"))
    checked = source._git_checked

    async def no_push(root_path, arguments):
        assert "push" not in arguments, "must refuse before pushing"
        return await checked(root_path, arguments)

    source._git_checked = no_push
    with pytest.raises(ValueError, match="named remote|work-order-owned"):
        asyncio.run(source.run_git(
            WORKSPACE, ["push", "--force", remote, "HEAD:refs/heads/agent/change"],
            owned_pull_requests=(("acme/api", 7),),
        ))
    source._api.assert_not_awaited()
