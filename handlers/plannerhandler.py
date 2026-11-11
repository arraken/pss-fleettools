from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from sqlmodel import select

from database import get_session
from database.models import PlannerCrew, PlannerProfile, PlannerStep
from handlers import prestigehandler

if TYPE_CHECKING:
    from classes import FleetToolsBot

STALE_AFTER = timedelta(minutes=30)

STATUS_PLANNED = "planned"
STATUS_MISSING = "missing"
STATUS_PENDING = "pending_confirm"
STATUS_DONE = "done"
ACTIVE_STATUSES = (STATUS_PLANNED, STATUS_MISSING, STATUS_PENDING)


class PlannerError(Exception):
    """Raised for user-facing planner problems."""


@dataclass
class SyncResult:
    profile: PlannerProfile
    warning: Optional[str] = None


@dataclass
class Issue:
    kind: str  # "match" | "missing"
    step: PlannerStep
    matched: Optional[PlannerCrew] = None
    crew1: str = ""
    crew2: str = ""


def crew_label(row: PlannerCrew) -> str:
    level = "?" if row.level is None else row.level
    return f"{row.design_name} - {row.crew_name} - Lv {level}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Profiles / sync
# ---------------------------------------------------------------------------

async def _get_profile(session, discord_id: int, player_name: str) -> Optional[PlannerProfile]:
    result = await session.exec(select(PlannerProfile).where(
        PlannerProfile.discord_id == discord_id, PlannerProfile.player_name == player_name.strip().lower()))
    return result.first()


async def _fetch_live_crew(bot: "FleetToolsBot", player_name: str) -> Dict[str, prestigehandler.CrewMember]:
    raw = await bot.api_manager.get_ship_characters_by_user_name(player_name)
    if not raw or "Character" not in raw:
        raise PlannerError(f"Could not retrieve crew for {player_name}.")
    return await prestigehandler.generate_crewmember_list_from_raw(raw)


def _design_lookup(bot: "FleetToolsBot") -> Dict[int, prestigehandler.CrewMember]:
    lookup = {}
    for crew in bot.cache_manager.get_api_crew_list():
        try:
            lookup[int(crew.design_id)] = crew
        except (TypeError, ValueError):
            continue
    return lookup


async def sync_profile(bot: "FleetToolsBot", discord_id: int, player_name: str,
                       force: bool = False) -> SyncResult:
    """Refresh the stored roster if it's stale (or forced) and reconcile planned steps with it."""
    async with get_session() as session:
        profile = await _get_profile(session, discord_id, player_name)

    is_new = profile is None
    fresh = (profile is not None and profile.last_synced is not None
             and _now() - _aware(profile.last_synced) < STALE_AFTER)
    if fresh and not force:
        return SyncResult(profile)

    warning = None
    live: Optional[Dict[str, prestigehandler.CrewMember]] = None
    try:
        live = await _fetch_live_crew(bot, player_name)
    except PlannerError:
        if is_new:
            raise
        warning = "Couldn't refresh your crew from the game; showing the last saved roster."
    except Exception as e:
        bot.logger.error(f"Planner roster fetch failed for {player_name!r}: {e}", exc_info=e)
        if is_new:
            raise PlannerError(f"Could not retrieve crew for {player_name}.")
        warning = "Couldn't refresh your crew from the game; showing the last saved roster."

    async with get_session() as session:
        profile = await _get_profile(session, discord_id, player_name)
        if profile is None:
            profile = PlannerProfile(discord_id=discord_id, player_name=player_name.strip().lower(),
                                     display_name=player_name.strip())
            session.add(profile)
            await session.flush()
        if live is not None:
            await _apply_live_roster(bot, session, profile, live)
            profile.last_synced = _now()
            session.add(profile)
    return SyncResult(profile, warning)


