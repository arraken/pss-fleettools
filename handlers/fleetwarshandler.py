import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Set

import discord
from pssapi.entities.raw import EngagementRaw
from pssapi.utils.exceptions import PssApiError

from classes.views.engagementparticipantsview import EngagementParticipantsView
from database import models
from data.constants.galaxy import STAR_SYSTEMS
from classes.databaseclasses import GalaxySystem
from classes.databaseclasses import EngagementSystemData

#from handlers import databasehandler as crud
#from handlers.databasehandler import get_session

if TYPE_CHECKING:
    from classes.bot import FleetToolsBot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_systems_to_refresh(existing_systems: Dict[int, GalaxySystem], active_engagement_system_ids: Set[int], current_time: datetime, force_refresh):
    # Determine which systems to refresh
    systems_to_refresh: List[int] = []

    if force_refresh:
        # Refresh all known systems
        systems_to_refresh = list(STAR_SYSTEMS.keys())
    else:
        # Get active engagement system IDs from in-memory cache

        for system_id in STAR_SYSTEMS.keys():
            if system_id in existing_systems:
                system = existing_systems[system_id]

                # Always refresh systems with an active engagement so we
                # pick up EngagementCooldownEndDate the cycle after it ends
                if system_id in active_engagement_system_ids:
                    systems_to_refresh.append(system_id)
                    continue

                if system.cooldown_end is None:
                    systems_to_refresh.append(system_id)
                else:
                    cooldown_aware = system.cooldown_end.replace(
                        tzinfo=timezone.utc) if system.cooldown_end.tzinfo is None else system.cooldown_end
                    time_until_cooldown = (cooldown_aware - current_time).total_seconds() / 60
                    if time_until_cooldown < 30:
                        systems_to_refresh.append(system_id)
            else:
                systems_to_refresh.append(system_id)

    return systems_to_refresh
