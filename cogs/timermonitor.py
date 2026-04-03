import asyncio
import logging
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

import discord
from discord.ext import commands, tasks

from data.constants.galaxy import STAR_SYSTEMS
#from handlers import fleetwarshandlers
from classes.databaseclasses import EngagementSystemData

if TYPE_CHECKING:
    from classes.bot import FleetToolsBot


class TimerMonitor(commands.Cog):
    def __init__(self, bot: "FleetToolsBot"):
        self.bot = bot

        # TODO: load this dynamically from the fleet_role_mappings DB table.
        # Structure: { "FleetName": {"admin_id": <discord_role_id>} }
        # Use databasehandler.get_all_fleet_role_mappings() when ready.
        self.admin_role_mapping: Dict[str, Dict[str, int]] = {}

    async def cog_load(self) -> None:
        await self._load_admin_role_mapping()
        self.engagements_pulse.start()
        self.galaxy_state_refresh.start()
        self.monthly_prestige_rebuild.start()

    async def cog_unload(self) -> None:
        self.engagements_pulse.cancel()
        self.galaxy_state_refresh.cancel()
        self.monthly_prestige_rebuild.cancel()

    @tasks.loop(minutes=5)
    async def engagements_pulse(self):
        try:
            await asyncio.wait_for(self._engagements_pulse_inner(), timeout=240.0)
        except asyncio.TimeoutError:
            self.bot.logger.error("Engagements pulse timed out after 4 minutes")
        except Exception as e:
            self.bot.logger.error(f"Engagements pulse error: {e}", exc_info=True)

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

    # Fires every day at UTC 00:00:00; only acts on the 1st of the month.
    @tasks.loop(time=dt_time(0, 0, 0, tzinfo=timezone.utc))
    async def monthly_prestige_rebuild(self):
        if datetime.now(timezone.utc).day != 1:
            return
        self.bot.logger.info("[Monthly] First of month — scheduling prestige recipe rebuild.")
        try:
            await asyncio.wait_for(self._monthly_prestige_rebuild_inner(), timeout=600.0)
        except asyncio.TimeoutError:
            self.bot.logger.error("[Monthly] Prestige rebuild timed out after 10 minutes.")

    @monthly_prestige_rebuild.before_loop
    async def before_monthly_prestige_rebuild(self):
        await self.bot.wait_until_ready()

    async def _monthly_prestige_rebuild_inner(self):
        from handlers import prestigehandler

        self.bot.logger.info("[Monthly] Clearing prestige recipe cache...")
        self.bot.cache_manager.clear_prestige_recipes()
        self.bot.cache_manager.api_prestige_recipes.clear()
        self.bot.cache_manager.prestige_build_status = "building_from_api"

        self.bot.logger.info("[Monthly] Rebuilding prestige recipes from API...")
        try:
            new_recipes = await prestigehandler.build_prestige_recipes(self.bot)
            self.bot.cache_manager.api_prestige_recipes = new_recipes
            self.bot.cache_manager.save_prestige_recipes_data()
            self.bot.cache_manager.prestige_build_status = "complete"
            total = sum(len(v) for v in new_recipes.values())
            self.bot.logger.info(f"[Monthly] ✅ Prestige recipes rebuilt: {total} recipes.")
        except Exception as e:
            self.bot.cache_manager.prestige_build_status = "failed"
            self.bot.logger.error(f"[Monthly] ❌ Failed to rebuild prestige recipes: {e}")

    async def _engagements_pulse_inner(self):
        self.bot.logger.info(f"Starting engagements pulse at {datetime.now(timezone.utc).isoformat()}")
        await self.bot.fleetwars_manager.prune_expired_engagements()
        active_engagements = await self.bot.fleetwars_manager.get_active_engagements_pulse()
        #active_engagements = [engagement for _, engagement in active_engagements.items()]
        await self.check_and_alert_new_engagements(active_engagements)
        await self.sync_active_engagements_from_db()

    async def _galaxy_state_refresh_inner(self):
        self.bot.logger.info("Galaxy State Refresh: Updating system ownership and cooldowns...")
        refreshed = 0
        try:
            refreshed = await self.bot.fleetwars_manager.refresh_galaxy_state()
        except Exception as e:
            print(f"Error in galaxy state refresh: {e}")

    async def sync_active_engagements_from_db(self) -> int:
        try:
            db_active_engagements = await self.bot.database_manager.get_all_active_engagements()
            new_map: dict[int, EngagementSystemData] = {}
            for engagement_id, db_engagement in db_active_engagements.items():
                try:
                    eng_data = EngagementSystemData.from_db_model(db_engagement)
                    new_map[engagement_id] = eng_data
                except Exception as e:
                    self.bot.logger.info(f"Skipping engagement {engagement_id} during sync: {e}")

            # Preferred API on CacheManager
            # if hasattr(self.bot.cache_manager, "replace_active_engagements"):
            await self.bot.fleetwars_manager.replace_active_engagements(new_map)
            # else:
                # # Fallback: ensure an asyncio.Lock is present
                # lock = getattr(self.bot.cache_manager, "_active_engagements_lock", None)
                # if lock is None:
                #     self.bot.cache_manager._active_engagements_lock = asyncio.Lock()
                #     lock = self.bot.cache_manager._active_engagements_lock

                # async with lock:
                #     priv = getattr(self.bot.cache_manager, "_CacheManager__active_engagements", None)
                #     if priv is None:
                #         self.bot.cache_manager._CacheManager__active_engagements = {}
                #         priv = self.bot.cache_manager._CacheManager__active_engagements
                #     priv.clear()
                #     priv.update(new_map)
                #     self.bot.cache_manager._CacheManager__active_engagements = priv

            return len(new_map)

        except Exception as e:
            self.bot.logger.info(f"Error during engagement cache sync {e}")
            return 0

    async def _load_admin_role_mapping(self) -> None:
        """Populate admin_role_mapping from the fleet_role_mappings DB table."""
        try:
            rows = await self.bot.database_manager.get_all_fleet_role_mappings()
            self.admin_role_mapping = {
                name: {"admin_id": row.admin_role_id}
                for name, row in rows.items()
            }
            self.bot.logger.info(f"Loaded {len(self.admin_role_mapping)} fleet role mappings.")
        except Exception as e:
            self.bot.logger.error(f"Failed to load fleet role mappings: {e}", exc_info=True)

    async def _get_engagement_alert_channel(self):
        """Resolve the engagement alert channel from the DB."""
        try:
            rows = await self.bot.database_manager.get_all_alert_channels(channel_type="engagements")
            for row in rows:
                channel = await self.bot.retrieve_channel(row.channel_id)
                if channel:
                    return channel
        except Exception as e:
            self.bot.logger.error(f"Error resolving engagement alert channel: {e}", exc_info=True)
        return None

    async def check_and_alert_new_engagements(self, new_engagements: List[EngagementSystemData]) -> None:
        if not new_engagements:
            return

        channel = await self._get_engagement_alert_channel()
        if not channel:
            self.bot.logger.error("Engagement alert channel not found — skipping alerts.")
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
            if not isinstance(channel, discord.abc.Messageable):
                return

            if admin_role_id and engagement_age < timedelta(minutes=10) and engagement.engagement_type != "raiding":
                role_mention = f"<@&{admin_role_id}>, RED ALERT - SHIELDS TO FULL"
                await channel.send(role_mention, embed=embed)
                if admin_role_id == 1478848490237989057: # If it's Dynasty ping Spoeb in dev server
                    dev_channel = await self.bot.retrieve_channel(1389338216099745862)
                    if not isinstance(dev_channel, discord.abc.Messageable):
                        return
                    await dev_channel.send(role_mention, embed=embed)
            else:
                # Send without role mention
                await channel.send(embed=embed)