async def _apply_live_roster(bot, session, profile: PlannerProfile,
                             live: Dict[str, prestigehandler.CrewMember]) -> None:
    designs = _design_lookup(bot)
    rows = (await session.exec(select(PlannerCrew).where(PlannerCrew.profile_id == profile.id))).all()
    real_by_cid = {r.crew_id: r for r in rows if not r.virtual and r.crew_id}

    for cid, member in live.items():
        try:
            design_id = int(member.design_id)
        except (TypeError, ValueError):
            continue
        design = designs.get(design_id)
        design_name = design.name if design else member.name
        existing = real_by_cid.get(str(cid))
        if existing:
            existing.level = member.level
            existing.crew_name = member.name
            existing.gone = False
            session.add(existing)
        else:
            session.add(PlannerCrew(
                profile_id=profile.id, crew_id=str(cid), design_id=design_id, design_name=design_name,
                crew_name=member.name, level=member.level, rarity=design.rarity if design else ""))

    live_ids = {str(c) for c in live}
    for cid, row in real_by_cid.items():
        if cid in live_ids:
            continue
        if row.used_in_step is None:
            await session.delete(row)
        else:
            row.gone = True
            session.add(row)
    await session.flush()
    await _reconcile_steps(session, profile)


async def _reconcile_steps(session, profile: PlannerProfile) -> None:
    rows = (await session.exec(select(PlannerCrew).where(PlannerCrew.profile_id == profile.id))).all()
    by_id = {r.id: r for r in rows}
    steps = (await session.exec(select(PlannerStep).where(
        PlannerStep.profile_id == profile.id, PlannerStep.status.in_((STATUS_PLANNED, STATUS_MISSING)))
        .order_by(PlannerStep.step_no))).all()

    for step in steps:
        ingredients = [by_id.get(step.crew1_row_id), by_id.get(step.crew2_row_id)]
        gone = [r for r in ingredients if r is None or (not r.virtual and r.gone)]
        if not gone:
            step.status = STATUS_PLANNED
            session.add(step)
            continue

        step.status = STATUS_MISSING
        if len(gone) == 2 and step.result_row_id is not None:
            rejected = set(filter(None, step.rejected_crew_ids.split(",")))
            candidates = sorted(
                (r for r in rows if not r.virtual and not r.gone and r.used_in_step is None
                 and r.design_id == step.target_design_id and r.id > step.result_row_id
                 and r.crew_id not in rejected),
                key=lambda r: r.id)
            if candidates:
                step.status = STATUS_PENDING
                step.matched_row_id = candidates[0].id
                candidates[0].used_in_step = step.id
                session.add(candidates[0])
        session.add(step)


async def get_issues(profile_id: int) -> List[Issue]:
    async with get_session() as session:
        steps = (await session.exec(select(PlannerStep).where(
            PlannerStep.profile_id == profile_id,
            PlannerStep.status.in_((STATUS_MISSING, STATUS_PENDING))).order_by(PlannerStep.step_no))).all()
        issues = []
        for step in steps:
            matched = await session.get(PlannerCrew, step.matched_row_id) if step.matched_row_id else None
            kind = "match" if step.status == STATUS_PENDING and matched else "missing"
            issues.append(Issue(kind, step, matched, step.crew1_label, step.crew2_label))
        return issues


# ---------------------------------------------------------------------------
# Roster / steps queries
# ---------------------------------------------------------------------------

async def get_roster(profile_id: int) -> Tuple[List[PlannerCrew], Dict[int, int]]:
    """Returns (all rows, step id -> step number)."""
    async with get_session() as session:
        rows = (await session.exec(select(PlannerCrew).where(PlannerCrew.profile_id == profile_id))).all()
        steps = (await session.exec(select(PlannerStep).where(PlannerStep.profile_id == profile_id))).all()
        return list(rows), {s.id: s.step_no for s in steps}


async def get_steps(profile_id: int) -> List[PlannerStep]:
    async with get_session() as session:
        steps = (await session.exec(select(PlannerStep).where(
            PlannerStep.profile_id == profile_id).order_by(PlannerStep.step_no))).all()
        return list(steps)


def available_rows(rows: Sequence[PlannerCrew]) -> List[PlannerCrew]:
    return [r for r in rows if r.used_in_step is None and not r.gone]


