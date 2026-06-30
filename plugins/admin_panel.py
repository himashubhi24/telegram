#(c) CodeXBotz / Advanced File Share Bot

from pyrogram import Client, filters
from pyrogram.errors import PhoneCodeExpired, PhoneCodeInvalid, PhoneNumberInvalid, SessionPasswordNeeded, PasswordHashInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

from bot import Bot
from admin_fsub_common import is_link_flow_enabled
from config import ADMINS, APP_ID, API_HASH
from auto_repost import run_backfill
from plugins.admin_fsub_panel import render_panel as render_fsub_panel
from database.database import (
    add_channel_pair,
    add_force_sub_channel,
    get_bot_statistics,
    list_channel_pairs,
    list_force_sub_channels,
    remove_channel_pair,
    remove_force_sub_channel,
    set_channel_pair_active,
    set_channel_pair_start,
    configure_channel_pair_schedule,
    set_setting,
    get_setting,
    get_userbot_session,
)

PENDING_ADMIN_INPUT = {}
PAIR_DRAFTS = {}
USERBOT_LOGIN = {}


def admin_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Add Source", callback_data="admin_add_source"),
                InlineKeyboardButton("Add Target", callback_data="admin_add_target"),
            ],
            [InlineKeyboardButton("Remove Source/Target", callback_data="admin_remove_pair")],
            [InlineKeyboardButton("Add Userbot Session", callback_data="admin_add_session")],
            [InlineKeyboardButton("Userbot Status", callback_data="admin_userbot_status")],
            [InlineKeyboardButton("Force Subscribe Panel", callback_data="admin_open_fsub")],
            [InlineKeyboardButton("View Channel Pairs", callback_data="admin_pairs")],
            [
                InlineKeyboardButton("Start From First", callback_data="admin_start_first"),
                InlineKeyboardButton("Start From Latest", callback_data="admin_start_latest"),
            ],
            [
                InlineKeyboardButton("Interval 30 Min", callback_data="admin_interval_30"),
                InlineKeyboardButton("Interval 1 Hour", callback_data="admin_interval_60"),
            ],
            [InlineKeyboardButton("Set Custom Start Post", callback_data="admin_set_start_post")],
            [InlineKeyboardButton("Live Repost Status", callback_data="admin_repost_status")],
            [InlineKeyboardButton("Run Backfill Now", callback_data="admin_run_backfill")],
            [InlineKeyboardButton("Statistics", callback_data="admin_stats")],
            [InlineKeyboardButton("Link Flow Status", callback_data="admin_link_flow_status")],
            [InlineKeyboardButton("Bot Settings", callback_data="admin_settings")],
            [InlineKeyboardButton("Close", callback_data="close")],
        ]
    )


@Bot.on_message(filters.command("admin") & filters.private & filters.user(ADMINS))
async def admin_panel(client: Client, message: Message):
    PENDING_ADMIN_INPUT.pop(message.from_user.id, None)
    await message.reply_text("<b>Admin Panel</b>", reply_markup=admin_markup())


