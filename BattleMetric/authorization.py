"""Reusable permission checks for BattleMetric commands."""

from redbot.core import commands


class BattleMetricAuthorizationError(commands.CheckFailure):
    """Raised when a member does not have the required BattleMetric access."""

    def __init__(self, message: str = "You are not authorized to use this BattleMetric command."):
        super().__init__(message)


async def _is_authorized_user(ctx: commands.Context) -> bool:
    if ctx.cog is not None and await ctx.cog.is_authorized(ctx.author):
        return True
    raise BattleMetricAuthorizationError()


async def _is_authorization_manager(ctx: commands.Context) -> bool:
    if ctx.cog is not None and await ctx.cog.can_manage_authorization(ctx.author):
        return True
    raise BattleMetricAuthorizationError("You are not allowed to manage BattleMetric authorization.")


async def _has_battlemetric_access(ctx: commands.Context) -> bool:
    if ctx.cog is not None and (
        await ctx.cog.is_authorized(ctx.author)
        or await ctx.cog.can_manage_authorization(ctx.author)
    ):
        return True
    raise BattleMetricAuthorizationError()


def requires_authorized_user():
    """Require an explicitly authorized member or bot owner."""
    return commands.check(_is_authorized_user)


def requires_authorization_manager():
    """Require a bot owner or a guild member who can manage server settings."""
    return commands.check(_is_authorization_manager)


def requires_battlemetric_access():
    """Permit command-group routing for users and authorization managers."""
    return commands.check(_has_battlemetric_access)