def sort_rows(rows: Sequence[PlannerCrew]) -> List[PlannerCrew]:
    return sorted(rows, key=lambda r: (r.design_name.lower(), -(r.level or 0), r.id or 0))


def _partners(row: PlannerCrew, pairs: Sequence[Tuple[int, int]],
              by_design: Dict[int, List[PlannerCrew]]) -> List[PlannerCrew]:
    seen, out = set(), []
    for a, b in pairs:
        for mine, other in ((a, b), (b, a)):
            if mine != row.design_id:
                continue
            for r in by_design.get(other, []):
                if r.id != row.id and r.id not in seen:
                    seen.add(r.id)
                    out.append(r)
    return out


def recipe_pairs(recipes: Sequence[prestigehandler.PrestigeRecipe]) -> List[Tuple[int, int]]:
    return [(int(r.crew1_id), int(r.crew2_id)) for r in recipes]


def first_choices(rows: Sequence[PlannerCrew], pairs: Sequence[Tuple[int, int]]) -> List[PlannerCrew]:
    avail = available_rows(rows)
    by_design = defaultdict(list)
    for r in avail:
        by_design[r.design_id].append(r)
    return sort_rows([r for r in avail if _partners(r, pairs, by_design)])


def second_choices(rows: Sequence[PlannerCrew], pairs: Sequence[Tuple[int, int]],
                   first: PlannerCrew) -> List[PlannerCrew]:
    avail = available_rows(rows)
    by_design = defaultdict(list)
    for r in avail:
        by_design[r.design_id].append(r)
    return sort_rows(_partners(first, pairs, by_design))


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

async def add_step(profile_id: int, target_design_id: int, target_name: str, target_rarity: str,
                   row1_id: int, row2_id: int, pairs: Sequence[Tuple[int, int]]) -> PlannerStep:
    """Validates the pick against current DB state, consumes both crew and adds the virtual result."""
    async with get_session() as session:
        r1 = await session.get(PlannerCrew, row1_id)
        r2 = await session.get(PlannerCrew, row2_id)
        if not r1 or not r2 or r1.id == r2.id or r1.profile_id != profile_id or r2.profile_id != profile_id:
            raise PlannerError("One of the selected crew is no longer in your planner roster.")
        if r1.used_in_step is not None or r2.used_in_step is not None or r1.gone or r2.gone:
            raise PlannerError("One of the selected crew was already used or has left your ship.")
        pair_set = {frozenset(p) if p[0] != p[1] else (p[0],) for p in pairs}
        key = frozenset((r1.design_id, r2.design_id)) if r1.design_id != r2.design_id else (r1.design_id,)
        if key not in pair_set:
            raise PlannerError("Those two crew can't prestige into the target.")

        existing = (await session.exec(select(PlannerStep.step_no).where(
            PlannerStep.profile_id == profile_id))).all()
        step = PlannerStep(profile_id=profile_id, step_no=(max(existing) + 1) if existing else 1,
                           target_design_id=target_design_id, target_name=target_name,
                           crew1_row_id=r1.id, crew2_row_id=r2.id,
                           crew1_label=crew_label(r1), crew2_label=crew_label(r2))
        session.add(step)
        await session.flush()

        result = PlannerCrew(profile_id=profile_id, crew_id=None, design_id=target_design_id,
                             design_name=target_name, crew_name=target_name, level=None,
                             rarity=target_rarity, virtual=True)
        session.add(result)
        await session.flush()

        step.result_row_id = result.id
        r1.used_in_step = step.id
        r2.used_in_step = step.id
        session.add_all([step, r1, r2])
        return step