@Bot.on_callback_query(filters.regex("^admin_") & filters.user(ADMINS))
async def admin_callbacks(client: Client, query: CallbackQuery):
    action = query.data
    user_id = query.from_user.id
    if action == "admin_help_add":
        text = (
            "<b>Channel Pair Commands</b>\n\n"
            "<code>/addchannel source_id target_id</code>\n"
            "<code>/removechannel source_id [target_id]</code>\n"
            "<code>/pausepair source_id target_id</code>\n"
            "<code>/resumepair source_id target_id</code>\n"
            "<code>/addforcesub channel_id</code>\n"
            "<code>/removeforcesub channel_id</code>\n"
            "<code>/setamazon https://amazon-link</code>\n"
            "<code>/stats</code>"
        )
    elif action == "admin_add_source":
        PENDING_ADMIN_INPUT[user_id] = "source"
        await query.answer("Send source channel ID")
        await query.message.reply_text("Send source channel ID like <code>-1001234567890</code>.")
        return
    elif action == "admin_add_target":
        if not PAIR_DRAFTS.get(user_id, {}).get("source"):
            PENDING_ADMIN_INPUT[user_id] = "source"
            await query.answer("Source required first")
            await query.message.reply_text("First send source channel ID like <code>-1001234567890</code>.")
            return
        PENDING_ADMIN_INPUT[user_id] = "target"
        await query.answer("Send target channel ID")
        await query.message.reply_text("Send target channel ID like <code>-1001234567890</code>.")
        return
    elif action == "admin_add_pair":
        PENDING_ADMIN_INPUT[user_id] = "source"
        await query.answer("Send source first")
        await query.message.reply_text("Send source channel ID like <code>-1001234567890</code>. Target will be asked after source.")
        return
    elif action == "admin_remove_pair":
        PENDING_ADMIN_INPUT[user_id] = "remove_pair"
        await query.answer("Send source/target")
        await query.message.reply_text(
            "Send like this to remove one pair:\n<code>-100SOURCE -100TARGET</code>\n\n"
            "Or send only source to remove all target pairs for that source:\n<code>-100SOURCE</code>"
        )
        return
    elif action == "admin_add_session":
        PENDING_ADMIN_INPUT[user_id] = "userbot_phone"
        USERBOT_LOGIN.pop(user_id, None)
        await query.answer("Send phone number")
        await query.message.reply_text("Send Telegram user account phone number with country code, example: <code>+91XXXXXXXXXX</code>.")
        return
    elif action == "admin_open_fsub":
        PENDING_ADMIN_INPUT.pop(user_id, None)
        await query.answer()
        await render_fsub_panel(query, "<b>Force-Subscribe Admin Panel</b>")
        return
    elif action in ("admin_start_first", "admin_start_latest"):
        mode = "first" if action == "admin_start_first" else "latest"
        PENDING_ADMIN_INPUT[user_id] = f"schedule_{mode}"
        await query.answer(f"Configure {mode} mode")
        await query.message.reply_text(
            "Send source and target IDs:\n<code>-100SOURCE -100TARGET</code>"
        )
        return
    elif action in ("admin_interval_30", "admin_interval_60"):
        minutes = 30 if action == "admin_interval_30" else 60
        PENDING_ADMIN_INPUT[user_id] = f"interval_{minutes}"
        await query.answer(f"Set {minutes} minute interval")
        await query.message.reply_text(
            "Send source and target IDs:\n<code>-100SOURCE -100TARGET</code>"
        )
        return
    elif action == "admin_set_start_post":
        PENDING_ADMIN_INPUT[user_id] = "start_post"
        await query.answer("Send source target post ID")
        await query.message.reply_text(
            "Send like this:\n<code>-100SOURCE -100TARGET 12345</code>\n\n"
            "Repost will start from this source channel post ID."
        )
        return
    elif action == "admin_repost_status":
        session = await get_userbot_session()
        enabled = await get_setting("auto_repost_enabled", False)
        pairs = await list_channel_pairs()
        total_processed = sum(int(p.get("total_posts_processed") or 0) for p in pairs)
        lines = [
            "<b>Live Repost Status</b>",
            f"Session: <code>{'Saved' if session else 'Not saved'}</code>",
            f"Auto repost: <code>{'ON' if enabled else 'OFF'}</code>",
            f"Pairs: <code>{len(pairs)}</code>",
            f"Total processed: <code>{total_processed}</code>",
            "",
        ]
        for p in pairs[:20]:
            lines.append(
                f"<code>{p.get('source_channel')}</code> -> <code>{p.get('target_channel')}</code> "
                f"active=<code>{p.get('active')}</code> "
                f"start=<code>{p.get('start_message_id') or 'latest/new'}</code> "
                f"processed=<code>{p.get('total_posts_processed') or 0}</code> "
                f"last_msg=<code>{p.get('last_message_id') or '-'}</code> "
                f"last=<code>{p.get('last_post_at') or '-'}</code> "
                f"error=<code>{p.get('last_error') or '-'}</code>"
            )
        text = "\n".join(lines)
    elif action == "admin_run_backfill":
        await query.answer("Backfill started", show_alert=False)
        await query.message.edit_text("<b>Backfill running...</b>\nPlease wait. Check Live Repost Status after this finishes.")
        result = await run_backfill(client, limit=100)
        if result.get("ok"):
            text = "<b>Backfill complete</b>\n" + f"Processed/sent: <code>{result.get('processed', 0)}</code>\n" + "\n".join(result.get("details", []))
        else:
            text = "<b>Backfill failed</b>\n" + f"<code>{result.get('error')}</code>"
        await query.message.edit_text(text, reply_markup=admin_markup())
        return
    elif action == "admin_userbot_status":
        session = await get_userbot_session()
        enabled = await get_setting("auto_repost_enabled", False)
        text = "\n".join(
            [
                "<b>Userbot Status</b>",
                f"Session: <code>{'Saved' if session else 'Not saved'}</code>",
                f"Auto repost: <code>{'ON' if enabled else 'OFF'}</code>",
                "Worker: <code>Check service logs for running state</code>",
            ]
        )
    elif action == "admin_link_flow_status":
        text = "\n".join(
            [
                "<b>Link Flow Status</b>",
                f"Gate before deeplink: <code>{'ON' if is_link_flow_enabled() else 'OFF'}</code>",
                "Use the Force Subscribe Panel to change link-flow behavior.",
            ]
        )
    elif action == "admin_pairs":
        pairs = await list_channel_pairs()
        if not pairs:
            text = "No channel pairs configured."
        else:
            lines = ["<b>Channel Pairs</b>"]
            for item in pairs[:25]:
                status = "active" if item.get("active") else "paused"
                lines.append(f"<code>{item['source_channel']}</code> -> <code>{item['target_channel']}</code> ({status})")
            text = "\n".join(lines)
    elif action == "admin_stats":
        stats = await get_bot_statistics()
        text = "\n".join(
            [
                "<b>Statistics</b>",
                f"Users: <code>{stats['total_users']}</code>",
                f"Verified: <code>{stats['verified_users']}</code>",
                f"Files: <code>{stats['total_files']}</code>",
                f"Batches: <code>{stats['total_batches']}</code>",
                f"Active pairs: <code>{stats['active_channels']}</code>",
                f"Today users: <code>{stats['today_users']}</code>",
                f"Today verifications: <code>{stats['today_verifications']}</code>",
            ]
        )
    else:
        text = "Settings are controlled by environment variables and /setamazon."
    await query.message.edit_text(text, reply_markup=admin_markup())
    await query.answer()


