
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

import discord
from pssapi.entities import StarSystemDetail
from pssapi.utils.exceptions import PssApiError

#from database import models
from classes.views import EngagementParticipantsView
from data.constants.galaxy import STAR_SYSTEMS
from classes.databaseclasses import EngagementSystemData
from database import models
from database.models import EngagementDB, GalaxySystemDB
from handlers.prestigehandler import CrewMember, PrestigeRecipe
from handlers import databasehandlers as DBH
from handlers import fleetwarshandlers as FWH

if TYPE_CHECKING:
    from classes.bot import FleetToolsBot
    from pssapi.entities.raw import EngagementRaw

logger = logging.getLogger(__name__)

# Resolve <project_root>/data/ regardless of where this module is imported from
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

class FleetWarsManager:
    def __init__(self, bot: "FleetToolsBot"):
        self.bot = bot
        self.__active_engagements: Dict[int, EngagementSystemData] = {}
        self.__galaxy_systems: Dict[int, models.GalaxySystemDB] = {}
        self.__active_engagements_lock = asyncio.Lock()
        self.__galaxy_data_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Systems
    # ------------------------------------------------------------------

    async def load_galaxy_systems_from_db(self) -> None:
        systems = await self.bot.database_manager.get_all_galaxy_systems()

        async with self.__galaxy_data_lock:
            self.__galaxy_systems = systems
    # ------------------------------------------------------------------
    def get_system_id_by_name(self, system_name: str) -> Optional[int]:
        for system_id, name in STAR_SYSTEMS.items():
            if name.lower() == system_name.lower():
                return system_id
        return None

    async def get_system_data_by_system_id(self, system_id: int):
        async with self.__galaxy_data_lock:
            return self.__galaxy_systems.get(system_id)

    async def get_all_systems_data(self):
        async with self.__galaxy_data_lock:
            return dict(self.__galaxy_systems)

    async def get_system_ids_under_attack(self) -> Set[int]:
        systems = await self.get_all_systems_data()
        systems_under_attack = {id for id, system in systems.items() if system.under_attack}
        return systems_under_attack

    async def update_galaxy_system_cache(self, system_id: int, galaxy_system: models.GalaxySystemDB) -> None:
        """Write-through update for a single galaxy system in the in-memory cache."""
        async with self.__galaxy_data_lock:
            self.__galaxy_systems[system_id] = galaxy_system
    # ------------------------------------------------------------------
    # Engagements
    # ------------------------------------------------------------------

    async def load_active_engagements_from_db(self) -> None:
        db_active_engagements = await self.bot.database_manager.get_all_active_engagements()

        async with self.__active_engagements_lock:
            self.__active_engagements.clear()
            for engagement_id, db_engagement in db_active_engagements.items():
                try:
                    self.__active_engagements[engagement_id] = EngagementSystemData.from_db_model(db_engagement)
                except Exception as e:
                    self.bot.logger.error(f"Error loading engagement {engagement_id} from DB: {e}")
    # ------------------------------------------------------------------
    async def get_active_engagements(self) -> Dict[int, EngagementSystemData]:
        async with self.__active_engagements_lock:
            return dict(self.__active_engagements)

    async def replace_active_engagements(self, new_map: Dict[int, EngagementSystemData]) -> None:
        async with self.__active_engagements_lock:
            self.__active_engagements.clear()
            self.__active_engagements.update(new_map)

    async def update_active_engagement(self, engagement_id: int, engagement_data: EngagementSystemData) -> None:
        """Write-through update for a single active engagement."""
        async with self.__active_engagements_lock:
            self.__active_engagements[engagement_id] = engagement_data

    async def remove_engagement_from_cache(self, engagement_id: int) -> bool:
        async with self.__active_engagements_lock:
            if engagement_id in self.__active_engagements:
                del self.__active_engagements[engagement_id]
                return True
            return False

    # ------------------------------------------------------------------
    # Command Response
    # ------------------------------------------------------------------
    async def get_fleet_wars_status(self):
        # active_engagements:Dict[int, EngagementSystemData] = await self.get_active_engagements()
        current_systems_data: Dict[int, GalaxySystemDB] = await self.get_all_systems_data()

        formatted_systems_data = []

        for system_id, system_name in STAR_SYSTEMS.items():
            system_data = current_systems_data.get(system_id)
            if not system_data:
                raise Exception

            remaining_seconds = FWH.get_remaining_cooldown_seconds(system_data)
            cooldown_status = FWH.format_cooldown_status(system_data, remaining_seconds)

            formatted_systems_data.append({
                'name': system_name,
                'owner': system_data.owner_name,
                'cooldown': cooldown_status,
                'cooldown_seconds': remaining_seconds,
                'under_attack': system_data.under_attack
            })

        return formatted_systems_data

    async def prune_expired_engagements(self) -> int:
        expired_engagements: Dict[int, EngagementDB] = await self.bot.database_manager.get_expired_engagments()
        current_time = datetime.now(timezone.utc)
        pruned_count = 0

        for engagement_id, engagement_data in expired_engagements.items():
            # Ensure DB value is timezone-aware before comparing because i goofed earlier
            end_time = engagement_data.end_time #getattr(engagement_data, "end_time", None)
            end_time = DBH.ensure_aware(end_time)
            #if end_time is None:
            #    continue

            try:
                success = await self.bot.database_manager.mark_engagement_inactive(engagement_id)
            except Exception as e:
                self.bot.logger.critical("Failed marking engagement inactive")
                success = False

            if not success:
                return pruned_count

            self.bot.logger.info(f"Pruning expired engagement ID {engagement_id} (ended at {end_time})")

            # Use lock-safe removal
            await self.remove_engagement_from_cache(engagement_id)

            # Clear under_attack on the galaxy system so state is consistent
            # before the next galaxy_state_refresh cycle runs
            try:
                updated_system = await self.bot.database_manager.deactivate_under_attack_system(engagement_data.system_id)
                if not updated_system:
                    raise Exception(f"Updated_system was found to be null when trying to clear under_attack for system {engagement_data.system_id}")

                await self.update_galaxy_system_cache(
                    engagement_data.system_id, updated_system
                )

            except Exception as e:
                self.bot.logger.warning(
                    f"Could not clear under_attack for system {engagement_data.system_id}: {e}"
                )
            pruned_count += 1

        try:
            count = await self.bot.database_manager.count_active_engagements()
            if pruned_count > 0:
                remaining_active = count - pruned_count
                self.bot.logger.info(f"Pruned {pruned_count} expired engagement(s). {remaining_active} active engagement(s) remaining.")

        except Exception as e:
            self.bot.logger.critical(f"❌ Error during engagement pruning: {e}")

        return pruned_count

    async def create_engagement_embed_option(self, engagements: Dict[int, EngagementSystemData] | List[EngagementSystemData]) -> discord.Embed:
        return await FWH.generate_engagements_status_embed(engagements)

    async def create_engagement_detail_embed(self,
                                             engagement_id: int) -> tuple[discord.Embed, Optional[discord.ui.View]]:
        # Get max engagement ID from DB
        max_engagement_id = await self.bot.database_manager.get_highest_engagement_id()

        # ID too high → error
        if engagement_id > max_engagement_id:
            return discord.Embed(
                title="❌ Invalid Engagement ID",
                description=(
                    f"Engagement ID **{engagement_id}** does not exist.\n"
                    f"Highest known engagement ID is **{max_engagement_id}**."
                ),
                color=0xFF0000
            ), None
        # Fetch fresh engagement data from API
        try:
            #await self.bot.api_manager.ensure_valid_token_age() # is this needed?
            engagement_raw: EngagementRaw = await self.bot.api_manager.get_engagement(engagement_id)
        except Exception as e:
            self.bot.logger.error(e)
            return discord.Embed(
                title="❌ Error Fetching Engagement",
                description=f"Failed to retrieve engagement **{engagement_id}**.",
                color=0xFF0000
            ), None

        return await FWH.create_engagement_detail_embed(engagement_id, engagement_raw)

    async def refresh_galaxy_state(self, force_refresh_all: bool = False) -> int:
        now = datetime.now(timezone.utc)
        refreshed_count = 0

        self.bot.logger.info(f"Starting galaxy state refresh (force_all={force_refresh_all})...")

        engagement_snapshot = await self.get_active_engagements()
        active_engagement_system_ids: Set[int] = {eng.system_id for eng in engagement_snapshot.values()}
        active_engagement_by_system: Dict[int, int] = {eng.system_id: eng.engagement_id for eng in engagement_snapshot.values()}

        # Validate token before starting batch operations to prevent cascade
        try:
            self.bot.logger.info("Galaxy State Refresh: Validating token before batch operations...")
            await self.bot.api_manager.ensure_valid_token_age()
        except Exception as e:
            self.bot.logger.error(f"Galaxy State Refresh: Token validation failed before refresh: {e}", exc_info=e)
            return 0

        existing_systems: Dict[int, GalaxySystemDB] = await self.bot.database_manager.get_all_galaxy_systems()

        systems_to_refresh = FWH.get_systems_to_refresh(existing_systems, active_engagement_system_ids, now, force_refresh_all)

        self.bot.logger.info(f"Refreshing {len(systems_to_refresh)} systems...")

        # Refresh systems concurrently in smaller batches with delays
        batch_size = 10
        for i in range(0, len(systems_to_refresh), batch_size):
            batch = systems_to_refresh[i:i + batch_size]
            tasks = [self.bot.api_manager.get_galaxy_system_data(system_id) for system_id in batch]
            results: List[StarSystemDetail | BaseException] = await asyncio.gather(*tasks, return_exceptions=True)

            for system_id, galaxy_data in zip(batch, results):
                if isinstance(galaxy_data, BaseException):
                    self.bot.logger.error(f"Error fetching system {system_id}: {galaxy_data}")
                    continue

                if galaxy_data is None:
                    continue

                try:
                    # Extract data
                    owner_name = galaxy_data.owner_name if galaxy_data.owner_name else "Unclaimed"
                    cooldown_value = galaxy_data.engagement_cooldown_end_date

                    # Parse cooldown
                    cooldown_end = None
                    if cooldown_value is not None:
                        if isinstance(cooldown_value, datetime):
                            sentinel_date = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
                            if cooldown_value > sentinel_date:
                                cooldown_end = cooldown_value.replace(tzinfo=timezone.utc) if cooldown_value.tzinfo is None else cooldown_value
                        elif isinstance(cooldown_value, str) and cooldown_value != "2000-01-01T00:00:00":
                            try:
                                cooldown_end = datetime.fromisoformat(cooldown_value).replace(tzinfo=timezone.utc)
                            except (ValueError, TypeError):
                                pass

                    is_under_attack = system_id in active_engagement_system_ids
                    engagement_id_for_system = active_engagement_by_system.get(system_id)

                    # Check if system exists in DB
                    existing_system = existing_systems.get(system_id)

                    if existing_system:
                        # Update existing system
                        existing_system.owner_name = owner_name
                        existing_system.cooldown_end = cooldown_end
                        existing_system.under_attack = is_under_attack
                        existing_system.active_engagement_id = engagement_id_for_system
                        existing_system.last_updated = now
                    else:
                        # Create new system
                        system_name = STAR_SYSTEMS.get(system_id, f"System {system_id}")
                        new_system = models.GalaxySystemDB(
                            system_id=system_id,
                            system_name=system_name,
                            owner_name=owner_name,
                            cooldown_end=cooldown_end,
                            under_attack=is_under_attack,
                            active_engagement_id=engagement_id_for_system,
                            last_updated=now,
                            is_targeted=False
                        )
                        await self.bot.database_manager.upsert_galaxy_system(new_system)
                        existing_systems[system_id] = new_system

                    refreshed_count += 1

                except Exception as e:
                    self.bot.logger.error(f"Error processing system {system_id}: {e}")
                    continue

                # Add delay between batches to avoid overwhelming the API (Option D)
                if i + batch_size < len(systems_to_refresh):
                    await asyncio.sleep(0.5)

            # Commit all changes

        # Update cache
        await self.load_galaxy_systems_from_db()

        self.bot.logger.info(f"Galaxy state refresh completed. Updated {refreshed_count} systems.")
        return refreshed_count
