from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

import discord

from handlers import plannerhandler as ph
from database.models import PlannerCrew, PlannerStep

PAGE_SIZE = 25


class _OwnedView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.interaction: Optional[discord.Interaction] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This planner belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.interaction is not None:
            try:
                await self.interaction.edit_original_response(view=None)
            except discord.HTTPException:
                pass


def _select_options(rows: Sequence[PlannerCrew], page: int, selected_id: Optional[int]) -> List[discord.SelectOption]:
    chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    return [discord.SelectOption(
        label=ph.crew_label(r)[:100], value=str(r.id), description=r.rarity or None,
        emoji="✨" if r.virtual else None, default=(r.id == selected_id)) for r in chunk]


def _pages(rows: Sequence) -> int:
    return max(1, -(-len(rows) // PAGE_SIZE))


class PlanView(_OwnedView):
    """Two dependent crew dropdowns (paged past Discord's 25-option cap) plus Confirm/Cancel."""

    def __init__(self, owner_id: int, profile_id: int, target: Tuple[int, str, str],
                 pairs: Sequence[Tuple[int, int]], rows: Sequence[PlannerCrew]):
        super().__init__(owner_id)
        self.profile_id = profile_id
        self.target_id, self.target_name, self.target_rarity = target
        self.pairs = pairs
        self.rows = list(rows)
        self.first_rows = ph.first_choices(self.rows, pairs)
        self.second_rows: List[PlannerCrew] = []
        self.page1 = self.page2 = 0
        self.first: Optional[PlannerCrew] = None
        self.second: Optional[PlannerCrew] = None
        self._refresh()

    def embed(self, note: Optional[str] = None) -> discord.Embed:
        embed = discord.Embed(title=f"Plan: {self.target_name}", color=discord.Color.blurple(),
                              description="Pick the two crew to prestige together.")
        embed.add_field(name="Crew 1", value=ph.crew_label(self.first) if self.first else "—", inline=False)
        embed.add_field(name="Crew 2", value=ph.crew_label(self.second) if self.second else "—", inline=False)
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        return embed

    def _refresh(self) -> None:
        self.pick1.options = _select_options(self.first_rows, self.page1, self.first.id if self.first else None)
        pages1 = _pages(self.first_rows)
        self.pick1.placeholder = "Crew 1" + (f" (page {self.page1 + 1}/{pages1})" if pages1 > 1 else "")
        self.prev1.disabled = self.page1 <= 0
        self.next1.disabled = self.page1 >= pages1 - 1
        self.prev1.row = self.next1.row = 2

        if self.first is None:
            self.pick2.options = [discord.SelectOption(label="Pick crew 1 first", value="none")]
            self.pick2.disabled = True
            self.pick2.placeholder = "Crew 2"
            self.prev2.disabled = self.next2.disabled = True
        else:
            pages2 = _pages(self.second_rows)
            self.pick2.options = _select_options(self.second_rows, self.page2,
                                                 self.second.id if self.second else None)
            self.pick2.disabled = False
            self.pick2.placeholder = "Crew 2" + (f" (page {self.page2 + 1}/{pages2})" if pages2 > 1 else "")
            self.prev2.disabled = self.page2 <= 0
            self.next2.disabled = self.page2 >= pages2 - 1
        self.confirm.disabled = not (self.first and self.second)

    async def _update(self, interaction: discord.Interaction, note: Optional[str] = None) -> None:
        self._refresh()
        await interaction.response.edit_message(embed=self.embed(note), view=self)

    @discord.ui.select(placeholder="Crew 1", row=0, options=[discord.SelectOption(label="-", value="-")])
    async def pick1(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.first = next((r for r in self.first_rows if str(r.id) == select.values[0]), None)
        self.second = None
        self.page2 = 0
        self.second_rows = ph.second_choices(self.rows, self.pairs, self.first) if self.first else []
        await self._update(interaction)

    @discord.ui.select(placeholder="Crew 2", row=1, options=[discord.SelectOption(label="-", value="-")])
    async def pick2(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.second = next((r for r in self.second_rows if str(r.id) == select.values[0]), None)
        await self._update(interaction)

    @discord.ui.button(label="◀ Crew 1", style=discord.ButtonStyle.secondary, row=2)
    async def prev1(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page1 -= 1
        await self._update(interaction)

    @discord.ui.button(label="Crew 1 ▶", style=discord.ButtonStyle.secondary, row=2)
    async def next1(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page1 += 1
        await self._update(interaction)

    @discord.ui.button(label="◀ Crew 2", style=discord.ButtonStyle.secondary, row=3)
    async def prev2(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page2 -= 1
        await self._update(interaction)

    @discord.ui.button(label="Crew 2 ▶", style=discord.ButtonStyle.secondary, row=3)
    async def next2(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page2 += 1
        await self._update(interaction)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, row=4, disabled=True)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            step = await ph.add_step(self.profile_id, self.target_id, self.target_name, self.target_rarity,
                                     self.first.id, self.second.id, self.pairs)
        except ph.PlannerError as e:
            await interaction.response.edit_message(embed=discord.Embed(
                title="❌ Couldn't plan that", description=str(e), color=discord.Color.red()), view=None)
            self.stop()
            return
        embed = discord.Embed(
            title=f"✅ Step {step.step_no} planned", color=discord.Color.green(),
            description=f"~~{step.crew1_label}~~\n~~{step.crew2_label}~~\n➜ ✨ **{step.target_name}** (Lv ?)")
        embed.set_footer(text="The two crew are now reserved. Use /prestige_planner roster to see your plan.")
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=4)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()


class IssueView(_OwnedView):
    """Resolves a step whose crew changed in-game: confirm a detected result, or keep/drop a broken step."""

    def __init__(self, owner_id: int, profile_id: int, issue: ph.Issue):
        super().__init__(owner_id)
        self.profile_id = profile_id
        self.issue = issue
        if issue.kind == "match":
            self.yes.label, self.no.label = "Yes, I did it", "Not yet"
        else:
            self.yes.label, self.no.label = "Drop step", "Keep"
            self.yes.style = discord.ButtonStyle.danger

    @staticmethod
    def embed_for(issue: ph.Issue) -> discord.Embed:
        step = issue.step
        if issue.kind == "match":
            return discord.Embed(
                title=f"🔎 Did you complete step {step.step_no}?", color=discord.Color.gold(),
                description=(f"Planned: {step.crew1_label} + {step.crew2_label} ➜ **{step.target_name}**\n"
                             f"Both ingredients left your ship and a new **{ph.crew_label(issue.matched)}** appeared.\n"
                             "Mark this step as done?"))
        return discord.Embed(
            title=f"⚠️ Step {step.step_no}: ingredient missing", color=discord.Color.orange(),
            description=(f"Planned: {step.crew1_label} + {step.crew2_label} ➜ **{step.target_name}**\n"
                         "At least one ingredient is no longer on your ship and no matching result was found.\n"
                         "Drop the step (frees any remaining crew and removes the planned result), or keep it?"))

    async def _finish(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.response.edit_message(content=text, embed=None, view=None)
        self.stop()

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if self.issue.kind == "match":
                await ph.confirm_match(self.profile_id, self.issue.step.id)
                await self._finish(interaction, f"✅ Step {self.issue.step.step_no} marked as done.")
            else:
                dropped = await ph.drop_step(self.profile_id, self.issue.step.id)
                extra = f" (and {len(dropped) - 1} dependent step(s))" if len(dropped) > 1 else ""
                await self._finish(interaction, f"🗑️ Step {self.issue.step.step_no} dropped{extra}.")
        except ph.PlannerError as e:
            await self._finish(interaction, f"❌ {e}")

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.issue.kind == "match":
            await ph.reject_match(self.profile_id, self.issue.step.id)
            await self._finish(interaction, "👍 Okay, I'll keep the step as planned and flag it as missing ingredients.")
        else:
            await self._finish(interaction, "👍 Keeping the step.")


class ConfirmView(_OwnedView):
    def __init__(self, owner_id: int, action: Callable[[], Awaitable[str]]):
        super().__init__(owner_id, timeout=120)
        self.action = action

    @discord.ui.button(label="Yes, reset", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        text = await self.action()
        await interaction.response.edit_message(content=text, embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()


class PagerView(_OwnedView):
    def __init__(self, owner_id: int, pages: List[discord.Embed]):
        super().__init__(owner_id)
        self.pages = pages
        self.index = 0
        self._sync()

    def _sync(self) -> None:
        self.prev.disabled = self.index <= 0
        self.next.disabled = self.index >= len(self.pages) - 1

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)


def _paged_embeds(title: str, lines: List[str], per_page: int, footer: str, color: discord.Color) -> List[discord.Embed]:
    chunks = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [["*Nothing here yet.*"]]
    pages = []
    for i, chunk in enumerate(chunks, 1):
        embed = discord.Embed(title=title, description="\n".join(chunk), color=color)
        embed.set_footer(text=f"{footer}  •  Page {i}/{len(chunks)}")
        pages.append(embed)
    return pages


def roster_pages(display_name: str, rows: Sequence[PlannerCrew], steps: Sequence[PlannerStep]) -> List[discord.Embed]:
    step_no = {s.id: s.step_no for s in steps}
    produced_by = {s.result_row_id: s.step_no for s in steps if s.result_row_id}

    def key(r: PlannerCrew):
        group = 0 if r.virtual else (2 if r.used_in_step is not None else 1)
        return group, r.design_name.lower(), -(r.level or 0), r.id or 0

    lines = []
    for r in sorted(rows, key=key):
        label = ph.crew_label(r)
        if r.virtual:
            lines.append(f"✨ {label} — planned in step {produced_by.get(r.id, '?')}")
        elif r.used_in_step is not None:
            warn = " ⚠️ no longer on ship" if r.gone else ""
            lines.append(f"🔒 ~~{label}~~ — used in step {step_no.get(r.used_in_step, '?')}{warn}")
        else:
            lines.append(f"✅ {label}")
    available = len(ph.available_rows([r for r in rows if not r.virtual]))
    return _paged_embeds(f"{display_name}'s planner roster", lines, 25,
                         f"✅ available ({available})  ✨ planned  🔒 used", discord.Color.blurple())


_STATUS_TEXT = {
    ph.STATUS_PLANNED: "📝 planned", ph.STATUS_MISSING: "⚠️ ingredient missing",
    ph.STATUS_PENDING: "🔎 awaiting confirmation", ph.STATUS_DONE: "✅ done",
}


def history_pages(display_name: str, steps: Sequence[PlannerStep]) -> List[discord.Embed]:
    lines = [f"**Step {s.step_no}** — {_STATUS_TEXT.get(s.status, s.status)}\n"
             f"{s.crew1_label} + {s.crew2_label} ➜ **{s.target_name}**" for s in steps]
    return _paged_embeds(f"{display_name}'s prestige plan", lines, 8, "Use /prestige_planner undo to remove the latest step",
                         discord.Color.blurple())
