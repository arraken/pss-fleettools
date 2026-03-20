import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from data import database_models as models
from data.constants.galaxy import STAR_SYSTEMS
from data.databaseclasses import EngagementSystemData
from handlers import databasehandler as crud
from handlers.databasehandler import get_session

if TYPE_CHECKING:
    from classes.bot import FleetWarsBot


class CacheManager:
    def __init__(self, bot: "FleetWarsBot"):
        self.bot = bot
        self.__active_engagements: Dict[int, EngagementSystemData] = {}
        self.__galaxy_systems: Dict[int, models.GalaxySystem] = {}
        self._active_engagements_lock = asyncio.Lock()
        self._galaxy_systems_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Engagement cache
    # ------------------------------------------------------------------

    async def load_active_engagements_from_db(self) -> None:
        async with get_session() as session:
            db_active = await crud.get_all_active_engagements(session)

        async with self._active_engagements_lock:
            self.__active_engagements.clear()
            for eid, db_eng in db_active.items():
                try:
                    self.__active_engagements[eid] = EngagementSystemData.from_db_model(db_eng)
                except Exception as e:
                    self.bot.logger.error(f"Error loading engagement {eid} from DB: {e}")

    async def replace_active_engagements(self, new_map: Dict[int, EngagementSystemData]) -> None:
        async with self._active_engagements_lock:
            self.__active_engagements.clear()
            self.__active_engagements.update(new_map)

    # ------------------------------------------------------------------
    # Galaxy system cache
    # ------------------------------------------------------------------

    async def load_galaxy_systems_from_db(self) -> None:
        async with get_session() as session:
            systems = await crud.get_all_galaxy_systems(session)

        async with self._galaxy_systems_lock:
            self.__galaxy_systems = systems

    async def save_fleet_wars_systems(self) -> None:
        """Hook for any additional cache-level save logic.
        Persistence is handled via DB sessions inside the handlers."""
        pass

    async def get_galaxy_data_cached(
        self,
        system_id: int,
        max_age_minutes: int = 90,
    ) -> Optional[Tuple[str, Optional[datetime]]]:
        """Return (owner_name, cooldown_end) from in-memory cache if fresh,
        otherwise fetch from the API, persist to DB, and update the cache."""
        async with self._galaxy_systems_lock:
            system = self.__galaxy_systems.get(system_id)

        if system is not None:
            last_updated = system.last_updated
            if last_updated.tzinfo is None:
                last_updated = last_updated.replace(tzinfo=timezone.utc)
            age_minutes = (datetime.now(timezone.utc) - last_updated).total_seconds() / 60
            if age_minutes < max_age_minutes:
                return system.owner_name or "Unclaimed", system.cooldown_end

        # Cache miss or stale — fetch from API
        try:
            galaxy_data = await self.bot.api_manager.get_galaxy_data(system_id)
            if galaxy_data is None:
                return None

            owner_name = galaxy_data.owner_name if galaxy_data.owner_name else "Unclaimed"
            cooldown_value = galaxy_data.engagement_cooldown_end_date
            cooldown_end = None

            if cooldown_value is not None:
                if isinstance(cooldown_value, datetime):
                    sentinel = datetime(2000, 1, 1, tzinfo=timezone.utc)
                    if cooldown_value > sentinel:
                        cooldown_end = (
                            cooldown_value.replace(tzinfo=timezone.utc)
                            if cooldown_value.tzinfo is None
                            else cooldown_value
                        )

            now = datetime.now(timezone.utc)
            async with self._galaxy_systems_lock:
                existing = self.__galaxy_systems.get(system_id)
                if existing:
                    existing.owner_name = owner_name
                    existing.cooldown_end = cooldown_end
                    existing.last_updated = now
                else:
                    system_name = STAR_SYSTEMS.get(system_id, f"System {system_id}")
                    existing = models.GalaxySystem(
                        system_id=system_id,
                        system_name=system_name,
                        owner_name=owner_name,
                        cooldown_end=cooldown_end,
                        last_updated=now,
                    )
                    self.__galaxy_systems[system_id] = existing

            async with get_session() as session:
                await crud.upsert_galaxy_system(session, existing)

            return owner_name, cooldown_end

        except Exception as e:
            self.bot.logger.error(f"Error fetching galaxy data for system {system_id}: {e}")
            return None

