# monosuite-cli

For the read-only co-presence and gameplay-log collector, player graph, and shared log explorer, see [Synapse player atlas](synapse/README.md). It includes a local demo and a Docker deployment for shared staff access.

A command line client for the [MonoSuite](https://monosuite.com) game server admin API, and the Python client class behind it.

Everything the dashboard does for bans, blacklists, notes, roles, players and logs, but in a terminal, so you can pipe it, script it, put it in a cron job or run it over SSH.

```
$ monosuite server players
                          14 players online
  Player          Steam id           Role      Play time  Online for  Flags
 ──────────────────────────────────────────────────────────────────────────────
  Gordon          76561198000000000  Citizen   120.4h     42 minutes  watched (possible alt)
  Alyx            76561198000000001  Rebel      88.1h     3 hours     -
  Barney          76561198000000002  Citizen     4.2h     11 minutes  1 active ban
```

## Requirements

- Python 3.8 or newer
- `click` and `rich`
- `browser-cookie3` for browser based sign in, optional but strongly recommended

## Install

```bash
git clone https://github.com/LazyHenrik/monosuite-cli.git
cd monosuite-cli
pip install -r requirements.txt
pip install browser-cookie3      # optional, for "auth login"
python monosuite_cli.py --help
```

It is a single file with no build step. Drop `monosuite_cli.py` wherever you like, or put it on your PATH:

```bash
chmod +x monosuite_cli.py
ln -s "$PWD/monosuite_cli.py" ~/.local/bin/monosuite
```

The rest of this README says `monosuite` where you may need `python monosuite_cli.py`.

## Signing in

```bash
monosuite auth login
```

MonoSuite keeps its session as a JWT in the `monosuite_token` cookie on `monosuite.com`. `auth login` looks for that cookie first, and if there is no live session it opens the login page, waits for you to finish with Steam or Discord, reads the cookie back out, checks it works and saves it.

```bash
monosuite auth login --provider steam    # skip the dashboard page, go straight to Steam
monosuite auth login --browser firefox   # only look in one browser
monosuite auth status                    # who am I, and when does this token die
```

Tokens last about a day. When one expires mid-command the client asks the browser for a fresh one and retries once, so a long running `logs follow` survives it as long as the browser session is still alive.

Two things worth knowing:

- Recent Chrome and Edge builds on Windows encrypt their cookie store in a way `browser-cookie3` cannot always read. If Chrome comes up empty, sign in with Firefox once, or use `auth set-token` and paste the JWT.
- On a headless box there is no browser to read. Use `auth set-token`, or set `MONOSUITE_TOKEN` in the environment.

## Pointing it at a server

Most commands need a server id. Find yours, then save it:

```bash
monosuite org list                     # your organizations and their groups
monosuite server list -g <group id>    # the servers in a group
monosuite config set server_id <server id>
```

Settings resolve in this order, first hit wins:

| | |
|---|---|
| 1 | a flag: `--token`, `--server-id`, `--group-id`, `--org-id` |
| 2 | the environment: `MONOSUITE_TOKEN`, `MONOSUITE_SERVER_ID`, `MONOSUITE_GROUP_ID` |
| 3 | the config file: `~/.monosuite_cli.json` |
| 4 | the browser session, for the token only |

The group id is worked out from the server when you leave it off, and the org id is worked out when you only belong to one.

## Everyday use

```bash
# who is on, and what is happening
monosuite server info
monosuite server players --flagged
monosuite activity
monosuite logs search -m "picked up" -n 40
monosuite logs follow --interval 10

# look someone up
monosuite player search gordon
monosuite player show 76561198000000000
monosuite player show 76561198000000000 --relations   # family sharing and likely alts
monosuite player history 76561198000000000

# moderation
monosuite ban add 76561198000000000 -r "RDM" -d 7d
monosuite ban list
monosuite ban remove 76561198000000000 -r "Appeal accepted"
monosuite watch add 76561198000000000 -r "Watch for alt behaviour"
monosuite note add 76561198000000000 "Warned about mic spam" --type Negative
monosuite blacklist add 76561198000000000 248 -r "Mic spam"      # 248 is a category id, see below
```

Durations are written the short way: `7d`, `12h`, `2w`, `1mo`, `1d12h`, or `perm`. A bare number counts as seconds.

The API counts ban length in **minutes**, not seconds. The CLI converts for you, and the confirmation prints both, so `-d 7d` shows `length 10,080 (the API counts minutes)`. If you ever call `addBan` yourself, that conversion is on you. A ban length over five years is refused outright, since it has never once been what somebody meant.

Run `monosuite --help` for the full command tree, and `monosuite <group> <command> --help` for any single one.

## From a log line to the person in it

Log lines carry their participants, ids included, so you never have to go and look someone up between reading a line and acting on it.

```bash
monosuite logs search -m "rdm" --pick
```

`--pick` lists everyone named in the results, opens the one you choose, and leaves you in their player view with the usual history in front of you. From there `w` watches, `n` adds a note and `b` bans, each with the same preview and confirmation as the standalone command, so `--dry-run` and `--yes` still behave as you expect. `q` goes back to the list, and a blank line leaves.

`player show <player> --pick` drops you straight into that same view without the log search.

If you would rather do it yourself, `--ids` puts the Steam id and the internal id next to each name:

```bash
monosuite logs search -m "rdm" --ids
monosuite activity --ids
```

Both ids also come through in `--json`, and `ban`, `watch` and `note` all accept an internal id wherever they accept a Steam id.

## Scripting it

Every command takes `--json` and prints exactly what the API returned, which makes `jq` the natural partner:

```bash
# Steam ids of everyone online who is on the watch list
monosuite server players --json \
  | jq -r '.[] | select(.watched | length > 0) | .steamId'

# how many bans this week
monosuite ban list --expired -n 1000 --json \
  | jq '[.[] | select(.createdAt > (now - 604800) * 1000)] | length'
```

Timestamps on bans, players and notes are unix **milliseconds**, while log lines use **seconds**, so a jq filter that works on one will silently return nothing on the other. `ban list` also shows active bans only unless you pass `--expired`, and the endpoint behind it returns at most 100 rows however high you set `-n`. For anything historical, page `group.bans(limit:, offset:)` through `raw` instead.

`--json` works before or after the command name, so both `monosuite --json ban list` and `monosuite ban list --json` are fine.

## Safety

Anything that changes state prints what it is about to do and asks first:

```
$ monosuite ban add 76561198000000000 -r "RDM" -d 7d
Ban a player
  player: Gordon (76561198000000000)
  reason: RDM
  duration: 1 week
  scope: this server
Go ahead? [y/N]:
```

- `--dry-run` prints that block and stops, sending nothing.
- `--yes` skips the prompt, for scripts and cron jobs.

Bans are always issued as the account behind the current token. There is no way to attribute one to somebody else.

Exit codes are `0` for success, `1` for an API or auth problem, `2` when the command line could not be parsed and `130` for Ctrl-C.

## As a library

`MonoSuiteClient` knows nothing about the CLI, so it imports cleanly into your own scripts:

```python
from monosuite_cli import MonoSuiteClient, token_from_browser

client = MonoSuiteClient(token_from_browser())

server = client.get_server(SERVER_ID)
print(server["name"], "online" if server["isOnline"] else "offline")

for player in client.get_online_players(SERVER_ID):
    if player.get("watched"):
        print(player["profile"]["username"], "is on the watch list")
```

Pass `token_provider=` to have it refresh itself:

```python
client = MonoSuiteClient(
    token_from_browser(),
    token_provider=token_from_browser,   # called when the token expires
)
```

Errors are typed: `AuthError`, `NotFoundError`, `ApiError` (with the raw GraphQL error list on `.errors`) and `TransportError`, all under `MonoSuiteError`.

## When the API changes

The queries here were checked against the live schema by introspection, but MonoSuite does change it. The symptom is a `VALIDATION_ERROR` naming an argument or a scalar that no longer exists, which is not much of a clue on its own. These help:

```bash
monosuite schema mutations           # everything the API accepts today, with argument types
monosuite schema mutations --search ban
monosuite schema queries
monosuite schema type Ban            # the fields on one type
monosuite raw 'query { organizations { id name } }'
```

`raw` sends anything this tool does not wrap yet, including variables:

```bash
monosuite raw @query.graphql --var limit=25
```

Introspection does not need a token, so `schema` works even when sign in is the thing that broke.

## Ban templates

`BAN_TEMPLATES` near the top of the file holds a punishment ladder: violation, class letter, duration and reason text.

```bash
monosuite templates list
monosuite templates show RDM
monosuite ban add 76561198000000000 -t RDM --class C
```

Using them is optional. The API knows nothing about them, they are staff policy expressed in a dict, so edit them to match your own rules. Anything you pass explicitly beats the template.

Classes marked `warn` are refused rather than silently issued as bans, because the API exposes no warning mutation. Issue those in game or in the dashboard.

## Known rough edges

- **Ban attribution.** `addBan` takes the account level id, and the API picks which of your account's profiles the ban is displayed under. If your account has more than one profile, the name in the ban history is not necessarily the one you signed in with, and nothing sent from the client changes that.
- **Length is in minutes.** `addBan` and `editBan` count in minutes. Sending seconds gives a ban sixty times too long, which is not obvious until a player asks why a one month ban runs to 2031. The CLI converts and shows the converted value before sending.
- **`editBan` restarts the clock.** The new expiry is measured from the moment of the edit, not from when the ban was created, so editing a three week old ban to two weeks gives two weeks from now. It is also why `editBan(length=1)` expires a ban on the spot.
- **Blacklist `value` is a category id**, not an IP or a hardware id. The numbers mean something in your own gamemode, and the same id can appear as a bare word in older entries. Check what a number is used for with `blacklist list` before sending it.
- **Blacklist length units are unverified.** The CLI converts to minutes on the assumption that blacklists count like bans do. Nobody has confirmed that.
- **`server.bans` returns at most 100 rows** while reporting the true total, and has no limit or offset argument. `group.bans(limit:, offset:)` is the paged view.
- **No warnings or kicks.** The API has queries for both but no mutations, so this tool can read them and not issue them.
- **Watch list.** There is no query for the whole list, so `watch list` shows watched players who are currently online. For an offline player, `player show` has the answer.
- **`player(value:)`** is typed as a plain String and the dashboard puts names through it as well as Steam ids. Only Steam ids are tested here.

## Contributing

Bug reports and pull requests are welcome. If the API has moved under you, please paste the output of `monosuite schema mutations --search <thing>` into the issue, since that says what the endpoint accepts today rather than what it accepted when this was written.

## License

MIT, see [LICENSE](LICENSE).

Not affiliated with MonoSuite.
