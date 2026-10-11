"""Tests for the GitHub OAuth device flow and credential store."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import keyring
import keyring.backend
import keyring.backends.fail
import pytest

from engine.apps.web.github_auth import (
    DeviceFlowComplete,
    DeviceFlowPending,
    DeviceFlowState,
    GitHubAuthError,
    GitHubCredentialStore,
    GitHubRefreshTokenInvalidError,
    StoredCredentials,
    credentials_from_device_flow,
    poll_device_flow,
    refresh_access_token,
    start_device_flow,
)
from engine.apps.web.oauth_lifecycle import oauth_lifecycle_event, token_fingerprint

# ---------------------------------------------------------------------------
# GitHubCredentialStore
# ---------------------------------------------------------------------------


class TestGitHubCredentialStore:
    def test_get_returns_none_when_nothing_stored(self, monkeypatch):
        monkeypatch.setattr(keyring, "get_password", lambda *_: None)
        assert GitHubCredentialStore().get() is None

    def test_get_returns_stored_token(self, monkeypatch):
        monkeypatch.setattr(keyring, "get_password", lambda *_: "tok-abc")
        assert GitHubCredentialStore().get() == "tok-abc"

    def test_set_writes_to_keyring(self, monkeypatch):
        written: list[tuple] = []
        real_backend = MagicMock()
        real_backend.priority = 5
        monkeypatch.setattr(keyring, "get_keyring", lambda: real_backend)
        monkeypatch.setattr(
            keyring,
            "set_password",
            lambda service, username, password: written.append(
                (service, username, password)
            ),
        )
        GitHubCredentialStore().set("tok-xyz")
        assert written == [
            (
                "openengine",
                "github-token",
                (
                    '{"access_token":"tok-xyz","refresh_token":null,'
                    '"expires_at":null,"refresh_token_expires_at":null}'
                ),
            )
        ]

    def test_get_credentials_migrates_legacy_bare_token(self, monkeypatch):
        monkeypatch.setattr(keyring, "get_password", lambda *_: "tok-abc")
        assert GitHubCredentialStore().get_credentials() == StoredCredentials(
            access_token="tok-abc"
        )

    def test_credentials_round_trip_as_one_keychain_value(self, monkeypatch):
        saved: dict[str, str] = {}
        backend = _high_priority_backend()
        monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
        monkeypatch.setattr(
            keyring,
            "set_password",
            lambda _s, _u, value: saved.setdefault("value", value),
        )
        monkeypatch.setattr(keyring, "get_password", lambda *_: saved.get("value"))
        credentials = StoredCredentials("access", "refresh", 10.0, 20.0)
        store = GitHubCredentialStore()
        store.set_credentials(credentials)
        assert store.get_credentials() == credentials

    def test_set_raises_when_no_secure_backend(self, monkeypatch):
        monkeypatch.setattr(
            keyring, "get_keyring", lambda: keyring.backends.fail.Keyring()
        )
        with pytest.raises(GitHubAuthError, match="no secure keyring"):
            GitHubCredentialStore().set("tok-xyz")

    def test_set_raises_for_any_low_priority_backend(self, monkeypatch):
        """Priority < 1 means stub/null backend, not just the fail class."""
        stub = MagicMock()
        stub.priority = 0
        monkeypatch.setattr(keyring, "get_keyring", lambda: stub)
        with pytest.raises(GitHubAuthError, match="no secure keyring"):
            GitHubCredentialStore().set("tok")

    def test_delete_swallows_missing_password_error(self, monkeypatch):
        def raise_missing(*_):
            raise keyring.errors.PasswordDeleteError("not found")

        monkeypatch.setattr(keyring, "delete_password", raise_missing)
        GitHubCredentialStore().delete()  # must not raise


# ---------------------------------------------------------------------------
# start_device_flow
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status_code, json=body, request=httpx.Request("POST", "https://x")
    )


class TestStartDeviceFlow:
    def test_returns_state_on_success(self, monkeypatch):
        body = {
            "device_code": "dev-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, body)),
        )
        state = asyncio.run(start_device_flow("client-id"))
        assert state.device_code == "dev-1"
        assert state.user_code == "ABCD-EFGH"
        assert state.expires_in == 900
        assert state.interval == 5

    def test_raises_on_http_error(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda **_: _client_returning(
                _mock_response(401, {"message": "Bad credentials"})
            ),
        )
        with pytest.raises(GitHubAuthError, match="401"):
            asyncio.run(start_device_flow("client-id"))

    def test_raises_on_error_field(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {"error": "not_found"})),
        )
        with pytest.raises(GitHubAuthError, match="not_found"):
            asyncio.run(start_device_flow("client-id"))

    def test_requests_offline_access_scope(self, monkeypatch):
        seen: dict[str, object] = {}

        class Client(_AsyncContextManager):
            async def post(self, url, **kwargs):
                seen["url"] = url
                seen.update(kwargs)
                return self._response

        response = _mock_response(
            200,
            {
                "device_code": "dev-1",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
            },
        )
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient", lambda: Client(response)
        )
        asyncio.run(start_device_flow("client-id"))
        assert seen["data"] == {
            "client_id": "client-id",
            "scope": "repo offline_access",
        }


# ---------------------------------------------------------------------------
# poll_device_flow
# ---------------------------------------------------------------------------


class TestPollDeviceFlow:
    def test_returns_complete_with_token(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda **_: _client_returning(
                _mock_response(200, {"access_token": "ghs_secret"})
            ),
        )
        result = asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))
        assert isinstance(result, DeviceFlowComplete)
        assert result.access_token == "ghs_secret"

    def test_captures_expiring_token_fields(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(
                _mock_response(
                    200,
                    {
                        "access_token": "access",
                        "refresh_token": "refresh",
                        "expires_in": 28800,
                        "refresh_token_expires_in": 15897600,
                    },
                )
            ),
        )
        result = asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))
        assert result == DeviceFlowComplete("access", "refresh", 28800, 15897600)

    def test_ignores_boolean_expiry_fields(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(
                _mock_response(200, {"access_token": "access", "expires_in": False})
            ),
        )
        result = asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))
        assert result == DeviceFlowComplete("access")


class TestRefreshAccessToken:
    def test_returns_rotated_token_pair(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda **_: _client_returning(
                _mock_response(
                    200,
                    {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 60,
                        "refresh_token_expires_in": 120,
                    },
                )
            ),
        )
        with patch("engine.apps.web.github_auth.time.time", return_value=100.0):
            result = asyncio.run(refresh_access_token("cid", "old-refresh"))
        assert result == StoredCredentials("new-access", "new-refresh", 160.0, 220.0)

    def test_preserves_refresh_token_when_provider_does_not_rotate_it(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda **_: _client_returning(
                _mock_response(200, {"access_token": "access"})
            ),
        )
        result = asyncio.run(refresh_access_token("cid", "old-refresh"))
        assert result == StoredCredentials("access", "old-refresh")

    def test_raises_a_typed_error_for_invalid_refresh_token(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda **_: _client_returning(
                _mock_response(200, {"error": "bad_refresh_token"})
            ),
        )
        with pytest.raises(GitHubRefreshTokenInvalidError):
            asyncio.run(refresh_access_token("cid", "old-refresh"))

    def test_uses_the_device_flow_refresh_grant(self, monkeypatch):
        seen: dict[str, object] = {}

        class Client(_AsyncContextManager):
            async def post(self, url, **kwargs):
                seen["url"] = url
                seen.update(kwargs)
                return self._response

        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda **_: Client(
                _mock_response(
                    200,
                    {"access_token": "access", "refresh_token": "refresh"},
                )
            ),
        )
        asyncio.run(refresh_access_token("cid", "old-refresh"))
        assert seen["data"] == {
            "client_id": "cid",
            "grant_type": "refresh_token",
            "refresh_token": "old-refresh",
        }


@pytest.mark.parametrize("provider", [None, "gh-cli"])
def test_agent_pr_uses_only_the_gh_cli_login(
    tmp_path, monkeypatch, caplog, provider, *, github_auth_app, client
):
    """Under GH CLI, agents act as `gh auth`, never as a UI connection or GITHUB_TOKEN."""
    from engine.apps.web.composition import Settings, build_capabilities
    from engine.apps.web.source_control import SourceControlPreferences

    saved = {}
    monkeypatch.setattr(keyring, "get_keyring", _high_priority_backend)
    monkeypatch.setattr(keyring, "get_password", lambda s, u: saved.get((s, u)))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, v: saved.update({(s, u): v}))
    monkeypatch.setenv("GITHUB_TOKEN", "engine-token")
    monkeypatch.setenv("GITHUB_ENTERPRISE_TOKEN", "engine-enterprise-token")
    preferences = SourceControlPreferences(tmp_path / "preferences.json")
    if provider:
        preferences.set(provider)
    caplog.set_level(logging.INFO)
    capabilities = build_capabilities(Settings(
        github_token="engine-token",
        sqlite_path=str(tmp_path / "state.sqlite3"),
        source_control_preferences=preferences if provider else None,
    ))
    source = capabilities.source_control
    adapter = source._providers[provider] if provider else source
    monkeypatch.setattr(adapter, "_root_path", AsyncMock(return_value=str(tmp_path)))
    monkeypatch.setattr(adapter, "_repo_coords", AsyncMock(return_value=("acme", "api")))
    monkeypatch.setattr(
        "engine.adapters.source_control.github.transports.httpx.AsyncClient",
        MagicMock(side_effect=AssertionError("agent PRs must not call the REST API directly")),
    )
    launches = []

    class Process:
        returncode = 0

        async def communicate(self, _input):
            return (
                b'{"html_url": "https://github.com/acme/api/pull/42",'
                b' "user": {"login": "openengine-worker"}}',
                b"",
            )

    async def launch(*argv, **kwargs):
        launches.append((argv, kwargs["env"]))
        return Process()

    monkeypatch.setattr(
        "engine.adapters.source_control.github.transports.asyncio.create_subprocess_exec",
        launch,
    )

    async def open_pr():
        return await source.request_review("workspace", "agent/fix", "main", "fix: bug", "body")

    # Before and after a Settings device flow, the agent still acts as `gh`.
    assert asyncio.run(open_pr()) == "https://github.com/acme/api/pull/42"
    app = github_auth_app()
    with client(app) as browser, patch(
        "engine.apps.web.api.start_device_flow",
        AsyncMock(return_value=DeviceFlowState("device", "code", "https://github.com/login/device", 900, 5)),
    ), patch(
        "engine.apps.web.api.poll_device_flow",
        AsyncMock(return_value=DeviceFlowComplete("personal-token", "personal-refresh")),
    ):
        assert browser.post("/api/github/connect").status_code == 200
        assert browser.post("/api/github/connect/poll").json() == {"status": "complete"}
    assert GitHubCredentialStore().get() == "personal-token"
    assert asyncio.run(open_pr()) == "https://github.com/acme/api/pull/42"

    assert len(launches) == 2
    for argv, env in launches:
        assert argv[:2] == ("gh", "api")
        assert "GITHUB_TOKEN" not in env
        assert "GITHUB_ENTERPRISE_TOKEN" not in env
        assert not any("token" in argument.lower() for argument in argv)
    assert "composition=web github_identity=gh-cli" in caplog.text
    assert "author=openengine-worker" in caplog.text
    assert "engine-token" not in caplog.text
    assert "personal-token" not in caplog.text


def test_agent_pr_under_github_oauth_uses_the_connected_token_read_once(
    tmp_path, monkeypatch, *, github_auth_app, client
):
    """The token `engine connect github` saved opens the PR, with no second keychain read."""
    from engine.apps.web.composition import Settings, build_capabilities
    from engine.apps.web.source_control import SourceControlPreferences

    saved = {}
    reads = []

    def get_password(service, username):
        reads.append(username)
        return saved.get((service, username))

    monkeypatch.setattr(keyring, "get_keyring", _high_priority_backend)
    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "set_password", lambda s, u, v: saved.update({(s, u): v}))
    monkeypatch.setenv("GITHUB_TOKEN", "engine-token")
    preferences = SourceControlPreferences(tmp_path / "preferences.json")
    preferences.set("github-oauth")
    store = GitHubCredentialStore(cached=True)
    capabilities = build_capabilities(
        Settings(
            github_token="engine-token",
            sqlite_path=str(tmp_path / "state.sqlite3"),
            source_control_preferences=preferences,
        ),
        github_credential_store=store,
    )
    source = capabilities.source_control
    adapter = source._providers["github-oauth"]
    monkeypatch.setattr(adapter, "_root_path", AsyncMock(return_value=str(tmp_path)))
    monkeypatch.setattr(adapter, "_repo_coords", AsyncMock(return_value=("acme", "api")))

    app = github_auth_app(credential_store=store)
    with client(app) as browser, patch(
        "engine.apps.web.api.start_device_flow",
        AsyncMock(return_value=DeviceFlowState("device", "code", "https://github.com/login/device", 900, 5)),
    ), patch(
        "engine.apps.web.api.poll_device_flow",
        AsyncMock(return_value=DeviceFlowComplete("personal-token", "personal-refresh")),
    ):
        assert browser.post("/api/github/connect").status_code == 200
        assert browser.post("/api/github/connect/poll").json() == {"status": "complete"}
        assert browser.get("/api/github/status").json()["connected"] is True
    reads.clear()

    recorded = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def request(self, method, url, headers=None, **_kwargs):
            recorded.append((method, url, dict(headers or {})))
            return httpx.Response(
                201, json={"html_url": "https://github.com/acme/api/pull/7", "user": {"login": "me"}},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(
        "engine.adapters.source_control.github.transports.httpx.AsyncClient",
        lambda **_: Client(),
    )
    monkeypatch.setattr(
        "engine.adapters.source_control.github.transports.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=AssertionError("GitHub OAuth must not shell out to gh")),
    )

    url = asyncio.run(source.request_review("workspace", "agent/fix", "main", "fix: bug", "body"))

    assert url == "https://github.com/acme/api/pull/7"
    assert recorded[0][:2] == ("POST", "https://api.github.com/repos/acme/api/pulls")
    assert recorded[0][2]["Authorization"] == "Bearer personal-token"
    assert "github-token" not in reads


def test_oauth_lifecycle_log_never_contains_token_material(caplog) -> None:
    token = "token-that-must-not-appear-in-logs"
    fingerprint = token_fingerprint(token)
    caplog.set_level(logging.INFO, logger="engine.apps.web.oauth_lifecycle")
    oauth_lifecycle_event("github_refresh_started", refresh_token=fingerprint)
    assert token not in caplog.text
    assert fingerprint in caplog.text
    assert '"event": "github_refresh_started"' in caplog.text
    assert '"pid": ' in caplog.text


class TestPollDeviceFlowErrors:
    def test_returns_pending_for_authorization_pending(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(
                _mock_response(200, {"error": "authorization_pending"})
            ),
        )
        result = asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))
        assert isinstance(result, DeviceFlowPending)
        assert result.next_interval == 5  # unchanged

    def test_slow_down_increases_interval_by_five(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {"error": "slow_down"})),
        )
        result = asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))
        assert isinstance(result, DeviceFlowPending)
        assert result.next_interval == 10  # 5 + 5 penalty

    def test_slow_down_accumulates_on_repeated_calls(self, monkeypatch):
        """Each slow_down adds 5 s to whatever the caller passes as current_interval."""
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {"error": "slow_down"})),
        )
        first = asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))
        assert isinstance(first, DeviceFlowPending)
        second = asyncio.run(
            poll_device_flow("cid", "dev-1", current_interval=first.next_interval)
        )
        assert isinstance(second, DeviceFlowPending)
        assert second.next_interval == 15

    def test_raises_on_expired(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {"error": "expired_token"})),
        )
        with pytest.raises(GitHubAuthError, match="expired_token"):
            asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))

    def test_raises_on_access_denied(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {"error": "access_denied"})),
        )
        with pytest.raises(GitHubAuthError, match="access_denied"):
            asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))

    def test_raises_on_http_error(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(503, {})),
        )
        with pytest.raises(GitHubAuthError, match="503"):
            asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))

    def test_raises_when_token_absent(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {})),
        )
        with pytest.raises(GitHubAuthError, match="no access_token"):
            asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))


def test_device_flow_expiries_become_absolute_timestamps():
    with patch("engine.apps.web.github_auth.time.time", return_value=100.0):
        result = credentials_from_device_flow(
            DeviceFlowComplete("access", "refresh", 60, 120)
        )
    assert result == StoredCredentials("access", "refresh", 160.0, 220.0)


def test_expiring_device_flow_credentials_round_trip_through_keychain(monkeypatch):
    saved: dict[str, str] = {}
    monkeypatch.setattr(keyring, "get_keyring", _high_priority_backend)
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda _service, _user, value: saved.setdefault("value", value),
    )
    monkeypatch.setattr(keyring, "get_password", lambda *_: saved.get("value"))
    with patch("engine.apps.web.github_auth.time.time", return_value=100.0):
        credentials = credentials_from_device_flow(
            DeviceFlowComplete("access", "refresh", 60, 120)
        )
    store = GitHubCredentialStore()
    store.set_credentials(credentials)
    assert store.get_credentials() == credentials


def test_credential_store_ignores_boolean_expiry_values(monkeypatch):
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda *_: (
            '{"access_token":"access","expires_at":true,"refresh_token_expires_at":false}'
        ),
    )
    assert GitHubCredentialStore().get_credentials() == StoredCredentials("access")


# ---------------------------------------------------------------------------
# CSRF guard (_is_local_request) via the API endpoints
# ---------------------------------------------------------------------------


class TestCsrfGuard:
    """Mutating GitHub endpoints must reject cross-origin requests."""

    def _post(self, app, path: str, origin: str | None = None, *, client):
        headers = {"origin": origin} if origin else {}
        with client(app, raise_server_exceptions=True) as browser:
            return browser.post(path, headers=headers)

    def test_connect_from_localhost_origin_is_allowed(self, *, github_auth_app, client):
        app = github_auth_app()
        with patch(
            "engine.apps.web.api.start_device_flow",
            new=AsyncMock(
                return_value=DeviceFlowState(
                    device_code="d",
                    user_code="U",
                    verification_uri="https://gh",
                    expires_in=900,
                    interval=5,
                )
            ),
        ):
            resp = self._post(
                app, "/api/github/connect", origin="http://localhost:4364",
                client=client,
            )
        assert resp.status_code != 403

    def test_connect_from_cross_origin_is_rejected(self, *, github_auth_app, client):
        app = github_auth_app()
        resp = self._post(app, "/api/github/connect", origin="https://evil.example.com", client=client)
        assert resp.status_code == 403

    def test_connect_from_lookalike_localhost_origin_is_rejected(
        self, *, github_auth_app, client
    ):
        app = github_auth_app()
        resp = self._post(
            app, "/api/github/connect", origin="https://localhost.evil.example.com",
            client=client,
        )
        assert resp.status_code == 403

    def test_disconnect_from_cross_origin_is_rejected(self, *, github_auth_app, client):
        app = github_auth_app()
        resp = self._post(
            app, "/api/github/disconnect", origin="https://evil.example.com",
            client=client,
        )
        assert resp.status_code == 403

    def test_disconnect_from_https_localhost_is_allowed(self, *, github_auth_app, client):
        app = github_auth_app()
        resp = self._post(
            app, "/api/github/disconnect", origin="https://localhost:8443",
            client=client,
        )
        assert resp.status_code == 204

    def test_status_is_exempt_from_csrf_guard(self, *, github_auth_app, client):
        app = github_auth_app()
        with client(app) as browser:
            resp = browser.get(
                "/api/github/status", headers={"origin": "https://evil.example.com"}
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# poll endpoint returns nextInterval only when pending
# ---------------------------------------------------------------------------


class TestPollEndpoint:
    def test_complete_response_has_no_next_interval(self, *, github_auth_app, client):
        app = github_auth_app()
        flow = DeviceFlowState(
            device_code="d",
            user_code="U",
            verification_uri="https://gh",
            expires_in=900,
            interval=5,
        )
        with (
            patch(
                "engine.apps.web.api.start_device_flow",
                new=AsyncMock(return_value=flow),
            ),
            patch(
                "engine.apps.web.api.poll_device_flow",
                new=AsyncMock(return_value=DeviceFlowComplete(access_token="tok")),
            ),
            patch(
                "engine.apps.web.oauth_credentials.keyring.get_keyring",
                return_value=_high_priority_backend(),
            ),
            patch("engine.apps.web.oauth_credentials.keyring.set_password"),
        ):
            with client(app) as browser:
                browser.post("/api/github/connect")
                resp = browser.post("/api/github/connect/poll")
        body = resp.json()
        assert body["status"] == "complete"
        assert "nextInterval" not in body

    def test_pending_response_carries_next_interval(self, *, github_auth_app, client):
        app = github_auth_app()
        flow = DeviceFlowState(
            device_code="d",
            user_code="U",
            verification_uri="https://gh",
            expires_in=900,
            interval=5,
        )
        with (
            patch(
                "engine.apps.web.api.start_device_flow",
                new=AsyncMock(return_value=flow),
            ),
            patch(
                "engine.apps.web.api.poll_device_flow",
                new=AsyncMock(return_value=DeviceFlowPending(next_interval=10)),
            ),
        ):
            with client(app) as browser:
                browser.post("/api/github/connect")
                resp = browser.post("/api/github/connect/poll")
        body = resp.json()
        assert body["status"] == "pending"
        assert body["nextInterval"] == 10


class TestSourceControlProviderEndpoint:
    def test_saved_oauth_choice_does_not_probe_gh_cli(
        self, tmp_path, monkeypatch, *, github_auth_app, client
    ) -> None:
        from engine.apps.web.source_control import SourceControlPreferences

        SourceControlPreferences(tmp_path / "settings.json").set("github-oauth")
        monkeypatch.setattr(
            "engine.apps.web.source_control.gh_cli_status",
            lambda: pytest.fail("saved OAuth preference must not probe GH CLI"),
        )
        app = github_auth_app()
        with client(app) as browser:
            response = browser.get("/api/source-control/provider")

        assert response.json() == {"provider": "github-oauth", "autoSelected": False}

    def test_selects_provider_and_rejects_obsolete_gitlab_name(
        self, monkeypatch, *, github_auth_app, client
    ) -> None:
        from engine.apps.web.source_control import GhCliStatus

        monkeypatch.setattr(
            "engine.apps.web.source_control.gh_cli_status",
            lambda: GhCliStatus(True, True, account="octocat"),
        )
        app = github_auth_app()
        with client(app) as browser:
            status = browser.get("/api/source-control/status")
            selected = browser.post(
                "/api/source-control/provider", json={"provider": "github-oauth"}
            )
            gitlab = browser.post(
                "/api/source-control/provider",
                json={"provider": "gitlab-oauth", "origin": "https://gitlab.com"},
            )
            rejected = browser.post(
                "/api/source-control/provider", json={"provider": "gitlab"}
            )

        assert status.json()["provider"] == "gh-cli"
        assert status.json()["ghCli"]["account"] == "octocat"
        assert selected.status_code == 204
        assert gitlab.status_code == 204
        assert rejected.status_code == 400


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _high_priority_backend():
    backend = MagicMock()
    backend.priority = 5
    return backend


class _AsyncContextManager:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def post(self, *_args, **_kwargs):
        return self._response


def _client_returning(response: httpx.Response) -> _AsyncContextManager:
    return _AsyncContextManager(response)


def test_browser_users_have_isolated_credentials_and_device_flows(
    monkeypatch, *, github_auth_app, client
):
    from engine.apps.web.github_login import GitHubLogin, GitHubLoginConfig

    saved = {}
    monkeypatch.setattr(keyring, "get_keyring", _high_priority_backend)
    monkeypatch.setattr(keyring, "get_password", lambda s, u: saved.get((s, u)))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, v: saved.update({(s, u): v}))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: saved.pop((s, u), None))
    GitHubCredentialStore().set("legacy-personal-token")
    config = GitHubLoginConfig("id", "secret", "https://engine.test/api/auth/github/callback")
    login = GitHubLogin(config)
    monkeypatch.setattr("engine.apps.web.api.GitHubLogin", lambda *_, **__: login)
    app = github_auth_app(client_id="", login_config=config)
    start = AsyncMock(side_effect=[
        DeviceFlowState("alice-device", "alice-code", "https://github.com/login/device", 900, 5),
        DeviceFlowState("bob-device", "bob-code", "https://github.com/login/device", 900, 5),
    ])
    poll = AsyncMock(side_effect=[DeviceFlowComplete("alice-token"), DeviceFlowComplete("bob-token")])
    monkeypatch.setattr("engine.apps.web.api.start_device_flow", start)
    monkeypatch.setattr("engine.apps.web.api.poll_device_flow", poll)
    with client(app, base_url="https://engine.test") as browser:
        def as_user(user_id, name):
            browser.cookies.set("engine_session", login._make_session_cookie(user_id, name))

        as_user(1, "alice")
        assert browser.get("/api/github/status").json() == {
            "connected": False, "clientIdConfigured": False, "agentsUseConnection": False
        }
        assert browser.post("/api/github/client-id", json={"clientId": "alice-client"}).status_code == 204
        assert browser.post("/api/github/connect").json()["userCode"] == "alice-code"
        as_user(2, "bob")
        assert browser.post("/api/github/connect/poll").status_code == 409
        assert browser.get("/api/github/status").json()["clientIdConfigured"] is False
        assert browser.post("/api/github/client-id", json={"clientId": "bob-client"}).status_code == 204
        assert browser.post("/api/github/connect").json()["userCode"] == "bob-code"
        as_user(1, "alice")
        assert browser.post("/api/github/connect/poll").json()["status"] == "complete"
        as_user(2, "bob")
        assert browser.get("/api/github/status").json()["connected"] is False
        assert browser.post("/api/github/connect/poll").json()["status"] == "complete"
        assert browser.post("/api/github/disconnect").status_code == 204
        as_user(1, "alice-renamed")
        assert browser.get("/api/github/status").json()["connected"] is True
    assert poll.await_args_list[0].args == ("alice-client", "alice-device", 5)
    assert poll.await_args_list[1].args == ("bob-client", "bob-device", 5)
    assert GitHubCredentialStore(user_id=1).get() == "alice-token"
    assert GitHubCredentialStore(user_id=2).get() is None
    assert GitHubCredentialStore().get() == "legacy-personal-token"
    assert GitHubCredentialStore(user_id=1).credential_identity != GitHubCredentialStore(user_id=2).credential_identity
