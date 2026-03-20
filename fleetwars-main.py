import asyncio
import logging
import os

from private.bot_token import PUBLIC_TOKEN
from classes.bot import FleetToolsBot


def setup_logging() -> None:
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/fleettools.log", encoding="utf-8"),
        ],
    )


async def main() -> None:
    setup_logging()
    bot = FleetToolsBot()
    async with bot:
        await bot.start(PUBLIC_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

