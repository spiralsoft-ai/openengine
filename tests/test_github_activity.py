"""What the web UI is told about GitHub comment activity.

Two things have to hold: each comment's row follows the one path through this
process -- queued, picked up, forwarded, answered -- and a row reaches the work
order page for the pull request it was left on, whether or not that comment was
the one that steered anything.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.apps.web.github_activity import (
    ACTIVITY_LIMIT,
    EXCERPT_LIMIT,
    GithubActivityLog,
    activity_json,
)
from engine.apps.web.github_ingress import GithubComment
from engine.runtime import WorkOrdersConfig

from test_slack_work_orders import SIGNING_SECRET, _workflow_catalog
from provider_fakes import FakeACPProvider
from web_fakes import RecordingCommunications


def _comment(
    comment_id: str = "1", body: str = "please fix it", *, is_pull_request: bool = True
) -> GithubComment:
    return GithubComment(
        comment_id=comment_id, repository="acme/api", number=7, author="someone",
        body=body, url=f"https://github.com/acme/api/pull/7#issuecomment-{comment_id}",
        event="issue_comment", is_pull_request=is_pull_request,
    )


def _ticking() -> GithubActivityLog:
    clock = iter(range(1, 100))
    return GithubActivityLog(now=lambda: float(next(clock)))


# --- the record ------------------------------------------------------------


def test_a_comment_that_started_the_work_it_asks_about_says_so() -> None:
    """Starting and steering are different answers, and the row keeps which.

    A comment on a pull request nothing is working on gets a work order of its
    own; one on a run in flight is steered into it. Both reach a work order,
    so both are `dispatched`, and only the flag tells them apart.
    """
    log = _ticking()
    comment = _comment()
    log.seen(comment)
    log.started(comment)
    log.dispatched("fresh", started_run=True)

    (entry,) = log.recent()
    assert (entry.run_id, entry.started_run) == ("fresh", True)
    (row,) = activity_json(log.recent(), run_id="fresh")["comments"]
    assert row["startedRun"] is True


def test_a_comment_keeps_one_row_through_the_whole_turn() -> None:
    log = _ticking()
    comment = _comment()
    log.seen(comment)
    log.started(comment)
    log.dispatched("existing")
    log.replied("Forwarded to work order `existing`.")
    log.finished(comment)

    (entry,) = log.recent()
    assert entry.status == "replied"
    assert entry.run_id == "existing"
    # Steered, not started: this comment did not create the work order.
    assert entry.started_run is False
    assert entry.reply == "Forwarded to work order `existing`."
    assert entry.url == comment.url
    # Every step is timed, which is what makes "it sat in the queue" and "the
    # forge was slow" different readings rather than one shrug.
    assert entry.seen_at < entry.started_at < entry.dispatched_at < entry.replied_at


def test_a_comment_engine_will_not_act_on_says_why() -> None:
    log = _ticking()
    comment = _comment(is_pull_request=False)
    log.seen(comment)
    log.started(comment)
    log.ignored("not a pull request")
    log.finished(comment)

    (entry,) = log.recent()
    assert (entry.status, entry.detail) == ("ignored", "not a pull request")


def test_a_comment_that_reached_no_work_order_says_so_beside_its_reply() -> None:
    """A dispatch that failed is not a turn that failed.

    The broker answers the agent rather than raising, so the concierge goes on
    to post its undelivered notice. The row has to carry both: the reply the
    pull request actually got, and the reason nothing was forwarded -- a row
    reading "replied" would be this panel contradicting the pull request.
    """
    log = _ticking()
    comment = _comment()
    log.seen(comment)
    log.started(comment)
    log.dispatch_failed("no workflow is configured under `work_orders.workflow`")
    log.replied("Could not deliver the feedback to a work order.")
    log.finished(comment)

    (entry,) = log.recent()
    assert entry.status == "failed"
    assert entry.detail == "no workflow is configured under `work_orders.workflow`"
    assert entry.reply == "Could not deliver the feedback to a work order."
    assert entry.run_id == ""


def test_a_failed_turn_is_recorded_as_one() -> None:
    log = _ticking()
    comment = _comment()
    log.seen(comment)
    log.started(comment)
    log.failed("permission API unavailable")

    (entry,) = log.recent()
    assert (entry.status, entry.detail) == ("failed", "permission API unavailable")


def test_updates_belong_to_the_comment_being_worked_on() -> None:
    """Nothing lands on a row whose turn is over.

    The callbacks that report forwarding and replying are handed a
    conversation rather than a delivery, so the in-flight comment is what says
    whose row they are writing to. A settled comment is not it.
    """
    log = _ticking()
    first, second = _comment("1"), _comment("2")
    log.seen(first)
    log.started(first)
    log.ignored("not a pull request")
    log.seen(second)
    log.started(second)
    log.dispatched("existing")
    log.replied("Forwarded to work order `existing`.")

    newest, oldest = log.recent()
    assert (newest.comment_id, newest.status) == ("2", "replied")
    assert (oldest.comment_id, oldest.status, oldest.run_id) == ("1", "ignored", "")


def test_a_handler_that_says_nothing_still_settles_its_row() -> None:
    log = _ticking()
    comment = _comment()
    log.seen(comment)
    log.started(comment)
    log.finished(comment)
    log.replied("this belongs to nobody")

    (entry,) = log.recent()
    assert (entry.status, entry.reply) == ("handled", "")


def test_a_redelivered_comment_keeps_the_work_order_it_already_reached() -> None:
    """A retried delivery is only a reply, and the row has to say so.

    The concierge answers a comment whose feedback already landed from what it
    recorded rather than forwarding again, so nothing calls `dispatched` the
    second time round.
    """
    log = _ticking()
    comment = _comment()
    log.seen(comment)
    log.started(comment)
    log.dispatched("existing", started_run=True)
    log.failed("the reply could not be posted")

    log.seen(comment)
    log.started(comment)
    log.replied("Forwarded to work order `existing`.")
    log.finished(comment)

    (entry,) = log.recent()
    assert (entry.status, entry.run_id) == ("replied", "existing")
    # Including that the work order was started for it: forwarding happened
    # once, and the row that survives is the one that says what happened.
    assert entry.started_run is True


def test_a_comment_is_kept_as_an_excerpt_rather_than_republished() -> None:
    log = _ticking()
    log.seen(_comment(body="please  fix\n\nthe thing " + "x" * 500))
    (entry,) = log.recent()
    assert len(entry.excerpt) == EXCERPT_LIMIT
    assert entry.excerpt.startswith("please fix the thing ")
    assert entry.excerpt.endswith("…")


def test_only_the_recent_past_is_remembered() -> None:
    log = _ticking()
    for comment_id in range(ACTIVITY_LIMIT + 5):
        log.seen(_comment(str(comment_id)))
    remembered = log.recent()
    assert len(remembered) == ACTIVITY_LIMIT
    # Newest first, which is the order the panel reads them in.
    assert remembered[0].comment_id == str(ACTIVITY_LIMIT + 4)


# --- the wire shape --------------------------------------------------------


def test_a_comment_is_attributed_to_whichever_run_opened_its_pull_request() -> None:
    """Ownership is joined at read time, so a row is never orphaned by timing.

    A pull request's owner is written down when it is opened, which can be
    after somebody commented on it -- and a comment Engine ignored never
    forwards anything to name a run by.
    """
    log = _ticking()
    comment = _comment()
    log.seen(comment)
    log.started(comment)
    log.ignored("someone cannot write to acme/api")

    body = activity_json(
        log.recent(), run_id="existing", pull_request=("acme/api", 7),
    )
    (row,) = body["comments"]
    assert row["status"] == "ignored"
    # A row never names the work order whose page it is on, nor links to it.
    assert "runId" not in row and "dispatchedRunId" not in row
    assert "runUrl" not in row
    # And a work order that opened no pull request has nothing to attribute
    # the comment to, so it belongs to no page rather than to all of them.
    assert activity_json(log.recent(), run_id="existing")["comments"] == []


def test_a_comment_is_never_shown_beside_work_it_has_nothing_to_do_with() -> None:
    log = _ticking()
    comment = _comment()
    log.seen(comment)
    log.started(comment)
    log.dispatched("existing")

    assert activity_json(
        log.recent(), run_id="other", pull_request=("acme/api", 7),
    )["comments"] == []


def test_the_panel_is_told_a_webhook_will_never_deliver_anything() -> None:
    empty = activity_json((), run_id="existing", repository="", configured=False)
    assert (empty["configured"], empty["comments"]) == (False, [])


# --- what the route answers ------------------------------------------------


def test_the_route_reports_a_comment_all_the_way_to_its_reply(
    *, slack_app, client
) -> None:
    from test_github_concierge import _graph_runtime
    from test_github_ingress import _issue_comment, _signed as github_signed

    _runtime, opened = _graph_runtime()
    app, capabilities, _ = slack_app(
        RecordingCommunications(),
        WorkOrdersConfig(
            repository="acme/api", workflow="implementation-review-v1", runner="default"
        ),
        _workflow_catalog(),
        provider=FakeACPProvider(create=True),
        github_webhook_secret=SIGNING_SECRET,
        graph_runtime=opened,
    )
    source_control = MagicMock()
    source_control.add_comment = AsyncMock()
    source_control.can_write_repository = AsyncMock(return_value=True)
    source_control.authenticated_login = AsyncMock(return_value="OpenEngineBot")
    object.__setattr__(capabilities, "source_control", source_control)

    payload = _issue_comment(1, "new workorder please")
    payload["issue"]["pull_request"] = {}
    body = json.dumps(payload).encode()
    with client(app) as browser:
        assert browser.post("/api/github/events", content=body, headers=dict(
            github_signed(body), **{"x-github-event": "issue_comment"})).status_code == 200
        browser.portal.call(app.state.github_ingress.drain)

        feed = browser.get("/api/runs/existing/github-comments").json()
        assert feed["configured"] and feed["repository"] == "acme/api"
        # Nothing process-wide is reported: a queue depth read off the one
        # ingress describes whichever comment is in flight, rarely this one.
        assert set(feed) == {"repository", "configured", "comments"}
        (row,) = feed["comments"]
        assert row["status"] == "replied"
        assert (row["author"], row["number"]) == ("someone", 7)
        assert row["startedRun"] is False
        assert row["reply"].startswith("Forwarded to work order `existing`.")
        assert row["url"] == "https://github.com/acme/api/issues/7#c"
        assert row["excerpt"] == "new workorder please"

        # And nowhere else: another WorkOrder's page does not carry it, and
        # there is no route that hands out every comment at once.
        assert browser.get("/api/runs/other/github-comments").json()["comments"] == []
        assert browser.get("/api/github/activity").status_code == 404


def test_the_route_shows_an_ignored_comment_on_the_page_that_opened_the_pr(
    *, slack_app, client
) -> None:
    """A comment that forwarded nothing still reaches the right reader.

    Nothing was dispatched, so the row names no work order of its own. It gets
    to this page because the page's work order opened the pull request it was
    left on -- asked of the store once, from the run.
    """
    from test_github_concierge import _graph_runtime
    from test_github_ingress import _issue_comment, _signed as github_signed

    _runtime, opened = _graph_runtime()
    app, capabilities, _ = slack_app(
        RecordingCommunications(),
        WorkOrdersConfig(
            repository="acme/api", workflow="implementation-review-v1", runner="default"
        ),
        _workflow_catalog(),
        provider=FakeACPProvider(create=True),
        github_webhook_secret=SIGNING_SECRET,
        graph_runtime=opened,
    )
    source_control = MagicMock()
    source_control.add_comment = AsyncMock()
    # The commenter cannot write to the repository, so Engine does not act.
    source_control.can_write_repository = AsyncMock(return_value=False)
    source_control.authenticated_login = AsyncMock(return_value="OpenEngineBot")
    object.__setattr__(capabilities, "source_control", source_control)

    payload = _issue_comment(1, "please fix the tests")
    payload["issue"]["pull_request"] = {}
    body = json.dumps(payload).encode()
    with client(app) as browser:
        assert browser.post("/api/github/events", content=body, headers=dict(
            github_signed(body), **{"x-github-event": "issue_comment"})).status_code == 200
        browser.portal.call(app.state.github_ingress.drain)

        (row,) = browser.get("/api/runs/existing/github-comments").json()["comments"]
        assert row["status"] == "ignored"
        assert row["detail"] == "someone cannot write to acme/api"
        assert row["excerpt"] == "please fix the tests"
        # And only there: another work order opened a different pull request,
        # or none, so this comment is none of its business.
        assert browser.get("/api/runs/other/github-comments").json()["comments"] == []


def test_the_route_answers_a_deployment_with_no_webhook(*, slack_app, client) -> None:
    """A repository with no webhook secret is a panel that will stay empty."""

    app, _capabilities, _ = slack_app(RecordingCommunications(), WorkOrdersConfig())
    with client(app) as browser:
        feed = browser.get("/api/runs/existing/github-comments").json()
    assert feed == {"repository": "acme/api", "configured": False, "comments": []}


@pytest.mark.parametrize("legacy_repository", ["", "acme/api"])
@pytest.mark.parametrize("repository_kind", ["project", "name", "path"])
@pytest.mark.parametrize("webhook_repositories", [("acme/api", "other/web"), ("acme/api",)])
def test_comment_panel_uses_runs_repository_before_a_pr_is_opened(
    tmp_path,
    legacy_repository,
    repository_kind,
    webhook_repositories,
    *,
    client,
    web_app,
):
    from test_web_app import _session_with

    from engine.domain import RunId, RunState, TaskId, WorkflowId

    checkout = str(tmp_path / "web")
    repository = {"project": "other/web", "name": "web", "path": checkout}[repository_kind]
    runners = {"default": MagicMock()}
    session = _session_with(runners)
    app = web_app(
        session, runners,
        github_repository=legacy_repository,
        github_repositories=webhook_repositories,
        github_webhook_secret=lambda: SIGNING_SECRET,
        repos={"web": checkout},
        repository_projects={"web": "other/web"},
    )
    with client(app) as browser:
        browser.portal.call(session.state_store.save, RunState(
            run_id=RunId("existing"), task_id=TaskId("task"),
            workflow_id=WorkflowId("workflow"), repository=repository,
        ))
        response = browser.get("/api/runs/existing/github-comments")
    assert response.status_code == 200
    assert response.json() == {
        "repository": "other/web", "configured": "other/web" in webhook_repositories,
        "comments": [],
    }


def test_same_comment_id_in_two_repositories_has_independent_activity():
    from dataclasses import replace

    log = _ticking()
    first = _comment()
    second = replace(first, repository="other/web")
    log.seen(first)
    log.started(first)
    log.dispatched("first-run")
    log.finished(first)
    log.seen(second)
    log.started(second)
    log.failed("second failed")
    assert log.entry(first).run_id == "first-run"
    assert log.entry(second).run_id == ""
    assert log.entry(second).status == "failed"
    assert log.entry(replace(first, repository="ACME/API")) == log.entry(first)


def test_comment_panel_reports_the_runs_repository_in_a_multi_repo_deployment(
    *, slack_app, client
):
    from test_github_concierge import _graph_runtime
    from test_github_ingress import _issue_comment, _signed as github_signed

    _, opened = _graph_runtime(repository="other/web")
    app, capabilities, _ = slack_app(
        RecordingCommunications(),
        WorkOrdersConfig(),
        graph_runtime=opened,
        github_repositories=("acme/api", "other/web"),
        github_webhook_secret=SIGNING_SECRET,
        github_comment_handler=AsyncMock(),
    )
    source = MagicMock(can_write_repository=AsyncMock(return_value=True))
    object.__setattr__(capabilities, "source_control", source)
    payload = _issue_comment()
    payload["repository"]["full_name"] = "other/web"
    body = json.dumps(payload).encode()
    with client(app) as browser:
        assert browser.post("/api/github/events", content=body, headers=dict(
            github_signed(body), **{"x-github-event": "issue_comment"},
        )).status_code == 200
        browser.portal.call(app.state.github_ingress.drain)
        feed = browser.get("/api/runs/existing/github-comments").json()
    assert feed["repository"] == "other/web"
    assert feed["configured"]
    assert len(feed["comments"]) == 1
