from typing import TYPE_CHECKING

import discord
from discord import ui

if TYPE_CHECKING:
    from pssapi.entities.raw import EngagementRaw


class EngagementParticipantsView(ui.View):
    def __init__(self, engagement_raw: "EngagementRaw", engagement_id: int, *, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.engagement_raw = engagement_raw
        self.engagement_id = engagement_id

    @ui.button(label="⚔️ Attacking Ships", style=discord.ButtonStyle.primary, custom_id="attacking_ships")
    async def attacking_ships_button(self, interaction: discord.Interaction, button: ui.Button):
        await self._show_participants(interaction, is_attacking=True)

    @ui.button(label="🛡️ Defending Ships", style=discord.ButtonStyle.secondary, custom_id="defending_ships")
    async def defending_ships_button(self, interaction: discord.Interaction, button: ui.Button):
        await self._show_participants(interaction, is_attacking=False)

    @ui.button(label="⚔️ Remaining Attackers", style=discord.ButtonStyle.success, custom_id="remaining_attackers")
    async def remaining_attackers_button(self, interaction: discord.Interaction, button: ui.Button):
        await self._show_remaining(interaction, is_attacking=True)

    @ui.button(label="🛡️ Remaining Defenders", style=discord.ButtonStyle.success, custom_id="remaining_defenders")
    async def remaining_defenders_button(self, interaction: discord.Interaction, button: ui.Button):
        await self._show_remaining(interaction, is_attacking=False)

    async def _show_remaining(self, interaction: discord.Interaction, is_attacking: bool):
        if is_attacking:
            group = self.engagement_raw.attacking_engagement_group
            group_name = self.engagement_raw.attacking_engagement_group_name
            emoji = "⚔️"
        else:
            group = self.engagement_raw.defending_engagement_group
            group_name = self.engagement_raw.defending_engagement_group_name
            emoji = "🛡️"

        raw_users = group._engagement_group_users
        player_users = [u for u in raw_users if u.user.id >= 10000]

        remaining_lines = []
        for idx, user_data in enumerate(player_users, 1):
            user = user_data.user
            ship_name = user.name if user.name else "Unknown Ship"
            attacks_left = user_data.max_attacks - user_data.attacks_used
            lives_left = user_data.max_lives - user_data.lives_used

            if attacks_left > 0:
                line = (
                    f"**{idx}.** {ship_name} - "
                    f"⚔️ {attacks_left} attacks left | "
                    f"❤️ {lives_left} lives left"
                )
                remaining_lines.append(line)

        if not remaining_lines:
            await interaction.response.send_message(
                f"No player ships found for **{group_name}**.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"{emoji} {group_name} - Remaining Attacks and Lives",
            description=f"**{len(remaining_lines)}** player ship(s) in engagement [{self.engagement_id}]",
            color=0xFF4500 if is_attacking else 0x4169E1,
        )
        full_text = "\n".join(remaining_lines)
        embed.description += f"\n\n{full_text}"
        embed.set_footer(text=f"Engagement ID: {self.engagement_id}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _show_participants(self, interaction: discord.Interaction, is_attacking: bool):
        if is_attacking:
            group = self.engagement_raw.attacking_engagement_group
            group_name = self.engagement_raw.attacking_engagement_group_name
            color = 0xFF4500
            emoji = "⚔️"
        else:
            group = self.engagement_raw.defending_engagement_group
            group_name = self.engagement_raw.defending_engagement_group_name
            color = 0x4169E1
            emoji = "🛡️"

        raw_users = group._engagement_group_users
        player_users = [u for u in raw_users if u.user.id >= 10000]

        seen_user_ids = set()
        unique_users = []
        for u in player_users:
            if u.user.id not in seen_user_ids:
                seen_user_ids.add(u.user.id)
                unique_users.append(u)

        if not unique_users:
            await interaction.response.send_message(
                f"No player ships found for **{group_name}**.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"{emoji} {group_name} - Participating Ships",
            description=f"**{len(unique_users)}** player ship(s) in engagement [{self.engagement_id}]",
            color=color,
        )

        ship_lines = []
        for idx, user_data in enumerate(unique_users, 1):
            user = user_data.user
            ship_name = user.name if user.name else "Unknown Ship"
            attacks_left = user_data.max_attacks - user_data.attacks_used
            lives_left = user_data.max_lives - user_data.lives_used
            score = user_data.score
            power = user_data.power_score

            line = (
                f"**{idx}.** {ship_name}\n"
                f"   └ ⚔️ {attacks_left}/{user_data.max_attacks} attacks | "
                f"❤️ {lives_left}/{user_data.max_lives} lives | "
                f"⚡ {power:,} power | "
                f"📊 {score:,} pts"
            )
            ship_lines.append(line)

        full_text = "\n".join(ship_lines)

        if len(full_text) <= 4096:
            embed.description += f"\n\n{full_text}"
        else:
            chunk_size = 10
            for i in range(0, len(ship_lines), chunk_size):
                chunk = ship_lines[i:i + chunk_size]
                field_name = f"Ships {i + 1}-{min(i + chunk_size, len(ship_lines))}"
                field_value = "\n".join(chunk)
                if len(field_value) > 1024:
                    half = len(chunk) // 2
                    embed.add_field(name=f"{field_name} (Part 1)", value="\n".join(chunk[:half]), inline=False)
                    embed.add_field(name=f"{field_name} (Part 2)", value="\n".join(chunk[half:]), inline=False)
                else:
                    embed.add_field(name=field_name, value=field_value, inline=False)

        embed.set_footer(text=f"Engagement ID: {self.engagement_id}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