@Bot.on_message(filters.private & filters.user(ADMINS) & filters.text & ~filters.command(["admin", "fsubadmin", "addchannel", "removechannel", "pausepair", "resumepair", "listchannels", "addforcesub", "removeforcesub", "setamazon", "stats", "broadcast", "users", "start"]))
async def admin_pending_input(client: Client, message: Message):
    user_id = message.from_user.id
    action = PENDING_ADMIN_INPUT.get(user_id)
    if not action:
        return
    text = (message.text or "").strip()
    try:
        if action == "source":
            source = int(text)
            PAIR_DRAFTS[user_id] = {"source": source}
            PENDING_ADMIN_INPUT[user_id] = "target"
            await message.reply_text(f"Source saved: <code>{source}</code>\nNow send target channel ID like <code>-1001234567890</code>.")
            return
        if action == "target":
            target = int(text)
            source = PAIR_DRAFTS.get(user_id, {}).get("source")
            if not source:
                PENDING_ADMIN_INPUT[user_id] = "source"
                await message.reply_text("Source missing. Send source channel ID first.")
                return
            await add_channel_pair(source, target, user_id)
            PENDING_ADMIN_INPUT.pop(user_id, None)
            PAIR_DRAFTS.pop(user_id, None)
            await message.reply_text(f"Source-target pair added:\n<code>{source}</code> -> <code>{target}</code>", reply_markup=admin_markup())
            return
        if action == "remove_pair":
            parts = text.split()
            if len(parts) not in (1, 2):
                await message.reply_text("Send <code>-100SOURCE</code> or <code>-100SOURCE -100TARGET</code>.")
                return
            source = int(parts[0])
            target = int(parts[1]) if len(parts) == 2 else None
            await remove_channel_pair(source, target)
            PENDING_ADMIN_INPUT.pop(user_id, None)
            if target is None:
                await message.reply_text(f"Removed all target pairs for source <code>{source}</code>.", reply_markup=admin_markup())
            else:
                await message.reply_text(f"Removed pair:\n<code>{source}</code> -> <code>{target}</code>", reply_markup=admin_markup())
            return
        if action in ("schedule_first", "schedule_latest"):
            parts = text.split()
            if len(parts) != 2:
                await message.reply_text(
                    "Send exactly: <code>-100SOURCE -100TARGET</code>"
                )
                return
            source, target = map(int, parts)
            mode = action.removeprefix("schedule_")
            await configure_channel_pair_schedule(
                source, target, mode=mode, updated_by=user_id
            )
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text(
                f"Schedule enabled: <code>{source}</code> -> "
                f"<code>{target}</code>\n"
                f"Mode: <code>{mode}</code>",
                reply_markup=admin_markup(),
            )
            return
        if action in ("interval_30", "interval_60"):
            parts = text.split()
            if len(parts) != 2:
                await message.reply_text(
                    "Send exactly: <code>-100SOURCE -100TARGET</code>"
                )
                return
            source, target = map(int, parts)
            minutes = int(action.removeprefix("interval_"))
            await configure_channel_pair_schedule(
                source, target,
                interval_minutes=minutes,
                updated_by=user_id,
            )
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text(
                f"Posting interval set to <code>{minutes} minutes</code>.",
                reply_markup=admin_markup(),
            )
            return
        if action == "start_post":
            parts = text.split()
            if len(parts) != 3:
                await message.reply_text("Send exactly like this: <code>-100SOURCE -100TARGET 12345</code>")
                return
            source, target, start_id = map(int, parts)
            await set_channel_pair_start(source, target, start_id, user_id)
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text(
                f"Start post saved:\n<code>{source}</code> -> <code>{target}</code> from post <code>{start_id}</code>",
                reply_markup=admin_markup(),
            )
            return
        if action == "session":
            session = text
            if len(session) < 50:
                await message.reply_text("This does not look like a valid session string. Send again or /admin to cancel.")
                return
            await set_setting("session_string", session, user_id)
            await set_setting("auto_repost_enabled", True, user_id)
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text("Userbot session saved. Restart service once to start auto-repost worker.", reply_markup=admin_markup())
            return
        if action == "userbot_phone":
            phone = text.replace(" ", "")
            user_client = Client(f"userbot_login_{user_id}", api_id=APP_ID, api_hash=API_HASH, in_memory=True)
            await user_client.connect()
            try:
                sent = await user_client.send_code(phone)
            except PhoneNumberInvalid:
                await user_client.disconnect()
                await message.reply_text("Invalid phone number. Send again with country code, example: <code>+91XXXXXXXXXX</code>.")
                return
            USERBOT_LOGIN[user_id] = {"client": user_client, "phone": phone, "hash": sent.phone_code_hash}
            PENDING_ADMIN_INPUT[user_id] = "userbot_otp"
            await message.reply_text("OTP sent on Telegram. Send OTP here. If Telegram shows code as <code>1 2 3 4 5</code>, send <code>12345</code>.")
            return
        if action == "userbot_otp":
            state = USERBOT_LOGIN.get(user_id)
            if not state:
                PENDING_ADMIN_INPUT[user_id] = "userbot_phone"
                await message.reply_text("Login expired. Send phone number again.")
                return
            code = text.replace(" ", "")
            user_client = state["client"]
            try:
                await user_client.sign_in(state["phone"], state["hash"], code)
            except SessionPasswordNeeded:
                PENDING_ADMIN_INPUT[user_id] = "userbot_password"
                await message.reply_text("2FA enabled. Send Telegram password.")
                return
            except (PhoneCodeInvalid, PhoneCodeExpired):
                await message.reply_text("Invalid/expired OTP. Click Add Userbot Session again and retry.")
                try:
                    await user_client.disconnect()
                except Exception:
                    pass
                USERBOT_LOGIN.pop(user_id, None)
                PENDING_ADMIN_INPUT.pop(user_id, None)
                return
            session = await user_client.export_session_string()
            await user_client.disconnect()
            await set_setting("session_string", session, user_id)
            await set_setting("auto_repost_enabled", True, user_id)
            USERBOT_LOGIN.pop(user_id, None)
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text("Userbot login complete. Session saved. Restarting service will enable auto-repost worker.", reply_markup=admin_markup())
            return
        if action == "userbot_password":
            state = USERBOT_LOGIN.get(user_id)
            if not state:
                PENDING_ADMIN_INPUT[user_id] = "userbot_phone"
                await message.reply_text("Login expired. Send phone number again.")
                return
            user_client = state["client"]
            try:
                await user_client.check_password(text)
            except PasswordHashInvalid:
                await message.reply_text("Wrong password. Send 2FA password again.")
                return
            session = await user_client.export_session_string()
            await user_client.disconnect()
            await set_setting("session_string", session, user_id)
            await set_setting("auto_repost_enabled", True, user_id)
            USERBOT_LOGIN.pop(user_id, None)
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text("Userbot login complete. Session saved. Restarting service will enable auto-repost worker.", reply_markup=admin_markup())
            return
    except ValueError:
        await message.reply_text("Invalid ID. Send a numeric channel ID like <code>-1001234567890</code>.")
    except Exception as exc:
        PENDING_ADMIN_INPUT.pop(user_id, None)
        await message.reply_text(f"Setup failed: <code>{exc}</code>", reply_markup=admin_markup())


