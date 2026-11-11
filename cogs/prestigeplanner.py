from typing import TYPE_CHECKING, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from classes.views.prestigeplannerviews import (ConfirmView, IssueView, PagerView, PlanView, history_pages,
                                                roster_pages)
from handlers import plannerhandler as ph

if TYPE_CHECKING:
    from classes.bot import FleetToolsBot

MAX_ISSUES_SHOWN = 5


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class PrestigePlanner(commands.Cog):
    planner = app_commands.Group(name="prestige_planner", description="Plan out multiple prestiges from your roster")

    def __init__(self, bot: "FleetToolsBot"):
        self.bot = bot

    # ------------------------------------------------------------------ helpers
    async def _target_autocomplete(self, interaction: discord.Interaction,
                                   current: str) -> list[app_commands.Choice[str]]:
        recipes = self.bot.cache_manager.api_prestige_recipes
        current = current.lower()
        names = sorted({c.name for c in self.bot.cache_manager.get_api_crew_list()
                        if _int(c.design_id) in recipes and current in c.name.lower()})
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]

    async def _resolve_target(self, name: str) -> Optional[Tuple[int, str, str]]:
        lowered = name.strip().lower()
        for crew in self.bot.cache_manager.get_api_crew_list():
            if crew.name.lower() == lowered:
                return int(crew.design_id), crew.name, crew.rarity
        design = await self.bot.api_manager.get_crew_by_name(name)
        if design is None:
            return None
        return design.character_design_id, design.character_design_name, design.rarity

    async def _prepare(self, interaction: discord.Interaction, player_name: str,
                       force: bool = False) -> Optional[ph.SyncResult]:
        """Sync the roster and surface anything needing the user's attention. Assumes the response is deferred."""
        try:
            result = await ph.sync_profile(self.bot, interaction.user.id, player_name, force=force)
        except ph.PlannerError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return None
        if result.warning:
            await interaction.followup.send(f"⚠️ {result.warning}", ephemeral=True)

        issues = await ph.get_issues(result.profile.id)
        for issue in issues[:MAX_ISSUES_SHOWN]:
            view = IssueView(interaction.user.id, result.profile.id, issue)
            await interaction.followup.send(embed=IssueView.embed_for(issue), view=view, ephemeral=True)
        if len(issues) > MAX_ISSUES_SHOWN:
            await interaction.followup.send(
                f"…and {len(issues) - MAX_ISSUES_SHOWN} more step(s) need attention. "
                "Resolve these and run a command again.", ephemeral=True)
        return result

    async def _run(self, interaction: discord.Interaction, name: str, coro) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await coro
        except Exception as e:
            self.bot.logger.error(f"Error in /prestige_planner {name}: {e}", exc_info=e)
            await interaction.followup.send("❌ Something went wrong with the prestige planner.", ephemeral=True)

    # ------------------------------------------------------------------ commands
    @planner.command(name="plan", description="Plan a prestige for a target crew using your current roster")
    @app_commands.describe(player_name="Your exact in-game player name", target_crew="The crew you want to create")
    @app_commands.autocomplete(target_crew=_target_autocomplete)
    async def plan(self, interaction: discord.Interaction, player_name: str, target_crew: str):
        await self._run(interaction, "plan", self._plan(interaction, player_name, target_crew))

    async def _plan(self, interaction: discord.Interaction, player_name: str, target_crew: str) -> None:
        recipes_by_target = self.bot.cache_manager.api_prestige_recipes
        if not recipes_by_target:
            await interaction.followup.send("⏳ Prestige recipes still loading, please try again in a moment.",
                                            ephemeral=True)
            return
        target = await self._resolve_target(target_crew)
        if target is None:
            await interaction.followup.send(f"❌ Could not find crew named: {target_crew}", ephemeral=True)
            return
        recipes = recipes_by_target.get(target[0], [])
        if not recipes:
            await interaction.followup.send(f"❌ No known prestige recipes create **{target[1]}**.", ephemeral=True)
            return

        result = await self._prepare(interaction, player_name)
        if result is None:
            return
        rows, _ = await ph.get_roster(result.profile.id)
        pairs = ph.recipe_pairs(recipes)
        if not ph.first_choices(rows, pairs):
            shown = "\n".join(f"• {r.crew1_name} + {r.crew2_name}" for r in recipes[:10])
            more = f"\n…and {len(recipes) - 10} more" if len(recipes) > 10 else ""
            await interaction.followup.send(
                f"❌ **{result.profile.display_name}** has no available crew pair that makes **{target[1]}**.\n"
                f"Crew already used in earlier planned steps are unavailable.\n**Recipes:**\n{shown}{more}",
                ephemeral=True)
            return

        view = PlanView(interaction.user.id, result.profile.id, target, pairs, rows)
        view.interaction = interaction
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)

    @planner.command(name="roster", description="Show your planner roster and which crew are used or planned")
    @app_commands.describe(player_name="Your exact in-game player name",
                           refresh="Re-fetch your crew from the game now instead of using the saved copy")
    async def roster(self, interaction: discord.Interaction, player_name: str, refresh: bool = False):
        await self._run(interaction, "roster", self._roster(interaction, player_name, refresh))

    async def _roster(self, interaction: discord.Interaction, player_name: str, refresh: bool) -> None:
        result = await self._prepare(interaction, player_name, force=refresh)
        if result is None:
            return
        rows, _ = await ph.get_roster(result.profile.id)
        steps = await ph.get_steps(result.profile.id)
        pages = roster_pages(result.profile.display_name, rows, steps)
        await self._send_pages(interaction, pages)

    @planner.command(name="history", description="Show every step in your prestige plan")
    @app_commands.describe(player_name="Your exact in-game player name")
    async def history(self, interaction: discord.Interaction, player_name: str):
        await self._run(interaction, "history", self._history(interaction, player_name))

    async def _history(self, interaction: discord.Interaction, player_name: str) -> None:
        result = await self._prepare(interaction, player_name)
        if result is None:
            return
        steps = await ph.get_steps(result.profile.id)
        await self._send_pages(interaction, history_pages(result.profile.display_name, steps))

    @staticmethod
    async def _send_pages(interaction: discord.Interaction, pages) -> None:
        if len(pages) == 1:
            await interaction.followup.send(embed=pages[0], ephemeral=True)
            return
        view = PagerView(interaction.user.id, pages)
        await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)

    @planner.command(name="undo", description="Remove your most recent planned step")
    @app_commands.describe(player_name="Your exact in-game player name")
    async def undo(self, interaction: discord.Interaction, player_name: str):
        await self._run(interaction, "undo", self._undo(interaction, player_name))

    async def _undo(self, interaction: discord.Interaction, player_name: str) -> None:
        result = await self._prepare(interaction, player_name)
        if result is None:
            return
        try:
            dropped = await ph.undo_last_step(result.profile.id)
        except ph.PlannerError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        names = ", ".join(f"step {s.step_no} ({s.target_name})" for s in dropped)
        await interaction.followup.send(f"↩️ Removed {names}. Those crew are available again.", ephemeral=True)

    @planner.command(name="reset", description="Clear your whole plan and reload your roster from the game")
    @app_commands.describe(player_name="Your exact in-game player name")
    async def reset(self, interaction: discord.Interaction, player_name: str):
        await self._run(interaction, "reset", self._reset(interaction, player_name))

    async def _reset(self, interaction: discord.Interaction, player_name: str) -> None:
        result = await self._prepare(interaction, player_name)
        if result is None:
            return
        profile_id = result.profile.id

        async def do_reset() -> str:
            await ph.reset_profile(profile_id)
            try:
                await ph.sync_profile(self.bot, interaction.user.id, player_name, force=True)
            except ph.PlannerError as e:
                return f"🗑️ Plan cleared, but the roster refresh failed: {e}"
            return "🗑️ Plan cleared and roster reloaded."

        view = ConfirmView(interaction.user.id, do_reset)
        await interaction.followup.send(
            "This deletes **all** planned and completed steps and reloads your roster. Continue?",
            view=view, ephemeral=True)
