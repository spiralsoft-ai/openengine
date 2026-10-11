from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import Mock

import pytest

from engine.runtime.config import EngineConfigError
from engine.runtime.repositories import RepositoryRegistry


def test_concurrent_adds_publish_complete_snapshots(monkeypatch, tmp_path):
    resolve = Mock(side_effect=lambda path: f"acme/{path.rsplit('/', 1)[-1]}")
    monkeypatch.setattr(RepositoryRegistry, "_project", lambda self, path: resolve(path))
    registry = RepositoryRegistry(projects={})
    before = registry.snapshot

    def add(index):
        registry.add(str(index), str(tmp_path / str(index)), mode="disconnected", trusted=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(40)))
    snapshot = registry.snapshot
    assert before.repos == {}
    assert len(snapshot.repos) == len(snapshot.projects) == len(snapshot.login_repositories) == 40
    assert snapshot.disconnected == snapshot.trusted == frozenset(tmp_path / str(i) for i in range(40))
    assert len(snapshot.trusted_repos) == 40
    assert resolve.call_count == 40
    with pytest.raises(EngineConfigError, match="already exists"):
        registry.add("0", "/replacement")
    assert registry.snapshot is snapshot


@pytest.mark.parametrize("name,path,options", [
    ("", "/repo", {}), ("repo", "", {}), ("repo", "/repo", {"mode": "offline"}),
    ("repo", "/repo", {"trusted": "yes"}),
])
def test_invalid_add_does_not_publish(name, path, options):
    registry = RepositoryRegistry(projects={})
    before = registry.snapshot
    with pytest.raises(EngineConfigError):
        registry.add(name, path, **options)
    assert registry.snapshot is before


def test_concurrent_duplicate_add_has_one_winner(monkeypatch):
    barrier = Barrier(8)

    def project(path):
        barrier.wait(timeout=5)
        return "acme/new"

    resolve = Mock(side_effect=project)
    monkeypatch.setattr(RepositoryRegistry, "_project", lambda self, path: resolve(path))
    registry = RepositoryRegistry(projects={})

    def add(index):
        try:
            registry.add("new", f"/repo/{index}")
        except EngineConfigError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(add, range(20))) == 1
    assert resolve.call_count == 8
    assert len(registry.snapshot.repos) == 1


@pytest.mark.parametrize("other_name", ["slow", "other"])
def test_origin_resolution_does_not_block_readers_or_adds(monkeypatch, tmp_path, other_name):
    resolving = Event()
    release = Event()
    slow_path = str(tmp_path / "slow")
    other_path = str(tmp_path / "other")

    def project(self, path):
        if path == slow_path:
            resolving.set()
            assert release.wait(timeout=5)
        return f"acme/{Path(path).name}"

    monkeypatch.setattr(RepositoryRegistry, "_project", project)
    registry = RepositoryRegistry(projects={})
    before = registry.snapshot
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(registry.add, "slow", slow_path)
        try:
            assert resolving.wait(timeout=5)
            assert pool.submit(lambda: registry.snapshot).result(timeout=1) is before
            pool.submit(
                registry.add, other_name, other_path, mode="disconnected", trusted=True,
            ).result(timeout=1)
        finally:
            release.set()
        if other_name == "slow":
            with pytest.raises(EngineConfigError, match="already exists"):
                slow.result(timeout=5)
        else:
            slow.result(timeout=5)

    snapshot = registry.snapshot
    assert snapshot.repos[other_name] == other_path
    assert snapshot.projects[other_name] == "acme/other"
    assert snapshot.disconnected == snapshot.trusted == frozenset({tmp_path / "other"})
    assert snapshot.trusted_repos == frozenset({other_name})
    assert snapshot.login_repositories == (
        ("acme/other",) if other_name == "slow" else ("acme/other", "acme/slow")
    )
    assert len(snapshot.repos) == (1 if other_name == "slow" else 2)
