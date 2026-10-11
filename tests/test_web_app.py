"""The assistant-ui server surface and its multi-chat coordination."""

import asyncio
import gzip
import json
import logging
import re
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from web_fakes import ConcurrentRunner, ConversationWorkspaces

import httpx
import pytest
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from engine.adapters.agent_runner.acp import ACPAgentRunner
from engine.adapters.communications.slack import SlackCommunications
from engine.adapters.state_store.memory import InMemoryStateStore
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.apps.web.__main__ import build_app
from engine.apps.web.api import ApprovalFeed, ThreadService
from engine.apps.web.github_login import GitHubLogin, GitHubLoginConfig
from engine.apps.web.utilization import (
    RunnerUtilization,
    UtilizationService,
    UtilizationWindow,
)
from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_communications,
    build_read_only_runners,
    build_runners,
    build_session,
    claude_session_config_for,
)
from engine.domain import (
    AgentId,
    AgentProfile,
    AgentRunId,
    ApprovalDecision,
    ApprovalId,
    ApprovalKind,
    Message,
    Role,
    RunId,
    RunPhase,
    RunState,
    TaskId,
    ToolCall,
    WorkflowId,
)
from engine.ports import (
    AgentTurn,
    ApprovalRequest,
    InteractiveAgentRunner,
    Workspace,
    WorkspaceState,
)
from engine.runtime import (
    PLANNER,
    AgentSession,
    ApprovalBroker,
    ApprovalCapability,
    ApprovalConfig,
    Capabilities,
    ClaudeConfig,
    CommunicationsConfig,
    EngineConfig,
    ResponseStyle,
    WorkflowCatalog,
)
from engine.graph_runtime import (
    CANCELLED,
    CheckpointId,
    GraphCompilationError,
    GraphId,
    NodeId,
    RunSnapshot,
    RunStatus,
)
from graph_runtime_fakes import (
    Ask,
    AwaitSteering,
    Fail,
    Say,
    ScriptedGraph,
    ScriptedGraphRuntime,
    ScriptedNode,
)

CODER = AgentId("coder")
PROFILES = {
    CODER: AgentProfile(
        agent_id=CODER,
        instructions="Be terse.",
        description="Reads code.",
    )
}


def test_web_composes_the_sqlite_conversation_store(tmp_path) -> None:
    database = tmp_path / "conversations.sqlite3"

    capabilities = build_capabilities(Settings(sqlite_path=str(database)))

    assert isinstance(capabilities.state_store, SQLiteStateStore)
    assert database.exists()
    capabilities.state_store.close()


def test_web_selects_the_configured_communications_provider() -> None:
    slack = build_communications(Settings())

    assert isinstance(slack, SlackCommunications)

    with pytest.raises(RuntimeError, match="provider 'buzz' is not available yet"):
        build_communications(
            Settings(
                engine_config=EngineConfig(
                    communications=CommunicationsConfig(provider="buzz")
                )
            )
        )


def test_repository_choices_reach_the_web_config(
    tmp_path, monkeypatch, *, async_client
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "engine.toml"
    path.write_text('[repos]\n"OpenEngine/OpenEngine" = "~/code/OpenEngine"\nn8n = "code/n8n"\n')
    (tmp_path / "code/OpenEngine").mkdir(parents=True)
    app = build_app(path)

    async def ask():
        async with async_client(app, base_url="http://test") as client:
            return (await client.get("/api/config")).json()

    assert asyncio.run(ask())["repositories"] == [
        {"name": "OpenEngine/OpenEngine", "path": str(tmp_path / "code/OpenEngine")},
        {"name": "n8n", "path": str(tmp_path / "code/n8n")},
    ]


def test_the_application_can_be_built_from_configuration_alone(
    tmp_path, monkeypatch, *, async_client
) -> None:
    """The contract the development server's reloader depends on.

    It constructs the application again in every child process it starts, with
    no command line and nothing handed to it, so a composition that only works
    when `main` assembles it would leave `engine-dev` reloading into nothing.
    """
    monkeypatch.chdir(tmp_path)

    monkeypatch.delenv("ENGINE_CONFIG", raising=False)
    app = build_app()
    async def ask() -> httpx.Response:
        async with async_client(app, base_url="http://test") as client:
            return await client.get("/api/config")

    answered = asyncio.run(ask())
    assert answered.status_code == 200
    assert "showProjects" not in answered.json()
    assert answered.json()["repositories"] == [{"name": f". ({tmp_path})", "path": "."}]
    assert answered.json()["runners"] == [
        {"id": "codex", "implementation": "ACPAgentRunner"},
        {"id": "claude", "implementation": "ACPAgentRunner"},
        {"id": "opencode", "implementation": "ACPAgentRunner"},
    ]
    # Composed from the working directory, exactly as `engine-web` composes it.
    assert (tmp_path / "conversations.sqlite3").exists()


def _claude_options(runner: ACPAgentRunner) -> dict:
    return runner.session_config_for(PROFILES[CODER])["claudeCode"]["options"]


def test_web_offers_one_interactive_runner_per_agent() -> None:
    runners = build_runners(Settings())

    assert tuple(runners) == ("codex", "claude", "opencode")
    for name, runner in runners.items():
        assert isinstance(runner, ACPAgentRunner)
        assert runner.provider.name == name
        # Which of them pause is what decides whether a run brokers approvals,
        # so it is read off the port rather than off the class name.
        assert isinstance(runner, InteractiveAgentRunner)


def test_no_composition_root_builds_a_cli_runner(tmp_path) -> None:
    """Chat, review, and the unattended runner all reach their agent over ACP."""
    from engine.apps.control_server.composition import (
        Settings as ControlServerSettings,
    )
    from engine.apps.control_server.composition import (
        build_capabilities as build_control_server,
    )
    from engine.apps.worker.composition import Settings as WorkerSettings
    from engine.apps.worker.composition import build_capabilities as build_worker

    capabilities = build_capabilities(Settings(sqlite_path=str(tmp_path / "c.sqlite3")))
    try:
        web = capabilities.agent_runner
    finally:
        capabilities.state_store.close()
    runners = (
        web,
        *build_runners(Settings()).values(),
        *build_read_only_runners(Settings()).values(),
        build_control_server(ControlServerSettings()).agent_runner,
        build_worker(WorkerSettings()).agent_runner,
    )

    assert all(isinstance(runner, ACPAgentRunner) for runner in runners)


def test_the_runner_nobody_is_watching_stays_read_only(tmp_path) -> None:
    """The port implementation a non-interactive caller reaches has nobody to
    ask, and runs in Codex's read-only sandbox."""
    capabilities = build_capabilities(Settings(sqlite_path=str(tmp_path / "c.sqlite3")))
    try:
        runner = capabilities.agent_runner
    finally:
        capabilities.state_store.close()

    assert runner.provider.name == "codex"
    assert runner.provider.env["INITIAL_AGENT_MODE"] == "read-only"
    assert runner.provider.env["ENGINE_CODEX_SANDBOX"] == "read-only"


def test_interactive_runners_may_do_what_the_user_approves() -> None:
    """A gate is only a gate if what it lets through can then happen."""
    runners = build_runners(Settings())

    # Codex: writable inside the worktree, and stopping to ask a person before
    # it would step outside one -- never a model approving on their behalf.
    assert runners["codex"].provider.env["INITIAL_AGENT_MODE"] == "read-only"
    assert runners["codex"].provider.env["ENGINE_CODEX_SANDBOX"] == "workspace-write"
    # Claude: reads run unattended, everything else reaches the user.
    options = _claude_options(runners["claude"])
    assert options["allowedTools"] == ["Read", "Glob", "Grep"]
    assert "tools" not in options
    # OpenCode: asks before any change, where by default it would not.
    permissions = _opencode_permissions(runners["opencode"])
    assert permissions["*"] == permissions["external_directory"] == "ask"


def _opencode_permissions(runner: ACPAgentRunner) -> dict:
    return json.loads(runner.provider.env["OPENCODE_CONFIG_CONTENT"])["permission"]


def test_the_configured_policy_builds_the_interactive_claude_runner() -> None:
    """`engine.toml` is where chat's permissions are written down.

    A preapproved tool is one whose requests never reach the callback at all,
    which is the only thing a provider allow-list can express. Shell stays off
    it however granted: a shell rule is written per command, and the patterns
    live where the requests arrive.
    """
    granted = EngineConfig(
        approvals=ApprovalConfig(
            allow=(ApprovalCapability.READ, ApprovalCapability.EDIT, ApprovalCapability.BASH)
        )
    )
    options = _claude_options(build_runners(Settings(engine_config=granted))["claude"])

    assert options["allowedTools"] == ["Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit"]
    assert "Bash" not in options["allowedTools"]


def test_the_interactive_codex_preset_is_not_widened_by_the_policy() -> None:
    """Codex's policy is applied to its requests, not to its preset."""
    auto = EngineConfig(approvals=ApprovalConfig(auto_approve=True))
    runner = build_runners(Settings(engine_config=auto))["codex"]

    assert runner.provider.env["INITIAL_AGENT_MODE"] == "read-only"
    assert runner.provider.env["ENGINE_CODEX_SANDBOX"] == "workspace-write"


def test_engine_config_styles_every_claude_runner_this_process_offers() -> None:
    """Chat and review alike: a style is a property of the runner rather than
    of the errand it is sent on."""
    settings = Settings(
        engine_config=EngineConfig(claude=ClaudeConfig(output_style=ResponseStyle.CONCISE))
    )

    for build in (build_runners, build_read_only_runners):
        options = _claude_options(build(settings)["claude"])
        assert options["settings"]["outputStyle"] == "Concise"


def test_engine_config_produces_claude_session_config_for_acp_runners() -> None:
    """The same attribution and style settings that reach the CLI runners also
    produce a session config for the ACP graph runners."""
    settings = Settings(
        engine_config=EngineConfig(
            attribution=False,
            claude=ClaudeConfig(output_style=ResponseStyle.CONCISE),
        )
    )
    config = claude_session_config_for(settings)
    assert config is not None
    assert config["claudeCode"]["options"]["settings"]["attribution"]["commit"] == ""
    assert config["claudeCode"]["options"]["settings"]["outputStyle"] == "Concise"


def test_default_engine_config_produces_no_session_config() -> None:
    assert claude_session_config_for(Settings()) is None


def test_a_planning_chat_is_answered_by_the_runner_that_cannot_write(tmp_path) -> None:
    """Half of the Plan button's difference from New chat: the tool set.

    Same provider the user picked, same conversation machinery, and a tool set
    without the tools to change the checkout it is reading -- a property of what
    the composition hands the planner rather than of its instructions.

    Only half, and the docstring says so deliberately: this proves the planner
    is *handed* less, not that it is *held* to less. A provider asking anyway
    reaches the approval broker, where a policy granting `edit` would allow it;
    what refuses it there is `read_only` on the profile, covered by
    `test_approvals.py`. Either half alone reads like the whole thing, which is
    how a claim like this one comes to be believed without being true.
    """
    settings = Settings(
        engine_config=EngineConfig(
            approvals=ApprovalConfig(
                allow=(ApprovalCapability.READ, ApprovalCapability.EDIT)
            )
        ),
        sqlite_path=str(tmp_path / "conversations.sqlite3"),
    )
    capabilities = build_capabilities(settings)
    try:
        session = build_session(
            capabilities,
            build_runners(settings),
            read_only_runners=build_read_only_runners(settings),
        )
        planner = session.runner_for(PLANNER.agent_id, "claude")
        coder = session.runner_for(CODER, "claude")
    finally:
        capabilities.state_store.close()

    planner_options = planner.session_config_for(PLANNER)["claudeCode"]["options"]
    coder_options = coder.session_config_for(PROFILES[CODER])["claudeCode"]["options"]

    assert planner_options["tools"] == ["Read", "Glob", "Grep"]
    assert planner_options["allowedTools"] == ["Read", "Glob", "Grep"]
    assert "tools" not in coder_options
    assert "Edit" in coder_options["allowedTools"]
    codex = session.runner_for(PLANNER.agent_id, "codex")
    assert codex is not session.runner_for(CODER, "codex")
    assert codex.provider.env["INITIAL_AGENT_MODE"] == "read-only"
    assert codex.provider.env["ENGINE_CODEX_SANDBOX"] == "read-only"
    opencode = session.runner_for(PLANNER.agent_id, "opencode")
    permissions = _opencode_permissions(opencode)
    assert permissions["*"] == permissions["external_directory"] == "deny"


def test_review_comments_reach_the_github_api(tmp_path) -> None:
    """Comments are posted via the GitHub API, not via the gh CLI.

    Proved by intercepting the HTTP request rather than by reading constructor
    arguments back: what matters is that the adapter actually calls the right
    endpoint with the right payload.
    """
    recorded: list[httpx.Request] = []

    async def fake_api(
        self,
        method: str,
        path: str,
        **kwargs: object,
    ) -> dict:
        recorded.append(httpx.Request(method, f"https://api.github.com{path}"))
        return {"id": 123, "html_url": "https://github.com/acme/api/pull/7#issuecomment-123"}

    from engine.adapters.source_control.github import GitHubSourceControl
    from unittest.mock import patch

    capabilities = build_capabilities(Settings(sqlite_path=str(tmp_path / "c.sqlite3")))
    try:
        with patch.object(GitHubSourceControl, "_api", fake_api):
            asyncio.run(
                capabilities.source_control.add_comment(
                    "https://github.com/acme/api/pull/7", "Looks right."
                )
            )
    finally:
        capabilities.state_store.close()

    assert len(recorded) == 1
    assert recorded[0].method == "POST"
    assert "/repos/acme/api/issues/7/comments" in str(recorded[0].url)


def test_web_restores_sqlite_conversations_after_restart(
    async_client, tmp_path, *, web_app
) -> None:
    database = tmp_path / "conversations.sqlite3"
    runner = ConcurrentRunner()
    other_runner = ConcurrentRunner(("persisted answer",))
    runners = {"test": runner, "other": other_runner}

    first_capabilities = build_capabilities(Settings(sqlite_path=str(database)))
    first_app = web_app(
        AgentSession(first_capabilities, profiles=PROFILES, runners=runners),
        runners,
    )

    async def first_process() -> str:
        async with async_client(first_app, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"text": "remember this", "runner": "other"},
            )
            renamed = await client.patch(
                f"/api/threads/{thread_id}",
                json={"title": "Persistent metadata"},
            )
            archived = await client.post(f"/api/threads/{thread_id}/archive")
            assert renamed.status_code == 200
            assert archived.status_code == 200
            return thread_id

    thread_id = asyncio.run(first_process())
    first_capabilities.state_store.close()

    second_capabilities = build_capabilities(Settings(sqlite_path=str(database)))
    second_app = web_app(
        AgentSession(second_capabilities, profiles=PROFILES, runners=runners),
        runners,
    )

    async def second_process():
        async with async_client(second_app, base_url="http://test") as client:
            threads = await client.get("/api/threads")
            messages = await client.get(f"/api/threads/{thread_id}/messages")
            return threads, messages

    try:
        threads, messages = asyncio.run(second_process())
    finally:
        second_capabilities.state_store.close()

    assert threads.json()["threads"] == [
        {
            "id": thread_id,
            "title": "Persistent metadata",
            "archived": True,
            "agentId": "coder",
            "runner": "other",
            "workspaceAttached": False,
        }
    ]
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [("user", "remember this"), ("assistant", "persisted answer")]


def _session(runner: ConcurrentRunner) -> AgentSession:
    return _session_with({"test": runner})


def _session_with(
    runners: Mapping[str, ConcurrentRunner],
    profiles: Mapping[AgentId, AgentProfile] = PROFILES,
    state_store: InMemoryStateStore | None = None,
) -> AgentSession:
    unused = object()
    return AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=next(iter(runners.values())),
            communications=unused,
            workspace_provider=unused,
            state_store=state_store or InMemoryStateStore(),
        ),
        profiles=profiles,
        runners=dict(runners),
    )


async def _await_phase(
    client: httpx.AsyncClient, run_id: RunId, phase: str
) -> httpx.Response:
    """Poll a run until it reaches `phase`, or return the last view it had."""
    for _ in range(200):
        response = await client.get(f"/api/runs/{run_id}")
        if response.json()["phase"] == phase:
            return response
        await asyncio.sleep(0.01)
    return response


