from typing import Sequence, TYPE_CHECKING

import discord, json
from sqlmodel import col, select, or_
from sqlmodel.ext.asyncio.session import AsyncSession
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from icecream import ic

from . import models


def _get_session():
    from database.db import get_session
    return get_session

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

async def get_engagements_by_system(session: AsyncSession, system_id: int, active_only: bool) -> Sequence[models.Engagement]:
    try:
        stmt = select(models.Engagement).where(models.Engagement.system_id == system_id)
        if active_only:
            stmt = stmt.where(models.Engagement.active == True)
        stmt = stmt.order_by(models.Engagement.start_time.desc())
        result = await session.exec(stmt)
        return result.all()
    except Exception:
        return []

async def get_engagements_by_fleet(session: AsyncSession, fleet_name: str, active_only: bool) -> Sequence[models.Engagement]:
    try:
        stmt = select(models.Engagement).where(
            or_(
                models.Engagement.attacker == fleet_name,
                models.Engagement.defender == fleet_name
            )
        )
        if active_only:
            stmt = stmt.where(models.Engagement.active == True)
        stmt = stmt.order_by(col(models.Engagement.start_time).desc())
        result = await session.exec(stmt)
        return result.all()
    except Exception:
        return []

# ============================================================================
# Galaxy system commands
# ============================================================================

async def upsert_galaxy_system(session: AsyncSession, galaxy_system: models.GalaxySystemDB) -> bool:
    try:
        await session.merge(galaxy_system)
        return True
    except Exception:
        return False


async def get_galaxy_system(session: AsyncSession, system_id: int) -> Optional[models.GalaxySystemDB]:
    return await session.get(models.GalaxySystemDB, system_id)


async def get_all_galaxy_systems(session: AsyncSession) -> Dict[int, models.GalaxySystemDB]:
    result = await session.exec(select(models.GalaxySystemDB))
    systems = result.all()
    return {system.system_id: system for system in systems}


async def get_targeted_galaxy_systems(session: AsyncSession) -> Dict[int, models.GalaxySystemDB]:
    result = await session.exec(select(models.GalaxySystemDB).where(models.GalaxySystemDB.is_targeted == True))
    systems = result.all()
    return {system.system_id: system for system in systems}


async def clear_system_target_by_fleet_id(session: AsyncSession, system_id: int, fleet_id: int) -> bool:
    system = await session.get(models.GalaxySystemDB, system_id)
    if not system or (system and not system.is_targeted):
        return False

    stmt = (
        select(models.SystemTargetDB)
        .where(models.SystemTargetDB.targeting_fleet_id == fleet_id)
        .where(models.SystemTargetDB.system_id == system_id)
    )
    target_data = await session.exec(stmt)
    if not target_data:
        return False

    await session.delete(target_data)

    stmt = (
        select(models.SystemTargetDB)
        .where(models.SystemTargetDB.system_id == system_id)
    )
    results = await session.exec(stmt)
    target_data = results.first()
    if results is None:
        system.is_targeted = False
        await upsert_galaxy_system(session, system)

    return True

# ============================================================================
# Dynamic config stubs (FleetRoleMapping / AlertChannel)
# TODO: finish these when implementing the dynamic admin role + channel system
# ============================================================================

async def get_all_fleet_role_mappings(session: AsyncSession) -> Dict[str, models.FleetRoleMappingDB]:
    """Returns a dict of fleet_name -> FleetRoleMapping for building admin_role_mapping."""
    result = await session.exec(select(models.FleetRoleMappingDB))
    rows = result.all()
    return {row.fleet_name: row for row in rows}


async def get_alert_channel(session: AsyncSession, guild_id: int, channel_type: str) -> Optional[models.AlertChannelDB]:
    """Returns the AlertChannel row for a given guild and channel type."""
    stmt = (
        select(models.AlertChannelDB)
        .where(models.AlertChannelDB.guild_id == guild_id)
        .where(models.AlertChannelDB.channel_type == channel_type)
    )
    result = await session.exec(stmt)
    return result.first()
