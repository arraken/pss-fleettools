"""
Handler for the /player_history command.

Pulls a player's entire recorded history from the PSS Fleet Data API
(via pss-fleet-data-client) and renders it as a spreadsheet-style image.
"""

import io
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont
from pss_fleet_data import ParameterInterval
from pss_fleet_data.models.client_models import UserHistory
from pss_fleet_data.models.enums import ParameterOnMissing

if TYPE_CHECKING:
    from classes.bot import FleetToolsBot

_MAX_TAKE_PER_PAGE = 100
_HEADERS = ["Year", "Month", "Player Name", "Fleet Name", "Rank", "Division", "Stars", "Trophies"]
_GAP_ROW = ["-"] * (len(_HEADERS) - 2)  # all columns except Year and Month


class PlayerNotFoundError(Exception):
    """Raised when a player name/ID could not be resolved to a PSS user."""


def _prettify_rank(raw_rank: Optional[str]) -> str:
    """Converts an enum-style rank like "FleetAdmiral" into "Fleet Admiral"."""
    if not raw_rank or raw_rank == "None":
        return "-"
    return re.sub(r"(?<!^)(?=[A-Z])", " ", raw_rank)


async def resolve_player(bot: "FleetToolsBot", player_input: str) -> Tuple[int, str]:
    """Resolves a player name or numeric ID to a (user_id, player_name) tuple.

    Args:
        player_input: Either a numeric player ID, or a player name to search for.

    Raises:
        PlayerNotFoundError: If no matching player could be found.
    """
    player_input = player_input.strip()

    if player_input.isdigit():
        # A numeric ID can't be resolved to a name via name-search, so use a
        # placeholder; the actual name gets backfilled from history data once fetched.
        return int(player_input), f"Player {player_input}"

    users = await bot.api_manager.get_user_by_name(player_input)
    if not users:
        raise PlayerNotFoundError(f"No player found matching '{player_input}'.")

    exact_match = next((u for u in users if u.name.lower() == player_input.lower()), None)
    player = exact_match or users[0]
    return player.id, player.name


def get_latest_player_name(history: List[UserHistory], fallback: str) -> str:
    """Returns the most recently recorded name for a player, or `fallback` if unavailable."""
    if not history:
        return fallback
    latest_entry = max(history, key=lambda entry: entry.collection.timestamp)
    return latest_entry.user.name or fallback


async def fetch_full_user_history(bot: "FleetToolsBot", user_id: int) -> List[UserHistory]:
    """Fetches the entire recorded monthly history of a player, paginating as needed."""
    client = bot.api_manager.fleet_data_client
    all_history: List[UserHistory] = []
    skip = 0

    while True:
        page = await client.get_user_history(
            user_id,
            interval=ParameterInterval.MONTHLY,
            desc=True,
            skip=skip,
            take=_MAX_TAKE_PER_PAGE,
            on_missing=ParameterOnMissing.SKIP,
        )
        all_history.extend(page)
        if len(page) < _MAX_TAKE_PER_PAGE:
            break
        skip += _MAX_TAKE_PER_PAGE

    return all_history


def _month_key(year: int, month: int) -> int:
    return year * 12 + month


async def build_history_rows(bot: "FleetToolsBot", player_name: str, history: List[UserHistory]) -> List[List[str]]:
    """Builds full table rows (newest to oldest) for every month between the first and last
    recorded entry, filling any gap month with "-" placeholders."""
    if not history:
        return []

    division_letters = await bot.api_manager.get_division_letter_map()

    entries_by_month: Dict[int, UserHistory] = {}
    for entry in history:
        ts = entry.collection.timestamp
        entries_by_month[_month_key(ts.year, ts.month)] = entry

    latest_key = max(entries_by_month)
    earliest_key = min(entries_by_month)

    rows: List[List[str]] = []
    key = latest_key
    while key >= earliest_key:
        year, month = divmod(key, 12)
        if month == 0:
            year -= 1
            month = 12

        entry = entries_by_month.get(key)
        if entry is None:
            rows.append([str(year), str(month), *_GAP_ROW])
        else:
            user = entry.user
            alliance = entry.alliance
            fleet_name = alliance.alliance_name if alliance else "-"
            division = division_letters.get(alliance.division_design_id, "-") if alliance else "-"
            rank = _prettify_rank(getattr(user, "alliance_membership_enum", None) and user.alliance_membership_enum.value)
            stars = user.alliance_score if user.alliance_score is not None else "-"
            trophies = user.trophy if user.trophy is not None else "-"
            rows.append([
                str(year),
                str(month),
                user.name or player_name,
                fleet_name or "-",
                rank,
                division or "-",
                str(stars),
                str(trophies),
            ])
        key -= 1

    return rows


def render_history_image(player_name: str, rows: List[List[str]]) -> io.BytesIO:
    """Renders the player's history rows as a spreadsheet-style PNG image."""
    font_size = 18
    padding = 10
    row_height = font_size + 2 * padding
    title_height = font_size + 4 * padding

    try:
        font = ImageFont.truetype("arial.ttf", font_size)
        font_bold = ImageFont.truetype("arialbd.ttf", font_size)
    except OSError:
        font = ImageFont.load_default(size=font_size)
        font_bold = font

    measuring_image = Image.new("RGB", (1, 1))
    measuring_draw = ImageDraw.Draw(measuring_image)

    def text_width(text: str, use_font) -> int:
        return measuring_draw.textbbox((0, 0), text, font=use_font)[2]

    col_widths = []
    for col_idx, header in enumerate(_HEADERS):
        widest = text_width(header, font_bold)
        for row in rows:
            widest = max(widest, text_width(row[col_idx], font))
        col_widths.append(widest + 2 * padding)

    table_width = sum(col_widths)
    table_height = title_height + row_height * (len(rows) + 1)

    image = Image.new("RGB", (table_width, table_height), color="white")
    draw = ImageDraw.Draw(image)

    draw.rectangle([(0, 0), (table_width - 1, title_height - 1)], fill=(30, 30, 60))
    draw.text((padding, padding), f"Player History: {player_name}", font=font_bold, fill="white")

    header_y = title_height
    draw.rectangle([(0, header_y), (table_width - 1, header_y + row_height - 1)], fill=(60, 60, 110))
    x = 0
    for col_idx, header in enumerate(_HEADERS):
        draw.text((x + padding, header_y + padding), header, font=font_bold, fill="white")
        x += col_widths[col_idx]

    for row_idx, row in enumerate(rows):
        y = header_y + row_height * (row_idx + 1)
        fill = (245, 245, 245) if row_idx % 2 == 0 else (225, 225, 235)
        draw.rectangle([(0, y), (table_width - 1, y + row_height - 1)], fill=fill)
        x = 0
        for col_idx, value in enumerate(row):
            draw.text((x + padding, y + padding), value, font=font, fill=(20, 20, 20))
            x += col_widths[col_idx]

    x = 0
    for width in col_widths[:-1]:
        x += width
        draw.line([(x, 0), (x, table_height - 1)], fill=(200, 200, 200))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer
