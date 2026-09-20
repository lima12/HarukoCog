from .haruadmins import HaruAdmins


async def setup(bot):
    """HaruAdmins entry point."""
    await bot.add_cog(HaruAdmins(bot))


__red_end_user_data_statement__ = (
    "This cog stores the Discord user ID, moderator ID, timeout expiration, and "
    "moderation reason for timeouts longer than Discord's 28-day limit. The data "
    "is removed when the managed timeout ends or is cancelled."
)