@Bot.on_message(filters.command("addchannel") & filters.private & filters.user(ADMINS))
async def add_channel_cmd(client: Client, message: Message):
    if len(message.command) < 3:
        await message.reply_text("Usage: <code>/addchannel source_id target_id</code>")
        return
    await add_channel_pair(int(message.command[1]), int(message.command[2]), message.from_user.id)
    await message.reply_text("Channel pair added.")


@Bot.on_message(filters.command("removechannel") & filters.private & filters.user(ADMINS))
async def remove_channel_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/removechannel source_id [target_id]</code>")
        return
    target = int(message.command[2]) if len(message.command) > 2 else None
    await remove_channel_pair(int(message.command[1]), target)
    await message.reply_text("Channel pair removed.")


@Bot.on_message(filters.command(["pausepair", "resumepair"]) & filters.private & filters.user(ADMINS))
async def toggle_pair_cmd(client: Client, message: Message):
    if len(message.command) < 3:
        await message.reply_text("Usage: <code>/pausepair source_id target_id</code>")
        return
    active = message.command[0] == "resumepair"
    await set_channel_pair_active(int(message.command[1]), int(message.command[2]), active)
    await message.reply_text("Pair updated.")


@Bot.on_message(filters.command("listchannels") & filters.private & filters.user(ADMINS))
async def list_channels_cmd(client: Client, message: Message):
    pairs = await list_channel_pairs()
    if not pairs:
        await message.reply_text("No channel pairs configured.")
        return
    text = "\n".join(
        f"<code>{p['source_channel']}</code> -> <code>{p['target_channel']}</code> {'active' if p.get('active') else 'paused'}"
        for p in pairs[:50]
    )
    await message.reply_text(text)


