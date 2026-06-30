import asyncio

from pyrogram import idle

from bot import Bot
from config import LOGGER

logger = LOGGER(__name__)

try:
    from auto_repost import AutoRepostWorker
except Exception as exc:
    AutoRepostWorker = None
    logger.warning("Auto repost worker unavailable: %s", exc)


async def main():
    bot = Bot()
    worker = AutoRepostWorker(bot) if AutoRepostWorker else None
    await bot.start()
    if worker:
        await worker.start()
    try:
        await idle()
    finally:
        if worker:
            await worker.stop()
        await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
