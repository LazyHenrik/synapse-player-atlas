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

An empty `allowed_subjects` list admits nobody. A successful MonoSuite login for an unapproved account displays its verified account identifier on the access-approval page. Confirm the person and add that exact identifier to the server-side list. Never auto-approve the first person to log in. Account identifiers come from the OAuth identity endpoint's `sub`, not a display name or an unverified decoded token.

Every approved account can read the entire collected dataset, including PMs. This is an explicit Atlas staff list, not an automatic mirror of MonoSuite group roles. Remove staff from this list when they should lose access; changing their MonoSuite role alone does not remove Atlas access. The list is re-read for every request, and malformed or unreadable configuration denies access.

Use an atomic file replacement when editing, preserving ownership and permissions. Because the directory is mounted, a replacement file becomes visible without recreating the container. Changes to the client ID, secret, or callback require a viewer restart.

## Sessions and secrets

Sessions expire after eight hours, survive a viewer restart, and are rechecked against MonoSuite at least once per minute while used. Refresh tokens rotate on the server when the access token approaches expiry. A provider failure denies data access until verification succeeds again. Removing an account from `allowed_subjects` takes effect on its next request.

Cookies are HttpOnly and SameSite=Lax. HTTPS uses Secure cookies with the `__Host-` prefix; the explicit loopback development configuration uses HTTP cookies. The app checks the configured Host instead of trusting forwarded headers. Caddy must preserve the public Host.

Sign out deletes the local session and stored tokens. It does not sign out of MonoSuite or withdraw the application grant. Users can withdraw that grant in MonoSuite's application settings. Session storage contains access and refresh tokens in the separate `synapse_authentication` volume, with a mode-700 directory and mode-600 database. It is not encrypted at rest; host administrators can read it. Treat backups of that volume as credentials. Observation backups do not include it.

Do not enable access logs containing callback query strings or log provider response bodies. The application deliberately returns generic provider errors so authorization codes and tokens do not end up in logs or error pages.

## Move to a domain

Add the exact HTTPS callback to the registered MonoSuite application, update `redirect_uri` on the VPS, and recreate the viewer. Configure the domain in the shared Caddy proxy and point its DNS A record to the VPS. The reverse proxy target remains `synapse-viewer:8787`. Retain any existing password gate until the OAuth flow has been verified through HTTPS; it can then be removed from this site's configuration.

Verify the full consent/callback flow, Secure cookies, sign out, and rejection of unauthenticated `/api/graph` and `/api/logs`. The temporary loopback callback does not make the app publicly accessible.

## Provider findings

Checked on 2026-09-11: [OAuth discovery](https://auth.monosuite.com/.well-known/oauth-authorization-server) advertises authorization codes, refresh tokens, S256 PKCE, and `client_secret_basic`/`client_secret_post`. It incorrectly duplicates `/api` in its endpoint URLs: those routes return 404. The working routes are under `https://auth.monosuite.com/api/oauth/`: `authorize`, `token`, and `userinfo`. The integration pins these verified routes and refuses HTTP redirects when sending credentials. OpenID discovery returns 404; this is OAuth with a verified identity endpoint, not an assumed OIDC/JWT implementation.

The collector credential is independent. Enabling sign-in does not replace its expiring dashboard token, and application-token compatibility with the GraphQL transport remains a separate check.
