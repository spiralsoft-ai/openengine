"""Local Git worktree workspace provisioning."""

import asyncio
from pathlib import Path
import shutil
import subprocess

import pytest

from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_BRANCH_REF,
    BranchInUseError,
    GitWorktreeError,
    GitWorktreeWorkspaceProvider,
)


_IDENTITY = ("-c", "user.name=Engine Tests", "-c", "user.email=engine@example.test")


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_origin_main_is_refreshed_without_moving_local_main(
    tmp_path: Path, *, git_repo
) -> None:
    upstream = tmp_path / "upstream"
    remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    git_repo(upstream, commit=True)
    subprocess.run(
        ["git", "clone", "--bare", str(upstream), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "clone", str(remote), str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    local_main = _git(repository, "rev-parse", "main")
    _git(upstream, "remote", "add", "origin", str(remote))
    (upstream / "latest.txt").write_text("from remote main\n")
    _git(upstream, "add", "latest.txt")
    _git(upstream, *_IDENTITY, "commit", "-m", "remote update")
    _git(upstream, "push", "origin", "main")
    remote_main = _git(upstream, "rev-parse", "main")
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))

    workspace = asyncio.run(provider.provision(str(repository), "origin/main"))

    assert _git(repository, "rev-parse", "main") == local_main
    assert _git(Path(workspace.root_path), "rev-parse", "HEAD") == remote_main
    assert Path(workspace.root_path, "latest.txt").read_text() == "from remote main\n"
    assert not _git(repository, "for-each-ref", "refs/engine/provisioning")


def test_origin_branch_is_refreshed_without_moving_the_local_branch(
    tmp_path: Path, *, git_repo
) -> None:
    upstream = tmp_path / "upstream"
    remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    git_repo(upstream, "master", commit=True)
    subprocess.run(
        ["git", "clone", "--bare", str(upstream), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "clone", str(remote), str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    local_master = _git(repository, "rev-parse", "master")
    _git(upstream, "remote", "add", "origin", str(remote))
    (upstream / "latest.txt").write_text("from remote master\n")
    _git(upstream, "add", "latest.txt")
    _git(upstream, *_IDENTITY, "commit", "-m", "remote update")
    _git(upstream, "push", "origin", "master")
    remote_master = _git(upstream, "rev-parse", "master")
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))

    workspace = asyncio.run(provider.provision(str(repository), "origin/master"))

    assert _git(repository, "rev-parse", "master") == local_master
    assert _git(Path(workspace.root_path), "rev-parse", "HEAD") == remote_master
    assert Path(workspace.root_path, "latest.txt").read_text() == "from remote master\n"


def test_missing_origin_branch_explains_how_to_fix_configuration(
    tmp_path: Path, *, git_repo
) -> None:
    upstream = tmp_path / "upstream"
    remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    git_repo(upstream, commit=True)
    subprocess.run(
        ["git", "clone", "--bare", str(upstream), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "clone", str(remote), str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))

    with pytest.raises(
        GitWorktreeError,
        match=(
            "Configured default branch 'master' does not exist on remote 'origin'. "
            "Create that branch, or set default_branch in engine.toml"
        ),
    ):
        asyncio.run(provider.provision(str(repository), "origin/master"))


def test_origin_head_follows_the_remote_default_branch(
    tmp_path: Path, *, git_repo
) -> None:
    upstream = tmp_path / "upstream"
    remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    git_repo(upstream, "trunk", commit=True)
    subprocess.run(
        ["git", "clone", "--bare", str(upstream), str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "clone", str(remote), str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))

    workspace = asyncio.run(provider.provision(str(repository), DEFAULT_BRANCH_REF))

    assert _git(Path(workspace.root_path), "rev-parse", "HEAD") == _git(upstream, "rev-parse", "trunk")


