"""Loops: the new loop form's API, when a loop runs next, and a loop run."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from engine.apps.web.loop_runs import (
    Loop,
    LoopHost,
    LoopRunner,
    LoopStore,
    LoopWorkOrder,
    next_run_at,
    parse_loop,
)
from engine.apps.web.loops import LoopSettings, LoopSettingsStore
from engine.domain import RunId, RunPhase, RunState, TaskId, WorkflowId

NOON = datetime(2026, 10, 1, 12, 0, tzinfo=timezone(timedelta(hours=-6)))
FORM = {
    "name": "Dead code",
    "repository": ".",
    "prompt": "Find and delete unused code.",
    "everyMinutes": 30,
    "activeHours": {"start": "09:00", "end": "17:00"},
    "maxWorkOrders": 2,
    "maxDailySpend": 5,
}


def _state(run_id: str, phase: RunPhase, repository: str = ".") -> RunState:
    return RunState(RunId(run_id), TaskId(run_id), WorkflowId("graph"), phase=phase,
                    name=f"WorkOrder {run_id}", repository=repository)


def test_the_form_is_parsed_and_refused_when_a_loop_could_not_run() -> None:
    loop = parse_loop(FORM, ["."], NOON)
    assert (loop.name, loop.every_minutes, loop.max_workorders, loop.max_daily_spend) == (
        "Dead code", 30, 2, 5.0)
    assert (loop.active_hours_start, loop.active_hours_end) == ("09:00", "17:00")
    for bad in ({"prompt": " "}, {"repository": "/elsewhere"}, {"everyMinutes": 0},
                {"activeHours": {"start": "9", "end": "17:00"}}, {"maxWorkOrders": True},
                {"maxDailySpend": float("nan")}):
        with pytest.raises(ValueError):
            parse_loop({**FORM, **bad}, ["."], NOON)


def test_next_run_follows_the_interval_active_hours_and_exit_criteria() -> None:
    loop = parse_loop(FORM, ["."], NOON)
    assert next_run_at(loop, NOON, capped=False) == NOON
    ran = replace(loop, last_run_at=NOON.isoformat())
    assert next_run_at(ran, NOON, capped=False) == NOON + timedelta(minutes=30)
    evening = NOON.replace(hour=18)
    assert next_run_at(loop, evening, capped=False) == NOON.replace(day=2, hour=9)
    assert next_run_at(loop, NOON, capped=True) == NOON.replace(day=2, hour=9)


class Host:
    """Runs that finish when the test says so."""

    def __init__(self) -> None:
        self.runs: dict[str, RunState] = {}
        self.created: list[str] = []
        self.costs: dict[str, float] = {}
        self.allowed = True

    async def create(self, loop: Loop, prompt: str) -> str:
        run_id = f"run-{len(self.created) + 1}"
        self.created.append(run_id)
        self.runs[run_id] = _state(run_id, RunPhase.RUNNING_AGENT)
        return run_id

    async def load(self, run_id: str) -> RunState | None:
        return self.runs.get(run_id)

    async def list_runs(self, repository: str) -> list[RunState]:
        return list(self.runs.values())

    async def steer(self, run_id: str, prompt: str) -> None:
        pass

    def finish(self, run_id: str) -> None:
        self.runs[run_id] = _state(run_id, RunPhase.SUCCEEDED)

    async def may_act(self, loop: Loop) -> bool:
        return self.allowed

    def host(self) -> LoopHost:
        return LoopHost(create=self.create, load=self.load, list_runs=self.list_runs,
                        steer=self.steer, resume=self.steer,
                        spend=lambda run_id: self.costs.get(run_id, 0.0),
                        may_act=self.may_act, now=lambda: NOON)


async def _call(config: dict, calls: list[tuple[str, dict]]) -> list[dict]:
    """Real stdio child -> loopback broker -> the runner's tools."""
    process = await asyncio.create_subprocess_exec(
        config["command"], *config["args"], stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    requests = [{"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, *[
        {"jsonrpc": "2.0", "id": index + 1, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}}
        for index, (name, arguments) in enumerate(calls)
    ]]
    stdout, stderr = await process.communicate(
        "".join(json.dumps(request) + "\n" for request in requests).encode())
    assert process.returncode == 0, stderr.decode()
    responses = [json.loads(line)["result"] for line in stdout.splitlines()]
    assert {tool["name"] for tool in responses[0]["tools"]} >= {
        "create_workorder", "list_workorders", "steer_workorder", "defer_until"}
    return responses[1:]


