import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

from .VCC.commands_mixin import VCCCommandsMixin
from .VCC.panel import VCHControlPanelView, VCHKickSelectView
from .VCC.VCOwnerCommand import VCOwnerCommandsMixin


log = logging.getLogger("red.VoiceChannelHandling")

CREATION_COOLDOWN_SECONDS = 180
PANEL_REFRESH_INTERVAL_SECONDS = 12
PANEL_REFRESH_EDIT_DELAY_SECONDS = 1.5


class VoiceChannelHandling(VCCCommandsMixin, VCOwnerCommandsMixin, commands.Cog):
    """Temporary voice channel handling cog.

    This cog:
    - Creates a temporary voice channel when a user joins the configured creator room.
    - Moves the user into their temp channel.
    - Reuses the user's existing temp channel if it still exists.
    - Deletes temp channels after a configurable delay when no human members remain.
    - Tracks state in Red Config and mirrors a per-guild JSON snapshot.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self._data_path: Path = cog_data_path(self)
        self._data_path.mkdir(parents=True, exist_ok=True)

        self.config = Config.get_conf(
            self,
            identifier=0x5643485F4D41494E_01,
            force_registration=True,
        )

        default_guild = {
            "creation_channel_id": None,
            "name_template": "{user}'s channel",
            "delete_delay": 10,
            "temp_category_id": None,
            "temp_channels": [],
            "counter": 1,
            "owner_channels": {},
            "control_panels": {},
        }

        self.config.register_guild(**default_guild)

        self._delete_tasks: Dict[int, asyncio.Task] = {}
        self._guild_locks: Dict[int, asyncio.Lock] = {}
        self._creation_cooldowns: Dict[Tuple[int, int], float] = {}
        self._dirty_panel_channels: Set[Tuple[int, int]] = set()
        self._dirty_panel_lock = asyncio.Lock()
        self._panel_refresh_task = asyncio.create_task(self._panel_refresh_worker())

        if hasattr(self.bot, "add_view"):
            try:
                self.bot.add_view(VCHControlPanelView(self))
            except Exception:
                log.exception("Failed to register persistent VCH control panel view.")

    def cog_unload(self):
        for task in self._delete_tasks.values():
            if not task.done():
                task.cancel()

        if not self._panel_refresh_task.done():
            self._panel_refresh_task.cancel()

        self._delete_tasks.clear()
        self._guild_locks.clear()
        self._creation_cooldowns.clear()
        self._dirty_panel_channels.clear()

    # ==========================================================
    # Lock helpers
    # ==========================================================

    def _get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    # ==========================================================
    # JSON DB helpers
    # ==========================================================

    def _guild_db_path(self, guild_id: int) -> Path:
        return self._data_path / f"{guild_id}.json"

    def _write_guild_json(self, guild_id: int, data: dict) -> None:
        """Atomic JSON write so a crash does not leave a half-written file."""
        path = self._guild_db_path(guild_id)
        tmp_path = path.with_suffix(".json.tmp")

        with tmp_path.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)

        os.replace(tmp_path, path)

    def _read_guild_json(self, guild_id: int) -> Optional[dict]:
        path = self._guild_db_path(guild_id)
        if not path.is_file():
            return None

        try:
            with path.open("r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            log.exception("Failed to read VCH JSON DB for guild %s", guild_id)
            return None

    async def _write_guild_snapshot(self, guild: discord.Guild) -> None:
        conf = self.config.guild(guild)

        data = {
            "guild_id": guild.id,
            "voicecreateroom_id": await conf.creation_channel_id(),
            "creation_channel_id": await conf.creation_channel_id(),
            "name_template": await conf.name_template(),
            "delete_delay": await conf.delete_delay(),
            "temp_category_id": await conf.temp_category_id(),
            "temp_channels": await conf.temp_channels(),
            "owner_channels": await conf.owner_channels(),
            "control_panels": await conf.control_panels(),
            "counter": await conf.counter(),
        }

        self._write_guild_json(guild.id, data)

    # ==========================================================
    # Public helper API for VCC command modules
    # ==========================================================

    async def set_creation_channel(self, guild: discord.Guild, channel_id: Optional[int]) -> None:
        await self.config.guild(guild).creation_channel_id.set(channel_id)
        await self._write_guild_snapshot(guild)

    async def get_creation_channel_id(self, guild: discord.Guild) -> Optional[int]:
        return await self.config.guild(guild).creation_channel_id()

    async def set_name_template(self, guild: discord.Guild, template: str) -> None:
        await self.config.guild(guild).name_template.set(template)
        await self._write_guild_snapshot(guild)

    async def get_name_template(self, guild: discord.Guild) -> str:
        return await self.config.guild(guild).name_template()

    async def set_delete_delay(self, guild: discord.Guild, delay: int) -> None:
        delay = max(3, int(delay))
        await self.config.guild(guild).delete_delay.set(delay)
        await self._write_guild_snapshot(guild)

    async def get_delete_delay(self, guild: discord.Guild) -> int:
        delay = await self.config.guild(guild).delete_delay()
        return max(3, int(delay or 10))

    async def set_temp_category(self, guild: discord.Guild, category_id: Optional[int]) -> None:
        await self.config.guild(guild).temp_category_id.set(category_id)
        await self._write_guild_snapshot(guild)

    async def get_temp_category(self, guild: discord.Guild) -> Optional[int]:
        return await self.config.guild(guild).temp_category_id()

    async def get_temp_channels(self, guild: discord.Guild) -> List[int]:
        channels = await self.config.guild(guild).temp_channels()
        return list(dict.fromkeys(channels))

    async def _get_next_counter_unlocked(self, guild: discord.Guild) -> int:
        """Increment counter without taking the guild lock.

        Only call this when:
        - You are already inside the guild lock, OR
        - You do not need locking.
        """
        conf = self.config.guild(guild)
        current = await conf.counter()

        if current is None or current < 1:
            current = 1

        await conf.counter.set(current + 1)
        return current

    async def get_next_counter(self, guild: discord.Guild) -> int:
        """Get the next counter value safely.

        This public wrapper takes the guild lock. Do not call this from code that
        already holds the same guild lock, because asyncio.Lock is not re-entrant.
        """
        async with self._get_guild_lock(guild.id):
            return await self._get_next_counter_unlocked(guild)

    # ==========================================================
    # Setup command
    # ==========================================================

    @commands.hybrid_command(name="setupvch", description="Configure temporary voice channel handling.")
    @app_commands.guild_only()
    @app_commands.describe(
        creator_room="Voice channel users join to create a temp channel.",
        delete_delay="Seconds before empty temp channels are deleted.",
        category="Optional category where temp channels are created.",
        name_template="Template: {user}, {id}, {tag}, {counter}.",
    )
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True, move_members=True)
    async def setupvch(
        self,
        ctx: commands.Context,
        creator_room: discord.VoiceChannel,
        delete_delay: int,
        category: Optional[discord.CategoryChannel] = None,
        *,
        name_template: str = "{user}'s channel",
    ):
        guild = ctx.guild
        if guild is None:
            return

        delete_delay = max(3, int(delete_delay))
        target_category_id = category.id if category else creator_room.category_id

        await self.config.guild(guild).creation_channel_id.set(creator_room.id)
        await self.config.guild(guild).name_template.set(name_template)
        await self.config.guild(guild).delete_delay.set(delete_delay)
        await self.config.guild(guild).temp_category_id.set(target_category_id)

        await self._write_guild_snapshot(guild)

        temp_cat_text = "None"
        if target_category_id is not None:
            cat_obj = guild.get_channel(target_category_id)
            if isinstance(cat_obj, discord.CategoryChannel):
                temp_cat_text = cat_obj.name

        await ctx.send(
            "VoiceChannelHandling configured.\n"
            f"- Creation room: {creator_room.mention}\n"
            f"- Name template: `{name_template}`\n"
            f"- Delete delay: `{delete_delay}` seconds\n"
            f"- Temp category: `{temp_cat_text}`\n"
            f"- JSON DB file: `{guild.id}.json`"
        )

    @commands.hybrid_command(name="voicedashboard", description="Repost the control panel for your voice room.")
    @app_commands.guild_only()
    @commands.guild_only()
    async def voicedashboard(self, ctx: commands.Context):
        guild = ctx.guild
        if guild is None:
            return

        author = ctx.author
        if not isinstance(author, discord.Member):
            return

        if not author.voice or not isinstance(author.voice.channel, discord.VoiceChannel):
            await ctx.send(
                "You must be connected to a managed temporary voice channel to use this command.",
                ephemeral=True,
            )
            return

        channel = author.voice.channel

        if ctx.channel.id != channel.id:
            await ctx.send(
                "Use this command from the voice channel chat you want to manage.",
                ephemeral=True,
            )
            return

        temp_channels = await self.get_temp_channels(guild)
        if channel.id not in temp_channels:
            await ctx.send(
                "This command only works in a managed temporary voice channel.",
                ephemeral=True,
            )
            return

        is_owner = await self._is_panel_channel_owner(author, channel)
        is_admin = author.guild_permissions.administrator
        if not is_owner and not is_admin:
            await ctx.send(
                "Only this room's owner or a server administrator can use this command.",
                ephemeral=True,
            )
            return

        sent = await self._send_control_panel(channel, force_new=True)
        if not sent:
            await ctx.send(
                "I could not send the voice dashboard in this channel.",
                ephemeral=True,
            )
            return

        await ctx.send("Voice dashboard posted.", ephemeral=True)

    # ==========================================================
    # Voice state listener
    # ==========================================================

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        if before.channel == after.channel:
            return

        guild = member.guild
        if guild is None:
            return

        guild_conf = self.config.guild(guild)
        creation_channel_id = await guild_conf.creation_channel_id()

        if creation_channel_id is None:
            return

        temp_channels = set(await guild_conf.temp_channels())

        # Leaving a tracked temp channel: schedule deletion if no human members remain.
        # Music bots do not keep temp channels alive. Bot-only channel = empty.
        if before.channel and before.channel.id in temp_channels:
            if isinstance(before.channel, discord.VoiceChannel) and not self._has_human_members(before.channel):
                await self._schedule_delete_temp_channel(before.channel)
            elif isinstance(before.channel, discord.VoiceChannel):
                await self._mark_control_panel_dirty(before.channel)

        # Joining a tracked temp channel: cancel pending deletion.
        if after.channel and after.channel.id in temp_channels:
            self._cancel_delete_task(after.channel.id)
            if isinstance(after.channel, discord.VoiceChannel):
                await self._mark_control_panel_dirty(after.channel)

        # Joining the creator room: create/reuse temp channel.
        if after.channel and after.channel.id == creation_channel_id:
            if isinstance(after.channel, discord.VoiceChannel):
                await self._handle_creation_join(member, after.channel)

    # ==========================================================
    # Creation logic
    # ==========================================================

    async def _handle_creation_join(
        self,
        member: discord.Member,
        base_channel: discord.VoiceChannel,
    ) -> None:
        guild = base_channel.guild

        async with self._get_guild_lock(guild.id):
            existing_id = await self._get_owner_channel_id(guild, member.id)

            if existing_id is not None:
                existing_channel = guild.get_channel(existing_id)

                if isinstance(existing_channel, discord.VoiceChannel):
                    self._cancel_delete_task(existing_channel.id)

                    moved = True
                    try:
                        await member.move_to(
                            existing_channel,
                            reason="Moved member back to their existing temporary voice channel.",
                        )
                    except discord.Forbidden:
                        moved = False
                        log.warning(
                            "Missing permission to move user %s to existing temp channel %s in guild %s",
                            member.id,
                            existing_id,
                            guild.id,
                        )
                    except discord.HTTPException:
                        moved = False
                        log.exception(
                            "Failed to move user %s to existing temp channel %s in guild %s",
                            member.id,
                            existing_id,
                            guild.id,
                        )

                    if moved:
                        await self._send_control_panel(existing_channel)
                    return

            cooldown_remaining = self._get_creation_cooldown_remaining(guild.id, member.id)
            if cooldown_remaining > 0:
                await self._reject_creation_cooldown(member, base_channel, cooldown_remaining)
                return

            me = guild.me
            if me is None:
                log.warning("Could not resolve bot member in guild %s", guild.id)
                return

            perms = me.guild_permissions
            if not perms.manage_channels:
                log.warning("Missing Manage Channels in guild %s", guild.id)
                return

            if not perms.move_members:
                log.warning("Missing Move Members in guild %s", guild.id)
                return

            guild_conf = self.config.guild(guild)

            name_template = await guild_conf.name_template()

            # IMPORTANT:
            # We are already inside the guild lock here.
            # Do not call get_next_counter(), because that would try to acquire
            # the same asyncio.Lock again and deadlock.
            counter = await self._get_next_counter_unlocked(guild)

            channel_name = self._render_channel_name(name_template, member, counter)

            overwrites = base_channel.overwrites.copy()
            overwrites[member] = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
            )

            category = await self._resolve_temp_category(guild, base_channel)

            try:
                log.info("Creating temp voice channel for user %s in guild %s", member.id, guild.id)

                new_channel = await guild.create_voice_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites,
                    reason=f"Temporary voice channel created for {member} ({member.id}).",
                )

                log.info(
                    "Created temp voice channel %s for user %s in guild %s",
                    new_channel.id,
                    member.id,
                    guild.id,
                )

            except discord.Forbidden:
                log.warning("Missing permission to create temp voice channel in guild %s", guild.id)
                return
            except discord.HTTPException:
                log.exception("Failed to create temp voice channel in guild %s", guild.id)
                return

            self._set_creation_cooldown(guild.id, member.id)
            await self._add_temp_channel(guild, new_channel.id)
            await self._set_owner_channel(guild, member.id, new_channel.id)
            await self._write_guild_snapshot(guild)

            try:
                await member.move_to(
                    new_channel,
                    reason="Moved member into their temporary voice channel.",
                )
            except discord.Forbidden:
                log.warning(
                    "Missing permission to move user %s into temp channel %s in guild %s",
                    member.id,
                    new_channel.id,
                    guild.id,
                )
                await self._schedule_delete_temp_channel(new_channel)
                return
            except discord.HTTPException:
                log.exception(
                    "Failed to move user %s into temp channel %s in guild %s",
                    member.id,
                    new_channel.id,
                    guild.id,
                )
                await self._schedule_delete_temp_channel(new_channel)
                return

            await self._send_control_panel(new_channel)

            if not self._has_human_members(new_channel):
                await self._schedule_delete_temp_channel(new_channel)

    async def _resolve_temp_category(
        self,
        guild: discord.Guild,
        base_channel: discord.VoiceChannel,
    ) -> Optional[discord.CategoryChannel]:
        temp_category_id = await self.config.guild(guild).temp_category_id()

        if temp_category_id is not None:
            cat_obj = guild.get_channel(temp_category_id)
            if isinstance(cat_obj, discord.CategoryChannel):
                return cat_obj

        return base_channel.category

    def _render_channel_name(self, template: str, member: discord.Member, counter: int) -> str:
        tag = str(member)

        try:
            rendered = template.format(
                user=member.display_name,
                id=member.id,
                tag=tag,
                counter=counter,
            )
        except Exception:
            rendered = f"{member.display_name}'s channel"

        return self._sanitize_channel_name(rendered, member)

    @staticmethod
    def _sanitize_channel_name(name: str, member: discord.Member) -> str:
        name = name.replace("\n", " ").replace("\r", " ").strip()

        if not name:
            name = f"{member.display_name}'s channel"

        return name[:100]

    @staticmethod
    def _has_human_members(channel: discord.VoiceChannel) -> bool:
        """Return True when at least one non-bot member is inside the channel."""
        return any(not member.bot for member in channel.members)

    # ==========================================================
    # Creation cooldown helpers
    # ==========================================================

    def _get_creation_cooldown_remaining(self, guild_id: int, user_id: int) -> float:
        key = (guild_id, user_id)
        now = asyncio.get_running_loop().time()
        last_created = self._creation_cooldowns.get(key)

        if last_created is None:
            return 0

        remaining = CREATION_COOLDOWN_SECONDS - (now - last_created)
        if remaining <= 0:
            self._creation_cooldowns.pop(key, None)
            return 0

        return remaining

    def _set_creation_cooldown(self, guild_id: int, user_id: int) -> None:
        now = asyncio.get_running_loop().time()
        self._creation_cooldowns[(guild_id, user_id)] = now

        expired_before = now - CREATION_COOLDOWN_SECONDS
        expired_keys = [
            key for key, timestamp in self._creation_cooldowns.items()
            if timestamp < expired_before
        ]
        for key in expired_keys:
            self._creation_cooldowns.pop(key, None)

    async def _reject_creation_cooldown(
        self,
        member: discord.Member,
        base_channel: discord.VoiceChannel,
        remaining: float,
    ) -> None:
        try:
            await member.move_to(
                None,
                reason="Temporary voice channel creation cooldown.",
            )
        except discord.Forbidden:
            log.warning(
                "Missing permission to disconnect user %s from creator room %s in guild %s during cooldown.",
                member.id,
                base_channel.id,
                base_channel.guild.id,
            )
        except discord.HTTPException:
            log.exception(
                "Failed to disconnect user %s from creator room %s in guild %s during cooldown.",
                member.id,
                base_channel.id,
                base_channel.guild.id,
            )

        log.info(
            "Blocked temp voice channel creation for user %s in guild %s; %.1f seconds remain.",
            member.id,
            base_channel.guild.id,
            remaining,
        )

    # ==========================================================
    # Control panel helpers
    # ==========================================================

    async def _mark_control_panel_dirty(self, channel: discord.VoiceChannel) -> None:
        async with self._dirty_panel_lock:
            self._dirty_panel_channels.add((channel.guild.id, channel.id))

    async def _panel_refresh_worker(self) -> None:
        try:
            await self.bot.wait_until_ready()

            while True:
                await asyncio.sleep(PANEL_REFRESH_INTERVAL_SECONDS)

                async with self._dirty_panel_lock:
                    dirty_channels = list(self._dirty_panel_channels)
                    self._dirty_panel_channels.clear()

                for guild_id, channel_id in dirty_channels:
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        continue

                    channel = guild.get_channel(channel_id)
                    if not isinstance(channel, discord.VoiceChannel):
                        continue

                    try:
                        await self._refresh_control_panel(channel)
                    except Exception:
                        log.exception(
                            "Unhandled error while refreshing dirty VCH panel for channel %s in guild %s",
                            channel_id,
                            guild_id,
                        )

                    await asyncio.sleep(PANEL_REFRESH_EDIT_DELAY_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _send_control_panel(
        self,
        channel: discord.VoiceChannel,
        *,
        force_new: bool = False,
    ) -> bool:
        """Send or refresh the button panel in a temporary voice channel chat."""
        existing_id = await self._get_control_panel_message_id(channel.guild, channel.id)
        if existing_id is not None and not force_new:
            await self._refresh_control_panel(channel)
            return True

        if existing_id is not None:
            await self._clear_control_panel_by_channel(channel.guild, channel.id)

        send = getattr(channel, "send", None)
        if send is None:
            log.warning(
                "This discord.py build does not expose voice channel text chat for channel %s in guild %s",
                channel.id,
                channel.guild.id,
            )
            return False

        try:
            message = await send(
                embed=await self._build_control_panel_embed(channel),
                view=VCHControlPanelView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            log.warning(
                "Missing permission to send VCH control panel in channel %s in guild %s",
                channel.id,
                channel.guild.id,
            )
            return False
        except discord.HTTPException:
            log.exception(
                "Failed to send VCH control panel in channel %s in guild %s",
                channel.id,
                channel.guild.id,
            )
            return False

        await self._set_control_panel_message(channel.guild, channel.id, message.id)
        await self._write_guild_snapshot(channel.guild)
        return True

    async def _refresh_control_panel(self, channel: discord.VoiceChannel) -> None:
        message_id = await self._get_control_panel_message_id(channel.guild, channel.id)
        if message_id is None:
            return

        try:
            if hasattr(channel, "get_partial_message"):
                message = channel.get_partial_message(message_id)
            elif hasattr(channel, "fetch_message"):
                message = await channel.fetch_message(message_id)
            else:
                return

            await message.edit(
                embed=await self._build_control_panel_embed(channel),
                view=VCHControlPanelView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            await self._clear_control_panel_by_channel(channel.guild, channel.id)
            await self._write_guild_snapshot(channel.guild)
        except discord.Forbidden:
            log.warning(
                "Missing permission to refresh VCH control panel in channel %s in guild %s",
                channel.id,
                channel.guild.id,
            )
        except discord.HTTPException:
            log.exception(
                "Failed to refresh VCH control panel in channel %s in guild %s",
                channel.id,
                channel.guild.id,
            )

    async def _build_control_panel_embed(self, channel: discord.VoiceChannel) -> discord.Embed:
        owner_id = await self._get_owner_id_by_channel(channel.guild, channel.id)
        owner = channel.guild.get_member(owner_id) if owner_id is not None else None
        owner_text = owner.mention if owner is not None else (f"<@{owner_id}>" if owner_id else "Unclaimed")

        locked = self._is_panel_channel_locked(channel)
        hidden = self._is_panel_channel_hidden(channel)
        lock_text = "Locked" if locked else "Unlocked"
        visible_text = "Hidden" if hidden else "Visible"

        created_at = getattr(channel, "created_at", None)
        if created_at is not None:
            created_text = f"<t:{int(created_at.timestamp())}:R>"
        else:
            created_text = "Unknown"

        human_count = len([member for member in channel.members if not member.bot])
        limit_text = "unlimited" if channel.user_limit == 0 else str(channel.user_limit)

        embed = discord.Embed(
            title="Voice Channel Control Panel",
            description="Use the buttons below to manage this temporary voice channel.\nUse `/voicedashboard` to bring up this panel again.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Name", value=channel.name, inline=False)
        embed.add_field(name="Owner", value=owner_text, inline=True)
        embed.add_field(name="Status", value=f"{lock_text} / {visible_text}", inline=True)
        embed.add_field(name="Created", value=created_text, inline=True)
        embed.add_field(name="People", value=f"{human_count}/{limit_text}", inline=True)
        return embed

    async def get_panel_channel(
        self,
        interaction: discord.Interaction,
        *,
        require_owner: bool,
        expected_channel_id: Optional[int] = None,
    ) -> Optional[discord.VoiceChannel]:
        guild = interaction.guild
        if guild is None:
            await self._send_panel_response(interaction, "This panel can only be used in a server.")
            return None

        member = interaction.user
        if not isinstance(member, discord.Member):
            await self._send_panel_response(interaction, "Could not resolve your server membership.")
            return None

        if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
            await self._send_panel_response(
                interaction,
                "You must be connected to this temporary voice channel to use its panel.",
            )
            return None

        channel = member.voice.channel
        temp_channels = await self.get_temp_channels(guild)
        if channel.id not in temp_channels:
            await self._send_panel_response(
                interaction,
                "This panel can only control a managed temporary voice channel.",
            )
            return None

        if expected_channel_id is not None and channel.id != expected_channel_id:
            await self._send_panel_response(
                interaction,
                "Join the voice channel this panel belongs to before using it.",
            )
            return None

        if interaction.channel_id is not None and interaction.channel_id != channel.id:
            await self._send_panel_response(
                interaction,
                "Use the panel from the voice channel you are currently connected to.",
            )
            return None

        if require_owner and not await self._is_panel_channel_owner(member, channel):
            await self._send_panel_response(
                interaction,
                "Only this temporary voice channel's owner can use that control.",
            )
            return None

        return channel

    async def panel_change_name(
        self,
        interaction: discord.Interaction,
        channel_id: int,
        new_name: str,
    ) -> None:
        channel = await self.get_panel_channel(
            interaction,
            require_owner=True,
            expected_channel_id=channel_id,
        )
        if channel is None:
            return

        new_name = new_name.replace("\n", " ").replace("\r", " ").strip()
        if not new_name:
            await self._send_panel_response(interaction, "Channel name cannot be empty.")
            return

        try:
            await channel.edit(
                name=new_name[:100],
                reason="Temporary voice channel renamed from control panel.",
            )
        except (discord.Forbidden, discord.HTTPException):
            await self._send_panel_response(interaction, "Failed to rename this channel.")
            return

        await self._refresh_control_panel(channel)
        await self._send_panel_response(interaction, f"Channel renamed to `{new_name[:100]}`.")

    async def panel_change_limit(
        self,
        interaction: discord.Interaction,
        channel_id: int,
        raw_limit: str,
    ) -> None:
        channel = await self.get_panel_channel(
            interaction,
            require_owner=True,
            expected_channel_id=channel_id,
        )
        if channel is None:
            return

        try:
            limit = int(raw_limit.strip())
        except ValueError:
            await self._send_panel_response(interaction, "Enter a number from 0 to 99.")
            return

        limit = max(0, min(99, limit))

        try:
            await channel.edit(
                user_limit=limit,
                reason="Temporary voice channel user limit changed from control panel.",
            )
        except (discord.Forbidden, discord.HTTPException):
            await self._send_panel_response(interaction, "Failed to change the user limit.")
            return

        await self._refresh_control_panel(channel)
        limit_text = "unlimited" if limit == 0 else str(limit)
        await self._send_panel_response(interaction, f"User limit is now {limit_text}.")

    async def panel_lock(self, interaction: discord.Interaction) -> None:
        channel = await self.get_panel_channel(interaction, require_owner=True)
        if channel is None:
            return

        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.connect = False

        try:
            await self._set_panel_overwrite(
                channel,
                channel.guild.default_role,
                overwrite,
                "Temporary voice channel locked from control panel.",
            )
            await self._ensure_owner_panel_permissions(channel, interaction.user)
        except (discord.Forbidden, discord.HTTPException):
            await self._send_panel_response(interaction, "Failed to lock this channel.")
            return

        await self._refresh_control_panel(channel)
        await self._send_panel_response(interaction, "Channel locked.")

    async def panel_unlock(self, interaction: discord.Interaction) -> None:
        channel = await self.get_panel_channel(interaction, require_owner=True)
        if channel is None:
            return

        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.connect = None

        try:
            await self._set_panel_overwrite(
                channel,
                channel.guild.default_role,
                overwrite,
                "Temporary voice channel unlocked from control panel.",
            )
        except (discord.Forbidden, discord.HTTPException):
            await self._send_panel_response(interaction, "Failed to unlock this channel.")
            return

        await self._refresh_control_panel(channel)
        await self._send_panel_response(interaction, "Channel unlocked.")

    async def panel_hide(self, interaction: discord.Interaction) -> None:
        channel = await self.get_panel_channel(interaction, require_owner=True)
        if channel is None:
            return

        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.view_channel = False

        try:
            await self._set_panel_overwrite(
                channel,
                channel.guild.default_role,
                overwrite,
                "Temporary voice channel hidden from control panel.",
            )
            await self._ensure_owner_panel_permissions(channel, interaction.user)
        except (discord.Forbidden, discord.HTTPException):
            await self._send_panel_response(interaction, "Failed to hide this channel.")
            return

        await self._refresh_control_panel(channel)
        await self._send_panel_response(interaction, "Channel hidden.")

    async def panel_unhide(self, interaction: discord.Interaction) -> None:
        channel = await self.get_panel_channel(interaction, require_owner=True)
        if channel is None:
            return

        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.view_channel = None

        try:
            await self._set_panel_overwrite(
                channel,
                channel.guild.default_role,
                overwrite,
                "Temporary voice channel unhidden from control panel.",
            )
        except (discord.Forbidden, discord.HTTPException):
            await self._send_panel_response(interaction, "Failed to unhide this channel.")
            return

        await self._refresh_control_panel(channel)
        await self._send_panel_response(interaction, "Channel unhidden.")

    async def panel_show_kick_select(self, interaction: discord.Interaction) -> None:
        channel = await self.get_panel_channel(interaction, require_owner=True)
        if channel is None:
            return

        owner = interaction.user
        if not isinstance(owner, discord.Member):
            await self._send_panel_response(interaction, "Could not resolve your server membership.")
            return

        targets = [member for member in channel.members if not member.bot and member.id != owner.id]
        if not targets:
            await self._send_panel_response(interaction, "There is nobody else in this channel to kick.")
            return

        await interaction.followup.send(
            "Select a member to disconnect and block from reconnecting to this room.",
            view=VCHKickSelectView(self, channel, owner),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def panel_kick_member(
        self,
        interaction: discord.Interaction,
        channel_id: int,
        member_id: int,
    ) -> None:
        channel = await self.get_panel_channel(
            interaction,
            require_owner=True,
            expected_channel_id=channel_id,
        )
        if channel is None:
            return

        guild = channel.guild
        member = guild.get_member(member_id)
        if member is None or not member.voice or member.voice.channel != channel:
            await self._send_panel_response(interaction, "That member is no longer in this channel.")
            return

        if member.id == interaction.user.id:
            await self._send_panel_response(interaction, "You cannot kick yourself from this panel.")
            return

        overwrite = channel.overwrites_for(member)
        overwrite.connect = False

        try:
            await self._set_panel_overwrite(
                channel,
                member,
                overwrite,
                "Temporary voice channel member blocked from reconnecting after kick.",
            )
            await member.move_to(
                None,
                reason="Temporary voice channel member kicked from control panel.",
            )
        except (discord.Forbidden, discord.HTTPException):
            await self._send_panel_response(interaction, "Failed to kick that member.")
            return

        await self._refresh_control_panel(channel)
        await self._send_panel_response(
            interaction,
            f"{member.mention} was disconnected and blocked from reconnecting to this room.",
        )

    async def panel_claim(self, interaction: discord.Interaction) -> None:
        channel = await self.get_panel_channel(interaction, require_owner=False)
        if channel is None:
            return

        claimant = interaction.user
        if not isinstance(claimant, discord.Member):
            await self._send_panel_response(interaction, "Could not resolve your server membership.")
            return

        owner_id = await self._get_owner_id_by_channel(channel.guild, channel.id)
        if owner_id == claimant.id:
            await self._send_panel_response(interaction, "You already own this channel.")
            return

        current_owner = channel.guild.get_member(owner_id) if owner_id is not None else None
        if current_owner is not None and current_owner.voice and current_owner.voice.channel == channel:
            await self._send_panel_response(
                interaction,
                "The current owner is still in this channel, so it cannot be claimed.",
            )
            return

        overwrites = channel.overwrites.copy()
        if owner_id is not None:
            for target in list(overwrites):
                if isinstance(target, discord.Member) and target.id == owner_id:
                    overwrites.pop(target, None)

        overwrites[claimant] = self._owner_panel_overwrite(channel.overwrites_for(claimant))

        try:
            await channel.edit(
                overwrites=overwrites,
                reason="Temporary voice channel claimed from control panel.",
            )
        except (discord.Forbidden, discord.HTTPException):
            await self._send_panel_response(interaction, "Failed to claim this channel.")
            return

        await self._clear_owner_by_channel(channel.guild, channel.id)
        await self._set_owner_channel(channel.guild, claimant.id, channel.id)
        await self._write_guild_snapshot(channel.guild)
        await self._refresh_control_panel(channel)
        await self._send_panel_response(interaction, "You are now the owner of this channel.")

    async def _send_panel_response(self, interaction: discord.Interaction, content: str) -> None:
        kwargs = {
            "content": content,
            "ephemeral": True,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    async def _set_panel_overwrite(
        self,
        channel: discord.VoiceChannel,
        target,
        overwrite: discord.PermissionOverwrite,
        reason: str,
    ) -> None:
        if overwrite.is_empty():
            await channel.set_permissions(target, overwrite=None, reason=reason)
        else:
            await channel.set_permissions(target, overwrite=overwrite, reason=reason)

    async def _ensure_owner_panel_permissions(
        self,
        channel: discord.VoiceChannel,
        owner,
    ) -> None:
        if not isinstance(owner, discord.Member):
            return

        overwrite = self._owner_panel_overwrite(channel.overwrites_for(owner))
        await self._set_panel_overwrite(
            channel,
            owner,
            overwrite,
            "Temporary voice channel owner permissions refreshed from control panel.",
        )

    @staticmethod
    def _owner_panel_overwrite(
        overwrite: discord.PermissionOverwrite,
    ) -> discord.PermissionOverwrite:
        overwrite.view_channel = True
        overwrite.connect = True
        overwrite.speak = True
        overwrite.manage_channels = True
        overwrite.move_members = True
        overwrite.mute_members = True
        overwrite.deafen_members = True
        return overwrite

    @staticmethod
    def _is_panel_channel_locked(channel: discord.VoiceChannel) -> bool:
        return channel.overwrites_for(channel.guild.default_role).connect is False

    @staticmethod
    def _is_panel_channel_hidden(channel: discord.VoiceChannel) -> bool:
        return channel.overwrites_for(channel.guild.default_role).view_channel is False

    # ==========================================================
    # Deletion logic
    # ==========================================================

    async def _schedule_delete_temp_channel(self, channel: discord.VoiceChannel) -> None:
        if channel.id in self._delete_tasks:
            return

        if self._has_human_members(channel):
            return

        task = asyncio.create_task(
            self._delete_temp_channel_after_delay(channel.guild.id, channel.id)
        )

        self._delete_tasks[channel.id] = task

    async def _delete_temp_channel_after_delay(self, guild_id: int, channel_id: int) -> None:
        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return

            delay = await self.get_delete_delay(guild)
            await asyncio.sleep(delay)

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return

            channel = guild.get_channel(channel_id)

            if not isinstance(channel, discord.VoiceChannel):
                await self._remove_temp_channel(guild, channel_id)
                await self._clear_owner_by_channel(guild, channel_id)
                await self._clear_control_panel_by_channel(guild, channel_id)
                await self._write_guild_snapshot(guild)
                return

            if self._has_human_members(channel):
                return

            try:
                await channel.delete(reason="Temporary voice channel has no human members.")
            except discord.NotFound:
                pass
            except discord.Forbidden:
                log.warning("Missing permission to delete temp channel %s in guild %s", channel_id, guild_id)
                return
            except discord.HTTPException:
                log.exception("Failed to delete temp channel %s in guild %s", channel_id, guild_id)
                return

            await self._remove_temp_channel(guild, channel_id)
            await self._clear_owner_by_channel(guild, channel_id)
            await self._clear_control_panel_by_channel(guild, channel_id)
            await self._write_guild_snapshot(guild)

        except asyncio.CancelledError:
            raise
        finally:
            self._delete_tasks.pop(channel_id, None)

    def _cancel_delete_task(self, channel_id: int) -> None:
        task = self._delete_tasks.pop(channel_id, None)

        if task and not task.done():
            task.cancel()

    # ==========================================================
    # Config helpers
    # ==========================================================

    async def _add_temp_channel(self, guild: discord.Guild, channel_id: int) -> None:
        async with self.config.guild(guild).temp_channels() as channels:
            if channel_id not in channels:
                channels.append(channel_id)

    async def _remove_temp_channel(self, guild: discord.Guild, channel_id: int) -> None:
        async with self.config.guild(guild).temp_channels() as channels:
            while channel_id in channels:
                channels.remove(channel_id)

    async def _set_owner_channel(self, guild: discord.Guild, user_id: int, channel_id: int) -> None:
        async with self.config.guild(guild).owner_channels() as owners:
            owners[str(user_id)] = channel_id

    async def _get_owner_channel_id(self, guild: discord.Guild, user_id: int) -> Optional[int]:
        owners = await self.config.guild(guild).owner_channels()
        chan_id = owners.get(str(user_id))

        if chan_id is None:
            return None

        try:
            chan_id = int(chan_id)
        except (TypeError, ValueError):
            async with self.config.guild(guild).owner_channels() as owners_mut:
                owners_mut.pop(str(user_id), None)
            await self._write_guild_snapshot(guild)
            return None

        channel = guild.get_channel(chan_id)

        if not isinstance(channel, discord.VoiceChannel):
            async with self.config.guild(guild).owner_channels() as owners_mut:
                owners_mut.pop(str(user_id), None)

            await self._remove_temp_channel(guild, chan_id)
            await self._clear_control_panel_by_channel(guild, chan_id)
            await self._write_guild_snapshot(guild)
            return None

        return chan_id

    async def _clear_owner_by_channel(self, guild: discord.Guild, channel_id: int) -> None:
        async with self.config.guild(guild).owner_channels() as owners:
            to_delete = [
                uid for uid, cid in owners.items()
                if str(cid) == str(channel_id)
            ]

            for uid in to_delete:
                owners.pop(uid, None)

    async def _get_owner_id_by_channel(self, guild: discord.Guild, channel_id: int) -> Optional[int]:
        owners = await self.config.guild(guild).owner_channels()
        for uid, cid in owners.items():
            if str(cid) != str(channel_id):
                continue

            try:
                return int(uid)
            except (TypeError, ValueError):
                continue

        return None

    async def _is_panel_channel_owner(
        self,
        member: discord.Member,
        channel: discord.VoiceChannel,
    ) -> bool:
        owner_id = await self._get_owner_id_by_channel(channel.guild, channel.id)
        return owner_id == member.id

    async def _get_control_panel_message_id(
        self,
        guild: discord.Guild,
        channel_id: int,
    ) -> Optional[int]:
        panels = await self.config.guild(guild).control_panels()
        message_id = panels.get(str(channel_id))
        if message_id is None:
            return None

        try:
            return int(message_id)
        except (TypeError, ValueError):
            await self._clear_control_panel_by_channel(guild, channel_id)
            return None

    async def _set_control_panel_message(
        self,
        guild: discord.Guild,
        channel_id: int,
        message_id: int,
    ) -> None:
        async with self.config.guild(guild).control_panels() as panels:
            panels[str(channel_id)] = message_id

    async def _clear_control_panel_by_channel(
        self,
        guild: discord.Guild,
        channel_id: int,
    ) -> None:
        async with self.config.guild(guild).control_panels() as panels:
            panels.pop(str(channel_id), None)

        async with self._dirty_panel_lock:
            self._dirty_panel_channels.discard((guild.id, channel_id))
