import json
from typing import Any, Dict, Mapping, Optional

import discord
from discord import app_commands
from redbot.core import commands

from .api import BattleMetricsAPIError


class BattleMetricCommandsMixin:
    """Command surface for the BattleMetric cog."""

    @commands.hybrid_group(name="battlemetric", aliases=["bm"], invoke_without_command=True)
    @commands.guild_only()
    async def battlemetric(self, ctx: commands.Context):
        """Show BattleMetric configuration for this server."""
        await self._send_bm_status(ctx)

    @battlemetric.command(name="status")
    @commands.guild_only()
    async def bm_status(self, ctx: commands.Context):
        """Show BattleMetric configuration for this server."""
        await self._send_bm_status(ctx)

    async def _send_bm_status(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            return

        server_id = await self.get_default_server_id(ctx.guild)
        game = await self.get_default_game(ctx.guild)
        token = await self.get_api_token()

        lines = [
            f"Token: {'configured' if token else 'not configured'}",
            f"Default server ID: `{server_id}`" if server_id else "Default server ID: not set",
            f"Default game filter: `{game}`" if game else "Default game filter: not set",
        ]

        await ctx.send("BattleMetric settings:\n" + "\n".join(f"- {line}" for line in lines))

    @battlemetric.command(name="settoken")
    @commands.is_owner()
    async def bm_set_token(self, ctx: commands.Context, *, token: str):
        """Store the BattleMetrics API bearer token globally for this bot."""
        token = token.strip()
        if not token:
            await ctx.send("Token cannot be empty.")
            return

        await self.set_api_token(token)

        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass

        await ctx.send("BattleMetrics token saved.", delete_after=20)

    @battlemetric.command(name="cleartoken")
    @commands.is_owner()
    async def bm_clear_token(self, ctx: commands.Context):
        """Remove the stored BattleMetrics API token."""
        await self.set_api_token(None)
        await ctx.send("BattleMetrics token cleared.")

    @battlemetric.command(name="setserver")
    @app_commands.describe(server_id="BattleMetrics server ID to use by default.")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def bm_set_server(self, ctx: commands.Context, server_id: str):
        """Set the default BattleMetrics server ID for this Discord server."""
        if ctx.guild is None:
            return

        await self.set_default_server_id(ctx.guild, server_id)
        await ctx.send(f"Default BattleMetrics server set to `{server_id}`.")

    @battlemetric.command(name="clearserver")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def bm_clear_server(self, ctx: commands.Context):
        """Clear the default BattleMetrics server ID for this Discord server."""
        if ctx.guild is None:
            return

        await self.set_default_server_id(ctx.guild, None)
        await ctx.send("Default BattleMetrics server cleared.")

    @battlemetric.command(name="setgame")
    @app_commands.describe(game="BattleMetrics game slug such as rust, ark, squad, dayz, or arma3.")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def bm_set_game(self, ctx: commands.Context, game: str):
        """Set the default BattleMetrics game filter for server searches."""
        if ctx.guild is None:
            return

        await self.set_default_game(ctx.guild, game)
        await ctx.send(f"Default BattleMetrics game filter set to `{game.strip().lower()}`.")

    @battlemetric.command(name="cleargame")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def bm_clear_game(self, ctx: commands.Context):
        """Clear the default BattleMetrics game filter for this Discord server."""
        if ctx.guild is None:
            return

        await self.set_default_game(ctx.guild, None)
        await ctx.send("Default BattleMetrics game filter cleared.")

    @battlemetric.command(name="server")
    @app_commands.describe(server_id="Optional BattleMetrics server ID. Uses the configured default when omitted.")
    @commands.guild_only()
    async def bm_server(self, ctx: commands.Context, server_id: Optional[str] = None):
        """Fetch a BattleMetrics server and show common status data."""
        if ctx.guild is None:
            return

        server_id = (server_id or await self.get_default_server_id(ctx.guild) or "").strip()
        if not server_id:
            await ctx.send("Provide a server ID or configure one with `battlemetric setserver <server_id>`.")
            return

        try:
            data = await self.api.get_server(server_id, auth=await self.has_api_token())
        except BattleMetricsAPIError as exc:
            await ctx.send(str(exc))
            return

        server = data.get("data")
        if not isinstance(server, Mapping):
            await ctx.send("BattleMetrics did not return a server document.")
            return

        await ctx.send(embed=self._server_embed(server))

    @battlemetric.command(name="search")
    @app_commands.describe(
        search="Server search text.",
        game="Optional BattleMetrics game slug. Uses the configured default when omitted.",
        limit="Number of results to show, from 1 to 10.",
    )
    @commands.guild_only()
    async def bm_search(
        self,
        ctx: commands.Context,
        search: str,
        game: Optional[str] = None,
        limit: int = 5,
    ):
        """Search BattleMetrics servers."""
        if ctx.guild is None:
            return

        limit = max(1, min(int(limit), 10))
        game = (game or await self.get_default_game(ctx.guild) or "").strip() or None

        try:
            data = await self.api.list_servers(
                search=search,
                game=game,
                page_size=limit,
                auth=await self.has_api_token(),
            )
        except BattleMetricsAPIError as exc:
            await ctx.send(str(exc))
            return

        servers = data.get("data")
        if not isinstance(servers, list) or not servers:
            await ctx.send("No BattleMetrics servers matched that search.")
            return

        embed = discord.Embed(
            title="BattleMetrics Server Search",
            color=discord.Color.blurple(),
        )
        if game:
            embed.description = f"Game filter: `{game}`"

        for server in servers[:limit]:
            if not isinstance(server, Mapping):
                continue
            attrs = self._attributes(server)
            name = str(attrs.get("name") or f"Server {server.get('id', 'unknown')}")[:256]
            players = self._player_count_text(attrs)
            address = self._address_text(attrs)
            status = attrs.get("status") or "unknown"
            value = f"ID: `{server.get('id', 'unknown')}`\nStatus: `{status}`\nPlayers: {players}"
            if address:
                value += f"\nAddress: `{address}`"
            embed.add_field(name=name, value=value[:1024], inline=False)

        await ctx.send(embed=embed)

    @battlemetric.command(name="player")
    @app_commands.describe(player_id="BattleMetrics player ID.")
    @commands.guild_only()
    async def bm_player(self, ctx: commands.Context, player_id: str):
        """Fetch a BattleMetrics player by ID."""
        try:
            data = await self.api.get_player(player_id, auth=await self.has_api_token())
        except BattleMetricsAPIError as exc:
            await ctx.send(str(exc))
            return

        player = data.get("data")
        if not isinstance(player, Mapping):
            await ctx.send("BattleMetrics did not return a player document.")
            return

        attrs = self._attributes(player)
        name = attrs.get("name") or f"Player {player.get('id', player_id)}"

        embed = discord.Embed(
            title=str(name)[:256],
            url=f"https://www.battlemetrics.com/players/{player.get('id', player_id)}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="BattleMetrics ID", value=f"`{player.get('id', player_id)}`", inline=False)
        if attrs.get("positiveMatch") is not None:
            embed.add_field(name="Positive Match", value=f"`{attrs.get('positiveMatch')}`", inline=True)

        await ctx.send(embed=embed)

    @battlemetric.command(name="rawget")
    @app_commands.describe(
        path="API path such as /servers/12345.",
        params_json="Optional JSON object for query parameters.",
    )
    @commands.is_owner()
    async def bm_raw_get(
        self,
        ctx: commands.Context,
        path: str,
        *,
        params_json: Optional[str] = None,
    ):
        """Owner-only raw GET helper for adding new endpoints."""
        params: Optional[Dict[str, Any]] = None
        if params_json:
            try:
                parsed = json.loads(params_json)
            except json.JSONDecodeError as exc:
                await ctx.send(f"Invalid params JSON: {exc}")
                return
            if not isinstance(parsed, dict):
                await ctx.send("Params JSON must be an object.")
                return
            params = parsed

        try:
            data = await self.api.get(path, params=params, auth=await self.has_api_token())
        except BattleMetricsAPIError as exc:
            await ctx.send(str(exc))
            return

        text = json.dumps(data, ensure_ascii=False, indent=2)
        if len(text) > 1900:
            text = text[:1900] + "\n..."
        await ctx.send(f"```json\n{text}\n```")

    def _server_embed(self, server: Mapping[str, Any]) -> discord.Embed:
        attrs = self._attributes(server)
        server_id = str(server.get("id") or "unknown")
        name = str(attrs.get("name") or f"Server {server_id}")

        embed = discord.Embed(
            title=name[:256],
            url=f"https://www.battlemetrics.com/servers/{server_id}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="BattleMetrics ID", value=f"`{server_id}`", inline=True)
        embed.add_field(name="Status", value=f"`{attrs.get('status') or 'unknown'}`", inline=True)
        embed.add_field(name="Players", value=self._player_count_text(attrs), inline=True)

        game = attrs.get("game")
        if game:
            embed.add_field(name="Game", value=f"`{game}`", inline=True)

        rank = attrs.get("rank")
        if rank is not None:
            embed.add_field(name="Rank", value=f"`{rank}`", inline=True)

        address = self._address_text(attrs)
        if address:
            embed.add_field(name="Address", value=f"`{address}`", inline=False)

        details = attrs.get("details")
        if isinstance(details, Mapping):
            map_name = details.get("map") or details.get("mapName")
            if map_name:
                embed.add_field(name="Map", value=str(map_name)[:1024], inline=True)
            country = details.get("country") or details.get("serverCountry")
            if country:
                embed.add_field(name="Country", value=f"`{country}`", inline=True)

        return embed

    @staticmethod
    def _attributes(resource: Mapping[str, Any]) -> Mapping[str, Any]:
        attrs = resource.get("attributes")
        return attrs if isinstance(attrs, Mapping) else {}

    @staticmethod
    def _player_count_text(attrs: Mapping[str, Any]) -> str:
        players = attrs.get("players")
        max_players = attrs.get("maxPlayers") or attrs.get("max_players")
        if players is None and max_players is None:
            return "`unknown`"
        if max_players is None:
            return f"`{players}`"
        return f"`{players}/{max_players}`"

    @staticmethod
    def _address_text(attrs: Mapping[str, Any]) -> Optional[str]:
        ip = attrs.get("ip")
        port = attrs.get("port")
        address = attrs.get("address")

        if address and port:
            return f"{address}:{port}"
        if ip and port:
            return f"{ip}:{port}"
        if address:
            return str(address)
        if ip:
            return str(ip)
        return None
