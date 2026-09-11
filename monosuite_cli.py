#!/usr/bin/env python3
"""
monosuite_cli.py
A command line framework for the MonoSuite API.

The file is one module with two halves:

  1. MonoSuiteClient, a plain Python class that talks to the API and knows
     nothing about the CLI, so you can import it from your own scripts:

         from monosuite_cli import MonoSuiteClient, token_from_browser

         client = MonoSuiteClient(token_from_browser())
         print(client.get_server(SERVER_ID)["name"])
         for player in client.get_online_players(SERVER_ID):
             print(player["profile"]["username"])

  2. A click command tree over that class. It prints rich tables, or JSON
     with --json.

A note on the schema
--------------------
Every query and mutation below was checked against the live schema by
introspection. The API does move, and when it does the symptom is a
VALIDATION_ERROR naming an argument or a scalar that no longer exists, which
is not much of a clue on its own. Three commands help:

    schema mutations      what the API accepts today, with argument types
    schema type Ban       the fields on a single type
    raw '<query>'         send something this tool does not wrap yet

Install
-------
    pip install click rich
    pip install browser-cookie3        # for browser login, strongly recommended

Quick start
-----------
    python monosuite_cli.py auth login          # opens your browser, waits, saves
    python monosuite_cli.py org list            # find your org
    python monosuite_cli.py server list -g GROUP_ID
    python monosuite_cli.py config set server_id SERVER_ID
    python monosuite_cli.py server players
    python monosuite_cli.py player show 76561198000000000
    python monosuite_cli.py ban add 76561198000000000 -r "RDM" -d 7d

Where settings come from
------------------------
Every setting resolves the same way, first hit wins:

    1. a command line flag        --token / --server-id / --group-id
    2. an environment variable    MONOSUITE_TOKEN / MONOSUITE_SERVER_ID /
                                  MONOSUITE_GROUP_ID
    3. the config file            ~/.monosuite_cli.json
    4. token only: the           the monosuite_token cookie on monosuite.com
       browser session

The token is a short lived JWT (about a day). The client reads its exp claim,
and if a request comes back 401 or 403 it asks for a fresh token and retries
once. A long running "logs follow" therefore survives a token expiring under
it, as long as the browser session behind it is still alive.

Safety
------
Anything that changes state asks for confirmation first. Pass --yes to skip the
prompt in scripts, or --dry-run to see the mutation and its variables without
sending anything.

Exit codes
----------
    0    success
    1    an API, auth or usage problem we recognised
    2    click could not parse the command line
    130  you pressed Ctrl-C
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

try:
    import click
except ImportError:
    sys.stderr.write("This tool needs click. Install it with: pip install click rich\n")
    raise SystemExit(1)

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
except ImportError:
    sys.stderr.write("This tool needs rich. Install it with: pip install click rich\n")
    raise SystemExit(1)


__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_URL = "https://api.monosuite.com/v2/api/graphql"

# The API picks which schema to serve from this header. Leave it out and every
# request comes back 400 MISSING_REALM, which reads like a broken endpoint
# rather than a missing header.
AUTH_REALM = "dashboard"

COOKIE_NAME = "monosuite_token"
COOKIE_DOMAIN = "monosuite.com"
LOGIN_URL = "https://monosuite.com/login"
SCREENSHOT_CDN = "https://cdn.monosuite.com/screenshots"

CONFIG_FILE = Path.home() / ".monosuite_cli.json"

ENV_TOKEN = "MONOSUITE_TOKEN"
ENV_SERVER = "MONOSUITE_SERVER_ID"
ENV_GROUP = "MONOSUITE_GROUP_ID"

# Keys "config set" accepts. Anything else is almost certainly a typo.
CONFIG_KEYS = ("token", "server_id", "group_id", "org_id", "browser", "timeout")

BROWSERS = ("chrome", "edge", "brave", "firefox", "opera")
LOGIN_PROVIDERS = ("steam", "discord", "google")

# Note.type and createNote(noteType:) are plain strings on the live schema.
# These are the three the dashboard uses.
NOTE_TYPES = ("Positive", "Neutral", "Negative")

# The punishment ladder: violation -> class letter -> (seconds, action, reason).
# 0 seconds means permanent. This is staff policy, not something the API
# enforces, so edit it freely when the rules change.
#
# Classes marked "warn" have no API equivalent, since there is no warning
# mutation, and "ban add --template" refuses them rather than quietly issuing a
# ban instead.
BAN_TEMPLATES: Dict[str, Dict[str, tuple]] = {
    "Text Chat Misuse": {
        "A": (0, "warn", "Text Chat Misuse | Appeal @ Discord.gg/hl2rp"),
        "B": (86400, "ban", "Text Chat Misuse | Appeal @ Discord.gg/hl2rp"),
        "C": (604800, "ban", "Text Chat Misuse | Appeal @ Discord.gg/hl2rp"),
        "D": (2592000, "ban", "Text Chat Misuse | Appeal @ Discord.gg/hl2rp"),
    },
    "NLR": {
        "A": (0, "warn", "New Life Rule | Appeal @ Discord.gg/hl2rp"),
        "B": (86400, "ban", "NLR | Appeal @ Discord.gg/hl2rp"),
        "C": (604800, "ban", "NLR | Appeal @ Discord.gg/hl2rp"),
        "D": (2592000, "ban", "NLR | Appeal @ Discord.gg/hl2rp"),
    },
    "Breaking Character": {
        "A": (0, "warn", "Breaking Character | Appeal @ Discord.gg/hl2rp"),
        "B": (86400, "ban", "Breaking Character | Appeal @ Discord.gg/hl2rp"),
        "C": (604800, "ban", "Breaking Character | Appeal @ Discord.gg/hl2rp"),
        "D": (2592000, "ban", "Breaking Character | Appeal @ Discord.gg/hl2rp"),
    },
    "NITRP": {
        "A": (0, "warn", "No Intent to Roleplay | Appeal @ Discord.gg/hl2rp"),
        "B": (2592000, "ban", "NITRP | Appeal @ Discord.gg/hl2rp"),
        "C": (7776000, "ban", "NITRP | Appeal @ Discord.gg/hl2rp"),
        "D": (0, "ban", "NITRP | Appeal @ Discord.gg/hl2rp"),
    },
    "Invalid Character Name": {
        "A": (0, "warn", "Invalid Character Name - Name Change Required | Appeal @ Discord.gg/hl2rp"),
        "B": (86400, "ban", "Invalid Character Name - Name Change Required | Appeal @ Discord.gg/hl2rp"),
        "C": (604800, "ban", "Invalid Character Name - Name Change Required | Appeal @ Discord.gg/hl2rp"),
        "D": (2592000, "ban", "Invalid Character Name - Name Change Required | Appeal @ Discord.gg/hl2rp"),
    },
    "RDM": {
        "A": (0, "warn", "Random Deathmatch | Appeal @ Discord.gg/hl2rp"),
        "B": (86400, "ban", "RDM | Appeal @ Discord.gg/hl2rp"),
        "C": (604800, "ban", "RDM | Appeal @ Discord.gg/hl2rp"),
        "D": (2592000, "ban", "RDM | Appeal @ Discord.gg/hl2rp"),
    },
    "Metagaming": {
        "A": (1209600, "ban", "Metagaming | Appeal @ Discord.gg/hl2rp"),
        "B": (2592000, "ban", "Metagaming | Appeal @ Discord.gg/hl2rp"),
        "C": (7776000, "ban", "Metagaming | Appeal @ Discord.gg/hl2rp"),
        "D": (0, "ban", "Metagaming | Appeal @ Discord.gg/hl2rp"),
    },
    "DTAC": {
        "A": (172800, "ban", "Disconnecting to Avoid Consequences | Appeal @ Discord.gg/hl2rp"),
        "B": (2592000, "ban", "DTAC | Appeal @ Discord.gg/hl2rp"),
        "C": (7776000, "ban", "DTAC | Appeal @ Discord.gg/hl2rp"),
        "D": (0, "ban", "DTAC | Appeal @ Discord.gg/hl2rp"),
    },
    "ERP": {
        "A": (1209600, "ban", "Erotic Roleplay | Appeal @ Discord.gg/hl2rp"),
        "B": (2592000, "ban", "ERP | Appeal @ Discord.gg/hl2rp"),
        "C": (31536000, "ban", "ERP | Appeal @ Discord.gg/hl2rp"),
        "D": (0, "ban", "ERP | Appeal @ Discord.gg/hl2rp"),
    },
    "Invalid Boosting": {
        "A": (0, "warn", "Invalid Boosting | Appeal @ Discord.gg/hl2rp"),
        "B": (86400, "ban", "Invalid Boosting | Appeal @ Discord.gg/hl2rp"),
        "C": (604800, "ban", "Invalid Boosting | Appeal @ Discord.gg/hl2rp"),
        "D": (2592000, "ban", "Invalid Boosting | Appeal @ Discord.gg/hl2rp"),
    },
    "Backseat Moderating": {
        "A": (0, "warn", "Backseat Moderating | Appeal @ Discord.gg/hl2rp"),
        "B": (86400, "ban", "Backseat Moderating | Appeal @ Discord.gg/hl2rp"),
        "C": (604800, "ban", "Backseat Moderating | Appeal @ Discord.gg/hl2rp"),
        "D": (2592000, "ban", "Backseat Moderating | Appeal @ Discord.gg/hl2rp"),
    },
    "Disorderly Behaviour": {
        "A": (0, "warn", "Disorderly Behaviour | Appeal @ Discord.gg/hl2rp"),
        "B": (86400, "ban", "Disorderly Behaviour | Appeal @ Discord.gg/hl2rp"),
        "C": (604800, "ban", "Disorderly Behaviour | Appeal @ Discord.gg/hl2rp"),
        "D": (2592000, "ban", "Disorderly Behaviour | Appeal @ Discord.gg/hl2rp"),
    },
    "Harassment": {
        "A": (604800, "ban", "Harassment | Appeal @ Discord.gg/hl2rp"),
        "B": (2592000, "ban", "Harassment | Appeal @ Discord.gg/hl2rp"),
        "C": (31536000, "ban", "Harassment | Appeal @ Discord.gg/hl2rp"),
        "D": (0, "ban", "Harassment | Appeal @ Discord.gg/hl2rp"),
    },
    "Exploiting": {
        "A": (0, "ban", "Exploiting | Appeal @ Discord.gg/hl2rp"),
        "B": (0, "ban", "Exploiting | Appeal @ Discord.gg/hl2rp"),
        "C": (0, "ban", "Exploiting | Appeal @ Discord.gg/hl2rp"),
        "D": (0, "ban", "Exploiting | Appeal @ Discord.gg/hl2rp"),
    },
    "Cheating": {
        "A": (0, "ban", "Cheating | Appeal @ Discord.gg/hl2rp"),
        "B": (0, "ban", "Cheating | Appeal @ Discord.gg/hl2rp"),
        "C": (0, "ban", "Cheating | Appeal @ Discord.gg/hl2rp"),
        "D": (0, "ban", "Cheating | Appeal @ Discord.gg/hl2rp"),
    },
    "Real World Trading": {
        "A": (2592000, "ban", "Real World Trading | Appeal @ Discord.gg/hl2rp"),
        "B": (15552000, "ban", "Real World Trading | Appeal @ Discord.gg/hl2rp"),
        "C": (31536000, "ban", "Real World Trading | Appeal @ Discord.gg/hl2rp"),
        "D": (0, "ban", "Real World Trading | Appeal @ Discord.gg/hl2rp"),
    },
    "Ban Evasion": {
        "A": (2592000, "ban", "Ban Evasion | Appeal @ Discord.gg/hl2rp"),
        "B": (15552000, "ban", "Ban Evasion | Appeal @ Discord.gg/hl2rp"),
        "C": (31536000, "ban", "Ban Evasion | Appeal @ Discord.gg/hl2rp"),
        "D": (0, "ban", "Ban Evasion | Appeal @ Discord.gg/hl2rp"),
    },
    "Alt Account": {
        "A": (2592000, "ban", "Alt Account | Appeal @ Discord.gg/hl2rp"),
        "B": (15552000, "ban", "Alt Account | Appeal @ Discord.gg/hl2rp"),
        "C": (31536000, "ban", "Alt Account | Appeal @ Discord.gg/hl2rp"),
        "D": (0, "ban", "Alt Account | Appeal @ Discord.gg/hl2rp"),
    },
    "Suspicious Account": {
        "A": (604800, "ban", "Suspicious Account | Appeal @ Discord.gg/hl2rp"),
        "B": (2592000, "ban", "Suspicious Account | Appeal @ Discord.gg/hl2rp"),
        "C": (7776000, "ban", "Suspicious Account | Appeal @ Discord.gg/hl2rp"),
        "D": (0, "ban", "Suspicious Account | Appeal @ Discord.gg/hl2rp"),
    },
    "Abuse of Ticketing System": {
        "A": (0, "warn", "Abuse of Ticketing System | Appeal @ Discord.gg/hl2rp"),
        "B": (86400, "ban", "Abuse of Ticketing System | Appeal @ Discord.gg/hl2rp"),
        "C": (604800, "ban", "Abuse of Ticketing System | Appeal @ Discord.gg/hl2rp"),
        "D": (2592000, "ban", "Abuse of Ticketing System | Appeal @ Discord.gg/hl2rp"),
    },
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MonoSuiteError(Exception):
    """Base class for every problem this module raises on purpose."""


class AuthError(MonoSuiteError):
    """No usable token, or the API refused the one we had."""


class NotFoundError(MonoSuiteError):
    """The server, player, ban or note you asked for does not exist."""


class ApiError(MonoSuiteError):
    """The API answered with a GraphQL error list.

    The full list stays on the exception as .errors, so a caller can inspect
    paths and extensions instead of parsing the message we built.
    """

    def __init__(self, message: str, errors: Optional[List[dict]] = None):
        super().__init__(message)
        self.errors = errors or []


class TransportError(MonoSuiteError):
    """The request never landed, or came back as something other than JSON."""


# ---------------------------------------------------------------------------
# Tokens and browser login
# ---------------------------------------------------------------------------

def decode_jwt_claims(token: str) -> Dict[str, Any]:
    """Read the claims out of a JWT without verifying the signature.

    We only look at this locally to find out when the token dies, so there is
    nothing to verify against. A token we cannot parse gives back an empty
    dict rather than raising, because a hand pasted token is still worth a try.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # restore base64url padding
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:
        return {}