def _work_order(
    phase: RunPhase = RunPhase.RUNNING_AGENT, *, failure_reason: str = ""
) -> RunState:
    """The row a graph WorkOrder is listed and opened by."""
    return RunState(
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        workflow_id=WorkflowId("implementation-review-rerank"),
        phase=phase,
        repository="acme/api",
        prompt="Add cancellation handling.",
        name="Add cancellation handling",
        failure_reason=failure_reason,
    )


def test_deleting_a_run_forgets_it(async_client, workflow_app) -> None:
    """The rail's × on a WorkOrder is not the project row's archive.

    Nothing lists or restores what it removes, so the row goes for good.
    """
    store = InMemoryStateStore()
    state = _work_order(RunPhase.SUCCEEDED)
    asyncio.run(store.save(state))
    app = workflow_app(store, ConcurrentRunner())

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            deleted = await client.delete(f"/api/runs/{state.run_id}")
            listed = await client.get("/api/runs")
            detail = await client.get(f"/api/runs/{state.run_id}")
            again = await client.delete(f"/api/runs/{state.run_id}")
            return deleted, listed, detail, again

    deleted, listed, detail, again = asyncio.run(scenario())

    assert deleted.status_code == 204
    assert listed.json()["runs"] == []
    assert detail.status_code == 404
    # A second × on a row the poll has not cleared yet says the same thing the
    # page does, rather than pretending to delete it twice.
    assert again.status_code == 404
    assert asyncio.run(store.load(state.run_id)) is None


def test_utilization_is_served_from_the_cache_and_then_scraped(
    async_client, tmp_path, *, workflow_app
) -> None:
    """The two calls the page makes, and why there are two of them.

    Opening it must draw something before either provider answers, so the cache
    is a read of its own that touches no network; the scrape that follows is
    what replaces the figures with today's.
    """
    reading = RunnerUtilization(
        runner="claude",
        plan="max",
        windows=(
            UtilizationWindow("five_hour", "5-hour", 12.0, "2026-09-08T20:10:00+00:00"),
            UtilizationWindow("seven_day", "Weekly", 41.0, "2026-09-10T02:00:00+00:00"),
        ),
    )

    async def read_claude(_client) -> RunnerUtilization:
        return reading

    utilization = UtilizationService(
        cache_path=tmp_path / "utilization.json", readers={"claude": read_claude}
    )
    app = workflow_app(
        InMemoryStateStore(),
        ConcurrentRunner(),
        runners={"claude": ConcurrentRunner(), "codex": ConcurrentRunner()},
        utilization=utilization,
    )

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            empty = await client.get("/api/utilization")
            scraped = await client.post("/api/utilization/refresh")
            cached = await client.get("/api/utilization")
            return empty, scraped, cached

    empty, scraped, cached = asyncio.run(scenario())

    # Nothing has been read yet, which is a page with no meters rather than an
    # error: the scrape is what fills it.
    assert empty.status_code == 200
    assert empty.json() == {"runners": []}
    assert scraped.status_code == 200
    listed = scraped.json()["runners"]
    # Only the runner something knows how to read, even though the deployment
    # offers two.
    assert [entry["runner"] for entry in listed] == ["claude"]
    assert listed[0]["plan"] == "max"
    assert [window["label"] for window in listed[0]["windows"]] == ["5-hour", "Weekly"]
    assert [window["usedPercent"] for window in listed[0]["windows"]] == [12.0, 41.0]
    # And the next open starts from what the scrape found, without asking again.
    assert cached.json() == scraped.json()


def test_utilization_refresh_refuses_a_cross_origin_page(
    async_client, tmp_path, *, workflow_app
) -> None:
    """It reads the tokens the runners signed in with, so it is guarded like
    every other endpoint that touches a stored credential."""
    asked = False

    async def read_claude(_client) -> RunnerUtilization:
        nonlocal asked
        asked = True
        return RunnerUtilization(runner="claude")

    app = workflow_app(
        InMemoryStateStore(),
        ConcurrentRunner(),
        runners={"claude": ConcurrentRunner()},
        utilization=UtilizationService(
            cache_path=tmp_path / "utilization.json", readers={"claude": read_claude}
        ),
    )

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            return await client.post(
                "/api/utilization/refresh", headers={"origin": "https://elsewhere.example"}
            )

    refused = asyncio.run(scenario())

    assert refused.status_code == 403
    assert not asked


def test_run_list_leaves_the_prose_to_the_run_it_names(
    async_client, workflow_app
) -> None:
    """Every screen polls `/api/runs` once a second to keep its rail current.

    What that list carries is what every screen pays for, on a payload that
    grows with every run ever started -- so the words an agent wrote stay with
    the single run the page showing them asks for, and the list keeps what a
    rail, a card and a milestone's task list read.
    """
    store = InMemoryStateStore()
    state = _work_order(RunPhase.FAILED, failure_reason="the reviewer gave up")
    asyncio.run(store.save(state))
    app = workflow_app(store, ConcurrentRunner())

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            return (
                await client.get("/api/runs"),
                await client.get(f"/api/runs/{state.run_id}"),
            )

    listed, detail = asyncio.run(scenario())
    (run,) = listed.json()["runs"]
    body = detail.json()

    # The requester, like the prose, is drawn only by the WorkOrder page.
    prose = {"taskPrompt", "failureReason", "requester"}
    assert prose.isdisjoint(run)
    assert prose <= set(body)
    # What the rail and the WorkOrder cards do read, which is how far the
    # listing can be trimmed before a screen loses something it draws.
    # Usage is summed from the run's events, so only the page about it pays.
    assert run == {
        key: value for key, value in body.items() if key not in prose | {"usage"}
    }
    assert body["failureReason"] == "the reviewer gave up"
    assert body["usage"]["costUsd"] is None and body["usage"]["nodes"] == {}

def test_approval_feed_replays_and_pushes_broker_transitions() -> None:
    store = InMemoryStateStore()
    feed = ApprovalFeed(store)
    broker = ApprovalBroker(store, observe=feed.publish)

    async def scenario():
        instance = await store.create_instance(CODER)
        stream = feed.stream(instance.instance_id)
        assert await anext(stream) == b": connected\n\n"

        handler = broker.handler(
            agent_run_id=AgentRunId("ar-feed"),
            instance_id=instance.instance_id,
            runner="test",
            present=lambda _approval: asyncio.sleep(0),
        )
        waiting = asyncio.create_task(
            handler(
                ApprovalRequest(
                    approval_id="provider-approval",
                    kind=ApprovalKind.COMMAND_EXECUTION,
                    reason="Run the test suite",
                    command="pytest",
                    cwd="/workspace",
                )
            )
        )
        pending = await asyncio.wait_for(anext(stream), timeout=1)
        record = (await store.list_approvals())[0]
        await broker.decide(
            record.approval_id,
            ApprovalDecision.ACCEPT,
            instance_id=instance.instance_id,
            agent_run_id=AgentRunId("ar-feed"),
        )
        decided = await asyncio.wait_for(anext(stream), timeout=1)
        await waiting
        await stream.aclose()
        return [
            json.loads(frame.decode().removeprefix("data:"))
            for frame in (pending, decided)
        ]

    events = asyncio.run(scenario())

    assert [event["status"] for event in events] == ["pending", "decided"]
    assert events[0]["id"] == events[1]["id"]
    assert events[1]["decision"] == "accept"


@pytest.mark.parametrize(
    "body",
    [
        {"workflowId": "unknown-v1", "prompt": "Task", "repository": "."},
        {"workflowId": "implementation-review-v1", "prompt": "", "repository": "."},
        {"workflowId": "implementation-review-v1", "prompt": "Task"},
        {
            "workflowId": "implementation-review-v1",
            "prompt": "Task",
            "repository": ".",
            "runner": "unknown",
        },
    ],
)
def test_create_workflow_run_rejects_invalid_requests(
    async_client, body: dict[str, str], *, workflow_app
) -> None:
    store = InMemoryStateStore()
    app = workflow_app(store, ConcurrentRunner())

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            return await client.post("/api/runs", json=body)

    response = asyncio.run(scenario())

    assert response.status_code == 400
    assert asyncio.run(store.list_runs()) == ()


def test_create_workflow_run_records_the_signed_in_requester(
    async_client, tmp_path, *, sqlite_store, workflow_app
) -> None:
    """A WorkOrder started from the web UI names its GitHub account, and still
    does after a restart."""
    path = tmp_path / "requester.sqlite3"
    store = sqlite_store(path)
    runtime = ScriptedGraphRuntime(_review_graph())
    app = _graph_app_over(
        store, runtime, _review_graph(),
        github_login_config=GitHubLoginConfig(
            "client", "secret", "https://engine.test/api/auth/github/callback"
        ),
     workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="https://engine.test") as client:
            # The login middleware wraps the app whose lifespan starts the engine.
            async with app.app.router.lifespan_context(app.app):
                return await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Document the milestone.",
                        "repository": ".",
                    },
                )

    with patch.object(
        GitHubLogin, "_read_session", return_value={"id": 42, "login": "alice"}
    ), patch.object(GitHubLogin, "has_access", AsyncMock(return_value=True)), patch.object(
        GitHubLogin, "visible_repositories", AsyncMock(return_value=None)
    ):
        created = asyncio.run(scenario())

    assert created.status_code == 201, created.text
    assert created.json()["requester"] == "github:42:alice"
    # Handed to the graph, whose workspace credits them on every commit.
    started = asyncio.run(runtime.snapshot(RunId(created.json()["runId"])))
    assert started.values["coAuthor"] == "alice <42+alice@users.noreply.github.com>"
    store.close()
    reopened = sqlite_store(path)
    try:
        (run,) = asyncio.run(reopened.list_runs())
        assert run.requester == "github:42:alice"
    finally:
        reopened.close()


#: The dev server's proxy table. TypeScript because Vite is what reads it, so
#: this is the one list about this application that cannot be imported.
PROXY_SOURCE = Path(__file__).resolve().parent.parent / "apps/web/src/api-proxy.ts"


def _proxied_prefixes() -> set[str]:
    """`PROXIED_PREFIXES`, read out of the source rather than restated here.

    Read the way `layout.py` reads `capabilities.py`: a second copy of a list
    that must not drift is the thing that drifts.
    """
    source = PROXY_SOURCE.read_text()
    listing = re.search(r"PROXIED_PREFIXES\s*=\s*\[(.*?)\]", source, re.DOTALL)
    assert listing is not None, f"no PROXIED_PREFIXES in {PROXY_SOURCE}"
    return set(re.findall(r'"([^"]+)"', listing.group(1)))


def test_every_prefix_this_application_serves_is_one_the_dev_server_forwards(
    web_app,
) -> None:
    """The failure this is here for is silent, and only in development.

    `apps/web/vite.config.ts` forwards the prefixes it was told about and
    answers everything else with `index.html` and a 200, so a prefix this
    application serves and the proxy has not heard of does not arrive as a 404
    -- the client gets a page where it asked for JSON, and reports a parse
    error. Every other test in this file talks to the application directly and
    cannot see it. That is how `/graph` was served, read by the client, and
    unproxied for two releases.

    Composed without a static directory, so what is left is the surface that is
    not the client's own: the SPA's pages are Vite's to answer and must not be
    forwarded.
    """
    app = web_app(_session(ConcurrentRunner()), {"test": ConcurrentRunner()})

    served = {
        "/" + route.path.lstrip("/").split("/")[0]
        for route in app.routes
        # The placeholder page for a checkout with no build, which is the
        # client's address rather than this application's.
        if route.path != "/"
    }

    # Containment rather than equality in both directions: adding a prefix to
    # both sides is the correct change and must stay green, and a test that
    # went red for it would be edited into agreement without being read.
    assert {"/api", "/graph"} <= served
    assert served <= _proxied_prefixes()


def test_run_id_frontend_route_serves_the_application(
    async_client, tmp_path, *, web_app
) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<main>workflow application</main>")
    app = web_app(_session(ConcurrentRunner()), {"test": ConcurrentRunner()}, static)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            return await client.get("/runs/run-42")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert "workflow application" in response.text


def test_new_workflow_frontend_route_serves_the_application(
    async_client, tmp_path, *, web_app
) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<main>workflow application</main>")
    app = web_app(_session(ConcurrentRunner()), {"test": ConcurrentRunner()}, static)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            return await client.get("/runs/new")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert "workflow application" in response.text


class VanishingWorkspaces(ConversationWorkspaces):
    """A provider that has never heard of a workspace the store still names."""

    def __init__(self) -> None:
        super().__init__()
        self.forgotten: set[str] = set()

    async def root_path(self, workspace_id: str) -> str:
        if workspace_id in self.forgotten:
            raise KeyError(f"no workspace {workspace_id!r}")
        return await super().root_path(workspace_id)

    async def state(self, workspace_id: str) -> WorkspaceState:
        if workspace_id in self.forgotten:
            raise KeyError(f"no workspace {workspace_id!r}")
        return await super().state(workspace_id)


def _workspace_session(
    runner: ConcurrentRunner,
    workspaces: ConversationWorkspaces,
    store: InMemoryStateStore | None = None,
    workspace_repository: str = "/repository",
) -> AgentSession:
    unused = object()
    return AgentSession(
        Capabilities(
            workflow_runtime=unused,
            source_control=unused,
            agent_runner=runner,
            communications=unused,
            workspace_provider=workspaces,
            state_store=store if store is not None else InMemoryStateStore(),
        ),
        profiles=PROFILES,
        runners={"test": runner},
        workspace_repository=workspace_repository,
    )


def test_each_new_chat_reports_its_own_worktree(async_client, web_app) -> None:
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    session = _workspace_session(runner, workspaces)
    app = web_app(session, {"test": runner})

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        async with async_client(app, base_url="http://test") as client:
            body = {"agentId": "coder", "runner": "test"}
            first = await client.post("/api/threads", json=body)
            second = await client.post("/api/threads", json=body)
            await client.post(
                f"/api/threads/{first.json()['id']}/runs", json={"text": "inspect"}
            )
            return first.json(), second.json()

    first, second = asyncio.run(scenario())

    assert first["workspaceRoot"] == "/worktrees/ws-1"
    assert second["workspaceRoot"] == "/worktrees/ws-2"
    assert first["workspaceRoot"] != second["workspaceRoot"]
    assert runner.workspace_ids == ["ws-1"]


def test_a_removed_worktree_does_not_take_the_other_chats_with_it(
    async_client, web_app
) -> None:
    """One vanished checkout used to brick every endpoint, new chats included."""
    runner = ConcurrentRunner()
    workspaces = VanishingWorkspaces()
    store = InMemoryStateStore()
    first_app = web_app(
        _workspace_session(runner, workspaces, store), {"test": runner}
    )

    async def scenario():
        async with async_client(first_app, base_url="http://test") as client:
            abandoned = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
        workspaces.forgotten.add("ws-1")

        # A restart: the registry is rebuilt from the store, whose instances
        # still name a workspace that is no longer on disk.
        restarted = web_app(
            _workspace_session(runner, workspaces, store), {"test": runner}
        )
        async with async_client(restarted, base_url="http://test") as client:
            listed = await client.get("/api/threads")
            survivor = await client.get(f"/api/threads/{abandoned.json()['id']}")
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            fresh = await client.get(f"/api/threads/{created.json()['id']}")
        return listed, survivor, created, fresh

    listed, survivor, created, fresh = asyncio.run(scenario())

    assert listed.status_code == 200
    assert survivor.status_code == 200
    assert "workspaceRoot" not in survivor.json()
    assert survivor.json()["workspaceAttached"] is False
    assert created.status_code == 201
    assert fresh.status_code == 200
    assert fresh.json()["workspaceRoot"] == "/worktrees/ws-2"


