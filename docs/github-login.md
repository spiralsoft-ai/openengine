# GitHub browser login

Register a separate GitHub OAuth App for login and configure its callback as
`https://your-engine-host/api/auth/github/callback`. Set these top-level values
in `engine.toml`:

```toml
github_login_client_id = "your-login-client-id"
github_login_redirect_uri = "https://your-engine-host/api/auth/github/callback"
```

`ENGINE_GITHUB_LOGIN_CLIENT_ID` and `ENGINE_GITHUB_LOGIN_REDIRECT_URI`
environment variables override those defaults. HTTP callbacks are accepted only
for loopback development hosts.

Store `ENGINE_GITHUB_LOGIN_CLIENT_SECRET=your-secret` in a server-local `.env`
beside the loaded `engine.toml` (or in the working directory if no config file
is loaded). This file is gitignored; restrict its permissions to the service
owner (`chmod 600 .env`) and edit it via SSH. The app reads it directly, with
dotenv interpolation disabled, without sourcing it into a shell. An explicit
process environment secret takes precedence. Secrets are not accepted in TOML.
The secret file is reread at each token exchange, so updating it takes effect
without a restart; changing the client ID or callback URL requires a restart.
All three values are required when enabling login.

Keep these values out of the repository's `engine.toml`: a checkout started
with the client ID and callback but no secret refuses to start. Give the
deployment its own config outside the checkout, such as
`~/.config/openengine/engine.toml` with its `.env` beside it, and point the
service at it with `ENGINE_CONFIG` or `engine-web --config`. `ENGINE_CONFIG`
wins over the machine config. Without an explicit selection, Engine prefers
`$XDG_CONFIG_HOME/openengine/engine.toml` (default
`~/.config/openengine/engine.toml`) over `engine.toml` in the working directory.
These files are not merged.

