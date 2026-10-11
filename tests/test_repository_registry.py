from concurrent.futures import ThreadPoolExecutor
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
    resolve = Mock(return_value="acme/new")
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
    assert resolve.call_count == 1
    assert len(registry.snapshot.repos) == 1