def test_detaching_keeps_the_work_reachable_and_reattaching_brings_it_back(
    async_client, web_app,
) -> None:
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    app = web_app(_workspace_session(runner, workspaces), {"test": runner})

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            detached = await client.delete(f"/api/threads/{thread_id}/workspace")
            listed_detached = await client.get(f"/api/threads/{thread_id}")
            reattached = await client.post(f"/api/threads/{thread_id}/workspace")
        return created.json(), detached.json(), listed_detached.json(), reattached.json()

    created, detached, listed, reattached = asyncio.run(scenario())

    assert created["workspaceAttached"] is True
    assert detached["workspaceAttached"] is False
    assert "workspaceRoot" not in detached
    # The work stays addressable while there is nowhere to run it.
    assert detached["workspaceRef"] == "engine/ws-1"
    assert listed["workspaceAttached"] is False
    # Reattaching is the same workspace, not a replacement for it.
    assert reattached["workspaceAttached"] is True
    assert reattached["workspaceRoot"] == created["workspaceRoot"]
    assert reattached["workspaceRef"] == "engine/ws-1"
    assert workspaces.count == 1


def test_a_detached_chat_is_told_to_reattach_rather_than_failing_on_a_path(
    async_client, web_app,
) -> None:
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    app = web_app(_workspace_session(runner, workspaces), {"test": runner})

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            await client.delete(f"/api/threads/{thread_id}/workspace")
            refused = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "carry on"}
            )
            await client.post(f"/api/threads/{thread_id}/workspace")
            accepted = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "carry on"}
            )
        return refused, accepted

    refused, accepted = asyncio.run(scenario())

    assert refused.status_code == 409
    assert "reattach" in refused.json()["error"]
    assert accepted.status_code == 200
    assert runner.workspace_ids == ["ws-1"]


def test_a_chat_that_never_had_a_workspace_can_be_given_one(
    async_client, web_app
) -> None:
    """Conversations from before worktrees existed, and any other stragglers."""
    runner = ConcurrentRunner()
    workspaces = ConversationWorkspaces()
    store = InMemoryStateStore()
    session = _workspace_session(runner, workspaces, store)
    app = web_app(session, {"test": runner})

    async def scenario():
        instance = await store.create_instance(CODER)
        async with async_client(app, base_url="http://test") as client:
            before = await client.get(f"/api/threads/{instance.instance_id}")
            attached = await client.post(f"/api/threads/{instance.instance_id}/workspace")
        # The pairing is durable, not just something the page is holding.
        stored = await store.load_instance(instance.instance_id)
        return before.json(), attached.json(), stored

    before, attached, stored = asyncio.run(scenario())

    assert before["workspaceAttached"] is False
    assert "workspaceRef" not in before
    assert attached["workspaceAttached"] is True
    assert attached["workspaceRoot"] == "/worktrees/ws-1"
    assert stored.workspace_id == "ws-1"


def test_a_process_without_a_workspace_repository_says_so(
    async_client, web_app
) -> None:
    runner = ConcurrentRunner()
    app = web_app(_session(runner), {"test": runner})

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            return await client.post(f"/api/threads/{created.json()['id']}/workspace")

    refused = asyncio.run(scenario())

    assert refused.status_code == 409
    assert "workspace repository" in refused.json()["error"]


def test_different_chats_can_run_at_the_same_time() -> None:
    runner = ConcurrentRunner(("one", "two"))
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario() -> None:
        first = await service.create(CODER, "test")
        second = await service.create(CODER, "test")
        await asyncio.gather(
            service.say(first.instance_id, "first", None, asyncio.Queue()),
            service.say(second.instance_id, "second", None, asyncio.Queue()),
        )

    asyncio.run(scenario())

    assert runner.most_active == 2


def test_one_chat_serializes_its_own_turns() -> None:
    runner = ConcurrentRunner(("one", "two"))
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario() -> tuple[Message, ...]:
        thread = await service.create(CODER, "test")
        await asyncio.gather(
            service.say(thread.instance_id, "first", None, asyncio.Queue()),
            service.say(thread.instance_id, "second", None, asyncio.Queue()),
        )
        return await service.history(thread.instance_id)

    history = asyncio.run(scenario())

    assert runner.most_active == 1
    assert [(message.role, message.content) for message in history] == [
        (Role.USER, "first"),
        (Role.ASSISTANT, "one"),
        (Role.USER, "second"),
        (Role.ASSISTANT, "two"),
    ]


def test_http_api_creates_lists_and_streams_threads(async_client, web_app) -> None:
    runner = ConcurrentRunner(("hello",))
    app = web_app(_session(runner), {"test": runner})

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            config = await client.get("/api/config")
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            streamed = await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"text": "hi", "runner": "test"},
            )
            messages = await client.get(f"/api/threads/{thread_id}/messages")
        return config, created, streamed, messages

    config, created, streamed, messages = asyncio.run(scenario())

    assert config.status_code == 200
    assert config.json()["defaultRunner"] == "test"
    assert created.status_code == 201
    assert streamed.status_code == 200
    assert '"type":"done"' in streamed.text
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [
        ("user", "hi"),
        ("assistant", "hello"),
    ]


def test_a_chat_keeps_the_runner_it_was_given_for_turns_that_name_none(
    async_client, web_app
) -> None:
    """The conversation remembers its runner; a turn need not repeat it.

    The header sends the choice once, so a turn that carries no runner has to
    reach whoever the chat was last set to rather than the wired default.
    """
    first = ConcurrentRunner(("from the first",))
    second = ConcurrentRunner(("from the second",))
    runners = {"test": first, "other": second}
    app = web_app(_session_with(runners), runners)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            switched = await client.patch(
                f"/api/threads/{thread_id}", json={"runner": "other"}
            )
            await client.post(f"/api/threads/{thread_id}/runs", json={"text": "hi"})
            reloaded = await client.get(f"/api/threads/{thread_id}")
            unknown = await client.patch(
                f"/api/threads/{thread_id}", json={"runner": "nobody"}
            )
            return switched, reloaded, unknown

    switched, reloaded, unknown = asyncio.run(scenario())

    assert switched.json()["runner"] == "other"
    assert reloaded.json()["runner"] == "other"
    assert [turn[-1].content for turn in second.seen] == ["hi"]
    assert first.seen == []
    assert unknown.status_code == 400


def test_agent_names_chat_before_answer_without_changing_conversation(
    async_client, web_app
) -> None:
    runner = ConcurrentRunner(('"SQLite Conversation Persistence"', "The answer."))
    app = web_app(_session(runner), {"test": runner})

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            title = await client.post(
                f"/api/threads/{thread_id}/title",
                json={
                    "text": "Why are chats missing after restart?",
                    "runner": "test",
                },
            )
            await client.post(
                f"/api/threads/{thread_id}/runs",
                json={"text": "Why are chats missing after restart?"},
            )
            repeated_title = await client.post(
                f"/api/threads/{thread_id}/title", json={}
            )
            messages = await client.get(f"/api/threads/{thread_id}/messages")
            return title, repeated_title, messages

    title, repeated_title, messages = asyncio.run(scenario())

    assert title.json() == {"title": "SQLite Conversation Persistence"}
    assert repeated_title.json() == title.json()
    assert runner.seen[0] == (
        Message.user("Why are chats missing after restart?"),
        Message.user(
            "Name this chat based on the conversation above. Reply with only a concise "
            "title of at most eight words, with no quotes or ending punctuation."
        ),
    )
    assert runner.seen[1] == (Message.user("Why are chats missing after restart?"),)
    assert len(runner.seen) == 2
    assert [
        (message["role"], message["content"][0]["text"])
        for message in messages.json()["messages"]
    ] == [
        ("user", "Why are chats missing after restart?"),
        ("assistant", "The answer."),
    ]


def test_a_provider_that_cannot_name_a_chat_does_not_cost_the_turn(
    async_client, web_app
) -> None:
    """Naming happens before the message it names is sent, so it cannot fail it.

    A CLI that is out of quota, unauthenticated, or simply broken fails the
    first thing the client asks of it, which is a title. Answered with a 500
    that would stop the chat working entirely -- for a name.
    """

    class FailsToName(ConcurrentRunner):
        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            if args[2][-1].content.startswith("Name this chat"):
                raise RuntimeError("codex exited 1: stream error: unauthorized")
            return await super().run_turn(*args, **kwargs)

    runner = FailsToName(("The answer.",))
    app = web_app(_session(runner), {"test": runner})

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            title = await client.post(
                f"/api/threads/{thread_id}/title", json={"text": "hello"}
            )
            run = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "hello"}
            )
            return title, run, await client.get(f"/api/threads/{thread_id}")

    title, run, thread = asyncio.run(scenario())

    assert title.status_code == 200
    assert title.json()["title"] == "New chat"
    # Not silence: the placeholder name says nothing about which provider
    # failed, and somebody reading the response deserves the reason.
    assert "unauthorized" in title.json()["error"]
    # The turn the client was about to send goes through regardless.
    assert run.status_code == 200
    assert thread.json()["title"] == "New chat"
    finished = json.loads([line for line in run.text.splitlines() if line][-1])
    assert finished["type"] == "done"
    assert finished["content"][0]["text"] == "The answer."


def test_missing_frontend_has_an_actionable_response(async_client, web_app) -> None:
    runner = ConcurrentRunner()
    app = web_app(_session(runner), {"test": runner})

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            return await client.get("/")

    response = asyncio.run(scenario())

    assert response.status_code == 503
    assert "npm --prefix apps/web run build" in response.text


def test_tool_activity_round_trips_as_assistant_ui_parts(async_client, web_app) -> None:
    call = ToolCall(call_id="call-1", name="Read", arguments='{"path":"README.md"}')

    class ToolRunner(ConcurrentRunner):
        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            return AgentTurn(
                Message.assistant("Found it."),
                steps=(
                    Message.assistant(tool_calls=(call,)),
                    Message.tool_result(call.call_id, "engine"),
                ),
            )

    runner = ToolRunner()
    app = web_app(_session(runner), {"test": runner})

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            await client.post(f"/api/threads/{thread_id}/runs", json={"text": "inspect"})
            return (await client.get(f"/api/threads/{thread_id}/messages")).json()

    content = asyncio.run(scenario())["messages"][1]["content"]

    assert content == [
        {
            "type": "tool-call",
            "toolCallId": "call-1",
            "toolName": "Read",
            "args": {"path": "README.md"},
            "argsText": '{"path":"README.md"}',
            "result": "engine",
        },
        {"type": "text", "text": "Found it."},
    ]


def test_replayed_tool_call_id_is_only_exposed_once(async_client, web_app) -> None:
    """Provider reconnects may repeat a completed item with its original id.

    assistant-ui treats the id as a resource key across the whole thread, so a
    replay must remain one displayed call rather than crashing the chat view.
    """
    call = ToolCall(
        call_id="call-replayed",
        name="Read",
        arguments='{"path":"README.md"}',
    )

    class ReplayRunner(ConcurrentRunner):
        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            return AgentTurn(
                Message.assistant("Found it."),
                steps=(
                    Message.assistant(tool_calls=(call,)),
                    Message.tool_result(call.call_id, "engine"),
                ),
            )

    runner = ReplayRunner()
    app = web_app(_session(runner), {"test": runner})

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            created = await client.post(
                "/api/threads",
                json={"agentId": "coder", "runner": "test"},
            )
            thread_id = created.json()["id"]
            await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "inspect"}
            )
            replayed = await client.post(
                f"/api/threads/{thread_id}/runs", json={"text": "inspect again"}
            )
            history = await client.get(f"/api/threads/{thread_id}/messages")
            return replayed, history

    replayed, history = asyncio.run(scenario())

    assert "call-replayed" not in replayed.text
    parts = [
        part
        for message in history.json()["messages"]
        for part in message["content"]
        if part["type"] == "tool-call"
    ]
    assert [part["toolCallId"] for part in parts] == ["call-replayed"]
    assert parts[0]["result"] == "engine"


def test_a_stopped_run_leaves_its_work_in_the_reloaded_transcript(
    async_client, web_app
) -> None:
    """Pressing stop ends the turn, not the record of it. What the agent had
    already done is on disk whatever the button does, so a reload that showed
    the question alone would be a transcript the worktree disagrees with."""
    call = ToolCall(call_id="call-1", name="Write", arguments='{"path":"worker.py"}')

    class StoppedMidWorkRunner(ConcurrentRunner):
        def __init__(self) -> None:
            super().__init__()
            self.reported = asyncio.Event()

        async def run_turn(self, *args, **kwargs) -> AgentTurn:
            raise AssertionError("the streaming method should be used")

        async def run_turn_streamed(
            self, agent_run_id, profile, messages, on_message, tools=(), workspace_id=None
        ) -> AgentTurn:
            on_message(Message.assistant("Rewriting the worker."))
            on_message(Message.assistant(tool_calls=(call,)))
            self.reported.set()
            await asyncio.Event().wait()
            raise AssertionError("this runner only ever ends by being stopped")

    runner = StoppedMidWorkRunner()
    app = web_app(_session(runner), {"test": runner})

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            created = await client.post(
                "/api/threads", json={"agentId": "coder", "runner": "test"}
            )
            thread_id = created.json()["id"]
            started = asyncio.create_task(
                client.post(f"/api/threads/{thread_id}/runs", json={"text": "rewrite it"})
            )
            await runner.reported.wait()
            stopped = await client.delete(f"/api/threads/{thread_id}/runs/current")
            await started
            # A fresh page load: the stream is gone, so this is all the client
            # gets to know about the turn that was stopped.
            return stopped, await client.get(f"/api/threads/{thread_id}/messages")

    stopped, messages = asyncio.run(scenario())

    assert stopped.status_code == 204
    reloaded = messages.json()["messages"]
    # The note the next turn is given is prompt context, not something to show
    # a person, so it does not become a message here.
    assert [message["role"] for message in reloaded] == ["user", "assistant"]
    assert reloaded[1]["content"] == [
        {"type": "text", "text": "Rewriting the worker."},
        {
            "type": "tool-call",
            "toolCallId": "call-1",
            "toolName": "Write",
            "args": {"path": "worker.py"},
            "argsText": '{"path":"worker.py"}',
            # Answered rather than left pending, which is what a client shows
            # as a tool still running.
            "result": "interrupted",
        },
    ]


def test_active_run_survives_stream_disconnect_and_replays_progress() -> None:
    call = ToolCall(call_id="call-1", name="Read", arguments='{"path":"README.md"}')

    class RefreshRunner(ConcurrentRunner):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_turn_streamed(
            self, agent_run_id, profile, messages, on_message, tools=(), workspace_id=None
        ) -> AgentTurn:
            tool_call = Message.assistant(tool_calls=(call,))
            tool_result = Message.tool_result(call.call_id, "engine")
            answer = Message.assistant("Found it.")
            on_message(tool_call)
            self.started.set()
            await self.release.wait()
            on_message(tool_result)
            on_message(answer)
            return AgentTurn(answer, steps=(tool_call, tool_result))

    runner = RefreshRunner()
    service = ThreadService(_session(runner), {"test": runner})

    async def scenario():
        thread = await service.create(CODER, "test")
        run = await service.start_run(thread.instance_id, "inspect", None)
        await runner.started.wait()

        original_stream = run.stream()
        first = json.loads((await anext(original_stream)).decode())
        await original_stream.aclose()  # the browser refreshed

        assert service.active_run(thread.instance_id) is run
        active = service.active_run(thread.instance_id)
        assert active is not None
        resumed_stream = active.stream()
        replayed = json.loads((await anext(resumed_stream)).decode())

        runner.release.set()
        events = [replayed]
        async for event in resumed_stream:
            events.append(json.loads(event.decode()))
        return first, events, await service.history(thread.instance_id)

    first, events, history = asyncio.run(scenario())

    assert first["content"] == [
        {
            "type": "tool-call",
            "toolCallId": "call-1",
            "toolName": "Read",
            "args": {"path": "README.md"},
            "argsText": '{"path":"README.md"}',
        }
    ]
    assert events[0] == first
    assert events[-1]["type"] == "done"
    assert events[-1]["content"][-1] == {"type": "text", "text": "Found it."}
    assert [(message.role, message.content) for message in history] == [
        (Role.USER, "inspect"),
        (Role.ASSISTANT, ""),
        (Role.TOOL, "engine"),
        (Role.ASSISTANT, "Found it."),
    ]


def test_the_built_client_is_revalidated_but_its_hashed_assets_are_not(
    async_client, tmp_path, *, web_app
) -> None:
    """A cached entry point asks for the assets of a build that is gone."""
    runner = ConcurrentRunner()
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script src="/assets/index-abc123.js"></script>')
    (dist / "assets" / "index-abc123.js").write_text("console.log('engine')")
    app = web_app(_session(runner), {"test": runner}, dist)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            return (
                await client.get("/"),
                await client.get("/assets/index-abc123.js"),
            )

    page, asset = asyncio.run(scenario())

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]


