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
- `/hllvn addvip member:@member duration:1d` - Authorized member. Add a linked Discord member to the HLL VIP list for a limited duration.
- `/hllvn addvip eos_id:EOS_ID duration:1d` - Authorized member. Add a game account directly to the HLL VIP list for a limited duration.
- `/hllvn ban member:@member duration:2d reason:REASON` - Authorized member. Ban a linked member through BattleMetrics and in-game RCON.
- `/hllvn ban eos_id:EOS_ID reason:REASON` - Authorized member. Permanently ban a game account through both systems.
- `/hllvn unban member:@member` - Authorized member. Remove a linked member's server ban from BattleMetrics and in-game RCON.
- `/hllvn unban eos_id:EOS_ID` - Authorized member. Remove a game account's server ban from both systems.
- `/hllvn mesg target:@member mesgs:MESSAGE` - Authorized member. Send a private in-game popup to a linked member.
- `/hllvn mesg target:EOS_ID mesgs:MESSAGE` - Authorized member. Send a private in-game popup directly by game account ID.
- `/hllvn mesg target:ALL mesgs:MESSAGE` - Authorized member. Send an in-game popup to every connected player.
- `/hllvn joinnotice test:EOS_ID` - Authorized member. Send the join welcome and database counters to an online player.
- `/hllvn joinnotice toggle:<true|false>` - Authorized member. Control automatic private welcome popups; omit both options to inspect the setting.
- `/hllvn discordstat [toggle:bool] [headcount_channel:VoiceChannel] [kills_channel:VoiceChannel] [deaths_channel:VoiceChannel] [sync_now:bool]` - Discord Administrator. Configure automatic server totals and optionally refresh immediately; omitted options keep saved settings.
- `/hllvn changemap map_name:<map>` - Authorized member. Submit a map change immediately using a specific map/mode or exact server map ID; the game server's countdown still applies.
- `/hllvn mapvote vote_start_minutes:20 number_of_options:5 enable:true duration_minutes:10` - Authorized member. Schedule in-game voting; omit duration to vote until match end.
- `/hllvn mapchange sync` - Authorized member. Refresh the bot's cached server rotation without changing existing ballots.
- `/hllvn mapchange startvote` - Authorized member. Start this match's vote immediately using the saved options and duration settings.
- `/hllvn mapchange status` - Authorized member. Privately inspect accepted ballots, the winner, recorded/live map IDs, and the actual server next map without changing it.
- `/hllvn admin group name:Admin` - Authorized member. Set the server-defined group used for future promotions.
- `/hllvn admin groups` - Authorized member. Read available server admin groups and the configured promotion group.
- `/hllvn admin promote eos_id:EOS_ID confirm:false` - Authorized member. Preview granting in-game admin access; use `confirm:true` to apply.
- `/hllvn admin demote eos_id:EOS_ID confirm:false` - Authorized member. Preview removing in-game admin access; use `confirm:true` to apply.
- `/hllvn giveseedvip duration:2d` - Authorized member. Reward current players with timed VIP, a private in-game popup, and the organization's BattleMetrics Seeder flag.
- `/hllvn purgevip confirm:false` - Authorized member. Preview server VIPs not tracked by the bot's timed grants or rewards.
- `/hllvn purgevip confirm:true` - Authorized member. Remove those unmanaged VIPs through throttled RCON requests.
- `/hllvn allowvipteamswap toggle:<choice>` - Authorized member. Enable or disable the VIP-only in-game `!changeteam` command.
- `/hllvn adminping role:@role toggle:<choice>` - Authorized member. Configure Discord staff alerts for the in-game `!admin` command.
- `/hllvn tkwatch toggle:<choice> channel:#channel threshold_per_min:3 watch_duration:15 exclude_commander:true role:@role` - Authorized member. Configure automatic team-kill warnings, timed watches, staff actions, and an optional role ping.
- `/hllvn seeding stage_one_players:60 stage_two_players:75 penalty_type:<choice> toggle:<choice> warning_seconds:15` - Authorized member. Configure two-stage Warfare and Offensive seeding protection with an optional 5-30 second warning duration.
- `/hllvn hqprotection penalty_type:<choice> toggle:<choice>` - Authorized member. Protect each team's locked HQ sector from enemies.
- `/hllvn buyvip` - Any guild member. Exchange confirmed kills for timed HLL VIP access.
- `[p]dogtag setup #channel` - Authorized member. Set the staff review channel for custom dog-tag submissions.
- `[p]dogtag status` - Authorized member. Show storage, IPC, and pending-review status.
- `/link` - Any guild member. Create a private, five-minute token used to verify and link their Discord and HLL accounts.
- `/vnstat`, `/vnstat member:@member`, or `/vnstat eos_id:EOS_ID` - Any guild member. Show their own, a member's, or a direct game account's HLL statistics.

## Authorization

Administrative BattleMetric commands are denied by default. `/link`, `/vnstat`,
and `/hllvn buyvip` remain available to every guild member. A bot owner or
member with the Administrator or Manage Server permission can grant
administrative access with:

```text
[p]bm auth add @member
```

Only explicitly authorized members and bot owners can then use BattleMetric
commands, including Server Info setup and modification. Server managers can
always manage the authorization list, even when they are not themselves on it.
`/hllvn discordstat` uses Discord Administrator permission instead of `bm auth`,
as explicitly required for this channel-management command. This permission is
checked at runtime before deferring or accessing settings; its replies are private.

## Discord Voice Channel Statistics

Configure three existing, distinct voice channels in this guild:

```text
/hllvn discordstat toggle:true headcount_channel:"Head Count" kills_channel:"Kills" deaths_channel:"Deaths"
/hllvn discordstat sync_now:true
/hllvn discordstat toggle:false
```

The bot needs effective **View Channel**, **Connect**, and **Manage Channels**
permissions in each selected voice channel. Discord implicitly removes Manage
Channels from a voice channel when Connect is denied, even if Manage Channels
is granted by a role or category. You can allow Connect for the bot's role while
keeping it denied for `@everyone`. Category overrides apply to channels synced
with that category; an unsynced channel's own overrides can differ. Enabling
identifies affected channels and missing permissions before saving any changes.
Omitted options retain their values; omit every option to inspect the settings.
Disabling retains the channel IDs and their last names. The feature creates no
channels and does not change their access permissions.

The worker waits for bot readiness, then refreshes
`slhhll.mv_server_summary` once per cycle through the existing asyncpg pool and
vaulted `battlemetric_db` credentials. All enabled guilds display the same database
totals, independently of their configured BattleMetrics/RCON server. The view
must return exactly one row with `total_unique_players`, `total_kills`, and
`total_deaths`. Null counters display as zero. The worker formats channel names as:

- `👥 Head Count: 1,234`
- `⚔️ Kills: 56,789`
- `💀 Deaths: 45,678`

