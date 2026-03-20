import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from data import database_models as models
from sqlmodel.ext.asyncio.session import AsyncSession

DATABASE_URL = "sqlite+aiosqlite:///./data/fleetwars.db"

_engine: AsyncEngine | None = None

async def init_engine():
    global _engine
    if _engine is None:
        os.makedirs("data", exist_ok=True)
        _engine = create_async_engine(
            DATABASE_URL,
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=StaticPool,
            echo=False,
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


# ============================================================================
# Fleet wars commands
# ============================================================================

async def upsert_engagement(session: AsyncSession, db_engagement: models.Engagement) -> bool:
    try:
        await session.merge(db_engagement)
        await session.flush()
        return True
    except Exception:
        return False

async def get_all_active_engagements(session: AsyncSession) -> Dict[int, models.Engagement]:
    try:
        stmt = select(models.Engagement).where(models.Engagement.active == True)
        result = await session.exec(stmt)
        rows = result.all()
        return {eng.engagement_id: eng for eng in rows}
    except Exception:
        return {}

async def get_max_engagement_id(session: AsyncSession) -> int:
    try:
        stmt = select(models.Engagement.engagement_id).order_by(models.Engagement.engagement_id.desc()).limit(1)
        result = await session.exec(stmt)
        row = result.first()
        return int(row) if row is not None else 0
    except Exception:
        return 0

async def mark_engagement_inactive(session: AsyncSession, engagement_id: int) -> bool:
    try:
        stmt = select(models.Engagement).where(models.Engagement.engagement_id == engagement_id)
        result = await session.exec(stmt)
        engagement: Optional[models.Engagement] = result.first()

        if engagement:
            engagement.active = False
            engagement.last_checked = datetime.now(timezone.utc)
            session.add(engagement)
            await session.flush()
            return True
        return False
    except Exception:
        return False

async def get_engagements_by_system(session: AsyncSession, system_id: int, active_only: bool = False) -> List[models.Engagement]:
    try:
        stmt = select(models.Engagement).where(models.Engagement.system_id == system_id)
        if active_only:
            stmt = stmt.where(models.Engagement.active == True)
        stmt = stmt.order_by(models.Engagement.start_time.desc())
        result = await session.exec(stmt)
        return result.all()
    except Exception:
        return []

async def get_engagements_by_fleet(session: AsyncSession, fleet_name: str, active_only: bool = False) -> List[models.Engagement]:
    try:
        stmt = select(models.Engagement).where(
            or_(
                models.Engagement.attacker == fleet_name,
                models.Engagement.defender == fleet_name
            )
        )
        if active_only:
            stmt = stmt.where(models.Engagement.active == True)
        stmt = stmt.order_by(models.Engagement.start_time.desc())
        result = await session.exec(stmt)
        return result.all()
    except Exception:
        return []

# ============================================================================
# Galaxy system commands
# ============================================================================

async def upsert_galaxy_system(session: AsyncSession, galaxy_system: models.GalaxySystem) -> bool:
    try:
        await session.merge(galaxy_system)
        await session.flush()
        return True
    except Exception:
        return False


async def get_galaxy_system(session: AsyncSession, system_id: int) -> Optional[models.GalaxySystem]:
    return await session.get(models.GalaxySystem, system_id)


async def get_all_galaxy_systems(session: AsyncSession) -> Dict[int, models.GalaxySystem]:
    result = await session.exec(select(models.GalaxySystem))
    systems = result.all()
    return {system.system_id: system for system in systems}


async def get_targeted_galaxy_systems(session: AsyncSession) -> Dict[int, models.GalaxySystem]:
    result = await session.exec(select(models.GalaxySystem).where(models.GalaxySystem.is_targeted == True))
    systems = result.all()
    return {system.system_id: system for system in systems}


async def clear_system_target(session: AsyncSession, system_id: int) -> bool:
    system = await session.get(models.GalaxySystem, system_id)
    if system:
        system.is_targeted = False
        system.targeting_fleet = None
        system.flagged_by = None
        system.admin_role_id = None
        system.flagged_at = None
        await session.flush()
        return True
    return False

# ============================================================================
# Dynamic config stubs (FleetRoleMapping / AlertChannel)
# TODO: finish these when implementing the dynamic admin role + channel system
# ============================================================================

async def get_all_fleet_role_mappings(session: AsyncSession) -> Dict[str, models.FleetRoleMapping]:
    """Returns a dict of fleet_name -> FleetRoleMapping for building admin_role_mapping."""
    result = await session.exec(select(models.FleetRoleMapping))
    rows = result.all()
    return {row.fleet_name: row for row in rows}


async def get_alert_channel(session: AsyncSession, guild_id: int, channel_type: str = "engagements") -> Optional[models.AlertChannel]:
    """Returns the AlertChannel row for a given guild and channel type."""
    stmt = (
        select(models.AlertChannel)
        .where(models.AlertChannel.guild_id == guild_id)
        .where(models.AlertChannel.channel_type == channel_type)
    )
    result = await session.exec(stmt)
    return result.first()

