from unittest.mock import AsyncMock, MagicMock, call, patch

import keyring
import pytest


from engine.adapters.communications.slack import (
    SlackAuthError,
    SlackCommunications,
    SlackCredentialStore,
    SlackCredentials,
    authorization_url,
)
from engine.ports import Message, MessageLink


def test_slack_communications_posts_to_requested_channel() -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    response = MagicMock(is_error=False)
    response.json.return_value = {"ok": True, "ts": "123.456"}
    first_page = MagicMock(is_error=False)
    first_page.json.return_value = {
        "ok": True,
        "channels": [{"id": "C999", "name": "general"}],
        "response_metadata": {"next_cursor": "next-page"},
    }
    second_page = MagicMock(is_error=False)
    second_page.json.return_value = {
        "ok": True,
        "channels": [{"id": "C123", "name": "openengine"}],
        "response_metadata": {"next_cursor": ""},
    }

    with patch("engine.adapters.communications.slack.httpx.AsyncClient") as client_type:
        client_type.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=response
        )
        client_type.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=[first_page, second_page]
        )
        message_id = __import__("asyncio").run(
            SlackCommunications(store).post("OpenEngine", "Review run-42")
        )

    assert message_id == "123.456"
    client_type.return_value.__aenter__.return_value.post.assert_awaited_once_with(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": "Bearer xoxb-token"},
        json={
            "channel": "C123",
            "text": "Review run-42",
        },
    )
    assert client_type.return_value.__aenter__.return_value.get.await_args_list == [
        call(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": "Bearer xoxb-token"},
            params={"types": "public_channel", "limit": 200},
        ),
        call(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": "Bearer xoxb-token"},
            params={
                "types": "public_channel",
                "limit": 200,
                "cursor": "next-page",
            },
        ),
    ]


def test_slack_communications_renders_structured_links_as_mrkdwn() -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    response = MagicMock(is_error=False)
    response.json.return_value = {"ok": True, "ts": "123.456"}

    with patch("engine.adapters.communications.slack.httpx.AsyncClient") as client_type:
        client = client_type.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=response)
        __import__("asyncio").run(
            SlackCommunications(store).post(
                "C12345678",
                Message(
                    "Review run-42",
                    (MessageLink("Open review", "https://example.com/review"),),
                ),
            )
        )

    assert client.post.await_args.kwargs["json"]["text"] == (
        "Review run-42\n<https://example.com/review|Open review>"
    )


def test_slack_communications_resolves_name_that_starts_like_an_id() -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    channels = MagicMock(is_error=False)
    channels.json.return_value = {
        "ok": True,
        "channels": [{"id": "C12345678", "name": "codex"}],
        "response_metadata": {"next_cursor": ""},
    }
    response = MagicMock(is_error=False)
    response.json.return_value = {"ok": True, "ts": "123.456"}

    with patch("engine.adapters.communications.slack.httpx.AsyncClient") as client_type:
        client = client_type.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=channels)
        client.post = AsyncMock(return_value=response)
        __import__("asyncio").run(
            SlackCommunications(store).post("Codex", "Review run-42")
        )

    client.get.assert_awaited_once()
    client.post.assert_awaited_once_with(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": "Bearer xoxb-token"},
        json={"channel": "C12345678", "text": "Review run-42"},
    )


def test_slack_communications_reports_slack_delivery_errors() -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    response = MagicMock(is_error=False)
    response.json.return_value = {"ok": False, "error": "channel_not_found"}

    with patch("engine.adapters.communications.slack.httpx.AsyncClient") as client_type:
        client_type.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=response
        )
        with pytest.raises(SlackAuthError, match="channel_not_found"):
            __import__("asyncio").run(
                SlackCommunications(store).post("C12345678", "Review run-42")
            )


def test_slack_communications_reports_a_disconnected_workspace() -> None:
    """The likeliest way for a message to reach nobody is not silent.

    Returning an empty id here told every caller above that the message went
    out -- including `update_status`, which answers the agent that asked.
    """
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = None

    with pytest.raises(SlackAuthError, match="not connected"):
        __import__("asyncio").run(
            SlackCommunications(store).post("C12345678", "Review run-42")
        )


def test_add_reaction_happy_path() -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    response = MagicMock(is_error=False)
    response.json.return_value = {"ok": True}

    with patch("engine.adapters.communications.slack.httpx.AsyncClient") as client_type:
        client = client_type.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=response)
        __import__("asyncio").run(
            SlackCommunications(store).add_reaction("C123", "1700.0001", "eyes")
        )

    client.post.assert_awaited_once_with(
        "https://slack.com/api/reactions.add",
        headers={"Authorization": "Bearer xoxb-token"},
        json={"channel": "C123", "timestamp": "1700.0001", "name": "eyes"},
    )


