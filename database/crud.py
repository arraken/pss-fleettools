from typing import Sequence, TYPE_CHECKING

import discord, json
from sqlmodel import col, func, select, or_, update
from sqlmodel.ext.asyncio.session import AsyncSession
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from icecream import ic

from . import models
from classes.databaseclasses import EngagementSystemData
from handlers import databasehandlers as DBH

def _get_session():
    from database.db import get_session
    return get_session

# ============================================================================
# Fleet wars commands
# ============================================================================

async def upsert_engagement(session: AsyncSession, db_engagement: models.EngagementDB) -> bool:
    try:
        await session.merge(db_engagement)
        return True
    except Exception:
        return False

async def get_all_active_engagements(session: AsyncSession) -> Dict[int, models.EngagementDB]:
    try:
        stmt = select(models.EngagementDB).where(models.EngagementDB.active == True)
        result = await session.exec(stmt)
        rows = result.all()
        return {eng.engagement_id: eng for eng in rows}
    except Exception:
        return {}

async def get_max_engagement_id(session: AsyncSession) -> int:
    try:
        stmt = select(models.EngagementDB.engagement_id).order_by(col(models.EngagementDB.engagement_id).desc()).limit(1)
        result = await session.exec(stmt)
        row = result.first()
        return int(row) if row is not None else 0
    except Exception:
        return 0

async def mark_engagement_inactive(session: AsyncSession, engagement_id: int) -> bool:
    stmt = select(models.EngagementDB).where(models.EngagementDB.engagement_id == engagement_id)
    result = await session.exec(stmt)
    engagement: Optional[models.EngagementDB] = result.first()

    if not engagement:
        return False
    
    engagement.active = False
    engagement.last_checked = datetime.now(timezone.utc)
    session.add(engagement)
    return True



async def get_engagements_by_system(session: AsyncSession, system_id: int, active_only: bool) -> Sequence[models.EngagementDB]:
    try:
        stmt = select(models.EngagementDB).where(models.EngagementDB.system_id == system_id)
        if active_only:
            stmt = stmt.where(models.EngagementDB.active == True)
        stmt = stmt.order_by(col(models.EngagementDB.start_time).desc())
        result = await session.exec(stmt)
        return result.all()
    except Exception:
        return []

async def get_engagements_by_fleet(session: AsyncSession, fleet_name: str, active_only: bool) -> Sequence[models.EngagementDB]:
    try:
        stmt = select(models.EngagementDB).where(
            or_(
                models.EngagementDB.attacker == fleet_name,
                models.EngagementDB.defender == fleet_name
            )
        )
        if active_only:
            stmt = stmt.where(models.EngagementDB.active == True)
        stmt = stmt.order_by(col(models.EngagementDB.start_time).desc())
        result = await session.exec(stmt)
        return result.all()
    except Exception:
        return []
    
async def get_expired_engagements(session: AsyncSession) -> Dict[int, models.EngagementDB]:
    current_time = DBH.ensure_aware(datetime.now(timezone.utc)).replace(tzinfo=None)
    stmt = (
        select(models.EngagementDB)
        .where(models.EngagementDB.active == True)
        .where(col(models.EngagementDB.end_time) < current_time)
    )
    result = await session.exec(stmt)
    rows = result.all()
    return {eng.engagement_id: eng for eng in rows}

async def count_active_engagements(session: AsyncSession) -> int:
    stmt = (
        select(func.count(col(models.EngagementDB.engagement_id)))
        .where(col(models.EngagementDB.active) == True)
    )
    result = await session.exec(stmt)
    return result.one()

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

async def mark_system_under_attack(session: AsyncSession, engagement_data: EngagementSystemData) -> models.GalaxySystemDB | None:
    now = datetime.now(timezone.utc)
    galaxy_system = await get_galaxy_system(session, engagement_data.system_id)
    if not galaxy_system:
        return None
    
    galaxy_system.under_attack = True
    galaxy_system.active_engagement_id = engagement_data.engagement_id
    galaxy_system.last_updated = now

    await upsert_galaxy_system(session, galaxy_system)
    await session.refresh(galaxy_system)

    return galaxy_system

async def deactivate_under_attack_system(session: AsyncSession, system_id: int) -> models.GalaxySystemDB | None:
    now = datetime.now(timezone.utc)
    galaxy_system = await get_galaxy_system(session, system_id)
    if not galaxy_system:
        return None
    
    galaxy_system.under_attack = False
    galaxy_system.active_engagement_id = None
    galaxy_system.last_updated = now

    await upsert_galaxy_system(session, galaxy_system)
    await session.refresh(galaxy_system)

    return galaxy_system


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
