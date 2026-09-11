# BattleMetric

BattleMetric is a modular Redbot cog for calling the BattleMetrics API from Discord.

## Layout

- `battlemetric.py` owns Red Config, cog lifecycle, and shared helper methods.
- `api.py` owns all BattleMetrics HTTP behavior.
- `authorization.py` owns reusable authorization checks for commands and modules.
- `commands_mixin.py` owns Discord commands and display formatting.
- `module/` contains self-contained, expandable features. Each module owns its
  own configuration, refresh behavior, and command mixin.

This keeps endpoint expansion simple: add a method to `BattleMetricsClient`, then add a command or background workflow that calls it.

## Commands

- `[p]battlemetric` or `[p]bm` - Authorized member. Show current guild settings.
- `[p]bm auth add @member` - Server manager or bot owner. Authorize a member to use BattleMetric commands.
- `[p]bm auth remove @member` - Server manager or bot owner. Revoke a member's authorization.
- `[p]bm auth list` - Server manager or bot owner. List authorized members.
- `[p]bm setserver <server_id>` - Authorized member. Set this guild's default BattleMetrics server.
- `[p]bm clearserver` - Authorized member. Clear this guild's default server.
- `[p]bm setgame <game>` - Authorized member. Set default search game, such as `rust`, `ark`, `squad`, `dayz`, or `arma3`.
- `[p]bm cleargame` - Authorized member. Clear the default search game.
- `[p]bm server [server_id]` - Authorized member. Show server status. Uses the configured default when no ID is supplied.
- `[p]bm search <search> [game] [limit]` - Authorized member. Search servers.
- `[p]bm player <player_id>` - Authorized member. Show a player by BattleMetrics ID.
- `[p]bm rawget <path> [params_json]` - Bot owner only. Raw GET helper for testing new endpoints.
- `[p]serverinfo setup #channel` - Authorized member. Create the automatic Server Info panel in the mentioned text channel.
- `[p]serverinfo modify <message_id>` - Authorized member. Run this in the channel containing a message sent by this bot to adopt and update that message as the panel.
- `[p]killfeed configure <host> <port>` - Authorized member. Configure the HLL: Vietnam RCON endpoint and disable the feed until setup is run.
- `[p]killfeed setup #channel` - Authorized member. Test RCON and enable pooled kill-feed messages in the mentioned channel.
- `[p]killfeed status` - Authorized member. Show the endpoint, channel, connection-secret status, and pending queue size.
- `[p]killfeed stop` - Authorized member. Disable the feed and discard queued events.
- `/link` - Any guild member. Create a private, five-minute token used to verify and link their Discord and HLL accounts.
- `/vnstat`, `/vnstat member:@member`, or `/vnstat eos_id:EOS_ID` - Any guild member. Show their own, a member's, or a direct game account's HLL statistics.

## Authorization

BattleMetric commands are denied by default. A bot owner or member with the
Administrator or Manage Server permission can grant access with:

```text
[p]bm auth add @member
```

Only explicitly authorized members and bot owners can then use BattleMetric
commands, including Server Info setup and modification. Server managers can
always manage the authorization list, even when they are not themselves on it.

## Server Info Panel

Set a default BattleMetrics server before setting up a panel:

```text
[p]bm setserver <server_id>
[p]serverinfo setup #server-status
```

The panel is an embed that refreshes at most once every 60 seconds. It shows the
server name, address, IP, port, status, player count, and the current player
names when BattleMetrics returns them. Some games and servers do not expose a
player list; the panel reports that limitation instead of showing incomplete
data. Player names are distributed across two inline columns to show more names
without making the panel excessively tall.

Each Discord guild has one tracked Server Info panel. Running `setup` creates a
new panel and replaces the previously tracked one. A panel is pinned to the
server selected during setup, so change the default server and run `modify` on
the panel message when you want it to track a different server.

## HLL: Vietnam Kill Feed