def test_add_reaction_tolerates_already_reacted() -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    response = MagicMock(is_error=False)
    response.json.return_value = {"ok": False, "error": "already_reacted"}

    with patch("engine.adapters.communications.slack.httpx.AsyncClient") as client_type:
        client_type.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=response
        )
        # Should not raise
        __import__("asyncio").run(
            SlackCommunications(store).add_reaction("C123", "1700.0001", "eyes")
        )


def test_add_reaction_reports_a_disconnected_workspace() -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = None

    with pytest.raises(SlackAuthError, match="not connected"):
        __import__("asyncio").run(
            SlackCommunications(store).add_reaction("C123", "1700.0001", "eyes")
        )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"ok": True, "user": {"name": "ada", "profile": {
                "real_name": "Ada Lovelace", "email": "ada@example.test",
            }}},
            ("Ada Lovelace", "ada@example.test"),
        ),
        # Without `users:read.email` the profile has no email to credit.
        ({"ok": True, "user": {"name": "ada", "profile": {"real_name": "Ada"}}}, None),
        ({"ok": False, "error": "missing_scope"}, None),
    ],
)
def test_user_identity_needs_a_profile_email(body, expected) -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    response = MagicMock(is_error=False)
    response.json.return_value = body

    with patch("engine.adapters.communications.slack.httpx.AsyncClient") as client_type:
        client = client_type.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=response)
        identity = __import__("asyncio").run(SlackCommunications(store).user_identity("U1"))

    assert identity == expected
    client.get.assert_awaited_once_with(
        "https://slack.com/api/users.info",
        headers={"Authorization": "Bearer xoxb-token"},
        params={"user": "U1"},
    )


def test_authorization_url_requests_notification_scope_and_state() -> None:
    url = authorization_url("123", "http://localhost/api/slack/callback", "nonce")
    assert url.startswith("https://slack.com/oauth/v2/authorize?")
    assert (
        "scope=app_mentions%3Aread%2Cchat%3Awrite%2Cchat%3Awrite.public"
        "%2Cchannels%3Aread%2Cchannels%3Ahistory%2Cgroups%3Ahistory"
        "%2Creactions%3Awrite" in url
    )
    assert "users%3Aread%2Cusers%3Aread.email" in url
    assert "state=nonce" in url


def test_credentials_are_restored_when_secret_write_fails() -> None:
    values = {"slack-client-id": "old-id", "slack-client-secret": "old-secret"}

    def set_password(_service: str, username: str, value: str) -> None:
        if username == "slack-client-secret" and value == "new-secret":
            raise keyring.errors.PasswordSetError("failed")
        values[username] = value

    with (
        patch("engine.adapters.communications.slack.keyring.get_keyring", return_value=MagicMock(priority=1)),
        patch("engine.adapters.communications.slack.keyring.get_password", side_effect=lambda _s, u: values.get(u)),
        patch("engine.adapters.communications.slack.keyring.set_password", side_effect=set_password),
        patch("engine.adapters.communications.slack.keyring.delete_password"),
    ):
        with pytest.raises(SlackAuthError):
            SlackCredentialStore().set_credentials("new-id", "new-secret")

    assert values == {"slack-client-id": "old-id", "slack-client-secret": "old-secret"}


def test_slack_oauth_endpoints_complete_connection(
    *, sqlite_store, web_app, client
) -> None:
    slack_store = MagicMock(spec=SlackCredentialStore)
    slack_store.credentials.return_value = SlackCredentials("client", "secret")
    slack_store.token.side_effect = [None, "xoxb-token"]
    slack_store.signing_secret.return_value = None
    app = web_app(
        state_store=sqlite_store(),
        runners={"default": object()},
        workflow_catalog=MagicMock(),
        slack_credential_store=slack_store,
    )

    with (
        patch("engine.apps.web.api.uuid4", return_value=MagicMock(hex="nonce")),
        patch("engine.apps.web.api.exchange_slack_code", new=AsyncMock(return_value="xoxb-token")),
        client(app) as browser,
    ):
        before = browser.get("/api/slack/status")
        connect = browser.post("/api/slack/connect")
        callback = browser.get("/api/slack/callback?code=code&state=nonce")
        after = browser.get("/api/slack/status")

    assert before.json() == {
        "configured": True,
        "connected": False,
        "events": False,
        "signingSecret": False,
    }
    assert "client_id=client" in connect.json()["authorizationUrl"]
    assert callback.status_code == 200
    slack_store.set_token.assert_called_once_with("xoxb-token")
    assert after.json() == {
        "configured": True,
        "connected": True,
        "events": False,
        "signingSecret": False,
    }


