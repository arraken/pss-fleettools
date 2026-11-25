"""
Handler for the /graph command.

A background task samples the PSS fleet ranking every 5 minutes during the tournament
(the last 7 days of the month, ending 00:00:00 UTC on the 1st) and stores each fleet's
stars in the database. /graph plots that stored data: live during the tournament and the
whole of the most recent tournament otherwise. Graphs are cached per sample, so requests
within the same 5-minute window return the same image.
"""

import asyncio
import io
import os
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")
from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import DateFormatter, date2num
from matplotlib.figure import Figure
from sqlmodel import col, delete, select

from database.models import TournamentSnapshot
from handlers.databasehandler import get_session

if TYPE_CHECKING:
    from classes.bot import FleetToolsBot

TOURNAMENT_LENGTH = timedelta(days=7)
RANKING_TAKE = 200
MAX_FLEETS = 20
_CJK_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"
_COLORS = ["red", "green", "blue", "black", "purple", "orange", "magenta", "brown", "gray", "cyan", "orangered", "peru",
           "gold", "navy", "lime", "deeppink", "teal", "olive", "slateblue", "darkred"]

_PERIOD_UNITS = {
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
}
_PERIOD_ALIASES = {
    "m": "minute", "min": "minute", "mins": "minute", "minute": "minute", "minutes": "minute",
    "h": "hour", "hr": "hour", "hrs": "hour", "hour": "hour", "hours": "hour",
    "d": "day", "day": "day", "days": "day",
    "w": "week", "wk": "week", "wks": "week", "week": "week", "weeks": "week",
    "mo": "month", "mos": "month", "month": "month", "months": "month",
}
_PERIOD_RE = re.compile(r"(\d+)\s*(" + "|".join(sorted(_PERIOD_ALIASES, key=len, reverse=True)) + r")(?![a-z])", re.IGNORECASE)
_PLACES_RE = re.compile(r"^\s*(\d+)\s*(?:-|to)\s*(\d+)\s*$", re.IGNORECASE)

# (division, places, period, latest sample time) -> rendered PNG bytes
_graph_cache: Dict[tuple, bytes] = {}


class GraphInputError(Exception):
    """Raised when the user's command arguments are invalid or no data is available."""


def get_tournament_window(now: datetime) -> Tuple[datetime, datetime, bool]:
    """Returns (start, end, is_live) of the relevant tournament.

    If `now` lies inside the tournament the window is live (`end` is `now`); otherwise
    the most recently finished tournament is returned."""
    now = now.astimezone(timezone.utc)
    next_month = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=timezone.utc)
    if now >= next_month - TOURNAMENT_LENGTH:
        return next_month - TOURNAMENT_LENGTH, now, True

    last_end = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return last_end - TOURNAMENT_LENGTH, last_end, False


def floor_to_sample(moment: datetime) -> datetime:
    moment = moment.astimezone(timezone.utc)
    return moment.replace(minute=moment.minute - moment.minute % 5, second=0, microsecond=0)


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc)


def parse_period(text: str) -> timedelta:
    matches = _PERIOD_RE.findall(text)
    if not matches or _PERIOD_RE.sub("", text).strip(" ,and"):
        raise GraphInputError('Invalid period. Example: "1 day 12 hours". Units: minute, hour, day, week, month.')
    total = sum((_PERIOD_UNITS[_PERIOD_ALIASES[unit.lower()]] * int(amount) for amount, unit in matches), timedelta())
    if total <= timedelta(0):
        raise GraphInputError("Period must be greater than zero.")
    return total


def parse_places(text: str) -> Tuple[int, int]:
    match = _PLACES_RE.match(text)
    if not match:
        raise GraphInputError('Invalid places. Example: "1-5" or "1 to 5".')
    low, high = sorted((int(match.group(1)), int(match.group(2))))
    if low < 1:
        raise GraphInputError("Places start at 1.")
    return low, high


async def record_snapshot(bot: "FleetToolsBot") -> int:
    """Stores the current stars of every ranked fleet. Does nothing outside the tournament.
    Returns the number of rows stored."""
    now = datetime.now(timezone.utc)
    tournament_start, _, is_live = get_tournament_window(now)
    if not is_live:
        return 0

    sample_time = _naive(floor_to_sample(now))
    async with get_session() as session:
        existing = (await session.exec(
            select(TournamentSnapshot.id).where(TournamentSnapshot.timestamp == sample_time).limit(1)
        )).first()
        # Only the latest tournament is kept; older samples are dropped once a new one starts.
        await session.exec(delete(TournamentSnapshot).where(TournamentSnapshot.timestamp < _naive(tournament_start)))
    if existing is not None:
        return 0

    alliances = await bot.api_manager.get_alliances_by_ranking(0, RANKING_TAKE)
    rows = [
        TournamentSnapshot(
            timestamp=sample_time,
            alliance_id=a.alliance_id,
            alliance_name=a.alliance_name or str(a.alliance_id),
            division_design_id=a.division_design_id or 0,
            stars=a.score or 0,
        )
        for a in alliances
    ]
    async with get_session() as session:
        session.add_all(rows)
    return len(rows)