@pytest.mark.parametrize("encoding", ["gzip", "identity"])
def test_web_compresses_large_json_and_static_assets(
    tmp_path, encoding, *, async_client, web_app
) -> None:
    runner = ConcurrentRunner()
    (tmp_path / "assets").mkdir()
    javascript = b"console.log('engine');\n" * 5000
    (tmp_path / "assets" / "index-abc123.js").write_bytes(javascript)
    (tmp_path / "index.html").write_text("<html>engine</html>")
    app = web_app(_session(runner), {"test": runner}, tmp_path)
    payload = {"messages": ["large response" * 1000]}

    async def large_response(request):
        return JSONResponse(payload, headers={"Vary": "Origin"})

    app.router.routes.insert(0, Route("/api/compression-test", large_response))

    async def scenario():
        async with async_client(app, base_url="http://test", headers={"Accept-Encoding": encoding}) as client:
            for path, expected in [
                ("/api/compression-test", JSONResponse(payload).body),
                ("/assets/index-abc123.js", javascript),
            ]:
                async with client.stream("GET", path) as response:
                    raw = b"".join([chunk async for chunk in response.aiter_raw()])
                    assert response.status_code == 200
                    if encoding == "gzip":
                        assert response.headers["content-encoding"] == "gzip"
                        assert gzip.decompress(raw) == expected
                        assert len(raw) < len(expected)
                        assert "Accept-Encoding" in response.headers["vary"]
                    else:
                        assert "content-encoding" not in response.headers
                        assert raw == expected
                    if path.startswith("/assets/"):
                        assert "immutable" in response.headers["cache-control"]
                    else:
                        assert "Origin" in response.headers["vary"]
            page = await client.get("/")
            assert "content-encoding" not in page.headers
            assert page.headers["cache-control"] == "no-cache"

    asyncio.run(scenario())


@pytest.mark.parametrize("path, media_type", [
    ("/api/threads/test/approval-events", "text/event-stream"),
    ("/api/runs/test/events", "text/event-stream"),
    ("/api/threads/test/runs", "application/x-ndjson"),
    ("/api/threads/test/runs/current", "application/x-ndjson"),
])
def test_web_compression_delivers_stream_chunks_immediately(
    path, media_type, *, web_app
) -> None:
    async def scenario():
        runner = ConcurrentRunner()
        app = web_app(_session(runner), {"test": runner})
        delivered = asyncio.Event()
        chunks = [b'data: {"message": "first"}\n\n', b'data: {"message": "second"}\n\n']
        bodies = []

        async def stream():
            for chunk in chunks:
                delivered.clear()
                yield chunk
                # The producer cannot continue until this chunk reaches the
                # client. Buffered transports would conceal this regression.
                await asyncio.wait_for(delivered.wait(), timeout=1)

        async def endpoint(request):
            return StreamingResponse(stream(), media_type=media_type)

        app.router.routes.insert(0, Route(path, endpoint))

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 200
                assert b"content-encoding" not in dict(message["headers"])
            elif message["type"] == "http.response.body" and message.get("body"):
                bodies.append(message["body"])
                assert message["more_body"]
                delivered.set()

        await asyncio.wait_for(app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": path, "query_string": b"",
            "headers": [(b"accept-encoding", b"gzip")],
        }, receive, send), timeout=2)
        assert bodies == chunks

    asyncio.run(scenario())


# --- graph WorkOrders (the graph entries in the dropdown) ---------------------
#
# A second kind of workflow can be picked from the same dropdown. It is run by
# the graph engine rather than by the step executor, and these are the three
# things that has to mean: it is offered, picking it starts a graph run, and
# what this app keeps for it is a row rather than a driver.


def _graph_app(
    store: InMemoryStateStore,
    *graphs: ScriptedGraph,
    approval_policy: ApprovalConfig = ApprovalConfig(),
    utilization: UtilizationService | None = None,
    workflow_app,
    **options,
):
    """The web app with a scripted graph engine wired in.

    A real `GraphRuntime` with real tasks, exactly as the graph package's own
    tests use it -- what it does not have is LangGraph, so no agent is started
    and no repository is checked out.
    """
    runtime = ScriptedGraphRuntime(*graphs)
    return (
        _graph_app_over(
            store, runtime, *graphs, approval_policy=approval_policy,
            utilization=utilization, **options,
         workflow_app=workflow_app),
        runtime,
    )


def _graph_app_over(
    store: InMemoryStateStore,
    runtime: ScriptedGraphRuntime,
    *graphs: ScriptedGraph,
    approval_policy: ApprovalConfig = ApprovalConfig(),
    github_login_config: GitHubLoginConfig | None = None,
    utilization: UtilizationService | None = None,
    workflow_app,
    **options,
):
    """A web app over an engine that already exists, so a restart can be one.

    The lifespan is what picks graph WorkOrders back up, and a context manager
    is entered once -- so "the server was restarted" is a second app over the
    same store and the same engine, rather than the same app opened twice.
    """

    @asynccontextmanager
    async def running(_app=None):
        yield runtime

    return workflow_app(
        store,
        ConcurrentRunner(),
        workflow_catalog=WorkflowCatalog.from_graphs(graphs),
        graph_runtime=running(),
        approval_policy=approval_policy,
        github_login_config=github_login_config,
        utilization=utilization,
        **options,
    )


def _review_graph() -> ScriptedGraph:
    return ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Say("Changed it."),)),),
    )


def test_a_workflow_is_offered_under_its_own_name(async_client, workflow_app) -> None:
    """The dropdown, which is where a person meets this at all.

    Asked of a started server, because that is when a workflow is offerable:
    the engine that would run one is opened on startup.
    """
    app, _ = _graph_app(InMemoryStateStore(), _review_graph(), workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                return (await client.get("/api/config")).json()["workflows"]

    offered = asyncio.run(scenario())

    assert offered == [
        {
            "id": "implementation-review-codex",
            "name": "Implementation review (codex)",
        },
    ]


def test_a_workflow_is_not_offered_without_an_engine_to_run_it(
    async_client, workflow_app
) -> None:
    """No graph engine composed, nothing on offer.

    The alternative is a choice that fails after somebody made it, which is
    worse than a choice that was never there.
    """
    app = workflow_app(
        InMemoryStateStore(),
        ConcurrentRunner(),
        workflow_catalog=WorkflowCatalog.from_graphs((_review_graph(),)),
    )

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            return (
                (await client.get("/api/config")).json()["workflows"],
                await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                ),
            )

    offered, refused = asyncio.run(scenario())

    assert offered == []
    assert refused.status_code == 400


def test_creating_a_work_order_starts_the_graph(async_client, workflow_app) -> None:
    """The whole point: picking one runs it on the graph engine.

    Checked on the engine rather than only on the answer, because a WorkOrder
    that was recorded and never started would look identical from here.
    """
    store = InMemoryStateStore()
    app, runtime = _graph_app(store, _review_graph(), workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                        "runner": "test",
                    },
                )
                run_id = RunId(created.json()["runId"])
                listed = await client.get("/api/runs")
                return created, listed, run_id, await runtime.snapshot(run_id)

    created, listed, run_id, snapshot = asyncio.run(scenario())

    assert created.status_code == 201
    # One run, on the graph engine, carrying the task and the repository the
    # WorkOrder was created with -- which is everything these graphs need.
    assert snapshot is not None
    assert str(snapshot.graph_id) == "implementation-review-codex"
    assert snapshot.values == {
        "task": "Add cancellation handling.",
        "repository": "acme/api",
    }
    # And a row for it here, under the graph engine's own run id, named after
    # the graph rather than after an id nobody chose.
    assert created.json()["workflowName"] == "Implementation review (codex)"
    assert [one["runId"] for one in listed.json()["runs"]] == [str(run_id)]


def test_graph_run_listing_carries_live_node_and_approval_state(
    monkeypatch, *, async_client, workflow_app
) -> None:
    graph = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Ask("Run tests"),)),),
    )
    app, runtime = _graph_app(InMemoryStateStore(), graph, workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": str(graph.graph_id),
                        "prompt": "Implement it",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                for _ in range(100):
                    snapshot = await runtime.snapshot(run_id)
                    if snapshot is not None and snapshot.pending_approvals:
                        break
                    await asyncio.sleep(0)
                else:
                    raise AssertionError("graph never requested approval")
                async def no_snapshot(_run_id):
                    raise AssertionError("listing must not read checkpoints")

                monkeypatch.setattr(runtime, "snapshot", no_snapshot)
                for _ in range(3):
                    row = (await client.get("/api/runs")).json()["runs"][0]
                return row

    row = asyncio.run(scenario())
    assert row["graphProgress"] == {
        "activeNodeIds": ["implementation"],
        "waitingNodeIds": ["implementation"],
        "nextNodeIds": [],
    }


def test_auto_approve_config_seeds_all_graph_nodes(async_client, workflow_app) -> None:
    """When `auto_approve = true`, every node starts auto-approved."""
    graph = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (
            ScriptedNode(
                NodeId("implementation"),
                (Say("Changed it."),),
                next_nodes=(NodeId("review"),),
            ),
            ScriptedNode(NodeId("review"), (Say("Looks good."),)),
        ),
    )
    app, runtime = _graph_app(
        InMemoryStateStore(),
        graph,
        approval_policy=ApprovalConfig(auto_approve=True),
     workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": str(graph.graph_id),
                        "prompt": "Review it",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                snapshot = await runtime.snapshot(run_id)
                return snapshot

    snapshot = asyncio.run(scenario())
    assert set(snapshot.auto_approve_nodes) == {
        NodeId("implementation"),
        NodeId("review"),
    }


def test_a_graph_naming_node_names_its_work_order(async_client, workflow_app) -> None:
    store = InMemoryStateStore()
    graph = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (
            ScriptedNode(
                NodeId("naming"),
                (Say('"Cancellation handling."'),),
                next_nodes=(NodeId("implementation"),),
                output_key="name",
            ),
            ScriptedNode(NodeId("implementation"), (AwaitSteering(),)),
        ),
    )
    app, _ = _graph_app(store, graph, workflow_app=workflow_app)

    async def scenario() -> dict[str, object]:
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = created.json()["runId"]
                for _ in range(100):
                    named = (await client.get(f"/api/runs/{run_id}")).json()
                    if named["name"] == "Cancellation handling":
                        return named
                    await asyncio.sleep(0)
                return named

    named = asyncio.run(scenario())

    assert named["name"] == "Cancellation handling"


def test_a_finished_graph_run_stops_saying_it_is_working(
    async_client, workflow_app
) -> None:
    """The row follows the graph to its ending.

    Nothing else would move it: the step executor is not driving this run, so
    without the engine's own report the WorkOrder would claim to be working
    forever.
    """
    store = InMemoryStateStore()
    app, _ = _graph_app(store, _review_graph(), workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                return created.json()["phase"], (
                    await _await_phase(client, run_id, "succeeded")
                ).json()["phase"]

    started, ended = asyncio.run(scenario())

    assert started == "running_agent"
    assert ended == "succeeded"


def test_deleting_a_graph_work_order_stops_the_engine_driving_it(
    async_client, workflow_app
) -> None:
    """The rail's x on a graph row has to reach the other engine.

    None of what stops a step WorkOrder touches a graph one: its driver is a
    task inside the graph engine rather than in this app's `workflow_tasks`,
    and the agent it has open is not an agent run this app started. So a delete
    that only forgot the row would take the WorkOrder off the rail and leave
    the run working -- agents still going in the repository, and nothing left
    on screen to stop them by.

    Scripted on a node that waits, so there is something still in flight at the
    moment the row is deleted; a graph that had already finished would pass
    this whatever the handler did.
    """
    store = InMemoryStateStore()
    waiting = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Say("Reading."), AwaitSteering())),),
    )
    app, runtime = _graph_app(store, waiting, workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                # The node is scripted to wait, so this is a run with something
                # genuinely in flight rather than one that raced to its end.
                while not runtime.running():
                    await asyncio.sleep(0)
                deleted = await client.delete(f"/api/runs/{run_id}")
                listed = await client.get("/api/runs")
                # Read here rather than after the loop is closed, which would
                # cancel the driver itself and pass whether or not the delete
                # had.
                driving = [str(one) for one in runtime.running()]
                return deleted, listed, run_id, driving, await runtime.snapshot(run_id)

    deleted, listed, run_id, driving, snapshot = asyncio.run(scenario())

    assert deleted.status_code == 204
    assert listed.json()["runs"] == []
    assert asyncio.run(store.load(run_id)) is None
    # Nothing left driving it, and the engine says the run is over rather than
    # reporting one that is working with no row and nobody watching.
    assert driving == []
    assert snapshot is not None
    assert snapshot.status is RunStatus.FAILED
    assert snapshot.error == CANCELLED


@pytest.mark.parametrize(
    "phase",
    [RunPhase.SCHEDULED, RunPhase.RUNNING_AGENT, RunPhase.SUCCEEDED, RunPhase.FAILED],
)
def test_deleting_a_prerequisite_with_scheduled_dependents_is_rejected(
    phase: RunPhase, *, async_client, workflow_app
) -> None:
    from unittest.mock import AsyncMock

    async def scenario():
        store = InMemoryStateStore()
        app, runtime = _graph_app(store, _review_graph(), workflow_app=workflow_app)
        runtime.cancel = AsyncMock()
        prerequisite = RunState(
            run_id=RunId("prerequisite"), task_id=TaskId("prerequisite"),
            workflow_id=WorkflowId("implementation-review-codex"), phase=phase,
        )
        dependent = RunState(
            run_id=RunId("dependent"), task_id=TaskId("dependent"),
            workflow_id=prerequisite.workflow_id, phase=RunPhase.SCHEDULED,
            depends_on_run_id=prerequisite.run_id,
        )
        await store.save(prerequisite)
        await store.save(dependent)
        # Keep dispatch stopped to also cover a completed prerequisite whose
        # dependent has not yet been started.
        async with async_client(app, base_url="http://test") as client:
            response = await client.delete("/api/runs/prerequisite")
            assert response.status_code == 409
            assert "dependent" in response.json()["error"]
            assert await store.load(prerequisite.run_id) == prerequisite
            assert await store.load(dependent.run_id) == dependent
            runtime.cancel.assert_not_awaited()
            assert (await client.delete("/api/runs/dependent")).status_code == 204
            assert (await client.delete("/api/runs/prerequisite")).status_code == 204
            assert await store.list_runs() == ()

    asyncio.run(scenario())


def test_deleting_a_graph_work_order_the_engine_never_heard_of_still_works(
    async_client, workflow_app,
) -> None:
    """A row whose graph state is gone is still the reader's to throw away.

    The case `restore_graph_runs` fails a run for: the engine has no record of
    it, so there is nothing to cancel. Refusing the delete would leave a
    WorkOrder that cannot be removed and that nothing is working on.
    """
    store = InMemoryStateStore()
    app, runtime = _graph_app(store, _review_graph(), workflow_app=workflow_app)
    stranded = RunState(
        run_id=RunId("run-stranded"),
        task_id=TaskId("task-stranded"),
        workflow_id=WorkflowId("implementation-review-codex"),
        phase=RunPhase.RUNNING_AGENT,
        prompt="Add cancellation handling.",
        repository="acme/api",
    )

    async def scenario():
        await store.save(stranded)
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                deleted = await client.delete(f"/api/runs/{stranded.run_id}")
                return deleted, [str(one) for one in runtime.running()]

    deleted, driving = asyncio.run(scenario())

    assert deleted.status_code == 204
    assert asyncio.run(store.load(stranded.run_id)) is None
    assert driving == []


def test_graph_events_cursor_replays_only_unseen_events(
    async_client, workflow_app
) -> None:
    store = InMemoryStateStore()
    graph = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Say("Reading."), AwaitSteering())),),
    )
    app, _ = _graph_app(store, graph, workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Read the code.",
                        "repository": "acme/api",
                    },
                )
                url = f"/api/runs/{created.json()['runId']}/graph-events"
                for _ in range(200):
                    full = (await client.get(url)).json()["events"]
                    if any(event["type"] == "transcript" for event in full):
                        break
                    await asyncio.sleep(0.01)
                assert len(full) > 1
                snapshot = (await client.get(
                    f"/graph/api/runs/{created.json()['runId']}"
                )).json()
                execution_id = snapshot["activeExecutions"][0]["executionId"]
                messages = [event for event in full if event["type"] == "transcript"]
                assert messages
                assert all(event["executionId"] == execution_id for event in messages)
                assert all(event["executionId"] is None for event in full
                           if event["type"] == "run.started")
                for cursor in ("0", "", " "):
                    response = await client.get(url, params={"cursor": cursor})
                    assert response.status_code == 200
                    assert response.json()["events"] == full
                cursor = full[0]["sequence"]
                response = await client.get(url, params={"cursor": cursor})
                assert response.json()["events"] == full[1:]
                last = full[-1]["sequence"]
                response = await client.get(url, params={"cursor": last})
                assert response.json()["events"] == []
                response = await client.get(url, headers={"Last-Event-ID": str(last)})
                assert response.json()["events"] == []
                response = await client.get(
                    url, params={"cursor": 0}, headers={"Last-Event-ID": str(last)}
                )
                assert response.json()["events"] == full
                for cursor in ("-1", "nope", "1.5"):
                    response = await client.get(url, params={"cursor": cursor})
                    assert response.status_code == 400
                    assert "cursor" in response.json()["error"]

    asyncio.run(scenario())