def test_slack_callback_rejects_wrong_state(*, sqlite_store, web_app, client) -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.credentials.return_value = SlackCredentials("client", "secret")
    app = web_app(
        state_store=sqlite_store(),
        runners={"default": object()},
        workflow_catalog=MagicMock(),
        slack_credential_store=store,
    )
    with client(app) as browser:
        browser.post("/api/slack/connect")
        response = browser.get("/api/slack/callback?code=code&state=wrong")
    assert response.status_code == 400
    store.set_token.assert_not_called()


def test_slack_disconnect_revokes_before_forgetting_token(
    *, sqlite_store, web_app, client
) -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    app = web_app(
        state_store=sqlite_store(),
        runners={"default": object()},
        workflow_catalog=MagicMock(),
        slack_credential_store=store,
    )
    revoke = AsyncMock()

    with patch("engine.apps.web.api.revoke_slack_token", new=revoke), client(app) as browser:
        response = browser.post("/api/slack/disconnect")

    assert response.status_code == 204
    revoke.assert_awaited_once_with("xoxb-token")
    store.disconnect.assert_called_once_with()


def test_slack_disconnect_preserves_token_when_revocation_fails(
    *, sqlite_store, web_app, client
) -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    app = web_app(
        state_store=sqlite_store(),
        runners={"default": object()},
        workflow_catalog=MagicMock(),
        slack_credential_store=store,
    )
    revoke = AsyncMock(side_effect=SlackAuthError("Slack unavailable"))

    with patch("engine.apps.web.api.revoke_slack_token", new=revoke), client(app) as browser:
        response = browser.post("/api/slack/disconnect")

    assert response.status_code == 502
    store.disconnect.assert_not_called()


def test_changing_credentials_revokes_existing_token_first(
    *, sqlite_store, web_app, client
) -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-old-token"
    app = web_app(
        state_store=sqlite_store(),
        runners={"default": object()},
        workflow_catalog=MagicMock(),
        slack_credential_store=store,
    )
    events: list[str] = []
    revoke = AsyncMock(side_effect=lambda _token: events.append("revoke"))
    store.disconnect.side_effect = lambda: events.append("disconnect")
    store.set_credentials.side_effect = lambda *_args: events.append("save")

    with patch("engine.apps.web.api.revoke_slack_token", new=revoke), client(app) as browser:
        response = browser.post(
            "/api/slack/credentials",
            json={"clientId": "new-client", "clientSecret": "new-secret"},
        )

    assert response.status_code == 204
    assert events == ["revoke", "disconnect", "save"]
    revoke.assert_awaited_once_with("xoxb-old-token")


def test_changing_credentials_keeps_existing_state_when_revocation_fails(
    *, sqlite_store, web_app, client
) -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-old-token"
    app = web_app(
        state_store=sqlite_store(),
        runners={"default": object()},
        workflow_catalog=MagicMock(),
        slack_credential_store=store,
    )
    revoke = AsyncMock(side_effect=SlackAuthError("Slack unavailable"))

    with patch("engine.apps.web.api.revoke_slack_token", new=revoke), client(app) as browser:
        response = browser.post(
            "/api/slack/credentials",
            json={"clientId": "new-client", "clientSecret": "new-secret"},
        )

    assert response.status_code == 502
    store.disconnect.assert_not_called()
    store.set_credentials.assert_not_called()


@pytest.mark.parametrize("operation", ["disconnect", "credentials"])
def test_successful_slack_mutation_invalidates_pending_oauth_flow(
    operation: str, *, sqlite_store, web_app, client
) -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.credentials.return_value = SlackCredentials("client", "secret")
    store.token.return_value = None
    app = web_app(
        state_store=sqlite_store(),
        runners={"default": object()},
        workflow_catalog=MagicMock(),
        slack_credential_store=store,
    )

    with patch("engine.apps.web.api.uuid4", return_value=MagicMock(hex="pending")), client(app) as browser:
        assert browser.post("/api/slack/connect").status_code == 200
        if operation == "disconnect":
            response = browser.post("/api/slack/disconnect")
        else:
            response = browser.post(
                "/api/slack/credentials",
                json={"clientId": "new-client", "clientSecret": "new-secret"},
            )
        callback = browser.get("/api/slack/callback?code=code&state=pending")

    assert response.status_code == 204
    assert callback.status_code == 400
    store.set_token.assert_not_called()