After each cycle completes, the worker waits ten minutes before starting another
cycle to avoid rename bursts after slow requests. Enabling without `sync_now`
only saves settings, so the first update may wait until the next worker cycle.
Use `sync_now:true` to refresh the database and attempt updates immediately;
stats must be enabled. The private reply reports database errors and each
channel's update, unchanged name, cooldown, or failure. Sync requests serialize
database refreshes with the worker and recheck that stats are still enabled.
Manual database refresh attempts are limited to one per 30 seconds across
guilds; a request during an existing refresh asks the administrator to retry.
Each changed channel is renamed at most once per ten minutes by this feature,
including across manual syncs and cog reloads. The cooldown is persisted before
the request because a lost response may mean Discord applied the rename. A known
permission or deleted-channel denial releases that cooldown; other failures keep
it until retry is safe. Each channel is edited only when its current name differs.
Database failures preserve the last
names, and deleted channels, permission failures, or Discord HTTP errors do not
stop future cycles or prevent the remaining channels from being attempted.
Missing channels are skipped until an administrator selects replacements.

The database operator must provision and populate the materialized view, give
the existing database role permission to refresh it and select its counters,
and create a qualifying UNIQUE index over ordinary columns covering all rows.
These are requirements of
[`REFRESH MATERIALIZED VIEW CONCURRENTLY`](https://www.postgresql.org/docs/current/sql-refreshmaterializedview.html).
PostgreSQL 17 and newer require the view's `MAINTAIN` privilege for refresh;
PostgreSQL 16 and older require the database role to own the view. The role also
needs `USAGE` on the schema and `SELECT` on the view. Granting `SELECT` alone
does not allow a refresh.
The cog does not create or alter the view or fall back to a blocking refresh.
Refreshes have a 120-second query timeout; failures retry on the next cycle.
Manual sync reports a sanitized reason when PostgreSQL supplies a recognizable
error code; unknown failures include the operation stage and SQLSTATE only.
For a read-only database-side check, connect to the same database and run:

```sql
SELECT current_database(), current_user, version(),
       to_regclass('slhhll.mv_server_summary') AS summary_relation;
SELECT matviewowner, ispopulated FROM pg_matviews
WHERE schemaname = 'slhhll' AND matviewname = 'mv_server_summary';
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = 'slhhll' AND tablename = 'mv_server_summary';
```

These queries do not change the view. Verify that a qualifying index uses
ordinary view columns, is unique, and has no WHERE clause. If the view does not
exist, its owner must provision it with the three documented counters; the cog
does not know the intended view definition and will not create one automatically.

Per-guild Red Config stores `discord_stats.enabled`, `headcount_id`, `kills_id`,
`deaths_id`, and recent channel rename cooldown deadlines. No additional personal records or credentials are stored; only
aggregate totals are published in channel names. The worker starts/stops with
the cog and uses the existing pool's token-refresh and shutdown handling.
After updating, run `[p]reload BattleMetric`, `[p]slash enablecog BattleMetric`
if needed, and `[p]slash sync`. Test setup, denial without Administrator,
unchanged names, a deleted/forbidden channel, and reload persistence in Red's
actual environment.

After adding `sync_now`, reload BattleMetric and run `[p]slash sync` so Discord
registers the new option.
Updates to the database diagnostics only require `[p]reload BattleMetric`.

## In-Game Admin Access

Only members authorized through `[p]bm auth add @member` and bot owners can use
any `/hllvn admin` command. Discord Administrator alone does not grant access.
Authorization is checked before deferring, reading settings, or contacting RCON.
These commands manage the game's admin list, not the bot's authorization list,
Discord roles, or BattleMetrics organization staff.

RCON promotion requires an existing group defined in the game server's
configuration. Inspect available groups, then select one for this Discord guild:

```text
/hllvn admin groups
/hllvn admin group name:Admin
```

`groups` makes one read-only RCON request and replies privately with the server's
group names, the configured promotion group, and whether that selection is
available, missing, or ambiguous. RCON returns names only, not each group's
permission definitions; inspect the server configuration for those permissions.
Long lists are returned in a complete text attachment instead of being truncated
or sending a burst of messages. This command never changes access or settings.

The default is `Admin`. Group names are matched case-insensitively against
`GetAdminGroups`, preserving the server's spelling; missing or ambiguous groups
are rejected. Use a group with the intended permissions, not a more privileged
owner group. Changing this setting does not alter existing admins.

Preview or confirm a specific game account:

```text
/hllvn admin promote eos_id:EOS_ID confirm:false
/hllvn admin promote eos_id:EOS_ID confirm:true
/hllvn admin demote eos_id:EOS_ID confirm:false
/hllvn admin demote eos_id:EOS_ID confirm:true
```

`confirm` defaults to `false`. Previews read the server list but never grant or
remove access. IDs accept the existing 17-digit game-account or 32-character EOS
format; linking to Discord is not required. Existing admins keep their current
group when promoted, including more privileged admins. Demoting an account not
on the admin list is a no-op.

Confirmed requests read, change, and verify the exact account in one shared RCON
operation under its per-guild lock. A promotion uses `AddAdmin` and a demotion
uses `RemoveAdmin`; success is reported only after `GetAdminUsers` verifies it.
Replies are private. An interrupted request or mismatched read-back is not
automatically retried, because the write may already have changed access. Check
the game server's admin list before retrying. There is no new poller or
BattleMetrics API traffic.

The group setting persists in per-guild Red Config. Grants persist in the game
server's admin list and are not tracked as bot-managed access; bot reloads and
Red user-data deletion do not revoke them. Use a confirmed demotion to remove
access. After updating, run `[p]reload BattleMetric` and `[p]slash sync` to expose
the new subgroup.

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
[p]pipinstall "Pillow>=11,<13" "pydantic>=2.11.5,<2.12" "hllrcon>=2.0.0.4,<2.0.1"
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

## Two-Stage Seeding Protection

Only bot owners and members authorized through `[p]bm auth add @member` can
configure this rule. It uses the same HLL: Vietnam RCON endpoint, password,
client, and connection lock as the kill feed. The Discord kill-feed channel
does not need to be enabled.

Configure both population stages, penalty behavior, and the toggle through the
slash-command choices. Stage 1 must be lower than Stage 2:

```text
/hllvn seeding stage_one_players:60 stage_two_players:75 penalty_type:"Warning, then punish" toggle:Enable warning_seconds:15
/hllvn seeding stage_one_players:60 stage_two_players:75 penalty_type:"Punish immediately" toggle:Enable
/hllvn seeding stage_one_players:60 stage_two_players:75 penalty_type:"Warning, then punish" toggle:Disable
```

While enabled on Warfare, the initial stage locks each side's fourth and fifth
sectors. Reaching `stage_one_players` unlocks the fourth sector while the fifth
remains locked. Reaching `stage_two_players` fully unlocks the map and suspends
seeding enforcement. If population later drops below a threshold, the matching
locks resume after the next status refresh.
Reaching a threshold does not permanently unlock objectives or disable the saved
configuration. Each downward stage change triggers a fresh position scan, so
established players already standing in a newly locked sector are also checked.

On Offensive, only the attacking team is restricted. Before Stage 1, attackers
may advance through the second objective while the third through fifth sectors
remain locked. Reaching Stage 1 opens the third objective while the fourth and
fifth remain locked. Stage 2 fully unlocks the map. The defending team is never
restricted by the seeding rule. `hllrcon` supplies the attacking-team identity,
so the cog does not infer it from player names or scores.

Mirrored map direction is handled from `hllrcon` map metadata. Unassigned
players and invalid or zero positions are ignored. The RCON response does not
identify which one of a sector's possible strongpoints is active, so the complete applicable
capture sectors are protected rather than guessing the active point. Protection
uses RCON warnings and kills, not a native capture-disable switch, and cannot undo
objectives already captured during the status-refresh delay.

To avoid alarming joining or team-switching players, territory enforcement
waits silently for ten seconds after first observing a player, a team change,
or a change in their death counter. It also requires a changed, valid position
after that transition. Unchanged coordinates remain silent even after ten
seconds, since RCON can retain a position while a player is waiting to spawn.
Leaving the server or becoming unassigned clears readiness. The same guard
applies to HQ protection and immediate-punishment mode. Only after the guard
passes does the configured warning countdown begin.

The RCON player response has no explicit alive/spawned flag; this guard uses
observed team, death counter, and position changes rather than claiming exact
life-state detection. Players first seen while stationary become eligible once
a position change is observed. These observations stay in memory and are
cleared on reload, layer changes, and RCON failures. Readiness is retained through
Stage 2 instead of treating established players as fresh joins when counts drop.
While fully unlocked with HQ protection inactive, one roster snapshot at each
60-second status refresh tracks observed joins, departures, team changes, deaths,
and movement without warning or punishing anyone. Transitions that occur entirely
between snapshots cannot always be detected, since RCON has no alive flag.

With warning-to-punish selected, a violating player receives an RCON warning
approximately once per second for the configured `warning_seconds` duration
(5-30 seconds). This setting is saved per guild and applies to both seeding
stages on Warfare and Offensive. Omitting it keeps the saved duration; existing
configurations and new guilds default to five seconds. Players are killed only
after a fresh position scan confirms they are still in the protected sector.
The three-second position scan can extend the actual wait past the configured
duration.
Immediate punishment skips the warning period. A player is not punished again
until a scan observes them leave the sector and later return.

Run `[p]reload BattleMetric` to load this fix. Only run `[p]slash sync` if your
installation does not yet expose the `warning_seconds` option; this fix does not
change command arguments.

Player count, game mode, and stage are checked once every 60 seconds. Player-position
requests run every three seconds only while the cached status says seeding or HQ
protection is active. When both are inactive at Stage 2, only the one roster
snapshot per status refresh is requested. This means crossing either population
threshold can take up to 60 seconds to change enforcement. Warnings and punishments are
serialized through the shared RCON lock, capped at 20 actions per rule in each
worker pass, and position-request failures back off to 60 seconds. This module
does not call BattleMetrics and sends no periodic Discord messages.

Configurations saved by the former single-threshold command use the old
`min_players` value as Stage 1 and default Stage 2 to 75 until an authorized
member saves the new command.

## HQ Protection

`/hllvn hqprotection` is restricted to bot owners and members authorized with
`[p]bm auth add @member`. It shares the seeding worker, status cache, player
position response, RCON connection, and connection lock, so enabling both
features does not create duplicate polling loops.

```text
/hllvn hqprotection penalty_type:"Warning for 5 seconds, then punish" toggle:Enable
/hllvn hqprotection penalty_type:"Punish immediately" toggle:Enable
/hllvn hqprotection penalty_type:"Warning for 5 seconds, then punish" toggle:Disable
```

The rule runs on Warfare layers and protects both sides. On normal maps,
Axis/North players are violations inside the Allies/South home sector, while
Allies/South players are violations inside the Axis/North home sector. That
mapping reverses automatically on mirrored maps. Defenders and unassigned
players are ignored, and the shared join/team-change/death guard suppresses
enforcement until a fresh position is observed after settling.

HLL RCON exposes player positions and sector geometry but not the individual HQ
spawn locations. For predictable enforcement, this command protects the
complete home HQ sector rather than guessing a radius around each spawn. A
side's protection automatically unlocks when the enemy controls four
objectives, allowing the final objective to be attacked normally. Objective
scores come from the shared 60-second status cache, so an unlock can take up to
60 seconds to be observed.

Warning-to-punish sends an RCON warning approximately once per second for at
least five seconds, then requires a fresh position scan before killing the
player. Immediate punishment skips the warning period. The shared player list
is requested every three seconds only while seeding or HQ protection is active;
the command sends no periodic Discord messages and makes no BattleMetrics API
calls.

## Linked BattleMetrics and HLL Bans

`/hllvn ban` and `/hllvn unban` are restricted to members authorized through
`[p]bm auth add` and bot owners. They support either a verified Discord link or
a direct game ID:

```text
/hllvn ban member:@member duration:2d reason:Repeated team killing
/hllvn ban eos_id:0123456789abcdef0123456789abcdef reason:Cheating
/hllvn unban member:@member
/hllvn unban eos_id:0123456789abcdef0123456789abcdef
```

Choose exactly one of `member` and `eos_id`. The member form resolves the EOS ID
from PostgreSQL; the direct form accepts a 17-digit or 32-character ID. Discord
slash commands require separate fields here because one option cannot be both a
native member picker and a free-text game-ID field.

Duration is optional and defaults to permanent. Temporary bans accept whole
hours, days, or weeks such as `6h`, `2d`, and `1w`; a unitless number means
days. The limit is 365 days because the HLL RCON ban operation accepts duration
in whole hours. A reason is required and is limited to 255 characters to fit
the BattleMetrics ban record.

The BattleMetrics record targets the Server Info panel's configured server,
falling back to `[p]bm setserver`. It stores the game identifier with its
BattleMetrics Steam, EOS, or HLL Windows identifier type, links the
BattleMetrics player profile when quick-match finds one, and records the
Discord administrator in the private ban note. The API token therefore needs
permission to create and delete bans for that server. The in-game side reuses
the shared, serialized HLL RCON connection rather than opening a competing
client. Both backends must be configured before either command changes a ban.

BattleMetrics and RCON are attempted independently for both operations. This is
intentional: a temporary outage on one service must not silently undo a
successful moderation action on the other. The private result embed identifies
complete, partial, or total failure and includes affected BattleMetrics ban IDs.

Unban searches BattleMetrics with the exact game identifier, verifies each
result locally, and only deletes bans directly scoped to the configured server.
It does not delete organization-wide or ban-list records. The RCON side clears
both temporary and permanent bans. A completed command may report that no ban
was found on one backend; this is treated as an already-clear, idempotent result.

## Authorized HLL Messages

`/hllvn mesg` is restricted to the same `[p]bm auth add` authorization list and
bot owners. Its single `target` text field accepts a linked Discord mention, a
17-digit or 32-character game account ID, or the case-insensitive word `ALL`:

```text
/hllvn mesg target:@member mesgs:Please contact an admin.
/hllvn mesg target:0123456789abcdef0123456789abcdef mesgs:Please contact an admin.
/hllvn mesg target:ALL mesgs:The server will restart in 10 minutes.
```

Discord exposes `target` as text because the same slash-command option must also
accept direct game IDs and `ALL`; type or paste the member mention into that
field. Member targets must belong to this Discord server and have a verified
`/link`. Direct and linked targets receive a private top-right in-game popup;
`ALL` uses the corresponding all-player popup. Messages are normalized to one
line and limited to 1,000 characters. The command uses the kill feed module's
shared, serialized RCON client and does not require the Discord kill-feed output
to be enabled.

Discord command replies are public in the channel where `/hllvn mesg` is used.
The confirmation embed shows the target, game account ID when applicable, and
message text so other administrators can see what was sent. Anyone with access
to that channel can see the reply. Authorization denials remain private, and
the confirmation does not ping the target or mentions in the message.

### Reply Directly To An Admin Alert

An authorized admin can use Discord's **Reply** on the bot's original `HLLVN SOS`
embed and type a response, such as `We are checking your report now.` The text
is sent as the same private in-game popup used by `/hllvn mesg`, targeting the
alert's `EOS_Id`. The reporting player does not need a linked Discord account.
The existing slash command remains available and unchanged.

Native reply forwarding is active while `/hllvn adminping` is enabled and works
only in its configured channel or thread. Only bot owners and members on the
`[p]bm auth` list can forward replies; merely having the pinged staff role is not
enough. Unauthorized replies are ignored without fetching messages or issuing
RCON commands. The referenced message must be this bot's SOS alert in the same
guild/channel, with a valid EOS ID. User/webhook copies, forwarded messages,
other embeds, replies to confirmation messages, and `ALL` targets are ignored.

Reply text is normalized to one line and limited to 1,000 characters. Attachments
are not forwarded. Replies enter a bounded 100-message queue per guild, with at
most one delivery every three seconds through the shared RCON client. Gateway
duplicates are tracked in a bounded runtime cache. Authorization and the alert
configuration are rechecked before delivery, so revocation or disabling/moving
admin alerts prevents queued replies from being sent. The target is the guild's
currently configured RCON server; no BattleMetrics or account-link lookup is
needed for a reply.

After successful delivery, the bot posts a public confirmation under the admin's
reply, showing the player, EOS ID, administrator, and sent text. Mentions do not
ping anyone. A failed RCON request gets an error instead of a success receipt and
is not automatically retried, because a timeout may occur after the game has
already shown the popup. Failure to post a Discord receipt never resends the
game message. Check the game before retrying with a new reply. Editing an
existing reply does not resend it. Pending replies and duplicate caches are
runtime-only and cleared on reload; original SOS embeds can still be replied to
after reload.

The bot needs View Channel, Read Message History, Send Messages (or Send Messages
in Threads), and Embed Links in the alert channel. Red must also be running with
the Message Content intent enabled, including the toggle in the Discord
Developer Portal. See the [Discord intent guide](https://discordpy.readthedocs.io/en/stable/intents.html#message-content).
The cog does not change gateway intents itself. Run `[p]reload BattleMetric` to
activate this feature; no new slash command or slash sync is required.

## Player Join Notices

Join notices are enabled by default for guilds with a configured HLL RCON endpoint,
the vaulted `hllrcon` password, and the existing `battlemetric_db` credentials.
No BattleMetrics token, Discord account link, or new database migration is needed.
Only new connection events observed after startup/enabling trigger a popup;
loading the cog does not greet everyone already online. Notices continue while
the Discord kill feed is disabled because they consume its shared RCON log stream.

The joining player receives a private in-game popup:

```text
Welcome to SLH, PlayerName
We tracked your performance
Kill: 1,234 Confirmed
Wounded: 56 Times
Want to show off? Join our Discord at discord.gg/slh4theppl
You can also customize your own dogtag there!
```

`PlayerName` comes from the connection event's in-game name. The test command
resolves the exact EOS ID against the live RCON player list and uses its current
in-game name; an offline target receives no test popup. Names are limited to one
line and 200 characters to keep the popup readable.

Counters come from PostgreSQL `RCON_DATA.Kill` and `RCON_DATA.Dead`, matching
`/vnstat`'s Confirmed Kills and Wounded Times. They reflect committed database
values; pending ingestion may lag slightly, and VIP purchases reduce the stored
kill balance. A player without a row gets zeroes. A failed database read shows
`Unavailable` for both counters rather than inventing zeroes.

Staff authorized through `bm auth` and bot owners can use:

```text
/hllvn joinnotice
/hllvn joinnotice test:EOS_ID
/hllvn joinnotice toggle:false
/hllvn joinnotice toggle:true
```

Discord requires the lowercase command name `joinnotice`. `test` accepts a
17-digit or 32-character game identifier and immediately sends the same popup
to that online player, including while automatic notices are disabled. It
does not alter the toggle. Set `toggle:true` to enable notices or `toggle:false`
to disable them; omit it to keep the saved setting. Use test or toggle separately. Command replies and
authorization denials are ephemeral, and authorization precedes configuration,
database, and RCON work.

Automatic notices wait five seconds after detection to let the player connect,
then use the existing serialized RCON client, at most once every two seconds per
guild. Queues hold at most 200 notices per guild; pending notices expire after
ten minutes. A bounded fingerprint cache deduplicates overlapping log lookbacks
and duplicate guild configurations for the same endpoint. Separate reconnect
events can receive a new welcome. Changing endpoints discards notices for the old
server. Disabling clears queued notices, and failures in one guild do not stop
delivery for others. RCON failures back off up to 60 seconds; uncertain deliveries
are not retried because the popup may already have arrived. A successful command
acknowledgement does not prove the player read it, so test with an online account.

Only the per-guild toggle is saved in Red Config. Pending game IDs, player names, connection-log
fingerprints (which contain player details), pacing, and deduplication state are
temporary and cleared on unload. The player's counters are sent only to that
game account through RCON; a staff test also shows its preview privately in
Discord. This feature creates no additional account links or statistic records
and follows the database module's existing account-deletion behavior.

After updating, run `[p]reload BattleMetric`, `[p]slash enablecog BattleMetric`
if needed, and `[p]slash sync`. Verify an authorized test, an unauthorized denial,
a real reconnect, persisted disabling across reload, and an unavailable database
in the running Red environment.

### If joinnotice does not appear

For a Downloader installation, updating the repository and updating the installed
cog are separate operations. Run:

```text
[p]repo update HarukoCog
[p]cog update BattleMetric
[p]reload BattleMetric
[p]slash enablecog BattleMetric
[p]slash sync
```

Replace `HarukoCog` with the repository name shown by `[p]repo list` if different.
`cog update` takes the installed cog name, `BattleMetric`, rather than the
repository name. For a local `addpath` installation, update the actual directory
loaded by the running bot, then reload and sync. Updating a different checkout
does not change the loaded cog.

These commands are registered globally, so use `slash sync` without a guild
argument. If the updated global command is still hidden in just one server,
check for an older guild-specific `/hllvn` registration before changing more
settings. Restarting the Discord client cannot update the code or command payload
loaded by the bot.

## Authorized Map Changes

```text
/hllvn changemap map_name:Cam Ranh Port Warfare
/hllvn changemap map_name:wdeve_warfare_day
```

Only bot owners and members authorized through `[p]bm auth add` can use this
command or its autocomplete. The check runs before defer, configuration, or
RCON work. Select the map and mode from autocomplete, enter its full display
name, or enter an exact map ID. A bare map name is accepted only if it matches
one available server variant; ambiguous modes are never guessed.

The stateless `module/hll_maps.py` service reads available maps through the
shared, serialized HLL: Vietnam RCON client and validates the selection before
submitting one `change_map` request. It needs the existing RCON endpoint and
vault password, but not an enabled Discord kill feed. No map rotation settings
are edited. Autocomplete reads the library's local Vietnam catalog without
RCON/API requests; new maps missing from that catalog can still be selected by
their exact server ID. A selection is always checked against the server's live
map list when submitted.

The request is submitted immediately without a confirmation button. However,
the library's `ChangeMap` operation starts a server-side 60-second countdown,
not a guaranteed zero-delay transition. The private confirmation reports an
accepted request, not that the new map is already loaded. See the
[hllrcon command implementation](https://github.com/timraay/hllrcon/blob/master/hllrcon/commands.py).
If RCON loses the response, check the current or pending map before retrying;
the bot does not blindly retry a potentially accepted change.

After updating, run `[p]reload BattleMetric` and `[p]slash sync` to register
`/hllvn changemap`. No new dependency or stored credential is required.

## In-Game Next-Map Voting

Configure scheduled voting after setting the existing RCON endpoint and vault
password. An enabled Discord kill-feed channel is not required. All staff
commands require `bm auth` authorization or bot ownership before any settings or
RCON access:

```text
/hllvn mapvote vote_start_minutes:20 number_of_options:5 enable:true duration_minutes:10
/hllvn mapvote vote_start_minutes:20 number_of_options:5 enable:true
/hllvn mapvote vote_start_minutes:20 number_of_options:5 enable:false
/hllvn mapchange sync
/hllvn mapchange startvote
/hllvn mapchange status
```

Start time accepts 0-1440 minutes after an observed match-start event. Duration
accepts 1-1440 minutes after voting opens; omitting it means until match end.
Choose 1-5 options. The bot samples unique map/mode IDs from the server rotation,
excludes the currently running map/mode, and offers fewer options if the rotation
has fewer alternatives. It never substitutes maps from the full available-map
catalog. Ties favor the earlier numbered option; zero ballots cause no next-map
write.

At vote start, one `MessageAllPlayers` request sends the numbered list through
the game's standard popup, including `Type "!vote #" to vote` and the example
`"!vote 1"`. The same instructions appear in the `!maplist` popup.
Any player can vote in team/unit chat without Discord
linking or staff permissions:

```text
!vote 1
!vote 2
!maplist
```

One ballot is stored per EOS/game account. Voting again changes the selection
rather than adding another vote. `!maplist` privately reopens the same numbered
options with current totals. Invalid votes get a private usage response. Repeated
vote requests are silently throttled for five seconds per account and list
requests for 30 seconds. Responses and announcements share a bounded delivery
queue, sending at most one RCON message every three seconds per guild. RCON does
not expose popup button configuration; the bot cannot require a custom Yes button
or detect that a player dismissed the popup. See the
[hllrcon messaging and sequence commands](https://github.com/timraay/hllrcon/blob/master/hllrcon/commands.py).

The rotation is cached in per-guild Red Config on the bot host. It syncs during
enable/setup, at observed match start, after a detected map change, or manually
with `/hllvn mapchange sync`. Chat votes and list requests never fetch rotation
data. Failed syncs leave the last good cache stored but cannot open a new vote
using another match's cache. Syncing mid-vote updates future eligibility, not
the displayed option numbers. If an offered map is removed during a manual sync,
the bot refuses further writes for it. Syncing does not undo a map already queued
before that removal.

Match-start/end and chat events use the existing shared admin-log poll, not a new
RCON connection. A 60-second status check catches map changes missed by the log
stream. If enabled partway through a match without an observed start time, the
bot waits for the next start event rather than guessing elapsed match time; staff
can use `startvote` immediately. Only one vote opens per observed match. Manual
start cannot replace an active or already-finished vote. Configuration changes
apply to future votes without rewriting a live ballot's options or deadline.

To avoid racing the game's natural transition, the current leader is queued
while voting remains open. Leader changes are applied at most once every 30
seconds, with an immediate final check when voting closes. An unchanged leader
or frozen winner is checked against the live sequence every 60 seconds until
the match advances (or the final-write window expires). If another integration
changes the next-map slot during that time, the bot restores the voted winner;
coordinate external next-map automation to avoid competing writes. A verified,
unchanged sequence needs no write. Periodic checks never recalculate a frozen
winner or rebroadcast its result. Successful queue updates and final verification
are logged at INFO with the winning map and ballot count, not player IDs.
The module uses
`GetMapSequence`, `MoveMapInSequence`, or `AddMapToSequence`, followed by read-back
verification under the shared RCON lock. It reorders the upcoming sequence, not
the rotation template, and never calls `ChangeMap` or ends a live match early.
The winner is frozen at vote close. Timed votes allow six seconds for the shared
log poll to deliver pre-deadline ballots; ballots timestamped at or after the
deadline are rejected. Until-match-end votes close on the end event.

An interrupted sequence write is retried only after re-reading live state, so
an accepted insertion is not blindly duplicated. Unknown/non-contiguous sequence
indices, a different current map, or an unverified read-back prevent a success
announcement. These safety refusals do not disconnect shared RCON. Ordinary
retries wait 60 seconds; vote closure requests one final check. An unchanged
refusal is logged only once until recovery, a new match, or a different reason.
Map mismatches include the recorded map ID,
sequence current index, and current map ID in the service log; it never guesses
another index or forces a map change to bypass a mismatch. An isolated refusal
can occur during transition. A persistent mismatch needs those diagnostic values
checked against the live server responses, rather than assuming bad credentials.
Final writes stop 30 seconds after an observed match end, and old
votes never intentionally change an already-advanced map. RCON outages or a
transition that beats the final update can still leave the last queued leader or
normal server sequence in effect; the bot does not force a replacement map after
transition. Verify the sequence behavior on your server before relying on it.

If a recorded vote does not affect the next match, use `/hllvn mapchange status`
while that match's ballot still exists. It reports accepted ballot counts,
the current/frozen winner, the last verified map, live session/sequence map IDs,
the actual next map, and the latest safety refusal. The reply is private, never
lists voters, does not change settings or the sequence, and uses the shared RCON
client for two reads. Requests are limited to one per 30 seconds per guild.
This separates missed chat votes from a server-side sequence update or override.

Settings, cached rotation, match timestamps, options, ballots keyed by game IDs,
and queue-verification state persist across reloads. Chat buffers, delivery
queues, fingerprints, cooldowns, and retry timers are runtime-only and bounded.
New matches replace old ballots; disabling clears the current vote but does not
undo a map already queued on the game server. Red user-data deletion removes a
linked account's ballot before its Discord/EOS link is deleted, including records
for guilds the bot has left. Unlinked game ballots cannot be resolved from a
Discord user ID and expire when that guild's next match is observed or voting is
disabled. No BattleMetrics calls or new secrets are used.

After updating, run `[p]reload BattleMetric` and `[p]slash sync` to register these
commands. The existing pinned `hllrcon` dependency provides the required methods.

## Timed HLL VIPs

`/hllvn addvip` uses the same HLL: Vietnam RCON endpoint, password, client, and
connection lock as the kill feed. The RCON endpoint and vault password must be
configured first, but the Discord kill-feed channel does not need to be
enabled.

Only members granted access through `[p]bm auth add @member` and bot owners can
run this command. Choose exactly one slash-command target:

```text
/hllvn addvip member:@member duration:1d
/hllvn addvip eos_id:0123456789abcdef0123456789abcdef duration:12h
```

The `member` form requires an existing `/link` record and resolves the member's
EOS ID from PostgreSQL. The `eos_id` form accepts a 17-digit or 32-character
game account ID directly. Duration accepts minutes, hours, days, or weeks, such
as `30m`, `12h`, `1d`, and `2w`; a number without a unit means days. It defaults
to one day and is limited to 365 days.

HLL RCON VIP records do not contain an expiration time. The cog stores each
expiration in per-guild Red Config and checks it every 60 seconds. When a grant
expires, the cog sends `RemoveVip` over RCON. Failed removals remain stored and
are retried, including after a cog or bot restart. Re-adding the same EOS ID
replaces its bot-managed expiration with the new duration.

Authorized members can reward everyone currently on the HLL: Vietnam server:

```text
/hllvn giveseedvip duration:2d
```

The command takes one RCON player snapshot, grants each player timed VIP access,
and sends a private in-game popup thanking them for seeding. Existing bot-managed
VIP time is extended rather than shortened, and these grants are protected from
`/hllvn purgevip`. Players who already have an externally managed VIP receive
the popup and temporary purge protection, but their original VIP is never
removed by the bot because its expiry is unknown. The command spaces RCON
requests two seconds apart and reports grant and popup failures separately.
Players joining after the snapshot are not included. The Discord response is
private to the authorized command user.

### BattleMetrics Seeder Flag

After the RCON reward batch, `/hllvn giveseedvip` also adds the BattleMetrics
player flag named `Seeder` to successfully rewarded players. External VIPs
whose original access was preserved also qualify. Failed VIP grants do not.
Popup failure does not prevent flagging a successfully rewarded player.

One-time setup:

1. In the BattleMetrics organization that owns your configured server, create
   exactly one organization-owned player flag named `Seeder`. Name matching is
   case-insensitive. The cog reuses this definition; it does not create, rename,
   or edit definitions. See [BattleMetrics' flag setup guide](https://learn.battlemetrics.com/article/51-how-can-i-create-or-edit-player-flags).
2. Ensure `[p]bm setserver <server_id>` (also used by Server Info) points to the
   BattleMetrics counterpart of your configured HLL RCON server.
3. Give the existing `battlemetrics` API key access to that organization's
   server and player identifiers, read player flags, and assign flags. Keep the
   key in Red's shared token vault; no new credential or database is required.
4. Run `[p]reload BattleMetric`, then `/hllvn giveseedvip duration:2d`. There are
   no new slash options, so this update does not require a slash sync.

The bot discovers the server's organization once per batch and refuses to
choose a same-named flag belonging to another organization, a shared/read-only
flag, or an ambiguous duplicate. Players are resolved using exact EOS/HLL
Windows IDs or 17-digit Steam IDs, never names or Discord links. Unmatched or
ambiguous profiles are reported as flag failures rather than guessed.

Identifier lookups are batched in groups of 25 players, with at most 50
identifier resources per request. Every flag-related HTTP request is paced
through the shared API client at least two seconds apart across guilds. Flag
lists and assignments are paginated with a bounded, same-origin check. Existing
active Seeder assignments are preserved without another write, and other flags
are never replaced or removed. A full-server command can take several minutes
longer than the RCON-only batch; do not start another reward batch while it runs.

The private result reports VIP grants, popup failures, flags added, players
already flagged, and unconfirmed flag EOS IDs independently. A missing token,
server, organization, flag, or API outage does not undo successful VIP rewards.
Permission denials and HTTP 429 stop remaining flag requests for that batch;
other per-player failures are isolated. Ambiguous writes are not blindly
retried. Inspect the BattleMetrics profiles before manually retrying flagging:
running `giveseedvip` again also extends VIP time again.

Seeder flags are persistent recognition, not timed VIP access: VIP expiry and
`purgevip` never remove them. They remain in BattleMetrics until staff remove
them there. Match results, flag IDs, and failure lists are held only for the
current command; the cog adds no persistent flag tracking or retry worker.

`/hllvn buyvip` is available to every guild member. It first requires the
member to have completed `/link`, then opens the private
`HLLVN VIP EXCHANGE - NO REFUND!!!` modal. Enter one of these package numbers:

| Package | Kill cost | VIP duration |
| --- | ---: | ---: |
| `1` | 100 | 1 day |
| `2` | 1,500 | 15 days |
| `3` | 3,000 | 30 days |
| `4` | 36,500 | 365 days |

The link and kill balance are checked again when the modal is submitted. The
database deduction uses one conditional PostgreSQL update, so simultaneous
purchases cannot spend the same kills. The transaction commits only after RCON
accepts the VIP grant; an RCON failure rolls the deduction back. Buying another
package extends an existing bot-managed VIP expiration so paid time is not
discarded. Successful exchanges are final and cannot be refunded through the
bot.

`/hllvn purgevip` compares the server's current VIP list with all timed grants
stored by the cog. The default `confirm:false` is a dry run. With `confirm:true`,
it removes control-panel and other external VIPs while preserving every grant
tracked by `/hllvn addvip`, `/hllvn buyvip`, `/hllvn giveseedvip`, or a verified
`/link` reward. Each removal is serialized through the same RCON connection lock and spaced two
seconds apart; the command does not call BattleMetrics. Failed removals are
reported once and are not retried in a tight loop.

### VIP team switching

Authorized members can enable the in-game VIP team-switch command:

```text
/hllvn allowvipteamswap toggle:Enable
/hllvn allowvipteamswap toggle:Disable
```

When enabled, a player can enter `!changeteam` in Team or Unit chat. The cog
checks the server's current RCON VIP list, so bot-managed and externally managed
VIPs are both recognized. A VIP is switched immediately; HLL kills their current
soldier when they are alive. A non-VIP receives the private in-game message
`This feature is for VIPs only.`

Each player enters a silent 90-second cooldown as soon as a request is accepted
for processing. Requests repeated during that period are ignored without an
RCON lookup or reply. The VIP list is cached for 30 seconds to limit `GetVips`
traffic during a burst. Chat detection uses the existing shared three-second
admin-log poll and does not call BattleMetrics or send Discord messages.

## In-Game Admin Alerts

Authorized members can enable the in-game admin request command from the text
channel where alerts should be posted:

```text
/hllvn adminping role:@HLL-Admin toggle:Enable
/hllvn adminping role:@HLL-Admin toggle:Disable
```

The enable command stores both the selected role and the channel where the
slash command was run. It verifies the shared HLL RCON connection before
enabling. The bot must be able to send messages and embeds in that channel;
`@everyone` cannot be selected as the alert role. The configuration command is
restricted to bot owners and members authorized through `[p]bm auth`.

Players can then send either `!admin` or `!admin message text` in HLL Team or
Unit chat. Discord receives an `HLLVN SOS` embed containing the player's in-game
name, EOS ID, linked Discord account when available, and the supplied text. The
configured role is the only mention Discord is allowed to notify; mentions
placed in player names or report text cannot ping other users or roles.

Authorized staff can also reply directly to the original SOS embed to send a
private in-game response to its reporting player. See
[Reply Directly To An Admin Alert](#reply-directly-to-an-admin-alert) for
authorization, pacing, permissions, and delivery confirmations.

Alerts use the existing shared three-second RCON admin-log poll. Overlapping log
results are deduplicated, and alerts enter a bounded queue that sends at most
one message per guild every three seconds. Temporary Discord failures use an
exponential retry delay. The feature makes no BattleMetrics API calls.

## Team-Kill Watch

Authorized members can configure rolling team-kill detection and choose the
Discord channel where staff action cards are sent:

```text
/hllvn tkwatch toggle:Enable channel:#admin-alerts threshold_per_min:3 watch_duration:15 exclude_commander:true role:@Admins
/hllvn tkwatch toggle:Disable
```

The enable command requires a threshold from 1 through 100 team kills and a
watch duration from 1 through 90 minutes. A player's count is measured over a
rolling 60-second window and an alert is created when the configured threshold
is reached. Configuration and every alert button are restricted to bot owners
and members authorized through `[p]bm auth add`; checks happen before any defer,
configuration read, database access, or RCON action.

Each alert contains the player's name, EOS ID, team, current in-game role,
threshold count, latest victim, and weapon. Reaching the threshold automatically
sends `your Team Kill have been noticed by admin. Please avoid TK by all cost`
through RCON, without waiting for staff. The staff controls are:

- **Forgive** cancels the pending five-minute default or removes an active watch. It cannot undo a warning already delivered or a completed kick.
- **Warn & Watch** sends another warning and persists a watch for the configured duration, starting when the action succeeds. The player's next team kill while watched triggers an automatic kick; older queued kills do not trigger that watch.
- **Kick** immediately removes the player from the game server through RCON.

With no staff decision after five minutes, **Warn & Watch** is applied
automatically. **Forgive** and **Kick** remain available until 15 minutes after
the original alert was created, including after a manual or automatic watch
starts. **Warn & Watch** is disabled once the watch starts, so the same alert
cannot repeatedly extend it. Forgiving or kicking removes all buttons. At the
15-minute deadline, any remaining buttons are removed; the watch continues for
its configured duration unless forgiven before that deadline. The embed and its
recorded decision are retained in Discord, not deleted. Disabling the feature
clears pending alerts and active watches and removes their buttons while keeping
the messages.

The optional `role` is pinged only when a new threshold alert is posted.
Warnings, decision updates, and retries do not repeat the ping. The role must be
mentionable, or the bot must have permission to mention roles in the channel;
`@everyone` is not accepted. Enable the feature without `role` to clear its role
ping. After updating the cog, run `[p]reload BattleMetric` and `[p]slash sync` so
Discord exposes the new option.

The `exclude_commander` option is retained and defaults to true. A current
Commander is ignored: no threshold alert, automatic warning, watch, or automatic
kick is issued for their TKs. Staff Warn & Watch and Kick buttons also recheck
the role, closing the alert and cancelling its watch instead of acting on a
player who became Commander after the alert was posted. The role is
verified from live RCON player data when a threshold or watched-player decision
is required. If RCON cannot verify the role, the fail-safe behavior is to skip
the alert, warning/watch, or kick. When commander exclusion is false, Commanders are
included and the embed explicitly identifies the Commander role.

Team-kill watch consumes the existing shared admin-log poll; it does not create
another RCON client. Overlapping logs are deduplicated and queued in a bounded
in-memory buffer so Discord delivery and role checks cannot delay the log poll.
All player messages, role lookups, and kicks use the shared per-guild serialized
RCON command path. Active Warn & Watch records and 15-minute action routing are
stored in Red Config along with warning delivery state, five-minute default
deadlines, role configuration, and decisions so they continue across reloads.
Failed warning/default actions retry with exponential backoff up to 60 seconds
without creating extra alerts or pretending a watch started, unless a fresh
RCON player list confirms the target is offline. An overdue default
is recovered when RCON becomes available, even if the button window has already
closed; its watch starts on successful recovery. Rolling counters and
deduplication caches are intentionally runtime-only.

### Players Who Disconnect Before An Action

If an initial warning, five-minute automatic Warn & Watch, or staff Warn & Watch
or Kick action fails, the bot requests a fresh player list through the shared
RCON client. This also covers failed commander-role verification after an alert
was created. Only a successful, valid list without the target's exact EOS ID
confirms absence. A timeout, connection failure, or malformed response does not
prove the player left: the case stays pending, automatic actions retain their
backoff, and staff can retry.

When absence is confirmed, the action card is closed with
`Closed - player disconnected before action`. Its buttons and automatic retries
are removed, but the embed is retained as an orange audit record. Closure is not
forgiveness and does not claim the warning or kick succeeded. Previously
confirmed warning delivery is preserved. The closed state survives reloads and
does not replay the pending action when the player rejoins.

An already-active watch is **not** cancelled or extended by this closure. It
keeps its original expiry across disconnects and reloads, and a new team kill
after reconnecting can still trigger its automatic kick. Disconnecting before
a watch starts, however, closes that pending case without starting a watch.
Closing an alert does not prevent new team kills from producing a new case.

Offline checks happen only after failed actions, not on another player poller.
Checks are limited to one request per guild every three seconds, share the
existing serialized RCON connection, and never cache absence for a later
decision. If another failed action arrives during that cooldown, it remains
pending until its next automatic retry or a new staff attempt. No BattleMetrics
API calls are added. Reload with `[p]reload BattleMetric`; this change adds no
slash options, so no slash sync is needed.

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

The slash enable/sync steps are required when these application commands are
first installed or changed. Discord may take a few minutes to display a newly
synchronized command.

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
used. After a successful verification, it also grants **14 days of in-game VIP**
and includes this in the confirmation: "Thank you for linking! Enjoy 14 days of
VIP on us." `/link` remains public; no `bm auth` authorization is required.

The linking bonus is once per Discord account and once per game account in each
guild. Verifying again, relinking to another account, replaying chat, or restarting
the bot does not add another bonus. Members who linked before this feature can
verify again to claim their first reward; existing links are not mass-rewarded.
Existing bot-managed VIP time gains 14 days rather than being shortened, and
the gift uses the normal VIP expiry worker and is protected from `purgevip`.
Externally managed VIPs keep their original access and gain temporary purge
protection; expiry never removes their external VIP.

Per-guild Red Config stores `hll_link_vip_rewards` receipts keyed by game ID,
with the Discord ID, fixed expiry, ownership flag, and completion flag. Receipts
remain after VIP expiry so that repeating `/link` cannot reclaim the bonus.
Red user-data deletion removes that user's receipts, while keeping a redacted
timed grant until normal expiry. No SQL migration or extra secret is needed.

If RCON or Config fails, the Discord link remains saved and the confirmation
clearly says the VIP reward could not be confirmed. The member can run `/link`
and verify again to retry. A reserved fixed expiry prevents an interrupted or
uncertain grant from adding another 14 days on retry; later purchased/admin VIP
time is never shortened. Rewards are tracked before RCON submission so purge
and expiry can safely handle an uncertain acknowledgement. There is no automatic
background retry; if an unconfirmed reward's reserved expiry has already passed,
staff must review it. Discord delivery failures do not undo a completed reward.
Run `[p]reload BattleMetric` after updating; the existing command needs no slash
sync and the receipts are registered automatically.

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

Account linking adds the Discord-to-EOS mapping and the one-time VIP reward
described above. Existing `RCON_DATA` statistics remain intact when a member
links, relinks, unlinks, or requests
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
BattleMetrics player and requests `playerServerInformation.timePlayed` from
`/players/{player_id}/servers/{server_id}`. It uses the active Server Info
panel's pinned server ID, falling back to the guild's configured default
BattleMetrics server. There is no separate player-stat server setting.
BattleMetrics names and playtime are cached for five minutes. Missing or
temporarily unavailable BattleMetrics data is shown as unavailable without
hiding the locally stored combat statistics.

`Enlisted Date` is the linked member's join date for the Discord server where
the command is used. A direct EOS lookup that is not linked to a member shows
that the Discord-specific fields are not linked.

The command renders these values into `res/template.png` with the bundled
`res/Wallpoet-Regular.ttf` font and sends the completed PNG in the Discord
embed while retaining the same values as searchable embed fields. Rendering
runs outside the bot event loop. If the image assets or Pillow cannot be
loaded, the command logs the failure and sends the text-field embed without the
image.

## Custom Dog Tags

The sibling `tagweb` service authenticates carvers through Discord OAuth2,
stages a validated transparent PNG in `/home/phuled/SLHTAG/staging`, and
notifies BattleMetric through an authenticated listener on `127.0.0.1:8765`.
Configure the shared secret from a private channel or DM:

```text
[p]set api tagweb ipc_secret,SAME_RANDOM_SECRET_USED_BY_TAGWEB
```

An authorized member configures the staff channel and checks readiness:

```text
[p]dogtag setup #dog-tag-review
[p]dogtag status
```

Approve and Deny buttons are restricted to bot owners and members authorized
through `[p]bm auth add @member`. Pending review metadata is stored in Red
Config, and persistent button handlers are restored after a restart. A new
submission from the same user supersedes the prior review. Each review is bound
to the staged file's SHA-256 identifier, preventing an older button from
approving a newer upload. Approval atomically moves the overlay to
`/home/phuled/SLHTAG/{discord_id}.png`; denial deletes the staged file.

When `/vnstat` renders a linked account, the cog layers an approved overlay
over `/home/phuled/SLHTAG/dogtag.png`, crops the transparent canvas margins,
and aspect-fits the visible tag inside `(612, 900)-(1024, 1100)` on the stat
card. This keeps the metal plate proportional while making better use of the
available space. A missing or invalid overlay does not prevent the remaining
card from rendering.

The storage path defaults to `/home/phuled/SLHTAG`. Set
`HLLVN_TAG_STORAGE` for both Red and `tagweb` before startup to use another
path. Red's listener can be changed with `HLLVN_TAG_IPC_HOST` and
`HLLVN_TAG_IPC_PORT`; keep it on localhost. See the sibling
`tagweb/README.md` deployment bundle for OAuth, systemd, Nginx, permissions,
and dependencies.

Downloader installs the `Pillow` Python package from `info.json`. Current
Ubuntu systems normally receive a prebuilt Pillow wheel and need no additional
APT packages. If Pillow must compile from source, install the compiler, Python
headers, and the PNG/font dependencies before reinstalling the cog:

```bash
sudo apt-get update
sudo apt-get install -y build-essential python3-dev python3-setuptools \
  libjpeg-dev zlib1g-dev libfreetype6-dev
```

These packages follow Pillow's Linux build requirements. The Wallpoet font is
loaded directly from the cog, so it does not need to be installed system-wide.

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