def test_a_work_order_of_a_withdrawn_workflow_still_lists_and_still_reads(
    async_client, workflow_app,
) -> None:
    """A WorkOrder outlives the workflow it ran, and the pages have to cope.

    Renaming a graph -- what #367 did to this one -- or taking it out of the
    workflow directory leaves rows behind whose graph nothing can describe. The
    list is every WorkOrder there is, so a refusal let out of one row would take
    the whole page down, and the transcript is recorded against the run rather
    than against the graph, so it is still there to be read.

    The engine's own surface says the same thing with a 404: "there is no such
    graph" is an answer, and it used to be a `KeyError` on the way out.
    """
    store = InMemoryStateStore()
    waiting = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Say("Reading."), AwaitSteering())),),
    )
    app, runtime = _graph_app(store, waiting, workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                # Waited for rather than assumed: the node is scripted to say
                # something and then stop, and withdrawing the graph before it
                # had spoken would test a transcript that was never recorded.
                for _ in range(200):
                    feed = await client.get(f"/api/runs/{run_id}/graph-events")
                    if any(
                        one["type"] == "transcript" for one in feed.json()["events"]
                    ):
                        break
                    await asyncio.sleep(0.01)
                # The deployment stops defining the graph, with the run of it
                # left exactly where it was.
                runtime.withdraw(GraphId("implementation-review-codex"))
                return (
                    await client.get("/api/runs"),
                    await client.get(f"/api/runs/{run_id}"),
                    await client.get(f"/api/runs/{run_id}/graph-events"),
                    await client.get(f"/graph/api/runs/{run_id}"),
                    await client.get("/graph/api/graphs/implementation-review-codex"),
                )

    listed, detail, events, snapshot, described = asyncio.run(scenario())

    assert listed.status_code == 200
    assert [one["workflowId"] for one in listed.json()["runs"]] == [
        "implementation-review-codex"
    ]
    # Listed without a frontier rather than not listed: nothing can say where
    # the run got to, and that is not a reason to hide it.
    assert "graphProgress" not in listed.json()["runs"][0]
    assert detail.status_code == 200
    # What the run said is still readable, which is the difference between an
    # old WorkOrder being openable and being a dead link.
    assert events.status_code == 200
    assert [one["type"] for one in events.json()["events"]].count("transcript") == 1
    assert snapshot.status_code == 404
    assert described.status_code == 404


def test_a_restart_fails_a_work_order_whose_workflow_is_gone(
    async_client, workflow_app
) -> None:
    """The row is told, rather than left claiming an agent is working on it.

    Nothing can pick this run back up -- there is no graph to run it -- so the
    honest ending is a failure naming the workflow that went missing. Left
    alone it would sit at "working" for as long as the deployment lives.
    """
    store = InMemoryStateStore()
    waiting = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Say("Reading."), AwaitSteering())),),
    )
    app, runtime = _graph_app(store, waiting, workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                while not runtime.running():
                    await asyncio.sleep(0)
        runtime.withdraw(GraphId("implementation-review-codex"))
        restarted = _graph_app_over(store, runtime, workflow_app=workflow_app)
        async with async_client(restarted, base_url="http://test") as client:
            async with restarted.router.lifespan_context(restarted):
                return await client.get(f"/api/runs/{run_id}")

    detail = asyncio.run(scenario()).json()

    assert detail["phase"] == "failed"
    assert "implementation-review-codex" in detail["failureReason"]
    assert "no longer available" in detail["failureReason"]


def test_the_graph_engine_answers_under_its_own_prefix(
    async_client, workflow_app
) -> None:
    """Where a graph run is watched and approved today.

    This app's pages cannot do either yet, and the graph engine's own API can,
    so it is served from here rather than left unreachable. Behind `/graph`
    because both call their runs `/api/runs`.
    """
    app, _ = _graph_app(InMemoryStateStore(), _review_graph(), workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                return await client.get("/graph/api/graphs")

    described = asyncio.run(scenario())

    assert described.status_code == 200
    assert [one["graphId"] for one in described.json()["graphs"]] == [
        "implementation-review-codex"
    ]


def test_a_failed_graph_run_says_why_on_its_row(async_client, workflow_app) -> None:
    """The other ending, and the reason that comes with it.

    The reason is read out of the event the engine publishes, so the row and
    the graph engine's own API give the same answer to "why did this stop?".
    A renamed key on that event would leave a failed WorkOrder with nothing to
    show, which is what this is here to catch.
    """
    store = InMemoryStateStore()
    broken = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation"), (Fail("codex is out of quota"),)),),
    )
    app, _ = _graph_app(store, broken, workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                return (await _await_phase(client, run_id, "failed")).json()

    ended = asyncio.run(scenario())

    assert ended["phase"] == "failed"
    assert ended["failureReason"] == "codex is out of quota"


def test_messaging_a_failed_graph_implementer_resets_its_workorder(
    async_client, workflow_app,
) -> None:
    graph = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(
            NodeId("implementation"),
            (AwaitSteering(), Fail("codex is out of quota")),
            always_open=True,
        ),),
    )
    app, runtime = _graph_app(InMemoryStateStore(), graph, workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                created = await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )
                run_id = RunId(created.json()["runId"])
                async with asyncio.timeout(5):
                    while not (await runtime.snapshot(run_id)).active_executions:
                        await asyncio.sleep(0)
                response = await client.post(
                    f"/graph/api/runs/{run_id}/steering",
                    json={"node": "implementation", "message": "Start implementing."},
                )
                assert response.status_code == 200
                failed = (await _await_phase(client, run_id, "failed")).json()
                assert failed["phase"] == "failed"
                assert failed["failureReason"] == "codex is out of quota"

                restarted = await client.post(
                    f"/graph/api/runs/{run_id}/steering",
                    json={"node": "implementation", "message": "Try again."},
                )
                assert restarted.status_code == 200
                assert restarted.json()["status"] == "running"
                assert restarted.json()["error"] == ""
                return (await client.get(f"/api/runs/{run_id}")).json()

    restarted = asyncio.run(scenario())
    assert restarted["phase"] == "running_agent"
    assert restarted["failureReason"] == ""
    assert restarted["terminalOutcome"] is None


def test_a_graph_run_that_ends_before_its_row_exists_is_still_recorded(
    async_client, workflow_app,
) -> None:
    """The narrowest bit of ordering in the whole change.

    A graph short enough to be over before `start` answers announces its ending
    to nobody: there is no row yet for the announcement to land on. So the
    engine is asked once more after the row is saved, and this is the case that
    exists for -- a graph with no work in it at all.
    """
    store = InMemoryStateStore()
    instant = ScriptedGraph(
        GraphId("implementation-review-codex"),
        "Implementation review (codex)",
        (ScriptedNode(NodeId("implementation")),),
    )
    app, _ = _graph_app(store, instant, workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                return await client.post(
                    "/api/runs",
                    json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Add cancellation handling.",
                        "repository": "acme/api",
                    },
                )

    created = asyncio.run(scenario())

    # Whether the ending arrived before or after the row was saved, the answer
    # a person is handed is never "an agent is working" on a run that is over.
    assert created.json()["phase"] in {"succeeded", "running_agent"}
    assert created.status_code == 201


# --- a graph WorkOrder across a restart ----------------------------------------
#
# What a graph run keeps in the engine's files is where it got to. What it does
# not keep is the *driver* -- the task working through the graph -- because that
# lives in a process, and a process that stops takes its drivers with it.
#
# Driven against a double rather than the scripted engine, because the thing
# under test is a second process finding runs a first one left behind, and the
# scripted engine keeps everything in the process that started it: a run it
# knows about is, by construction, one it is still driving.


@dataclass
class _EngineAfterARestart:
    """A graph engine that remembers runs but is driving none of them.

    Everything the recovery pass calls, and nothing else. `resumed` is what a
    test asserts on: relaunching a stranded run is invisible in this app's own
    state, because the run carries on being a run that is working.
    """

    answers: dict[RunId, RunSnapshot | None]
    resumed: list[tuple[RunId, CheckpointId]] = field(default_factory=list)

    def observe(self, observer) -> None:
        self._observer = observer

    async def snapshot(self, run_id: RunId) -> RunSnapshot | None:
        return self.answers.get(run_id)

    async def resume_from(self, run_id: RunId, checkpoint_id: CheckpointId):
        self.resumed.append((run_id, checkpoint_id))
        return self.answers[run_id]

    def topology(self, graph_id):
        from engine.graph_runtime.topology import GraphTopology
        return GraphTopology(graph_id, "Review", NodeId("implementation"))

    def graphs(self) -> tuple:
        return (self.topology(GraphId("implementation-review-codex")),)


def _restarted(
    store: InMemoryStateStore, answers: dict[RunId, RunSnapshot | None], *, workflow_app
) -> tuple[object, _EngineAfterARestart]:
    runtime = _EngineAfterARestart(answers)

    @asynccontextmanager
    async def running(_app=None):
        yield runtime

    app = workflow_app(
        store,
        ConcurrentRunner(),
        workflow_catalog=WorkflowCatalog.from_graphs((_review_graph(),)),
        graph_runtime=running(),
    )
    return app, runtime


def _interrupted_run() -> RunState:
    return RunState(
        run_id=RunId("run-graph"),
        task_id=TaskId("task-graph"),
        workflow_id=WorkflowId("implementation-review-codex"),
        phase=RunPhase.RUNNING_AGENT,
        prompt="Add cancellation handling.",
        repository="acme/api",
    )


def _graph_snapshot(status: RunStatus, error: str = "") -> RunSnapshot:
    return RunSnapshot(
        run_id=RunId("run-graph"),
        graph_id=GraphId("implementation-review-codex"),
        status=status,
        checkpoint_id=CheckpointId("checkpoint-3"),
        error=error,
    )


def _after_a_restart(app, store: InMemoryStateStore, run: RunState) -> RunState:
    async def scenario():
        await store.save(run)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
        restored = await store.load(run.run_id)
        assert restored is not None
        return restored

    return asyncio.run(scenario())


def test_a_graph_run_interrupted_mid_execution_is_picked_back_up(workflow_app) -> None:
    """The reason this pass exists at all.

    A run that was working when the process died has no driver in the process
    that replaces it, and nothing else would build one: the step executor
    cannot -- a graph has no steps -- and the engine only builds one when a run
    is started or a decision arrives. So the run is sent back to the last
    position it saved and carried on from there.
    """
    store = InMemoryStateStore()
    app, runtime = _restarted(store, {RunId("run-graph"): _graph_snapshot(RunStatus.RUNNING)}, workflow_app=workflow_app)

    restored = _after_a_restart(app, store, _interrupted_run())

    assert runtime.resumed == [(RunId("run-graph"), CheckpointId("checkpoint-3"))]
    # Still working, and still not the step executor's: a resumed graph run is
    # a graph run, and nothing here started a step for it.
    assert restored.phase is RunPhase.RUNNING_AGENT
    assert restored.failure_reason == ""


def test_a_graph_run_waiting_on_a_person_is_left_where_it_is(workflow_app) -> None:
    """The case that already worked, and must not be disturbed.

    A run parked on a question is picked back up by the answer, not by the
    restart. Resuming it here would throw the question away -- the execution
    that asked it is gone, so the person's answer would have nowhere to go.
    """
    store = InMemoryStateStore()
    app, runtime = _restarted(
        store, {RunId("run-graph"): _graph_snapshot(RunStatus.AWAITING_APPROVAL)},
        workflow_app=workflow_app,
    )

    restored = _after_a_restart(app, store, _interrupted_run())

    assert runtime.resumed == []
    assert restored.phase is RunPhase.RUNNING_AGENT


def test_a_graph_run_that_ended_while_the_server_was_down_catches_up(
    workflow_app,
) -> None:
    """An ending announced to a process that was not there to hear it.

    `graph_event` only moves a row while this process is running. A run that
    finished during a restart would otherwise be a row that says "working"
    about a run the engine considers over.
    """
    store = InMemoryStateStore()
    app, runtime = _restarted(
        store,
        {RunId("run-graph"): _graph_snapshot(RunStatus.FAILED, "the checkout vanished")},
     workflow_app=workflow_app)

    restored = _after_a_restart(app, store, _interrupted_run())

    assert runtime.resumed == []
    assert restored.phase is RunPhase.FAILED
    assert restored.failure_reason == "the checkout vanished"


def test_a_graph_run_the_engine_has_forgotten_is_failed_rather_than_left_working(
    workflow_app,
) -> None:
    """State deleted from under a row -- `graph-state/` thrown away, say.

    Nothing can recover it and nobody will ever answer it, so it is failed with
    a reason. The alternative is a WorkOrder that claims to be working for as
    long as the database survives.
    """
    store = InMemoryStateStore()
    app, _ = _restarted(store, {}, workflow_app=workflow_app)

    restored = _after_a_restart(app, store, _interrupted_run())

    assert restored.phase is RunPhase.FAILED
    assert "no record" in restored.failure_reason


def test_a_graph_that_does_not_compile_stops_the_server_and_names_itself(
    caplog: pytest.LogCaptureFixture, *, workflow_app
) -> None:
    """A broken definition is not something to carry on without.

    A graph that does not compile means a file in this deployment's workflow
    directory says something that is not a graph. Starting anyway would serve a
    deployment nobody configured, and the person who could fix it would find out
    the first time somebody picked the workflow. So startup fails.

    What is logged is the only way anybody learns which one: the graph's id and
    the reason it would not compile. "a graph failed to compile" is not
    actionable in a directory holding several.
    """
    store = InMemoryStateStore()

    @asynccontextmanager
    async def broken(_app=None):
        raise GraphCompilationError(
            GraphId("implementation-review-codex"),
            ValueError("node 'review' is not reachable from '__start__'"),
        )
        yield  # pragma: no cover -- unreachable, and required to make this a CM

    app = workflow_app(
        store,
        ConcurrentRunner(),
        workflow_catalog=WorkflowCatalog.from_graphs((_review_graph(),)),
        graph_runtime=broken(),
    )

    async def scenario():
        async with app.router.lifespan_context(app):  # pragma: no cover -- raises
            pass

    with caplog.at_level(logging.ERROR, logger="engine.apps.web.api"):
        with pytest.raises(GraphCompilationError):
            asyncio.run(scenario())

    assert "implementation-review-codex" in caplog.text
    assert "not reachable" in caplog.text


