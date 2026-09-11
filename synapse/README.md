# Synapse player atlas

Synapse collects who is online, estimates shared time, and overlays gameplay interactions and administrative history on an interactive player graph. It is a Python collector plus a browser application. Both use the same SQLite database. The optional login gateway holds its own OAuth credentials and calls MonoSuite to verify sessions. The viewer never receives the collector credential.

This is ready to run locally or as one private, shared staff application on a Linux server. The Docker deployment includes HTTPS and individual staff passwords. Optional MonoSuite sign-in uses an explicit staff approval list. This is not a public player directory or a multi-tenant service. The application integration findings and the next steps are below.

## Try the viewer

The viewer follows [Project: Synapse](https://project-synapse.com/)'s charcoal-and-white palette, uppercase typography, and outlined controls. The bundled lambda icon and wordmark come from the site's public `assets/images/navbar-icon.png` and `assets/images/title.webp` files. They are Project: Synapse branding, not covered by this repository's code license. Other communities should replace these assets with their own branding. The interface uses system sans-serif fonts rather than redistributing the site's DIN font.

From the repository root, with Python 3.11 or newer:

```sh
python -m pip install -r synapse/requirements.txt
python -m synapse demo --db demo.sqlite
python -m synapse serve --db demo.sqlite
```

Open http://127.0.0.1:8787. Demo generation writes seven days of fictional observations and refuses to overwrite an existing file. Wait for it to finish before opening the viewer. The demo is visibly labelled throughout.

Select a date range and connection layers, then apply the filters. Click a player or find them in the directory to see their observed time, strongest visible co-presence connections, and administrative records. Scroll to zoom, drag the background to pan, and drag nodes to rearrange them. Graph nodes also support Enter and Space. Clicking an edge displays its measures below the graph. Co-presence and gameplay edges also open the log explorer for that pair. The player panel shows every collected history kind in the selected window, regardless of the graph layer checkboxes.

## Collect real observations

The existing `monosuite_cli.MonoSuiteClient` handles every GraphQL request, authentication header, timeout, and HTTP authentication retry. Synapse passes a `token_provider`, and also retries once after a GraphQL authentication error. It does not implement another MonoSuite HTTP client.

Credentials are resolved in this order:

1. `SYNAPSE_TOKEN_FILE`: a UTF-8 file containing only the credential. The provider reads it again when asked to refresh.
2. `SYNAPSE_BROWSER`: a browser name understood by the CLI, such as `firefox`. Requires `browser-cookie3`.
3. `MONOSUITE_TOKEN`.
4. The CLI's saved token in `~/.monosuite_cli.json`, reread on refresh.

A token provider does not manufacture a new login. A browser session must still be valid, or an external credential manager must replace the token file. Updating the environment of a shell does not update an already-running collector. For an unattended host, use the file provider and an established rotation process. API keys may make this simpler, but their compatibility needs to be verified first; see the integration section.

For a local Firefox session in PowerShell:

```powershell
python -m pip install browser-cookie3
$env:SYNAPSE_BROWSER = 'firefox'
python -m synapse servers
python -m synapse collect --server YOUR_SERVER_ID --db observations.sqlite --once
python -m synapse collect --server YOUR_SERVER_ID --db observations.sqlite
```

On Linux, use `export SYNAPSE_TOKEN_FILE=/path/to/monosuite.token` instead. The server resolves from `--server`, `MONOSUITE_SERVER_ID`, or the existing CLI configuration. `servers` lists available organizations, groups, and servers without changing the configuration. Each database is bound to one server and one sampling interval; the program rejects accidental reuse for another server or cadence.

In another terminal:

```sh
python -m synapse serve --db observations.sqlite
```

The collector defaults to one presence sample per minute. History runs independently: one ban page and one discovered player's history per step, with at least two seconds between steps. A completed history scan becomes due again six hours later. `--interval` and `--history-interval` change these values. The minimum poll interval is ten seconds. Check the account's API limits before increasing the rate.

`--once` validates the schema, attempts one presence sample, runs one history step, and fetches one gameplay-log window. It is useful for checking credentials; it is not a complete historical backfill. Inspect the collection status for history errors and unfinished page offsets.

## What the graph measures

A sample records the roster returned by `server.onlinePlayers`, the local UTC time when the response completed, and the server's online flag. We deliberately do not infer presence from lifetime playtime, account creation dates, bans, or notes.

For consecutive successful polling slots, take the intersection of their rosters. A player in both receives the elapsed time between samples. A pair in both receives that much shared time. We only count intervals up to 1.5 times the configured cadence, clip them to the selected time window, and require adjacent slots. Failed polls, skipped slots, and longer gaps receive no inferred duration. The final sample has no duration until another sample arrives. A genuine empty/offline roster is a successful observation; a null roster or missing server status is an error.

For pair A and B, the graph exposes:

| Measure | Calculation | Reading |
| --- | --- | --- |
| Shared minutes | Sum of accepted shared seconds / 60 | How much sampled time overlaps |
| Crowd-adjusted minutes | Sum of shared seconds / `max(larger endpoint roster size - 1, 1)` / 60 | Discounts overlap on a busy server |
| Share of observed time | `shared / (A's observed time + B's observed time - shared)` | Jaccard overlap, from zero to one |
| UTC days | Distinct UTC dates touched by accepted overlap | A small check against a single long session |

Crowd-adjusted time determines the default edge thickness and weights Louvain community detection, with a fixed seed for reproducibility. The minimum filter always means raw shared minutes. Choosing another edge emphasis changes the drawing, not the clustering. Cluster colors come only from retained co-presence edges; neither administrative nor gameplay links affect community detection. Isolated players are grey and are not counted as a suggested cluster.

Administrative edges are directed from the recorded staff account to the subject. Their weight is the number of records of that kind in the time window. Records without a known author still appear in player history but create no invented author edge. Group-wide bans and group-scoped notes are labelled as such. Sharing a staff member, a ban reason, or a note type never creates a player-to-player social connection.

### Where this will mislead you

Co-presence is opportunity for interaction. It does not establish conversation, proximity, friendship, faction membership, coordination, or misconduct. Everyone on the same busy server overlaps. Timezones, work schedules, AFK time, server events, staff shifts, and population peaks can all produce convincing-looking clusters without a social relationship. Small quiet sessions get more weight, which can also exaggerate coincidental overlap.

Sampling misses short visits, and a disconnect/reconnect between two samples is invisible. Requiring presence at both endpoints undercounts arrivals and departures, while counting between endpoints can overcount a brief intervening absence. Local response time includes API latency. The API may itself return a stale roster; we have no independent heartbeat from the game server to prove freshness. A server reporting offline with players still listed is rejected, but not every stale response is detectable.

Coverage is accepted observation time divided by the entire selected window. Empty-server intervals count as coverage. Time before collection began does not. It is not uptime, the fraction of players captured, or a confidence score. Missing intervals are unknown, not evidence that players were absent. Compare windows with similar coverage before interpreting a change.

Jaccard can approach 100% for a pair seen only briefly; look at shared minutes and days too. Louvain always tries to partition a weighted graph. Its output changes with the time range, minimum threshold, missing samples, population, and display limits. Cluster numbers are not stable identifiers across windows. These are leads to explore, not named factions or findings about player intent.

## Player interactions and shared logs

Enable Damage, Kills & deaths, Radio, Private messages, or Other communication in the graph filters. These are separate layers; no combined friendship score is calculated. A gameplay edge means the API listed both accounts in the same record. It is undirected because the participant list does not specify attacker, victim, sender, or recipient roles. Read the original message to interpret the event. Damage can come from combat, accidents, or training. The Deaths category also contains corpse creation, and Communication includes commands as well as speech.

For a record listing `n` distinct accounts, each pair receives `1 / (n - 1)` weight. This reduces the influence of large multi-person records. The edge also reports the unadjusted number of shared records. Counts are not damage totals, confirmed kills, friendship, or proof that a message was delivered. The co-presence emphasis selector changes only co-presence thickness. When the graph reaches its edge cap, sorting compares these different numerical scales; filter to one layer when deciding which connections are strongest.

Radio and PM labels recognize the logged slash-command envelope, including its Steam ID, rather than searching message text for words like "radio". The recognized radio commands are `/radio`, `/radiowhisper`, `/radioyell`, and `/tac`; PM commands are `/pm` and `/privatemessage`. Other communication stays in the general layer. Unrecognized server formats, aliases, and plain-text PM envelopes may therefore appear under Other communication. These labels identify the logged command, not a verified audience or successful delivery.

Many real communication records list only the sender. They remain searchable and their account can appear as an isolated node, but they cannot produce a pair edge. Synapse never guesses recipients from the online roster, radio membership, message text, or character names. Character names and current profile names can differ. Participant IDs are the join keys.

The log explorer uses the **applied graph time range**, independently of its connection checkboxes. Open it from a graph edge or a player's detail panel, or search its full collected directory by name, Steam ID, or MonoSuite ID. Select up to 20 accounts:

- **Between any two selected** finds records with at least two selected accounts. With one selected account, it finds that account's linked records.
- **All selected in the same record** requires every selected account in one record. It does not combine separate pairwise encounters into a group event.
- **Any selected player** also includes single-sender records and encounters with unselected people.

With no accounts selected, the search covers all collected records in the window, including records without participants. Filter by log type and a literal message substring. Results show the original message, linked accounts, category, record ID, original seconds timestamp, and last retrieval time. Load more uses a local `(timestamp, ID)` cursor, so records sharing a timestamp do not vanish between pages. Live imports can add records ahead of your current page; run the search again to include them. Unknown categories are retained and available under Other categories.

### Log collection and missing evidence

A third worker imports all server log categories, starting with the preceding seven days on a new collection. Set `--log-lookback-days` from 1 to 90 on first run to choose that starting window. Changing it later does not reset existing progress. After a restart, queued work and the saved end of the queue are resumed, including time while the collector was stopped.

New windows end at the minute boundary at least 60 seconds behind the collector clock. Every ten minutes, the last five minutes of completed windows are queued again to catch late arrivals; very late imports and edits to older records can be missed. The worker alternates old backfill and recent pending work, requests at most 500 records at a time, and waits two seconds between steps. Failures retain the pending window and retry after a minute. Presence sampling has its own thread.

If a response reaches the requested limit or reports more rows than it returned, its time window is split in half. This continues down to one second. Returned rows are upserted by ID and window progress is committed in the same transaction. A one-second response that is still capped is stored as **truncated**, with a visible warning; it is never labelled complete. Parent windows are excluded from coverage after splitting, so they do not double-count their children.

The log coverage percentage means the fraction of the selected time range fetched without a reported row cap. It does **not** establish completeness: upstream retention, disabled categories, missing participants, delayed ingestion, permission filtering, or unreported truncation can still hide events. Empty successful responses count as fetched time, not evidence that nothing happened. Pending, capped, and failing windows are shown separately. A successful log job timestamp refers to its last successful request, not a finished backfill.

## Storage and recovery

SQLite uses WAL, foreign keys, a ten-second busy timeout, and full synchronous writes. The tables hold:

| Table | Purpose |
| --- | --- |
| `polls` | One attempt per UTC polling slot, response time, status, and a safe error category |
| `presence` | Unique player membership per successful sample |
| `players` | MonoSuite user ID, Steam ID when available, latest display name, first/last observed presence, history schedule |
| `events` | Latest fetched ban, blacklist, warning, kick, or note, keyed by kind and API ID |
| `logs`, `log_participants` | Latest raw gameplay records, original and normalized timestamps, and unique linked accounts |
| `log_windows` | Durable half-open second ranges, splits, retries, and disclosed truncation |
| `jobs`, `ban_seen` | Durable page offsets, retry times, scan progress, and duplicate-page detection |
| `settings` | Server, group, cadence, schema snapshot, and database metadata |

No pair table is materialized. Keeping the raw membership observations lets the viewer recompute weights for different time windows without committing to one interpretation. Identical roster intersections are combined before expanding pairs. Presence storage grows with polls × population, rather than polls × population squared. Gameplay logs add message text and participant memberships for every retained record; busy damage or system categories can dominate disk use. There is no automatic retention policy, so monitor growth before committing to a disk size. A minute cadence with an average of 50 players produces about 2.2 million membership rows in 30 days, plus indexes and history. A local synthetic check with a constant 50-player roster and 2,160,050 membership rows produced its 1,225 edges in about 1.5 seconds. Constant rosters are favorable; churn increases pair-computation work. Monitor disk space and measure your own query times.

A successful slot is immutable. Failed slots may be retried, and duplicate players in a response do not create duplicate membership. Poll data and history pages commit atomically; a failed page does not advance its offset. Events are upserted by ID, so repeated scans update edits and unbans without adding duplicate actions. An OS-held lock prevents two collector processes from using the same database. A process crash releases the lock automatically.

Presence, administrative history, and gameplay logs have separate workers, clients, and database connections. A history backfill cannot occupy the polling thread. Poll failures use bounded backoff with jitter; history failures retry later. All three workers periodically revalidate the live schema and keep retrying if it is temporarily unavailable. Keyboard interruption and SIGTERM stop gracefully. A disk failure may prevent recording the error itself; errors are also sent to standard output/error for the service manager to retain.

The viewer opens the database read-only, uses a consistent read transaction per request, rejects HTTP methods other than GET, and serves only its explicitly listed static assets. Graph queries allow at most 90 days, 500 nodes, and 5,000 displayed edges. The UI defaults to 300 nodes, ranked by observed time and then selected record count; an already selected player is retained. Hidden nodes and edges are disclosed. Clustering uses the node-limited graph before the edge display cap. Narrow the window when the cap matters. The graph directory searches the current view. The log explorer has a separate full collected directory, returning up to 30 search matches; refine the name or use an ID. Log results are paginated at 50 per page in the UI and at most 100 per API request. Player history is capped at 1,000 records per request, with a visible notice. A small graph-response cache lasts 30 seconds.

All raw observations are retained. There is no automatic pruning or remote deletion. `events` and `logs` store the latest fetched version, not an immutable revision log: records deleted or made inaccessible in MonoSuite are not automatically removed locally. Every record includes its last retrieval time. Treat old local text accordingly. Protect both the live database and its backups as staff data, including private messages. Each staff login in the supplied deployment can read all collected content; there is no separate PM permission.

## Schema and endpoint traps

The live dashboard schema was inspected on 2026-09-10. A full introspection snapshot is in `schema.json`. Each fixed query is validated against the live schema before collection, then again daily. You can check it explicitly:

```sh
python -m synapse check-schema
python -m synapse check-schema --output checked-schema.json
```

Schema validation proves field and argument compatibility, not endpoint semantics. The CLI README's documented gotchas are still needed:

- `server.bans` is active-only and capped at 100 rows. Synapse never uses it. It pages `group.bans(limit: 100, offset: ...)`, advancing by the actual returned count and checking the reported total. Bans are kept only for this server or when `serverGroupWide` is true. There is no `serverId` scalar on the current Ban type: scope comes from `server { id }`.
- Offset paging is not snapshot paging. Concurrent inserts or removals can move records between pages. ID upserts, repeated-page detection, durable offsets, and periodic rescans reduce the damage; they cannot promise an exact historical snapshot during continuous changes. The UI does not claim the backfill is complete before the scan finishes.
- Player and moderation timestamps are Unix milliseconds. Live checks established that both log output **and log query bounds use seconds**, contrary to the CLI documentation about log query inputs. A narrow millisecond window returned zero rows while the equivalent seconds window returned 33. Synapse validates integer log seconds, preserves them, and explicitly multiplies by 1,000 for local queries.
- Log time bounds were inclusive in live checks. Internal half-open windows `[start_s, end_s)` are sent as `startTimestamp=start_s` and `endTimestamp=end_s - 1`. The timestamp-and-ID pagination fields were silently ignored in repeated same-second checks; `scrollId` was absent and the reported total reached 10,000. The collector therefore uses adaptive time windows and does not trust that cursor or total as a guarantee of completeness.
- The blacklist API contains very distant expiry dates in real data. These remain their original millisecond values; Synapse does not divide them to make them look reasonable. Ban duration input is minutes, blacklist duration input is unverified, and neither is used here because no duration is ever submitted.
- Notes have group scope and no global paginated listing in the checked schema. They are fetched for players discovered through presence, gameplay logs, and collected history, using verified Steam IDs. Players without Steam IDs are retained but cannot be scheduled through that lookup. Players never discovered this way may be missing completely.
- Player notes/warnings/kicks and the server blacklist list expose no pagination or reliable completeness indicator in the checked schema. They are refreshed, but a successful read is not proof of all history. Ban history uses the paginated group endpoint instead of those player lists.
- A null list is an error, not an empty dataset. Missing authors are kept as unknown; missing subject IDs or indeterminate event scope stop the affected page for retry rather than producing a misleading edge.

The existing CLI helpers include some stale field selections. Synapse builds a small set of validated documents and sends them through `MonoSuiteClient.execute`. Its request hook permits only these exact documents and introspection. This blocks mutations, subscriptions, arbitrary GraphQL, and even side-effecting queries such as `logout` before transport. No API key or OAuth application is created by this project.

## Put it on a web server

For a VPS that also hosts unrelated applications, use the [shared-host deployment](deploy/host/README.md). It runs one host-wide proxy and separate application stacks.

For a single community with several staff users, start with a small Linux VM: roughly two CPU cores, 2–4 GB RAM, and an SSD volume is a reasonable starting estimate, not a load-tested requirement. Run the supplied Compose stack. Choose a region near your staff and MonoSuite. Budget for backups as well as the VM.

| Choice | Fit |
| --- | --- |
| Linux VM + Docker Compose | Recommended for this version. Collector, viewer, SQLite, and TLS live on one host. Simple operations and persistent storage. |
| Managed container host with a persistent disk | Also possible if it supports a continuous worker and both services accessing the same local volume. If volumes cannot be shared, run both processes under a supervisor in one container, or migrate storage to Postgres first. |
| Vercel | Useful for a separately hosted frontend or a future stateless API. This collector is continuous and this database is local, so the supplied application is not a drop-in Vercel deployment. |

Vercel's June 2026 announcement permits Node/Python functions up to 30 minutes on eligible plans, still not a weeks-long process. A Vercel version would use a separate persistent worker and Postgres, or replace the loop with secured scheduled invocations and a database lease per polling slot. That introduces schedule jitter and still requires credential refresh. I would only split the stack when deployment scale or an existing Vercel setup makes the extra parts worthwhile. See [Vercel's duration announcement](https://vercel.com/changelog/vercel-functions-can-now-run-up-to-30-minutes).

### Deploy the included stack

Install Docker Engine and the Compose plugin on the VM. Point a DNS name at it and allow inbound ports 80 and 443. The viewer's port is not published by Compose. Caddy handles HTTPS and protects both the HTML and every API route with staff authentication. Each authorized staff login can see all collected data in this instance.

Run these from `synapse/deploy` on the Linux host:

```sh
cp .env.example .env
mkdir -p secrets/collector
chmod 700 secrets
```

Set `SYNAPSE_DOMAIN` to your DNS name and `MONOSUITE_SERVER_ID` to the intended server in `.env`. Place the collector credential in `secrets/collector/monosuite.token`, outside version control. The container uses UID 10001, so arrange ownership and permissions accordingly:

```sh
sudo chown -R 10001:10001 secrets/collector
sudo chmod 700 secrets/collector
sudo chmod 600 secrets/collector/monosuite.token
```

Generate a distinct password hash for each staff member. This command prompts for a password rather than putting it in shell history:

```sh
docker run --rm -it caddy:2-alpine caddy hash-password
```

Create `secrets/staff.caddy` with one username and generated hash per line, for example `alice <generated-bcrypt-hash>`. Use the actual hash without angle brackets. The proxy does not accept plaintext passwords. Do not reuse a MonoSuite password. Caddy's [authentication documentation](https://caddyserver.com/docs/caddyfile/directives/basic_auth) explains the browser login prompt and hash format. The file must exist before starting the stack.

```sh
docker compose config --quiet
docker compose build
docker compose run --rm collector check-schema
docker compose up -d
docker compose ps
docker compose logs --tail 50 collector
```

The one-time initialization service creates the database. The collector runs continuously with restart-on-failure behavior; the viewer runs behind the proxy and opens SQLite with `mode=ro` and `query_only=ON`. Its data-directory mount permits SQLite to create WAL coordination files when no collector is connected; mounting that directory read-only prevents a fresh or stopped-collector database from opening on Linux. The viewer has no write API and no MonoSuite credentials, but its filesystem mount is not a separate write-protection boundary. Container logs rotate. Credentials are mounted only into the collector. The mounted credential *directory* permits atomic file replacement during rotation; the token provider rereads the file on refresh. Preserve UID 10001 access when replacing it. Never copy credentials into the image.

For additional staff, add another hash line and reload Caddy. Remove a line to revoke that staff login:

```sh
docker compose exec proxy caddy reload --config /etc/caddy/Caddyfile
```

There is no per-user role system or in-app logout in this Basic-auth deployment. Browsers may retain the login until closed. For a larger staff group, put an identity-aware reverse proxy with MFA in front instead. Keep its origin private and protect `/api/*` as well as `/`; authentication on the frontend alone is insufficient.

### Operations

`/api/health` reports 503 when the latest poll failed or is more than three polling intervals old. The endpoint is protected by the proxy like the rest of the site. History errors are separate and visible in the collection-status section. An unhealthy Docker healthcheck does not itself restart the container; monitor it. `restart: unless-stopped` handles a stopped/crashed process, while transient API failures are handled inside the collector.

Back up with SQLite's online backup operation, rather than copying only the main file while WAL writes are active:

```sh
docker compose exec collector python -m synapse backup --db /data/synapse.sqlite /data/backup-2026-09-10.sqlite
docker compose cp collector:/data/backup-2026-09-10.sqlite ./backup-2026-09-10.sqlite
```

Use a unique backup filename, encrypt backups, and copy them off the host on your own backup schedule. To restore, stop collector and viewer, preserve the old database and WAL files together, then install a verified backup as `/data/synapse.sqlite` with UID 10001 ownership and restart. Test restoration before relying on the backup. Named volumes survive container replacement; removing the volume removes the collected history. See [Docker volume lifecycle](https://docs.docker.com/engine/storage/volumes/).

Deploy code updates with `docker compose build` followed by `docker compose up -d`. Back up first. This version uses database schema version 2. `init` and `collect` add the new log tables to version 1 databases while preserving existing observations. Stop the viewer and collector and take a backup before upgrading. For a local database, run `python -m synapse init --db observations.sqlite --server YOUR_SERVER_ID` before serving it with the new viewer. Unknown versions are refused. Do not share SQLite over NFS or mount it on several machines. If you need several app replicas, move the database to Postgres and add a distributed collector lease.

### Other communities

People can self-host this repository with their own MonoSuite server and credentials. For separate communities, run separate Compose projects with separate domains, credentials, and volumes. On the same VM, use one shared reverse proxy to route domains rather than having multiple proxies bind ports 80/443. The current instance does not accept a tenant ID from a browser. A hosted service for unrelated organizations would need verified tenant membership, tenant-scoped storage and queries, separate encrypted credentials, per-tenant collection leases, and revocation handling. Those are not implemented here.

## MonoSuite applications and API keys

[MonoSuite sign-in](deploy/OAUTH.md) is implemented as an optional gateway around the viewer. It uses authorization codes with PKCE, verifies the identity at MonoSuite, and admits only explicitly approved account identifiers. It protects every data endpoint, including collection health and logs. Read the deployment guide for setup, session behavior, staff removal, and the verified provider URL gotcha.

Account approval grants access to all collected records, including PMs. The three requested OAuth read scopes do not create per-record restrictions in the local database. Atlas's approval list is independent of MonoSuite group roles; administrators must keep it current.

The collector still uses its own credential through `MonoSuiteClient`. OAuth login does not renew that credential. The API-key settings describe keys that act as their creator, fixed scopes, configurable expiry including no expiry, and a secret shown once. Compatibility of those keys or application tokens with the CLI's GraphQL realm/header remains unverified. Do not request wildcard or mutation-capable permissions to make a failed read work.

## Verification

```sh
python -m unittest discover -s tests -v
node --check synapse/static/app.js
```

The 37 tests exercise gameplay timestamp units, pair/group matching, sender-only records, command classification, same-second local pagination, literal searches, adaptive window splitting, truncation warnings, retries, replay, version 1 migration, read-only enforcement before transport, schema compatibility, token-file rotation, GraphQL authentication refresh, transactional/idempotent writes, failure gaps, time-window clipping, weighting, server scope, history edits, interrupted pagination, duplicate pages, collector locking, and HTTP validation.

Live smoke checks used the configured America server, which was offline with an empty roster at the time. They fetched multiple historical ban pages, notes, warnings, and 1,673 blacklist records. This verifies those reads and storage paths, not weeks of operation or complete history. A bounded live gameplay import stored 33 records and validated the new query against the live schema. The synthetic browser check covers a populated graph, player detail, gameplay layers, multi-person log filters, and pagination. The shared-host Docker deployment was built and its 37 tests passed on an Ubuntu 26.04 VPS. Public-domain TLS still needs verification after a domain is configured.