@pytest.mark.parametrize(
    ("server_timezone", "month", "local_hour", "zone"),
    [("America/Denver", 1, "05", "MST"), ("America/Denver", 9, "06", "MDT"),
     ("UTC", 9, "12", "UTC")],
)
def test_progress_edits_timestamped_history_without_overwriting_agent_messages(
    monkeypatch, server_timezone, month, local_hour, zone,
):
    import asyncio
    import time
    from datetime import datetime, timezone

    if not hasattr(time, "tzset"):
        pytest.skip("requires time.tzset to configure the server timezone")

    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    response = MagicMock(is_error=False)
    response.json.return_value = {"ok": True, "ts": "123.456"}
    links = (MessageLink("View work order", "https://example.com/runs/run-42"),)
    states = ["CI check", "Review (Security)", "Review (Bugs & task adherence)"]

    async def scenario():
        slack = SlackCommunications(store)
        for index, state in enumerate(states):
            clock.now.return_value = datetime(2026, month, 23, 12, index, tzinfo=timezone.utc)
            await slack.post("C12345678", Message(f"*{state}* started.", links, progress=True),
                             "run-42", thread_id="1")
            if index == 0:
                await slack.post("C12345678", Message("Implemented the requested fix."),
                                 "run-42", thread_id="1")
        await slack.post("C12345678", Message("Review complete and ready for your decision.",
                                              links, mention="U123"), "run-42", thread_id="1")
        await slack.post("C12345678", Message("Work order finished.", links, progress=True),
                         "run-42", thread_id="1")
        await slack.post("C12345678", Message("Another run started.", progress=True),
                         "run-43", thread_id="1")

    with patch("engine.adapters.communications.slack.httpx.AsyncClient") as client_type, \
         patch("engine.adapters.communications.slack.datetime") as clock:
        client = client_type.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=response)
        try:
            with monkeypatch.context() as timezone_env:
                timezone_env.setenv("TZ", server_timezone)
                time.tzset()
                asyncio.run(scenario())
        finally:
            time.tzset()

    calls = client.post.await_args_list
    assert [call.args[0].rsplit("/", 1)[-1] for call in calls] == [
        "chat.postMessage", "chat.postMessage", "chat.update", "chat.update",
        "chat.postMessage", "chat.update", "chat.postMessage",
    ]
    history = "\n".join(f"*{state}* started. ({local_hour}:0{index}:00 {zone})"
                        for index, state in enumerate(states))
    assert calls[3].kwargs["json"] == {
        "channel": "C12345678", "ts": "123.456",
        "text": history + "\n<https://example.com/runs/run-42|View work order>",
    }
    assert calls[4].kwargs["json"]["text"].startswith(
        "<@U123> Review complete and ready for your decision.")
    assert calls[5].kwargs["json"]["text"].startswith(history + "\nWork order finished.")
    assert calls[6].kwargs["json"]["thread_ts"] == "1"


def test_progress_cache_evicts_least_recently_updated_history():
    import asyncio

    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    response = MagicMock(is_error=False)
    response.json.return_value = {"ok": True, "ts": "123.456"}

    async def scenario():
        slack = SlackCommunications(store)

        async def progress(run_id, text):
            await slack.post("C12345678", Message(text, progress=True),
                             run_id, thread_id="1")

        await progress("run-1", "First started")
        await progress("run-2", "Second started")
        await progress("run-1", "First continued")
        await progress("run-3", "Third started")
        assert len(slack._progress) == 2
        assert ("C12345678", "1", "run-2") not in slack._progress
        await progress("run-1", "First finished")
        retained = client.post.await_args.kwargs["json"]
        assert "ts" in retained
        assert all(state in retained["text"] for state in (
            "First started", "First continued", "First finished"))

        await progress("run-2", "Second resumed")
        restarted = client.post.await_args.kwargs["json"]
        assert client.post.await_args.args[0].endswith("chat.postMessage")
        assert restarted["thread_ts"] == "1"
        assert "Second started" not in restarted["text"]
        assert "Second resumed" in restarted["text"]
        assert len(slack._progress) == 2

    with patch("engine.adapters.communications.slack.httpx.AsyncClient") as client_type, \
         patch("engine.adapters.communications.slack._MAX_PROGRESS_MESSAGES", 2):
        client = client_type.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=response)
        asyncio.run(scenario())
