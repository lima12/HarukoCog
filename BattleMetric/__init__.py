from .battlemetric import BattleMetric


async def setup(bot):
    """BattleMetric entry point."""
    await bot.add_cog(BattleMetric(bot))


__red_end_user_data_statement__ = (
    "This cog stores a global BattleMetrics API token and per-guild BattleMetrics "
    "settings such as a default server ID and game filter. It does not store "
    "Discord user data."
)
