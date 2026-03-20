import logging
from typing import Optional

import discord
from discord.ext import commands



class FleetWarsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.logger = logging.getLogger("FleetWarsBot")
        # These are set during setup_hook after the engine is ready
        self.api_manager = None
        self.cache_manager = None

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

        await self.cache_manager.load_active_engagements_from_db()
        await self.cache_manager.load_galaxy_systems_from_db()

        await self.add_cog(TimerMonitor(self))
        await self.add_cog(Commands(self))

        await self.tree.sync()
        self.logger.info("Command tree synced.")

    async def on_ready(self) -> None:
        self.logger.info(f"Logged in as {self.user} ({self.user.id})")

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

