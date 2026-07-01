#(©)Codexbotz

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from bot import Bot
from config import ADMINS
from helper_func import encode, get_message_id


PENDING_LINK_INPUT = {}


async def _ensure_runtime_attrs(client):
    if not getattr(client, "db_channel", None):
        raise RuntimeError("DB channel is not ready yet. Try again in a few seconds.")
    if not getattr(client, "username", None):
        me = await client.get_me()
        client.username = me.username


def _share_markup(link):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Share URL", url=f"https://telegram.me/share/url?url={link}")]]
    )


@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("batch"), group=-50)
async def batch(client: Client, message: Message):
    await _ensure_runtime_attrs(client)
    PENDING_LINK_INPUT[message.from_user.id] = {"mode": "batch_first"}
    await message.reply_text(
        "Forward the First Message from DB Channel (with Quotes)..\n\nor Send the DB Channel Post Link",
        quote=True,
    )


@Bot.on_message(filters.private & filters.user(ADMINS) & filters.command("genlink"), group=-50)
async def link_generator(client: Client, message: Message):
    await _ensure_runtime_attrs(client)
    PENDING_LINK_INPUT[message.from_user.id] = {"mode": "single"}
    await message.reply_text(
        "Forward Message from the DB Channel (with Quotes)..\nor Send the DB Channel Post link",
        quote=True,
    )


@Bot.on_message(
    filters.private
    & filters.user(ADMINS)
    & (filters.forwarded | (filters.text & ~filters.forwarded))
    & ~filters.command(
        ["start", "admin", "broadcast", "users", "stats", "addchannel", "removechannel", "pausepair", "resumepair", "batch", "genlink", "autobatch"]
    ),
    group=11,
)
async def handle_pending_link_input(client: Client, message: Message):
    state = PENDING_LINK_INPUT.get(message.from_user.id)
    if not state:
        return
    await _ensure_runtime_attrs(client)
    msg_id = await get_message_id(client, message)
    if not msg_id:
        await message.reply_text(
            "Error: this forwarded post is not from my DB Channel or this link is not from my DB Channel.",
            quote=True,
        )
        return

    mode = state.get("mode")
    if mode == "single":
        base64_string = await encode(f"get-{msg_id * abs(client.db_channel.id)}")
        link = f"https://t.me/{client.username}?start={base64_string}"
        PENDING_LINK_INPUT.pop(message.from_user.id, None)
        await message.reply_text(
            f"<b>Here is your link</b>\n\n{link}",
            quote=True,
            reply_markup=_share_markup(link),
        )
        return

    if mode == "batch_first":
        PENDING_LINK_INPUT[message.from_user.id] = {"mode": "batch_second", "first_id": msg_id}
        await message.reply_text(
            "Forward the Last Message from DB Channel (with Quotes)..\nor Send the DB Channel Post link",
            quote=True,
        )
        return

    if mode == "batch_second":
        first_id = int(state.get("first_id") or 0)
        second_id = int(msg_id)
        if not first_id:
            PENDING_LINK_INPUT[message.from_user.id] = {"mode": "batch_first"}
            await message.reply_text("Batch state expired. Send /batch again.", quote=True)
            return
        string = f"get-{first_id * abs(client.db_channel.id)}-{second_id * abs(client.db_channel.id)}"
        base64_string = await encode(string)
        link = f"https://t.me/{client.username}?start={base64_string}"
        PENDING_LINK_INPUT.pop(message.from_user.id, None)
        await message.reply_text(
            f"<b>Here is your link</b>\n\n{link}",
            quote=True,
            reply_markup=_share_markup(link),
        )
