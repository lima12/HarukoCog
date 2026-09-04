"""Server Info panel module.

This module owns its persistent panel state and periodic refresh lifecycle so
future BattleMetrics features can be added without growing the cog class.
"""

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import discord
from discord import app_commands
from discord.ext import tasks
from redbot.core import commands

from ..api import BattleMetricsAPIError
from ..authorization import requires_authorized_user


log = logging.getLogger("red.BattleMetric.server_info")


class ServerInfoModule:
    """Maintain one automatically refreshed server-info panel per guild."""

    UPDATE_INTERVAL_SECONDS = 60
    _EMPTY_PANEL = {"channel_id": None, "message_id": None, "server_id": None}

    def __init__(self, cog: commands.Cog):
        self.cog = cog

    def register_config(self) -> None:
        self.cog.config.register_guild(server_info_panel=self._EMPTY_PANEL)

    async def start(self) -> None:
        if not self.panel_updater.is_running():
            self.panel_updater.start()

    def stop(self) -> None:
        self.panel_updater.cancel()

    async def get_panel(self, guild: discord.Guild) -> Dict[str, Optional[Union[int, str]]]:
        panel = await self.cog.config.guild(guild).server_info_panel()
        if not isinstance(panel, Mapping):
            return dict(self._EMPTY_PANEL)

        channel_id = panel.get("channel_id")
        message_id = panel.get("message_id")
        server_id = panel.get("server_id")
        return {
            "channel_id": int(channel_id) if channel_id else None,
            "message_id": int(message_id) if message_id else None,
            "server_id": str(server_id).strip() if server_id else None,
        }

    async def set_panel(
        self,
        guild: discord.Guild,
        *,
        channel_id: int,
        message_id: int,
        server_id: str,
    ) -> None:
        await self.cog.config.guild(guild).server_info_panel.set(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "server_id": server_id,
            }
        )

    async def clear_panel(self, guild: discord.Guild) -> None:
        await self.cog.config.guild(guild).server_info_panel.set(dict(self._EMPTY_PANEL))

    async def build_embed(self, server_id: str) -> discord.Embed:
        document = await self.cog.api.get_server(
            server_id,
            include="player",
            auth=await self.cog.has_api_token(),
        )
        return self.render_embed(document)

    def render_embed(self, document: Mapping[str, Any]) -> discord.Embed:
        server = document.get("data")
        if not isinstance(server, Mapping):
            raise ValueError("BattleMetrics did not return a server document.")

        attrs = self._attributes(server)
        server_id = str(server.get("id") or "unknown")
        server_name = str(attrs.get("name") or f"Server {server_id}")
        status = str(attrs.get("status") or "unknown").lower()
        color = {
            "online": discord.Color.green(),
            "offline": discord.Color.red(),
        }.get(status, discord.Color.orange())

        embed = discord.Embed(
            title=server_name[:256],
            url=f"https://www.battlemetrics.com/servers/{server_id}",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Server Name", value=self._code(server_name), inline=False)
        embed.add_field(name="Address", value=self._code(self._address(attrs)), inline=False)
        embed.add_field(name="IP", value=self._code(attrs.get("ip") or "unknown"), inline=True)
        embed.add_field(name="Port", value=self._code(attrs.get("port") or "unknown"), inline=True)
        embed.add_field(name="Status", value=self._code(status), inline=True)
        embed.add_field(name="Players", value=self._player_count(attrs), inline=True)
        embed.add_field(
            name="Online Players",
            value=self._player_list(self._online_player_names(document, server), attrs.get("players")),
            inline=False,
        )
        embed.set_footer(text="Updates automatically every 60 seconds")
        return embed

    @tasks.loop(seconds=UPDATE_INTERVAL_SECONDS)
    async def panel_updater(self) -> None:
        try:
            await self._refresh_all_panels()
        except Exception:
            log.exception("Unhandled error while refreshing Server Info panels")

    @panel_updater.before_loop
    async def before_panel_updater(self) -> None:
        await self.cog.bot.wait_until_ready()

    async def _refresh_all_panels(self) -> None:
        panels: List[Tuple[discord.Guild, Dict[str, Optional[Union[int, str]]]]] = []
        for guild in self.cog.bot.guilds:
            panel = await self.get_panel(guild)
            if all(panel.get(key) for key in ("channel_id", "message_id", "server_id")):
                panels.append((guild, panel))

        documents: Dict[str, Mapping[str, Any]] = {}
        use_authentication = await self.cog.has_api_token()
        for server_id in {panel["server_id"] for _, panel in panels if panel["server_id"]}:
            try:
                documents[server_id] = await self.cog.api.get_server(
                    server_id,
                    include="player",
                    auth=use_authentication,
                )
            except BattleMetricsAPIError as exc:
                log.warning("Could not refresh BattleMetrics server %s: %s", server_id, exc)

        for guild, panel in panels:
            server_id = panel["server_id"]
            document = documents.get(server_id) if server_id else None
            if document is None:
                continue

            try:
                embed = self.render_embed(document)
            except (TypeError, ValueError) as exc:
                log.warning("Could not render Server Info panel for server %s: %s", server_id, exc)
                continue

            await self._edit_panel(guild, panel, embed)

    async def _edit_panel(
        self,
        guild: discord.Guild,
        panel: Mapping[str, Optional[Union[int, str]]],
        embed: discord.Embed,
    ) -> None:
        channel_id = panel.get("channel_id")
        message_id = panel.get("message_id")
        if not channel_id or not message_id:
            return

        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            await self.clear_panel(guild)
            log.info("Cleared Server Info panel for guild %s because its channel is unavailable", guild.id)
            return

        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            await self.clear_panel(guild)
            log.info("Cleared Server Info panel for guild %s because its message was deleted", guild.id)
            return
        except discord.Forbidden:
            log.warning("Cannot view Server Info panel %s in guild %s", message_id, guild.id)
            return
        except discord.HTTPException as exc:
            log.warning("Could not fetch Server Info panel %s in guild %s: %s", message_id, guild.id, exc)
            return

        bot_user = self.cog.bot.user
        if bot_user is None or message.author.id != bot_user.id:
            await self.clear_panel(guild)
            log.info("Cleared Server Info panel for guild %s because the bot no longer owns the message", guild.id)
            return

        try:
            await message.edit(content=None, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            log.warning("Cannot edit Server Info panel %s in guild %s", message_id, guild.id)
        except discord.HTTPException as exc:
            log.warning("Could not edit Server Info panel %s in guild %s: %s", message_id, guild.id, exc)

    @staticmethod
    def _attributes(resource: Mapping[str, Any]) -> Mapping[str, Any]:
        attrs = resource.get("attributes")
        return attrs if isinstance(attrs, Mapping) else {}

    @staticmethod
    def _code(value: Any) -> str:
        text = str(value).replace("`", "'")
        return f"`{text[:1018]}`"

    @staticmethod
    def _address(attrs: Mapping[str, Any]) -> str:
        address = attrs.get("address")
        if address:
            return str(address)

        ip = attrs.get("ip")
        port = attrs.get("port")
        if ip is not None and port is not None:
            return f"{ip}:{port}"
        return str(ip or "unknown")

    @staticmethod
    def _player_count(attrs: Mapping[str, Any]) -> str:
        players = attrs.get("players")
        max_players = attrs.get("maxPlayers") or attrs.get("max_players")
        if players is None and max_players is None:
            return "`unknown`"
        if max_players is None:
            return f"`{players}`"
        return f"`{players}/{max_players}`"

    def _online_player_names(
        self,
        document: Mapping[str, Any],
        server: Mapping[str, Any],
    ) -> Optional[List[str]]:
        included = document.get("included")
        relationships = server.get("relationships")
        has_player_relationship = isinstance(relationships, Mapping) and any(
            key in relationships for key in ("player", "players")
        )
        if not isinstance(included, Sequence) or isinstance(included, (str, bytes)):
            return None if not has_player_relationship else []

        names: List[str] = []
        seen = set()
        for resource in included:
            if not isinstance(resource, Mapping) or resource.get("type") != "player":
                continue
            name = self._attributes(resource).get("name")
            if not name:
                continue
            name = str(name)
            if name not in seen:
                seen.add(name)
                names.append(name)

        return names if names or has_player_relationship else None

    @staticmethod
    def _player_list(player_names: Optional[Sequence[str]], player_count: Any) -> str:
        if player_names is None:
            return "Player list is unavailable from BattleMetrics for this server."
        if not player_names:
            return "No players online." if player_count in (0, "0") else "Player names are unavailable."

        lines: List[str] = []
        for index, name in enumerate(player_names):
            safe_name = discord.utils.escape_markdown(str(name), as_needed=False)[:180]
            entry = f"- {safe_name}"
            if len("\n".join(lines + [entry])) > 1000:
                remaining = len(player_names) - index
                lines.append(f"... and {remaining} more")
                break
            lines.append(entry)
        return "\n".join(lines)[:1024]


class ServerInfoCommandsMixin:
    """Commands for managing the Server Info module."""

    @commands.hybrid_group(name="serverinfo", invoke_without_command=True)
    @commands.guild_only()
    @requires_authorized_user()
    async def serverinfo(self, ctx: commands.Context) -> None:
        """Manage this guild's automatic BattleMetrics Server Info panel."""
        await ctx.send_help()

    @serverinfo.command(name="setup")
    @app_commands.describe(channel="Channel that should receive the Server Info panel.")
    @commands.guild_only()
    async def serverinfo_setup(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Create a Server Info panel in a mentioned text channel."""
        if ctx.guild is None:
            return

        server_id = await self.get_default_server_id(ctx.guild)
        if not server_id:
            await ctx.send("Set a default BattleMetrics server first with `battlemetric setserver <server_id>`.")
            return

        try:
            embed = await self.server_info.build_embed(server_id)
            message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except BattleMetricsAPIError as exc:
            await ctx.send(str(exc))
            return
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        except discord.Forbidden:
            await ctx.send("I cannot send embeds in that channel.")
            return
        except discord.HTTPException as exc:
            await ctx.send(f"Could not create the Server Info message: {exc}")
            return

        await self.server_info.set_panel(
            ctx.guild,
            channel_id=channel.id,
            message_id=message.id,
            server_id=server_id,
        )
        await ctx.send(f"Server Info panel configured in {channel.mention}.")

    @serverinfo.command(name="modify")
    @app_commands.describe(message_id="ID of this bot's message in the current channel.")
    @commands.guild_only()
    async def serverinfo_modify(self, ctx: commands.Context, message_id: int) -> None:
        """Make one of this bot's messages in the current channel the Server Info panel."""
        if ctx.guild is None:
            return
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("Run this command in a text channel containing the target message.")
            return

        try:
            message = await ctx.channel.fetch_message(message_id)
        except discord.NotFound:
            await ctx.send("That message was not found in this channel.")
            return
        except discord.Forbidden:
            await ctx.send("I cannot view messages in this channel.")
            return
        except discord.HTTPException as exc:
            await ctx.send(f"Could not fetch that message: {exc}")
            return

        bot_user = self.bot.user
        if bot_user is None or message.author.id != bot_user.id:
            await ctx.send("The message must have been sent by this bot.")
            return

        server_id = await self.get_default_server_id(ctx.guild)
        if not server_id:
            await ctx.send("Set a default BattleMetrics server first with `battlemetric setserver <server_id>`.")
            return

        try:
            embed = await self.server_info.build_embed(server_id)
            await message.edit(content=None, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except BattleMetricsAPIError as exc:
            await ctx.send(str(exc))
            return
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        except discord.Forbidden:
            await ctx.send("I cannot edit that message in this channel.")
            return
        except discord.HTTPException as exc:
            await ctx.send(f"Could not update that message: {exc}")
            return

        await self.server_info.set_panel(
            ctx.guild,
            channel_id=ctx.channel.id,
            message_id=message.id,
            server_id=server_id,
        )
        await ctx.send("Server Info panel updated and scheduled for 60-second refreshes.")
