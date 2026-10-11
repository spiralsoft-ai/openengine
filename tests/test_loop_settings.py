"""The settings the rail's Loops section reads and saves."""

import json

import pytest

from engine.apps.web.loops import LoopSettings, LoopSettingsStore, parse_loop_settings

_SETTINGS = {
    "activeHours": {"start": "09:00", "end": "17:30"},
    "maxPrs": 2,
    "maxDailySpend": 12.5,
    "runnerStrategy": "manual",
    "implementationRunner": "codex",
    "reviewRunner": "claude",
}


def test_saved_settings_are_read_back(
    tmp_path, *, client, sqlite_store, web_app
) -> None:
    store = LoopSettingsStore(tmp_path / "loops.json")
    with client(web_app(runners={"codex": object(), "claude": object()},
                        state_store=sqlite_store(), loop_settings=store)) as browser:
        before = browser.get("/api/loops/settings").json()
        saved = browser.put("/api/loops/settings", json=_SETTINGS)
        after = browser.get("/api/loops/settings").json()

    assert before == LoopSettings().json()
    assert saved.status_code == 200
    assert saved.json() == after == _SETTINGS
    assert LoopSettingsStore(tmp_path / "loops.json").get().max_prs == 2


def test_rejected_settings_leave_the_saved_ones(
    tmp_path, *, client, sqlite_store, web_app
) -> None:
    store = LoopSettingsStore(tmp_path / "loops.json")
    with client(web_app(runners={"codex": object(), "claude": object()},
                        state_store=sqlite_store(), loop_settings=store)) as browser:
        rejected = browser.put(
            "/api/loops/settings", json={**_SETTINGS, "reviewRunner": "unknown"}
        )
        after = browser.get("/api/loops/settings").json()

    assert rejected.status_code == 400
    assert after == LoopSettings().json()


def test_a_spend_limit_that_is_not_a_number_is_refused(
    tmp_path, *, client, sqlite_store, web_app
) -> None:
    store = LoopSettingsStore(tmp_path / "loops.json")
    body = json.dumps({**_SETTINGS, "maxDailySpend": 0}).replace(
        '"maxDailySpend": 0', '"maxDailySpend": NaN'
    )
    with client(web_app(runners={"codex": object(), "claude": object()},
                        state_store=sqlite_store(), loop_settings=store)) as browser:
        rejected = browser.put(
            "/api/loops/settings", content=body, headers={"content-type": "application/json"}
        )
        after = browser.get("/api/loops/settings")

    assert rejected.status_code == 400
    assert after.status_code == 200


def test_only_manual_keeps_its_runners() -> None:
    settings = parse_loop_settings({**_SETTINGS, "runnerStrategy": "round-robin"}, ["codex"])

    assert settings.implementation_runner == settings.review_runner == ""


@pytest.mark.parametrize("change", [
    {"activeHours": {"start": "9am", "end": "17:00"}},
    {"maxPrs": 0},
    {"maxPrs": True},
    {"maxDailySpend": -1},
    {"maxDailySpend": float("nan")},
    {"maxDailySpend": float("inf")},
    {"runnerStrategy": "random"},
])
def test_invalid_settings_are_refused(change) -> None:
    with pytest.raises(ValueError):
        parse_loop_settings({**_SETTINGS, **change}, ["codex", "claude"])
