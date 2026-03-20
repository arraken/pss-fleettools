from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from handlers import fleetwarshandler

if TYPE_CHECKING:
    from classes.bot import FleetWarsBot


class Commands(commands.Cog):
    def __init__(self, bot: "FleetWarsBot"):
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

    @app_commands.command(name="galaxy_status", description="Show fleet war system ownership and cooldowns")
    async def galaxy_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            systems_data = await fleetwarshandler.get_fleet_wars_status(self.bot)

            # Sort: systems that can be attacked (⚔️ NOW) first, then by cooldown ascending
            def sort_key(s):
                return (0 if s.get("cooldown") == "⚔️ NOW" else 1, s.get("cooldown_seconds", 9999999))

            systems_data.sort(key=sort_key)

            lines = []
            for system in systems_data:
                name = system.get("name", "Unknown")
                owner = system.get("owner", "Unknown")
                cooldown = system.get("cooldown", "Unknown")
                lines.append(f"**{name}** — {owner} | {cooldown}")

            embed = discord.Embed(
                title="🌌 Galaxy Status",
                color=0x4169E1,
            )

            full_text = "\n".join(lines)
            if len(full_text) <= 4096:
                embed.description = full_text
            else:
                chunk_size = 20
                for i in range(0, len(lines), chunk_size):
                    chunk = lines[i:i + chunk_size]
                    embed.add_field(
                        name=f"Systems {i + 1}–{min(i + chunk_size, len(lines))}",
                        value="\n".join(chunk),
                        inline=False,
                    )

            embed.set_footer(text="⚔️ NOW = cooldown expired, can be attacked")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            self.bot.logger.error(f"Error in /galaxy_status: {e}", exc_info=e)
            await interaction.followup.send("❌ Error fetching galaxy status.", ephemeral=True)

