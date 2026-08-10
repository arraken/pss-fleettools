import asyncio
import uuid
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

import aiohttp
import pssapi
from discord.app_commands.errors import CommandInvokeError
from pssapi import PssApiClient
from pssapi.entities.character import Character as _Characters
from pssapi.utils.exceptions import PssApiError
from pssapi.exc import PusherConnectionClosed
from fuzzywuzzy import fuzz

from data.constants.galaxy import STAR_SYSTEMS as STAR_SYSTEM_IDS
from handlers import errorhandlers
from private.bot_token import CHECKSUM_KEY, PUBLIC_TOKEN

if TYPE_CHECKING:
    from pssapi.entities.raw import EngagementRaw
    from classes.bot import FleetToolsBot

_FUZZY_MATCH_THRESHOLD = 80

DEV_USER_ID = 210545386580869121
DEBUG_CHANNEL = 1400497125850349759

class ApiManager:
    def __init__(self, bot: "FleetToolsBot"):
        self.bot = bot

        self.api_call_counter = 0
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._http_session_lock = asyncio.Lock()

        self.__client = PssApiClient()
        self.__access_token: Optional[str] = None
        self.__access_token_age: Optional[datetime] = None
        self.__token_lock = asyncio.Lock()
        self.__token_max_age = timedelta(minutes=4) # Slightly faster than engagement pulses to ensure it's always accurate
        self.__uuid_token: Optional[str] = None
        self.__max_call_retries = 3
        self.__retry_interval_step = 1
        self.__token_refresh_in_progress = False  # Flag to suppress duplicate error logs
        self._market_watch_task: Optional[asyncio.Task] = None
        self._market_flush_task: Optional[asyncio.Task] = None
        self._market_message_queue: List[str] = []
        self._market_watch_channels: Dict[int, int] = {}  # guild_id -> channel_id
        self.pusher = pssapi.pusher.Pusher

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def client(self) -> PssApiClient:
        return self.__client

    # ------------------------------------------------------------------
    # UUID / Token management
    # ------------------------------------------------------------------

    async def load_or_generate_uuid_token(self) -> Optional[str]:
        try:
            import private.bot_token as bot_token
            existing = getattr(bot_token, "UUID", None)
            if existing and existing.strip():
                return existing
        except Exception as e:
            self.bot.logger.fatal(f"Error loading UUID from bot_token.py: {e}")

        # Only generate a new UUID if none exists
        new_uuid = str(uuid.uuid4())
        try:
            import private.bot_token as bot_token
            with open(bot_token.__file__, 'a', encoding="utf-8") as f:
                f.write(f"\nUUID = '{new_uuid}'\n")
            self.bot.logger.info(f"Generated and added new UUID to bot_token.py: [{new_uuid}]")
        except Exception as e:
            self.bot.logger.fatal(f"Failed to write UUID to bot_token.py: {e}")

        return new_uuid

    async def generate_pss_access_token(self) -> Optional[str]:
        if self.__uuid_token is None:
            self.__uuid_token = await self.load_or_generate_uuid_token()
            if self.__uuid_token is None:
                self.bot.logger.fatal("UUID Token Generation Failed")
                return None

        device_key = self.__uuid_token
        client_date_time = pssapi.utils.get_utc_now()
        checksum = self.client.user_service.utils.create_device_login_checksum(
            device_key, self.client.device_type, client_date_time, CHECKSUM_KEY
        )
        user_login = await self.client.user_service.device_login_17(
            checksum, client_date_time, device_key, self.client.device_type, self.client.language_key
        )
        if not isinstance(user_login, pssapi.entities.UserLogin) or not user_login.access_token:
            self.bot.logger.critical(
                "Failed to get valid user login or access token from PSS API during token generation.")
            return None

        self.bot.logger.info(f"Successfully generated new PSS access token. {user_login.access_token}")
        return user_login.access_token

    async def ensure_valid_token_age(self) -> None:
        # Hard timeout: a hanging network call inside the lock must never stall
        # the event loop for more than 30 seconds.
        try:
            await asyncio.wait_for(self._ensure_valid_token_age_inner(), timeout=30.0)
        except asyncio.TimeoutError:
            self.__token_refresh_in_progress = False
            self.bot.logger.fatal("ensure_valid_token_age timed out after 30 s — token refresh aborted.")
            raise PssApiError("Token refresh timed out.")

    async def _ensure_valid_token_age_inner(self) -> None:
        async with self.__token_lock:
            now = datetime.now(timezone.utc)

            if self.__access_token and self.__access_token_age:
                age_seconds = (now - self.__access_token_age).total_seconds()
                if 0 <= age_seconds < self.__token_max_age.total_seconds():
                    return

            self.__token_refresh_in_progress = True
            max_retry = 2

            for attempt in range(1, max_retry + 1):
                new_token = await self.generate_pss_access_token()
                self.bot.logger.info(f"Token refresh attempt {attempt}/{max_retry} completed.")

                if new_token:
                    self.__access_token = new_token
                    self.__access_token_age = datetime.now(timezone.utc)
                    self.__token_refresh_in_progress = False
                    return

            self.__token_refresh_in_progress = False
            self.bot.logger.fatal(f"Token generation failed after {max_retry} attempts")
            raise PssApiError(f"Failed to generate access token after {max_retry} attempts.")

    def get_uuid(self) -> Optional[str]:
        return self.__uuid_token

    async def get_token(self) -> Optional[str]:
        async with self.__token_lock:
            return self.__access_token

    # Market stuff

    async def market_watch_api(self):
        """Single Pusher run — blocks until the connection closes or errors."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        await self.ensure_valid_token_age()
        async with self.__token_lock:
            access_token = self.__access_token
        device_user_id = self.__device_user_id

        if not access_token or not device_user_id:
            raise RuntimeError(
                "Cannot start market watch: no valid device access token or device user ID. "
                "Token generation may have failed."
            )

        # Reset class-level Pusher state from any previous run.
        if self.pusher._scheduler.running:
            self.pusher._scheduler.shutdown(wait=False)
        self.pusher._scheduler = AsyncIOScheduler()

        if self.pusher._connection is not None:
            try:
                await self.pusher._connection.close()
            except Exception:
                pass
        self.pusher._connection = None
        self.pusher._socket_id = ""
        self.pusher._channels.clear()

        market_channel = pssapi.pusher.Channel(pssapi.enums.PusherChannelType.MARKET)
        market_channel.on_message(self.process_market_data)
        self.pusher.add(market_channel)

        await self.pusher.run(access_token, device_user_id)

    async def _notify_market_watch_dropped(self, reason: str) -> None:
        """Send a Discord alert to DEBUG_CHANNEL mentioning the dev."""
        try:
            channel = await self.bot.retrieve_channel(DEBUG_CHANNEL)
            if channel:
                await channel.send(
                    f"<@{DEV_USER_ID}> ⚠️ **Market Watch** connection dropped — reconnecting automatically.\n"
                    f"```{reason}```"
                )
        except Exception as e:
            self.bot.logger.error(f"[MarketWatch] Failed to send drop notification: {e}")

    async def _market_watch_loop(self) -> None:
        """Infinite reconnect loop. Runs as a background task for the bot's lifetime."""
        while True:
            try:
                await self.market_watch_api()
                # market_watch_api() only returns without exception on a clean exit.
                self.bot.logger.warning("[MarketWatch] Connection exited cleanly. Reconnecting in 10 s…")
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return
            except PusherConnectionClosed as e:
                self.bot.logger.warning(f"[MarketWatch] Connection closed: {e}. Reconnecting in 10 s…")
                await self._notify_market_watch_dropped(str(e))
                await asyncio.sleep(10)
            except Exception as e:
                self.bot.logger.error(f"[MarketWatch] Unexpected error: {e}", exc_info=e)
                await self._notify_market_watch_dropped(str(e))
                await asyncio.sleep(30)

    def is_market_watch_running(self) -> bool:
        return bool(self._market_watch_task) and not self._market_watch_task.done()

    def start_market_watch(self) -> None:
        """Create (or restart) the market watch background task."""
        if self._market_watch_task and not self._market_watch_task.done():
            self._market_watch_task.cancel()
        if self._market_flush_task and not self._market_flush_task.done():
            self._market_flush_task.cancel()
        self._market_message_queue.clear()
        self._market_watch_task = asyncio.ensure_future(self._market_watch_loop())
        self._market_flush_task = asyncio.ensure_future(self._market_flush_loop())

    def stop_market_watch(self) -> None:
        """Cancel the market watch background tasks entirely."""
        if self._market_watch_task and not self._market_watch_task.done():
            self._market_watch_task.cancel()
        if self._market_flush_task and not self._market_flush_task.done():
            self._market_flush_task.cancel()
        self._market_watch_task = None
        self._market_flush_task = None
        self._market_message_queue.clear()

    def load_market_watch_channels(self, channels: Dict[int, int]) -> None:
        """Bulk-load guild_id -> channel_id mappings (used on startup from DB)."""
        self._market_watch_channels.update(channels)

    def set_market_watch_channel(self, guild_id: int, channel_id: int) -> None:
        """Register/update the market watch output channel for a guild, starting the pusher if it isn't already running."""
        self._market_watch_channels[guild_id] = channel_id
        if not self.is_market_watch_running():
            self.start_market_watch()

    def clear_market_watch_channel(self, guild_id: int) -> bool:
        """Remove a guild's market watch channel. Stops the pusher entirely once no channels remain."""
        existed = self._market_watch_channels.pop(guild_id, None) is not None
        if not self._market_watch_channels:
            self.stop_market_watch()
        return existed

    # PSS sends short message texts; the actor is always in user_name.
    # SOLD:    message = "Bought {qty} {item} from {seller}"   user_name = buyer
    # LISTED:  message = "Selling {qty} {item}"                user_name = seller
    # EXPIRED: message = "Expired {qty} {item}" (or similar)  user_name = seller
    # Price/currency live in the argument field as "price||currency" or similar.
    _MARKET_SOLD_RE = re.compile(
        r"^Bought (?P<qty>\d+) (?P<item>.+?) from (?P<seller>.+?)$"
    )
    _MARKET_LISTED_RE = re.compile(
        r"^Selling (?P<qty>\d+) (?P<item>.+?)$"
    )
    _MARKET_EXPIRED_RE = re.compile(
        r"^Expired (?P<qty>\d+) (?P<item>.+?)$"
    )

    @staticmethod
    def _parse_market_price(argument: str) -> str:
        """
        Try to extract a human-readable price string from the argument field.
        PSS typically encodes it as "price||currency" or "price|currency".
        Returns an empty string if nothing useful is found.
        """
        if not argument:
            return ""
        sep = "||" if "||" in argument else "|"
        parts = [p.strip() for p in argument.split(sep) if p.strip()]
        # Expect at least [price, currency]; may have more fields before/after.
        # Find the first part that looks like a number (the price).
        for i, part in enumerate(parts):
            if part.isdigit() and i + 1 < len(parts):
                return f" for {part} {parts[i + 1]}"
        return ""

    def process_market_data(self, data: Dict):
        message = pssapi.entities.Message(data)

        formatted = self._format_market_message(message)
        # Buffer the message; the flush loop sends batches to avoid rate limits.
        self._market_message_queue.append(formatted)

    async def _flush_market_queue(self) -> None:
        """Drain the queue and send all pending lines as one or more batched Discord messages to every configured channel."""
        if not self._market_message_queue:
            return

        if not self._market_watch_channels:
            self._market_message_queue.clear()
            return

        # Snapshot and clear atomically so new messages keep arriving cleanly.
        pending = self._market_message_queue.copy()
        self._market_message_queue.clear()

        # Pack lines into ≤ 1990-char chunks (Discord limit is 2000).
        batches: List[str] = []
        batch: List[str] = []
        batch_len = 0
        for line in pending:
            # +1 for the joining newline
            if batch and batch_len + len(line) + 1 > 1990:
                batches.append("\n".join(batch))
                batch = []
                batch_len = 0
            batch.append(line)
            batch_len += len(line) + 1
        if batch:
            batches.append("\n".join(batch))

        # De-duplicate target channel IDs in case multiple guilds share one channel.
        for channel_id in set(self._market_watch_channels.values()):
            try:
                channel = await self.bot.retrieve_channel(channel_id)
                if not channel:
                    self.bot.logger.error(f"[MarketWatch] Channel {channel_id} not found.")
                    continue
                for message in batches:
                    await channel.send(message)
            except Exception as e:
                self.bot.logger.error(f"[MarketWatch] Failed to flush message queue to channel {channel_id}: {e}")

    async def _market_flush_loop(self) -> None:
        """Periodically flush buffered market messages to Discord (every 4 s)."""
        while True:
            try:
                await asyncio.sleep(4)
                await self._flush_market_queue()
            except asyncio.CancelledError:
                # Final flush before exit so no messages are lost.
                await self._flush_market_queue()
                return
            except Exception as e:
                self.bot.logger.error(f"[MarketWatch] Flush loop error: {e}")

    def _format_market_message(self, message) -> str:
        """Return a Discord-ready market event string with ship names in backticks."""
        ts = message.message_date
        if ts:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        else:
            ts_str = "??-??-?? ??:??:??"

        raw_text = (message.message or "").strip()
        actor = message.user_name or "Unknown"
        activity = message.activity_type_enum
        price_str = self._parse_market_price(message.argument)

        if activity == pssapi.enums.ActivityType.MARKET_SOLD:
            m = self._MARKET_SOLD_RE.match(raw_text)
            if m:
                return (
                    f"{ts_str} - `{actor}` bought {m['qty']} {m['item']}"
                    f" from `{m['seller']}`{price_str}"
                )

        elif activity == pssapi.enums.ActivityType.MARKET_LISTED:
            m = self._MARKET_LISTED_RE.match(raw_text)
            if m:
                return (
                    f"{ts_str} - `{actor}` is selling {m['qty']} {m['item']}{price_str}"
                )

        elif activity == pssapi.enums.ActivityType.MARKET_EXPIRED:
            m = self._MARKET_EXPIRED_RE.match(raw_text)
            if m:
                return (
                    f"{ts_str} - `{actor}`'s listing of {m['qty']} {m['item']} expired{price_str}"
                )

        # Fallback: actor + raw PSS text (regex didn't match — check DEBUG log).
        return f"{ts_str} - `{actor}` {raw_text}" if raw_text else f"{ts_str} - (no text, activity={message.activity_type})"


    # ------------------------------------------------------------------
    # PSS API wrappers
    # ------------------------------------------------------------------

    async def get_engagement(self, engagement_id: int):
        client_date_time = pssapi.utils.get_utc_now()
        checksum = self.client.battle_service.utils.create_get_engagement_checksum(client_date_time, CHECKSUM_KEY)

        async def _call():
            access_token = await self.get_token()
            return await self.client.battle_service.get_engagement(access_token, checksum, client_date_time, engagement_id)

        return await self._make_api_call(_call, allow_token_refresh=False, max_retries=0)

    async def get_galaxy_data(self, system_id: int):
        production_server = await self.client.get_production_server()

        async def _call():
            token = await self.get_token()
            return await pssapi.services.raw.galaxy_service_raw.get_star_system_details(production_server, token, system_id)

        return await self._make_api_call(_call)

    async def get_user_by_name(self, name: str):
        return await self._make_api_call(self.client.user_service.search_users, name)

    async def get_crew_by_name(self, crew_name: str):
        crew_list = await self._make_api_call(self.client.character_service.list_all_character_designs)

        best_match = None
        best_score = 0
        crew_name_lower = crew_name.lower()
        query_len = len(crew_name_lower)

        for crew in crew_list:
            candidate = crew.character_design_name.lower()
            candidate_len = len(candidate)

            # WRatio gives a balanced score across multiple fuzzy strategies
            base_score = fuzz.WRatio(crew_name_lower, candidate)

            # Apply a length-ratio penalty: a candidate much shorter than the
            # query gets penalised so "Eric" can't beat "Server Eric" when the
            # user typed "Server Eric".
            if candidate_len < query_len:
                length_ratio = candidate_len / query_len
                score = base_score * length_ratio
            else:
                score = base_score

            if score > best_score:
                best_score = score
                best_match = crew

        return best_match if best_score >= _FUZZY_MATCH_THRESHOLD else None

    async def get_ship_characters_by_user_name(self, player_name: str):
        _PATH: str = "PublicService/GetShipCharactersByUsername"
        params = {"username": player_name, "accessToken": PUBLIC_TOKEN}
        production_server = await self.client.get_production_server()
        return await pssapi.core.get_entities_from_path(
            ((_Characters, "Characters", False),),
            "GetShipCharactersByUsername",
            production_server,
            _PATH,
            "GET",
            response_gzipped=False,
            **params,
        )

    async def get_all_crew(self):
        return await self._make_api_call(self.client.character_service.list_all_character_designs)

    async def prestige_from(self, crew_id: int):
        return await self._make_api_call(self.client.character_service.prestige_character_from, crew_id)

    async def prestige_to(self, crew_id: int):
        return await self._make_api_call(self.client.character_service.prestige_character_to, crew_id)
    # ------------------------------------------------------------------
    # Core API call machinery
    # ------------------------------------------------------------------

    async def _make_api_call(self, func, *args, max_retries: Optional[int] = None, allow_token_refresh: bool = True,
                             **kwargs):
        retry_limit = max_retries if max_retries is not None else self.__max_call_retries
        attempt = 0

        while attempt <= retry_limit:
            attempt += 1

            # Ensure the stored token is fresh; refresh under lock if needed.
            await self.ensure_valid_token_age()

            try:
                # Hard per-call timeout: prevent any single API call from hanging
                # the event loop indefinitely.
                result = await asyncio.wait_for(func(*args, **kwargs), timeout=60.0)
                return result

            except asyncio.TimeoutError:
                if attempt > retry_limit:
                    self.bot.logger.error(
                        f"API call timed out after 60 s (attempt {attempt}/{retry_limit}), giving up.")
                    raise PssApiError("API call timed out.")
                self.bot.logger.warning(
                    f"API call timed out after 60 s (attempt {attempt}/{retry_limit}), retrying...")
                continue

            except Exception as error:
                actual_error = error.original if isinstance(error, CommandInvokeError) else error

                if errorhandlers.is_pssapi_rate_limit_error(actual_error):
                    if attempt > retry_limit:
                        raise
                    self.bot.logger.info(f"Rate limit hit, waiting 10 seconds before retry (attempt {attempt})")
                    await asyncio.sleep(10)
                    continue

                is_token_error = errorhandlers.is_pssapi_token_error(actual_error)

                if is_token_error and allow_token_refresh:
                    if attempt > retry_limit:
                        raise

                    if not self.__token_refresh_in_progress:
                        self.bot.logger.fatal(
                            f"Unexpected token error with fresh token, regenerating "
                            f"(attempt {attempt}/{retry_limit})",
                            exc_info=actual_error,
                        )

                    # Invalidate the token; the next loop iteration will refresh it.
                    async with self.__token_lock:
                        self.__access_token = None
                        self.__access_token_age = None

                    await asyncio.sleep(0.5)
                    continue

                if is_token_error and not allow_token_refresh:
                    raise

                if attempt <= retry_limit:
                    retry_wait = self.__retry_interval_step * (2 ** (attempt - 1))
                    self.bot.logger.error(
                        f"Attempt [{attempt}] to make API call failed, retrying after [{retry_wait}]s")
                    await asyncio.sleep(retry_wait)
                    continue
                else:
                    raise

        raise RuntimeError("Unreachable code reached in _make_api_call")