@Bot.on_message(filters.command("addforcesub") & filters.private & filters.user(ADMINS))
async def add_force_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/addforcesub channel_id</code>")
        return
    await add_force_sub_channel(int(message.command[1]))
    await message.reply_text("Force-sub channel added.")


@Bot.on_message(filters.command("removeforcesub") & filters.private & filters.user(ADMINS))
async def remove_force_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/removeforcesub channel_id</code>")
        return
    await remove_force_sub_channel(int(message.command[1]))
    await message.reply_text("Force-sub channel removed.")


@Bot.on_message(filters.command("setamazon") & filters.private & filters.user(ADMINS))
async def set_amazon_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/setamazon https://...</code>")
        return
    await set_setting("amazon_link", message.command[1], message.from_user.id)
    await message.reply_text("Amazon link saved in database. Update AMAZON_LINK env for web app default.")


@Bot.on_message(filters.command("stats") & filters.private & filters.user(ADMINS))
async def stats_cmd(client: Client, message: Message):
    stats = await get_bot_statistics()
    await message.reply_text(
        "\n".join(
            [
                "<b>Statistics</b>",
                f"Users: <code>{stats['total_users']}</code>",
                f"Verified: <code>{stats['verified_users']}</code>",
                f"Files: <code>{stats['total_files']}</code>",
                f"Batches: <code>{stats['total_batches']}</code>",
                f"Active pairs: <code>{stats['active_channels']}</code>",
            ]
        )
    )
