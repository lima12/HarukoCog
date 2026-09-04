# BattleMetric

BattleMetric is a modular Redbot cog for calling the BattleMetrics API from Discord.

## Layout

- `battlemetric.py` owns Red Config, cog lifecycle, and shared helper methods.
- `api.py` owns all BattleMetrics HTTP behavior.
- `commands_mixin.py` owns Discord commands and display formatting.

This keeps endpoint expansion simple: add a method to `BattleMetricsClient`, then add a command or background workflow that calls it.

## Commands

- `[p]battlemetric` or `[p]bm` - Show current guild settings.
- `[p]bm settoken <token>` - Bot owner only. Store the BattleMetrics bearer token.
- `[p]bm cleartoken` - Bot owner only. Remove the stored token.
- `[p]bm setserver <server_id>` - Server admin. Set this guild's default BattleMetrics server.
- `[p]bm clearserver` - Server admin. Clear this guild's default server.
- `[p]bm setgame <game>` - Server admin. Set default search game, such as `rust`, `ark`, `squad`, `dayz`, or `arma3`.
- `[p]bm cleargame` - Server admin. Clear the default search game.
- `[p]bm server [server_id]` - Show server status. Uses the configured default when no ID is supplied.
- `[p]bm search <search> [game] [limit]` - Search servers.
- `[p]bm player <player_id>` - Show a player by BattleMetrics ID.
- `[p]bm rawget <path> [params_json]` - Bot owner only. Raw GET helper for testing new endpoints.

## Token

BattleMetrics uses OAuth bearer tokens. Public server endpoints can often be queried without a token, but protected RCON, ban, note, and organization data requires one.

Create a token in the BattleMetrics developer area, then run:

```text
[p]bm settoken YOUR_TOKEN
```

The command attempts to delete the invoking message after saving the token.

## Expanding API Calls

Add focused wrappers in `api.py`:

```python
async def get_ban(self, ban_id: str) -> Dict[str, Any]:
    return await self.get(f"/bans/{ban_id}")
```

Then call that wrapper from a command, task, or service module. Avoid building URLs directly in command methods unless it is the owner-only `rawget` command.