def test_origin_head_without_an_origin_uses_the_checked_out_commit(
    tmp_path: Path, *, git_repo
) -> None:
    repository = tmp_path / "repository"
    git_repo(repository, "trunk", commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))

    workspace = asyncio.run(provider.provision(str(repository), DEFAULT_BRANCH_REF))

    assert _git(Path(workspace.root_path), "rev-parse", "HEAD") == _git(repository, "rev-parse", "HEAD")


def test_each_workspace_is_a_distinct_worktree(tmp_path: Path, *, git_repo) -> None:
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))

    first = asyncio.run(provider.provision(str(repository), "HEAD"))
    second = asyncio.run(provider.provision(str(repository), "HEAD"))

    assert first.workspace_id != second.workspace_id
    assert first.root_path != second.root_path
    assert Path(first.root_path, "README.md").read_text() == "engine\n"
    assert _git(Path(first.root_path), "branch", "--show-current") == (
        f"engine/{first.workspace_id}"
    )
    assert asyncio.run(provider.root_path(first.workspace_id)) == first.root_path


def test_dispose_takes_the_work_with_it(tmp_path: Path, *, git_repo) -> None:
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))

    asyncio.run(provider.dispose(workspace.workspace_id))
    asyncio.run(provider.dispose(workspace.workspace_id))

    assert not Path(workspace.root_path).exists()
    assert workspace.ref not in _git(repository, "branch", "--list", workspace.ref)


def test_detaching_keeps_the_branch_and_reattaching_restores_the_work(
    tmp_path: Path, *, git_repo
) -> None:
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    Path(workspace.root_path, "agent.md").write_text("what the agent did\n")

    asyncio.run(provider.detach(workspace.workspace_id))
    detached = asyncio.run(provider.state(workspace.workspace_id))
    reattached = asyncio.run(
        provider.attach(workspace.workspace_id, str(repository), "HEAD")
    )

    assert not detached.attached
    assert detached.root_path is None
    # Uncommitted work is the normal state of an agent's worktree; detaching
    # snapshots it onto the branch rather than throwing it away.
    assert detached.ref == workspace.ref
    assert "agent.md" in _git(repository, "show", "--name-only", detached.ref)
    assert reattached.workspace_id == workspace.workspace_id
    assert reattached.root_path == workspace.root_path
    assert Path(reattached.root_path, "agent.md").read_text() == "what the agent did\n"
    assert _git(Path(reattached.root_path), "branch", "--show-current") == workspace.ref


def test_detaching_preserves_work_after_switching_to_a_publishing_branch(
    tmp_path: Path, *, git_repo
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "main"))
    root = Path(workspace.root_path)
    _git(root, "switch", "-c", "agent/publish-change")
    (root / "committed.md").write_text("published work\n")
    _git(root, "add", "committed.md")
    _git(root, *_IDENTITY, "commit", "-m", "feat: published change")
    (root / "uncommitted.md").write_text("remaining work\n")

    asyncio.run(provider.detach(workspace.workspace_id))
    restored = asyncio.run(provider.attach(workspace.workspace_id, str(repository), "main"))

    assert restored.workspace_id == workspace.workspace_id
    assert Path(restored.root_path, "committed.md").read_text() == "published work\n"
    assert Path(restored.root_path, "uncommitted.md").read_text() == "remaining work\n"
    assert _git(repository, "rev-parse", workspace.ref) == _git(
        repository, "rev-parse", "agent/publish-change"
    )


def test_detach_is_idempotent_and_leaves_committed_work_alone(
    tmp_path: Path, *, git_repo
) -> None:
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    root_path = Path(workspace.root_path)
    (root_path / "agent.md").write_text("committed by the agent\n")
    _git(root_path, "add", "agent.md")
    _git(root_path, *_IDENTITY, "commit", "-m", "the agent's own commit")
    committed = _git(root_path, "rev-parse", "HEAD")

    asyncio.run(provider.detach(workspace.workspace_id))
    asyncio.run(provider.detach(workspace.workspace_id))

    assert not root_path.exists()
    assert _git(repository, "rev-parse", workspace.ref) == committed


