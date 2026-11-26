"""
TEMPORARY one-time backfill of hourly tournament snapshots for /graph.

To remove: delete this file and the two lines marked "TEMP backfill" in classes/bot.py.
"""

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from pss_fleet_data import ParameterInterval
from sqlmodel import select

from classes.bot import DEV_GUILD_ID
from database.models import TournamentSnapshot
from handlers import graphhandler
from handlers.databasehandler import get_session

if TYPE_CHECKING:
    from classes.bot import FleetToolsBot


async def backfill_hourly(bot: "FleetToolsBot") -> tuple[int, int]:
    """Returns (hours_added, hours_skipped) for the current/most recent tournament."""
    start, end, _ = graphhandler.get_tournament_window(datetime.now(timezone.utc))
    client = bot.api_manager.fleet_data_client
    added = skipped = 0
    skip = 0

    while True:
        page = await client.get_collections(from_date=start, to_date=end, interval=ParameterInterval.HOURLY,
                                            desc=False, skip=skip, take=100)
        if not page:
            break
        skip += len(page)

        for meta in page:
            moment = meta.timestamp
            moment = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment
            hour_start = graphhandler._naive(moment).replace(minute=0, second=0, microsecond=0)

            async with get_session() as session:
                existing = (await session.exec(
                    select(TournamentSnapshot.id)
                    .where(TournamentSnapshot.timestamp >= hour_start,
                           TournamentSnapshot.timestamp < hour_start + timedelta(hours=1))
                    .limit(1)
                )).first()
            if existing is not None:
                skipped += 1
                continue

            _, alliances = await client.get_alliances_from_collection(meta.collection_id)
            alliances = sorted(alliances, key=lambda a: -(a.score or 0))[:graphhandler.RANKING_TAKE]
            sample_time = graphhandler._naive(moment)
            async with get_session() as session:
                session.add_all([
                    TournamentSnapshot(
                        timestamp=sample_time,
                        alliance_id=a.alliance_id,
                        alliance_name=a.alliance_name or str(a.alliance_id),
                        division_design_id=a.division_design_id or 0,
                        stars=a.score or 0,
                    )
                    for a in alliances
                ])
            added += 1
    return added, skipped


class TempBackfill(commands.Cog):
    def __init__(self, bot: "FleetToolsBot"):
        self.bot = bot

    @app_commands.guilds(discord.Object(id=DEV_GUILD_ID))
    @app_commands.command(name="backfill_graph", description="TEMP: backfill hourly tournament data for /graph")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def backfill_graph(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            added, skipped = await backfill_hourly(self.bot)
            graphhandler._graph_cache.clear()
            await interaction.followup.send(f"✅ Backfilled {added} hourly samples ({skipped} hours already had data).")
        except Exception as e:
            self.bot.logger.error(f"Error in /backfill_graph: {e}", exc_info=e)
            await interaction.followup.send("❌ Backfill failed; see logs.")
