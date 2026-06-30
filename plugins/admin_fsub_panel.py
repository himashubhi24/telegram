from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from bot import Bot
from config import ADMINS
from admin_fsub_common import (
    add_force_sub_channels,
    edit_styled_panel,
    handle_join_request,
    is_force_sub_globally_enabled,
    parse_channel_ids,
    remove_force_sub_channels,
    render_force_sub_list,
    send_styled_panel,
    set_force_sub_channels_enabled,
    set_force_sub_globally_enabled,
)


PENDING_INPUT = {}
HAS_EXISTING_PANEL = Path(__file__).with_name("admin_panel.py").exists()
ADMIN_COMMANDS = ["fsubadmin"] if HAS_EXISTING_PANEL else ["admin", "fsubadmin"]


def panel_rows():
    return [
        [
            {"text": "Add Normal", "callback_data": "fsub:add:normal", "style": "success"},
            {"text": "Add Request", "callback_data": "fsub:add:request", "style": "primary"},
        ],
        [
            {"text": "Remove", "callback_data": "fsub:remove", "style": "danger"},
            {"text": "List", "callback_data": "fsub:list", "style": "primary"},
        ],
        [
            {"text": "Enable IDs", "callback_data": "fsub:enable", "style": "success"},
            {"text": "Disable IDs", "callback_data": "fsub:disable", "style": "danger"},
        ],
        [
            {
                "text": "Disable All" if is_force_sub_globally_enabled() else "Enable All",
                "callback_data": "fsub:toggle_global",
                "style": "primary",
            }
        ],
        [{"text": "Close", "callback_data": "fsub:close", "style": "danger"}],
    ]


async def render_panel(target, text=None):
    body = text or render_force_sub_list()
    if isinstance(target, Message):
        if not await send_styled_panel(target, body, panel_rows()):
            await target.reply_text(body)
        return
    if not await edit_styled_panel(target.message, body, panel_rows()):
        await target.message.edit_text(body)


@Bot.on_message(filters.command(ADMIN_COMMANDS) & filters.private & filters.user(ADMINS))
async def open_fsub_panel(client: Client, message: Message):
    PENDING_INPUT.pop(message.from_user.id, None)
    await render_panel(message, "<b>Force-Subscribe Admin Panel</b>")


@Bot.on_callback_query(filters.regex("^fsub:") & filters.user(ADMINS))
async def fsub_callbacks(client: Client, query: CallbackQuery):
    action = query.data
    user_id = query.from_user.id
    if action == "fsub:close":
        PENDING_INPUT.pop(user_id, None)
        await query.message.delete()
        await query.answer()
        return
    if action == "fsub:list":
        PENDING_INPUT.pop(user_id, None)
        await render_panel(query, render_force_sub_list())
        await query.answer()
        return
    if action == "fsub:toggle_global":
        set_force_sub_globally_enabled(not is_force_sub_globally_enabled())
        PENDING_INPUT.pop(user_id, None)
        await render_panel(query, render_force_sub_list())
        await query.answer("Global status updated")
        return
    mapping = {
        "fsub:add:normal": "add_normal",
        "fsub:add:request": "add_request",
        "fsub:remove": "remove",
        "fsub:enable": "enable",
        "fsub:disable": "disable",
    }
    next_state = mapping.get(action)
    if next_state:
        PENDING_INPUT[user_id] = next_state
        prompts = {
            "add_normal": "Send one or more channel IDs for normal force-sub.\nExample:\n<code>-1001234567890 -1009876543210</code>",
            "add_request": "Send one or more channel IDs for request-to-join force-sub.\nBot should be admin there.",
            "remove": "Send one or more channel IDs to remove from dynamic force-sub.",
            "enable": "Send one or more channel IDs to enable.",
            "disable": "Send one or more channel IDs to disable.",
        }
        await query.answer()
        await query.message.reply_text(prompts[next_state])
        return
    await query.answer()


@Bot.on_message(filters.private & filters.user(ADMINS) & filters.text & ~filters.command(ADMIN_COMMANDS))
async def handle_fsub_pending_input(client: Client, message: Message):
    state = PENDING_INPUT.get(message.from_user.id)
    if not state:
        return
    channel_ids = parse_channel_ids(message.text or "")
    if not channel_ids:
        await message.reply_text("Invalid input. Send numeric channel IDs only.")
        return
    try:
        if state == "add_normal":
            added = await add_force_sub_channels(client, channel_ids, False, message.from_user.id)
            text = "\n".join(f"{item['title']} added to the fsubs" for item in added)
        elif state == "add_request":
            added = await add_force_sub_channels(client, channel_ids, True, message.from_user.id)
            text = "\n".join(f"{item['title']} added to the request fsubs" for item in added)
        elif state == "remove":
            removed = remove_force_sub_channels(channel_ids)
            text = f"Removed {removed} force-sub channel(s)."
        elif state == "enable":
            changed = set_force_sub_channels_enabled(channel_ids, True)
            text = f"Enabled {changed} force-sub channel(s)."
        else:
            changed = set_force_sub_channels_enabled(channel_ids, False)
            text = f"Disabled {changed} force-sub channel(s)."
        PENDING_INPUT.pop(message.from_user.id, None)
        await message.reply_text(text)
        await render_panel(message, render_force_sub_list())
    except Exception as exc:
        await message.reply_text(f"Setup failed: <code>{exc}</code>")


@Bot.on_chat_join_request()
async def force_sub_join_request(client: Client, join_request):
    await handle_join_request(client, join_request)
