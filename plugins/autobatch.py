import asyncio

from pyrogram import StopPropagation, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait

from bot import Bot
from config import ADMINS
from helper_func import encode


AUTO_BATCH_WAIT = 60
AUTO_BATCH_STATE = {}
AUTO_BATCH_TASKS = {}
AUTO_BATCH_LOCK = asyncio.Lock()


def is_autobatch_collecting(user_id: int) -> bool:
    state = AUTO_BATCH_STATE.get(user_id) or {}
    return bool(state.get("collecting"))


async def _ensure_runtime_attrs(client):
    if not getattr(client, "db_channel", None):
        raise RuntimeError("DB channel is not ready yet. Try again in a few seconds.")
    if not getattr(client, "username", None):
        me = await client.get_me()
        client.username = me.username


async def _finish_auto_batch(client, user_id: int):
    try:
        await asyncio.sleep(AUTO_BATCH_WAIT)
    except asyncio.CancelledError:
        return

    state = AUTO_BATCH_STATE.pop(user_id, None)
    AUTO_BATCH_TASKS.pop(user_id, None)
    if not state or not state.get("ids"):
        return

    ids = state["ids"]
    reply_msg = state["reply_msg"]
    first_id = min(ids)
    last_id = max(ids)
    payload = await encode(f"get-{first_id}-{last_id}")
    link = f"https://t.me/{client.username}?start={payload}"

    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Share URL", url=f"https://telegram.me/share/url?url={link}")]]
    )
    text = (
        "Your auto batch link is ready.\n\n"
        f"{link}\n\n"
        f"Total Files: {len(ids)}"
    )
    try:
        await reply_msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        await client.send_message(user_id, text, reply_markup=markup, disable_web_page_preview=True)


@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("autobatch"), group=-40)
async def start_autobatch(client, message):
    await _ensure_runtime_attrs(client)
    user_id = message.from_user.id
    async with AUTO_BATCH_LOCK:
        old = AUTO_BATCH_TASKS.pop(user_id, None)
        if old:
            old.cancel()
        AUTO_BATCH_STATE[user_id] = {
            "collecting": True,
            "ids": [],
            "reply_msg": await message.reply_text(
                "Auto batch started.\n\nSend all files/videos/documents now. I will create one batch link after 60 seconds of no new file.",
                quote=True,
            ),
        }


@Bot.on_message(filters.private & filters.user(ADMINS) & (filters.document | filters.video | filters.audio | filters.animation | filters.photo), group=-30)
async def collect_autobatch_media(client, message):
    user_id = message.from_user.id
    if not is_autobatch_collecting(user_id):
        return
    await _ensure_runtime_attrs(client)
    try:
        post_message = await message.copy(chat_id=client.db_channel.id, disable_notification=True)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        post_message = await message.copy(chat_id=client.db_channel.id, disable_notification=True)
    except Exception:
        return

    converted_id = post_message.id * abs(client.db_channel.id)
    AUTO_BATCH_STATE[user_id]["ids"].append(converted_id)
    old = AUTO_BATCH_TASKS.get(user_id)
    if old:
        old.cancel()
    AUTO_BATCH_TASKS[user_id] = asyncio.create_task(_finish_auto_batch(client, user_id))
    raise StopPropagation
