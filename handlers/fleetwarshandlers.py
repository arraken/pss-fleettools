import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Set

import discord
from pssapi.entities.raw import EngagementRaw
from pssapi.utils.exceptions import PssApiError

from classes.databaseclasses import GalaxySystemDB
from classes.databaseclasses import EngagementSystemData
from classes.views.engagementparticipantsview import EngagementParticipantsView
from database import models
from data.constants.galaxy import STAR_SYSTEMS
from handlers import databasehandlers as DBH

if TYPE_CHECKING:
    from classes.bot import FleetToolsBot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_systems_to_refresh(existing_systems: Dict[int, GalaxySystemDB], active_engagement_system_ids: Set[int], current_time: datetime, force_refresh):
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

def format_cooldown_status(system: GalaxySystemDB, remaining_seconds: int):
    # Format cooldown status
    if system.under_attack:
        return "⚔️"

    if remaining_seconds > 0:
        hours = int(remaining_seconds // 3600)
        minutes = int((remaining_seconds % 3600) // 60)
        return f"{hours}h {minutes}m"

    return "Open"

def get_remaining_cooldown_seconds(system_data: GalaxySystemDB):
    now = datetime.now(tz=timezone.utc)

    cooldown_end = system_data.cooldown_end
    if not cooldown_end:
        return 0

    cooldown_end = DBH.ensure_aware(cooldown_end)
    time_remaining = cooldown_end - now

    return int(time_remaining.total_seconds())

async def generate_engagements_status_embed(engagements: Dict[int, EngagementSystemData] | List[EngagementSystemData]) -> discord.Embed:
    # Accept dict or list
    if isinstance(engagements, dict):
        engagements = list(engagements.values())

    # Empty case
    if not engagements:
        embed = discord.Embed(
            title="⚔️ Active Engagements",
            description="**No active engagements at this time.**",
            color=0x808080
        )
        return embed

    # Validate type
    if not isinstance(engagements[0], EngagementSystemData):
        raise TypeError(f"Expected EngagementSystemData objects, got {type(engagements[0])}")

    # Normalize times and filter out entries without end_time
    now = datetime.now(timezone.utc)
    processed: List[tuple[EngagementSystemData, datetime, datetime]] = []

    for eng in engagements:
        start = DBH.ensure_aware(eng.start_time)
        end = DBH.ensure_aware(eng.end_time)
        if not end:
            continue
        processed.append((eng, start, end))

    if not processed:
        embed = discord.Embed(
            title="⚔️ Active Engagements",
            description="**No active engagements at this time.**",
            color=0x808080
        )
        return embed

    # Sort by end_time (earliest ending first)
    sorted_engagements = sorted(processed, key=lambda t: t[2])

    # Determine minimum time remaining (hours)
    now = datetime.now(timezone.utc)
    min_time_remaining = min((t[2] - now).total_seconds() / 3600 for t in sorted_engagements)

    # Choose color / status based on min remaining
    if min_time_remaining < 1:
        color = 0xFF0000
        color_status = "🔴"
    elif min_time_remaining < 2:
        color = 0xFF8C00
        color_status = "🟠"
    elif min_time_remaining < 6:
        color = 0xFFFF00
        color_status = "🟡"
    else:
        color = 0x00FF00
        color_status = "🟢"

    embed = discord.Embed(
        title="⚔️ Active Engagements",
        description=f"**{len(sorted_engagements)} ongoing battles**",
        color=color
    )

    # Build lines using the timezone-aware end_time and current now
    lines: List[str] = []
    for eng, start, end in sorted_engagements:
        system_name = STAR_SYSTEMS.get(eng.system_id, f"System #{eng.system_id}")
        timestamp = int(end.timestamp())

        time_remaining_hours = (end - now).total_seconds() / 3600
        if time_remaining_hours < 1:
            status_emoji = "🔴"
        elif time_remaining_hours < 6:
            status_emoji = "🟠"
        elif time_remaining_hours < 12:
            status_emoji = "🟡"
        else:
            status_emoji = "🟢"


        engagement_type = (eng.engagement_type or "Unknown").lower()
        if "raid" in engagement_type or "raiding" in engagement_type:
            action_word = "raiding"
            status_emoji = "🟣"
        elif "invasion" in engagement_type or "invasions" in engagement_type:
            action_word = "invading"
        else:
            action_word = "attacking"

        line = f"{status_emoji} [{eng.engagement_id}] **{eng.attacker}** {action_word} **{eng.defender}** | {system_name} | ends <t:{timestamp}:R>"
        lines.append(line)

    full_text = "\n".join(lines)

    # Respect Discord embed description limits
    if len(full_text) <= 4096:
        embed.description = f"**{len(sorted_engagements)} ongoing battles** • {color_status}\n\n" + full_text
    else:
        embed.description = f"**{len(sorted_engagements)} ongoing battles** • {color_status}"
        chunk_size = 10
        for i in range(0, len(lines), chunk_size):
            chunk = lines[i:i + chunk_size]
            field_name = f"Battles {i + 1}-{min(i + chunk_size, len(lines))}"
            embed.add_field(name=field_name, value="\n".join(chunk), inline=False)

    embed.set_footer(text="🟣 raid | 🟢 >12h | 🟡 <12h | 🟠 <6h | 🔴 <1h • Sorted by time remaining")
    return embed

async def create_engagement_detail_embed(engagement_id: int, engagement_raw: EngagementRaw) -> tuple[discord.Embed, Optional[discord.ui.View]]:
    now = datetime.now(timezone.utc)
    end_time = DBH.ensure_aware(engagement_raw.end_date)
    is_active = end_time > now if end_time else False

    # Engagement type + emoji
    etype = (engagement_raw.engagement_type or "").lower()
    if "invasion" in etype:
        emoji = "🚀"
        title = "Invasion Engagement"
    elif "raid" in etype:
        emoji = "⚔️"
        title = "Raid Engagement"
    else:
        emoji = "⚔️"
        title = "Fleet Engagement"

    # Score + outcome

    #if engagement_raw.outcome_type == "Playing":
    raw_attack_users = engagement_raw.attacking_engagement_group._engagement_group_users
    seen_attack_users = set()
    attack_users = [
        u for u in raw_attack_users
        if u.user.id not in seen_attack_users and not seen_attack_users.add(u.user.id)
    ]
    number_attackers = len(attack_users)

    raw_defend_users = engagement_raw.defending_engagement_group._engagement_group_users
    defend_users = list(raw_defend_users)
    number_defenders = len({u.user.id for u in raw_defend_users if u.user.id > 10000})

    attacker_score = sum(((user.max_lives - user.lives_used) * user.power_score) + user.score for user in attack_users)
    defender_score = sum(((user.max_lives - user.lives_used) * user.power_score) + user.score for user in defend_users)

    final_score = (
        f"{engagement_raw.attacking_engagement_group_name} "
        f"**{attacker_score}** - "
        f"**{defender_score}** "
        f"{engagement_raw.defending_engagement_group_name}"
    )
    '''else:
        final_score = (
            f"{engagement_raw.attacking_engagement_group_name} "
            f"{engagement_raw.attacking_points} - "
            f"{engagement_raw.defending_points} "
            f"{engagement_raw.defending_engagement_group_name}"
        )'''

    # System name
    system_name = STAR_SYSTEMS.get(
        engagement_raw.star_system_id,
        f"System #{engagement_raw.star_system_id}"
    )

    # Embed color
    color = 0x00FF00 if is_active else 0xFF0000

    embed = discord.Embed(
        title=f"{emoji} {title}",
        description=(
            f"**{engagement_raw.attacking_engagement_group_name}** "
            f"vs "
            f"**{engagement_raw.defending_engagement_group_name}** [{engagement_raw.engagement_id}]"
        ),
        color=color
    )

    embed.add_field(
        name="🪐 System",
        value=system_name,
        inline=True
    )

    if engagement_raw.outcome_type == "Playing":
        embed.add_field(
            name="📊 Current Score",
            value=final_score,
            inline=True
        )
    else:
        embed.add_field(
            name="📊 Final Score",
            value=final_score,
            inline=True
        )

    embed.add_field(
        name="🏁 Outcome",
        value=engagement_raw.outcome_type or "Unknown",
        inline=True
    )

    if end_time:
        embed.add_field(
            name="⏳ Ends",
            value=f"<t:{int(end_time.timestamp())}:R>",
            inline=False
        )

        if "invasion" in etype:
            cutoff_time = engagement_raw.start_date + timedelta(hours=12)
        elif "raid" in etype:
            cutoff_time = engagement_raw.start_date + timedelta(hours=3)
        else:
            cutoff_time = engagement_raw.start_date + timedelta(hours=12)

        embed.add_field(
            name="🗓️ Cutoff to Join",
            value=f"<t:{int(cutoff_time.timestamp())}:F>",
            inline=True
        )

        # Remaining attacks
        atk_attacks_left = sum((u.max_attacks - u.attacks_used) for u in attack_users)
        _defend_users_no_npcs = [u for u in defend_users if u.user.id >= 10000]
        dfn_attacks_left = sum((u.max_attacks - u.attacks_used) for u in _defend_users_no_npcs)
        atk_lives_left = sum((u.max_lives - u.lives_used) for u in attack_users)
        dfn_lives_left = sum((u.max_lives - u.lives_used) for u in _defend_users_no_npcs)

        embed.add_field(
            name="⚔️ Remaining Attacks",
            value=(
                f"**{engagement_raw.attacking_engagement_group_name}:** {atk_attacks_left} attacks left\n"
                f"**{engagement_raw.defending_engagement_group_name}:** {dfn_attacks_left} attacks left"
            ),
            inline=False
        )

        embed.add_field(
            name="❤️ Remaining Lives",
            value=(
                f"**{engagement_raw.attacking_engagement_group_name}:** {atk_lives_left} lives left\n"
                f"**{engagement_raw.defending_engagement_group_name}:** {dfn_lives_left} lives left"
            ),
            inline=True
        )

        embed.add_field(
            name="👥 Participants",
            value=(
                f"**{engagement_raw.attacking_engagement_group_name}:** {number_attackers} participants\n"
                f"**{engagement_raw.defending_engagement_group_name}:** {number_defenders} participants"
            ),
            inline=True
        )

    embed.set_footer(
        text=f"Engagement ID: {engagement_id} • {'ACTIVE' if is_active else 'FINISHED'}"
    )

    # Create the view with participant buttons
    view = EngagementParticipantsView(engagement_raw, engagement_id)

    return embed, view