def token_expiry(token: str) -> Optional[int]:
    """Return the exp claim as unix seconds, or None when there is no exp."""
    exp = decode_jwt_claims(token or "").get("exp")
    try:
        return int(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


def token_is_alive(token: str, leeway: int = 60) -> bool:
    """True when a token is either unexpired or has no expiry we can read."""
    if not token:
        return False
    exp = token_expiry(token)
    if exp is None:
        return True
    return time.time() < (exp - leeway)


def token_from_browser(browser: Optional[str] = None) -> Optional[str]:
    """Read the monosuite_token cookie out of a logged in browser session.

    Pass a browser name to look in one place only, or leave it out to check
    them all and keep whichever token expires latest, which is the freshest
    session.

    Raises ImportError when browser-cookie3 is missing, because that is worth
    saying out loud rather than pretending there was no cookie.

    Windows note: recent Chrome and Edge builds encrypt their cookie store in a
    way browser-cookie3 cannot always read. If Chrome comes up empty, sign in
    with Firefox once, or paste the token with "auth set-token".
    """
    try:
        import browser_cookie3  # type: ignore
    except ImportError:
        raise ImportError(
            "Reading the browser session needs the browser-cookie3 package.\n"
            "Install it with: pip install browser-cookie3"
        )

    names = list(BROWSERS)
    if browser:
        wanted = browser.strip().lower()
        if wanted not in BROWSERS:
            raise MonoSuiteError(
                f"Unknown browser {browser!r}. Pick one of: {', '.join(BROWSERS)}"
            )
        names = [wanted]

    best_token: Optional[str] = None
    best_exp = -1
    for name in names:
        loader = getattr(browser_cookie3, name, None)
        if loader is None:
            continue
        try:
            jar = loader(domain_name=COOKIE_DOMAIN)
        except Exception:
            # A browser that is not installed, or a locked cookie database.
            # Neither is worth stopping for while other browsers remain.
            continue
        for cookie in jar:
            if cookie.name != COOKIE_NAME or not cookie.value:
                continue
            exp = token_expiry(cookie.value) or 0
            if exp > best_exp:
                best_exp = exp
                best_token = cookie.value
    return best_token


def normalise_auth_url(url: str) -> str:
    """Tidy the OAuth start URL the API hands back.

    The login query hands back http://auth.monosuite.com:443/auth/steam, an
    http scheme sitting on the https port. Swap the scheme and drop the port
    before giving it to a browser.
    """
    if not url:
        return url
    if url.startswith("http://") and ":443" in url:
        return "https://" + url[len("http://"):].replace(":443", "", 1)
    return url


def open_in_browser(url: str) -> bool:
    """Open a URL in the default browser, quietly reporting whether it worked."""
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def wait_for_browser_token(
    browser: Optional[str] = None,
    timeout: int = 180,
    interval: float = 3.0,
    ignore: Optional[str] = None,
    on_tick: Optional[Callable[[int], None]] = None,
) -> Optional[str]:
    """Poll the browser cookie jar until a fresh token turns up, or time runs out.

    This is the second half of the login dance: we send you to the login page,
    you sign in with Steam or Discord, the dashboard drops its cookie, and this
    loop notices. Pass the old token as "ignore" so a stale cookie sitting in
    the jar does not count as success.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            token = token_from_browser(browser)
        except ImportError:
            raise
        except Exception:
            token = None
        if token and token != ignore and token_is_alive(token):
            return token
        if on_tick:
            on_tick(max(0, int(deadline - time.time())))
        time.sleep(interval)
    return None


# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------

class Config:
    """A small JSON file in your home directory holding the boring settings.

    None of it is required. The CLI runs fine on flags and environment
    variables alone, the file just saves you retyping a server UUID.
    """

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = Path(path)
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> "Config":
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except Exception:
            # A corrupt config should never stop a command from running.
            self.data = {}
        return self

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        # The file can hold a token, so keep it to the owner where the OS allows.
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        value = self.data.get(key, default)
        return default if value in ("", None) else value

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def unset(self, key: str) -> None:
        self.data.pop(key, None)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

DURATION_UNITS = {
    "s": 1,
    "sec": 1,
    "m": 60,
    "min": 60,
    "h": 3600,
    "hr": 3600,
    "d": 86400,
    "w": 604800,
    "mo": 2592000,   # 30 flat days, not a calendar month
    "y": 31536000,   # 365 days
}


def parse_duration(text: Any) -> float:
    """Turn 7d, 12h, 2w, 1mo, 1d12h or perm into seconds.

    Zero means permanent, which is how the API reads a length of 0. A bare
    number counts as seconds, so "3600" still works.
    """
    if text is None:
        return 0
    raw = str(text).strip().lower().replace(" ", "")
    if raw in ("", "0", "perm", "permanent", "forever", "never"):
        return 0

    total = 0.0
    number = ""
    unit = ""
    matched = False

    def flush() -> None:
        nonlocal total, number, unit, matched
        if not number:
            return
        seconds_per = DURATION_UNITS.get(unit or "s")
        if seconds_per is None:
            raise MonoSuiteError(
                f"I do not know the duration unit {unit!r}. "
                f"Use one of: {', '.join(sorted(set(DURATION_UNITS)))}, or perm."
            )
        total += float(number) * seconds_per
        matched = True
        number = ""
        unit = ""

    for char in raw:
        if char.isdigit() or char == ".":
            if unit:
                flush()
            number += char
        else:
            unit += char
    flush()

    if not matched:
        raise MonoSuiteError(f"I could not read {text!r} as a duration.")
    return total


def format_duration(seconds: Optional[float]) -> str:
    """Render a length in seconds the way a person would say it."""
    if seconds is None:
        return "-"
    seconds = float(seconds)
    if seconds <= 0:
        return "permanent"
    for size, name in (
        (31536000, "year"),
        (2592000, "month"),
        (604800, "week"),
        (86400, "day"),
        (3600, "hour"),
        (60, "minute"),
    ):
        if seconds >= size:
            value = seconds / size
            rendered = f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"
            return f"{rendered} {name}" + ("" if rendered == "1" else "s")
    return f"{seconds:.0f} seconds"


def parse_time(value: Any) -> Optional[datetime]:
    """Read the timestamp shapes the API hands back.

    Most fields are the Long scalar holding unix milliseconds, a few are ISO
    strings, and unix seconds show up here and there, so all three are handled.
    """
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return parse_time(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_time(value: Any, with_seconds: bool = False) -> str:
    """Format a timestamp in local time, which is what you compare against."""
    parsed = parse_time(value)
    if parsed is None:
        return "-"
    pattern = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    return parsed.astimezone().strftime(pattern)


def format_relative(value: Any) -> str:
    """Say how long ago, or how far ahead, a timestamp is."""
    parsed = parse_time(value)
    if parsed is None:
        return "-"
    delta = (datetime.now(timezone.utc) - parsed).total_seconds()
    if delta < 0:
        return f"in {format_duration(-delta)}"
    return f"{format_duration(delta)} ago" if delta >= 60 else "just now"


def format_playtime(seconds: Any) -> str:
    """Play time arrives in seconds. Hours is what people actually want."""
    try:
        value = float(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    hours = value / 3600.0
    return f"{hours:,.0f}h" if hours >= 100 else f"{hours:,.1f}h"


# The API measures punishment length in MINUTES. Everything else in this file
# works in seconds, because that is what people mean when they write 7d, so the
# conversion happens here and nowhere else. Getting this wrong is expensive: a
# length sent in seconds produced a ban 60 times longer than intended, and the
# player was the one who found out.
SECONDS_PER_API_UNIT = 60

# A timed ban longer than this is almost certainly a units mistake rather than
# somebody's actual intention. Permanent bans are a separate thing and are sent
# as a length of 0, so refusing here costs nothing real.
MAX_BAN_SECONDS = 5 * 365 * 86400


def seconds_to_api_length(seconds: float) -> float:
    """Convert a duration in seconds to the minutes the API wants.

    Zero stays zero, since that is how both sides spell permanent. Sub minute
    lengths survive as a fraction, which the API accepts because length is a
    Float, though it is not worth relying on for anything shorter than a minute.
    """
    value = float(seconds or 0)
    return 0.0 if value <= 0 else value / SECONDS_PER_API_UNIT


def check_ban_length(seconds: float) -> float:
    """Reject a ban length that only makes sense as a units bug."""
    value = float(seconds or 0)
    if value > MAX_BAN_SECONDS:
        raise MonoSuiteError(
            f"A ban of {format_duration(value)} is longer than this tool will send. "
            f"If you mean it to never expire, use a permanent ban (-d perm) instead."
        )
    return value


def is_ban_active(ban: dict) -> bool:
    """Active means not lifted and either permanent or still in the future."""
    if not isinstance(ban, dict):
        return False
    if ban.get("unbannedAt"):
        return False
    expire = ban.get("expire")
    if not expire:
        return True
    parsed = parse_time(expire)
    if parsed is None:
        return True  # cannot read it, so assume it still bites
    return parsed > datetime.now(timezone.utc)


def profile_name(record: Any, default: str = "-") -> str:
    """Pull a username out of a User, a UserProfile, or something with a user."""
    if not isinstance(record, dict):
        return default
    if record.get("username"):
        return str(record["username"])
    profile = record.get("profile")
    if isinstance(profile, dict) and profile.get("username"):
        return str(profile["username"])
    if record.get("name"):
        return str(record["name"])
    return default


def admin_name(record: Any, default: str = "-") -> str:
    """The admin behind a punishment record, or a dash when it was the system."""
    if not isinstance(record, dict):
        return default
    return profile_name(record.get("admin"), default)


def user_name(record: Any, default: str = "-") -> str:
    """The player a punishment record points at."""
    if not isinstance(record, dict):
        return default
    return profile_name(record.get("user"), default)


def cell(value: Any) -> str:
    """Table cell text, with a dash for anything empty.

    Nothing is trimmed here on purpose. rich folds a long value onto more
    lines, which beats hiding the end of a ban reason behind an ellipsis.
    """
    if value is None:
        return "-"
    text = " ".join(str(value).split())
    return text or "-"


def shorten(text: Any, width: int = 60) -> str:
    """Trim long free text.

    Only for places where one long value would wreck a layout, such as the
    flags cell in the online players table. Table content goes through cell().
    """
    if text is None:
        return "-"
    value = " ".join(str(text).split())
    if len(value) <= width:
        return value or "-"
    return value[: max(1, width - 3)] + "..."


def steam_id_of(player: dict) -> str:
    """Find a Steam id on a player record, however it is presented."""
    if not isinstance(player, dict):
        return "-"
    if player.get("steamId"):
        return str(player["steamId"])
    for identity in player.get("identities") or []:
        if str(identity.get("platformType", "")).lower().startswith("steam"):
            return str(identity.get("platformId"))
    return "-"


def steam_profile_url(player: dict) -> Optional[str]:
    """A Steam profile link, either the given one or one built from the id."""
    profile = (player or {}).get("profile") or {}
    if profile.get("profileUrl"):
        return str(profile["profileUrl"])
    steam_id = steam_id_of(player or {})
    return f"https://steamcommunity.com/profiles/{steam_id}" if steam_id != "-" else None


def looks_like_uuid(value: str) -> bool:
    """Crude but reliable for the two id shapes we deal with here.

    Steam ids are 17 digits with no dashes, internal ids are UUIDs with dashes.
    """
    return "-" in str(value)


# ---------------------------------------------------------------------------
# GraphQL fragments
# ---------------------------------------------------------------------------
# Selection sets get repeated across queries, so they live here once. They are
# plain strings spliced into the queries below, nothing clever.

F_PROFILE = "profile { id username avatar profileUrl }"
F_ADMIN = "admin { id " + F_PROFILE + " }"
F_BAN = f"""
    id reason type expire createdAt editedAt serverGroupWide
    unbanReason unbannedAt serverId userId adminId
    user {{ id steamId {F_PROFILE} }}
    {F_ADMIN}
    unbannedBy {{ id {F_PROFILE} }}
"""
F_BLACKLIST = f"""
    id value type reason expire createdAt serverId userId
    user {{ id steamId {F_PROFILE} }}
    {F_ADMIN}
"""
F_WARNING = f"""
    id reason points active createdAt serverId
    user {{ id steamId {F_PROFILE} }}
    {F_ADMIN}
"""
F_KICK = f"""
    id reason createdAt serverId
    user {{ id steamId {F_PROFILE} }}
    {F_ADMIN}
"""
F_NOTE = f"id content type createdAt updatedAt userId {F_ADMIN}"
F_LOG = "id timestamp category message participants { id steamId " + F_PROFILE + " }"
F_ROLE = """
    id name color immunity aliases banTime inheritsId discordRoleId
    createdAt updatedAt
"""


# ---------------------------------------------------------------------------
# The API client
# ---------------------------------------------------------------------------

class MonoSuiteClient:
    """Talks to the MonoSuite GraphQL API.

    The API is one GraphQL endpoint. Auth is a JWT in the Authorization header
    with no "Bearer " prefix, plus the x-authorization-realm header that picks
    the dashboard schema.

    Parameters
    ----------
    token:
        The JWT. May be empty if you pass a token_provider that can find one.
    token_provider:
        A zero argument callable returning a fresh token, normally a lambda
        around token_from_browser. It gets called when the token we hold has
        expired, and once more if a request comes back 401 or 403.
    url:
        Override the endpoint, handy for pointing at a staging deployment.
    timeout:
        Per request timeout in seconds.
    on_request:
        Optional callback taking (query, variables). The CLI uses it for
        --verbose, and it is a reasonable hook for logging or metrics.
    """

    def __init__(
        self,
        token: str = "",
        token_provider: Optional[Callable[[], Optional[str]]] = None,
        url: str = API_URL,
        timeout: int = 30,
        on_request: Optional[Callable[[str, Optional[dict]], None]] = None,
    ):
        self.url = url
        self.timeout = timeout
        self.token_provider = token_provider
        self.on_request = on_request
        self._token = token or ""
        self._expiry = token_expiry(self._token) if self._token else None
        self._cache: Dict[str, Any] = {}

    # -- token bookkeeping --------------------------------------------------

    @property
    def token(self) -> str:
        return self._token

    def set_token(self, token: str) -> None:
        """Swap in a new token and re-read its expiry."""
        self._token = token or ""
        self._expiry = token_expiry(self._token) if self._token else None
        self._cache.clear()

    @property
    def expires_at(self) -> Optional[datetime]:
        """When the token dies, or None if it carried no exp claim."""
        return datetime.fromtimestamp(self._expiry, tz=timezone.utc) if self._expiry else None

    def seconds_left(self) -> Optional[float]:
        """Seconds until expiry, negative once past, None when unknown."""
        return None if self._expiry is None else self._expiry - time.time()

    def is_expired(self, leeway: int = 60) -> bool:
        """True when the token is known to be dead or nearly so.

        A token with no readable exp claim counts as valid and the API gets to
        decide. Hand pasted tokens land there.
        """
        if self._expiry is None:
            return False
        return time.time() >= (self._expiry - leeway)

    def refresh_token(self) -> bool:
        """Ask the provider for a fresh token. True when we got a new one."""
        if not self.token_provider:
            return False
        try:
            new_token = self.token_provider()
        except Exception:
            return False
        if new_token and new_token != self._token:
            self.set_token(new_token)
            return True
        return False

    # -- transport ----------------------------------------------------------

    def _post(self, body: bytes) -> Dict[str, Any]:
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._token,
                "x-authorization-realm": AUTH_REALM,
                "User-Agent": f"monosuite-cli/{__version__}",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = response.read().decode("utf-8")
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            raise TransportError(
                f"The API replied with something that is not JSON: {payload[:200]}"
            )

    def raw(self, query: str, variables: Optional[dict] = None) -> Dict[str, Any]:
        """Send a query and hand back the whole envelope, errors included.

        Use this when you want to inspect the errors yourself. Everything else
        in this class goes through execute(), which raises instead.
        """
        if self.on_request:
            self.on_request(query, variables)

        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        body = json.dumps(payload).encode("utf-8")

        # If we already know the token is stale, renew before spending a trip.
        if self.is_expired():
            self.refresh_token()

        try:
            return self._post(body)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            if error.code in (401, 403) and self.refresh_token():
                try:
                    return self._post(body)
                except urllib.error.HTTPError as retry:
                    body_text = retry.read().decode("utf-8", "replace")
                    raise AuthError(
                        f"HTTP {retry.code} even after refreshing the token: {body_text[:300]}"
                    )
                except urllib.error.URLError as retry:
                    raise TransportError(f"Could not reach the API: {retry.reason}")
            if error.code in (401, 403):
                raise AuthError(
                    f"HTTP {error.code}: the API rejected this token. "
                    f"Run 'auth login' for a fresh session. Details: {detail[:300]}"
                )
            raise TransportError(f"HTTP {error.code}: {detail[:500]}")
        except urllib.error.URLError as error:
            raise TransportError(f"Could not reach the API: {error.reason}")

    def execute(self, query: str, variables: Optional[dict] = None) -> Dict[str, Any]:
        """Send a query and return the data block, raising on any error."""
        envelope = self.raw(query, variables)
        errors = envelope.get("errors")
        if errors:
            message = self._describe_errors(errors)
            if self._is_auth_error(errors):
                raise AuthError(
                    f"{message}. Run 'auth login' to sign in again."
                )
            raise ApiError(message, errors)
        data = envelope.get("data")
        if data is None:
            raise ApiError("The API answered without a data block and without an error.")
        return data

    @staticmethod
    def _is_auth_error(errors: Any) -> bool:
        """Spot the "Not logged in" shape so we can say something useful."""
        for error in errors if isinstance(errors, list) else []:
            if not isinstance(error, dict):
                continue
            code = (error.get("extensions") or {}).get("errorCode")
            if code == "UNAUTHORIZED":
                return True
            if str(error.get("message", "")).lower().startswith("not logged in"):
                return True
        return False

    @staticmethod
    def _describe_errors(errors: Any) -> str:
        """Turn a GraphQL error list into one sentence worth showing a person.

        The API sometimes returns an error with an empty message and only a
        path, which in practice means a permission problem, so say that rather
        than printing nothing at all.
        """
        if not isinstance(errors, list) or not errors:
            return "The API returned an unknown error."
        messages = []
        for error in errors:
            if not isinstance(error, dict):
                messages.append(str(error))
                continue
            message = error.get("message")
            if message:
                messages.append(str(message))
                continue
            path = error.get("path")
            if path:
                joined = ".".join(str(part) for part in path)
                messages.append(
                    f"Permission denied or invalid operation at '{joined}'. "
                    f"Check your permissions on this server."
                )
            else:
                messages.append("Permission denied or invalid operation.")
        return " | ".join(messages)

    # -- unwrapping helpers -------------------------------------------------

    def _server(self, server_id: str, query: str, variables: Optional[dict] = None) -> Dict[str, Any]:
        data = self.execute(query, variables or {"serverId": server_id})
        server = data.get("server")
        if not server:
            raise NotFoundError(
                f"No server with id {server_id}, or your account cannot see it."
            )
        return server

    def _group(self, group_id: str, query: str, variables: Optional[dict] = None) -> Dict[str, Any]:
        data = self.execute(query, variables or {"groupId": group_id})
        group = data.get("group")
        if not group:
            raise NotFoundError(
                f"No server group with id {group_id}, or your account cannot see it."
            )
        return group

    def _player(self, server_id: str, value: str, query: str) -> Dict[str, Any]:
        server = self._server(server_id, query, {"serverId": server_id, "value": value})
        player = server.get("player")
        if not player:
            raise NotFoundError(f"No player matching {value!r} on this server.")
        return player

    # -- account ------------------------------------------------------------

    def get_self(self) -> Dict[str, Any]:
        """The account behind the token: name, email and linked providers."""
        query = """
        query Self {
            self {
                id name email avatar createdAt updatedAt
                profiles { id provider name email platformId createdAt }
            }
        }
        """
        me = self.execute(query).get("self")
        if not me:
            raise AuthError("The API did not recognise this token.")
        return me

    def get_auth_sessions(self) -> List[dict]:
        """Every browser session signed in to this account."""
        query = """
        query Sessions {
            sessions { id ip invalidated lastActivity createdAt updatedAt }
        }
        """
        return self.execute(query).get("sessions") or []

    def get_login_url(self, provider: str = "steam") -> str:
        """Ask the API where to send a browser to sign in with a provider.

        The URL comes back with an http scheme and a :443 port, which is https
        wearing the wrong hat, so it goes through normalise_auth_url first.
        """
        query = "query Login($provider: String!) { login(provider: $provider) }"
        url = self.execute(query, {"provider": provider}).get("login")
        if not url:
            raise MonoSuiteError(f"The API did not return a login URL for {provider!r}.")
        return normalise_auth_url(str(url))

    def logout(self) -> Any:
        """Invalidate the current session server side."""
        return self.execute("query Logout { logout }").get("logout")

    def get_notifications(self, limit: int = 25) -> List[dict]:
        """Recent push notifications raised for this account."""
        query = """
        query Notifications($limit: Int) {
            notifications(limit: $limit) {
                id serverGroupId action title body status createdAt
            }
        }
        """
        return self.execute(query, {"limit": limit}).get("notifications") or []

    def get_dashboard_user(self, group_id: str, server_id: Optional[str] = None) -> Optional[dict]:
        """Your own in game user record for a group: roles, play time, Steam id.

        Handy for checking what you can do in a group. Not related to the
        adminId punishments carry, see resolve_admin_id.
        """
        query = f"""
        query DashboardUser($groupId: ID!, $serverId: ID) {{
            dashboardUser(groupId: $groupId, serverId: $serverId) {{
                id steamId playTime
                {F_PROFILE}
                primaryRole {{ id name color immunity }}
                roles {{ id name }}
            }}
        }}
        """
        return self.execute(query, {"groupId": group_id, "serverId": server_id}).get("dashboardUser")

    def resolve_admin_id(self) -> str:
        """The account id that punishments are stamped with.

        This is the account level id from self, not a profile id. An account
        can carry several profiles (Steam, Discord and so on) and the API
        decides on its own which of them a ban is displayed under, so the name
        in the ban history will not always be the profile you signed in with.
        Nothing sent from here changes that.
        """
        if "admin_id" not in self._cache:
            self._cache["admin_id"] = self.get_self()["id"]
        return self._cache["admin_id"]

    # -- organizations and groups -------------------------------------------

    def list_organizations(self) -> List[dict]:
        """Every organization your account belongs to."""
        query = """
        query Organizations {
            organizations {
                id name description ownerId logRetentionDays createdAt
                groups { id name }
            }
        }
        """
        return self.execute(query).get("organizations") or []

    def get_organization(self, org_id: str) -> Dict[str, Any]:
        """One organization with its groups."""
        query = """
        query Organization($orgId: ID!) {
            organization(orgId: $orgId) {
                id name description ownerId logRetentionDays
                linearIntegrationEnabled createdAt updatedAt
                owner { id name email }
                groups { id name organizationId linkedDiscordId createdAt }
            }
        }
        """
        org = self.execute(query, {"orgId": org_id}).get("organization")
        if not org:
            raise NotFoundError(f"No organization with id {org_id}.")
        return org

    def list_groups(self, org_id: str) -> List[dict]:
        """Server groups inside an organization."""
        query = """
        query Groups($orgId: ID!) {
            groups(orgId: $orgId) {
                id name organizationId linkedDiscordId createdAt
                servers { id name isOnline }
            }
        }
        """
        return self.execute(query, {"orgId": org_id}).get("groups") or []

    def get_group(self, group_id: str) -> Dict[str, Any]:
        """One server group, with its servers, roles and your permissions."""
        query = """
        query Group($groupId: ID!) {
            group(groupId: $groupId) {
                id name organizationId linkedDiscordId createdAt updatedAt
                servers { id name ip port game isOnline }
                roles { id name color immunity }
                selfPermissions { id name node }
                loggingCategories
            }
        }
        """
        return self._group(group_id, query)

    def get_group_config(self, group_id: str) -> Any:
        """The group config blob. It arrives as a JSON string, so parse it."""
        query = "query GroupConfig($groupId: ID!) { group(groupId: $groupId) { config } }"
        config = self._group(group_id, query).get("config")
        if isinstance(config, str):
            try:
                return json.loads(config)
            except json.JSONDecodeError:
                return config
        return config

    def get_group_config_schema(self, group_id: str) -> Any:
        """The schema describing which config keys exist and what they accept."""
        query = "query ConfigSchema($groupId: ID!) { group(groupId: $groupId) { configSchema } }"
        schema = self._group(group_id, query).get("configSchema")
        if isinstance(schema, str):
            try:
                return json.loads(schema)
            except json.JSONDecodeError:
                return schema
        return schema

    def list_servers(self, group_id: str) -> List[dict]:
        """Servers in a group."""
        query = """
        query Servers($groupId: ID!) {
            servers(groupId: $groupId) {
                id name ip port game description isOnline serverGroupId createdAt
            }
        }
        """
        return self.execute(query, {"groupId": group_id}).get("servers") or []

    def list_roles(self, group_id: str) -> List[dict]:
        """Roles defined in a group, with their permissions."""
        query = f"""
        query Roles($groupId: ID!) {{
            roles(groupId: $groupId) {{
                {F_ROLE}
                inherits {{ id name }}
                permissions {{ id name node }}
            }}
        }}
        """
        return self.execute(query, {"groupId": group_id}).get("roles") or []

    def list_permissions(self, group_id: str) -> List[dict]:
        """Every permission node the group can hand out."""
        query = """
        query Permissions($groupId: ID!) {
            permissions(groupId: $groupId) {
                id name description node game isDashboardOnly
            }
        }
        """
        return self.execute(query, {"groupId": group_id}).get("permissions") or []

    def list_discord_roles(self, group_id: str) -> List[dict]:
        """Roles in the Discord server linked to this group."""
        query = """
        query DiscordRoles($groupId: ID!) {
            discordRoles(groupId: $groupId) { id name color position }
        }
        """
        return self.execute(query, {"groupId": group_id}).get("discordRoles") or []

    def list_discord_channels(self, group_id: str) -> List[dict]:
        """Channels in the Discord server linked to this group."""
        query = """
        query DiscordChannels($groupId: ID!) {
            discordChannels(groupId: $groupId) { id name kind }
        }
        """
        return self.execute(query, {"groupId": group_id}).get("discordChannels") or []

    def get_group_recent_actions(self, group_id: str, limit: int = 50) -> List[dict]:
        """Recent activity across every server in the group."""
        query = f"""
        query GroupRecentActions($groupId: ID!, $limit: Int) {{
            group(groupId: $groupId) {{ recentActions(limit: $limit) {{ {F_LOG} }} }}
        }}
        """
        group = self._group(group_id, query, {"groupId": group_id, "limit": limit})
        return group.get("recentActions") or []

    # -- server -------------------------------------------------------------

    def get_server(self, server_id: str) -> Dict[str, Any]:
        """Name, address, game, online flag, owning group and log categories."""
        query = """
        query Server($serverId: ID!) {
            server(serverId: $serverId) {
                id name ip port game description isOnline
                serverGroupId createdAt updatedAt
                group { id name organizationId }
                loggingCategories
                stats { totalBans totalKicks totalWarnings }
            }
        }
        """
        return self._server(server_id, query)

    def get_group_id(self, server_id: str) -> Optional[str]:
        """The group a server belongs to. Notes and roles are group scoped."""
        cache_key = f"group_of:{server_id}"
        if cache_key not in self._cache:
            server = self.get_server(server_id)
            group = server.get("group") or {}
            self._cache[cache_key] = group.get("id") or server.get("serverGroupId")
        return self._cache[cache_key]

    def get_server_stats(self, server_id: str) -> Dict[str, Any]:
        """Lifetime totals for bans, kicks and warnings."""
        query = """
        query ServerStats($serverId: ID!) {
            server(serverId: $serverId) {
                stats { totalBans totalKicks totalWarnings }
            }
        }
        """
        return self._server(server_id, query).get("stats") or {}

    def get_logging_categories(self, server_id: str) -> List[str]:
        """The log categories this server actually emits, for use as filters."""
        query = """
        query Categories($serverId: ID!) {
            server(serverId: $serverId) { loggingCategories }
        }
        """
        return self._server(server_id, query).get("loggingCategories") or []

    def get_online_players(self, server_id: str) -> List[dict]:
        """Everyone connected right now, with their flags and punishment counts."""
        query = f"""
        query OnlinePlayers($serverId: ID!) {{
            server(serverId: $serverId) {{
                onlinePlayers {{
                    id steamId playTime createdAt
                    {F_PROFILE}
                    primaryRole {{ id name color }}
                    watched {{ id reason createdAt }}
                    bans {{ id expire unbannedAt }}
                    warnings {{ id active }}
                    kicks {{ id }}
                }}
            }}
        }}
        """
        return self._server(server_id, query).get("onlinePlayers") or []

    def get_connections(self, server_id: str) -> List[dict]:
        """Connection records for the server, open ones have no endedAt."""
        query = """
        query Connections($serverId: ID!) {
            server(serverId: $serverId) {
                connections { userId createdAt endedAt }
            }
        }
        """
        return self._server(server_id, query).get("connections") or []

    def get_logs(
        self,
        server_id: str,
        fetch_count: int = 50,
        message: Optional[str] = None,
        categories: Optional[Sequence[str]] = None,
        participants: Optional[Sequence[str]] = None,
        ordering: str = "desc",
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        scroll_time: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Server logs, newest first by default.

        message filters on the log text, categories narrows to what the server
        advertises in loggingCategories, and participants takes player UUIDs
        (not Steam ids). Timestamps are unix milliseconds.

        Returns the whole {total, scrollId, logs} block so you can page with
        scroll_logs().
        """
        query = f"""
        query Logs($serverId: ID!, $data: GetLogsInput!) {{
            server(serverId: $serverId) {{
                logs(data: $data) {{ total scrollId logs {{ {F_LOG} }} }}
            }}
        }}
        """
        data: Dict[str, Any] = {"fetchCount": fetch_count, "ordering": ordering}
        if message:
            data["message"] = message
        if categories:
            data["categories"] = list(categories)
        if participants:
            data["participants"] = list(participants)
        if start_timestamp is not None:
            data["startTimestamp"] = start_timestamp
        if end_timestamp is not None:
            data["endTimestamp"] = end_timestamp
        if scroll_time is not None:
            data["scrollTime"] = scroll_time
        server = self._server(server_id, query, {"serverId": server_id, "data": data})
        return server.get("logs") or {"total": 0, "scrollId": None, "logs": []}

    def scroll_logs(self, server_id: str, scroll_id: str, scroll_time: Optional[int] = None) -> Dict[str, Any]:
        """Fetch the next page of a log search using the scrollId you got back."""
        query = f"""
        query ScrollLogs($serverId: ID!, $scrollId: String!, $scrollTime: Int) {{
            server(serverId: $serverId) {{
                scrollLogs(scrollId: $scrollId, scrollTime: $scrollTime) {{
                    total scrollId logs {{ {F_LOG} }}
                }}
            }}
        }}
        """
        server = self._server(
            server_id,
            query,
            {"serverId": server_id, "scrollId": scroll_id, "scrollTime": scroll_time},
        )
        return server.get("scrollLogs") or {"total": 0, "scrollId": None, "logs": []}

    def get_audit_logs(self, server_id: str) -> Dict[str, Any]:
        """The audit trail, meaning what admins did rather than what players did."""
        query = """
        query AuditLogs($serverId: ID!) {
            server(serverId: $serverId) {
                auditLogs { total scrollId logs { id timestamp category message } }
            }
        }
        """
        return self._server(server_id, query).get("auditLogs") or {"total": 0, "logs": []}

    def get_recent_actions(self, server_id: str) -> List[dict]:
        """The server's recent activity feed, as log lines."""
        query = f"""
        query RecentActions($serverId: ID!) {{
            server(serverId: $serverId) {{ recentActions {{ {F_LOG} }} }}
        }}
        """
        return self._server(server_id, query).get("recentActions") or []

    def get_bans(self, server_id: str) -> Dict[str, Any]:
        """Server ban list with counts. Returns {total, active, expired, bans}.

        The API has no filter argument here, so filtering active from expired
        happens on this side with is_ban_active().
        """
        query = f"""
        query Bans($serverId: ID!) {{
            server(serverId: $serverId) {{
                bans {{ total active expired bans {{ {F_BAN} }} }}
            }}
        }}
        """
        return self._server(server_id, query).get("bans") or {
            "total": 0, "active": 0, "expired": 0, "bans": []
        }

    def get_group_bans(self, group_id: str, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """Bans across the whole group, which does support paging."""
        query = f"""
        query GroupBans($groupId: ID!, $limit: Int, $offset: Int) {{
            group(groupId: $groupId) {{
                bans(limit: $limit, offset: $offset) {{
                    total active expired bans {{ {F_BAN} }}
                }}
            }}
        }}
        """
        group = self._group(
            group_id, query, {"groupId": group_id, "limit": limit, "offset": offset}
        )
        return group.get("bans") or {"total": 0, "active": 0, "expired": 0, "bans": []}

    def get_blacklists(self, server_id: str) -> List[dict]:
        """Blacklist entries, which match on a value such as an IP or hardware id."""
        query = f"""
        query Blacklists($serverId: ID!) {{
            server(serverId: $serverId) {{ blacklists {{ {F_BLACKLIST} }} }}
        }}
        """
        return self._server(server_id, query).get("blacklists") or []

    def get_warnings(self, server_id: str) -> List[dict]:
        """Every warning issued on this server."""
        query = f"""
        query Warnings($serverId: ID!) {{
            server(serverId: $serverId) {{ warnings {{ {F_WARNING} }} }}
        }}
        """
        return self._server(server_id, query).get("warnings") or []

    def get_kicks(self, server_id: str) -> List[dict]:
        """Every kick issued on this server."""
        query = f"""
        query Kicks($serverId: ID!) {{
            server(serverId: $serverId) {{ kicks {{ {F_KICK} }} }}
        }}
        """
        return self._server(server_id, query).get("kicks") or []

    def get_server_screenshots(self, server_id: str) -> List[dict]:
        """Screenshots taken by admins on this server."""
        query = f"""
        query ServerScreenshots($serverId: ID!) {{
            server(serverId: $serverId) {{
                screenshots {{
                    id cdnId url createdAt
                    user {{ id steamId {F_PROFILE} }}
                    {F_ADMIN}
                }}
            }}
        }}
        """
        shots = self._server(server_id, query).get("screenshots") or []
        return [self._fill_screenshot_url(shot) for shot in shots]

    @staticmethod
    def _fill_screenshot_url(shot: dict) -> dict:
        """Build a CDN link when the API leaves url empty, as the web dashboard does."""
        if isinstance(shot, dict) and not shot.get("url"):
            key = shot.get("cdnId") or shot.get("id")
            if key:
                shot["url"] = f"{SCREENSHOT_CDN}/{key}"
        return shot

    # -- players ------------------------------------------------------------

    def find_player(self, server_id: str, value: str) -> Dict[str, Any]:
        """Cheap lookup, enough to turn a Steam id into an internal user id.

        value is typed as a plain String and the web dashboard puts names
        through it as well. Only Steam ids are tested here.
        """
        query = f"""
        query FindPlayer($serverId: ID!, $value: String!) {{
            server(serverId: $serverId) {{
                player(value: $value) {{
                    id steamId serverGroupId
                    {F_PROFILE}
                    watched {{ id reason }}
                }}
            }}
        }}
        """
        return self._player(server_id, value, query)

    def get_player(self, server_id: str, value: str) -> Dict[str, Any]:
        """The full player record: history, punishments, notes, roles, identities."""
        query = f"""
        query Player($serverId: ID!, $value: String!) {{
            server(serverId: $serverId) {{
                player(value: $value) {{
                    id steamId serverId serverGroupId playTime createdAt updatedAt
                    {F_PROFILE}
                    primaryRole {{ id name color immunity }}
                    roles {{ id name color }}
                    identities {{ platformId platformType createdAt }}
                    watched {{ id reason serverId createdAt }}
                    bans {{ {F_BAN} }}
                    warnings {{ {F_WARNING} }}
                    kicks {{ {F_KICK} }}
                    notes {{ {F_NOTE} }}
                }}
            }}
        }}
        """
        return self._player(server_id, value, query)

    def get_player_sessions(self, server_id: str, value: str) -> List[dict]:
        """Connection history. Kept separate because it can be long."""
        query = """
        query PlayerSessions($serverId: ID!, $value: String!) {
            server(serverId: $serverId) {
                player(value: $value) { sessions { serverId createdAt endedAt } }
            }
        }
        """
        return self._player(server_id, value, query).get("sessions") or []

    def get_player_extras(self, server_id: str, value: str) -> Dict[str, Any]:
        """Screenshots and incognito records, also split out for size reasons."""
        query = f"""
        query PlayerExtras($serverId: ID!, $value: String!) {{
            server(serverId: $serverId) {{
                player(value: $value) {{
                    id
                    screenshots {{ id cdnId url createdAt {F_ADMIN} }}
                    incognito {{ id role serverId expiry }}
                }}
            }}
        }}
        """
        player = self._player(server_id, value, query)
        player["screenshots"] = [
            self._fill_screenshot_url(shot) for shot in player.get("screenshots") or []
        ]
        return player

    def get_player_relations(self, server_id: str, value: str) -> Dict[str, Any]:
        """Family sharing and accounts the API believes are related.

        Worth a look before a ban evasion call, since it is the cheapest way to
        spot an alt.
        """
        query = f"""
        query PlayerRelations($serverId: ID!, $value: String!) {{
            server(serverId: $serverId) {{
                player(value: $value) {{
                    id steamId {F_PROFILE}
                    familyOwner {{ id steamId {F_PROFILE} }}
                    familyChildren {{ id steamId {F_PROFILE} }}
                    relatedAccounts {{ id steamId playTime {F_PROFILE} }}
                }}
            }}
        }}
        """
        return self._player(server_id, value, query)

    def get_player_punishments(self, server_id: str, value: str) -> List[dict]:
        """Bans, kicks and warnings in one list, newest last as the API sends them.

        punishments returns the Action interface, so each entry carries
        __typename and only bans have an expiry.
        """
        query = f"""
        query PlayerPunishments($serverId: ID!, $value: String!) {{
            server(serverId: $serverId) {{
                player(value: $value) {{
                    punishments {{
                        __typename id type reason createdAt serverId
                        {F_ADMIN}
                        ... on Ban {{ expire unbannedAt serverGroupWide }}
                        ... on Warning {{ points active }}
                    }}
                }}
            }}
        }}
        """
        return self._player(server_id, value, query).get("punishments") or []

    def search_players(self, server_id: str, value: str) -> List[dict]:
        """Search this server's players by name. Steam ids go to find_player."""
        query = """
        query SearchUsers($serverId: ID!, $value: String!) {
            server(serverId: $serverId) {
                searchUsers(value: $value) { id username avatar profileUrl }
            }
        }
        """
        server = self._server(server_id, query, {"serverId": server_id, "value": value})
        return server.get("searchUsers") or []

    def search_profiles(self, name: str, limit: int = 20) -> List[dict]:
        """Search profiles across everything your account can see."""
        query = """
        query SearchProfiles($name: String!, $limit: Int!) {
            searchProfiles(name: $name, limit: $limit) {
                id username avatar profileUrl
            }
        }
        """
        return self.execute(query, {"name": name, "limit": limit}).get("searchProfiles") or []

    def get_profiles(self, ids: Sequence[str]) -> List[dict]:
        """Look up profiles by id, handy for turning ids in logs into names."""
        query = """
        query Profiles($ids: [ID!]!) {
            getProfiles(ids: $ids) { id username avatar profileUrl }
        }
        """
        return self.execute(query, {"ids": list(ids)}).get("getProfiles") or []

    def resolve_user_id(self, server_id: str, who: str) -> str:
        """Accept a Steam id or an internal id and always return an internal id."""
        if looks_like_uuid(who):
            return who
        return self.find_player(server_id, who)["id"]

    # -- punishments --------------------------------------------------------

    def add_ban(
        self,
        server_id: str,
        user_id: str,
        reason: str,
        length: float = 0,
        server_group_wide: bool = False,
    ) -> Dict[str, Any]:
        """Ban a player. length is in seconds here and 0 means permanent.

        The API itself counts in minutes, so the value is converted on the way
        out. Keeping this method in seconds means callers do not have to think
        about it, and there is exactly one place to fix if the API ever changes.

        The ban is issued as the account behind the current token.
        """
        query = """
        mutation AddBan($serverId: ID!, $userId: ID!, $adminId: ID!,
                        $reason: String!, $length: Float!, $serverGroupWide: Boolean!) {
            addBan(serverId: $serverId, userId: $userId, adminId: $adminId,
                   reason: $reason, length: $length, serverGroupWide: $serverGroupWide) {
                id reason expire createdAt serverGroupWide
            }
        }
        """
        variables = {
            "serverId": server_id,
            "userId": user_id,
            "adminId": self.resolve_admin_id(),
            "reason": reason,
            "length": seconds_to_api_length(check_ban_length(length)),
            "serverGroupWide": bool(server_group_wide),
        }
        return self.execute(query, variables)["addBan"]

    def edit_ban(
        self,
        server_id: str,
        ban_id: str,
        reason: Optional[str] = None,
        length: Optional[float] = None,
        server_group_wide: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Change a ban in place. Only the arguments you pass get touched.

        length is in seconds here and gets converted to the minutes the API
        wants, same as add_ban.

        The new expiry is measured from the moment of the edit, not from when
        the ban was created. A ban created three weeks ago and edited to two
        weeks therefore runs for two weeks from now, and editBan(length=1)
        expires a ban more or less immediately, which is how the old unban
        workaround did its job.
        """
        query = """
        mutation EditBan($serverId: ID!, $id: ID!, $reason: String,
                         $length: Float, $serverGroupWide: Boolean) {
            editBan(serverId: $serverId, id: $id, reason: $reason,
                    length: $length, serverGroupWide: $serverGroupWide) {
                id reason expire editedAt serverGroupWide
            }
        }
        """
        variables: Dict[str, Any] = {"serverId": server_id, "id": ban_id}
        if reason is not None:
            variables["reason"] = reason
        if length is not None:
            variables["length"] = seconds_to_api_length(check_ban_length(length))
        if server_group_wide is not None:
            variables["serverGroupWide"] = bool(server_group_wide)
        return self.execute(query, variables)["editBan"]

    def unban(self, server_id: str, ban_id: str, reason: str) -> Dict[str, Any]:
        """Lift a ban and record who did it and why.

        Fills in unbannedAt, unbannedBy and unbanReason. The other way round,
        editBan with length=1, expires the ban but leaves all three empty, so
        only reach for that if this mutation is missing.
        """
        query = """
        mutation Unban($serverId: ID!, $id: ID!, $reason: String!) {
            unban(serverId: $serverId, id: $id, reason: $reason) {
                id reason expire unbanReason unbannedAt
            }
        }
        """
        return self.execute(
            query, {"serverId": server_id, "id": ban_id, "reason": reason}
        )["unban"]

    def add_blacklist(
        self,
        server_id: str,
        user_id: str,
        value: str,
        reason: str,
        length: float = 0,
    ) -> Dict[str, Any]:
        """Blacklist a value against a user.

        value is a category id from your own gamemode rather than an IP or a
        hardware id, so check what a number means before sending it. Run
        "blacklist list" and look at which reasons cluster on which value.

        length is in seconds here and is converted to minutes on the way out,
        on the assumption that blacklists count the same way bans do. That part
        is not confirmed. If a blacklist comes back 60 times too long or too
        short, this is the line to look at.
        """
        query = """
        mutation AddBlacklist($serverId: ID!, $userId: ID!, $value: String!,
                              $reason: String!, $length: Float!) {
            addBlacklist(serverId: $serverId, userId: $userId, value: $value,
                         reason: $reason, length: $length) {
                id value reason expire createdAt
            }
        }
        """
        variables = {
            "serverId": server_id,
            "userId": user_id,
            "value": value,
            "reason": reason,
            "length": seconds_to_api_length(length),
        }
        return self.execute(query, variables)["addBlacklist"]

    def expire_blacklist(self, server_id: str, user_id: str, blacklist_id: str) -> Dict[str, Any]:
        """Retire a blacklist entry. Needs the owning user id as well as the entry."""
        query = """
        mutation ExpireBlacklist($serverId: ID!, $userId: ID!, $id: ID!) {
            expireBlacklist(serverId: $serverId, userId: $userId, id: $id) {
                id value expire
            }
        }
        """
        return self.execute(
            query, {"serverId": server_id, "userId": user_id, "id": blacklist_id}
        )["expireBlacklist"]

    def set_watched(self, server_id: str, user_id: str, reason: str) -> Dict[str, Any]:
        """Flag a player for attention. An empty reason clears the flag."""
        query = """
        mutation SetWatched($serverId: ID!, $userId: ID!, $reason: String!) {
            setWatched(serverId: $serverId, userId: $userId, reason: $reason) {
                id reason createdAt
            }
        }
        """
        return self.execute(
            query, {"serverId": server_id, "userId": user_id, "reason": reason}
        )["setWatched"]

    def clear_watched(self, server_id: str, user_id: str) -> Dict[str, Any]:
        """Take a player off the watch list, which is a watch with no reason."""
        return self.set_watched(server_id, user_id, "")

    # -- notes --------------------------------------------------------------
    # Notes hang off the server group rather than the server, so every note
    # call wants a group id. get_group_id() digs it out of the server record.

    def create_note(
        self, group_id: str, user_id: str, content: str, note_type: str = "Neutral"
    ) -> Dict[str, Any]:
        """Attach a note to a player. Type is Positive, Neutral or Negative."""
        query = """
        mutation CreateNote($groupId: ID!, $userId: ID!, $content: String!, $noteType: String!) {
            createNote(groupId: $groupId, userId: $userId, content: $content, noteType: $noteType) {
                id content type createdAt
            }
        }
        """
        variables = {
            "groupId": group_id,
            "userId": user_id,
            "content": content,
            "noteType": note_type,
        }
        return self.execute(query, variables)["createNote"]

    def edit_note(
        self, note_id: str, user_id: str, group_id: str, content: str, note_type: str
    ) -> Dict[str, Any]:
        """Rewrite a note. Both the content and the type have to be supplied."""
        query = """
        mutation EditNote($id: ID!, $userId: ID!, $groupId: ID!,
                          $content: String!, $noteType: String!) {
            editNote(id: $id, userId: $userId, groupId: $groupId,
                     content: $content, noteType: $noteType) {
                id content type updatedAt
            }
        }
        """
        variables = {
            "id": note_id,
            "userId": user_id,
            "groupId": group_id,
            "content": content,
            "noteType": note_type,
        }
        return self.execute(query, variables)["editNote"]

    def delete_note(self, note_id: str, user_id: str, group_id: str) -> Any:
        """Remove a note for good."""
        query = """
        mutation DeleteNote($id: ID!, $userId: ID!, $groupId: ID!) {
            deleteNote(id: $id, userId: $userId, groupId: $groupId)
        }
        """
        return self.execute(
            query, {"id": note_id, "userId": user_id, "groupId": group_id}
        )["deleteNote"]

    # -- roles --------------------------------------------------------------

    def set_roles(self, group_id: str, user_id: str, role_ids: Sequence[str]) -> List[dict]:
        """Replace a player's roles with exactly this list.

        This is not additive. Pass every role the player should end up with,
        and an empty list to strip them all.
        """
        query = """
        mutation SetRoles($groupId: ID!, $userId: ID!, $roleIds: [ID!]!) {
            setRoles(groupId: $groupId, userId: $userId, roleIds: $roleIds) {
                id name color immunity
            }
        }
        """
        return self.execute(
            query, {"groupId": group_id, "userId": user_id, "roleIds": list(role_ids)}
        )["setRoles"]

    def create_role(
        self,
        group_id: str,
        name: str,
        immunity: int = 0,
        aliases: Optional[Sequence[str]] = None,
        color: str = "#ffffff",
        inherits_id: Optional[str] = None,
        permissions: Optional[Sequence[str]] = None,
        discord_role_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a role. Immunity decides who can punish whom, higher wins."""
        query = """
        mutation CreateRole($groupId: ID!, $name: String!, $immunity: Int!,
                            $aliases: [String!]!, $color: String!, $inheritsId: ID,
                            $permissions: [ID!]!, $discordRoleId: String) {
            createRole(groupId: $groupId, name: $name, immunity: $immunity,
                       aliases: $aliases, color: $color, inheritsId: $inheritsId,
                       permissions: $permissions, discordRoleId: $discordRoleId) {
                id name color immunity aliases
            }
        }
        """
        variables = {
            "groupId": group_id,
            "name": name,
            "immunity": int(immunity),
            "aliases": list(aliases or []),
            "color": color,
            "inheritsId": inherits_id,
            "permissions": list(permissions or []),
            "discordRoleId": discord_role_id,
        }
        return self.execute(query, variables)["createRole"]

    def edit_role(self, role_id: str, group_id: str, **fields: Any) -> Dict[str, Any]:
        """Change a role. Accepts name, immunity, aliases, color, inherits_id,
        permissions and discord_role_id, and sends only what you pass."""
        query = """
        mutation EditRole($id: ID!, $groupId: ID!, $name: String, $immunity: Int,
                          $aliases: [String!], $color: String, $inheritsId: ID,
                          $permissions: [ID!], $discordRoleId: String) {
            editRole(id: $id, groupId: $groupId, name: $name, immunity: $immunity,
                     aliases: $aliases, color: $color, inheritsId: $inheritsId,
                     permissions: $permissions, discordRoleId: $discordRoleId) {
                id name color immunity aliases
            }
        }
        """
        rename = {
            "inherits_id": "inheritsId",
            "discord_role_id": "discordRoleId",
        }
        variables: Dict[str, Any] = {"id": role_id, "groupId": group_id}
        for key, value in fields.items():
            if value is None:
                continue
            variables[rename.get(key, key)] = value
        return self.execute(query, variables)["editRole"]

    def delete_role(self, role_id: str, group_id: str) -> Any:
        """Delete a role. Anyone holding it simply stops holding it."""
        query = """
        mutation DeleteRole($id: ID!, $groupId: ID!) {
            deleteRole(id: $id, groupId: $groupId)
        }
        """
        return self.execute(query, {"id": role_id, "groupId": group_id})["deleteRole"]

    def reconcile_role_sync(self, group_id: str, role_id: str, dry_run: bool = True) -> Dict[str, Any]:
        """Line a role up with its linked Discord role.

        Leave dry_run on to see what would change before anything moves.
        """
        query = """
        mutation ReconcileRoleSync($groupId: ID!, $roleId: ID!, $dryRun: Boolean!) {
            reconcileRoleSync(groupId: $groupId, roleId: $roleId, dryRun: $dryRun) {
                direction holders resolvable capped dispatched
            }
        }
        """
        return self.execute(
            query, {"groupId": group_id, "roleId": role_id, "dryRun": bool(dry_run)}
        )["reconcileRoleSync"]

    # -- group and server administration ------------------------------------

    def set_config(self, group_id: str, key: str, value: str) -> Any:
        """Set one group config key. Values go over the wire as strings."""
        query = """
        mutation SetConfig($groupId: ID!, $key: String!, $value: String!) {
            setConfig(groupId: $groupId, key: $key, value: $value)
        }
        """
        return self.execute(
            query, {"groupId": group_id, "key": key, "value": value}
        )["setConfig"]

    def create_organization(self, name: str, description: str = "") -> Dict[str, Any]:
        """Create an organization, the thing that owns groups."""
        query = """
        mutation CreateOrganization($name: String!, $description: String!) {
            createOrganization(name: $name, description: $description) {
                id name description createdAt
            }
        }
        """
        return self.execute(
            query, {"name": name, "description": description}
        )["createOrganization"]

    def create_group(
        self, org_id: str, name: str, linked_discord_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a server group inside an organization."""
        query = """
        mutation CreateGroup($orgId: ID!, $name: String!, $linkedDiscordId: String) {
            createGroup(orgId: $orgId, name: $name, linkedDiscordId: $linkedDiscordId) {
                id name organizationId linkedDiscordId createdAt
            }
        }
        """
        return self.execute(
            query, {"orgId": org_id, "name": name, "linkedDiscordId": linked_discord_id}
        )["createGroup"]

    def create_server(
        self,
        group_id: str,
        name: str,
        ip: str,
        port: int,
        game: str,
        description: str = "",
    ) -> Dict[str, Any]:
        """Register a server in a group. game is a ServerGame enum value, for
        example GARRYS_MOD, RUST, SANDBOX, MINECRAFT or FIVE_M."""
        query = """
        mutation CreateServer($groupId: ID!, $name: String!, $description: String!,
                              $ip: String!, $port: Int!, $game: ServerGame!) {
            createServer(groupId: $groupId, name: $name, description: $description,
                         ip: $ip, port: $port, game: $game) {
                id name ip port game serverGroupId
            }
        }
        """
        variables = {
            "groupId": group_id,
            "name": name,
            "description": description,
            "ip": ip,
            "port": int(port),
            "game": game,
        }
        return self.execute(query, variables)["createServer"]

    def regenerate_server_key(self, server_id: str) -> str:
        """Issue a new server key. The old one stops working straight away."""
        query = """
        mutation RegenerateServerKey($serverId: ID!) {
            regenerateServerKey(serverId: $serverId)
        }
        """
        return self.execute(query, {"serverId": server_id})["regenerateServerKey"]

    def generate_download_link(self, server_id: str) -> str:
        """A one off link to download the server addon build for this server."""
        query = """
        mutation GenerateDownloadLink($serverId: ID!) {
            generateDownloadLink(serverId: $serverId)
        }
        """
        return self.execute(query, {"serverId": server_id})["generateDownloadLink"]

    def generate_link_token(self, group_id: str) -> str:
        """A token for linking something (typically Discord) to this group."""
        query = """
        mutation GenerateLinkToken($groupId: ID!) {
            generateLinkToken(groupId: $groupId)
        }
        """
        return self.execute(query, {"groupId": group_id})["generateLinkToken"]

    # -- support ------------------------------------------------------------

    def list_support_requests(self, org_id: str) -> List[dict]:
        """Support tickets raised for an organization."""
        query = """
        query SupportRequests($orgId: ID!) {
            supportRequests(orgId: $orgId) {
                id type title description status linearIssueIdentifier
                resolvedAt createdAt updatedAt
                updates { id kind body url createdAt }
            }
        }
        """
        return self.execute(query, {"orgId": org_id}).get("supportRequests") or []

    def submit_support_request(
        self,
        org_id: str,
        request_type: str,
        title: str,
        description: str,
        attachment_urls: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Raise a support ticket against an organization."""
        query = """
        mutation SubmitSupportRequest($orgId: ID!, $type: String!, $title: String!,
                                      $description: String!, $attachmentAssetUrls: [String!]!) {
            submitSupportRequest(orgId: $orgId, type: $type, title: $title,
                                 description: $description,
                                 attachmentAssetUrls: $attachmentAssetUrls) {
                id type title status createdAt
            }
        }
        """
        variables = {
            "orgId": org_id,
            "type": request_type,
            "title": title,
            "description": description,
            "attachmentAssetUrls": list(attachment_urls or []),
        }
        return self.execute(query, variables)["submitSupportRequest"]

    # -- introspection ------------------------------------------------------
    # Schemas move. Rather than trusting this file to stay current, ask the API
    # what it supports today and print that.

    _TYPE_REF = """
        kind name
        ofType { kind name ofType { kind name ofType { kind name } } }
    """

    @staticmethod
    def type_name(type_ref: Any) -> str:
        """Flatten a GraphQL type reference into something readable, like [Ban!]!."""
        if not type_ref:
            return "?"
        kind = type_ref.get("kind")
        if kind == "NON_NULL":
            return MonoSuiteClient.type_name(type_ref.get("ofType")) + "!"
        if kind == "LIST":
            return "[" + MonoSuiteClient.type_name(type_ref.get("ofType")) + "]"
        return type_ref.get("name") or MonoSuiteClient.type_name(type_ref.get("ofType"))

    def list_operations(self, kind: str = "mutation") -> List[dict]:
        """Every mutation or query the API currently exposes, with argument types."""
        root = "mutationType" if kind == "mutation" else "queryType"
        query = """
        query Introspect {
            __schema {
                %s {
                    name
                    fields {
                        name description
                        args { name type { %s } }
                        type { %s }
                    }
                }
            }
        }
        """ % (root, self._TYPE_REF, self._TYPE_REF)
        schema = self.execute(query)["__schema"][root] or {}
        return schema.get("fields") or []

    def describe_type(self, name: str) -> Dict[str, Any]:
        """Field list for one type, the GraphQL equivalent of a table definition."""
        query = """
        query DescribeType($name: String!) {
            __type(name: $name) {
                kind name description
                enumValues { name description }
                inputFields { name type { %s } }
                fields {
                    name description
                    args { name type { %s } }
                    type { %s }
                }
            }
        }
        """ % (self._TYPE_REF, self._TYPE_REF, self._TYPE_REF)
        described = self.execute(query, {"name": name}).get("__type")
        if not described:
            raise NotFoundError(f"The schema has no type called {name!r}.")
        return described

    def list_type_names(self) -> List[str]:
        """Every type name in the schema, minus the introspection plumbing."""
        query = "query TypeNames { __schema { types { name kind } } }"
        types = self.execute(query)["__schema"]["types"]
        return sorted(
            t["name"] for t in types if t.get("name") and not t["name"].startswith("__")
        )


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------

class Context:
    """Everything a command needs: settings, a console and a lazy client.

    The client is built on first use so that commands which never touch the
    network (config, templates, help) work with no token at all.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        token: Optional[str] = None,
        server_id: Optional[str] = None,
        group_id: Optional[str] = None,
        org_id: Optional[str] = None,
        browser: Optional[str] = None,
        as_json: bool = False,
        timeout: Optional[int] = None,
        verbose: bool = False,
        assume_yes: bool = False,
        dry_run: bool = False,
        no_color: bool = False,
    ):
        self.config = Config(Path(config_path) if config_path else CONFIG_FILE)
        self.as_json = as_json
        self.verbose = verbose
        self.assume_yes = assume_yes
        self.dry_run = dry_run
        self.console = Console(no_color=no_color, soft_wrap=False)
        self.errors = Console(stderr=True, no_color=no_color)

        self._token_flag = token
        self._server_id = server_id or os.environ.get(ENV_SERVER) or self.config.get("server_id")
        self._group_id = group_id or os.environ.get(ENV_GROUP) or self.config.get("group_id")
        self._org_id = org_id or self.config.get("org_id")
        self.browser = browser or self.config.get("browser")
        self.timeout = int(timeout or self.config.get("timeout") or 30)
        self._client: Optional[MonoSuiteClient] = None

    # -- settings -----------------------------------------------------------

    def resolve_token(self) -> str:
        """Find a token: flag, environment, config file, then browser cookie."""
        for candidate in (
            self._token_flag,
            os.environ.get(ENV_TOKEN),
            self.config.get("token"),
        ):
            if candidate:
                return str(candidate)
        try:
            return token_from_browser(self.browser) or ""
        except ImportError:
            return ""
        except MonoSuiteError:
            return ""

    def token_provider(self) -> Optional[str]:
        """Called by the client when its token has gone stale.

        Only the browser can hand out a genuinely new token, so that is all we
        try here. A flag or config value that has expired stays expired.
        """
        try:
            return token_from_browser(self.browser)
        except Exception:
            return None

    def client(self, allow_anonymous: bool = False) -> MonoSuiteClient:
        """The API client, built once and reused.

        Pass allow_anonymous for the handful of things the API answers without
        a session, namely introspection and the login URL query.
        """
        if self._client is None:
            token = self.resolve_token()
            if not token and not allow_anonymous:
                raise AuthError(
                    "No auth token found.\n"
                    "Run 'auth login' to sign in through your browser, or pass --token, "
                    f"or set {ENV_TOKEN}."
                )
            self._client = MonoSuiteClient(
                token,
                token_provider=self.token_provider,
                timeout=self.timeout,
                on_request=self._trace if self.verbose else None,
            )
        return self._client

    def _trace(self, query: str, variables: Optional[dict]) -> None:
        """Print the outgoing query when --verbose is on. Goes to stderr so it
        never pollutes piped JSON."""
        self.errors.print("[dim]--> " + " ".join(query.split())[:400] + "[/dim]")
        if variables:
            self.errors.print("[dim]    variables: " + json.dumps(variables)[:400] + "[/dim]")

    def require_server(self) -> str:
        if not self._server_id:
            raise MonoSuiteError(
                "No server id. Pass --server-id, set "
                f"{ENV_SERVER}, or run: config set server_id <uuid>\n"
                "Not sure of the id? Try: org list, then server list -g <group id>"
            )
        return str(self._server_id)

    def require_group(self) -> str:
        """The group id, looked up from the server when it was not given."""
        if not self._group_id:
            self._group_id = self.client().get_group_id(self.require_server())
        if not self._group_id:
            raise MonoSuiteError(
                "No group id, and the server record did not carry one. "
                "Pass --group-id or run: config set group_id <uuid>"
            )
        return str(self._group_id)

    def require_org(self) -> str:
        """The organization id, taken from your only org when there is just one."""
        if not self._org_id:
            orgs = self.client().list_organizations()
            if len(orgs) == 1:
                self._org_id = orgs[0]["id"]
            elif not orgs:
                raise MonoSuiteError("Your account is not in any organization.")
            else:
                names = ", ".join(f"{o['name']} ({o['id']})" for o in orgs)
                raise MonoSuiteError(
                    f"You are in several organizations, so pass --org-id. Options: {names}"
                )
        return str(self._org_id)

    # -- output -------------------------------------------------------------

    def emit(self, payload: Any, render: Callable[[], None]) -> None:
        """Print JSON or a table, depending on how the user asked for it."""
        if self.as_json:
            self.console.print_json(json.dumps(payload, default=str))
        else:
            render()

    def say(self, message: str) -> None:
        """A status line that stays out of the way when JSON was requested."""
        if not self.as_json:
            self.console.print(message)

    def confirm(self, question: str) -> bool:
        """Ask before anything destructive, unless --yes said not to bother."""
        if self.assume_yes:
            return True
        return click.confirm(question, default=False)

    def preview(self, description: str, details: Optional[dict] = None) -> bool:
        """Show what a mutation is about to do. False means do not send it.

        Returns False under --dry-run (after printing) and when the user says
        no at the prompt, so a command can simply return.
        """
        self.console.print(f"[bold]{description}[/bold]")
        if details:
            for key, value in details.items():
                self.console.print(f"  {key}: {value}")
        if self.dry_run:
            self.console.print("[yellow]Dry run, nothing was sent.[/yellow]")
            return False
        return self.confirm("Go ahead?")


pass_ctx = click.make_pass_decorator(Context)


class MonoCommand(click.Command):
    """A command that also accepts --json after its own name.

    Click only takes group level options before the subcommand, and nobody
    types "monosuite --json ban list" when "monosuite ban list --json" is
    right there, so the flag lives on both ends and is folded into the shared
    context before the command runs.
    """

    extra_options: Sequence[click.Option] = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.params.append(click.Option(
            ["--json", "json_out"], is_flag=True, default=False,
            help="Print raw JSON instead of tables.",
        ))
        for option in self.extra_options:
            self.params.append(option)

    def _fold_into_context(self, ctx: click.Context) -> None:
        """Move our injected flags off the parameter list and onto the Context."""
        shared = ctx.find_object(Context)
        json_out = ctx.params.pop("json_out", False)
        yes = ctx.params.pop("yes", False)
        dry_run = ctx.params.pop("dry_run", False)
        if shared is None:
            return
        shared.as_json = shared.as_json or json_out
        shared.assume_yes = shared.assume_yes or yes
        shared.dry_run = shared.dry_run or dry_run

    def invoke(self, ctx: click.Context):
        self._fold_into_context(ctx)
        return super().invoke(ctx)


class MutatingCommand(MonoCommand):
    """A command that changes something, so it carries its own safety flags."""

    extra_options = (
        click.Option(["--yes", "-y"], is_flag=True, default=False,
                     help="Skip the confirmation prompt."),
        click.Option(["--dry-run"], is_flag=True, default=False,
                     help="Show what would change, then stop."),
    )


class MonoGroup(click.Group):
    """A group whose commands and subgroups pick up the behaviour above."""

    command_class = MonoCommand

    @property
    def group_class(self):  # click uses this when a subgroup is declared
        return MonoGroup



def new_table(*columns: str, title: Optional[str] = None) -> Table:
    """A table with the house style, so every command looks the same."""
    table = Table(title=title, header_style="bold", title_justify="left", expand=False)
    for column in columns:
        table.add_column(column, overflow="fold")
    return table


def print_kv(console: Console, title: str, pairs: Sequence[tuple]) -> None:
    """Render a simple label and value block inside a panel."""
    body = "\n".join(f"[bold]{label}:[/bold] {value}" for label, value in pairs)
    console.print(Panel(body, title=title, title_align="left", expand=False))


def flag_summary(player: dict) -> str:
    """One short string covering watch status and punishment counts."""
    flags = []
    watched = player.get("watched") or []
    if watched:
        reason = watched[0].get("reason") if isinstance(watched[0], dict) else None
        flags.append(f"[yellow]watched[/yellow] ({shorten(reason, 50)})" if reason else "[yellow]watched[/yellow]")
    active_bans = [b for b in player.get("bans") or [] if is_ban_active(b)]
    if active_bans:
        flags.append(f"[red]{len(active_bans)} active ban[/red]")
    warnings = [w for w in player.get("warnings") or [] if w.get("active")]
    if warnings:
        flags.append(f"{len(warnings)} warnings")
    kicks = player.get("kicks") or []
    if kicks:
        flags.append(f"{len(kicks)} kicks")
    return ", ".join(flags) if flags else "-"


# ---------------------------------------------------------------------------
# Root command
# ---------------------------------------------------------------------------

@click.group(cls=MonoGroup, context_settings={
    "help_option_names": ["-h", "--help"],
    # click caps help at 80 columns unless told otherwise, so a wide
    # terminal gets no benefit and the command list truncates early.
    "max_content_width": 110,
})
@click.option("--token", help="Auth JWT. Overrides the environment and the config file.")
@click.option("--server-id", "-s", help="Server UUID to work against.")
@click.option("--group-id", "-g", help="Server group UUID. Worked out from the server when left off.")
@click.option("--org-id", help="Organization UUID. Worked out when you only have one.")
@click.option("--browser", type=click.Choice(BROWSERS), help="Only read the session cookie from this browser.")
@click.option("--config", "config_path", help="Use a different config file.")
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON instead of tables.")
@click.option("--timeout", type=int, help="Request timeout in seconds (default 30).")
@click.option("--verbose", "-v", is_flag=True, help="Print each query to stderr before sending it.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Skip confirmation prompts.")
@click.option("--dry-run", is_flag=True, help="Show what a change would do, then stop.")
@click.option("--no-color", is_flag=True, help="Plain output, for logs and pipes.")
@click.version_option(__version__, prog_name="monosuite-cli")
@click.pass_context
def cli(ctx, token, server_id, group_id, org_id, browser, config_path, as_json,
        timeout, verbose, assume_yes, dry_run, no_color):
    """MonoSuite from the command line.

    Start with "auth login" to sign in through your browser, then "org list"
    and "server list -g <group>" to find the ids you care about, then put the
    one you use every day in the config with "config set server_id <uuid>".

    Every command takes --json, so this plays nicely with jq and with scripts.
    """
    ctx.obj = Context(
        config_path=config_path,
        token=token,
        server_id=server_id,
        group_id=group_id,
        org_id=org_id,
        browser=browser,
        as_json=as_json,
        timeout=timeout,
        verbose=verbose,
        assume_yes=assume_yes,
        dry_run=dry_run,
        no_color=no_color,
    )


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

@cli.group()
def auth():
    """Sign in and manage the stored token."""


@auth.command("login")
@click.option("--provider", type=click.Choice(LOGIN_PROVIDERS),
              help="Go straight to this provider instead of the dashboard login page.")
@click.option("--browser", type=click.Choice(BROWSERS), help="Read the cookie from this browser only.")
@click.option("--wait", default=180, show_default=True, help="Seconds to wait for you to finish signing in.")
@click.option("--open/--no-open", "do_open", default=True, show_default=True,
              help="Open the login page automatically.")
@click.option("--force", is_flag=True, help="Sign in again even if a good token is already there.")
@click.option("--save/--no-save", default=True, show_default=True,
              help="Store the token in the config file so other shells can use it.")
@pass_ctx
def auth_login(ctx: Context, provider, browser, wait, do_open, force, save):
    """Sign in through your browser and pick up the session token.

    The dashboard keeps its JWT in the monosuite_token cookie, so this opens
    the login page, waits for you to finish with Steam or Discord, then reads
    the cookie back out.

    If you are already signed in in a browser, it finds the token straight away
    and does not open anything.

    Headless box with no browser? Use "auth set-token" and paste the JWT from
    your workstation, or point MONOSUITE_TOKEN at it.
    """
    browser = browser or ctx.browser
    existing = None
    try:
        existing = token_from_browser(browser)
    except ImportError as error:
        # No browser-cookie3 here. Say so once, then carry on to the login page.
        ctx.console.print(f"[yellow]{error}[/yellow]")

    if existing and token_is_alive(existing) and not force:
        ctx.console.print("[green]Found a live session in your browser.[/green]")
        _finish_login(ctx, existing, save)
        return

    # Nothing usable yet, so send the browser somewhere useful.
    url = LOGIN_URL
    if provider:
        # login(provider) is a public query, so this works before we have a token.
        url = MonoSuiteClient("", url=API_URL, timeout=ctx.timeout).get_login_url(provider)

    if do_open and open_in_browser(url):
        ctx.console.print(f"Opened [link={url}]{url}[/link] in your browser.")
    else:
        ctx.console.print(f"Open this page and sign in: [link={url}]{url}[/link]")

    ctx.console.print(f"Waiting up to {wait} seconds for the session cookie...")
    try:
        token = wait_for_browser_token(browser, timeout=wait, ignore=existing)
    except ImportError as error:
        raise MonoSuiteError(
            f"{error}\n\nWithout it I cannot read the cookie. "
            f"Sign in, then paste the token with: auth set-token"
        )

    if not token:
        raise AuthError(
            "No session cookie turned up in time.\n"
            "If you did sign in, your browser may be locking its cookie store "
            "(recent Chrome builds on Windows do this). Try --browser firefox, "
            "or paste the token with: auth set-token"
        )
    _finish_login(ctx, token, save)


def _finish_login(ctx: Context, token: str, save: bool) -> None:
    """Verify a freshly found token and, if asked, write it to the config."""
    client = MonoSuiteClient(token, timeout=ctx.timeout)
    me = client.get_self()
    expiry = client.expires_at
    ctx.console.print(
        f"[green]Signed in as {me.get('name', 'Unknown')}[/green] "
        f"({me.get('email', 'no email')})"
    )
    if expiry:
        ctx.console.print(
            f"Token runs out {format_time(expiry, with_seconds=True)} "
            f"({format_relative(expiry)})"
        )
    if save:
        ctx.config.set("token", token)
        ctx.config.save()
        ctx.console.print(f"Saved to {ctx.config.path}")
    else:
        ctx.console.print("Not saved. Export it yourself if you need it:")
        ctx.console.print(f"  set {ENV_TOKEN}={token[:24]}...")


@auth.command("status")
@pass_ctx
def auth_status(ctx: Context):
    """Show who the current token belongs to and when it expires."""
    client = ctx.client()
    me = client.get_self()
    left = client.seconds_left()
    payload = {
        "id": me.get("id"),
        "name": me.get("name"),
        "email": me.get("email"),
        "expiresAt": client.expires_at.isoformat() if client.expires_at else None,
        "secondsLeft": left,
        "providers": [p.get("provider") for p in me.get("profiles") or []],
    }

    def render():
        print_kv(ctx.console, "Signed in", [
            ("Account", me.get("name", "-")),
            ("Email", me.get("email", "-")),
            ("Account id", me.get("id", "-")),
            ("Providers", ", ".join(p.get("provider", "?") for p in me.get("profiles") or []) or "-"),
            ("Token expires", format_time(client.expires_at, with_seconds=True) if client.expires_at else "unknown"),
            ("Time left", format_duration(left) if left and left > 0 else ("expired" if left is not None else "unknown")),
        ])
    ctx.emit(payload, render)


@auth.command("token")
@pass_ctx
def auth_token(ctx: Context):
    """Print the token itself, for feeding into curl or another tool."""
    token = ctx.resolve_token()
    if not token:
        raise AuthError("No token found. Run: auth login")
    click.echo(token)


@auth.command("set-token")
@click.option("--token", help="The JWT. Leave it off and you get a hidden prompt.")
@pass_ctx
def auth_set_token(ctx: Context, token):
    """Store a token you pasted in yourself.

    For machines with no browser to read a cookie from.
    """
    token = token or click.prompt("Paste the token", hide_input=True)
    client = MonoSuiteClient(token.strip(), timeout=ctx.timeout)
    me = client.get_self()
    ctx.config.set("token", token.strip())
    ctx.config.save()
    ctx.console.print(f"[green]Stored a working token for {me.get('name')}.[/green]")


@auth.command("logout")
@click.option("--server-side", is_flag=True, help="Also invalidate the session at the API.")
@pass_ctx
def auth_logout(ctx: Context, server_side):
    """Forget the stored token.

    With --server-side it also invalidates the session at the API.
    """
    if server_side:
        try:
            ctx.client().logout()
            ctx.console.print("Session invalidated at the API.")
        except MonoSuiteError as error:
            ctx.console.print(f"[yellow]Could not invalidate the session: {error}[/yellow]")
    ctx.config.unset("token")
    ctx.config.save()
    ctx.console.print("Removed the stored token.")


@auth.command("sessions")
@pass_ctx
def auth_sessions(ctx: Context):
    """List the browser sessions signed in to your account."""
    sessions = ctx.client().get_auth_sessions()

    def render():
        table = new_table("Session", "IP", "Last activity", "Created", "State",
                          title=f"{len(sessions)} sessions")
        for session in sessions:
            table.add_row(
                str(session.get("id", "-"))[:12],
                str(session.get("ip", "-")),
                format_time(session.get("lastActivity")),
                format_time(session.get("createdAt")),
                "invalidated" if session.get("invalidated") else "active",
            )
        ctx.console.print(table)
    ctx.emit(sessions, render)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@cli.group("config")
def config_group():
    """Read and write the settings file."""


@config_group.command("show")
@click.option("--reveal", is_flag=True, help="Print the stored token in full.")
@pass_ctx
def config_show(ctx: Context, reveal):
    """Show the saved settings.

    The token is masked unless you pass --reveal.
    """
    data = dict(ctx.config.data)
    if data.get("token") and not reveal:
        data["token"] = data["token"][:12] + "... (use --reveal to see it)"

    def render():
        table = new_table("Key", "Value", title=str(ctx.config.path))
        for key in sorted(data):
            table.add_row(key, str(data[key]))
        if not data:
            table.add_row("-", "nothing saved yet")
        ctx.console.print(table)
    ctx.emit(data, render)


@config_group.command("set")
@click.argument("key")
@click.argument("value")
@pass_ctx
def config_set(ctx: Context, key, value):
    """Save one setting.

    For example: config set server_id 0f0e...c21
    """
    if key not in CONFIG_KEYS:
        raise MonoSuiteError(f"Unknown key {key!r}. Known keys: {', '.join(CONFIG_KEYS)}")
    ctx.config.set(key, value)
    ctx.config.save()
    ctx.console.print(f"[green]{key} saved.[/green]")


@config_group.command("unset")
@click.argument("key")
@pass_ctx
def config_unset(ctx: Context, key):
    """Remove one setting."""
    ctx.config.unset(key)
    ctx.config.save()
    ctx.console.print(f"[green]{key} removed.[/green]")


@config_group.command("path")
@pass_ctx
def config_path_cmd(ctx: Context):
    """Print where the config file lives."""
    click.echo(str(ctx.config.path))


# ---------------------------------------------------------------------------
# schema and raw
# ---------------------------------------------------------------------------

@cli.group()
def schema():
    """Ask the API what it can do, live.

    Useful when something here stops matching the server, which does happen.
    Everything these commands print comes from introspection, not from this
    file, so it is always current.
    """


@schema.command("mutations")
@click.option("--search", help="Only show operations whose name contains this.")
@pass_ctx
def schema_mutations(ctx: Context, search):
    """List every mutation the API exposes, with its arguments."""
    _print_operations(ctx, "mutation", search)


@schema.command("queries")
@click.option("--search", help="Only show operations whose name contains this.")
@pass_ctx
def schema_queries(ctx: Context, search):
    """List every root query the API exposes, with its arguments."""
    _print_operations(ctx, "query", search)


def _print_operations(ctx: Context, kind: str, search: Optional[str]) -> None:
    fields = ctx.client(allow_anonymous=True).list_operations(kind)
    if search:
        needle = search.lower()
        fields = [f for f in fields if needle in f["name"].lower()]
    payload = [
        {
            "name": f["name"],
            "args": {a["name"]: MonoSuiteClient.type_name(a["type"]) for a in f.get("args") or []},
            "returns": MonoSuiteClient.type_name(f.get("type")),
        }
        for f in fields
    ]

    def render():
        table = new_table("Operation", "Arguments", "Returns",
                          title=f"{len(payload)} {kind}s")
        for item in payload:
            args = "\n".join(f"{k}: {v}" for k, v in item["args"].items()) or "-"
            table.add_row(item["name"], args, item["returns"])
        ctx.console.print(table)
    ctx.emit(payload, render)


@schema.command("type")
@click.argument("name")
@pass_ctx
def schema_type(ctx: Context, name):
    """Show the fields of one type, for example: schema type Ban"""
    described = ctx.client(allow_anonymous=True).describe_type(name)

    def render():
        kind = described.get("kind")
        ctx.console.print(f"[bold]{described['name']}[/bold] ({kind})")
        if described.get("enumValues"):
            ctx.console.print("values: " + ", ".join(v["name"] for v in described["enumValues"]))
        rows = new_table("Field", "Type", "Arguments")
        for field in described.get("inputFields") or []:
            rows.add_row(field["name"], MonoSuiteClient.type_name(field["type"]), "-")
        for field in described.get("fields") or []:
            args = ", ".join(
                f"{a['name']}: {MonoSuiteClient.type_name(a['type'])}"
                for a in field.get("args") or []
            )
            rows.add_row(field["name"], MonoSuiteClient.type_name(field["type"]), args or "-")
        ctx.console.print(rows)
    ctx.emit(described, render)


@schema.command("types")
@click.option("--search", help="Only show names containing this.")
@pass_ctx
def schema_types(ctx: Context, search):
    """List every type name in the schema."""
    names = ctx.client(allow_anonymous=True).list_type_names()
    if search:
        names = [n for n in names if search.lower() in n.lower()]
    ctx.emit(names, lambda: ctx.console.print(", ".join(names)))


@cli.command("raw")
@click.argument("query")
@click.option("--var", "variables", multiple=True, metavar="KEY=VALUE",
              help="A variable. Repeat for more. Values that parse as JSON are sent as JSON.")
@click.option("--vars-json", help="All variables at once as a JSON object.")
@pass_ctx
def raw_cmd(ctx: Context, query, variables, vars_json):
    """Send a query this tool does not wrap yet.

    Pass the query as a string, or as @filename to read it from a file. The
    reply is printed as JSON, errors and all.

    \b
        raw 'query { organizations { id name } }'
        raw @mine.graphql --var limit=10
    """
    if query.startswith("@"):
        query = Path(query[1:]).read_text(encoding="utf-8")

    payload: Dict[str, Any] = {}
    if vars_json:
        payload.update(json.loads(vars_json))
    for item in variables:
        if "=" not in item:
            raise MonoSuiteError(f"--var wants KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        try:
            payload[key] = json.loads(value)
        except json.JSONDecodeError:
            payload[key] = value

    result = ctx.client().raw(query, payload or None)
    ctx.console.print_json(json.dumps(result, default=str))


# ---------------------------------------------------------------------------
# org and group
# ---------------------------------------------------------------------------

@cli.group("org")
def org_group():
    """Organizations, which own your server groups."""


@org_group.command("list")
@pass_ctx
def org_list(ctx: Context):
    """List your organizations and their groups."""
    orgs = ctx.client().list_organizations()

    def render():
        table = new_table("Organization", "Id", "Groups", title=f"{len(orgs)} organizations")
        for org in orgs:
            groups = "\n".join(f"{g['name']}  {g['id']}" for g in org.get("groups") or []) or "-"
            table.add_row(org.get("name", "-"), str(org.get("id")), groups)
        ctx.console.print(table)
    ctx.emit(orgs, render)


@org_group.command("show")
@click.argument("org_id", required=False)
@pass_ctx
def org_show(ctx: Context, org_id):
    """Show one organization in detail."""
    org = ctx.client().get_organization(org_id or ctx.require_org())

    def render():
        owner = org.get("owner") or {}
        print_kv(ctx.console, org.get("name", "Organization"), [
            ("Id", org.get("id")),
            ("Description", org.get("description") or "-"),
            ("Owner", owner.get("name", "-")),
            ("Log retention", f"{org.get('logRetentionDays', '-')} days"),
            ("Created", format_time(org.get("createdAt"))),
        ])
        table = new_table("Group", "Id", "Discord")
        for group in org.get("groups") or []:
            table.add_row(group.get("name", "-"), str(group.get("id")),
                          str(group.get("linkedDiscordId") or "-"))
        ctx.console.print(table)
    ctx.emit(org, render)


@cli.group("group")
def group_group():
    """Server groups: servers, roles and settings."""


@group_group.command("list")
@click.option("--org-id", help="Which organization to look in.")
@pass_ctx
def group_list(ctx: Context, org_id):
    """List the groups in an organization."""
    groups = ctx.client().list_groups(org_id or ctx.require_org())

    def render():
        table = new_table("Group", "Id", "Servers", title=f"{len(groups)} groups")
        for group in groups:
            servers = "\n".join(
                f"{'online ' if s.get('isOnline') else 'offline'}  {s['name']}  {s['id']}"
                for s in group.get("servers") or []
            ) or "-"
            table.add_row(group.get("name", "-"), str(group.get("id")), servers)
        ctx.console.print(table)
    ctx.emit(groups, render)


@group_group.command("show")
@pass_ctx
def group_show(ctx: Context):
    """Show the current group in detail."""
    group = ctx.client().get_group(ctx.require_group())

    def render():
        print_kv(ctx.console, group.get("name", "Group"), [
            ("Id", group.get("id")),
            ("Organization", group.get("organizationId")),
            ("Discord", group.get("linkedDiscordId") or "not linked"),
            ("Created", format_time(group.get("createdAt"))),
            ("Your permissions", str(len(group.get("selfPermissions") or []))),
        ])
        servers = new_table("Server", "Address", "Game", "Online", "Id")
        for server in group.get("servers") or []:
            servers.add_row(
                server.get("name", "-"),
                f"{server.get('ip', '-')}:{server.get('port', '-')}",
                str(server.get("game", "-")),
                "yes" if server.get("isOnline") else "no",
                str(server.get("id")),
            )
        ctx.console.print(servers)
        roles = new_table("Role", "Immunity", "Id")
        for role in group.get("roles") or []:
            roles.add_row(role.get("name", "-"), str(role.get("immunity", "-")), str(role.get("id")))
        ctx.console.print(roles)
    ctx.emit(group, render)


@group_group.command("permissions")
@click.option("--mine", is_flag=True, help="Only the permissions you personally hold.")
@pass_ctx
def group_permissions(ctx: Context, mine):
    """List permission nodes, yours or all of them."""
    group_id = ctx.require_group()
    if mine:
        items = ctx.client().get_group(group_id).get("selfPermissions") or []
    else:
        items = ctx.client().list_permissions(group_id)

    def render():
        table = new_table("Permission", "Node", "Dashboard only", title=f"{len(items)} permissions")
        for item in items:
            table.add_row(
                item.get("name", "-"),
                item.get("node", "-"),
                "yes" if item.get("isDashboardOnly") else "no",
            )
        ctx.console.print(table)
    ctx.emit(items, render)


@group_group.command("config")
@click.option("--schema", "want_schema", is_flag=True, help="Print the config schema instead of the values.")
@pass_ctx
def group_config(ctx: Context, want_schema):
    """Print the group config as JSON."""
    client = ctx.client()
    group_id = ctx.require_group()
    data = client.get_group_config_schema(group_id) if want_schema else client.get_group_config(group_id)
    ctx.console.print_json(json.dumps(data, default=str))


@group_group.command("set-config", cls=MutatingCommand)
@click.argument("key")
@click.argument("value")
@pass_ctx
def group_set_config(ctx: Context, key, value):
    """Set one config key on the group.

    Values travel as strings. For a JSON value, quote it the way your shell
    needs, for example: group set-config welcome.enabled true
    """
    group_id = ctx.require_group()
    if not ctx.preview("Change group config", {"group": group_id, "key": key, "value": value}):
        return
    result = ctx.client().set_config(group_id, key, value)
    ctx.console.print(f"[green]Set {key}.[/green] API said: {result}")


@group_group.command("discord")
@click.option("--channels", is_flag=True, help="Show channels instead of roles.")
@pass_ctx
def group_discord(ctx: Context, channels):
    """List roles or channels on the linked Discord."""
    group_id = ctx.require_group()
    client = ctx.client()
    items = client.list_discord_channels(group_id) if channels else client.list_discord_roles(group_id)

    def render():
        if channels:
            table = new_table("Channel", "Kind", "Id", title=f"{len(items)} channels")
            for item in items:
                table.add_row(item.get("name", "-"), item.get("kind", "-"), str(item.get("id")))
        else:
            table = new_table("Role", "Colour", "Position", "Id", title=f"{len(items)} roles")
            for item in items:
                table.add_row(item.get("name", "-"), str(item.get("color") or "-"),
                              str(item.get("position", "-")), str(item.get("id")))
        ctx.console.print(table)
    ctx.emit(items, render)


@group_group.command("link-token", cls=MutatingCommand)
@pass_ctx
def group_link_token(ctx: Context):
    """Generate a link token for this group.

    This is what the Discord link flow asks for.
    """
    group_id = ctx.require_group()
    if not ctx.preview("Generate a link token", {"group": group_id}):
        return
    click.echo(ctx.client().generate_link_token(group_id))


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

@cli.group("server")
def server_group():
    """Status, players and settings for one server."""


@server_group.command("list")
@click.option("--group-id", "-g", help="Which group to list servers from.")
@pass_ctx
def server_list(ctx: Context, group_id):
    """List the servers in a group, with their ids."""
    servers = ctx.client().list_servers(group_id or ctx.require_group())

    def render():
        table = new_table("Server", "Address", "Game", "Online", "Id",
                          title=f"{len(servers)} servers")
        for server in servers:
            table.add_row(
                server.get("name", "-"),
                f"{server.get('ip', '-')}:{server.get('port', '-')}",
                str(server.get("game", "-")),
                "[green]yes[/green]" if server.get("isOnline") else "[red]no[/red]",
                str(server.get("id")),
            )
        ctx.console.print(table)
    ctx.emit(servers, render)


@server_group.command("info")
@pass_ctx
def server_info(ctx: Context):
    """Show address, status, group and lifetime totals."""
    server = ctx.client().get_server(ctx.require_server())

    def render():
        stats = server.get("stats") or {}
        group = server.get("group") or {}
        print_kv(ctx.console, server.get("name", "Server"), [
            ("Id", server.get("id")),
            ("Address", f"{server.get('ip', '-')}:{server.get('port', '-')}"),
            ("Game", server.get("game", "-")),
            ("Status", "[green]online[/green]" if server.get("isOnline") else "[red]offline[/red]"),
            ("Group", f"{group.get('name', '-')} ({group.get('id', '-')})"),
            ("Description", server.get("description") or "-"),
            ("Created", format_time(server.get("createdAt"))),
            ("Totals", f"{stats.get('totalBans', 0)} bans, "
                       f"{stats.get('totalKicks', 0)} kicks, "
                       f"{stats.get('totalWarnings', 0)} warnings"),
            ("Log categories", str(len(server.get("loggingCategories") or []))),
        ])
    ctx.emit(server, render)


@server_group.command("players")
@click.option("--sort", type=click.Choice(["name", "playtime", "session"]), default="name",
              show_default=True, help="How to order the list.")
@click.option("--flagged", is_flag=True, help="Only players who are watched or carry punishments.")
@pass_ctx
def server_players(ctx: Context, sort, flagged):
    """Show who is online right now, with their flags.

    Open sessions come from the server's connection list, which is how the
    "online for" column gets filled in.
    """
    client = ctx.client()
    server_id = ctx.require_server()
    players = client.get_online_players(server_id)

    # Match each player to their open connection so we can show session length.
    session_start = {}
    try:
        for connection in client.get_connections(server_id):
            if not connection.get("endedAt"):
                session_start[connection.get("userId")] = connection.get("createdAt")
    except MonoSuiteError:
        # Connections are a nice to have. Losing them should not lose the list.
        pass

    for player in players:
        player["_sessionStart"] = session_start.get(player.get("id"))

    if flagged:
        players = [
            p for p in players
            if (p.get("watched") or [])
            or any(is_ban_active(b) for b in p.get("bans") or [])
            or any(w.get("active") for w in p.get("warnings") or [])
        ]

    if sort == "playtime":
        players.sort(key=lambda p: float(p.get("playTime") or 0), reverse=True)
    elif sort == "session":
        players.sort(key=lambda p: float(p.get("_sessionStart") or 0))
    else:
        players.sort(key=lambda p: profile_name(p).lower())

    def render():
        table = new_table("Player", "Steam id", "Role", "Play time", "Online for", "Flags",
                          title=f"{len(players)} players online")
        for player in players:
            table.add_row(
                profile_name(player),
                steam_id_of(player),
                (player.get("primaryRole") or {}).get("name", "-"),
                format_playtime(player.get("playTime")),
                format_relative(player["_sessionStart"]).replace(" ago", "") if player.get("_sessionStart") else "-",
                flag_summary(player),
            )
        ctx.console.print(table)
    ctx.emit(players, render)


@server_group.command("categories")
@pass_ctx
def server_categories(ctx: Context):
    """List the log categories this server emits.

    Feed them to logs search --category.
    """
    categories = ctx.client().get_logging_categories(ctx.require_server())
    ctx.emit(categories, lambda: ctx.console.print("\n".join(categories) or "none"))


@server_group.command("stats")
@pass_ctx
def server_stats(ctx: Context):
    """Lifetime ban, kick and warning totals."""
    stats = ctx.client().get_server_stats(ctx.require_server())

    def render():
        print_kv(ctx.console, "Totals", [
            ("Bans", stats.get("totalBans", 0)),
            ("Kicks", stats.get("totalKicks", 0)),
            ("Warnings", stats.get("totalWarnings", 0)),
        ])
    ctx.emit(stats, render)


@server_group.command("download-link", cls=MutatingCommand)
@pass_ctx
def server_download_link(ctx: Context):
    """Generate a download link for the addon build."""
    server_id = ctx.require_server()
    if not ctx.preview("Generate a download link", {"server": server_id}):
        return
    click.echo(ctx.client().generate_download_link(server_id))


@server_group.command("regenerate-key", cls=MutatingCommand)
@pass_ctx
def server_regenerate_key(ctx: Context):
    """Issue a new server key.

    The running server loses the old one immediately.
    """
    server_id = ctx.require_server()
    if not ctx.preview(
        "Regenerate the server key",
        {"server": server_id, "warning": "the current key stops working immediately"},
    ):
        return
    click.echo(ctx.client().regenerate_server_key(server_id))


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

@cli.group("logs")
def logs_group():
    """Search, tail and audit the server logs."""


def _render_log_table(ctx: Context, entries: Sequence[dict], title: str,
                      show_ids: bool = False) -> None:
    table = new_table("Time", "Category", "Message", "Players", title=title)
    for entry in entries:
        people = entry.get("participants") or []
        if show_ids:
            players = "\n".join(
                f"{profile_name(person)}  {steam_id_of(person)}  {person.get('id')}"
                for person in people
            )
        else:
            players = ", ".join(profile_name(person) for person in people)
        table.add_row(
            format_time(entry.get("timestamp"), with_seconds=True),
            str(entry.get("category", "-")),
            cell(entry.get("message")),
            cell(players),
        )
    ctx.console.print(table)


def log_participants(entries: Sequence[dict]) -> List[dict]:
    """The distinct players named across a set of log lines, in first seen order."""
    seen = set()
    people = []
    for entry in entries:
        for person in entry.get("participants") or []:
            key = person.get("id")
            if key and key not in seen:
                seen.add(key)
                people.append(person)
    return people


def pick_from_logs(ctx: Context, client: MonoSuiteClient, server_id: str,
                   entries: Sequence[dict]) -> None:
    """Turn a set of log lines into a list of players you can open.

    Log lines carry their participants, ids included, so there is no lookup
    needed between reading a line and acting on whoever is in it.
    """
    people = log_participants(entries)
    if not people:
        ctx.console.print("[yellow]No players are named in these lines.[/yellow]")
        return

    while True:
        table = new_table("#", "Player", "Steam id", "Internal id",
                          title=f"{len(people)} players in these lines")
        for number, person in enumerate(people, 1):
            table.add_row(str(number), profile_name(person),
                          steam_id_of(person), str(person.get("id")))
        ctx.console.print(table)

        choice = click.prompt("Open which player? (number, or blank to stop)",
                              default="", show_default=False).strip()
        if not choice:
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(people):
            ctx.console.print("[yellow]Pick a number from the list.[/yellow]")
            continue
        player_console(ctx, client, server_id, people[int(choice) - 1]["id"])


@logs_group.command("search")
@click.option("--message", "-m", help="Only lines containing this text.")
@click.option("--category", "-c", "categories", multiple=True,
              help="Only these categories. Repeat for more. See: server categories")
@click.option("--player", "-p", "players", multiple=True,
              help="Only lines involving this player. Takes a Steam id or an internal id.")
@click.option("--limit", "-n", default=50, show_default=True, help="How many lines to fetch.")
@click.option("--oldest-first", is_flag=True, help="Print oldest first instead of newest first.")
@click.option("--pages", default=1, show_default=True, help="Follow the scroll id for this many pages.")
@click.option("--ids", "show_ids", is_flag=True, help="Show Steam and internal ids for the players on each line.")
@click.option("--pick", is_flag=True, help="List the players in the results and open one of them.")
@pass_ctx
def logs_search(ctx: Context, message, categories, players, limit, oldest_first, pages,
                show_ids, pick):
    """Search the log.

    Everything is optional, so a bare call gets the latest lines.

    \b
        logs search -n 20
        logs search -m "picked up" -c Pickup
        logs search -p 76561198000000000 --pages 3
        logs search -m "rdm" --pick

    --pick lists everyone named in the results, opens the one you choose, and
    lets you watch, note or ban them without leaving the log.
    """
    client = ctx.client()
    server_id = ctx.require_server()
    if pick:
        if ctx.as_json:
            raise MonoSuiteError("--pick is interactive, so it does not go with --json.")
        if not sys.stdin.isatty():
            raise MonoSuiteError("--pick needs a terminal it can prompt in.")
    participant_ids = [client.resolve_user_id(server_id, p) for p in players]

    result = client.get_logs(
        server_id,
        fetch_count=limit,
        message=message,
        categories=list(categories) or None,
        participants=participant_ids or None,
        ordering="asc" if oldest_first else "desc",
    )
    entries = list(result.get("logs") or [])
    scroll_id = result.get("scrollId")
    for _ in range(max(0, pages - 1)):
        if not scroll_id:
            break
        page = client.scroll_logs(server_id, scroll_id)
        entries.extend(page.get("logs") or [])
        scroll_id = page.get("scrollId")

    payload = {"total": result.get("total"), "scrollId": scroll_id, "logs": entries}
    ctx.emit(payload, lambda: _render_log_table(
        ctx, entries, f"{len(entries)} of {result.get('total', '?')} log lines", show_ids
    ))
    if pick:
        pick_from_logs(ctx, client, server_id, entries)


@logs_group.command("follow")
@click.option("--interval", default=10, show_default=True, help="Seconds between polls.")
@click.option("--message", "-m", help="Only lines containing this text.")
@click.option("--category", "-c", "categories", multiple=True, help="Only these categories.")
@click.option("--limit", "-n", default=25, show_default=True, help="Lines to fetch per poll.")
@pass_ctx
def logs_follow(ctx: Context, interval, message, categories, limit):
    """Tail the log until you stop it with Ctrl-C.

    Each poll asks for the newest lines and prints whatever has not been seen
    yet, so a slow interval never skips anything as long as fewer than --limit
    lines happened in between.
    """
    client = ctx.client()
    server_id = ctx.require_server()
    seen: set = set()
    ctx.console.print(f"[dim]Following {server_id}, polling every {interval}s. Ctrl-C to stop.[/dim]")
    try:
        while True:
            result = client.get_logs(
                server_id,
                fetch_count=limit,
                message=message,
                categories=list(categories) or None,
            )
            fresh = [entry for entry in reversed(result.get("logs") or [])
                     if entry.get("id") not in seen]
            for entry in fresh:
                seen.add(entry.get("id"))
                players = ", ".join(profile_name(p) for p in entry.get("participants") or [])
                line = (
                    f"[dim]{format_time(entry.get('timestamp'), with_seconds=True)}[/dim] "
                    f"[cyan]{entry.get('category', '-')}[/cyan] {entry.get('message', '')}"
                )
                if players:
                    line += f" [dim]({players})[/dim]"
                ctx.console.print(line)
            # Keep the seen set from growing without bound on a busy server.
            if len(seen) > 5000:
                seen = set(list(seen)[-2000:])
            time.sleep(interval)
    except KeyboardInterrupt:
        ctx.console.print("\n[dim]Stopped.[/dim]")


@logs_group.command("audit")
@pass_ctx
def logs_audit(ctx: Context):
    """Show the audit trail.

    Admin actions rather than player actions.
    """
    result = ctx.client().get_audit_logs(ctx.require_server())
    entries = result.get("logs") or []
    ctx.emit(result, lambda: _render_log_table(
        ctx, entries, f"{len(entries)} of {result.get('total', '?')} audit lines"
    ))


@cli.command("activity")
@click.option("--group", "whole_group", is_flag=True, help="Across the group instead of one server.")
@click.option("--limit", "-n", default=50, show_default=True, help="How many entries (group mode only).")
@click.option("--ids", "show_ids", is_flag=True, help="Show Steam and internal ids for the players on each line.")
@pass_ctx
def activity_cmd(ctx: Context, whole_group, limit, show_ids):
    """Show the recent activity feed.

    --group covers every server in the group instead of just this one.
    """
    client = ctx.client()
    if whole_group:
        entries = client.get_group_recent_actions(ctx.require_group(), limit)
        title = f"{len(entries)} recent group actions"
    else:
        entries = client.get_recent_actions(ctx.require_server())
        title = f"{len(entries)} recent actions"
    ctx.emit(entries, lambda: _render_log_table(ctx, entries, title, show_ids))


# ---------------------------------------------------------------------------
# player
# ---------------------------------------------------------------------------

@cli.group("player")
def player_group():
    """Look up players: history, punishments, notes."""


@player_group.command("search")
@click.argument("name")
@click.option("--global", "everywhere", is_flag=True, help="Search every profile you can see, not just this server.")
@click.option("--limit", "-n", default=20, show_default=True, help="Maximum results (global search only).")
@pass_ctx
def player_search(ctx: Context, name, everywhere, limit):
    """Search players by name."""
    client = ctx.client()
    results = (
        client.search_profiles(name, limit) if everywhere
        else client.search_players(ctx.require_server(), name)
    )

    def render():
        table = new_table("Player", "Profile id", "Steam profile", title=f"{len(results)} matches")
        for item in results:
            table.add_row(
                profile_name(item),
                str(item.get("id", "-")),
                str(item.get("profileUrl") or "-"),
            )
        ctx.console.print(table)
    ctx.emit(results, render)


def render_player(ctx: Context, record: dict,
                  sessions: Optional[Sequence[dict]] = None,
                  relations: Optional[dict] = None) -> None:
    """Print the whole player view: summary, then whatever history exists.

    Shared by "player show" and by the picker in "logs search --pick" so the
    two look the same. Empty sections are skipped rather than printed empty.
    """
    watched = record.get("watched") or []
    print_kv(ctx.console, profile_name(record), [
        ("Steam id", steam_id_of(record)),
        ("Internal id", record.get("id")),
        ("Steam profile", steam_profile_url(record) or "-"),
        ("Role", (record.get("primaryRole") or {}).get("name", "-")),
        ("Play time", format_playtime(record.get("playTime"))),
        ("First seen", format_time(record.get("createdAt"))),
        ("Last update", format_relative(record.get("updatedAt"))),
        ("Watched", cell(watched[0].get("reason")) if watched else "no"),
    ])

    bans = record.get("bans") or []
    if bans:
        table = new_table("Ban id", "Reason", "Expires", "Admin", "State", title="Bans")
        for ban in bans:
            table.add_row(
                str(ban.get("id")),
                cell(ban.get("reason")),
                format_time(ban.get("expire")) if ban.get("expire") else "permanent",
                admin_name(ban),
                "[red]active[/red]" if is_ban_active(ban) else "expired",
            )
        ctx.console.print(table)

    warnings = record.get("warnings") or []
    if warnings:
        table = new_table("When", "Reason", "Points", "Admin", "Active", title="Warnings")
        for warning in warnings:
            table.add_row(
                format_time(warning.get("createdAt")),
                cell(warning.get("reason")),
                str(warning.get("points", "-")),
                admin_name(warning),
                "yes" if warning.get("active") else "no",
            )
        ctx.console.print(table)

    kicks = record.get("kicks") or []
    if kicks:
        table = new_table("When", "Reason", "Admin", title="Kicks")
        for kick in kicks:
            table.add_row(format_time(kick.get("createdAt")),
                          cell(kick.get("reason")), admin_name(kick))
        ctx.console.print(table)

    notes = record.get("notes") or []
    if notes:
        table = new_table("Note id", "Type", "Content", "Admin", "When", title="Notes")
        for note in notes:
            table.add_row(
                str(note.get("id")),
                str(note.get("type", "-")),
                cell(note.get("content")),
                admin_name(note),
                format_time(note.get("createdAt")),
            )
        ctx.console.print(table)

    if sessions is not None:
        table = new_table("Connected", "Left", "Length", title="Recent sessions")
        for session in list(sessions)[-15:]:
            started = parse_time(session.get("createdAt"))
            ended = parse_time(session.get("endedAt"))
            length = format_duration((ended - started).total_seconds()) if started and ended else "still on"
            table.add_row(format_time(session.get("createdAt")),
                          format_time(session.get("endedAt")) if ended else "-", length)
        ctx.console.print(table)

    if relations is not None:
        data = relations or {}
        table = new_table("Relation", "Player", "Steam id", title="Related accounts")
        owner = data.get("familyOwner")
        if owner:
            table.add_row("family owner", profile_name(owner), steam_id_of(owner))
        for child in data.get("familyChildren") or []:
            table.add_row("family child", profile_name(child), steam_id_of(child))
        for other in data.get("relatedAccounts") or []:
            table.add_row("related", profile_name(other), steam_id_of(other))
        if table.row_count:
            ctx.console.print(table)
        else:
            ctx.console.print("[dim]No related accounts found.[/dim]")


def player_console(ctx: Context, client: MonoSuiteClient, server_id: str,
                   player_ref: str) -> None:
    """Show a player and stay there, so you can act on what you just read.

    Mutations go through the same preview and confirmation as the standalone
    commands, so --dry-run and --yes behave identically here.
    """
    while True:
        record = client.get_player(server_id, player_ref)
        render_player(ctx, record)
        choice = click.prompt(
            "Action: [w]atch, [n]ote, [b]an, [r]efresh, [q]uit",
            default="q", show_default=False,
        ).strip().lower()[:1]

        if choice in ("", "q"):
            return
        if choice == "r":
            continue
        try:
            if choice == "w":
                _console_watch(ctx, client, server_id, record)
            elif choice == "n":
                _console_note(ctx, client, server_id, record)
            elif choice == "b":
                _console_ban(ctx, client, server_id, record)
            else:
                ctx.console.print("[yellow]Not one of the options.[/yellow]")
        except MonoSuiteError as error:
            # A refused mutation should not throw you out of the player view.
            ctx.console.print(f"[red]{error}[/red]")


def _console_watch(ctx: Context, client: MonoSuiteClient, server_id: str, record: dict) -> None:
    """Watch or unwatch from the player view."""
    if record.get("watched"):
        if not click.confirm("Already watched. Clear the flag?", default=False):
            return
        if ctx.preview("Stop watching a player", {"player": profile_name(record)}):
            client.clear_watched(server_id, record["id"])
            ctx.console.print("[green]Watch cleared.[/green]")
        return

    reason = click.prompt("Watch reason", default="", show_default=False).strip()
    if not reason:
        return
    if ctx.preview("Watch a player", {"player": profile_name(record), "reason": reason}):
        client.set_watched(server_id, record["id"], reason)
        ctx.console.print("[green]Player is now watched.[/green]")


def _console_note(ctx: Context, client: MonoSuiteClient, server_id: str, record: dict) -> None:
    """Add a note from the player view."""
    content = click.prompt("Note", default="", show_default=False).strip()
    if not content:
        return
    note_type = click.prompt(
        "Type", default="Neutral",
        type=click.Choice(NOTE_TYPES, case_sensitive=False),
    ).capitalize()
    group_id = ctx.require_group()
    if ctx.preview("Add a note", {
        "player": profile_name(record),
        "type": note_type,
        "content": cell(content),
    }):
        client.create_note(group_id, record["id"], content, note_type)
        ctx.console.print("[green]Note added.[/green]")


def _console_ban(ctx: Context, client: MonoSuiteClient, server_id: str, record: dict) -> None:
    """Ban from the player view.

    Deliberately plain: reason, length, scope. Templates live on "ban add -t",
    where there is room to argue with the choice before it goes through.
    """
    reason = click.prompt("Ban reason", default="", show_default=False).strip()
    if not reason:
        return
    seconds = parse_duration(
        click.prompt("Length (7d, 2w, 1mo, perm)", default="perm")
    )
    group_wide = click.confirm("Apply across the whole group?", default=False)
    if ctx.preview("Ban a player", {
        "player": f"{profile_name(record)} ({steam_id_of(record)})",
        "reason": reason,
        "duration": format_duration(seconds),
        "sent as": f"length {seconds_to_api_length(seconds):,.0f} (the API counts minutes)",
        "scope": "every server in the group" if group_wide else "this server",
    }):
        result = client.add_ban(server_id, record["id"], reason, seconds, group_wide)
        ctx.console.print(f"[green]Banned.[/green] Ban id {result.get('id')}")


@player_group.command("show")
@click.argument("player")
@click.option("--sessions", "show_sessions", is_flag=True, help="Also list recent sessions.")
@click.option("--relations", is_flag=True, help="Also list family sharing and related accounts.")
@click.option("--pick", is_flag=True, help="Stay open afterwards so you can watch, note or ban.")
@pass_ctx
def player_show(ctx: Context, player, show_sessions, relations, pick):
    """Show everything about one player.

    Takes a Steam id, a name or an internal id. With --pick it stays open
    afterwards and offers watch, note and ban.
    """
    client = ctx.client()
    server_id = ctx.require_server()

    if pick:
        if ctx.as_json:
            raise MonoSuiteError("--pick is interactive, so it does not go with --json.")
        if not sys.stdin.isatty():
            raise MonoSuiteError("--pick needs a terminal it can prompt in.")
        player_console(ctx, client, server_id, player)
        return

    record = client.get_player(server_id, player)
    payload = dict(record)
    sessions = client.get_player_sessions(server_id, player) if show_sessions else None
    relation_data = client.get_player_relations(server_id, player) if relations else None
    if sessions is not None:
        payload["sessions"] = sessions
    if relation_data is not None:
        payload["relations"] = relation_data

    ctx.emit(payload, lambda: render_player(ctx, record, sessions, relation_data))


@player_group.command("sessions")
@click.argument("player")
@click.option("--limit", "-n", default=25, show_default=True, help="How many to show, newest last.")
@pass_ctx
def player_sessions(ctx: Context, player, limit):
    """List a player's connection history."""
    sessions = ctx.client().get_player_sessions(ctx.require_server(), player)
    shown = sessions[-limit:]

    def render():
        table = new_table("Connected", "Left", "Length", title=f"{len(sessions)} sessions")
        for session in shown:
            started = parse_time(session.get("createdAt"))
            ended = parse_time(session.get("endedAt"))
            length = format_duration((ended - started).total_seconds()) if started and ended else "still on"
            table.add_row(format_time(session.get("createdAt")),
                          format_time(session.get("endedAt")) if ended else "-", length)
        ctx.console.print(table)
    ctx.emit(sessions, render)


@player_group.command("screenshots")
@click.argument("player")
@pass_ctx
def player_screenshots(ctx: Context, player):
    """List screenshots taken of a player."""
    extras = ctx.client().get_player_extras(ctx.require_server(), player)
    shots = extras.get("screenshots") or []

    def render():
        table = new_table("When", "Admin", "Link", title=f"{len(shots)} screenshots")
        for shot in shots:
            table.add_row(format_time(shot.get("createdAt")), admin_name(shot), str(shot.get("url", "-")))
        ctx.console.print(table)
        incognito = extras.get("incognito") or []
        if incognito:
            ctx.console.print(f"[dim]{len(incognito)} incognito records on this player.[/dim]")
    ctx.emit(extras, render)


@player_group.command("history")
@click.argument("player")
@pass_ctx
def player_history(ctx: Context, player):
    """Bans, kicks and warnings in one timeline."""
    punishments = ctx.client().get_player_punishments(ctx.require_server(), player)

    def render():
        table = new_table("When", "Kind", "Reason", "Admin", "Detail",
                          title=f"{len(punishments)} punishments")
        for item in punishments:
            kind = item.get("__typename", item.get("type", "-"))
            if kind == "Ban":
                detail = "permanent" if not item.get("expire") else format_time(item.get("expire"))
                if item.get("unbannedAt"):
                    detail = f"lifted {format_time(item.get('unbannedAt'))}"
            elif kind == "Warning":
                detail = f"{item.get('points', 0)} points" + ("" if item.get("active") else ", inactive")
            else:
                detail = "-"
            table.add_row(
                format_time(item.get("createdAt")),
                str(kind),
                cell(item.get("reason")),
                admin_name(item),
                detail,
            )
        ctx.console.print(table)
    ctx.emit(punishments, render)


@player_group.command("roles", cls=MutatingCommand)
@click.argument("player")
@click.option("--set", "role_ids", multiple=True,
              help="Replace the player's roles with these ids. Repeat per role, none strips them all.")
@click.option("--clear", is_flag=True, help="Remove every role from the player.")
@pass_ctx
def player_roles(ctx: Context, player, role_ids, clear):
    """Show a player's roles, or replace them.

    Setting roles is not additive: whatever you pass becomes the full list, so
    include the roles they should keep.
    """
    client = ctx.client()
    server_id = ctx.require_server()
    record = client.get_player(server_id, player)

    if not role_ids and not clear:
        roles = record.get("roles") or []
        ctx.emit(roles, lambda: ctx.console.print(
            "\n".join(f"{r.get('name')}  {r.get('id')}" for r in roles) or "no roles"
        ))
        return

    group_id = ctx.require_group()
    wanted = [] if clear else list(role_ids)
    if not ctx.preview("Replace roles", {
        "player": profile_name(record),
        "roles": ", ".join(wanted) or "none, this strips every role",
    }):
        return
    result = client.set_roles(group_id, record["id"], wanted)
    ctx.console.print("[green]Roles set:[/green] " +
                      (", ".join(r.get("name", "?") for r in result) or "none"))


# ---------------------------------------------------------------------------
# ban
# ---------------------------------------------------------------------------

@cli.group("ban")
def ban_group():
    """Bans: list them, add them, edit them, lift them."""


@ban_group.command("list")
@click.option("--expired", "show_expired", is_flag=True, help="Include bans that have run out.")
@click.option("--group", "whole_group", is_flag=True, help="List the whole group, not just this server.")
@click.option("--limit", "-n", default=50, show_default=True, help="Maximum rows (group mode pages server side).")
@pass_ctx
def ban_list(ctx: Context, show_expired, whole_group, limit):
    """List bans.

    Active ones only, unless you ask for the expired ones too.
    """
    client = ctx.client()
    if whole_group:
        result = client.get_group_bans(ctx.require_group(), limit=limit)
    else:
        result = client.get_bans(ctx.require_server())
    bans = result.get("bans") or []
    if not show_expired:
        bans = [ban for ban in bans if is_ban_active(ban)]
    bans = bans[:limit]

    def render():
        table = new_table("Player", "Reason", "Expires", "Admin", "Scope", "Ban id",
                          title=f"{len(bans)} bans shown, {result.get('active', '?')} active "
                                f"of {result.get('total', '?')} total")
        for ban in bans:
            if ban.get("unbannedAt"):
                expires = f"lifted {format_time(ban.get('unbannedAt'))}"
            elif ban.get("expire"):
                expires = format_time(ban.get("expire"))
            else:
                expires = "permanent"
            table.add_row(
                user_name(ban),
                cell(ban.get("reason")),
                expires,
                admin_name(ban),
                "group" if ban.get("serverGroupWide") else "server",
                str(ban.get("id")),
            )
        ctx.console.print(table)
    ctx.emit(bans, render)


@ban_group.command("add", cls=MutatingCommand)
@click.argument("player")
@click.option("--reason", "-r", help="Why. Required unless a template supplies it.")
@click.option("--duration", "-d", default="perm", show_default=True,
              help="How long: 7d, 12h, 2w, 1mo, or perm.")
@click.option("--template", "-t", help="Use a ban template. See: templates list")
@click.option("--class", "offense_class", help="Which class of the template, usually A to D.")
@click.option("--group-wide", is_flag=True, help="Apply across every server in the group.")
@pass_ctx
def ban_add(ctx: Context, player, reason, duration, template, offense_class, group_wide):
    """Ban a player.

    \b
        ban add 76561198000000000 -r "RDM" -d 7d
        ban add 76561198000000000 -t RDM --class C
        ban add somename -r "Cheating" --group-wide

    With a template, the reason and the duration come from the punishment
    ladder, and anything you pass explicitly still wins. Templates whose class
    calls for a warning are refused, because the API has no warning mutation
    and quietly turning a warning into a ban would be the wrong favour.
    """
    seconds = parse_duration(duration)
    if template:
        entry = _template_entry(template, offense_class)
        template_seconds, action, template_reason = entry
        if action == "warn":
            raise MonoSuiteError(
                f"{template} class {offense_class} calls for a warning, not a ban. "
                f"The API exposes no warning mutation, so issue it in game or in the "
                f"dashboard. Pass --reason and --duration explicitly if you really "
                f"do want a ban."
            )
        reason = reason or template_reason
        if duration == "perm":  # untouched default, so let the template decide
            seconds = template_seconds
    if not reason:
        raise MonoSuiteError("A ban needs a reason. Pass --reason or use a template.")

    client = ctx.client()
    server_id = ctx.require_server()
    record = client.find_player(server_id, player)

    if not ctx.preview("Ban a player", {
        "player": f"{profile_name(record)} ({steam_id_of(record) if record.get('steamId') else record['id']})",
        "reason": reason,
        "duration": format_duration(seconds),
        "sent as": f"length {seconds_to_api_length(seconds):,.0f} (the API counts minutes)",
        "scope": "every server in the group" if group_wide else "this server",
    }):
        return

    result = client.add_ban(server_id, record["id"], reason, seconds, group_wide)
    ctx.console.print(
        f"[green]Banned {profile_name(record)}[/green] "
        f"({format_duration(seconds)}), ban id {result.get('id')}"
    )


@ban_group.command("edit", cls=MutatingCommand)
@click.argument("ban_id")
@click.option("--reason", "-r", help="New reason.")
@click.option("--duration", "-d", help="New length, counted from now rather than from when the ban started.")
@click.option("--group-wide/--server-only", "group_wide", default=None, help="Change the scope.")
@pass_ctx
def ban_edit(ctx: Context, ban_id, reason, duration, group_wide):
    """Change an existing ban.

    Careful with --duration: the new expiry runs from the moment of the edit,
    not from when the ban started. Editing a three week old ban to two weeks
    gives the player two more weeks, it does not cut the ban short.
    """
    if reason is None and duration is None and group_wide is None:
        raise MonoSuiteError("Nothing to change. Pass --reason, --duration or a scope flag.")
    seconds = parse_duration(duration) if duration is not None else None
    server_id = ctx.require_server()

    if not ctx.preview("Edit a ban", {
        "ban id": ban_id,
        "reason": reason if reason is not None else "unchanged",
        "duration": (f"{format_duration(seconds)} from now, not from when the ban started"
                     if seconds is not None else "unchanged"),
        "sent as": (f"length {seconds_to_api_length(seconds):,.0f} (the API counts minutes)"
                    if seconds is not None else "unchanged"),
        "scope": "unchanged" if group_wide is None else ("group" if group_wide else "server"),
    }):
        return

    result = ctx.client().edit_ban(server_id, ban_id, reason, seconds, group_wide)
    expires = format_time(result.get("expire")) if result.get("expire") else "permanent"
    ctx.console.print(f"[green]Ban updated.[/green] Expires: {expires}")


@ban_group.command("remove", cls=MutatingCommand)
@click.argument("player", required=False)
@click.option("--ban-id", help="Lift this exact ban instead of looking one up.")
@click.option("--reason", "-r", default="Unbanned via CLI", show_default=True,
              help="Recorded as the unban reason.")
@click.option("--all", "lift_all", is_flag=True, help="Lift every active ban the player has here.")
@click.option("--legacy", is_flag=True,
              help="Expire the ban with editBan(length=1) instead of calling unban().")
@pass_ctx
def ban_remove(ctx: Context, player, ban_id, reason, lift_all, legacy):
    """Lift a ban.

    \b
        ban remove 76561198000000000 -r "Appeal accepted"
        ban remove --ban-id 8f2c...  -r "Wrong person"

    unban() records who lifted it and why. --legacy shortens the ban to one
    second instead, which expires it just as well but leaves unbannedBy and
    unbanReason empty. Only worth it if unban() is missing or misbehaving.
    """
    client = ctx.client()
    server_id = ctx.require_server()

    targets: List[dict] = []
    if ban_id:
        targets = [{"id": ban_id, "reason": "(not loaded)"}]
        label = ban_id
    else:
        if not player:
            raise MonoSuiteError("Name a player, or pass --ban-id.")
        record = client.get_player(server_id, player)
        active = [ban for ban in record.get("bans") or [] if is_ban_active(ban)]
        if not active:
            ctx.console.print(f"{profile_name(record)} has no active bans here.")
            return
        targets = active if lift_all else active[:1]
        label = profile_name(record)

    if not ctx.preview("Lift bans", {
        "target": label,
        "bans": ", ".join(str(t.get("id")) for t in targets),
        "reason": reason,
        "method": "shorten to one second (legacy)" if legacy else "unban mutation",
    }):
        return

    for target in targets:
        if legacy:
            client.edit_ban(server_id, target["id"], length=1)
        else:
            client.unban(server_id, target["id"], reason)
        ctx.console.print(f"[green]Lifted ban {target['id']}.[/green]")


# ---------------------------------------------------------------------------
# blacklist, watch, note
# ---------------------------------------------------------------------------

@cli.group("blacklist")
def blacklist_group():
    """Blacklist entries, matched on IP or hardware id."""


@blacklist_group.command("list")
@click.option("--expired", "show_expired", is_flag=True, help="Include entries that have run out.")
@pass_ctx
def blacklist_list(ctx: Context, show_expired):
    """List blacklist entries on this server."""
    entries = ctx.client().get_blacklists(ctx.require_server())
    if not show_expired:
        now = datetime.now(timezone.utc)
        entries = [
            entry for entry in entries
            if not entry.get("expire") or (parse_time(entry.get("expire")) or now) > now
        ]

    def render():
        table = new_table("Player", "Value", "Reason", "Expires", "Admin", "Entry id",
                          title=f"{len(entries)} entries")
        for entry in entries:
            table.add_row(
                user_name(entry),
                cell(entry.get("value")),
                cell(entry.get("reason")),
                format_time(entry.get("expire")) if entry.get("expire") else "permanent",
                admin_name(entry),
                str(entry.get("id")),
            )
        ctx.console.print(table)
    ctx.emit(entries, render)


@blacklist_group.command("add", cls=MutatingCommand)
@click.argument("player")
@click.argument("value")
@click.option("--reason", "-r", required=True, help="Why this value is blacklisted.")
@click.option("--duration", "-d", default="perm", show_default=True, help="How long: 7d, 1mo, perm.")
@pass_ctx
def blacklist_add(ctx: Context, player, value, reason, duration):
    """Blacklist a value against a player.

    \b
        blacklist add 76561198000000000 192.0.2.44 -r "Ban evasion"
    """
    client = ctx.client()
    server_id = ctx.require_server()
    record = client.find_player(server_id, player)
    seconds = parse_duration(duration)

    if not ctx.preview("Add a blacklist entry", {
        "player": profile_name(record),
        "value": f"{value}  (a category id, not an IP. See: blacklist list)",
        "reason": reason,
        "duration": format_duration(seconds),
        "sent as": (f"length {seconds_to_api_length(seconds):,.0f}, assuming blacklists "
                    f"count in minutes like bans do, which is unconfirmed"),
    }):
        return
    result = client.add_blacklist(server_id, record["id"], value, reason, seconds)
    ctx.console.print(f"[green]Blacklisted {value}.[/green] Entry id {result.get('id')}")


@blacklist_group.command("expire", cls=MutatingCommand)
@click.argument("player")
@click.argument("blacklist_id")
@pass_ctx
def blacklist_expire(ctx: Context, player, blacklist_id):
    """Retire a blacklist entry.

    Needs the player it belongs to as well as the entry id.
    """
    client = ctx.client()
    server_id = ctx.require_server()
    record = client.find_player(server_id, player)
    if not ctx.preview("Expire a blacklist entry", {
        "player": profile_name(record),
        "entry": blacklist_id,
    }):
        return
    client.expire_blacklist(server_id, record["id"], blacklist_id)
    ctx.console.print("[green]Entry expired.[/green]")


@cli.group("watch")
def watch_group():
    """Flag players without punishing them."""


@watch_group.command("add", cls=MutatingCommand)
@click.argument("player")
@click.option("--reason", "-r", required=True, help="What to watch them for.")
@pass_ctx
def watch_add(ctx: Context, player, reason):
    """Put a player on the watch list."""
    client = ctx.client()
    server_id = ctx.require_server()
    record = client.find_player(server_id, player)
    if not ctx.preview("Watch a player", {"player": profile_name(record), "reason": reason}):
        return
    client.set_watched(server_id, record["id"], reason)
    ctx.console.print(f"[green]{profile_name(record)} is now watched.[/green]")


@watch_group.command("remove", cls=MutatingCommand)
@click.argument("player")
@pass_ctx
def watch_remove(ctx: Context, player):
    """Take a player off the watch list."""
    client = ctx.client()
    server_id = ctx.require_server()
    record = client.find_player(server_id, player)
    if not ctx.preview("Stop watching a player", {"player": profile_name(record)}):
        return
    client.clear_watched(server_id, record["id"])
    ctx.console.print(f"[green]{profile_name(record)} is no longer watched.[/green]")


@watch_group.command("list")
@pass_ctx
def watch_list(ctx: Context):
    """Show watched players who are online right now.

    The API has no query for the whole watch list, so this is the online view.
    For an offline player, use "player show" and look at the watched line.
    """
    players = [p for p in ctx.client().get_online_players(ctx.require_server()) if p.get("watched")]

    def render():
        table = new_table("Player", "Steam id", "Reason", title=f"{len(players)} watched and online")
        for player in players:
            watched = (player.get("watched") or [{}])[0]
            table.add_row(profile_name(player), steam_id_of(player), cell(watched.get("reason")))
        ctx.console.print(table)
    ctx.emit(players, render)


@cli.group("note")
def note_group():
    """Notes attached to a player.

    They live on the server group rather than on one server.
    """


@note_group.command("list")
@click.argument("player")
@pass_ctx
def note_list(ctx: Context, player):
    """List the notes on a player."""
    record = ctx.client().get_player(ctx.require_server(), player)
    notes = record.get("notes") or []

    def render():
        table = new_table("Note id", "Type", "Content", "Admin", "When",
                          title=f"{len(notes)} notes on {profile_name(record)}")
        for note in notes:
            table.add_row(
                str(note.get("id")),
                str(note.get("type", "-")),
                cell(note.get("content")),
                admin_name(note),
                format_time(note.get("createdAt")),
            )
        ctx.console.print(table)
    ctx.emit(notes, render)


@note_group.command("add", cls=MutatingCommand)
@click.argument("player")
@click.argument("content")
@click.option("--type", "note_type", type=click.Choice(NOTE_TYPES, case_sensitive=False),
              default="Neutral", show_default=True, help="Tone of the note.")
@pass_ctx
def note_add(ctx: Context, player, content, note_type):
    """Add a note to a player.

    \b
        note add 76561198000000000 "Warned about mic spam" --type Negative
    """
    client = ctx.client()
    server_id = ctx.require_server()
    record = client.find_player(server_id, player)
    group_id = ctx.require_group()
    if not ctx.preview("Add a note", {
        "player": profile_name(record),
        "type": note_type,
        "content": cell(content),
    }):
        return
    result = client.create_note(group_id, record["id"], content, note_type.capitalize())
    ctx.console.print(f"[green]Note added.[/green] Id {result.get('id')}")


@note_group.command("edit", cls=MutatingCommand)
@click.argument("player")
@click.argument("note_id")
@click.argument("content")
@click.option("--type", "note_type", type=click.Choice(NOTE_TYPES, case_sensitive=False),
              default="Neutral", show_default=True, help="Tone of the note.")
@pass_ctx
def note_edit(ctx: Context, player, note_id, content, note_type):
    """Rewrite a note.

    The API wants the type again, so pass it if it should stay the same.
    """
    client = ctx.client()
    server_id = ctx.require_server()
    record = client.find_player(server_id, player)
    group_id = ctx.require_group()
    if not ctx.preview("Edit a note", {
        "player": profile_name(record),
        "note": note_id,
        "type": note_type,
        "content": cell(content),
    }):
        return
    client.edit_note(note_id, record["id"], group_id, content, note_type.capitalize())
    ctx.console.print("[green]Note updated.[/green]")


@note_group.command("delete", cls=MutatingCommand)
@click.argument("player")
@click.argument("note_id")
@pass_ctx
def note_delete(ctx: Context, player, note_id):
    """Delete a note for good."""
    client = ctx.client()
    server_id = ctx.require_server()
    record = client.find_player(server_id, player)
    group_id = ctx.require_group()
    if not ctx.preview("Delete a note", {"player": profile_name(record), "note": note_id}):
        return
    client.delete_note(note_id, record["id"], group_id)
    ctx.console.print("[green]Note deleted.[/green]")


# ---------------------------------------------------------------------------
# role
# ---------------------------------------------------------------------------

@cli.group("role")
def role_group():
    """Roles: list, create, edit, delete, sync."""


@role_group.command("list")
@pass_ctx
def role_list(ctx: Context):
    """List the roles in this group."""
    roles = ctx.client().list_roles(ctx.require_group())

    def render():
        table = new_table("Role", "Immunity", "Inherits", "Permissions", "Discord", "Id",
                          title=f"{len(roles)} roles")
        for role in sorted(roles, key=lambda r: -int(r.get("immunity") or 0)):
            table.add_row(
                role.get("name", "-"),
                str(role.get("immunity", "-")),
                (role.get("inherits") or {}).get("name", "-"),
                str(len(role.get("permissions") or [])),
                str(role.get("discordRoleId") or "-"),
                str(role.get("id")),
            )
        ctx.console.print(table)
    ctx.emit(roles, render)


@role_group.command("show")
@click.argument("role_id")
@pass_ctx
def role_show(ctx: Context, role_id):
    """Show one role and the permission nodes it carries."""
    roles = ctx.client().list_roles(ctx.require_group())
    role = next((r for r in roles if r.get("id") == role_id or r.get("name") == role_id), None)
    if not role:
        raise NotFoundError(f"No role matching {role_id!r} in this group.")

    def render():
        print_kv(ctx.console, role.get("name", "Role"), [
            ("Id", role.get("id")),
            ("Immunity", role.get("immunity")),
            ("Colour", role.get("color")),
            ("Aliases", ", ".join(role.get("aliases") or []) or "-"),
            ("Inherits", (role.get("inherits") or {}).get("name", "-")),
            ("Discord role", role.get("discordRoleId") or "-"),
        ])
        table = new_table("Permission", "Node")
        for permission in role.get("permissions") or []:
            table.add_row(permission.get("name", "-"), permission.get("node", "-"))
        ctx.console.print(table)
    ctx.emit(role, render)


@role_group.command("create", cls=MutatingCommand)
@click.argument("name")
@click.option("--immunity", default=0, show_default=True, help="Higher immunity outranks lower.")
@click.option("--color", default="#ffffff", show_default=True, help="Hex colour for the dashboard.")
@click.option("--alias", "aliases", multiple=True, help="In game alias. Repeat for more.")
@click.option("--inherits-id", help="Inherit permissions from this role.")
@click.option("--permission", "permissions", multiple=True, help="Permission id. Repeat for more.")
@click.option("--discord-role-id", help="Discord role to keep in step with this one.")
@pass_ctx
def role_create(ctx: Context, name, immunity, color, aliases, inherits_id, permissions, discord_role_id):
    """Create a role."""
    group_id = ctx.require_group()
    if not ctx.preview("Create a role", {
        "name": name, "immunity": immunity, "permissions": len(permissions),
    }):
        return
    role = ctx.client().create_role(
        group_id, name, immunity, list(aliases), color, inherits_id,
        list(permissions), discord_role_id,
    )
    ctx.console.print(f"[green]Created role {role.get('name')}.[/green] Id {role.get('id')}")


@role_group.command("edit", cls=MutatingCommand)
@click.argument("role_id")
@click.option("--name", help="New name.")
@click.option("--immunity", type=int, help="New immunity.")
@click.option("--color", help="New hex colour.")
@click.option("--alias", "aliases", multiple=True, help="Replace the alias list with these.")
@click.option("--inherits-id", help="New parent role.")
@click.option("--permission", "permissions", multiple=True, help="Replace the permission list with these.")
@click.option("--discord-role-id", help="New linked Discord role.")
@pass_ctx
def role_edit(ctx: Context, role_id, name, immunity, color, aliases, inherits_id, permissions, discord_role_id):
    """Edit a role.

    Lists you pass replace what was there, they do not add to it.
    """
    group_id = ctx.require_group()
    fields = {
        "name": name,
        "immunity": immunity,
        "color": color,
        "aliases": list(aliases) or None,
        "inherits_id": inherits_id,
        "permissions": list(permissions) or None,
        "discord_role_id": discord_role_id,
    }
    if all(value is None for value in fields.values()):
        raise MonoSuiteError("Nothing to change. Pass at least one option.")
    if not ctx.preview("Edit a role", {"role": role_id,
                                       "changes": ", ".join(k for k, v in fields.items() if v is not None)}):
        return
    role = ctx.client().edit_role(role_id, group_id, **fields)
    ctx.console.print(f"[green]Updated {role.get('name')}.[/green]")


@role_group.command("delete", cls=MutatingCommand)
@click.argument("role_id")
@pass_ctx
def role_delete(ctx: Context, role_id):
    """Delete a role.

    Everyone holding it simply stops holding it.
    """
    group_id = ctx.require_group()
    if not ctx.preview("Delete a role", {"role": role_id}):
        return
    ctx.client().delete_role(role_id, group_id)
    ctx.console.print("[green]Role deleted.[/green]")


@role_group.command("sync", cls=MutatingCommand)
@click.argument("role_id")
@click.option("--apply", "apply_changes", is_flag=True, help="Actually make the changes. Off means dry run.")
@pass_ctx
def role_sync(ctx: Context, role_id, apply_changes):
    """Reconcile a role against its linked Discord role.

    Runs as a dry run by default and prints what would move, so you can look
    before you leap.
    """
    group_id = ctx.require_group()
    if apply_changes and not ctx.preview("Reconcile role sync for real", {"role": role_id}):
        return
    result = ctx.client().reconcile_role_sync(group_id, role_id, dry_run=not apply_changes)

    def render():
        print_kv(ctx.console, "Role sync" + ("" if apply_changes else " (dry run)"), [
            ("Direction", result.get("direction")),
            ("Holders", result.get("holders")),
            ("Resolvable", result.get("resolvable")),
            ("Capped", result.get("capped")),
            ("Dispatched", result.get("dispatched")),
        ])
    ctx.emit(result, render)


# ---------------------------------------------------------------------------
# support, notifications, templates
# ---------------------------------------------------------------------------

@cli.group("support")
def support_group():
    """Support tickets raised against your organization."""


@support_group.command("list")
@click.option("--org-id", help="Which organization.")
@pass_ctx
def support_list(ctx: Context, org_id):
    """List support tickets."""
    requests = ctx.client().list_support_requests(org_id or ctx.require_org())

    def render():
        table = new_table("Title", "Type", "Status", "Created", "Linear",
                          title=f"{len(requests)} tickets")
        for request in requests:
            table.add_row(
                cell(request.get("title")),
                str(request.get("type", "-")),
                str(request.get("status", "-")),
                format_time(request.get("createdAt")),
                str(request.get("linearIssueIdentifier") or "-"),
            )
        ctx.console.print(table)
    ctx.emit(requests, render)


@support_group.command("submit", cls=MutatingCommand)
@click.argument("title")
@click.argument("description")
@click.option("--type", "request_type", default="support", show_default=True,
              help="Ticket type, as the dashboard uses it.")
@click.option("--org-id", help="Which organization.")
@click.option("--attachment", "attachments", multiple=True, help="Attachment URL. Repeat for more.")
@pass_ctx
def support_submit(ctx: Context, title, description, request_type, org_id, attachments):
    """Raise a support ticket."""
    org = org_id or ctx.require_org()
    if not ctx.preview("Submit a support request", {"org": org, "type": request_type, "title": title}):
        return
    result = ctx.client().submit_support_request(org, request_type, title, description, list(attachments))
    ctx.console.print(f"[green]Ticket created.[/green] Id {result.get('id')}")


@cli.command("notifications")
@click.option("--limit", "-n", default=25, show_default=True, help="How many to show.")
@pass_ctx
def notifications_cmd(ctx: Context, limit):
    """Show recent push notifications."""
    items = ctx.client().get_notifications(limit)

    def render():
        table = new_table("When", "Action", "Title", "Body", "Status",
                          title=f"{len(items)} notifications")
        for item in items:
            table.add_row(
                format_time(item.get("createdAt")),
                str(item.get("action", "-")),
                cell(item.get("title")),
                cell(item.get("body")),
                str(item.get("status", "-")),
            )
        ctx.console.print(table)
    ctx.emit(items, render)


def _template_entry(name: str, offense_class: Optional[str]) -> tuple:
    """Look up one template class, with helpful errors when it is not there."""
    matches = [key for key in BAN_TEMPLATES if key.lower() == name.lower()]
    if not matches:
        raise MonoSuiteError(
            f"No template called {name!r}. See: templates list"
        )
    template = BAN_TEMPLATES[matches[0]]
    if not offense_class:
        raise MonoSuiteError(
            f"Template {matches[0]} needs a class. Options: {', '.join(template)}"
        )
    key = offense_class.upper()
    if key not in template:
        raise MonoSuiteError(
            f"Template {matches[0]} has no class {key}. Options: {', '.join(template)}"
        )
    return template[key]


@cli.group("templates")
def templates_group():
    """The punishment ladder.

    Staff policy, not something the API enforces.
    """


@templates_group.command("list")
@pass_ctx
def templates_list(ctx: Context):
    """List every template and what each class does."""
    payload = {
        name: {
            key: {"seconds": seconds, "action": action, "reason": reason}
            for key, (seconds, action, reason) in classes.items()
        }
        for name, classes in BAN_TEMPLATES.items()
    }

    def render():
        table = new_table("Violation", "A", "B", "C", "D", title=f"{len(BAN_TEMPLATES)} templates")
        for name, classes in BAN_TEMPLATES.items():
            cells = []
            for key in ("A", "B", "C", "D"):
                entry = classes.get(key)
                if not entry:
                    cells.append("-")
                    continue
                seconds, action, _ = entry
                cells.append("warn" if action == "warn" else format_duration(seconds))
            table.add_row(name, *cells)
        ctx.console.print(table)
        ctx.console.print("[dim]Use with: ban add <player> -t \"<violation>\" --class B[/dim]")
    ctx.emit(payload, render)


@templates_group.command("show")
@click.argument("name")
@pass_ctx
def templates_show(ctx: Context, name):
    """Show one template in full.

    Includes the reason text it fills in.
    """
    matches = [key for key in BAN_TEMPLATES if key.lower() == name.lower()]
    if not matches:
        raise MonoSuiteError(f"No template called {name!r}. See: templates list")
    classes = BAN_TEMPLATES[matches[0]]

    def render():
        table = new_table("Class", "Action", "Duration", "Reason", title=matches[0])
        for key, (seconds, action, reason) in classes.items():
            table.add_row(key, action, format_duration(seconds), reason)
        ctx.console.print(table)
    ctx.emit(classes, render)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI and turn our own errors into tidy messages.

    click is left in non standalone mode so that MonoSuiteError can be caught
    here and printed as one red line, rather than as a traceback.
    """
    console = Console(stderr=True)
    try:
        cli.main(args=list(argv) if argv is not None else None, standalone_mode=False)
        return 0
    except click.exceptions.Exit as exit_signal:
        return int(exit_signal.exit_code)
    except click.Abort:
        console.print("[yellow]Cancelled.[/yellow]")
        return 130
    except KeyboardInterrupt:
        console.print("[yellow]Cancelled.[/yellow]")
        return 130
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except AuthError as error:
        console.print(f"[red]Auth problem:[/red] {error}")
        return 1
    except NotFoundError as error:
        console.print(f"[red]Not found:[/red] {error}")
        return 1
    except ApiError as error:
        console.print(f"[red]The API said no:[/red] {error}")
        return 1
    except MonoSuiteError as error:
        console.print(f"[red]Error:[/red] {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