Visit `/login` and select **Sign in with GitHub** to start the browser
authorization flow. It requests only `read:user`, uses OAuth state and PKCE,
and issues a signed, HttpOnly session cookie at the callback before redirecting
to `/`. The user token is not returned or stored in the server's repository
credential store. This follows
[GitHub's web OAuth flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).

When login is configured, the frontend requires a verified session before
mounting the application. Middleware returns 401 for unauthenticated requests
to protected `/api/` and `/graph/api/` routes; the four GitHub login endpoints and `/api/health` remain public. Slack events bypass browser
session checks and retain Slack signature verification. The frontend rechecks
session status every 30 seconds and unmounts the app if the session is invalid.

## Repository access

A GitHub account alone does not open the app: WorkOrder links are posted to
GitHub issues, which anyone can read. At the callback, the server asks GitHub
whether the signed-in account has write access (write, maintain, or admin) to
any of this deployment's repositories: the `[github] repository` or `repositories` named in
`engine.toml`, and the GitHub repository behind each `[repos]` checkout (read
from its `origin` remote). Team and organization grants count. The check uses
the server's own GitHub connection, the one selected under **GitHub** in
Settings: the host's `gh` login for **GH CLI**, or the token saved by
`engine connect github` for **GitHub OAuth**. It binds GitHub's answer to the
signed-in account's numeric user ID, so a renamed login cannot inherit another
account's access. An account without write access is sent to
`/login?error=forbidden` and receives no session.

The server's connection can fail: an expired OAuth token or `gh` login, or
GitHub not answering. So that this does not lock out everyone who could fix it,
the server then asks GitHub with the user's own sign-in token, within what is
left of the same 10-second budget. That lookup runs only when the server's
lookup fails, and its answer is used only to let the user in. It cannot overturn the
server's no. The sign-in token has only `read:user` scope, so it can confirm
write access to public github.com repositories only. For a private repository
it gets no answer, and a lookup that gets no answer admits nobody. The token
was issued for the verified user ID, so the answer stays bound to that ID. The
server keeps it in memory, never on disk, for rechecks. Each browser session
keeps its own token, so signing out in one browser leaves the others theirs.
The user's tokens are asked one at a time, newest first, and any one that
confirms write access lets the user in, so one browser's revoked token does not
lock out the others.
The server drops a session's token when that session signs out or its cookie
expires, and drops all of a user's tokens when GitHub says the user no longer
has access. When neither lookup confirms access, login is
refused with `/login?error=unverified`, and the login page says the server's
GitHub connection probably needs reconnecting.

Signed-in requests recheck access. `/api/auth/github/status` and protected API
requests reuse GitHub's answer for up to five minutes per user, then ask
again. When access has been revoked, the API answers 401 and the status
endpoint clears the session. A failed lookup is not cached: that request,
including the status check, gets 503, and the next request asks GitHub again.
Responses that stay open, such as event streams, recheck every 30 seconds and
end when access is revoked or can no longer be confirmed.

### Operators

`[access] operators` lists GitHub user IDs that sign in without a repository
check:

```toml
[access]
operators = [583231]
```

Use numeric IDs, not logins, because a login can be renamed and then claimed
by someone else. Find an account's ID with `gh api users/<login> --jq .id`.
Operators can let in a new person before that person has write access
anywhere. Operators can also sign in when the server's GitHub connection has
failed and the repositories are private.

### When the server's GitHub connection fails

While the server's lookups are failing, everyone signed in sees a warning
beside the Sign out button. The failing GitHub error is written to the server
log under `could not check repository access for <login>`. To recover:

- From the web UI, anyone signed in can switch **GitHub** in Settings to a
  login that works. For example, choose **GH CLI** when the host's `gh` login
  is valid.
- On the server, run `engine connect github` to reconnect **GitHub OAuth**, or
  `gh auth login` to fix **GH CLI**. Run `gh auth status` to diagnose.

While browser login is on, the web UI cannot replace the server's GitHub OAuth
token. A token connected in Settings belongs to the signed-in user (see
[Agent GitHub identity](#agent-github-identity)), because the server's token is
the account every agent acts as. Letting a web session replace it would let
anyone who gets past the access check change who the agents act as, including
someone admitted only by their own token's answer. Replacing it takes shell
access on the server.

Configuring GitHub login with no `[github] repository` or `repositories`, no GitHub checkout in
`[repos]`, and no operators is a configuration error, and the server does not
start.

## Service token for the MCP gateway

The [remote MCP gateway](remote-mcp.md) creates work orders server to server
and cannot hold a browser session. Set `ENGINE_SERVICE_TOKEN` in the same
server-local `.env` (or the process environment, which takes precedence) and
give the gateway the same value as `OE_MCP_ENGINE_TOKEN`:

```sh
openssl rand -hex 32
```

The middleware accepts `Authorization: Bearer <token>` in place of a session
only for `POST /api/runs`; every other protected route still requires login.
The token must have at least 32 non-whitespace characters; a shorter value
fails startup. Like the client secret, it is never read from TOML and is reread
on each request, so rotating it in `.env` needs no restart (update the
gateway's value and restart the gateway). Leaving it unset admits no service
requests.

Login state and the PKCE verifier live in a signed, HttpOnly browser cookie that expires after ten minutes;
abandoned logins reserve no server slots. Replay protection relies on GitHub
consuming authorization codes once and binding them to the PKCE verifier.
The cookie signing key lives in one process, so a restart requires a new
login and multiple workers require sticky routing or a shared signing key.
Without login configuration the app remains accessible and the status endpoint
reports `loginRequired: false`; starting the OAuth flow returns 503.

## Agent GitHub identity

Agent GitHub API actions in the web composition follow the GitHub choice in
Settings. **GH CLI** uses the host's `gh auth` login: the account shown by
`gh auth status` for the OS user that runs the web process. **GitHub OAuth**
uses the host's device-flow token saved by `engine connect github` (or by
Settings while browser login is off), and that account is the PR/comment
author for every user's WorkOrders. The web process reads that token from
the OS keychain once and keeps it in memory, so connecting is the only
keychain prompt until the service restarts. GitLab routing is unchanged.
Neither the browser login nor `GITHUB_TOKEN` is used for agent actions.
OpenEngine removes `GITHUB_TOKEN` and `GITHUB_ENTERPRISE_TOKEN` from the
environment it passes to `gh`, so an engine setting cannot override the CLI
login. `GH_TOKEN` remains `gh`'s own setting and is honored. The worker
composition still uses its configured `GITHUB_TOKEN`.

Git commits still use Git's author/committer configuration and configured
agent attribution; pushes still use the host's Git credential helper or SSH
credentials (`gh auth setup-git` makes Git use the same login).

Browser login requests `read:user` and only creates a session cookie. The
separate Settings device flow stores repository connection credentials in the
OS keychain. With browser login enabled, its token and client-ID keys include
the verified session's stable GitHub user ID (for example,
`github-token:user:123`). Pending device flows are also scoped to that ID.
Logging out or renaming an account does not transfer its connection to another
user. Authenticated users never inherit the legacy `github-token` entry: they
must reconnect. Local mode without browser login retains the legacy entry,
and that entry is the one **GitHub OAuth** agent actions use, so a signed-in
user's own connection never changes the agents' account; Settings says so.

The web `--check` wiring report shows whether `gh` is authenticated and as
which account. Successful PR creation logs the returned URL, GitHub's actual
author login, and transport at INFO level. Enable INFO logging to retain this
audit evidence.

For graph runs served by the web app, `compose_app` passes its composed
`source_control` to `build_graph_runtime`; terminal MCP resolves that runtime
capability for `open_pull_request`. Worker-dispatched work uses the worker
composition's independent capability. A historical PR cannot be attributed to
a particular process or token from the code alone: correlate its URL and run
with process logs. No historical run ID, PR URL, or credential audit was
available for this change; the old web keychain and CLI paths could both use a
personal identity, whereas the worker used only its configured token.

Repository configuration is live within the daemon. Adding a GitHub checkout to
the in-memory repository registry immediately extends sign-in access to that
project's writers, just as restarting with that checkout in `[repos]` would.
Its project also participates in repository visibility checks immediately.
Registry updates do not write the configuration file; persistence is a separate
operation.
