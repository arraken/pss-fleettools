from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from handlers import fleetwarshandler, databasehandler
from handlers.databasehandler import get_session

if TYPE_CHECKING:
    from classes.bot import FleetToolsBot


class Commands(commands.Cog):
    def __init__(self, bot: "FleetToolsBot"):
        self.bot = bot

    @app_commands.command(name="engagements", description="Show all currently active fleet engagements")
    async def engagements(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            active = self.bot.cache_manager._CacheManager__active_engagements
            embed = await fleetwarshandler.create_engagement_embed_option(active)
            await interaction.followup.send(embed=embed)

        except Exception as e:
            self.bot.logger.error(f"Error in /engagements: {e}", exc_info=e)
            await interaction.followup.send("❌ Error fetching engagements.", ephemeral=True)

    @app_commands.command(name="engagement_stats", description="Show detailed stats for a specific engagement")
    @app_commands.describe(engagement_id="The engagement ID to look up")
    async def engagement_stats(self, interaction: discord.Interaction, engagement_id: int) -> None:
        await interaction.response.defer()
        try:
            embed, view = await fleetwarshandler.create_engagement_detail_embed(self.bot, engagement_id)
            if view:
                await interaction.followup.send(embed=embed, view=view)
            else:
                await interaction.followup.send(embed=embed)
        except Exception as e:
            self.bot.logger.error(f"Error in /engagement_stats: {e}", exc_info=e)
            await interaction.followup.send("❌ Error fetching engagement stats.", ephemeral=True)

    @app_commands.command(name="galaxy_status", description="View all star systems with cooldown times and owners")
    async def galaxy_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.followup.send("🔄 Fetching galaxy data for all systems... This may take a moment.", ephemeral=True)
        systems_data = await fleetwarshandler.get_fleet_wars_status(self.bot)

        # Group systems by owner
        groups: dict[str, list] = {}
        for system in systems_data:
            owner = system.get('owner') or "Unknown"
            groups.setdefault(owner, []).append(system)

        # Within each group: under attack first, attackable (cooldown_seconds == 0) first, then sorted by cooldown_seconds asc
        def sort_key(s):
            if s.get('under_attack'):
                return (0, 0)
            secs = s.get('cooldown_seconds', 0)
            return (1 if secs == 0 else 2, secs)

        for owner in groups:
            groups[owner].sort(key=sort_key)

        # Sort fleet groups: "Unowned" last, errors last, then alphabetical
        def group_sort_key(owner):
            if owner == "Error":
                return (2, owner)
            if owner.lower() == "unowned":
                return (1, owner)
            return (0, owner.lower())

        sorted_owners = sorted(groups.keys(), key=group_sort_key)

        embed = discord.Embed(
            title="🌌 Fleet Wars System Status",
            color=discord.Color.purple()
        )

        # Build a flat list of lines per fleet: bold header + system lines
        # Then distribute across exactly 3 columns (fields) by line count.
        all_blocks: list[list[str]] = []
        for owner in sorted_owners:
            systems = groups[owner]
            block = [f"**{owner}**"]
            for system in systems:
                if system.get('under_attack'):
                    icon = "🟡"
                elif system.get('cooldown_seconds', 0) == 0:
                    icon = "🟢"
                else:
                    icon = "🔴"
                block.append(f"{icon} {system['name']} • {system['cooldown']}")
            all_blocks.append(block)

        # Flatten to a single ordered list of lines for distribution
        all_lines: list[str] = []
        for block in all_blocks:
            all_lines.extend(block)
            all_lines.append("")  # blank separator between fleets

        # Remove trailing blank
        while all_lines and all_lines[-1] == "":
            all_lines.pop()

        # Distribute lines across 3 columns as evenly as possible,
        # but never split a fleet block across columns.
        # Greedily fill each column up to ~1/3 of total lines.
        target = len(all_lines) / 3
        columns: list[list[str]] = [[], [], []]
        col_idx = 0
        current_count = 0

        for block in all_blocks:
            block_with_sep = block + [""]
            # Move to next column if we've hit the target and aren't on the last column
            if current_count >= target * (col_idx + 1) and col_idx < 2:
                col_idx += 1
            columns[col_idx].extend(block_with_sep)
            current_count += len(block_with_sep)

        # Strip trailing blanks from each column and join
        for i, col in enumerate(columns):
            while col and col[-1] == "":
                col.pop()
            value = "\n".join(col) if col else "\u200b"
            # Truncate to Discord's 1024 char field limit
            if len(value) > 1024:
                value = value[:1021] + "…"
            embed.add_field(name="\u200b", value=value, inline=True)

        embed.set_footer(text="🟢 Attackable NOW  •  🟡 Active Engagement  •  🔴 On cooldown")
        embed.timestamp = discord.utils.utcnow()

        await interaction.channel.send(embed=embed)

    @app_commands.command(name="prestige_calculator", description="Generates prestige paths (if any) for the crew for that player")
    @app_commands.choices(min_rarity=[
        app_commands.Choice(name="1 Stars/Common/White", value="Common"),
        app_commands.Choice(name="2 Stars/Elite/Green", value="Elite"),
        app_commands.Choice(name="3 Stars/Unique/Blue", value="Unique"),
        app_commands.Choice(name="4 Stars/Epic/Purple", value="Epic"),
        app_commands.Choice(name="5 Stars/Hero/Orange", value="Hero"),])
    async def prestige_calculator(self, interaction: discord.Interaction, player_name: str, target_crew_name: str, exclude: Optional[str] = None, min_rarity: str = "Common"):
        from handlers import prestigehandler
        await interaction.response.defer()

        # Get player
        users = await self.bot.api_manager.get_user_by_name(player_name)
        if not users:
            return await interaction.followup.send(f"❌ Player not found: {player_name}")
        player = users[0]

        # Get target crew
        target_crews = await self.bot.api_manager.get_crew_by_name(target_crew_name)
        if not target_crews:
            return await interaction.followup.send(f"❌ Could not find crew named: {target_crew_name}")
        target_crew = target_crews[0] if isinstance(target_crews, list) else target_crews

        # Get player's crew
        raw_crew_list = await self.bot.api_manager.get_ship_characters_by_user_name(player_name)
        if not raw_crew_list:
            return await interaction.followup.send(f"❌ Could not retrieve crew for {player_name}")

        player_crew = await prestigehandler.generate_crewmember_list_from_raw(raw_crew_list)

        # Filter crew by minimum rarity FIRST (before exclusions)
        if min_rarity != "Common":
            original_crew_count = len(player_crew)
            player_crew = prestigehandler.filter_crew_by_minimum_rarity(self.bot, player_crew, min_rarity)
            filtered_count = original_crew_count - len(player_crew)
            if filtered_count > 0:
                self.bot.logger.info(f"Filtered out {filtered_count} crew below {min_rarity} rarity for {player_name}")

        # Handle exclusions AFTER rarity filter
        excluded_crew_names = []
        if exclude:
            player_crew, excluded_crew_names = await prestigehandler.resolve_excluded_crew(exclude, player_crew, self.bot)

        # Use prestige recipes from cache_manager (loads from storage or rebuilds if needed)
        if not self.bot.cache_manager.api_prestige_recipes:
            await interaction.followup.send("⏳ Prestige recipes still loading, please try again in a moment...")
            return

        # Find prestige paths
        paths, missing_crew_status = await prestigehandler.find_prestige_paths(
            self.bot,
            player_crew,
            target_crew.character_design_id,
            self.bot.cache_manager.api_prestige_recipes
        )

        # Create and send embed
        embed = await prestigehandler.create_prestige_embed(
            player_name,
            target_crew,
            paths,
            missing_crew_status,
            excluded_crew_names if excluded_crew_names else None,
            min_rarity if min_rarity != "Common" else None,
        )
        # Only pass view if it's not None
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="regen_prestige", description="regen prestige recipes")
    @app_commands.checks.has_permissions(administrator=True)
    async def regen_prestige_recipes(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.bot.cache_manager.rebuild_prestige_recipes()
        await interaction.followup.send("Prestige recipes have been regenerated")

    @app_commands.command(name="player_history", description="Show a player's entire recorded history as a table image")
    @app_commands.describe(player="Player name or numeric player ID")
    async def player_history(self, interaction: discord.Interaction, player: str):
        from handlers import playerhistoryhandler

        await interaction.response.defer()
        try:
            try:
                user_id, player_name = await playerhistoryhandler.resolve_player(self.bot, player)
            except playerhistoryhandler.PlayerNotFoundError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return

            history = await playerhistoryhandler.fetch_full_user_history(self.bot, user_id)
            if not history:
                await interaction.followup.send(f"❌ No history found for **{player_name}**.", ephemeral=True)
                return

            player_name = playerhistoryhandler.get_latest_player_name(history, player_name)
            rows = await playerhistoryhandler.build_history_rows(self.bot, player_name, history)
            image_buffer = playerhistoryhandler.render_history_image(player_name, rows)

            file = discord.File(image_buffer, filename=f"{player_name}_history.png")
            await interaction.followup.send(file=file)
        except Exception as e:
            self.bot.logger.error(f"Error in /player_history: {e}", exc_info=e)
            await interaction.followup.send("❌ Error fetching player history.", ephemeral=True)

    @app_commands.command(name="search_engagements", description="Search engagements for the fleet name and number of engagements to return")
    @app_commands.describe(fleet_name="Fleet name to search for", limit="Number of engagements to return (default: 5)")
    async def search_engagements(self, interaction: discord.Interaction, fleet_name: str, limit: int = 5):
        await interaction.response.defer()
        if limit > 20:
            await interaction.followup.send("❌ Limit cannot exceed 15.", ephemeral=True)
            return
        try:
            engagements = await fleetwarshandler.search_engagements_by_fleet(self.bot, fleet_name, limit)
            if not engagements:
                await interaction.followup.send(f"❌ No engagements found for fleet: {fleet_name}", ephemeral=True)
                return

            embed = await fleetwarshandler.create_engagement_search_embed(engagements, fleet_name)
            await interaction.followup.send(embed=embed)
        except Exception as e:
            self.bot.logger.error(f"Error in /search_engagements: {e}", exc_info=e)
            await interaction.followup.send("❌ Error searching engagements.", ephemeral=True)

    @app_commands.command(name="helpfleettools", description="List all available FleetTools commands and their arguments")
    async def helpfleettools(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="FleetTools — Command Reference",
            description="Below are all available commands and their arguments.",
            color=discord.Color.blurple(),
        )
        embed.timestamp = discord.utils.utcnow()

        embed.add_field(
            name="`/engagements`",
            value=("Show all currently active fleet war engagements."),
            inline=False,
        )

        embed.add_field(
            name="`/engagement_stats`",
            value=(
                "Show detailed statistics for a specific engagement.\n"
                "**Required**\n"
                "• `engagement_id` — the numeric ID of the engagement to look up, use `/engagements` for numbers"),
            inline=False,
        )

        embed.add_field(
            name="`/galaxy_status`",
            value=(
                "View all star systems with their current owners and cooldown timers.\n"
                "Systems are grouped by owner fleet, with attackable systems shown first.\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="`/prestige_calculator`",
            value=(
                "Find prestige crafting paths to reach a target crew using a player's current roster.\n"
                "**Required**\n"
                "• `player_name` — exact in-game player name to look up\n"
                "• `target_crew_name` — name of the crew you want to craft\n"
                "**Optional**\n"
                "• `exclude` — comma-separated crew names to exclude from the calculation\n"
                "• `min_rarity` — ignore crew below this rarity tier *(default: Common)*\n"
                "  › `Common` · `Elite` · `Unique` · `Epic` · `Hero`"
            ),
            inline=False,
        )

        embed.add_field(
            name="`/set_marketwatch_channel`",
            value=(
                "**Admin only.** Sets the channel where market watch (buy/sell) updates are posted for this server.\n"
                "**Optional**\n"
                "• `channel` — the text channel to post updates to. Omit to stop market watch for this server."
            ),
            inline=False,
        )

        embed.add_field(
            name="`/player_history`",
            value=(
                "Show a player's entire recorded month-by-month history (rank, fleet, division, stars, trophies) "
                "as a spreadsheet-style image. Missing months are shown as `-`.\n"
                "**Required**\n"
                "• `player` — a player name or a numeric player ID"
            ),
            inline=False,
        )

        embed.set_footer(text="FleetTools  •  Use / to autocomplete any command")
        await interaction.response.send_message(embed=embed, ephemeral=True)

        if interaction.user.id == 210545386580869121:  # Only show this debug message to the bot owner
            for guild in self.bot.guilds:
                self.bot.logger.info(f"Found guild: {guild.name} - ID: {guild.id}")


    @app_commands.command(name="market_watch", description="(Re)starts the market watch background feed")
    async def market_watch_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            self.bot.api_manager.start_market_watch()
            await interaction.followup.send("✅ Market watch (re)started — check logs for the live feed.", ephemeral=True)
        except Exception as e:
            self.bot.logger.error(f"[market_watch] Failed to start market watch: {e}", exc_info=e)
            await interaction.followup.send(f"❌ Market watch failed to start: `{e}`", ephemeral=True)

    @app_commands.command(name="set_marketwatch_channel", description="Set (or clear) the channel where market watch updates are posted for this server")
    @app_commands.describe(channel="Channel to post market updates to. Omit / choose none to stop market watch for this server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_marketwatch_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id

        try:
            async with get_session() as session:
                if channel is None:
                    removed = await databasehandler.delete_alert_channel(session, guild_id, channel_type="marketwatch")
                    self.bot.api_manager.clear_market_watch_channel(guild_id)
                    if removed:
                        await interaction.followup.send("🛑 Market watch channel cleared — pusher stopped for this server.", ephemeral=True)
                    else:
                        await interaction.followup.send("ℹ️ No market watch channel was configured for this server.", ephemeral=True)
                else:
                    await databasehandler.upsert_alert_channel(session, guild_id, channel.id, channel_type="marketwatch")
                    self.bot.api_manager.set_market_watch_channel(guild_id, channel.id)
                    await interaction.followup.send(f"✅ Market watch will now post updates to {channel.mention}.", ephemeral=True)
        except Exception as e:
            self.bot.logger.error(f"Error in /set_marketwatch_channel: {e}", exc_info=e)
            await interaction.followup.send("❌ Failed to update the market watch channel.", ephemeral=True)
    @app_commands.command(name="prestige_status", description="Show prestige recipe cache/storage status")
    async def prestige_status(self, interaction: discord.Interaction):
        meta = self.bot.cache_manager.get_prestige_meta()
        cached_recipe_count = sum(len(r) for r in self.bot.cache_manager.api_prestige_recipes.values())

        last_attempt_start = meta.get("last_attempt_start")
        last_success = meta.get("last_success")
        saved_recipe_count = meta.get("recipe_count")

        embed = discord.Embed(
            title="Prestige Recipe Status",
            color=discord.Color.blue()
        )
        embed.add_field(name="Recipes currently in memory", value=str(cached_recipe_count), inline=False)
        embed.add_field(name="Last successful rebuild", value=str(last_success or "Never recorded"), inline=False)
        embed.add_field(name="Recipes at last successful rebuild",
                        value=str(saved_recipe_count if saved_recipe_count is not None else "Unknown"), inline=False)
        embed.add_field(name="Last rebuild attempt started", value=str(last_attempt_start or "Never recorded"),
                        inline=False)

        # Warn if the most recent attempt is newer than the most recent success -
        # that means a regen was started but never finished (e.g. bot was
        # restarted mid-build), so the data on disk/in-memory is stale.
        if last_attempt_start and (not last_success or last_attempt_start > last_success):
            embed.color = discord.Color.orange()
            embed.add_field(
                name="⚠️ Warning",
                value=(
                    "The last rebuild attempt appears to have never completed "
                    "(likely interrupted by a bot restart). The recipes shown above "
                    "are from an earlier, older rebuild. Try running /regen_prestige again."
                ),
                inline=False
            )

        await interaction.followup.send(embed=embed)