from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from admin_fsub_common import (
    add_force_sub_channels,
    approve_all_pending_join_requests,
    approve_pending_join_requests_for_channel,
    delete_message_ids_later,
    edit_markup_message,
    get_force_sub_entry,
    get_pending_join_request_counts,
    handle_join_request,
    is_force_sub_globally_enabled,
    is_link_flow_enabled,
    parse_channel_ids,
    remove_force_sub_channels,
    render_force_sub_list,
    send_markup_message,
    set_force_sub_channels_enabled,
    set_force_sub_globally_enabled,
    set_link_flow_enabled,
)
from bot import Bot
from config import ADMINS


PENDING_INPUT = {}


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
                "text": ("Gate OFF" if is_force_sub_globally_enabled() else "Gate ON"),
                "callback_data": "fsub:toggle_global",
                "style": "primary",
            },
            {
                "text": ("Link Flow OFF" if is_link_flow_enabled() else "Link Flow ON"),
                "callback_data": "fsub:toggle_link_flow",
                "style": "primary",
            },
        ],
        [
            {"text": "Pending Requests", "callback_data": "fsub:pending_list", "style": "success"},
            {"text": "Accept All Pending", "callback_data": "fsub:accept_pending", "style": "success"},
        ],
        [{"text": "Back", "callback_data": "fsub:back", "style": "primary"}],
    ]


def pending_rows(items):
    rows = []
    current_row = []
    for item in items:
        current_row.append(
            {
                "text": f"{item['title']} ({item['count']})",
                "callback_data": f"fsub:pending_channel:{item['channel_id']}",
                "style": "primary",
            }
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([{"text": "Back", "callback_data": "fsub:back", "style": "primary"}])
    return rows


async def render_pending_panel(client: Client, target):
    items = await get_pending_join_request_counts(client)
    text = ["<b>Pending Join Requests</b>", ""]
    if not items:
        text.append("No pending request channel found.")
    else:
        for item in items:
            text.append(
                f"<code>{item['channel_id']}</code> | {item['title']} | pending=<code>{item['count']}</code>"
            )
    body = "\n".join(text)
    if isinstance(target, Message):
        await send_markup_message(
            client,
            chat_id=target.chat.id,
            text=body,
            rows=pending_rows(items),
            reply_to_message_id=target.id,
            disable_web_page_preview=True,
        )
        return
    await edit_markup_message(
        client,
        chat_id=target.message.chat.id,
        message_id=target.message.id,
        text=body,
        rows=pending_rows(items),
        disable_web_page_preview=True,
    )


async def render_panel(target, text=None):
    body = text or render_force_sub_list()
    if isinstance(target, Message):
        sent_id = await send_markup_message(
            target._client,
            chat_id=target.chat.id,
            text=body,
            rows=panel_rows(),
            reply_to_message_id=target.id,
            disable_web_page_preview=True,
        )
        if target.text and target.text.startswith("/fsubadmin"):
            import asyncio
            asyncio.create_task(delete_message_ids_later(target._client, target.chat.id, [target.id, sent_id], 300))
        return
    await edit_markup_message(
        target._client,
        chat_id=target.message.chat.id,
        message_id=target.message.id,
        text=body,
        rows=panel_rows(),
        disable_web_page_preview=True,
    )


@Bot.on_message(filters.command(["fsubadmin"]) & filters.private & filters.user(ADMINS))
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
    if action == "fsub:back":
        PENDING_INPUT.pop(user_id, None)
        await render_panel(query, render_force_sub_list())
        await query.answer()
        return
    if action == "fsub:toggle_global":
        set_force_sub_globally_enabled(not is_force_sub_globally_enabled())
        PENDING_INPUT.pop(user_id, None)
        await render_panel(query, render_force_sub_list())
        await query.answer("Gate status updated")
        return
    if action == "fsub:toggle_link_flow":
        set_link_flow_enabled(not is_link_flow_enabled())
        PENDING_INPUT.pop(user_id, None)
        await render_panel(query, render_force_sub_list())
        await query.answer("Link flow updated")
        return
    if action == "fsub:accept_pending":
        PENDING_INPUT.pop(user_id, None)
        count = await approve_all_pending_join_requests(client)
        await render_panel(query, render_force_sub_list())
        await query.answer(f"Accepted {count} pending request(s)")
        return
    if action == "fsub:pending_list":
        PENDING_INPUT.pop(user_id, None)
        await render_pending_panel(client, query)
        await query.answer()
        return
    if action.startswith("fsub:pending_channel:"):
        channel_id = int(action.rsplit(":", 1)[1])
        entry = get_force_sub_entry(channel_id)
        title = entry.get("title") if entry else str(channel_id)
        PENDING_INPUT[user_id] = f"pending_accept:{channel_id}"
        await query.answer("Send count to accept")
        await query.message.reply_text(
            f"Send how many requests to accept for <b>{title}</b>.\nExample: <code>5</code>"
        )
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


@Bot.on_message(filters.private & filters.user(ADMINS) & filters.text & ~filters.command(["fsubadmin", "admin", "start", "broadcast", "batch", "users"]), group=10)
async def handle_fsub_pending_input(client: Client, message: Message):
    state = PENDING_INPUT.get(message.from_user.id)
    if not state:
        return
    channel_ids = parse_channel_ids(message.text or "")
    if state.startswith("pending_accept:"):
        try:
            count = int((message.text or "").strip())
        except Exception:
            await message.reply_text("Send a number like <code>5</code>.")
            return
        if count < 1:
            await message.reply_text("Count must be at least <code>1</code>.")
            return
        channel_id = int(state.split(":", 1)[1])
        entry = get_force_sub_entry(channel_id)
        title = entry.get("title") if entry else str(channel_id)
        approved = await approve_pending_join_requests_for_channel(client, channel_id, count)
        PENDING_INPUT.pop(message.from_user.id, None)
        await message.reply_text(
            f"Accepted <code>{approved}</code> pending request(s) for <b>{title}</b>."
        )
        await render_panel(message, render_force_sub_list())
        return
    if not channel_ids:
        await message.reply_text("Invalid input. Send numeric channel IDs only.")
        return
    try:
        if state == "add_normal":
            added = await add_force_sub_channels(client, channel_ids, False, message.from_user.id)
            set_force_sub_globally_enabled(True)
            text = "\n".join(f"{item['title']} channel added to the fsubs" for item in added)
        elif state == "add_request":
            added = await add_force_sub_channels(client, channel_ids, True, message.from_user.id)
            set_force_sub_globally_enabled(True)
            text = "\n".join(f"{item['title']} channel added to the request fsubs" for item in added)
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
        await client.send_message(message.chat.id, f"✅ {text}")
        await render_panel(message, render_force_sub_list())
    except Exception as exc:
        await message.reply_text(f"Setup failed: <code>{exc}</code>")


@Bot.on_chat_join_request()
async def force_sub_join_request(client: Client, join_request):
    await handle_join_request(client, join_request)
