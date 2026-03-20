import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

import discord
from discord.ext import commands, tasks

from data.constants.galaxy import STAR_SYSTEMS
from handlers import fleetwarshandler, databasehandler
from data.databaseclasses import EngagementSystemData
from handlers.databasehandler import get_session

if TYPE_CHECKING:
    from classes.bot import FleetWarsBot


class TimerMonitor(commands.Cog):
    def __init__(self, bot: "FleetWarsBot"):
        self.bot = bot

        # TODO: load this dynamically from the fleet_role_mappings DB table.
        # Structure: { "FleetName": {"admin_id": <discord_role_id>} }
        # Use databasehandler.get_all_fleet_role_mappings() when ready.
        self.admin_role_mapping: Dict[str, Dict[str, int]] = {}

    async def cog_load(self) -> None:
        self.engagements_pulse.start()
        self.galaxy_state_refresh.start()

    async def cog_unload(self) -> None:
        self.engagements_pulse.cancel()
        self.galaxy_state_refresh.cancel()

    @tasks.loop(minutes=5)
    async def engagements_pulse(self):
        try:
            await asyncio.wait_for(self._engagements_pulse_inner(), timeout=240.0)
        except asyncio.TimeoutError:
            print("Engagements pulse timed out after 4 minutes")

    @tasks.loop(minutes=10)
    async def galaxy_state_refresh(self):
        try:
            await asyncio.wait_for(self._galaxy_state_refresh_inner(), timeout=540.0)
        except asyncio.TimeoutError:
            self.bot.logger.error("galaxy_state_refresh timed out after 9 minutes — skipping this cycle.")

    @engagements_pulse.before_loop
    async def before_engagements_pulse(self):
        await self.bot.wait_until_ready()

    @galaxy_state_refresh.before_loop
    async def before_galaxy_state_refresh(self):
        await self.bot.wait_until_ready()

    async def _engagements_pulse_inner(self):
        print(f"Starting engagements pulse at {datetime.now(timezone.utc).isoformat()}")
        await fleetwarshandler.prune_expired_engagements(self.bot)
        new_engagements = await fleetwarshandler.get_active_engagements(self.bot)
        await self.check_and_alert_new_engagements(new_engagements)
        await self.sync_active_engagements_from_db()

    async def _galaxy_state_refresh_inner(self):
        print("Galaxy State Refresh: Updating system ownership and cooldowns...")
        refreshed = 0
        try:
            refreshed = await fleetwarshandler.refresh_galaxy_state(self.bot, force_refresh_all=False)
            print(f"Galaxy state refresh completed. Updated {refreshed} systems.")
        except Exception as e:
            print(f"Error in galaxy state refresh: {e}")

    async def sync_active_engagements_from_db(self) -> int:
        try:
            async with get_session() as session:
                db_active = await databasehandler.get_all_active_engagements(session)  # Dict[int, Engagement]

            new_map: dict[int, EngagementSystemData] = {}
            for eid, db_eng in db_active.items():
                try:
                    eng_data = EngagementSystemData.from_db_model(db_eng)
                    new_map[eid] = eng_data
                except Exception as e:
                    print(f"Skipping engagement {eid} during sync: {e}")

            # Preferred API on CacheManager
            if hasattr(self.bot.cache_manager, "replace_active_engagements"):
                await self.bot.cache_manager.replace_active_engagements(new_map)
            else:
                # Fallback: ensure an asyncio.Lock is present
                lock = getattr(self.bot.cache_manager, "_active_engagements_lock", None)
                if lock is None:
                    self.bot.cache_manager._active_engagements_lock = asyncio.Lock()
                    lock = self.bot.cache_manager._active_engagements_lock

                async with lock:
                    priv = getattr(self.bot.cache_manager, "_CacheManager__active_engagements", None)
                    if priv is None:
                        self.bot.cache_manager._CacheManager__active_engagements = {}
                        priv = self.bot.cache_manager._CacheManager__active_engagements
                    priv.clear()
                    priv.update(new_map)
                    self.bot.cache_manager._active_engagements = priv

            return len(new_map)

        except Exception as e:
            print(f"Error during engagement cache sync {e}")
            return 0

    async def check_and_alert_new_engagements(self, new_engagements: List[EngagementSystemData]) -> None:
        if not new_engagements:
            return

        channel_id = 0 # Need to make this dynamic

        channel = await self.bot.retrieve_channel(channel_id)
        if not channel:
            self.bot.logger.error("Engagement alert channel not found!")
            return

        for engagement in new_engagements:
            # Get system name
            system_name = STAR_SYSTEMS.get(engagement.system_id, f"System #{engagement.system_id}")

            # Create embed

            if "raid" in engagement.engagement_type.lower():
                embed = discord.Embed(
                    title="⚔️ New Raid Engagement Detected!",
                    color=discord.Color.red()
                )
            elif "invasion" in engagement.engagement_type.lower():
                embed = discord.Embed(
                    title="⚔️ New Invasion Engagement Detected!",
                    color=discord.Color.red()
                )
            else:
                embed = discord.Embed(
                    title="⚔️ New Engagement Detected!",
                    color=discord.Color.red()
                )

            embed.add_field(
                name="System",
                value=f"**{system_name}**",
                inline=True
            )
            embed.add_field(
                name="Engagement ID",
                value=f"#{engagement.engagement_id}",
                inline=True
            )
            if "raid" in engagement.engagement_type:
                embed.add_field(
                    name="Raider",
                    value=f"**{engagement.attacker}**",
                    inline=False
                )
            elif "invasion" in engagement.engagement_type:
                embed.add_field(
                    name="Invader",
                    value=f"**{engagement.attacker}**",
                    inline=False
                )
            else:
                embed.add_field(
                    name="Attacker",
                    value=f"**{engagement.attacker}**",
                    inline=False
                )
            embed.add_field(
                name="Defender",
                value=f"**{engagement.defender}**",
                inline=False
            )

            # Add timestamps
            start_timestamp = int(engagement.start_time.timestamp())
            end_timestamp = int(engagement.end_time.timestamp())

            embed.add_field(
                name="Started",
                value=f"<t:{start_timestamp}:R>",
                inline=True
            )
            embed.add_field(
                name="Ends",
                value=f"<t:{end_timestamp}:R>",
                inline=True
            )

            embed.timestamp = datetime.now(timezone.utc)

            # Check if defender matches a fleet in the map
            admin_role_id = self.admin_role_mapping[engagement.defender]["admin_id"] if engagement.defender in self.admin_role_mapping else None

            # Only send role_mention if the engagement is < 10 minutes from starting because database stuff is being weird and sometimes engagements are detected late and we don't want to ping for old engagements
            engagement_age = datetime.now(timezone.utc) - engagement.start_time
            if admin_role_id and engagement_age < timedelta(minutes=10) and engagement.engagement_type != "raiding":
                role_mention = f"<@&{admin_role_id}>, RED ALERT - SHIELDS TO FULL"
                await channel.send(role_mention, embed=embed)
                if admin_role_id == 1478848490237989057: # If it's Dynasty ping Spoeb in dev server
                    dev_channel = await self.bot.retrieve_channel(1389338216099745862)
                    await dev_channel.send(role_mention, embed=embed)
            else:
                # Send without role mention
                await channel.send(embed=embed)