The kill feed connects directly to the game server's RCON port through the
external [`hllrcon`](https://github.com/timraay/hllrcon) package. It does not
read the BattleMetrics web console and does not consume BattleMetrics API
requests. `hllrcon` requires Python 3.11 or newer; with the current stable Red
3.5 release, run the bot on Python 3.11.

Downloader installs the pinned `hllrcon` dependency into Red's environment when
the cog is installed or updated. For a local checkout where dependencies were
not installed by Downloader, the bot owner can install the same release range
from Discord and then restart Red:

```text
[p]load downloader
[p]pipinstall "pydantic>=2.11.5,<2.12" "hllrcon>=2.0.0.4,<2.0.1"
```

The version range follows `hllrcon`'s compatibility guidance: it accepts patch
fixes but does not automatically cross an HLL server compatibility boundary.
The Pydantic range keeps its compiled core compatible with the
`typing_extensions` version used by stable Red 3.5.

Store the RCON password in Red's shared API-token vault from a private channel
or DM. The password is global to this Red instance and is never placed in the
guild configuration:

```text
[p]set api hllrcon password,YOUR_RCON_PASSWORD
```

Then configure the RCON endpoint and target Discord channel:

```text
[p]killfeed configure RCON_HOST RCON_PORT
[p]killfeed setup #kill-feed
```

Use the server's RCON port, which may differ from its public game/query port.
The Red host must be allowed through the game host's RCON firewall or allowlist.
Because BattleMetrics may already hold an RCON connection, verify that the game
host permits another concurrent RCON client.

`hllrcon` 2.0.0.4 performs a strict transport type check that rejects the
`uvloop` TCP transport used by Red on Linux, even though that transport provides
the required asyncio interface. BattleMetric applies a narrow compatibility
shim for that exact `uvloop` error. Seeing `Accepted uvloop TCP transport for
HLL RCON` in Red's log confirms that the workaround was used.

If `Connection is closed` remains after that log line, the connection reached
the actual RCON handshake. Confirm RCON is enabled in the host panel, restart
the game server after changing its RCON password or port, and temporarily
disconnect BattleMetrics or other RCON tools before retrying `killfeed setup`.
This isolates a concurrent-client limit from a password or server configuration
problem.

The setup test reports its failing stage without exposing the password:

- `RCON V2 handshake` covers session creation and password authentication.
- `GetAdminLog request` confirms that the authenticated account can read the
  admin log used by the feed.

The module polls every three seconds and places new kills and team kills into a
bounded in-memory queue. A separate worker sends at most one pooled embed per
configured guild every three seconds. Overlapping RCON lookbacks are
deduplicated, temporary connection failures use exponential retry delays, and
the lookback expands up to five minutes to recover missed events. Queued and
deduplication state is intentionally in memory, so reloading the cog starts a
fresh feed rather than replaying old logs.

Each batch contains up to 50 events within Discord's embed size limit. If more
than 500 events accumulate while Discord is unavailable, the oldest pending
events are discarded and the next successful message reports how many were
dropped. Player names cannot create Discord mentions.

If Red reports `cannot import name 'Sentinel' from 'typing_extensions'`, update
the cog so Downloader receives the Pydantic compatibility pin. If the old
dependency remains in Downloader's library, run the `pipinstall` command above
as the bot owner and fully restart the Red process. BattleMetric now loads
without its kill-feed workers when `hllrcon` is broken, allowing Server Info
and the BattleMetrics API commands to remain available while the dependency is
repaired.

## HLL Database And Account Linking

The database module uses PostgreSQL through `asyncpg`. Downloader installs the
dependency with the cog. The database password is never written to Red Config
or this repository; store it in Red's shared API-token vault from a private
channel or DM:

```text
[p]set api battlemetric_db user,DB_USER password,DB_PASSWORD host,162.120.6.39 port,5432 database,slhhll schema,slhhll
```

Only `user` and `password` are required. Host `162.120.6.39`, port `5432`,
database `slhhll`, and schema `slhhll` are the defaults. Reload the cog after
initial configuration:

```text
[p]reload BattleMetric
[p]slash enablecog BattleMetric
[p]slash sync
```

The slash enable/sync steps are required the first time `/link` is installed.
Discord may take a few minutes to display a newly synchronized command.

The database role needs `SELECT`, `INSERT`, `UPDATE`, and `DELETE` on
`slhhll."Discord"` and `slhhll."RCON_DATA"`. The supplied upserts also require
these database constraints:

- `slhhll."Discord"."Discord_Id"` must be a primary key or unique.
- `slhhll."Discord"."EOS_Id"` should be unique so one game account cannot be
  assigned to multiple Discord accounts.
- `slhhll."RCON_DATA"."EOS_Id"` must be a primary key or unique.
- If a foreign key is used, `Discord.EOS_Id` should reference
  `RCON_DATA.EOS_Id`. `RCON_DATA.EOS_Id` must not reference `Discord.EOS_Id`,
  because statistics exist before a Discord account is linked.

Run [`sql/hll_constraints.sql`](sql/hll_constraints.sql) once as the database
owner to validate existing data, normalize the counter columns, remove the
reverse foreign key, and install these constraints. The migration is
transactional and refuses to silently merge duplicate account identifiers.

Members run `/link` and receive an ephemeral `VN-####` token. They send that
token in Unit or Team chat on the configured HLL server within five minutes.
The shared RCON poller reads the player ID from the chat event, performs the
Discord/EOS upsert in a transaction, burns the token, and sends a confirmation
DM. If DMs are closed, it posts the result in the channel where `/link` was
used.

Player-connect, kill, and death records use the same RCON response as the
Discord kill feed, so the database module does not open a second RCON
connection. A `CONNECTED` event creates an `RCON_DATA` row with zero kills and
deaths. Every kill then increments the attacker's `Kill` value and the victim's
`Dead` value whether or not either player has linked Discord.

Records enter a bounded `asyncio.Queue` and are committed in one transaction
when 50 records accumulate or three seconds pass. A failed batch is retained
and retried with backoff. Overlapping RCON lookbacks and duplicate guild
configurations for the same endpoint are deduplicated before queueing. Database
ingestion continues when the Discord kill feed is disabled, provided the RCON
endpoint, RCON password, and database credentials are configured.

Account linking only adds the Discord-to-EOS mapping. Existing `RCON_DATA`
statistics remain intact when a member links, relinks, unlinks, or requests
deletion of their Discord mapping.

## Player Statistics

`/vnstat` is public to guild members and supports three lookup forms. Discord
slash-command options are named, so member and EOS lookups use separate native
fields:

- `/vnstat` looks up the invoking member's linked Discord ID. If they are not
  linked, the bot directs them to `/link`.
- `/vnstat member:@member` looks up another member's linked Discord ID.
- `/vnstat eos_id:EOS_ID` reads an existing `RCON_DATA` row directly, whether
  or not that game account has linked Discord.

The embed always reads kills and deaths from PostgreSQL. It uses the most
recent alias observed by the shared RCON poller when available. With a
BattleMetrics API token configured, it also matches the EOS identifier to a
BattleMetrics player and requests that player's time on the guild's configured
default BattleMetrics server. BattleMetrics names and playtime are cached for
five minutes. Missing or temporarily unavailable BattleMetrics data is shown
as unavailable without hiding the locally stored combat statistics.

`Enlisted Date` is the linked member's join date for the Discord server where
the command is used. A direct EOS lookup that is not linked to a member shows
that the Discord-specific fields are not linked.

## Token

BattleMetrics uses OAuth bearer tokens. Public server endpoints can often be queried without a token, but protected RCON, ban, note, and organization data requires one.

Store the token with Red's shared API-token vault. Run this as the bot owner in
a private channel or DM:

```text
[p]set api battlemetrics api_key,YOUR_TOKEN
```

The cog reads the `battlemetrics` service's `api_key` value from the vault. On
its first load after this update, any token stored by the earlier cog version is
migrated into that vault and removed from cog Config.

## Expanding API Calls

Add focused wrappers in `api.py`:

```python
async def get_ban(self, ban_id: str) -> Dict[str, Any]:
    return await self.get(f"/bans/{ban_id}")
```

Then call that wrapper from a command, task, or service module. Avoid building URLs directly in command methods unless it is the owner-only `rawget` command.
