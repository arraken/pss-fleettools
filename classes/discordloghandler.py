import asyncio
import io
import logging
from typing import Optional

import discord

MAX_INLINE = 1900


class DiscordLogHandler(logging.Handler):
    """Forwards log records to a Discord channel. Long records (e.g. tracebacks) are sent in full as a file."""

    def __init__(self, bot: discord.Client, channel_id: int, level: int = logging.WARNING):
        super().__init__(level)
        self.bot = bot
        self.channel_id = channel_id
        self._queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=500)
        self._task: Optional[asyncio.Task] = None
        self._emitting = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._worker())

    def emit(self, record: logging.LogRecord) -> None:
        # Ignore discord.py/http chatter to avoid feedback loops while sending.
        if record.name.startswith(("discord.http", "discord.gateway")) or self._emitting:
            return
        try:
            text = self.format(record)
            self._queue.put_nowait(text)
        except Exception:
            pass

    async def _worker(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            text = await self._queue.get()
            self._emitting = True
            try:
                channel = self.bot.get_channel(self.channel_id) or await self.bot.fetch_channel(self.channel_id)
                if len(text) <= MAX_INLINE:
                    await channel.send(f"```\n{text}\n```")
                else:
                    file = discord.File(io.BytesIO(text.encode("utf-8")), filename="log.txt")
                    await channel.send(f"```\n{text[:MAX_INLINE]}\n```*(truncated, full log attached)*", file=file)
            except Exception:
                pass
            finally:
                self._emitting = False
            await asyncio.sleep(1)
