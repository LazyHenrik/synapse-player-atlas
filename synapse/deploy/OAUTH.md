# MonoSuite sign-in

The shared VPS deployment can require MonoSuite sign-in. It uses a confidential application, authorization codes, S256 PKCE, and a browser-bound, single-use state value. The GraphQL collector still uses `MonoSuiteClient`; the OAuth token exchange is a separate protocol. Nothing in this integration changes players, logs, punishments, or notes.

## Enable it

Register a confidential application in [MonoSuite settings](https://monosuite.com/settings/applications). Use the exact callback `https://YOUR-DOMAIN/auth/monosuite/callback`. During development, `http://127.0.0.1:8790/auth/monosuite/callback` works through an SSH tunnel on local port 8790. Other public HTTP callbacks are rejected.

Only request `moderation.blacklist.view`, `moderation.note.view`, and `moderation.warning.view`. The viewer uses OAuth to verify identity, not to collect records with each visitor's token. These scopes do not restrict access to records already stored in the shared database.

Save this structure as `synapse/deploy/secrets/oauth/client.json` on the server. Fill in actual values there, never in a commit or browser bundle:

```json
{
  "client_id": "YOUR_CLIENT_ID",
  "client_secret": "YOUR_CLIENT_SECRET",
  "redirect_uri": "https://YOUR-DOMAIN/auth/monosuite/callback",
  "scopes": [
    "moderation.blacklist.view",
    "moderation.note.view",
    "moderation.warning.view"
  ],
  "allowed_subjects": []
}
```

Set the directory to mode 700 and the file to mode 600, owned by UID/GID 10001 (the viewer container account). Only the viewer mounts this directory. Add `SYNAPSE_REQUIRE_AUTH=true` to the deployment `.env`. This makes startup fail if someone omits the OAuth override, instead of silently serving data without login.

From `synapse/deploy`:

```sh
sudo docker compose -f compose.shared.yaml -f compose.oauth.yaml build viewer
sudo docker compose -f compose.shared.yaml -f compose.oauth.yaml run --rm --no-deps \
  --user root --cap-add CHOWN --cap-add FOWNER --entrypoint python viewer \
  -c 'import os; os.chmod("/auth",0o700); os.chown("/auth",10001,10001)'
sudo docker compose -f compose.shared.yaml -f compose.oauth.yaml up -d --no-deps viewer
```

The extra capabilities are used only for that one-time volume initialization. The running services retain their dropped capabilities. Include both Compose files in subsequent operations; the collector and host proxy remain separate from login.

## Approve staff

Set `owner_subjects` in `client.json` to an explicit list of verified MonoSuite account identifiers belonging to the people who manage Atlas access. For example, add `"owner_subjects": ["YOUR_VERIFIED_ACCOUNT_ID"]`. Obtain the identifier by signing in and checking the approval page. Never automatically promote the first visitor. Owner accounts can enter Atlas and manage access even if absent from `allowed_subjects`.

Owners see **Manage access** in the Atlas header, linking to `/admin/access`. Unapproved visitors who successfully sign in automatically create a pending request with their verified account identifier and display name. Owners can approve or deny requests, revoke existing access, and restore denied or revoked accounts. Newly approved staff sign in again. Display names are not unique: verify the account identifier when deciding.

Approved staff can read the entire collected dataset, including PMs. Staff approval does not grant permission to manage other users. This is an explicit Atlas decision, not a mirror of MonoSuite group roles.

Requests, decisions, and an audit trail of who changed access are persisted in the authentication database. Revocation invalidates all local sessions immediately and overrides the legacy `allowed_subjects` list. Repeat sign-ins do not reset denied or revoked decisions. Existing allowed staff remain supported and appear in the portal. Owners can only be changed through server configuration, preventing accidental owner lockout in the portal.

Configuration is re-read on every request and malformed or unreadable configuration denies access. Use atomic replacement preserving ownership and permissions. Client ID, secret, or callback changes require a viewer restart. With both access lists empty and no portal approvals, nobody is admitted.

## Sessions and secrets

Sessions expire after eight hours, survive a viewer restart, and are rechecked against MonoSuite at least once per minute while used. Refresh tokens rotate on the server when the access token approaches expiry. A provider failure denies data access until verification succeeds again. Removing an account from `allowed_subjects` takes effect on its next request.

Cookies are HttpOnly and SameSite=Lax. HTTPS uses Secure cookies with the `__Host-` prefix; the explicit loopback development configuration uses HTTP cookies. The app checks the configured Host instead of trusting forwarded headers. Caddy must preserve the public Host.

Let the application set `Referrer-Policy`; do not override it in Caddy. The owner portal uses `same-origin` so native approval forms send the origin required by CSRF validation. Other authentication responses, including callbacks, use `no-referrer` to keep authorization query strings private. Setting `no-referrer` globally can make browser form submissions send `Origin: null` and fail with `Invalid access decision`.

Sign out deletes the local session and stored tokens. It does not sign out of MonoSuite or withdraw the application grant. Users can withdraw that grant in MonoSuite's application settings. Session storage contains access and refresh tokens in the separate `synapse_authentication` volume, with a mode-700 directory and mode-600 database. It is not encrypted at rest; host administrators can read it. Treat backups of that volume as credentials. Observation backups do not include it.

Do not enable access logs containing callback query strings or log provider response bodies. The application deliberately returns generic provider errors so authorization codes and tokens do not end up in logs or error pages.

## Move to a domain

Add the exact HTTPS callback to the registered MonoSuite application, update `redirect_uri` on the VPS, and recreate the viewer. Configure the domain in the shared Caddy proxy and point its DNS A record to the VPS. The reverse proxy target remains `synapse-viewer:8787`. Retain any existing password gate until the OAuth flow has been verified through HTTPS; it can then be removed from this site's configuration.

Verify the full consent/callback flow, Secure cookies, sign out, and rejection of unauthenticated `/api/graph` and `/api/logs`. The temporary loopback callback does not make the app publicly accessible.

## Provider findings

Checked on 2026-09-17: when a visitor is signed out of MonoSuite, its authorization flow lands on `https://monosuite.com/login` without a return destination. Completing provider login can therefore land on the MonoSuite dashboard instead of invoking the Atlas callback. Atlas's sign-in page offers a separate-tab MonoSuite login: finish that login, return to Atlas, and use its sign-in button to start a fresh authorization request. This is a workaround for the provider's lost continuation, not an automatic callback repair. Do not reuse an old callback URL or weaken state/cookie validation. A seamless first-time sign-in requires MonoSuite to preserve the pending authorization request through its login flow.

Checked on 2026-09-11: [OAuth discovery](https://auth.monosuite.com/.well-known/oauth-authorization-server) advertises authorization codes, refresh tokens, S256 PKCE, and `client_secret_basic`/`client_secret_post`. It incorrectly duplicates `/api` in its endpoint URLs: those routes return 404. The working routes are under `https://auth.monosuite.com/api/oauth/`: `authorize`, `token`, and `userinfo`. The integration pins these verified routes and refuses HTTP redirects when sending credentials. OpenID discovery returns 404; this is OAuth with a verified identity endpoint, not an assumed OIDC/JWT implementation.

The collector credential is independent. Enabling sign-in does not replace its expiring dashboard token, and application-token compatibility with the GraphQL transport remains a separate check.
