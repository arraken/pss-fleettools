import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, List, Optional, Sequence, TYPE_CHECKING

from sqlalchemy import or_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession


DATABASE_URL = "sqlite+aiosqlite:///./data/fleetwars.db"

# Global engine for standalone CRUD operations
_engine: AsyncEngine | None = None

async def init_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            DATABASE_URL,
            future=True,
            pool_pre_ping=True,
            connect_args={
                "timeout": 30,
                "check_same_thread": False,
            },
            pool_size=5,
            max_overflow=10
        )
        async with _engine.begin() as conn:
            # Enable WAL mode for better concurrency
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.run_sync(SQLModel.metadata.create_all)

@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    global _engine
    if _engine is None:
        await init_engine()

    async with AsyncSession(_engine, expire_on_commit=False) as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

if TYPE_CHECKING:
    from classes import FleetWarsBot

# Import crud after get_session is defined to avoid circular import
from . import crud
from . import models

class AsyncAutoRollbackSession:
    def __init__(self, engine: AsyncEngine):
        self.__engine: AsyncEngine = engine
        self.__session = AsyncSession(self.__engine, expire_on_commit=False)

    async def __aenter__(self):
        self.__connection = await self.__engine.connect()
        async with self.__session.begin():
            return self.__session

    async def __aexit__(self, exc_type, exception, _):
        if exception and exc_type is DBAPIError:
            # Auto-rollback on error
            await self.__session.rollback()
        # Optional: auto-commit
        else:
            await self.__session.commit()
        await self.__session.close()
        await self.__connection.close()

class DatabaseManager():
    __engine: AsyncEngine

    def __init__(self, bot: "FleetWarsBot"):
        self.bot = bot
        #asyncio.create_task(self.__set_up_db_engine(DATABASE_URL))

    async def async_init(self):
        await self.__set_up_db_engine(DATABASE_URL)
    # -------------------------------------------- ENGAGEMENTS --------------------------------------------------------------
    async def add_engagement(self, db_engagement: models.Engagement) -> bool:
        async with self.__session() as session:
            return await crud.upsert_engagement(session, db_engagement)

    async def get_all_active_engagements(self) -> Dict[int, models.Engagement]:
        async with self.__session() as session:
            return await crud.get_all_active_engagements(session)

    async def get_highest_engagement_id(self) -> int:
        async with self.__session() as session:
            return await crud.get_max_engagement_id(session)

    async def mark_engagement_inactive(self, engagement_id: int) -> bool:
        async with self.__session() as session:
            return await crud.mark_engagement_inactive(session, engagement_id)

    async def get_engagements_by_system(self, system_id: int, active_only: bool = False) -> Sequence[models.Engagement]:
        async with self.__session() as session:
            return await crud.get_engagements_by_system(session, system_id, active_only)

    async def get_engagements_by_fleet(self, fleet_name: str, active_only: bool = False) -> Sequence[models.Engagement]:
        async with self.__session() as session:
            return await crud.get_engagements_by_fleet(session, fleet_name, active_only)
    # -------------------------------------------- GALAXY DATA --------------------------------------------------------------
    async def add_galaxy_system(self, galaxy_system: models.GalaxySystemDB) -> bool:
        async with self.__session() as session:
            return await crud.upsert_galaxy_system(session, galaxy_system)

    async def get_galaxy_system(self, system_id: int) -> Optional[models.GalaxySystemDB]:
        async with self.__session() as session:
            return await crud.get_galaxy_system(session, system_id)

    async def get_all_galaxy_systems(self) -> Dict[int, models.GalaxySystemDB]:
        async with self.__session() as session:
            return await crud.get_all_galaxy_systems(session)

    async def get_targeted_galaxy_systems(self) -> Dict[int, models.GalaxySystemDB]:
        async with self.__session() as session:
            return await crud.get_targeted_galaxy_systems(session)

    async def clear_system_target_by_fleet_id(self, system_id: int, fleet_id: int) -> bool:
        async with self.__session() as session:
            return await crud.clear_system_target_by_fleet_id(session, system_id, fleet_id)
    #     # -------------------------------------------- DISCORD DATA --------------------------------------------------------------

    async def get_all_fleet_role_mappings(self) -> Dict[str, models.FleetRoleMappingDB]:
        async with self.__session() as session:
            return await crud.get_all_fleet_role_mappings(session)

    async def get_alert_channel(self, guild_id: int, channel_type: str = "engagements") -> Optional[models.AlertChannelDB]:
        async with self.__session() as session:
            return await crud.get_alert_channel(session, guild_id, channel_type)
    # ----------------------------------------------------------------------------------------------------------
    def __session(self):
        return AsyncAutoRollbackSession(self.__engine)

    async def __set_up_db_engine(self, database_url: str):
        connect_args = {
            "timeout": 30,
            "check_same_thread": False,
        }
        self.__engine: AsyncEngine = create_async_engine(
            database_url,
            future=True,
            connect_args=connect_args,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10
        )
        async with self.__engine.begin() as conn:
            # Enable WAL mode for better concurrency
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.run_sync(SQLModel.metadata.create_all)
        #SQLModel.metadata.create_all(engine)
