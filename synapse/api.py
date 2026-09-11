import os
from pathlib import Path

from graphql import build_client_schema, get_introspection_query, parse, validate
from graphql.language import OperationType, OperationDefinitionNode

from monosuite_cli import (
    ApiError,
    AuthError,
    Config,
    MonoSuiteClient,
    token_from_browser,
)

PERSON = "id steamId profile { username }"
ACTION = f"id reason createdAt user {{ {PERSON} }} admin {{ {PERSON} }} server {{ id }}"
LOG_FIELDS = f"id serverId timestamp category message participants {{ {PERSON} }}"
QUERIES = {
    "categories": """query LogCategories($server: ID!) {
        server(serverId: $server) { loggingCategories }
    }""",
    "logs": f"""query InteractionLogs($server: ID!, $data: GetLogsInput!) {{
        server(serverId: $server) {{ logs(data: $data) {{ total scrollId logs {{ {LOG_FIELDS} }} }} }}
    }}""",
    "servers": """query ServerDirectory { organizations { id name groups {
        id name servers { id name isOnline }
    } } }""",
    "presence": f"""query Presence($server: ID!) {{ server(serverId: $server) {{
        id name serverGroupId isOnline onlinePlayers {{ {PERSON} }}
    }} }}""",
    "bans": f"""query History($group: ID!, $limit: Int, $offset: Int) {{
        group(groupId: $group) {{ bans(limit: $limit, offset: $offset) {{
            total bans {{ {ACTION} expire unbannedAt unbanReason editedAt serverGroupWide }}
        }} }} }}""",
    "player": f"""query PlayerHistory($server: ID!, $value: String!) {{
        server(serverId: $server) {{ player(value: $value) {{ {PERSON}
            notes {{ id content type createdAt updatedAt userId admin {{ {PERSON} }} }}
            warnings {{ {ACTION} points active updatedAt }}
            kicks {{ {ACTION} }}
        }} }} }}""",
    "blacklists": f"""query Blacklists($server: ID!) {{ server(serverId: $server) {{
        blacklists {{ {ACTION} value expire updatedAt }}
    }} }}""",
}
QUERIES["player_without_kicks"] = QUERIES["player"].replace(f"kicks {{ {ACTION} }}", "")
INTROSPECTION = get_introspection_query(descriptions=False)
ALLOWED = frozenset([*QUERIES.values(), INTROSPECTION])


def read_only(query, variables=None):
    if query not in ALLOWED:
        raise ValueError("Only the fixed Synapse read queries are allowed")
    for definition in parse(query).definitions:
        if isinstance(definition, OperationDefinitionNode):
            if definition.operation != OperationType.QUERY:
                raise ValueError("Mutations and subscriptions are forbidden")


def token_provider():
    path = os.environ.get("SYNAPSE_TOKEN_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    if os.environ.get("SYNAPSE_BROWSER"):
        return token_from_browser(os.environ["SYNAPSE_BROWSER"]) or ""
    return os.environ.get("MONOSUITE_TOKEN") or Config().load().get("token") or ""


def make_client():
    return MonoSuiteClient(
        token_provider(),
        token_provider=token_provider,
        timeout=20,
        on_request=read_only,
    )


class Source:
    def __init__(self, client):
        self.client = client
        self._kick_restricted_token = None

    def check_schema(self):
        schema = self.client.execute(INTROSPECTION)
        built = build_client_schema(schema)
        for name, query in QUERIES.items():
            errors = validate(built, parse(query))
            if errors:
                raise ValueError(f"Schema incompatible with {name}: {errors}")
        return schema

    def query(self, name, **variables):
        limited = name == "player" and self._kick_restricted_token == self.client.token
        try:
            result = self._execute(
                "player_without_kicks" if limited else name, variables
            )
        except ApiError as exc:
            if (
                name != "player"
                or str(exc) != "This credential is not scoped for: moderation.kick"
            ):
                raise
            self._kick_restricted_token = self.client.token
            result = self._execute("player_without_kicks", variables)
            limited = True
        if limited:
            result["unavailable_history"] = ["kicks"]
        return result

    def _execute(self, name, variables):
        try:
            return self.client.execute(QUERIES[name], variables)
        except AuthError:
            # The CLI retries HTTP auth failures, but not GraphQL auth errors.
            if self.client.refresh_token():
                return self.client.execute(QUERIES[name], variables)
            raise


def timestamp_ms(value, nullable=False, expiry=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean timestamp")
    number = int(value)
    maximum = 253402300799999 if expiry else 4102444800000
    if number < 946684800000 or number > maximum:
        raise ValueError("Timestamp is outside the expected millisecond range")
    return number
