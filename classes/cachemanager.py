import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from database import models
from data.constants.galaxy import STAR_SYSTEMS
from data.databaseclasses import EngagementSystemData
from handlers.prestigehandler import CrewMember, PrestigeRecipe
from handlers import databasehandler as crud
from handlers.databasehandler import get_session

if TYPE_CHECKING:
    from classes.bot import FleetToolsBot

logger = logging.getLogger(__name__)

# Resolve <project_root>/data/ regardless of where this module is imported from
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

_DEFAULTS: Dict[str, Any] = {
    "prestige_recipes": {},
    "fleet_wars_systems": {},
}


class CacheManager:
    def __init__(self, bot: "FleetToolsBot"):
        self.bot = bot
        self.__active_engagements: Dict[int, EngagementSystemData] = {}
        self.__galaxy_systems: Dict[int, models.GalaxySystem] = {}
        self._active_engagements_lock = asyncio.Lock()
        self._CacheManager__active_engagements = self.__active_engagements
        self._galaxy_systems_lock = asyncio.Lock()
        self.api_prestige_recipes: Dict[int, List[PrestigeRecipe]] = {}
        self.api_crew_list: List[CrewMember] = []

        # File paths for JSON persistence
        os.makedirs(DATA_DIR, exist_ok=True)
        self.files: Dict[str, str] = {
            "prestige_recipes": os.path.join(DATA_DIR, "prestige_recipes.json"),
            "fleet_wars_systems": os.path.join(DATA_DIR, "fleet_wars_systems.json"),
        }

        # Prestige recipe building status tracking
        self.prestige_build_status = "pending"  # pending, loading_storage, building_from_api, complete, failed
        self.prestige_build_progress = {
            "current_crew_index": 0,
            "total_crew": 0,
            "recipes_found": 0,
            "last_updated": None,
            "error_message": None
        }

        api_task = self.bot.loop.create_task(self.load_api_crew_list())
        prestige_task = self.bot.loop.create_task(self.load_api_prestige_recipes())

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

    async def load_fleet_wars_systems_from_json(self) -> None:
        """Populate __galaxy_systems from fleet_wars_systems.json."""
        data = self.load_json("fleet_wars_systems")
        async with self._galaxy_systems_lock:
            self.__galaxy_systems.clear()
            for key, value in data.items():
                try:
                    system_id = int(key)
                    cooldown_end: Optional[datetime] = None
                    if value.get("cooldown_end"):
                        cooldown_end = datetime.fromisoformat(value["cooldown_end"])
                        if cooldown_end.tzinfo is None:
                            cooldown_end = cooldown_end.replace(tzinfo=timezone.utc)

                    last_updated = datetime.now(timezone.utc)
                    if value.get("last_updated"):
                        last_updated = datetime.fromisoformat(value["last_updated"])
                        if last_updated.tzinfo is None:
                            last_updated = last_updated.replace(tzinfo=timezone.utc)

                    system = models.GalaxySystem(
                        system_id=system_id,
                        system_name=value.get(
                            "system_name",
                            STAR_SYSTEMS.get(system_id, f"System {system_id}"),
                        ),
                        owner_name=value.get("owner_name"),
                        cooldown_end=cooldown_end,
                        last_updated=last_updated,
                        is_targeted=value.get("is_targeted", False),
                    )
                    self.__galaxy_systems[system_id] = system
                except Exception as e:
                    self.bot.logger.error(f"Error loading galaxy system {key} from JSON: {e}")

    def save_fleet_wars_systems_to_json(self) -> bool:
        """Persist __galaxy_systems to fleet_wars_systems.json."""
        data: Dict[str, Any] = {}
        for system_id, system in self.__galaxy_systems.items():
            data[str(system_id)] = {
                "system_id": system.system_id,
                "system_name": system.system_name,
                "owner_name": system.owner_name,
                "cooldown_end": system.cooldown_end.isoformat() if system.cooldown_end else None,
                "last_updated": system.last_updated.isoformat() if system.last_updated else None,
                "is_targeted": system.is_targeted,
            }
        return self.save_json("fleet_wars_systems", data)

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
            cooldown_end: Optional[datetime] = None

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

    # ------------------------------------------------------------------
    # Prestige recipes — JSON persistence
    # ------------------------------------------------------------------

    def save_prestige_recipes_data(self) -> None:
        """Serialize self.api_prestige_recipes (PrestigeRecipe objects) to JSON."""
        serializable: Dict[str, Any] = {
            str(result_id): [r.to_dict() for r in recipes]
            for result_id, recipes in self.api_prestige_recipes.items()
        }
        ok = self.save_json("prestige_recipes", serializable)
        if ok:
            self.bot.logger.info("Prestige recipes saved to prestige_recipes.json")
        else:
            self.bot.logger.error("Failed to save prestige recipes to JSON")

    def load_prestige_recipes(self) -> Dict[int, Any]:
        """Load raw prestige recipe dicts from JSON (keys coerced to int)."""
        data = self.load_json("prestige_recipes")
        if not data:
            return {}
        try:
            return {int(k): v for k, v in data.items()}
        except (ValueError, TypeError) as e:
            logger.error(f"Error parsing prestige recipes: {e}")
            return {}

    def save_prestige_recipes(self, prestige_recipes: Dict[int, Any]) -> bool:
        """Persist a pre-serialized prestige recipe dict (values already dicts)."""
        try:
            return self.save_json(
                "prestige_recipes",
                {str(k): v for k, v in prestige_recipes.items()},
            )
        except Exception as e:
            logger.error(f"Error serializing prestige recipes: {e}")
            return False

    def clear_prestige_recipes(self) -> bool:
        return self.save_json("prestige_recipes", {})

    async def load_api_prestige_recipes(self) -> None:
        # Wait for crew list to load first
        max_wait = 30
        for _ in range(max_wait * 10):  # Check every 100ms for 30 seconds
            if self.api_crew_list:
                break
            await asyncio.sleep(0.1)

        if not self.api_crew_list:
            self.prestige_build_status = "failed"
            self.prestige_build_progress["error_message"] = "API crew list failed to load"
            self.bot.logger.error("API crew list failed to load - skipping prestige recipes")
            return

        from handlers import prestigehandler

        # Try to load from storage first
        self.prestige_build_status = "loading_storage"
        self.prestige_build_progress["last_updated"] = datetime.now(tz=timezone.utc)

        stored_recipes = await prestigehandler.load_prestige_recipes_from_storage(self.bot)

        if stored_recipes:
            self.api_prestige_recipes = stored_recipes
            self.prestige_build_status = "complete"
            self.prestige_build_progress["recipes_found"] = sum(
                len(recipes) for recipes in stored_recipes.values()
            )
            self.prestige_build_progress["last_updated"] = datetime.now(tz=timezone.utc)
            return

        # If not in storage, build from API
        self.prestige_build_status = "building_from_api"
        self.prestige_build_progress["total_crew"] = len(self.api_crew_list)
        self.prestige_build_progress["last_updated"] = datetime.now(tz=timezone.utc)
        self.bot.logger.info("Building prestige recipes from API data...")

        try:
            self.api_prestige_recipes = await prestigehandler.build_prestige_recipes(
                self.bot,
                self._update_prestige_build_progress,
            )
            self.prestige_build_status = "complete"
            self.prestige_build_progress["recipes_found"] = sum(
                len(recipes) for recipes in self.api_prestige_recipes.values()
            )
            self.prestige_build_progress["last_updated"] = datetime.now(tz=timezone.utc)
            self.bot.logger.info(
                f"✅ Prestige recipes built from API: "
                f"{self.prestige_build_progress['recipes_found']} recipes"
            )
            self.save_prestige_recipes_data()
        except Exception as e:
            self.prestige_build_status = "failed"
            self.prestige_build_progress["error_message"] = str(e)
            self.prestige_build_progress["last_updated"] = datetime.now(tz=timezone.utc)
            self.bot.logger.error(f"❌ Error building prestige recipes: {e}")

    def _update_prestige_build_progress(self, crew_index: int, recipes_count: int) -> None:
        self.prestige_build_progress["current_crew_index"] = crew_index
        self.prestige_build_progress["recipes_found"] = recipes_count
        self.prestige_build_progress["last_updated"] = datetime.now(tz=timezone.utc)

    def get_prestige_build_status(self) -> Dict[str, Any]:
        return {
            "status": self.prestige_build_status,
            "progress": self.prestige_build_progress.copy(),
        }

    def get_api_crew_list(self) -> List[CrewMember]:
        return self.api_crew_list

    async def load_api_crew_list(self) -> None:
        crew_list = await self.bot.api_manager.get_all_crew()

        for crew in crew_list:
            crew_member = CrewMember(
                name=crew.character_design_name,
                crew_id=0,
                design_id=crew.character_design_id,
                rarity=crew.rarity,
                special=crew.special_ability_final_argument,
                collection=crew.collection_design_id,
                hp=crew.final_hp,
                atk=crew.final_attack,
                rpr=crew.final_repair,
                abl=crew.special_ability_final_argument,
                sta=0,
                plt=crew.final_pilot,
                sci=crew.final_science,
                eng=crew.final_engine,
                wpn=crew.final_weapon,
                rst=crew.fire_resistance,
                walk=crew.walking_speed,
                run=crew.run_speed,
                tp=crew.training_capacity,
            )
            self.api_crew_list.append(crew_member)

    # ------------------------------------------------------------------
    # Core JSON load / save
    # ------------------------------------------------------------------

    def _json_default(self, obj: Any) -> Any:
        """JSON serializer for types not handled by default (e.g. datetime)."""
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    def _atomic_json_write(self, file_path: str, data: Any) -> None:
        """Write *data* to *file_path* atomically via a temp file + replace."""
        dir_name = os.path.dirname(file_path)
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=self._json_default)
            os.replace(tmp_path, file_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load_json(self, key: str) -> Any:
        file_path = self.files.get(key)
        if not file_path or not os.path.exists(file_path):
            return _DEFAULTS.get(key, {})

        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                with open(file_path, "r", encoding=enc) as f:
                    return json.load(f)
            except UnicodeDecodeError:
                continue
            except json.JSONDecodeError as e:
                logger.critical(
                    f"Error loading {key}: JSON decode error ({enc}): {e}. Using defaults."
                )
                return _DEFAULTS.get(key, {})
            except Exception as e:
                logger.critical(
                    f"Unexpected error loading {key} ({enc}): {e}. Using defaults."
                )
                return _DEFAULTS.get(key, {})

        logger.critical(f"Failed to decode {file_path} with all known encodings. Using defaults.")
        return _DEFAULTS.get(key, {})

    def save_json(self, key: str, data: Any) -> bool:
        file_path = self.files.get(key)
        if not file_path:
            logger.critical(f"Unknown key: {key}")
            return False

        try:
            # Validate serializability before touching the real file
            json.dumps(data, ensure_ascii=False, default=self._json_default)
            self._atomic_json_write(file_path, data)
            return True
        except TypeError as e:
            logger.critical(f"Serialization error saving {key}: {e}")
            return False
        except Exception as e:
            logger.critical(f"Error saving {key}: {e}")
            return False