class ScriptedProvider:
    """An agent that makes the tool calls of each turn in order."""

    name = "scripted"

    def __init__(self, turns: list[list[tuple[str, dict]]], on_turn=None) -> None:
        self.turns, self.on_turn = turns, on_turn
        self.prompts: list[str] = []
        self.results: list[list[dict]] = []

    async def connect(self):
        provider = self

        class Client:
            async def new_session(self, *, cwd, mcp_servers):
                self.config = mcp_servers[0]
                return self

            async def prompt(self, prompt):
                provider.prompts.append(prompt)
                if provider.on_turn:
                    provider.on_turn(len(provider.prompts))
                calls = provider.turns.pop(0) if provider.turns else []
                provider.results.append(await _call(self.config, calls) if calls else [])
                return
                yield

            async def close(self):
                pass

        return Client()


async def _created(host: Host, run_id: str) -> None:
    async with asyncio.timeout(10):
        while run_id not in host.runs:
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)


def _runner(tmp_path, host: Host, provider, **changes) -> tuple[LoopRunner, Loop]:
    store = LoopStore(tmp_path / "loops.json")
    loop = parse_loop({**FORM, **changes}, ["."], NOON)
    store.save(loop)
    return LoopRunner(store, host.host(), provider, poll_seconds=0.01), loop


def test_a_loop_waits_for_its_workorder_before_its_next_turn(tmp_path) -> None:
    host = Host()
    seen: list[RunPhase] = []
    provider = ScriptedProvider(
        [[("create_workorder", {"prompt": "Delete unused helpers"})], []],
        on_turn=lambda turn: seen.append(host.runs["run-1"].phase) if turn > 1 else None,
    )
    runner, loop = _runner(tmp_path, host, provider)

    async def scenario() -> None:
        running = asyncio.create_task(runner.run(loop.loop_id))
        await _created(host, "run-1")
        assert len(provider.prompts) == 1
        host.finish("run-1")
        await asyncio.wait_for(running, 10)

    asyncio.run(scenario())
    assert seen == [RunPhase.SUCCEEDED]
    assert "`run-1` WorkOrder run-1: succeeded" in provider.prompts[1]
    assert "Find and delete unused code." in provider.prompts[0]
    saved = runner.store.get(loop.loop_id)
    assert [one.run_id for one in saved.workorders] == ["run-1"]
    assert runner.json(saved, [host.runs["run-1"]])["workOrders"] == [
        {"runId": "run-1", "name": "WorkOrder run-1", "phase": "succeeded"}]


def test_a_loop_creates_no_more_workorders_than_it_may_in_a_day(tmp_path) -> None:
    host = Host()
    provider = ScriptedProvider([[
        ("create_workorder", {"prompt": "one"}), ("create_workorder", {"prompt": "two"}),
    ], [("create_workorder", {"prompt": "three"})]], on_turn=lambda _turn: [
        host.finish(run_id) for run_id in list(host.runs)])
    runner, loop = _runner(tmp_path, host, provider, maxWorkOrders=1)

    async def scenario() -> None:
        running = asyncio.create_task(runner.run(loop.loop_id))
        await _created(host, "run-1")
        host.finish("run-1")
        await asyncio.wait_for(running, 10)

    asyncio.run(scenario())
    assert host.created == ["run-1"]
    assert provider.results[0][1]["isError"] is True
    assert "exit criterion" in provider.results[0][1]["content"][0]["text"]
    # Capped for today: no second turn, and the next run is tomorrow morning.
    assert len(provider.prompts) == 1
    saved = runner.store.get(loop.loop_id)
    assert runner.json(saved, [])["nextRunAt"] == NOON.replace(day=2, hour=9).isoformat()


def test_defer_until_holds_the_next_run_until_that_workorder_is_complete(tmp_path) -> None:
    host = Host()
    host.runs["other"] = _state("other", RunPhase.RUNNING_AGENT)
    provider = ScriptedProvider([[("defer_until", {"run_id": "other"})]])
    runner, loop = _runner(tmp_path, host, provider, everyMinutes=1)
    runner.store.save(replace(loop, last_run_at=(NOON - timedelta(hours=1)).isoformat()))

    async def scenario() -> None:
        assert await runner.due(runner.store.get(loop.loop_id))
        await asyncio.gather(*await runner.tick())
        held = runner.store.get(loop.loop_id)
        assert held.deferred_until == "other"
        runner.host.now = lambda: NOON + timedelta(minutes=5)
        assert not await runner.due(held)
        host.finish("other")
        assert await runner.due(held)

    asyncio.run(scenario())
    assert provider.results[0][0]["content"][0]["text"].startswith(
        "The next run of this loop waits until `other`")


def test_a_loop_has_one_workorder_in_progress_at_a_time(tmp_path) -> None:
    host = Host()
    provider = ScriptedProvider([[
        ("create_workorder", {"prompt": "one"}), ("create_workorder", {"prompt": "two"}),
    ], []])
    runner, loop = _runner(tmp_path, host, provider, maxWorkOrders=5)

    async def scenario() -> None:
        running = asyncio.create_task(runner.run(loop.loop_id))
        await _created(host, "run-1")
        host.finish("run-1")
        await asyncio.wait_for(running, 10)

    asyncio.run(scenario())
    assert host.created == ["run-1"]
    assert "`run-1` is still in progress" in provider.results[0][1]["content"][0]["text"]
    assert len(provider.prompts) == 2