def test_work_is_snapshotted_even_where_git_has_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, git_repo
) -> None:
    """A machine that has never run `git config user.email` still detaches."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-system"))
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    Path(workspace.root_path, "agent.md").write_text("what the agent did\n")

    asyncio.run(provider.detach(workspace.workspace_id))

    assert "agent.md" in _git(repository, "show", "--name-only", workspace.ref)


def test_reattaching_a_branch_someone_is_reading_says_where_it_went(
    tmp_path: Path, *, git_repo
) -> None:
    """Reviewing the work is the point of the branch, so say how to hand it back."""
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    asyncio.run(provider.detach(workspace.workspace_id))
    _git(repository, "switch", workspace.ref)

    with pytest.raises(BranchInUseError) as refusal:
        asyncio.run(provider.attach(workspace.workspace_id, str(repository), "HEAD"))

    assert refusal.value.ref == workspace.ref
    assert refusal.value.checkout == str(repository)
    assert str(refusal.value).splitlines()[1] == (
        f"hint: switch that checkout to another branch first via "
        f"`git -C {repository} switch -`"
    )
    # Refused, not half-done: the checkout it could not make is not left behind.
    assert not Path(workspace.root_path).exists()


def test_the_branch_a_workspace_is_already_on_is_not_in_use_by_someone_else(
    tmp_path: Path, *, git_repo
) -> None:
    """The workspace's own checkout must not read as a stranger holding it."""
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))

    reattached = asyncio.run(
        provider.attach(workspace.workspace_id, str(repository), "HEAD")
    )

    assert reattached.root_path == workspace.root_path


def test_attach_replaces_a_checkout_deleted_behind_gits_back(
    tmp_path: Path, *, git_repo
) -> None:
    """A swept /tmp leaves an administrative entry that would refuse a new one."""
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    shutil.rmtree(workspace.root_path)

    reattached = asyncio.run(
        provider.attach(workspace.workspace_id, str(repository), "HEAD")
    )

    assert reattached.root_path == workspace.root_path
    assert Path(reattached.root_path, "README.md").read_text() == "engine\n"


def test_attach_checks_out_afresh_when_even_the_branch_is_gone(
    tmp_path: Path, *, git_repo
) -> None:
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    asyncio.run(provider.dispose(workspace.workspace_id))

    reattached = asyncio.run(
        provider.attach(workspace.workspace_id, str(repository), "HEAD")
    )

    assert reattached.workspace_id == workspace.workspace_id
    assert Path(reattached.root_path, "README.md").read_text() == "engine\n"


def test_attach_is_idempotent(tmp_path: Path, *, git_repo) -> None:
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    Path(workspace.root_path, "scratch.txt").write_text("mid-turn\n")

    reattached = asyncio.run(
        provider.attach(workspace.workspace_id, str(repository), "HEAD")
    )

    assert reattached.root_path == workspace.root_path
    # An attached workspace is left exactly as it stands, work in progress and all.
    assert Path(workspace.root_path, "scratch.txt").read_text() == "mid-turn\n"


def test_a_non_repository_is_reported_as_a_workspace_error(tmp_path: Path) -> None:
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))

    with pytest.raises(GitWorktreeError):
        asyncio.run(provider.provision(str(tmp_path), "HEAD"))


_CO_AUTHOR = "Ada Lovelace <ada@example.test>"


def _commit(checkout: Path, *arguments: str) -> str:
    _git(checkout, *_IDENTITY, "commit", "--allow-empty", *arguments)
    return _git(checkout, "log", "-1", "--format=%B")


