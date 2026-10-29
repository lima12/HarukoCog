from .haruadmins import HaruAdmins

async def setup(bot):
    """HaruAdmins entry point."""
    await bot.add_cog(HaruAdmins(bot))

__red_end_user_data_statement__ = (
    "This cog stores the Discord user ID, moderator ID, timeout expiration, and "
    "moderation reason for timeouts longer than Discord's 28-day limit. The data "
    "is removed when the managed timeout ends or is cancelled. It also stores "
    "bot-protection role and channel IDs and whitelisted bot IDs, plus honeypot "
    "channel and role IDs and pending suspect kick deadlines."
)
