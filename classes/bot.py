import logging
from typing import Optional

import discord
from discord.ext import commands

class FleetToolsBot(commands.Bot):
    def __init__(self, intents: discord.Intents | None = None):
        if intents is None:
            intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.logger = logging.getLogger("FleetWarsBot")
        # These are set during setup_hook after the engine is ready
        self.api_manager = None
        self.cache_manager = None
        self.timer_monitor = None

    async def setup_hook(self) -> None:
        #from handlers.databasehandler import init_engine
        from classes.apimanager import ApiManager
        from classes.cachemanager import CacheManager
        from classes.commands import Commands
        from cogs.timermonitor import TimerMonitor
        from database import DatabaseManager

        self.database_manager = DatabaseManager(self)
        await self.database_manager.async_init()
        #await init_engine()

        self.api_manager = ApiManager(self)
        self.cache_manager = CacheManager(self)
        timer_monitor = TimerMonitor(self)
        self.timer_monitor = timer_monitor

        await self.cache_manager.load_active_engagements_from_db()
        await self.cache_manager.load_galaxy_systems_from_db()

        await self.add_cog(timer_monitor)
        await self.add_cog(Commands(self))

        await self.tree.sync()
        self.logger.info("Command tree synced.")

    async def on_ready(self) -> None:
        self.logger.info(f"Logged in as {self.user} ({self.user.id})")
        await self._load_market_watch_channels()

    async def _load_market_watch_channels(self) -> None:
        """Load configured market watch channels from the DB and (re)start the pusher if any are set."""
        from handlers import databasehandler
        from handlers.databasehandler import get_session

        try:
            async with get_session() as session:
                rows = await databasehandler.get_all_alert_channels(session, channel_type="marketwatch")
            channels = {row.guild_id: row.channel_id for row in rows}
            self.api_manager.load_market_watch_channels(channels)
            if channels:
                self.api_manager.start_market_watch()
                self.logger.info(f"Market watch auto-started for {len(channels)} guild(s).")
        except Exception as e:
            self.logger.error(f"Failed to load market watch channels: {e}", exc_info=e)

    async def retrieve_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        """Fetch a channel by ID from cache, falling back to an API call."""
        if not channel_id:
            return None
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception:
                return None
        return channel

