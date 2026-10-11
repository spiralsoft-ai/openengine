"""Live repository configuration. Updates publish one immutable snapshot."""
from __future__ import annotations

import subprocess
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from types import MappingProxyType

from engine.runtime.change_requests import remote_project
from engine.runtime.config import EngineConfigError, parse_engine_config


@dataclass(frozen=True)
class RepositorySnapshot:
    repos: Mapping[str, str]
    repo_modes: Mapping[str, str]
    trusted_repos: frozenset[str]
    projects: Mapping[str, str]
    disconnected: frozenset[Path]
    trusted: frozenset[Path]
    login_repositories: tuple[str, ...]
    run_projects: Mapping[str, str]


class RepositoryRegistry:
    """Own configured checkouts and their access policy; never write configuration.

    Adding a GitHub checkout immediately lets its writers sign in. Existing
    names cannot be replaced. Readers may retain a snapshot across async work.
    """

    def __init__(
        self,
        repos: Mapping[str, str] | None = None,
        repo_modes: Mapping[str, str] | None = None,
        trusted_repos: Collection[str] = (),
        *,
        host_aliases: Collection[str] = (),
        projects: Mapping[str, str] | None = None,
        login_repositories: Collection[str] = (),
    ) -> None:
        self._lock = RLock()
        self._hosts = frozenset(host_aliases)
        self._extra_login = tuple(login_repositories)
        config = parse_engine_config({
            "repos": dict(repos or {}),
            "repo_modes": dict(repo_modes or {}),
            "trusted_repos": dict.fromkeys(trusted_repos, True),
        })
        resolved = dict(projects) if projects is not None else {
            name: project for name, path in {".": ".", **config.repos}.items()
            if (project := self._project(path)) is not None
        }
        self._snapshot = self._build(config.repos, config.repo_modes, config.trusted_repos, resolved)

    def _project(self, path: str) -> str | None:
        try:
            remote = subprocess.run(
                ["git", "-C", str(Path(path).expanduser()), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        project = remote_project(remote)
        if project is not None:
            host, _, rest = project.partition("/")
            if "/" not in rest or host in self._hosts:
                return project
        return None

    def _build(
        self, repos: Mapping[str, str], modes: Mapping[str, str],
        trusted: Collection[str], projects: Mapping[str, str],
    ) -> RepositorySnapshot:
        paths = {name: Path(path).expanduser().resolve() for name, path in repos.items()}
        run_projects = {}
        for name, project in projects.items():
            path = str(paths.get(name, Path(name).expanduser().resolve()))
            run_projects[name] = run_projects[path] = project.lower()
        return RepositorySnapshot(
            MappingProxyType(dict(repos)), MappingProxyType(dict(modes)), frozenset(trusted),
            MappingProxyType(dict(projects)),
            frozenset(paths[name] for name in repos if modes.get(name) == "disconnected"),
            frozenset(paths[name] for name in trusted),
            tuple(dict.fromkeys((*self._extra_login, *(projects[n] for n in repos if n in projects)))),
            MappingProxyType(run_projects),
        )

    @property
    def snapshot(self) -> RepositorySnapshot:
        with self._lock:
            return self._snapshot

    def add(self, name: str, path: str, mode: str | None = None, *, trusted: bool = False) -> None:
        """Validate like config loading, resolve origin once, then publish atomically."""
        config = parse_engine_config({
            "repos": {name: path},
            "repo_modes": {} if mode is None else {name: mode},
            "trusted_repos": {name: trusted},
        })
        if name in self.snapshot.repos:
            message = f"repos.{name} already exists"
            raise EngineConfigError(message)
        # Git may block; readers must remain free to acquire the snapshot lock.
        project = self._project(path)
        with self._lock:
            previous = self._snapshot
            if name in previous.repos:
                message = f"repos.{name} already exists"
                raise EngineConfigError(message)
            projects = dict(previous.projects)
            if project is not None:
                projects[name] = project
            self._snapshot = self._build(
                {**previous.repos, **config.repos}, {**previous.repo_modes, **config.repo_modes},
                previous.trusted_repos | config.trusted_repos, projects,
            )
