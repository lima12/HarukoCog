import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from redbot.core import commands


log = logging.getLogger("red.HaruAdmins.bot_protection")

AUDIT_LOG_RETRY_DELAYS = (0, 1, 2)


class BotProtectionMixin:
    """Command and event handling for unapproved-bot protection."""

    def register_bot_protection_config(self) -> None:
        self.config.register_guild(
            bot_protection_enabled=False,
            bot_protection_role_id=None,
            bot_protection_channel_id=None,
            bot_protection_whitelist=[],
        )

    async def delete_bot_protection_user_data(
        self,
        guild_id: int,
        guild_data: dict,
        user_id: int,
    ) -> None:
        whitelist = guild_data.get("bot_protection_whitelist", [])
        if user_id not in whitelist:
            return
        await self.config.guild_from_id(guild_id).bot_protection_whitelist.set(
            [bot_id for bot_id in whitelist if bot_id != user_id]
        )

    @commands.hybrid_command(
        name="botprotection",
        description="Configure automatic removal of unapproved bots or whitelist one.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        notify_role="Role to notify when an unapproved bot joins.",
        channel="Text channel where bot-join alerts are sent.",
        toggle="Enable or disable protection. Defaults to enabled.",
        allow="Discord ID of a bot that should be allowed to join.",
    )
    @commands.guild_only()
    async def botprotection_command(
        self,
        ctx: commands.Context,
        notify_role: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None,
        toggle: bool = True,
        allow: Optional[str] = None,
    ) -> None:
        """Configure bot protection or add a bot ID to its whitelist."""
        guild = ctx.guild
        if guild is None or not isinstance(ctx.author, discord.Member):
            return

        if allow is not None:
            if notify_role is not None or channel is not None or toggle is not True:
                await self._send(ctx, "Use `allow` by itself when whitelisting a bot.")
                return
            if not await self._can_use_bot_allow(ctx):
                return
            await self._allow_bot(ctx, allow)
            return

        if not ctx.author.guild_permissions.manage_guild:
            await self._send(
                ctx,
                "You need the Manage Server permission to configure bot protection.",
            )
            return

        current_role_id = await self.config.guild(guild).bot_protection_role_id()
        current_channel_id = await self.config.guild(guild).bot_protection_channel_id()

        selected_role = notify_role
        if selected_role is None and current_role_id is not None:
            selected_role = guild.get_role(current_role_id)

        selected_channel = channel
        if selected_channel is None and current_channel_id is not None:
            existing_channel = guild.get_channel(current_channel_id)
            if isinstance(existing_channel, discord.TextChannel):
                selected_channel = existing_channel

        if toggle:
            if selected_role is None or selected_channel is None:
                await self._send(
                    ctx,
                    "Choose both `notify_role` and `channel` the first time bot "
                    "protection is enabled.",
                )
                return

            validation_error = self._bot_protection_setup_error(
                guild,
                selected_role,
                selected_channel,
            )
            if validation_error is not None:
                await self._send(ctx, validation_error)
                return

        async with self._guild_lock(guild.id):
            conf = self.config.guild(guild)
            if notify_role is not None:
                await conf.bot_protection_role_id.set(notify_role.id)
            if channel is not None:
                await conf.bot_protection_channel_id.set(channel.id)
            await conf.bot_protection_enabled.set(toggle)

        if not toggle:
            await self._send(ctx, "Bot protection is now disabled.")
            return

        audit_note = ""
        bot_member = guild.me
        if bot_member is not None and not bot_member.guild_permissions.view_audit_log:
            audit_note = (
                " I can still kick and alert, but identifying the inviter requires "
                "the View Audit Log permission."
            )
        await self._send(
            ctx,
            "Bot protection is now enabled. Unapproved bots will be kicked and "
            f"{selected_role.mention} will be notified in {selected_channel.mention}."
            f" Members with {selected_role.mention} are the only members who can "
            f"use `allow`.{audit_note}",
        )

    async def _can_use_bot_allow(self, ctx: commands.Context) -> bool:
        member = ctx.author
        if not isinstance(member, discord.Member):
            return False
        async with self._guild_lock(member.guild.id):
            role_id = await self.config.guild(member.guild).bot_protection_role_id()
        role = member.guild.get_role(role_id) if role_id is not None else None
        if role is None:
            await self._send(
                ctx,
                "Bot protection has no valid notification role. Ask an administrator "
                "to configure it first.",
            )
            return False
        if role not in member.roles:
            await self._send(
                ctx,
                f"Only members with {role.mention} can whitelist bots.",
            )
            return False
        return True

    async def _allow_bot(self, ctx: commands.Context, raw_bot_id: str) -> None:
        guild = ctx.guild
        if guild is None:
            return

        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)

        bot_id = self._parse_user_id(raw_bot_id)
        if bot_id is None:
            await self._send(ctx, "Enter a valid Discord bot ID in `allow`.")
            return

        try:
            user = self.bot.get_user(bot_id) or await self.bot.fetch_user(bot_id)
        except discord.NotFound:
            await self._send(ctx, "Discord could not find a user with that ID.")
            return
        except discord.HTTPException:
            log.exception("Could not fetch bot ID %s for the protection whitelist", bot_id)
            await self._send(ctx, "Discord could not look up that bot right now.")
            return

        if not user.bot:
            await self._send(ctx, "That Discord ID belongs to a user, not a bot.")
            return

        role_changed = False
        async with self._guild_lock(guild.id):
            role_id = await self.config.guild(guild).bot_protection_role_id()
            role = guild.get_role(role_id) if role_id is not None else None
            if role is None or role not in ctx.author.roles:
                role_changed = True
            else:
                async with self.config.guild(guild).bot_protection_whitelist() as whitelist:
                    if bot_id not in whitelist:
                        whitelist.append(bot_id)

        if role_changed:
            await self._send(
                ctx,
                "The notification role changed while this command was running. "
                "Only members with the newly configured role can whitelist bots.",
            )
            return

        await self._send(
            ctx,
            f"Bot {user.name} with {bot_id} is now whitelisted, please re-invite them!",
        )

    def _bot_protection_setup_error(
        self,
        guild: discord.Guild,
        role: discord.Role,
        channel: discord.TextChannel,
    ) -> Optional[str]:
        if not self.bot.intents.members:
            return (
                "Enable Discord's Server Members Intent for this bot before bot "
                "protection is enabled."
            )
        if role.is_default():
            return "The `@everyone` role cannot be used as the notification role."

        bot_member = guild.me
        if bot_member is None:
            return "I could not resolve my server member record."

        guild_permissions = bot_member.guild_permissions
        if not guild_permissions.kick_members:
            return "I need the Kick Members permission before bot protection can be enabled."

        channel_permissions = channel.permissions_for(bot_member)
        if not channel_permissions.view_channel or not channel_permissions.send_messages:
            return f"I need View Channel and Send Messages in {channel.mention}."
        if not role.mentionable and not channel_permissions.mention_everyone:
            return (
                f"{role.mention} must be mentionable, or I need Mention @everyone, "
                "@here, and All Roles in the notification channel."
            )
        return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not member.bot:
            return

        async with self._guild_lock(member.guild.id):
            conf = self.config.guild(member.guild)
            protection_enabled = await conf.bot_protection_enabled()
            whitelist = await conf.bot_protection_whitelist()

        if not protection_enabled or member.id in whitelist:
            return

        joined_at = discord.utils.utcnow()
        kick_succeeded = False
        try:
            await member.kick(reason="HaruAdmins bot protection: bot is not whitelisted.")
            kick_succeeded = True
        except discord.Forbidden:
            log.warning(
                "Could not kick unapproved bot %s from guild %s due to permissions",
                member.id,
                member.guild.id,
            )
        except discord.HTTPException:
            log.exception(
                "Discord failed to kick unapproved bot %s from guild %s",
                member.id,
                member.guild.id,
            )

        inviter = await self._find_bot_inviter(member.guild, member.id, joined_at)
        await self._send_bot_protection_alert(
            member,
            inviter,
            joined_at,
            kick_succeeded=kick_succeeded,
        )

    async def _find_bot_inviter(
        self,
        guild: discord.Guild,
        bot_id: int,
        joined_at: datetime,
    ) -> Optional[discord.User]:
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.view_audit_log:
            return None

        for delay in AUDIT_LOG_RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                async for entry in guild.audit_logs(
                    limit=10,
                    action=discord.AuditLogAction.bot_add,
                ):
                    if getattr(entry.target, "id", None) != bot_id:
                        continue
                    age = (joined_at - entry.created_at).total_seconds()
                    if -5 <= age <= 30:
                        return entry.user
            except discord.Forbidden:
                return None
            except discord.HTTPException:
                log.warning("Could not read bot-add audit logs in guild %s", guild.id)
                return None
        return None

    async def _send_bot_protection_alert(
        self,
        bot_member: discord.Member,
        inviter: Optional[discord.User],
        joined_at: datetime,
        *,
        kick_succeeded: bool,
    ) -> None:
        guild = bot_member.guild
        conf = self.config.guild(guild)
        role_id = await conf.bot_protection_role_id()
        channel_id = await conf.bot_protection_channel_id()
        role = guild.get_role(role_id) if role_id is not None else None
        channel = guild.get_channel(channel_id) if channel_id is not None else None
        if role is None or not isinstance(channel, discord.TextChannel):
            log.error(
                "Bot protection could not alert guild %s because its role or channel is missing",
                guild.id,
            )
            return

        inviter_text = inviter.mention if inviter is not None else "Someone"
        bot_name = discord.utils.escape_markdown(bot_member.name)
        timestamp = discord.utils.format_dt(joined_at, style="F")
        outcome = ""
        if not kick_succeeded:
            outcome = (
                "\n**Warning:** I could not kick this bot. Check my Kick Members "
                "permission and role position."
            )
        content = (
            f"{role.mention}, {inviter_text} just tried to invite this bot into the server.\n"
            f"Bot info: {bot_name} | ID: `{bot_member.id}` | invited at {timestamp}\n"
            f"Please use `/botprotection allow:{bot_member.id}` to whitelist the bot."
            f"{outcome}"
        )
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            roles=[role],
            users=[inviter] if inviter is not None else False,
            replied_user=False,
        )
        try:
            await channel.send(content, allowed_mentions=allowed_mentions)
        except (discord.Forbidden, discord.HTTPException):
            log.exception(
                "Could not send bot protection alert for bot %s in guild %s",
                bot_member.id,
                guild.id,
            )

    @staticmethod
    def _parse_user_id(value: str) -> Optional[int]:
        normalized = value.strip()
        mention_match = re.fullmatch(r"<@!?(\d{15,22})>", normalized)
        if mention_match is not None:
            normalized = mention_match.group(1)
        if not re.fullmatch(r"\d{15,22}", normalized):
            return None
        return int(normalized)