def test_a_workorder_created_before_deferring_is_still_waited_on(tmp_path) -> None:
    host = Host()
    host.runs["other"] = _state("other", RunPhase.RUNNING_AGENT)
    provider = ScriptedProvider([[
        ("create_workorder", {"prompt": "one"}), ("defer_until", {"run_id": "other"}),
    ]])
    runner, loop = _runner(tmp_path, host, provider)

    async def scenario() -> None:
        running = asyncio.create_task(runner.run(loop.loop_id))
        await _created(host, "run-1")
        await asyncio.sleep(0.1)
        assert not running.done()
        host.finish("run-1")
        await asyncio.wait_for(running, 10)

    asyncio.run(scenario())
    assert runner.store.get(loop.loop_id).deferred_until == "other"
    assert len(provider.prompts) == 1


def test_defer_until_refuses_a_workorder_in_another_repository(tmp_path) -> None:
    host = Host()
    host.runs["elsewhere"] = _state("elsewhere", RunPhase.RUNNING_AGENT, "/elsewhere")
    provider = ScriptedProvider([[("defer_until", {"run_id": "elsewhere"})]])
    runner, loop = _runner(tmp_path, host, provider)
    asyncio.run(runner.run(loop.loop_id))
    assert provider.results[0][0]["isError"] is True
    assert "no WorkOrder `elsewhere` in this loop's repository" in (
        provider.results[0][0]["content"][0]["text"])
    assert runner.store.get(loop.loop_id).deferred_until == ""


def test_a_loop_whose_creator_lost_access_does_not_run(tmp_path) -> None:
    host = Host()
    provider = ScriptedProvider([[("create_workorder", {"prompt": "one"})]])
    runner, loop = _runner(tmp_path, host, provider)
    host.allowed = False
    assert not asyncio.run(runner.due(loop))
    asyncio.run(runner.run(loop.loop_id))
    assert host.created == []
    assert "can no longer write" in provider.results[0][0]["content"][0]["text"]


def test_a_loop_without_active_hours_is_told_it_may_run_any_time(tmp_path) -> None:
    provider = ScriptedProvider([])
    runner, loop = _runner(tmp_path, Host(), provider,
                           activeHours={"start": "00:00", "end": "00:00"})
    asyncio.run(runner.run(loop.loop_id))
    assert "any time of day" in provider.prompts[0] and "00:00-00:00" not in provider.prompts[0]


def test_spend_today_counts_only_todays_workorders(tmp_path) -> None:
    host = Host()
    host.costs = {"today": 1.25, "yesterday": 9.0}
    runner, loop = _runner(tmp_path, host, ScriptedProvider([]), maxDailySpend=1)
    loop = replace(loop, workorders=(
        LoopWorkOrder("today", NOON.isoformat()),
        LoopWorkOrder("yesterday", (NOON - timedelta(days=1)).isoformat()),
    ))
    assert runner.spent_today(loop) == 1.25
    assert runner.capped(loop)


def test_the_new_loop_form_creates_a_loop_that_says_when_it_runs(
    tmp_path, *, client, sqlite_store, web_app
) -> None:
    settings = LoopSettingsStore(tmp_path / "settings.json")
    settings.set(LoopSettings(active_hours_start="08:00", active_hours_end="18:00",
                              max_prs=4, max_daily_spend=7.5))
    app = web_app(runners={"codex": object()}, state_store=sqlite_store(),
                   loop_settings=settings, loop_store=LoopStore(tmp_path / "loops.json"),
                   loop_provider=ScriptedProvider([]))
    with client(app) as browser:
        defaults = browser.get("/api/loops/defaults").json()
        created = browser.post("/api/loops", json=FORM)
        listed = browser.get("/api/loops").json()["loops"]
        fetched = browser.get(f"/api/loops/{created.json()['loopId']}").json()
        refused = browser.post("/api/loops", json={**FORM, "prompt": ""})
        deleted = browser.delete(f"/api/loops/{created.json()['loopId']}")
        after = browser.get("/api/loops").json()["loops"]

    assert defaults == {"everyMinutes": 60, "activeHours": {"start": "08:00", "end": "18:00"},
                        "maxWorkOrders": 4, "maxDailySpend": 7.5}
    assert created.status_code == 201
    assert [{**one, "nextRunAt": None} for one in listed] == [{**fetched, "nextRunAt": None}]
    assert fetched["prompt"] == FORM["prompt"]
    assert fetched["nextRunAt"] and fetched["spentToday"] == 0
    assert fetched["workOrders"] == [] and fetched["running"] is False
    assert refused.status_code == 400
    assert deleted.status_code == 204 and after == []
