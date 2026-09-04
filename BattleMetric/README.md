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
data.

Each Discord guild has one tracked Server Info panel. Running `setup` creates a
new panel and replaces the previously tracked one. A panel is pinned to the
server selected during setup, so change the default server and run `modify` on
the panel message when you want it to track a different server.

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