async def _load_snapshots(start: datetime, end: datetime) -> List[TournamentSnapshot]:
    async with get_session() as session:
        result = await session.exec(
            select(TournamentSnapshot)
            .where(TournamentSnapshot.timestamp >= _naive(start), TournamentSnapshot.timestamp <= _naive(end))
            .order_by(col(TournamentSnapshot.timestamp))
        )
        return list(result.all())


def _select_series(snapshots: List[TournamentSnapshot], division_ids: Optional[set],
                   places: Optional[Tuple[int, int]]) -> List[Tuple[str, List[datetime], List[int]]]:
    latest_time = snapshots[-1].timestamp
    standings = [s for s in snapshots if s.timestamp == latest_time
                 and (division_ids is None or s.division_design_id in division_ids)]
    standings.sort(key=lambda s: -s.stars)
    if places:
        standings = standings[places[0] - 1:places[1]]
    standings = standings[:MAX_FLEETS]
    if not standings:
        raise GraphInputError("No fleets found for that division/places.")

    series: Dict[int, Tuple[str, List[datetime], List[int]]] = {
        s.alliance_id: (s.alliance_name, [], []) for s in standings
    }
    for snapshot in snapshots:
        entry = series.get(snapshot.alliance_id)
        if entry:
            entry[1].append(_aware(snapshot.timestamp))
            entry[2].append(snapshot.stars)
    return [series[s.alliance_id] for s in standings]


def _configure_fonts() -> None:
    if os.path.exists(_CJK_FONT_PATH):
        font_manager.fontManager.addfont(_CJK_FONT_PATH)
        family = font_manager.FontProperties(fname=_CJK_FONT_PATH).get_name()
        matplotlib.rcParams["font.family"] = [family, "DejaVu Sans"]
    else:
        matplotlib.rcParams["font.family"] = ["Microsoft YaHei", "SimSun", "DejaVu Sans"]


def render_graph(series_list: List[Tuple[str, List[datetime], List[int]]], start: datetime, end: datetime) -> bytes:
    _configure_fonts()
    figure = Figure(figsize=(12.8, 10), dpi=100)
    FigureCanvasAgg(figure)
    axes = figure.add_axes([0.05, 0.2, 0.72, 0.72])

    for index, (name, times, stars) in enumerate(series_list):
        axes.plot(date2num(times), stars, color=_COLORS[index % len(_COLORS)], linewidth=2, label=name)

    axes.set_xlim(date2num(start), date2num(end))
    axes.set_ylim(bottom=0)
    axes.grid(True, color="#dddddd", linewidth=0.6)
    axes.xaxis.set_major_formatter(DateFormatter("%d.%m.%Y %H:%M"))
    for label in axes.get_xticklabels() + axes.get_yticklabels():
        label.set_fontweight("bold")
    for label in axes.get_xticklabels():
        label.set_rotation(90)
    axes.set_xlabel("PSS Fleet Tools", fontsize=14, fontweight="bold")
    fmt = "%H:%M %d.%m.%Y"
    figure.suptitle(f"Stars for the period {start.strftime(fmt)} - {end.strftime(fmt)} (UTC)",
                    fontsize=14, fontweight="bold", y=0.97)
    axes.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), frameon=False, prop={"weight": "bold", "size": 11})

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png")
    return buffer.getvalue()


async def build_graph(bot: "FleetToolsBot", division: Optional[str], places: Optional[str],
                      period: Optional[str]) -> io.BytesIO:
    """Validates the arguments, loads the stored samples and returns the PNG (cached per sample)."""
    places_range = parse_places(places) if places else None
    if not division and not places_range:
        raise GraphInputError("Specify at least one of division or places.")
    length = parse_period(period) if period else None

    tournament_start, window_end, _ = get_tournament_window(datetime.now(timezone.utc))
    snapshots = await _load_snapshots(tournament_start, window_end)
    if not snapshots:
        raise GraphInputError("No tournament data has been recorded yet.")

    # Anchor on the latest sample (not "now") so every request in a 5-minute window is identical.
    end = _aware(snapshots[-1].timestamp)
    start = max(end - length, tournament_start) if length else tournament_start
    snapshots = [s for s in snapshots if _aware(s.timestamp) >= start]

    cache_key = (division, places_range, length, end)
    cached = _graph_cache.get(cache_key)
    if cached is None:
        division_ids = None
        if division:
            letters = await bot.api_manager.get_division_letter_map()
            division_ids = {did for did, letter in letters.items() if letter.lower() == division.lower()}
        series_list = _select_series(snapshots, division_ids, places_range)
        cached = await asyncio.get_running_loop().run_in_executor(None, render_graph, series_list, start, end)
        for key in [k for k in _graph_cache if k[3] != end]:
            del _graph_cache[key]
        _graph_cache[cache_key] = cached
    return io.BytesIO(cached)
