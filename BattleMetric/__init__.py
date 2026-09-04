from .battlemetric import BattleMetric


async def setup(bot):
    """BattleMetric entry point."""
    await bot.add_cog(BattleMetric(bot))


__red_end_user_data_statement__ = (
    "This cog reads a global BattleMetrics API token from Red's shared API-token "
    "storage. It stores per-guild settings such as a default server ID, game "
    "filter, Server Info panel channel/message IDs, and authorized Discord user IDs."
)