async def _drop_step(session, step: PlannerStep) -> List[PlannerStep]:
    """Removes an active step (and any later steps consuming its result). Returns every dropped step."""
    dropped = [step]
    if step.result_row_id is not None:
        dependents = (await session.exec(select(PlannerStep).where(
            PlannerStep.profile_id == step.profile_id, PlannerStep.status.in_(ACTIVE_STATUSES),
            PlannerStep.id != step.id))).all()
        for dep in dependents:
            if step.result_row_id in (dep.crew1_row_id, dep.crew2_row_id):
                dropped += await _drop_step(session, dep)

    for row_id in (step.crew1_row_id, step.crew2_row_id, step.matched_row_id):
        row = await session.get(PlannerCrew, row_id) if row_id else None
        if row is None:
            continue
        if row.gone:
            await session.delete(row)
        else:
            row.used_in_step = None
            session.add(row)
    result = await session.get(PlannerCrew, step.result_row_id) if step.result_row_id else None
    if result is not None and result.virtual:
        await session.delete(result)
    await session.delete(step)
    await session.flush()
    return dropped


async def drop_step(profile_id: int, step_id: int) -> List[PlannerStep]:
    async with get_session() as session:
        step = await session.get(PlannerStep, step_id)
        if not step or step.profile_id != profile_id or step.status not in ACTIVE_STATUSES:
            raise PlannerError("That step no longer exists.")
        return await _drop_step(session, step)


async def undo_last_step(profile_id: int) -> List[PlannerStep]:
    async with get_session() as session:
        steps = (await session.exec(select(PlannerStep).where(
            PlannerStep.profile_id == profile_id).order_by(PlannerStep.step_no.desc()).limit(1))).all()
        if not steps:
            raise PlannerError("There are no planned steps to undo.")
        if steps[0].status not in ACTIVE_STATUSES:
            raise PlannerError("The most recent step is already completed in-game and can't be undone.")
        return await _drop_step(session, steps[0])


async def confirm_match(profile_id: int, step_id: int) -> PlannerStep:
    """The player did the prestige: consume the ingredients and swap the virtual result for the real crew."""
    async with get_session() as session:
        step = await session.get(PlannerStep, step_id)
        if not step or step.profile_id != profile_id or step.status != STATUS_PENDING:
            raise PlannerError("That prestige is no longer awaiting confirmation.")
        matched = await session.get(PlannerCrew, step.matched_row_id) if step.matched_row_id else None
        if matched is None:
            raise PlannerError("The detected crew is no longer on your ship.")
        virtual = await session.get(PlannerCrew, step.result_row_id) if step.result_row_id else None

        for row_id in (step.crew1_row_id, step.crew2_row_id):
            row = await session.get(PlannerCrew, row_id) if row_id else None
            if row is not None:
                await session.delete(row)

        matched.used_in_step = virtual.used_in_step if virtual else None
        session.add(matched)
        if virtual is not None:
            later = (await session.exec(select(PlannerStep).where(
                PlannerStep.profile_id == profile_id, PlannerStep.id != step.id))).all()
            for other in later:
                if other.crew1_row_id == virtual.id:
                    other.crew1_row_id = matched.id
                if other.crew2_row_id == virtual.id:
                    other.crew2_row_id = matched.id
                session.add(other)
            await session.delete(virtual)

        step.status = STATUS_DONE
        step.result_row_id = matched.id
        step.matched_row_id = None
        step.crew1_row_id = None
        step.crew2_row_id = None
        session.add(step)
        return step


async def reject_match(profile_id: int, step_id: int) -> None:
    async with get_session() as session:
        step = await session.get(PlannerStep, step_id)
        if not step or step.profile_id != profile_id or step.status != STATUS_PENDING:
            return
        matched = await session.get(PlannerCrew, step.matched_row_id) if step.matched_row_id else None
        if matched is not None:
            matched.used_in_step = None
            rejected = [c for c in step.rejected_crew_ids.split(",") if c]
            if matched.crew_id:
                rejected.append(matched.crew_id)
            step.rejected_crew_ids = ",".join(rejected)
            session.add(matched)
        step.status = STATUS_MISSING
        step.matched_row_id = None
        session.add(step)


async def reset_profile(profile_id: int) -> None:
    async with get_session() as session:
        for model in (PlannerStep, PlannerCrew):
            for row in (await session.exec(select(model).where(model.profile_id == profile_id))).all():
                await session.delete(row)
        profile = await session.get(PlannerProfile, profile_id)
        if profile:
            profile.last_synced = None
            session.add(profile)
