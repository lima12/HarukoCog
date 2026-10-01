from .battlemetric import BattleMetric


async def setup(bot):
    """BattleMetric entry point."""
    await bot.add_cog(BattleMetric(bot))


__red_end_user_data_statement__ = (
    "This cog reads global BattleMetrics, HLL RCON, PostgreSQL, and tagweb IPC secrets "
    "from Red's shared API-token storage. It stores settings such as a default server ID, "
    "game filter, Server Info panel IDs, HLL RCON endpoint, kill-feed channel, "
    "seeding, HQ-protection, and team-kill watch settings, plus short-lived "
    "team-kill alert and player-watch records, dog-tag review settings, and authorized "
    "Discord user IDs. Territory protection temporarily tracks game player IDs, teams, "
    "positions, and death counters in memory. It stores verified "
    "Discord-to-EOS links and aggregated HLL statistics in PostgreSQL, plus staged "
    "or approved dog-tag overlays in the configured host directory. Moderation commands "
    "send game IDs, messages, reasons, durations, and acting Discord administrator "
    "identities to the configured external services."
)