def test_a_graph_engine_that_will_not_open_does_not_take_the_app_with_it(
    async_client, caplog: pytest.LogCaptureFixture, *, workflow_app
) -> None:
    """Everything else that can go wrong stays inside the graph feature.

    Opening the engine also creates a directory and opens two SQLite files, and
    those fail for reasons that are about this machine rather than about any
    graph: a state directory it cannot write, a checkpoint file another process
    is holding. None of them is a reason for chats and projects to go down, so
    the engine simply does not run here.

    The failure is logged, because it is the only place anybody could find out.
    """
    store = InMemoryStateStore()

    @asynccontextmanager
    async def refusing(_app=None):
        raise PermissionError("graph-state/: read-only file system")
        yield  # pragma: no cover -- unreachable, and required to make this a CM

    app = workflow_app(
        store,
        ConcurrentRunner(),
        workflow_catalog=WorkflowCatalog.from_graphs((_review_graph(),)),
        graph_runtime=refusing(),
    )

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            async with app.router.lifespan_context(app):
                return (
                    await client.get("/api/config"),
                    await client.post(
                        "/api/runs",
                        json={
                            "workflowId": "implementation-review-codex",
                            "prompt": "Add cancellation handling.",
                            "repository": "acme/api",
                        },
                    ),
                    await client.get("/graph/api/graphs"),
                )

    with caplog.at_level(logging.ERROR, logger="engine.apps.web.api"):
        config, refused, graph = asyncio.run(scenario())

    # The application is up, and answering about everything it can still do.
    assert config.status_code == 200
    assert config.json()["workflows"] == []
    # Nothing offers the graph, so picking one is picking something that does
    # not exist rather than something that cannot be started.
    assert refused.status_code == 400
    assert graph.status_code == 503
    assert "read-only file system" in caplog.text


