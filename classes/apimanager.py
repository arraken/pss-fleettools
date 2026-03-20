import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict as _Dict, List, Optional

import aiohttp
import pssapi
from discord.app_commands.errors import CommandInvokeError
from pssapi import PssApiClient
from pssapi.utils.exceptions import PssApiError

from data.constants.galaxy import STAR_SYSTEMS as STAR_SYSTEM_IDS
from handlers import errorhandlers
from private.bot_token import CHECKSUM_KEY, PUBLIC_TOKEN

if TYPE_CHECKING:
    from pssapi.entities.raw import EngagementRaw
    from classes.bot import FleetWarsBot


class ApiManager:
    def __init__(self, bot: "FleetWarsBot"):
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

    # ------------------------------------------------------------------
    # PSS API wrappers
    # ------------------------------------------------------------------

    async def get_engagement(self, engagement_id: int) -> "EngagementRaw":
        pass # TODO pull from MA

    async def get_galaxy_data(self, system_id: int):
        pass # TODO pull from MA
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