def test_commits_credit_the_co_author_once(tmp_path: Path, *, git_repo) -> None:
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    hooks = repository / ".git" / "hooks"
    # The repository's own hooks keep running in the credited checkout.
    (hooks / "commit-msg").write_text('#!/bin/sh\necho "Checked-by: repo" >> "$1"\n')
    (hooks / "pre-commit").write_text('#!/bin/sh\ntouch "$(git rev-parse --git-dir)/pre-commit-ran"\n')
    for hook in hooks.iterdir():
        hook.chmod(0o755)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(
        provider.provision(str(repository), "HEAD", co_author=_CO_AUTHOR)
    )
    checkout = Path(workspace.root_path)

    first = _commit(checkout, "-m", "feat: work")
    amended = _commit(checkout, "--amend", "--no-edit")

    assert first.count(f"Co-authored-by: {_CO_AUTHOR}") == 1
    assert "Checked-by: repo" in first
    assert Path(_git(checkout, "rev-parse", "--absolute-git-dir"), "pre-commit-ran").exists()
    assert amended.count(f"Co-authored-by: {_CO_AUTHOR}") == 1
    # Only the workspace's checkout credits anyone.
    assert "Co-authored-by" not in _commit(repository, "-m", "chore: mine")


def test_reattaching_keeps_crediting_the_co_author(tmp_path: Path, *, git_repo) -> None:
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(
        provider.provision(str(repository), "HEAD", co_author=_CO_AUTHOR)
    )
    asyncio.run(provider.detach(workspace.workspace_id))

    reattached = asyncio.run(
        provider.attach(
            workspace.workspace_id, str(repository), "HEAD", co_author=_CO_AUTHOR
        )
    )

    message = _commit(Path(reattached.root_path), "-m", "feat: more")
    assert f"Co-authored-by: {_CO_AUTHOR}" in message


def test_the_detach_snapshot_credits_the_co_author(tmp_path: Path, *, git_repo) -> None:
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(
        provider.provision(str(repository), "HEAD", co_author=_CO_AUTHOR)
    )
    Path(workspace.root_path, "agent.md").write_text("what the agent did\n")

    asyncio.run(provider.detach(workspace.workspace_id))

    message = _git(repository, "log", "-1", "--format=%B", workspace.ref)
    assert message.count(f"Co-authored-by: {_CO_AUTHOR}") == 1


def test_no_co_author_adds_no_trailer(tmp_path: Path, *, git_repo) -> None:
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))

    assert "Co-authored-by" not in _commit(Path(workspace.root_path), "-m", "feat: x")


@pytest.mark.parametrize("issue_repo,reference", [("acme/api", "#7"), ("acme/other", "acme/other#7")])
def test_issue_references_survive_commit_amend_snapshot_and_reattach(
    tmp_path, issue_repo, reference, *, git_repo
):
    repository = tmp_path / "repository"
    git_repo(repository, commit=True)
    _git(repository, "remote", "add", "origin", "https://github.com/acme/api.git")
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    issue = {"repository": issue_repo, "number": 7}
    workspace = asyncio.run(provider.provision(str(repository), "main", co_author="Alice <alice@example.test>", issue=issue))
    root = Path(workspace.root_path)
    _git(root, *_IDENTITY, "commit", "--allow-empty", "-m", f"feat: change\n\nRefs {reference}")
    _git(root, *_IDENTITY, "commit", "--amend", "--allow-empty", "--no-edit")
    message = _git(root, "log", "-1", "--format=%B")
    assert message.count(f"Refs {reference}") == 1
    assert message.count("Co-authored-by: Alice <alice@example.test>") == 1
    (root / "new.txt").write_text("uncommitted")
    asyncio.run(provider.detach(workspace.workspace_id))
    workspace = asyncio.run(provider.attach(workspace.workspace_id, str(repository), "main", co_author="Alice <alice@example.test>", issue=issue))
    root = Path(workspace.root_path)
    assert f"Refs {reference}" in _git(root, "log", "-1", "--format=%B")
    _git(root, *_IDENTITY, "commit", "--allow-empty", "-m", "feat: follow up")
    assert _git(root, "log", "-1", "--format=%B").count(f"Refs {reference}") == 1