@pytest.mark.parametrize("values, status", [
    ({"implementation_runner": "claude", "review_runner": "codex"}, 201),
    ({}, 201),
    ({"review_runner": "unknown"}, 400),
    ({"review_runner": ""}, 400),
    ({"review_runner": 42}, 400),
    ({"undeclared": "value"}, 400),
    ([], 400),
])
def test_graph_workorder_inputs_are_validated_and_passed_to_execution(
    values, status, *, async_client, workflow_app
):
    from engine.graph_runtime.inputs import WorkflowInput

    from graph_runtime_fakes import InputGraph

    graph = InputGraph(
        GraphId("inputs"), "Inputs",
        (ScriptedNode(NodeId("work"), (Say("Done"),)),),
        inputs=(
            WorkflowInput("implementation_runner", "Implementation runner", "codex", True, ("codex", "claude")),
            WorkflowInput("review_runner", "Review runner", "claude", True, ("codex", "claude")),
        ),
    )
    app, runtime = _graph_app(InMemoryStateStore(), graph, workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                config = (await client.get("/api/config")).json()
                offered = next(item for item in config["workflows"] if item["id"] == "inputs")
                assert offered["inputs"][0]["choices"] == ["codex", "claude"]
                response = await client.post("/api/runs", json={
                    "workflowId": "inputs", "repository": ".", "prompt": "Task",
                    "inputs": values,
                })
                assert response.status_code == status
                if status == 201:
                    snapshot = await runtime.snapshot(RunId(response.json()["runId"]))
                    assert snapshot.values["inputs"] == {
                        "implementation_runner": "codex", "review_runner": "claude", **values,
                    }
                else:
                    assert (await client.get("/api/runs")).json()["runs"] == []

    asyncio.run(scenario())


def test_a_disconnected_repository_runs_every_workorder_disconnected(
    tmp_path, *, async_client, workflow_app
):
    from graph_runtime_fakes import ModeGraph

    graph = ModeGraph(GraphId("modes"), "Modes", (ScriptedNode(NodeId("work"), (Say("Done"),)),))
    offline, online = tmp_path / "offline", tmp_path / "online"
    app, runtime = _graph_app(
        InMemoryStateStore(), graph,
        repos={"acme/offline": str(offline), "acme/online": str(online)},
        repo_modes={"acme/offline": "disconnected"},
     workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                config = (await client.get("/api/config")).json()
                assert config["repositories"] == [
                    {"name": "acme/offline", "path": str(offline), "mode": "disconnected"},
                    {"name": "acme/online", "path": str(online)},
                ]
                modes = {}
                for path in (offline, online):
                    response = await client.post("/api/runs", json={
                        "workflowId": "modes", "repository": str(path), "prompt": "Task",
                        "inputs": {"mode": "connected"},
                    })
                    assert response.status_code == 201
                    snapshot = await runtime.snapshot(RunId(response.json()["runId"]))
                    modes[path.name] = snapshot.values["inputs"]["mode"]
                assert modes == {"offline": "disconnected", "online": "connected"}

    asyncio.run(scenario())


def test_a_trusted_repository_auto_approves_its_workorders_only(
    tmp_path, *, async_client, workflow_app
):
    graph = ScriptedGraph(
        GraphId("trust"), "Trust", (ScriptedNode(NodeId("work"), (Say("Done"),)),),
    )
    trusted, other = tmp_path / "trusted", tmp_path / "other"
    app, runtime = _graph_app(
        InMemoryStateStore(), graph,
        repos={"acme/trusted": str(trusted), "acme/other": str(other)},
        trusted_repos=frozenset({"acme/trusted"}),
     workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                approved = {}
                for path in (trusted, other):
                    response = await client.post("/api/runs", json={
                        "workflowId": "trust", "repository": str(path), "prompt": "Task",
                    })
                    assert response.status_code == 201
                    snapshot = await runtime.snapshot(RunId(response.json()["runId"]))
                    approved[path.name] = snapshot.auto_approve_nodes
                assert approved == {"trusted": (NodeId("work"),), "other": ()}

    asyncio.run(scenario())


def test_a_disconnected_repository_is_matched_through_subfolders_and_worktrees(
    tmp_path, *, async_client, git_repo, workflow_app
):
    import subprocess

    from graph_runtime_fakes import ModeGraph

    def git(*args: str) -> None:
        subprocess.run(["git", *args], check=True, capture_output=True)

    offline = tmp_path / "offline"
    (offline / "src").mkdir(parents=True)
    git_repo(offline)
    git("-C", str(offline), "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-q", "--allow-empty", "-m", "init")
    other_worktree = tmp_path / "other-worktree"
    git("-C", str(offline), "worktree", "add", "-q", str(other_worktree))
    graph = ModeGraph(GraphId("modes"), "Modes", (ScriptedNode(NodeId("work"), (Say("Done"),)),))
    app, runtime = _graph_app(
        InMemoryStateStore(), graph,
        repos={"acme/offline": str(offline)},
        repo_modes={"acme/offline": "disconnected"},
     workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                for path in (offline / "src", other_worktree):
                    response = await client.post("/api/runs", json={
                        "workflowId": "modes", "repository": str(path), "prompt": "Task",
                        "inputs": {"mode": "connected"},
                    })
                    assert response.status_code == 201
                    snapshot = await runtime.snapshot(RunId(response.json()["runId"]))
                    assert snapshot.values["inputs"]["mode"] == "disconnected"

    asyncio.run(scenario())


def _runner_input_graph(*choices: str) -> ScriptedGraph:
    """A graph whose one input is an implementation runner offering `choices`."""
    from engine.graph_runtime.inputs import WorkflowInput

    from graph_runtime_fakes import InputGraph

    return InputGraph(
        GraphId("inputs"), "Inputs",
        (ScriptedNode(NodeId("work"), (Say("Done"),)),),
        inputs=(
            WorkflowInput(
                "implementation_runner", "Implementation runner", "codex", True, choices,
            ),
        ),
    )


def _least_utilized_app(
    tmp_path, read_test, cached: float | None = None, *, workflow_app
):
    """A least-utilized graph app whose "test" runner is read by `read_test`.

    `cached` seeds an hour-old "test" reading at that percentage, so the start
    still scrapes but has something to fall back to.
    """
    from engine.graph_runtime.inputs import LEAST_UTILIZED

    service = UtilizationService(
        cache_path=tmp_path / "utilization.json", readers={"test": read_test}
    )
    if cached is not None:
        service._write((RunnerUtilization(
            runner="test", read_at=time.time() - 2 * 60 * 60,
            windows=(UtilizationWindow("five_hour", "5-hour", cached, ""),),
        ),))
    return _graph_app(
        InMemoryStateStore(), _runner_input_graph("codex", "test", LEAST_UTILIZED),
        utilization=service,
     workflow_app=workflow_app)


def test_graph_workorder_round_robin_runner_resolves_at_start(
    async_client, workflow_app
):
    from engine.graph_runtime.inputs import ROUND_ROBIN

    graph = _runner_input_graph("codex", "claude", ROUND_ROBIN)
    app, runtime = _graph_app(InMemoryStateStore(), graph, workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                picked = []
                for _ in range(3):
                    response = await client.post("/api/runs", json={
                        "workflowId": "inputs", "repository": ".", "prompt": "Task",
                        "inputs": {"implementation_runner": ROUND_ROBIN},
                    })
                    assert response.status_code == 201
                    snapshot = await runtime.snapshot(RunId(response.json()["runId"]))
                    picked.append(snapshot.values["inputs"]["implementation_runner"])
                assert picked == ["codex", "claude", "codex"]

    asyncio.run(scenario())


def test_graph_workorder_least_utilized_runner_scrapes_before_choosing(
    tmp_path, *, async_client, workflow_app
):
    """Nothing else keeps the utilization cache warm, so starting the run reads it."""
    from engine.graph_runtime.inputs import LEAST_UTILIZED

    async def read_test(_client) -> RunnerUtilization:
        return RunnerUtilization(
            runner="test",
            windows=(UtilizationWindow("five_hour", "5-hour", 5.0, ""),),
        )

    app, runtime = _least_utilized_app(tmp_path, read_test, workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                response = await client.post("/api/runs", json={
                    "workflowId": "inputs", "repository": ".", "prompt": "Task",
                    "inputs": {"implementation_runner": LEAST_UTILIZED},
                })
                assert response.status_code == 201
                snapshot = await runtime.snapshot(RunId(response.json()["runId"]))
                return snapshot.values["inputs"]["implementation_runner"]

    assert asyncio.run(scenario()) == "test"
    assert (tmp_path / "utilization.json").exists()


def test_graph_workorder_least_utilized_falls_back_to_the_cache_when_the_scrape_hangs(
    monkeypatch, tmp_path, *, async_client, workflow_app
):
    from engine.apps.web import api as web_api
    from engine.graph_runtime.inputs import LEAST_UTILIZED

    async def hang(_client) -> RunnerUtilization:
        await asyncio.Event().wait()

    monkeypatch.setattr(web_api, "UTILIZATION_REFRESH_TIMEOUT_SECONDS", 0.05)
    app, runtime = _least_utilized_app(tmp_path, hang, cached=5.0, workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                response = await client.post("/api/runs", json={
                    "workflowId": "inputs", "repository": ".", "prompt": "Task",
                    "inputs": {"implementation_runner": LEAST_UTILIZED},
                })
                assert response.status_code == 201
                snapshot = await runtime.snapshot(RunId(response.json()["runId"]))
                return snapshot.values["inputs"]["implementation_runner"]

    assert asyncio.run(scenario()) == "test"


def test_graph_workorder_least_utilized_scrape_does_not_hold_up_other_starts(
    tmp_path, *, async_client, workflow_app
):
    """The scrape runs outside the lock every WorkOrder creation takes."""
    from engine.graph_runtime.inputs import LEAST_UTILIZED

    scraping, release = asyncio.Event(), asyncio.Event()

    async def slow(_client) -> RunnerUtilization:
        scraping.set()
        await release.wait()
        return RunnerUtilization(
            runner="test", windows=(UtilizationWindow("five_hour", "5-hour", 5.0, ""),),
        )

    app, _runtime = _least_utilized_app(tmp_path, slow, workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                placed = asyncio.create_task(client.post("/api/runs", json={
                    "workflowId": "inputs", "repository": ".", "prompt": "Placed",
                    "inputs": {"implementation_runner": LEAST_UTILIZED},
                }))
                try:
                    async with asyncio.timeout(5):
                        await scraping.wait()
                        explicit = await client.post("/api/runs", json={
                            "workflowId": "inputs", "repository": ".", "prompt": "Explicit",
                            "inputs": {"implementation_runner": "codex"},
                        })
                    assert explicit.status_code == 201
                    assert not placed.done()
                finally:
                    release.set()
                assert (await placed).status_code == 201

    asyncio.run(scenario())


def test_dependent_graph_workorder_resolves_its_runner_policy_when_it_starts(
    async_client, workflow_app
):
    """A scheduled dependent keeps the policy and is placed once it can run."""
    from engine.graph_runtime.inputs import ROUND_ROBIN

    graph = _runner_input_graph("codex", "claude", ROUND_ROBIN)
    store = InMemoryStateStore()
    app, runtime = _graph_app(store, graph, workflow_app=workflow_app)
    prerequisite = RunState(
        run_id=RunId("prerequisite"), task_id=TaskId("task-prerequisite"),
        workflow_id=WorkflowId(str(graph.graph_id)), phase=RunPhase.RUNNING_AGENT,
        repository=".",
    )

    async def scenario():
        await store.save(prerequisite)
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                response = await client.post("/api/runs", json={
                    "workflowId": "inputs", "repository": ".", "prompt": "Follow up",
                    "dependsOnRunId": "prerequisite",
                    "inputs": {"implementation_runner": ROUND_ROBIN},
                })
                assert response.status_code == 201
                dependent = RunId(response.json()["runId"])
                scheduled = await store.load(dependent)
                assert scheduled.phase is RunPhase.SCHEDULED
                assert scheduled.inputs["implementation_runner"] == ROUND_ROBIN

                await store.save(replace(prerequisite, phase=RunPhase.SUCCEEDED))
                started = await client.post(f"/api/runs/{dependent}/start")
                assert started.status_code == 200, started.text
                snapshot = await runtime.snapshot(dependent)
                return snapshot.values["inputs"]["implementation_runner"]

    assert asyncio.run(scenario()) == "codex"


def test_scheduled_graph_workorder_survives_restart_and_starts_with_same_id(
    async_client, workflow_app
) -> None:
    async def scenario():
        store = InMemoryStateStore()
        graph = ScriptedGraph(GraphId("scheduled-graph"), "Scheduled graph", (
            ScriptedNode(NodeId("work"), "Work", (AwaitSteering(),)),
        ))
        state = RunState(
            run_id=RunId("run-scheduled-graph"), task_id=TaskId("task-scheduled"),
            workflow_id=WorkflowId(str(graph.graph_id)), phase=RunPhase.SCHEDULED,
            name="Scheduled graph work", prompt="Do the work", repository=".",
        )
        await store.save(state)
        app, runtime = _graph_app(store, graph, workflow_app=workflow_app)
        async with app.router.lifespan_context(app):
            assert (await store.load(state.run_id)).phase is RunPhase.SCHEDULED
            assert await runtime.snapshot(state.run_id) is None
            async with async_client(app, base_url="http://test") as client:
                first, second = await asyncio.gather(
                    client.post("/api/runs/run-scheduled-graph/start"),
                    client.post("/api/runs/run-scheduled-graph/start"),
                )
                assert sorted([first.status_code, second.status_code]) == [200, 409]
                response = first if first.status_code == 200 else second
                assert (await client.post("/api/runs/missing/start")).status_code == 404
                assert len(await store.list_runs()) == 1
                assert response.json()["runId"] == state.run_id
                assert response.json()["phase"] != "scheduled"
                assert await runtime.snapshot(state.run_id) is not None
                assert (await client.post("/api/runs/run-scheduled-graph/start")).status_code == 409
    asyncio.run(scenario())


@pytest.mark.parametrize(("proposer", "expected"), [
    ("github:7:bob", "github:7:bob"),
    (None, "github:42:alice"),
])
def test_starting_a_scheduled_workorder_keeps_its_requester(
    async_client, proposer, expected, *, workflow_app
) -> None:
    """Whoever clicks Start does not replace the proposer as requester."""
    graph = _review_graph()
    store = InMemoryStateStore()
    app = _graph_app_over(
        store, ScriptedGraphRuntime(graph), graph,
        github_login_config=GitHubLoginConfig(
            "client", "secret", "https://engine.test/api/auth/github/callback"
        ),
     workflow_app=workflow_app)

    async def scenario():
        await store.save(RunState(
            run_id=RunId("run-proposed"), task_id=TaskId("task-proposed"),
            workflow_id=WorkflowId(str(graph.graph_id)), phase=RunPhase.SCHEDULED,
            name="Proposed work", prompt="Do the work", repository=".",
            requester=proposer,
        ))
        async with async_client(app, base_url="https://engine.test") as client:
            async with app.app.router.lifespan_context(app.app):
                return await client.post("/api/runs/run-proposed/start")

    with patch.object(
        GitHubLogin, "_read_session", return_value={"id": 42, "login": "alice"}
    ), patch.object(GitHubLogin, "has_access", AsyncMock(return_value=True)), patch.object(
        GitHubLogin, "visible_repositories", AsyncMock(return_value=None)
    ):
        started = asyncio.run(scenario())

    assert started.status_code == 200, started.text
    assert started.json()["requester"] == expected
    assert asyncio.run(store.load(RunId("run-proposed"))).requester == expected


def test_agent_created_workorder_links_to_its_creator(
    async_client, workflow_app
) -> None:
    from engine.runtime.terminal_mcp import TerminalMcpBroker, TerminalResultRegistry
    from engine.domain import AgentRunId

    graph = _review_graph()
    runtime = ScriptedGraphRuntime(graph)
    callbacks = []
    runtime.bind_workorder_creator = callbacks.append
    store = InMemoryStateStore()
    app = _graph_app_over(store, runtime, graph, workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                parent = (await client.post("/api/runs", json={
                    "workflowId": str(graph.graph_id), "prompt": "Original task",
                    "repository": "acme/api",
                })).json()
                broker = TerminalMcpBroker(
                    run_id=RunId(parent["runId"]), agent_run_id=AgentRunId("agent-1"),
                    step=None, registry=TerminalResultRegistry(),
                )
                broker.enable_workorder_creation(callbacks[0])
                result = await broker._submit({
                    "token": broker._token, "request_id": 1,
                    "name": "create_workorder", "arguments": {"prompt": "Follow up"},
                })
                assert result["ok"] is True
                child_id = json.loads(result["output"])["run_id"]
                child = (await client.get(f"/api/runs/{child_id}")).json()
                assert child["parentRunId"] == parent["runId"]
                assert child["repository"] == "acme/api"
                assert child["taskPrompt"] == "Follow up"
                assert (await client.get(f"/api/runs/{child['parentRunId']}")).status_code == 200

    asyncio.run(scenario())


def test_workorder_dependencies_wait_and_release_a_chain(
    async_client, workflow_app
) -> None:
    async def scenario():
        store = InMemoryStateStore()
        waiting = ScriptedGraph(GraphId("waiting"), "Waiting", (
            ScriptedNode(NodeId("work"), (AwaitSteering(),)),
        ))
        quick = ScriptedGraph(GraphId("quick"), "Quick", (
            ScriptedNode(NodeId("work"), (Say("Done"),)),
        ))
        app, runtime = _graph_app(store, waiting, quick, workflow_app=workflow_app)
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                async def create(graph, dependency=None):
                    body = {"workflowId": graph, "repository": ".", "prompt": "Task"}
                    if dependency:
                        body["dependsOnRunId"] = dependency
                    return await client.post("/api/runs", json=body)

                assert (await create("quick", "missing")).status_code == 400
                first = (await create("waiting")).json()["runId"]
                chain = [first]
                # Exceeds Python's usual recursion limit.
                for _ in range(1050):
                    response = await create("quick", chain[-1])
                    assert response.status_code == 201
                    assert response.json()["phase"] == "scheduled"
                    assert response.json()["dependsOnRunId"] == chain[-1]
                    chain.append(response.json()["runId"])
                    assert await runtime.snapshot(RunId(chain[-1])) is None
                assert (await client.post(f"/api/runs/{chain[1]}/start")).status_code == 409
                # Wait until the prerequisite is ready to receive steering.
                for _ in range(100):
                    snapshot = await runtime.snapshot(RunId(first))
                    if snapshot.active_executions:
                        break
                    await asyncio.sleep(0.001)
                await runtime.steer(RunId(first), "Proceed")
                async with asyncio.timeout(10):
                    while (await store.load(RunId(chain[-1]))).phase is not RunPhase.SUCCEEDED:
                        await asyncio.sleep(0.001)
                for run_id in chain:
                    assert (await store.load(RunId(run_id))).phase is RunPhase.SUCCEEDED
                ready = await create("waiting", chain[-1])
                assert ready.json()["phase"] != "scheduled"
                assert len(await store.list_runs()) == len(chain) + 1
        await runtime.aclose()
    asyncio.run(scenario())


def test_dependency_dispatch_recovers_after_restart_and_keeps_failed_prerequisites_blocked(
    workflow_app,
) -> None:
    async def scenario():
        store = InMemoryStateStore()
        graph = _review_graph()
        for run_id, phase, dependency in (
            ("complete", RunPhase.SUCCEEDED, None),
            ("failed", RunPhase.FAILED, None),
            ("ready", RunPhase.SCHEDULED, "complete"),
            ("blocked", RunPhase.SCHEDULED, "failed"),
        ):
            await store.save(RunState(
                run_id=RunId(run_id), task_id=TaskId(run_id),
                workflow_id=WorkflowId(str(graph.graph_id)), repository=".",
                phase=phase, depends_on_run_id=RunId(dependency) if dependency else None,
            ))
        app, runtime = _graph_app(store, graph, workflow_app=workflow_app)
        async with app.router.lifespan_context(app):
            for _ in range(1000):
                if (await store.load(RunId("ready"))).phase is RunPhase.SUCCEEDED:
                    break
                await asyncio.sleep(0.001)
            assert (await store.load(RunId("ready"))).phase is RunPhase.SUCCEEDED
            assert (await store.load(RunId("blocked"))).phase is RunPhase.SCHEDULED
            assert await runtime.snapshot(RunId("blocked")) is None
        await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("creation", ["api", "agent"])
def test_dependency_creation_during_prerequisite_cancellation_is_rejected(
    creation, *, async_client, web_app
) -> None:
    async def scenario():
        store = InMemoryStateStore()
        graph = _review_graph()
        runtime = ScriptedGraphRuntime(graph)
        callbacks = []
        runtime.bind_workorder_creator = callbacks.append
        cancelling = asyncio.Event()
        release = asyncio.Event()

        async def cancel(run_id):
            cancelling.set()
            await release.wait()

        runtime.cancel = cancel

        @asynccontextmanager
        async def running(_app=None):
            yield runtime

        app = web_app(
            _session_with({"test": ConcurrentRunner()}, state_store=store),
            {"test": ConcurrentRunner()},
            workflow_catalog=WorkflowCatalog.from_graphs((graph,)),
            graph_runtime=running(),
        )
        async with app.router.lifespan_context(app):
            prerequisite = RunState(
                run_id=RunId("prerequisite"), task_id=TaskId("task"),
                workflow_id=WorkflowId(str(graph.graph_id)),
                phase=RunPhase.RUNNING_AGENT, repository=".",
            )
            await store.save(prerequisite)
            async with async_client(app, base_url="http://test") as client:
                deletion = asyncio.create_task(client.delete("/api/runs/prerequisite"))
                try:
                    async with asyncio.timeout(5):
                        await cancelling.wait()
                        if creation == "api":
                            result = await client.post("/api/runs", json={
                                "workflowId": str(graph.graph_id), "repository": ".",
                                "prompt": "Follow up", "dependsOnRunId": "prerequisite",
                            })
                            assert result.status_code == 400
                        elif creation == "agent":
                            with pytest.raises(ValueError, match="prerequisite"):
                                await callbacks[0](prerequisite.run_id, "Follow up", prerequisite.run_id)
                    assert await store.list_runs() == (prerequisite,)
                finally:
                    release.set()
                    result = await deletion
                assert result.status_code == 204
                assert await store.list_runs() == ()
        await runtime.aclose()

    asyncio.run(scenario())


def test_graph_frontier_is_seeded_once_when_restoring_pending_approvals(
    monkeypatch, *, async_client, workflow_app
):
    from engine.graph_runtime.control import PendingApproval
    from engine.graph_runtime.identity import ExecutionId

    store = InMemoryStateStore()
    snapshot = replace(
        _graph_snapshot(RunStatus.AWAITING_APPROVAL),
        next_nodes=(NodeId("implementation"),),
        pending_approvals=(PendingApproval(
            ApprovalId("approval"), ExecutionId("execution"), NodeId("implementation"),
            ApprovalKind.COMMAND_EXECUTION,
        ),),
    )
    app, runtime = _restarted(store, {snapshot.run_id: snapshot}, workflow_app=workflow_app)

    async def scenario():
        await store.save(_interrupted_run())
        async with app.router.lifespan_context(app):
            async def no_snapshot(_run_id):
                raise AssertionError("restored frontier must not read checkpoints on polls")

            monkeypatch.setattr(runtime, "snapshot", no_snapshot)
            async with async_client(app, base_url="http://test") as client:
                return (await client.get("/api/runs")).json()["runs"][0]

    row = asyncio.run(scenario())
    assert row["graphProgress"] == {
        "activeNodeIds": [],
        "waitingNodeIds": ["implementation"],
        "nextNodeIds": ["implementation"],
    }


def test_finished_run_resumed_after_restart_gets_live_frontier(
    async_client, workflow_app
):
    from engine.graph_runtime.events import EventKind, RuntimeEvent

    store = InMemoryStateStore()
    snapshot = _graph_snapshot(RunStatus.COMPLETED)
    app, runtime = _restarted(store, {snapshot.run_id: snapshot}, workflow_app=workflow_app)

    async def scenario():
        await store.save(replace(_interrupted_run(), phase=RunPhase.SUCCEEDED))
        async with app.router.lifespan_context(app):
            await runtime._observer(RuntimeEvent(
                snapshot.run_id, EventKind.RUN_FORKED, {"nodes": ["implementation"]},
            ))
            async with async_client(app, base_url="http://test") as client:
                return (await client.get("/api/runs")).json()["runs"][0]

    row = asyncio.run(scenario())
    assert row["graphProgress"] == {
        "activeNodeIds": [], "waitingNodeIds": [], "nextNodeIds": ["implementation"],
    }


@pytest.mark.parametrize("login_enabled", [False, True])
def test_health_identity_and_lifecycle(login_enabled, *, client, web_app):
    from importlib.metadata import version
    from engine.apps.web.github_login import GitHubLoginConfig

    runner = ConcurrentRunner()
    app = web_app(
        _session(runner), {"test": runner},
        github_login_config=(GitHubLoginConfig(
            "client", "secret", "https://engine.test/api/auth/github/callback"
        ) if login_enabled else None),
    )
    expected = {"service": "openengine", "version": version("engine-web"),
                "ready": False, "api_version": 1}
    with client(app) as browser:
        response = browser.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {**expected, "ready": True}
        assert response.headers["cache-control"] == "no-store"
        assert browser.get("/api/health").json() == response.json()
        if login_enabled:
            assert browser.get("/api/threads").status_code == 401
    response = browser.get("/api/health")
    assert response.status_code == 503
    assert response.json() == expected


def test_health_not_ready_when_configured_graph_runtime_fails(client, web_app):
    @asynccontextmanager
    async def broken_runtime():
        raise OSError("cannot open graph state")
        yield

    runner = ConcurrentRunner()
    app = web_app(_session(runner), {"test": runner}, graph_runtime=broken_runtime())
    with client(app) as browser:
        response = browser.get("/api/health")
        assert response.status_code == 503
        assert response.json()["ready"] is False
        assert response.json()["service"] == "openengine"


def test_production_port_default_preserves_explicit_settings():
    assert Settings().port == 4364
    assert Settings(port=8123).port == 8123


def _login_gate(
    repository: str,
    source_control: object,
    login_repositories=(),
    check="authorize",
    *,
    web_app,
):
    """The `check` the app hands its GitHub login, over `source_control`."""
    unused = object()
    session = AgentSession(
        Capabilities(
            workflow_runtime=unused, source_control=source_control,
            agent_runner=ConcurrentRunner(), communications=unused,
            workspace_provider=ConversationWorkspaces(),
            state_store=InMemoryStateStore(),
        ),
        profiles=PROFILES, runners={"test": ConcurrentRunner()},
    )
    app = web_app(
        session, {"test": ConcurrentRunner()},
        workflow_catalog=WorkflowCatalog.from_graphs(()),
        github_login_config=GitHubLoginConfig(
            "client", "secret", "https://engine.test/api/auth/github/callback"
        ),
        github_repository=repository,
        login_repositories=login_repositories,
    )
    callback = next(
        route.endpoint for route in app.app.routes
        if getattr(route, "path", "") == "/api/auth/github/callback"
    )
    return getattr(callback.__self__, check)


def test_signing_in_requires_write_access_to_the_configured_repository(web_app) -> None:
    """WorkOrders are visible to the people who can push to the repository they
    work on, and to nobody else with a GitHub account."""
    source_control = MagicMock(can_write_repository=AsyncMock(side_effect=[True, False]))
    authorize = _login_gate("acme/api", source_control, web_app=web_app)

    assert asyncio.run(authorize(1, "maintainer")) == {"acme/api": True}
    assert asyncio.run(authorize(2, "stranger")) == {"acme/api": False}
    assert [
        (call.args, call.kwargs) for call in source_control.can_write_repository.await_args_list
    ] == [
        (("https://github.com/acme/api/pull/1", "maintainer"), {"user_id": 1}),
        (("https://github.com/acme/api/pull/1", "stranger"), {"user_id": 2}),
    ]


def test_signing_in_is_refused_without_a_repository_to_check(web_app) -> None:
    source_control = MagicMock(can_write_repository=AsyncMock(return_value=True))
    authorize = _login_gate("", source_control, web_app=web_app)

    assert asyncio.run(authorize(1, "maintainer")) == {}
    source_control.can_write_repository.assert_not_awaited()


def test_every_configured_repository_is_asked_and_a_failure_is_unknown(web_app) -> None:
    """Each repository gets its own answer, so what a user sees can be scoped to
    the ones they can push to; a lookup that fails is unknown, not a no."""
    async def can_write(pr_url, login, *, user_id):
        if "acme/api" in pr_url:
            raise RuntimeError("GitHub is down")
        return "acme/web" in pr_url

    source_control = MagicMock(can_write_repository=AsyncMock(side_effect=can_write))
    authorize = _login_gate("Acme/API", source_control, ("acme/docs", "acme/web", "acme/api"), web_app=web_app)

    assert asyncio.run(authorize(1, "maintainer")) == {
        "acme/api": None, "acme/docs": False, "acme/web": True,
    }
    assert sorted(
        call.args[0] for call in source_control.can_write_repository.await_args_list
    ) == [
        "https://github.com/acme/api/pull/1",
        "https://github.com/acme/docs/pull/1",
        "https://github.com/acme/web/pull/1",
    ]


def test_the_users_own_token_is_asked_about_public_github_repositories(
    monkeypatch, *, web_app
) -> None:
    """The stand-in for a failed server lookup asks GitHub with the signed-in
    user's token, reads only a write role as a yes, and skips other forges."""
    asked = []

    def github(request):
        asked.append((request.url.path, request.headers["authorization"]))
        if request.url.path == "/repos/acme/api":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"permissions": {"admin": False, "push": True}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "engine.apps.web.api.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(github), **kwargs),
    )
    authorize_user = _login_gate(
        "acme/api", MagicMock(), ("acme/web", "gitlab.example/acme/ops"), "authorize_user",
        web_app=web_app,
    )

    assert asyncio.run(authorize_user("user-token")) == {"acme/api": None, "acme/web": True}
    assert sorted(asked) == [
        ("/repos/acme/api", "Bearer user-token"),
        ("/repos/acme/web", "Bearer user-token"),
    ]


def test_the_users_own_token_without_a_write_role_is_no_yes(
    monkeypatch, *, web_app
) -> None:
    def github(request):
        return httpx.Response(200, json={"permissions": {"pull": True, "push": False}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "engine.apps.web.api.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(github), **kwargs),
    )
    authorize_user = _login_gate("acme/api", MagicMock(), check="authorize_user", web_app=web_app)

    assert asyncio.run(authorize_user("user-token")) == {"acme/api": False}


def test_retired_project_routes_and_conversation_ownership_are_absent(
    async_client, *, web_app
) -> None:
    runner = ConcurrentRunner()
    store = InMemoryStateStore()
    app = web_app(_session_with({"test": runner}, state_store=store), {"test": runner})

    async def scenario():
        async with async_client(app, base_url="http://test") as client:
            for method, path in (
                ("GET", "/api/projects"),
                ("POST", "/api/projects"),
                ("GET", "/api/projects/old/milestones"),
                ("POST", "/api/projects/old/milestones/goal/scope"),
            ):
                assert (await client.request(method, path)).status_code == 404
            config = (await client.get("/api/config")).json()
            assert "showProjects" not in config
            assert "planAgent" not in config
            # Old clients cannot create a conversation-owned project anymore.
            response = await client.post("/api/threads", json={
                "agentId": "coder", "runner": "test", "createProject": True,
            })
            assert response.status_code == 201
            assert response.json()["title"] == "New chat"
            assert not hasattr(store, "save_project")

    asyncio.run(scenario())


def _scoped_app(tmp_path, runtime=None, *, workflow_app):
    """Two checkouts behind GitHub login, and a run in each place a run can be."""
    graph = _review_graph()
    store = InMemoryStateStore()
    repos = {"api": str(tmp_path / "api"), "web": str(tmp_path / "web")}
    app = _graph_app_over(
        store, runtime or ScriptedGraphRuntime(graph), graph,
        github_login_config=GitHubLoginConfig(
            "client", "secret", "https://engine.test/api/auth/github/callback"
        ),
        repos=repos,
        login_repositories=("acme/api", "acme/web"),
        repository_projects={"api": "acme/api", "web": "acme/web"},
     workflow_app=workflow_app)
    for run_id, repository, phase in (
        ("run-api", "api", RunPhase.SUCCEEDED),
        ("run-api-path", str((tmp_path / "api").resolve()), RunPhase.SUCCEEDED),
        # Started from a GitHub comment or assignment, which names the project.
        ("run-github", "Acme/API", RunPhase.SUCCEEDED),
        ("run-web", "web", RunPhase.SUCCEEDED),
        ("run-web-scheduled", "web", RunPhase.SCHEDULED),
        ("run-elsewhere", "/srv/unknown", RunPhase.SUCCEEDED),
    ):
        asyncio.run(store.save(RunState(
            run_id=RunId(run_id), task_id=TaskId(f"task-{run_id}"),
            workflow_id=WorkflowId(str(graph.graph_id)), phase=phase,
            prompt="Do the work", repository=repository,
        )))
    return app, store, repos


def _as_user(writable):
    """Requests from a signed-in user who can write to `writable` only."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(
        GitHubLogin, "_read_session", return_value={"id": 7, "login": "bob"}
    ))
    stack.enter_context(patch.object(GitHubLogin, "has_access", AsyncMock(return_value=True)))
    stack.enter_context(patch.object(
        GitHubLogin, "writable_repositories", AsyncMock(return_value=frozenset(writable))
    ))
    return stack


def test_runs_are_scoped_to_the_repositories_a_user_can_write_to(
    async_client, tmp_path, *, workflow_app
) -> None:
    """Someone who can push only to `api` sees `api`'s WorkOrders, and every
    other run answers as if it did not exist."""
    app, store, repos = _scoped_app(tmp_path, workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="https://engine.test") as client:
            async with app.app.router.lifespan_context(app.app):
                listed = (await client.get("/api/runs")).json()["runs"]
                assert sorted(run["runId"] for run in listed) == [
                    "run-api", "run-api-path", "run-github",
                ]
                assert (await client.get("/api/runs/run-api")).status_code == 200
                for method, path in (
                    ("GET", "/api/runs/run-web"),
                    ("GET", "/api/runs/run-elsewhere"),
                    ("POST", "/api/runs/run-web-scheduled/start"),
                    ("DELETE", "/api/runs/run-web"),
                    ("GET", "/api/runs/run-web/graph-events"),
                    ("GET", "/api/runs/run-web/github-comments"),
                    ("GET", "/graph/api/runs/run-web"),
                    ("POST", "/graph/api/runs/run-web/cancel"),
                ):
                    response = await client.request(method, path)
                    assert response.status_code == 404, (method, path, response.text)
                    assert response.json() == {"error": "run not found"}
                assert (await client.post("/graph/api/runs", json={})).status_code == 403
                refused = await client.post("/api/runs", json={
                    "workflowId": "implementation-review-codex",
                    "prompt": "Change the web app.",
                    "repository": str(Path(repos["web"]).resolve()),
                })
                assert refused.status_code == 403
                # Another repository's run is as unknown a prerequisite as one
                # that does not exist, whatever state it is in.
                for prerequisite in ("run-web", "run-missing"):
                    hidden = await client.post("/api/runs", json={
                        "workflowId": "implementation-review-codex",
                        "prompt": "Follow up.",
                        "repository": str(Path(repos["api"]).resolve()),
                        "dependsOnRunId": prerequisite,
                    })
                    assert hidden.status_code == 400, hidden.text
                    assert hidden.json() == {
                        "error": f"unknown prerequisite workorder: {prerequisite}",
                    }
                config = (await client.get("/api/config")).json()
                assert [repo["name"] for repo in config["repositories"]] == ["api"]

    with _as_user({"acme/api"}):
        asyncio.run(scenario())
    assert asyncio.run(store.load(RunId("run-web"))) is not None
    assert asyncio.run(store.load(RunId("run-web-scheduled"))).phase is RunPhase.SCHEDULED


def test_a_graph_run_stream_is_rechecked_against_its_own_repository(
    async_client, tmp_path, *, workflow_app
) -> None:
    """A stream from `/graph` ends when its run's repository is out of reach,
    even for someone who can still write to another repository."""
    from engine.apps.web.github_login import STREAM_ACCESS

    app, _store, _repos = _scoped_app(tmp_path, workflow_app=workflow_app)
    scopes = []

    async def recording(scope, receive, send):
        scopes.append(scope)
        await app(scope, receive, send)

    writable = {"acme/api"}

    async def scenario():
        async with async_client(recording, base_url="https://engine.test") as client:
            async with app.app.router.lifespan_context(app.app):
                await client.get("/graph/api/runs/run-api")
                still_visible = scopes[-1][STREAM_ACCESS]
                assert await still_visible() is True
                writable.clear()
                writable.add("acme/web")
                assert await still_visible() is False

    with _as_user(()), patch.object(
        GitHubLogin, "writable_repositories",
        AsyncMock(side_effect=lambda *_args, **_kwargs: frozenset(writable)),
    ):
        asyncio.run(scenario())


def test_an_agent_cannot_depend_on_another_repositorys_run(
    tmp_path, *, workflow_app
) -> None:
    """An agent's WorkOrder may wait on runs in its own repository only; any
    other run is as unknown as a missing one, whatever its state."""
    runtime = ScriptedGraphRuntime(_review_graph())
    creators = []
    runtime.bind_workorder_creator = creators.append
    app, store, _repos = _scoped_app(tmp_path, runtime, workflow_app=workflow_app)

    async def scenario():
        async with app.app.router.lifespan_context(app.app):
            create = creators[0]
            for prerequisite in ("run-web", "run-web-scheduled", "run-elsewhere", "run-missing"):
                with pytest.raises(ValueError) as refused:
                    await create(RunId("run-api"), "Follow up", RunId(prerequisite))
                assert str(refused.value) == f"unknown prerequisite workorder: {prerequisite}"
            # The same repository, named by its checkout path or GitHub project.
            for prerequisite in ("run-api-path", "run-github"):
                _url, child = await create(RunId("run-api"), "Follow up", RunId(prerequisite))
                assert (await store.load(RunId(child))).depends_on_run_id == prerequisite

    asyncio.run(scenario())


class BranchingWorkspaces(ConversationWorkspaces):
    """Checkouts whose branches are made in the repository they came from, as
    a worktree's are, so which repository a chat is in can be read back."""

    def _workspace(self, workspace_id: str, repository: str, base_ref: str) -> Workspace:
        import subprocess

        subprocess.run(["git", "-C", repository, "branch", "--force", f"engine/{workspace_id}"],
                       check=True, capture_output=True)
        return super()._workspace(workspace_id, repository, base_ref)


def _scoped_chat_app(tmp_path, default="/repository", *, git_repo, web_app):
    """Two real checkouts, `api` and `web`, behind GitHub login."""
    import subprocess

    repos = {"api": str(tmp_path / "api"), "web": str(tmp_path / "web")}
    for path in repos.values():
        git_repo(path)
        subprocess.run(["git", "-C", path, "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    runner = ConcurrentRunner()
    store = InMemoryStateStore()
    workspaces = BranchingWorkspaces()
    app = web_app(
        _workspace_session(runner, workspaces, store, repos.get(default, default)),
        {"test": runner},
        github_login_config=GitHubLoginConfig(
            "client", "secret", "https://engine.test/api/auth/github/callback"
        ),
        repos=repos,
        login_repositories=("acme/api", "acme/web"),
        repository_projects={"api": "acme/api", "web": "acme/web"},
    )
    return app, store, repos, workspaces


def test_chats_are_scoped_to_the_repositories_a_user_can_write_to(
    async_client, tmp_path, *, git_repo, web_app
) -> None:
    """A chat whose branch is in a repository its user cannot push to is not
    listed and answers as missing, attached or detached, so its transcript
    cannot be read nor its agent prompted, and its checkout cannot be kept by
    naming another repository."""
    app, store, repos, workspaces = _scoped_chat_app(tmp_path, git_repo=git_repo, web_app=web_app)

    async def scenario():
        async with async_client(app, base_url="https://engine.test") as client:
            ids = {}
            for name in ("api", "web", None):
                instance = await store.create_instance(CODER)
                ids[name] = str(instance.instance_id)
                if name is not None:
                    attached = await client.post(
                        f"/api/threads/{ids[name]}/workspace", json={"repository": repos[name]},
                    )
                    assert attached.status_code == 200, attached.text
            return ids

    async def as_api_user(ids):
        async with async_client(app, base_url="https://engine.test") as client:
            listed = {t["id"] for t in (await client.get("/api/threads")).json()["threads"]}
            assert ids["api"] in listed and ids["web"] not in listed
            # A chat that never had a checkout is in no repository.
            for name in ("api", None):
                assert (await client.get(f"/api/threads/{ids[name]}")).status_code == 200
            for detached in (False, True):
                hidden = f"/api/threads/{ids['web']}"
                for method, path, body in (
                    ("GET", hidden, None),
                    ("GET", f"{hidden}/messages", None),
                    ("POST", f"{hidden}/runs", {"text": "hello"}),
                    ("DELETE", f"{hidden}/runs/current", None),
                    ("POST", f"{hidden}/workspace", {"repository": repos["api"]}),
                    ("DELETE", hidden, None),
                ):
                    response = await client.request(method, path, json=body)
                    assert response.status_code == 404, (detached, method, path, response.text)
                if not detached:
                    # The branch outlives its checkout, and still says whose it is.
                    workspace_id = (await store.load_instance(ids["web"])).workspace_id
                    workspaces.detached.add(workspace_id)

    # Set up by an operator, who can put a chat in either repository.
    with _as_user(()), patch.object(
        GitHubLogin, "visible_repositories", AsyncMock(return_value=None)
    ):
        ids = asyncio.run(scenario())
    with _as_user({"acme/api"}):
        asyncio.run(as_api_user(ids))


def test_a_new_chat_is_refused_the_default_checkout_its_user_cannot_write_to(
    async_client, tmp_path, *, git_repo, web_app
) -> None:
    """A new chat gets the default checkout at once, so it is refused before
    anything is checked out for somebody who cannot write there."""
    app, store, _repos, _workspaces = _scoped_chat_app(tmp_path, default="web", git_repo=git_repo, web_app=web_app)

    async def scenario():
        async with async_client(app, base_url="https://engine.test") as client:
            refused = await client.post("/api/threads", json={"agentId": str(CODER), "runner": "test"})
            assert refused.status_code == 403, refused.text
            assert await store.list_instances() == ()

    with _as_user({"acme/api"}):
        asyncio.run(scenario())
    with _as_user({"acme/web"}):
        async def allowed():
            async with async_client(app, base_url="https://engine.test") as client:
                created = await client.post("/api/threads", json={"agentId": str(CODER), "runner": "test"})
                assert created.status_code == 201, created.text
        asyncio.run(allowed())


def test_a_chat_checkout_is_scoped_to_the_repositories_a_user_can_write_to(
    async_client, tmp_path, *, git_repo, web_app
) -> None:
    """A chat cannot be given a checkout of a repository its user cannot push
    to, whether named or the server's default, attached for the first time or
    again after a detach."""
    app, store, repos, _workspaces = _scoped_chat_app(tmp_path, git_repo=git_repo, web_app=web_app)

    async def scenario():
        instance = await store.create_instance(CODER)
        path = f"/api/threads/{instance.instance_id}/workspace"
        async with async_client(app, base_url="https://engine.test") as client:
            for body in ({"repository": str(Path(repos["web"]).resolve())}, {"repository": "web"}, {}):
                refused = await client.post(path, json=body)
                assert refused.status_code == 403, (body, refused.text)
            assert (await store.load_instance(instance.instance_id)).workspace_id is None
            api = {"repository": str(Path(repos["api"]).resolve())}
            assert (await client.post(path, json=api)).status_code == 200
            assert (await client.delete(path)).status_code == 200
            assert (await client.post(path, json={"repository": "web"})).status_code == 403
            assert (await client.post(path, json=api)).status_code == 200

    with _as_user({"acme/api"}):
        asyncio.run(scenario())


def test_operators_see_every_run(async_client, tmp_path, *, workflow_app) -> None:
    """Operators see everything, including runs no GitHub repository maps to."""
    app, _store, _repos = _scoped_app(tmp_path, workflow_app=workflow_app)

    async def scenario():
        async with async_client(app, base_url="https://engine.test") as client:
            async with app.app.router.lifespan_context(app.app):
                listed = (await client.get("/api/runs")).json()["runs"]
                assert (await client.get("/api/runs/run-elsewhere")).status_code == 200
                config = (await client.get("/api/config")).json()
                return {run["runId"] for run in listed}, config["repositories"]

    with patch.object(
        GitHubLogin, "_read_session", return_value={"id": 42, "login": "alice"}
    ), patch.object(GitHubLogin, "has_access", AsyncMock(return_value=True)), patch.object(
        GitHubLogin, "visible_repositories", AsyncMock(return_value=None)
    ):
        runs, repositories = asyncio.run(scenario())
    assert runs == {
        "run-api", "run-api-path", "run-github", "run-web", "run-web-scheduled", "run-elsewhere",
    }
    assert [repo["name"] for repo in repositories] == ["api", "web"]


def test_engines_own_info_lines_reach_the_log():
    """Without a handler Python prints only warnings, so every decision Engine
    logged at INFO -- a webhook ignored and why -- went nowhere."""
    import logging

    from engine.apps.web.__main__ import configure_logging

    engine = logging.getLogger("engine")
    before = engine.level
    try:
        configure_logging()
        assert logging.getLogger("engine.apps.web.github_ingress").isEnabledFor(logging.INFO)
    finally:
        engine.setLevel(before)


def test_onboarded_repository_can_be_selected_for_a_workorder(
    tmp_path, monkeypatch, *, async_client, workflow_app
):
    from engine.apps.web.repositories import ensure_repository_checkouts

    checkout = tmp_path / "repo"
    repos = {"owner/repo": str(checkout)}

    def clone(args, **kwargs):
        Path(args[-1]).mkdir()

    monkeypatch.setattr("engine.apps.web.repositories.subprocess.run", clone)
    ensure_repository_checkouts(repos)
    graph = ScriptedGraph(
        GraphId("repo-edit"), "Edit repo",
        (ScriptedNode(NodeId("work"), (Say("Done"),)),),
    )
    store = InMemoryStateStore()
    app, _ = _graph_app(store, graph, repos=repos, workflow_app=workflow_app)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with async_client(app, base_url="http://test") as client:
                config = (await client.get("/api/config")).json()
                choice = next(r for r in config["repositories"] if r["name"] == "owner/repo")
                assert Path(choice["path"]).is_dir()
                response = await client.post("/api/runs", json={
                    "workflowId": "repo-edit", "repository": choice["path"],
                    "prompt": "Edit and lint repo",
                })
                assert response.status_code == 201, response.text
                run = await store.load(RunId(response.json()["runId"]))
                assert run.repository == str(checkout)

    asyncio.run(scenario())
