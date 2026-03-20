import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

import discord
from pssapi.entities.raw import EngagementRaw
from pssapi.utils.exceptions import PssApiError

from classes.views.engagementparticipantsview import EngagementParticipantsView
from data import database_models as models
from data.constants.galaxy import STAR_SYSTEMS
from data.databaseclasses import EngagementSystemData, _ensure_aware
from handlers import databasehandler as crud
from handlers.databasehandler import get_session

if TYPE_CHECKING:
    from classes.bot import FleetWarsBot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_system_id_by_name(system_name: str) -> Optional[int]:
    for sid, name in STAR_SYSTEMS.items():
        if name.lower() == system_name.lower():
            return sid
    return None


async def get_fleet_wars_status(bot: "FleetWarsBot") -> List[Dict]:
    now = datetime.now(tz=timezone.utc)
    systems_data = []

    for system_id, system_name in STAR_SYSTEMS.items():
        try:
            # Get data from cache (will use Fleet Wars cache if tracked, galaxy cache otherwise)
            # Only makes API call if cache is older than 90 minutes, so we avoid missing a 30 minute timer update
            result = await bot.cache_manager.get_galaxy_data_cached(
                system_id=system_id,
                max_age_minutes=90
            )
            if result is None:
                # API call failed
                systems_data.append({
                    'name': system_name,
                    'owner': "Error",
                    'cooldown': "Error"
                })
                continue

            owner_name, cooldown_end = result

            # Format cooldown status
            cooldown_secs = 0
            if cooldown_end:
                time_remaining = cooldown_end - now
                remaining_secs = time_remaining.total_seconds()

                if remaining_secs > 0:
                    hours = int(remaining_secs // 3600)
                    minutes = int((remaining_secs % 3600) // 60)
                    cooldown_status = f"{hours}h {minutes}m"
                    cooldown_secs = int(remaining_secs)
                else:
                    cooldown_status = "⚔️ NOW"
            else:
                cooldown_status = "⚔️ NOW"

            systems_data.append({
                'name': system_name,
                'owner': owner_name,
                'cooldown': cooldown_status,
                'cooldown_seconds': cooldown_secs
            })

        except Exception as e:
            bot.logger.error(f"Error fetching status for {system_name}: {e}")
            systems_data.append({
                'name': system_name,
                'owner': "Error",
                'cooldown': "Error",
                'cooldown_seconds': 0
            })

    await bot.cache_manager.save_fleet_wars_systems()
    return systems_data


async def get_system_status(bot: "FleetWarsBot", system_name: str) -> Optional[Dict]:
    system_id = get_system_id_by_name(system_name)
    if system_id is None:
        return None

    now = datetime.now(tz=timezone.utc)

    result = await bot.cache_manager.get_galaxy_data_cached(
        system_id=system_id,
        max_age_minutes=90
    )

    if result is None:
        return None

    owner_name, cooldown_end = result

    # Format cooldown status
    if cooldown_end:
        time_remaining = cooldown_end - now

        if time_remaining.total_seconds() > 0:
            hours = int(time_remaining.total_seconds() // 3600)
            minutes = int((time_remaining.total_seconds() % 3600) // 60)
            cooldown_status = f"{hours}h {minutes}m"
        else:
            cooldown_status = "⚔️ NOW"
    else:
        cooldown_status = "⚔️ NOW"

    return {
        'name': system_name,
        'owner': owner_name,
        'cooldown': cooldown_status
    }
async def get_active_engagements(bot: "FleetWarsBot") -> List[EngagementSystemData]:
    # Get the highest engagement_id from the database to know where to start
    async with get_session() as session:
        last_engagement_id = await crud.get_max_engagement_id(session)

    await bot.api_manager.ensure_valid_token_age()
    await asyncio.sleep(0.5)

    new_active_engagements = []
    engagement_id = last_engagement_id + 1
    current_time = datetime.now(timezone.utc)
    consecutive_failures = 0
    max_consecutive_failures = 2

    while consecutive_failures < max_consecutive_failures:
        try:
            # Get EngagementRaw object from API
            engagement_raw: EngagementRaw = await bot.api_manager.get_engagement(engagement_id)
            await asyncio.sleep(2)

            # Check if the engagement is active (end_date is in the future)
            is_active = engagement_raw.end_date > current_time

            # Determine final score
            final_score = f"{engagement_raw.attacking_engagement_group_name} {engagement_raw.attacking_points} - {engagement_raw.defending_points} {engagement_raw.defending_engagement_group_name}"

            engagement_type = getattr(engagement_raw, "engagement_type", "Unknown")

            # Create EngagementSystemData object
            engagement_data = EngagementSystemData(
                active=is_active,
                attacker=engagement_raw.attacking_engagement_group_name,
                defender=engagement_raw.defending_engagement_group_name,
                engagement_id=engagement_raw.engagement_id,
                system_id=engagement_raw.star_system_id,
                start_time=engagement_raw.start_date,
                end_time=engagement_raw.end_date,
                outcome=engagement_raw.outcome_type,
                final_score=final_score,
                engagement_type=engagement_type
            )

            # Save to database (upsert handles both new and updates)
            try:
                async with get_session() as session:
                    db_model = engagement_data.to_db_model()
                    await crud.upsert_engagement(session, db_model)
            except Exception as e:
                bot.logger.error(f"Error upserting engagement {engagement_data.engagement_id}: {e}")

            # Add to return list if active
            if is_active:
                bot.logger.info(f"Active engagement found: ID {engagement_id} in system ID {engagement_raw.star_system_id}")
                new_active_engagements.append(engagement_data)

                # Update in-memory cache (write-through)
                bot.cache_manager._CacheManager__active_engagements[engagement_id] = engagement_data

            # Reset consecutive failures on success
            consecutive_failures = 0
            engagement_id += 1

        except PssApiError as e:
            consecutive_failures += 1

            if consecutive_failures >= max_consecutive_failures:
                bot.logger.info(
                    f"Engagement scan complete. "
                    f"Last checked: {engagement_id - 1}. "
                    f"Found {len(new_active_engagements)} new active engagement(s)."
                )
                break

            engagement_id += 1
            await asyncio.sleep(0.5)

        except Exception as e:
            bot.logger.critical(f"Unexpected exception while fetching engagement {engagement_id}: {e}", exc_info=e)
            raise

    return new_active_engagements


async def prune_expired_engagements(bot: "FleetWarsBot") -> int:
    current_time = datetime.now(timezone.utc)
    pruned_count = 0

    try:
        async with get_session() as session:
            active_engagements = await crud.get_all_active_engagements(session)

            for engagement_id, engagement_data in active_engagements.items():
                # Ensure DB value is timezone-aware before comparing because i goofed earlier
                end_time = getattr(engagement_data, "end_time", None)
                end_time = _ensure_aware(end_time)

                if end_time is None:
                    continue

                if end_time <= current_time:
                    try:
                        success = await crud.mark_engagement_inactive(session, engagement_id)
                    except Exception as e:
                        bot.logger.critical("Failed marking engagement inactive")
                        success = False

                    if success:
                        pruned_count += 1
                        bot.logger.info(f"Pruning expired engagement ID {engagement_id} (ended at {end_time})")

                        if engagement_id in bot.cache_manager._CacheManager__active_engagements:
                            del bot.cache_manager._CacheManager__active_engagements[engagement_id]

        if pruned_count > 0:
            remaining_active = len(active_engagements) - pruned_count
            bot.logger.info(f"Pruned {pruned_count} expired engagement(s). {remaining_active} active engagement(s) remaining.")

    except Exception as e:
        bot.logger.critical(f"❌ Error during engagement pruning: {e}")

    return pruned_count
async def create_engagement_embed_option(engagements) -> discord.Embed:
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
    processed: List[tuple[EngagementSystemData, datetime, Optional[datetime]]] = []
    for eng in engagements:
        start = _ensure_aware(getattr(eng, "start_time", None))
        end = _ensure_aware(getattr(eng, "end_time", None))
        if end is None:
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


        et = (eng.engagement_type or "Unknown").lower()
        if "raid" in et or "raiding" in et:
            action_word = "raiding"
            status_emoji = "🟣"
        elif "invasion" in et or "invasions" in et:
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

async def create_engagement_detail_embed(
                                            bot: "FleetWarsBot",
                                            engagement_id: int) -> tuple[discord.Embed, Optional[discord.ui.View]]:
    # Get max engagement ID from DB
    async with get_session() as session:
        max_engagement_id = await crud.get_max_engagement_id(session)

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
        await bot.api_manager.ensure_valid_token_age() # is this needed?
        engagement_raw: EngagementRaw = await bot.api_manager.get_engagement(engagement_id)
    except Exception as e:
        bot.logger.error(e)
        return discord.Embed(
            title="❌ Error Fetching Engagement",
            description=f"Failed to retrieve engagement **{engagement_id}**.",
            color=0xFF0000
        ), None


    now = datetime.now(timezone.utc)
    end_time = _ensure_aware(engagement_raw.end_date)
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
    _raw_attack_users = engagement_raw.attacking_engagement_group._engagement_group_users
    _seen_attack_users = set()
    _attack_users = [
        u for u in _raw_attack_users
        if u.user.id not in _seen_attack_users and not _seen_attack_users.add(u.user.id)
    ]
    number_attackers = len(_attack_users)

    _raw_defend_users = engagement_raw.defending_engagement_group._engagement_group_users
    _defend_users = list(_raw_defend_users)
    number_defenders = len({u.user.id for u in _raw_defend_users if u.user.id > 10000})

    attacker_score = sum(((u.max_lives - u.lives_used) * u.power_score) + u.score for u in _attack_users)
    defender_score = sum(((u.max_lives - u.lives_used) * u.power_score) + u.score for u in _defend_users)

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
        atk_attacks_left = sum((u.max_attacks - u.attacks_used) for u in _attack_users)
        _defend_users_no_npcs = [u for u in _defend_users if u.user.id >= 10000]
        dfn_attacks_left = sum((u.max_attacks - u.attacks_used) for u in _defend_users_no_npcs)
        atk_lives_left = sum((u.max_lives - u.lives_used) for u in _attack_users)
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


async def refresh_galaxy_state(bot: "FleetWarsBot", force_refresh_all: bool = False) -> int:
    now = datetime.now(timezone.utc)
    refreshed_count = 0

    bot.logger.info(f"Starting galaxy state refresh (force_all={force_refresh_all})...")

    # Validate token before starting batch operations to prevent cascade
    try:
        bot.logger.info("Galaxy State Refresh: Validating token before batch operations...")
        await bot.api_manager.ensure_valid_token_age()
    except Exception as e:
        bot.logger.error(f"Galaxy State Refresh: Token validation failed before refresh: {e}", exc_info=e)
        return 0

    async with get_session() as session:
        # Get existing systems from DB
        existing_systems = await crud.get_all_galaxy_systems(session)

        # Determine which systems to refresh
        systems_to_refresh = []

        if force_refresh_all:
            # Refresh all known systems
            systems_to_refresh = list(STAR_SYSTEMS.keys())
        else:
            # Get active engagement system IDs from in-memory cache
            active_engagement_system_ids = {
                eng.system_id
                for eng in bot.cache_manager._CacheManager__active_engagements.values()
            }

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
                        time_until_cooldown = (cooldown_aware - now).total_seconds() / 60
                        if time_until_cooldown < 30:
                            systems_to_refresh.append(system_id)
                else:
                    systems_to_refresh.append(system_id)

        bot.logger.info(f"Refreshing {len(systems_to_refresh)} systems...")

        # Refresh systems concurrently in smaller batches with delays (Option D)
        batch_size = 5  # Reduced from 10 to 5
        for i in range(0, len(systems_to_refresh), batch_size):
            batch = systems_to_refresh[i:i + batch_size]
            tasks = [bot.api_manager.get_galaxy_data(system_id) for system_id in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for system_id, galaxy_data in zip(batch, results):
                if isinstance(galaxy_data, Exception):
                    bot.logger.error(f"Error fetching system {system_id}: {galaxy_data}")
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

                    # Check if system exists in DB
                    existing_system = existing_systems.get(system_id)

                    if existing_system:
                        # Update existing system
                        existing_system.owner_name = owner_name
                        existing_system.cooldown_end = cooldown_end
                        existing_system.last_updated = now
                    else:
                        # Create new system
                        system_name = STAR_SYSTEMS.get(system_id, f"System {system_id}")
                        new_system = models.GalaxySystem(
                            system_id=system_id,
                            system_name=system_name,
                            owner_name=owner_name,
                            cooldown_end=cooldown_end,
                            last_updated=now,
                            is_targeted=False
                        )
                        await crud.upsert_galaxy_system(session, new_system)
                        existing_systems[system_id] = new_system

                    refreshed_count += 1

                except Exception as e:
                    bot.logger.error(f"Error processing system {system_id}: {e}")
                    continue

            # Add delay between batches to avoid overwhelming the API (Option D)
            if i + batch_size < len(systems_to_refresh):
                await asyncio.sleep(0.5)

        # Commit all changes
        await session.commit()

    # Update cache
    await bot.cache_manager.load_galaxy_systems_from_db()

    bot.logger.info(f"Galaxy state refresh completed. Updated {refreshed_count} systems.")
    return refreshed_count