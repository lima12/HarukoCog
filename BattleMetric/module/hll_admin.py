"""Authorized, confirmed in-game admin changes through shared HLL RCON."""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands

from .hll_ban import HLLBanModule
from .hll_group import HLLVN_COMMAND_GROUP
from .kill_feed import KillFeedConnectionTestError

log = logging.getLogger("red.BattleMetric.hll_admin")


class HLLAdminValidationError(ValueError):
    """An admin operation cannot safely be submitted."""


class HLLAdminVerificationError(Exception):
    """A mutation was accepted but its resulting state was not verified."""


@dataclass(frozen=True)
class HLLAdminResult:
    eos_id: str
    action: str
    group: str | None
    changed: bool
    confirmed: bool
    needed: bool = True


class HLLAdminModule:
    """Manage the server admin list without changing Discord authorization."""

    DEFAULT_GROUP = "Admin"

    def __init__(self, cog: Any):
        self.cog = cog

    def register_config(self) -> None:
        self.cog.config.register_guild(hll_admin_group=self.DEFAULT_GROUP)

    @staticmethod
    def validate_group(name: str) -> str:
        if not isinstance(name, str) or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise HLLAdminValidationError("Enter an admin group name without control characters.")
        name = name.strip()
        if not name or len(name) > 100:
            raise HLLAdminValidationError("Enter an admin group name between 1 and 100 characters.")
        return name

    @classmethod
    def group_names(cls, response: Any) -> tuple[str, ...]:
        names = getattr(response, "group_names", None)
        if not isinstance(names, (list, tuple)) or not all(
            isinstance(name, str) and name.strip() for name in names
        ):
            raise HLLAdminValidationError("RCON returned an invalid admin group list. No change was submitted.")
        return tuple(cls.validate_group(name) for name in names)

    @classmethod
    def resolve_group(cls, response: Any, requested: str) -> str:
        names = cls.group_names(response)
        requested = cls.validate_group(requested)
        matches = [name for name in names if name.casefold() == requested.casefold()]
        if len(matches) != 1:
            raise HLLAdminValidationError(
                "That admin group is missing or ambiguous on the server. "
                "Configure a server-defined group with /hllvn admin group."
            )
        return cls.validate_group(matches[0])

    async def get_groups(self, guild: discord.Guild) -> tuple[str, ...]:
        response = await self.cog.kill_feed.execute_rcon(
            guild, "GetAdminGroups request", lambda client: client.get_admin_groups()
        )
        return self.group_names(response)

    @staticmethod
    def groups_embed(groups: tuple[str, ...], selected: str) -> tuple[discord.Embed, discord.File | None]:
        names = sorted(groups, key=str.casefold)
        description = "\n".join(f"- {discord.utils.escape_markdown(name)}" for name in names)
        attachment = None
        if len(description) > 3800:
            attachment = discord.File(
                io.BytesIO(("\n".join(names) + "\n").encode("utf-8")), filename="hllvn-admin-groups.txt"
            )
            description = f"{len(names)} groups returned. The complete list is attached."
        matches = [name for name in groups if name.casefold() == selected.casefold()]
        embed = discord.Embed(
            title="HLL VN Admin Groups",
            description=description or "No admin groups were returned by the server.",
            color=discord.Color.green() if len(matches) == 1 else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Promotion Group", value=discord.utils.escape_markdown(selected), inline=False)
        embed.add_field(
            name="Promotion Group Status",
            value="Available" if len(matches) == 1 else "Missing from server" if not matches else "Ambiguous name",
            inline=False,
        )
        embed.add_field(name="Group Permissions", value="Not returned by RCON; defined in server configuration.", inline=False)
        return embed, attachment

    @staticmethod
    def admin_users(response: Any) -> dict[str, str]:
        entries = getattr(response, "admin_users", None)
        if not isinstance(entries, (list, tuple)):
            raise HLLAdminValidationError("RCON returned an invalid admin list. No change was submitted.")
        users = {}
        for entry in entries:
            raw_id = getattr(entry, "user_id", None)
            group = getattr(entry, "group", None)
            eos_id = HLLBanModule.normalize_eos_id(raw_id) if isinstance(raw_id, str) else None
            if eos_id is None or not isinstance(group, str) or not group.strip() or eos_id in users:
                raise HLLAdminValidationError("RCON returned an invalid admin entry. No change was submitted.")
            users[eos_id] = group
        return users

    async def set_group(self, guild: discord.Guild, name: str) -> str:
        requested = self.validate_group(name)
        response = await self.cog.kill_feed.execute_rcon(
            guild, "GetAdminGroups request", lambda client: client.get_admin_groups()
        )
        group = self.resolve_group(response, requested)
        await self.cog.config.guild(guild).hll_admin_group.set(group)
        return group

    async def change(
        self, guild: discord.Guild, *, action: str, eos_id: str, confirm: bool = False
    ) -> HLLAdminResult:
        target = HLLBanModule.normalize_eos_id(eos_id)
        if target is None:
            raise HLLAdminValidationError("Provide a valid 17-digit or 32-character EOS ID.")
        if action not in {"promote", "demote"} or not isinstance(confirm, bool):
            raise HLLAdminValidationError("Choose promote or demote, and a true/false confirmation.")
        requested_group = None
        if action == "promote":
            requested_group = self.validate_group(await self.cog.config.guild(guild).hll_admin_group())

        async def operation(client: Any) -> HLLAdminResult:
            users = self.admin_users(await client.get_admin_users())
            current_group = users.get(target)
            if (action == "promote" and current_group is not None) or (
                action == "demote" and current_group is None
            ):
                return HLLAdminResult(target, action, current_group, False, confirm, needed=False)
            group = current_group
            if action == "promote":
                group = self.resolve_group(await client.get_admin_groups(), requested_group)
            if not confirm:
                return HLLAdminResult(target, action, group, False, False)

            if action == "promote":
                await client.add_admin(target, group, "Granted through Discord /hllvn admin")
            else:
                await client.remove_admin(target)
            # Read/check/mutate/verify share one guild RCON lock. Do not retry an
            # ambiguous write: it may already have granted or removed access.
            try:
                updated = self.admin_users(await client.get_admin_users())
            except HLLAdminValidationError as exc:
                raise HLLAdminVerificationError(
                    "The server accepted the request, but returned an invalid verification list. "
                    "Check the server admin list before retrying."
                ) from exc
            verified = updated.get(target) == group if action == "promote" else target not in updated
            if not verified:
                raise HLLAdminVerificationError(
                    "The server accepted the request, but the admin list does not confirm the change. "
                    "Check the server admin list before retrying."
                )
            return HLLAdminResult(target, action, group, True, True)

        try:
            return await self.cog.kill_feed.execute_rcon(guild, "HLL admin change requests", operation)
        except KillFeedConnectionTestError as exc:
            if isinstance(exc.__cause__, (HLLAdminValidationError, HLLAdminVerificationError)):
                raise exc.__cause__ from None
            raise


class HLLAdminCommandsMixin:
    """Explicitly authorized in-game admin commands, with safe previews."""

    hllvn = HLLVN_COMMAND_GROUP
    hllvn_admin = app_commands.Group(
        name="admin", description="Configure, grant, or revoke in-game admin access.", parent=hllvn
    )

    async def _hllvn_admin_request(
        self, interaction: discord.Interaction, *, action: str, eos_id: str = "",
        confirm: bool = False, name: str = "",
    ) -> None:
        if not await self.is_authorized(interaction.user):
            await interaction.response.send_message(
                "You are not authorized to use HLL: Vietnam administration commands.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            if action == "groups":
                groups = await self.hll_admin.get_groups(guild)
                selected = self.hll_admin.validate_group(await self.config.guild(guild).hll_admin_group())
                embed, attachment = self.hll_admin.groups_embed(groups, selected)
                kwargs = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
                if attachment is not None:
                    kwargs["file"] = attachment
                try:
                    await interaction.followup.send(**kwargs)
                finally:
                    if attachment is not None:
                        attachment.close()
                return
            if action == "group":
                group = await self.hll_admin.set_group(guild, name)
                await interaction.followup.send(
                    f"Future promotions will use the server admin group `{discord.utils.escape_markdown(group)}`. "
                    "Existing admins were not changed.", allowed_mentions=discord.AllowedMentions.none()
                )
                return
            result = await self.hll_admin.change(guild, action=action, eos_id=eos_id, confirm=confirm)
        except asyncio.CancelledError:
            raise
        except (HLLAdminVerificationError, ValueError) as exc:
            await interaction.followup.send(str(exc), allowed_mentions=discord.AllowedMentions.none())
            return
        except KillFeedConnectionTestError:
            if action == "groups":
                await interaction.followup.send("Could not read the server admin groups. No access was changed.")
                return
            await interaction.followup.send(
                "RCON admin state could not be confirmed. A confirmed request may already have "
                "changed access. Check the server admin list before retrying.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except RuntimeError:
            await interaction.followup.send("The required `hllrcon` dependency is unavailable.")
            return
        except Exception:
            log.exception("Unexpected HLL admin %s failure for guild %s", action, guild.id)
            await interaction.followup.send(
                "The admin request could not be confirmed. Check the server admin list before "
                "retrying and ask the bot owner to check the Red service log."
            )
            return

        if result.changed:
            description = (
                "Admin access granted and verified."
                if action == "promote" else "Admin access removed and verified."
            )
        elif not result.needed:
            description = (
                "No change needed: the player is already an admin."
                if action == "promote" else "No change needed: the player is not an admin."
            )
        else:
            description = "Preview only. No admin access was changed. Repeat with confirm:true to apply."
        embed = discord.Embed(
            title="HLL VN Admin Change" if confirm else "HLL VN Admin Preview",
            description=description,
            color=discord.Color.green() if result.changed else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Action", value=action.title(), inline=True)
        embed.add_field(name="EOS ID", value=f"`{result.eos_id}`", inline=False)
        if result.group is not None:
            embed.add_field(name="Server Admin Group", value=result.group, inline=False)
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @hllvn_admin.command(name="promote", description="Preview or confirm granting in-game admin access.")
    @app_commands.describe(
        eos_id="Game account ID (17-digit or 32-character EOS ID).",
        confirm="Apply the change; false previews only.",
    )
    @app_commands.guild_only()
    async def hllvn_admin_promote(
        self, interaction: discord.Interaction, eos_id: str, confirm: bool = False
    ) -> None:
        await self._hllvn_admin_request(interaction, action="promote", eos_id=eos_id, confirm=confirm)

    @hllvn_admin.command(name="demote", description="Preview or confirm removing in-game admin access.")
    @app_commands.describe(
        eos_id="Game account ID (17-digit or 32-character EOS ID).",
        confirm="Apply the change; false previews only.",
    )
    @app_commands.guild_only()
    async def hllvn_admin_demote(
        self, interaction: discord.Interaction, eos_id: str, confirm: bool = False
    ) -> None:
        await self._hllvn_admin_request(interaction, action="demote", eos_id=eos_id, confirm=confirm)

    @hllvn_admin.command(name="group", description="Set the server-defined group used for future admin promotions.")
    @app_commands.describe(name="An existing in-game server admin group, such as Admin.")
    @app_commands.guild_only()
    async def hllvn_admin_group(self, interaction: discord.Interaction, name: str) -> None:
        await self._hllvn_admin_request(interaction, action="group", name=name)

    @hllvn_admin.command(name="groups", description="List available in-game admin groups and the promotion group.")
    @app_commands.guild_only()
    async def hllvn_admin_groups(self, interaction: discord.Interaction) -> None:
        await self._hllvn_admin_request(interaction, action="groups")
