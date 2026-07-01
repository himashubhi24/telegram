#(c) CodeXBotz / Advanced File Share Bot

import asyncio

from pyrogram import Client, filters
from pyrogram.errors import PhoneCodeExpired, PhoneCodeInvalid, PhoneNumberInvalid, SessionPasswordNeeded, PasswordHashInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

from bot import Bot
from admin_fsub_common import (
    delete_message_ids_later,
    edit_markup_message,
    is_link_flow_enabled,
    set_link_flow_enabled,
    send_markup_message,
)
from config import ADMINS, APP_ID, API_HASH
from auto_repost import run_backfill
from plugins.admin_fsub_panel import render_panel as render_fsub_panel, render_pending_panel
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
    save_userbot_session,
    clear_userbot_session,
    get_auto_repost_enabled,
    set_auto_repost_enabled,
)

PENDING_ADMIN_INPUT = {}
PAIR_DRAFTS = {}
USERBOT_LOGIN = {}


async def validate_userbot_session(session):
    probe = Client(
        "validate_userbot_session",
        api_id=APP_ID,
        api_hash=API_HASH,
        session_string=session,
        in_memory=True,
        no_updates=True,
    )
    try:
        await asyncio.wait_for(probe.start(), timeout=45)
        me = await probe.get_me()
        if getattr(me, "is_bot", False):
            raise RuntimeError("A user account session is required, not a bot token session")
    finally:
        try:
            await probe.stop()
        except Exception:
            pass


async def reload_auto_repost_worker(client):
    worker = getattr(client, "auto_repost_worker", None)
    if worker is None:
        raise RuntimeError("Auto-repost worker is unavailable in this bot process")
    started = await worker.restart()
    if not started:
        raise RuntimeError("Worker did not start; check session and auto-repost setting")
    return worker


async def stop_auto_repost_worker(client):
    worker = getattr(client, "auto_repost_worker", None)
    if worker is not None:
        await worker.stop()


async def get_userbot_status_details(client):
    session = await get_userbot_session()
    worker = getattr(client, "auto_repost_worker", None)
    running = bool(worker and worker.is_running())
    user = getattr(getattr(worker, "userbot", None), "me", None) if running else None
    error = None

    if session and user is None:
        probe = Client(
            "userbot_status_probe",
            api_id=APP_ID,
            api_hash=API_HASH,
            session_string=session,
            in_memory=True,
            no_updates=True,
        )
        try:
            await asyncio.wait_for(probe.start(), timeout=45)
            user = await probe.get_me()
        except Exception as exc:
            error = str(exc)
        finally:
            try:
                await probe.stop()
            except Exception:
                pass

    return {
        "session": bool(session),
        "running": running,
        "user": user,
        "error": error,
    }


def admin_markup():
    return [
        [
            {"text": "Add Source", "callback_data": "admin_add_source", "style": "success"},
            {"text": "Add Target", "callback_data": "admin_add_target", "style": "success"},
        ],
        [
            {"text": "Remove Source Target", "callback_data": "admin_remove_pair", "style": "danger"},
            {"text": "Add Userbot Session", "callback_data": "admin_add_session", "style": "success"},
        ],
        [
            {"text": "Remove Userbot", "callback_data": "admin_remove_session", "style": "danger"},
            {"text": "Enable Auto Repost", "callback_data": "admin_auto_enable", "style": "success"},
        ],
        [
            {"text": "Disable Auto Repost", "callback_data": "admin_auto_disable", "style": "danger"},
            {"text": "Userbot Status", "callback_data": "admin_userbot_status", "style": "primary"},
        ],
        [
            {"text": "Force Subscribe Panel", "callback_data": "admin_open_fsub", "style": "primary"},
            {"text": "View Channel Pairs", "callback_data": "admin_pairs", "style": "primary"},
        ],
        [
            {"text": "Pending Requests", "callback_data": "admin_open_pending", "style": "success"},
        ],
        [
            {"text": "Start From First", "callback_data": "admin_start_first", "style": "success"},
            {"text": "Start From Latest", "callback_data": "admin_start_latest", "style": "primary"},
        ],
        [
            {"text": "Interval 30 Min", "callback_data": "admin_interval_30", "style": "success"},
            {"text": "Interval 1 Hour", "callback_data": "admin_interval_60", "style": "success"},
        ],
        [
            {"text": "Set Custom Start Post", "callback_data": "admin_set_start_post", "style": "primary"},
            {"text": "Set Custom Interval", "callback_data": "admin_interval_custom", "style": "success"},
        ],
        [
            {"text": "Live Repost Status", "callback_data": "admin_repost_status", "style": "primary"},
            {"text": "Run Backfill Now", "callback_data": "admin_run_backfill", "style": "success"},
        ],
        [
            {"text": "Statistics", "callback_data": "admin_stats", "style": "primary"},
            {"text": "Link Flow Status", "callback_data": "admin_link_flow_status", "style": "primary"},
        ],
        [
            {"text": "Enable Link Flow", "callback_data": "admin_link_flow_enable", "style": "success"},
            {"text": "Disable Link Flow", "callback_data": "admin_link_flow_disable", "style": "danger"},
        ],
        [
            {"text": "Set Link", "callback_data": "admin_set_link", "style": "success"},
            {"text": "Bot Settings", "callback_data": "admin_settings", "style": "primary"},
        ],
        [{"text": "Close", "callback_data": "close", "style": "danger"}],
    ]


async def render_admin_panel(target, text="<b>Admin Panel</b>"):
    rows = admin_markup()
    if isinstance(target, Message):
        sent_id = await send_markup_message(
            target._client,
            chat_id=target.chat.id,
            text=text,
            rows=rows,
            reply_to_message_id=target.id,
            disable_web_page_preview=True,
        )
        if target.text and target.text.startswith("/admin"):
            import asyncio
            asyncio.create_task(delete_message_ids_later(target._client, target.chat.id, [target.id, sent_id], 300))
        return
    await edit_markup_message(
        target._client,
        chat_id=target.message.chat.id,
        message_id=target.message.id,
        text=text,
        rows=rows,
        disable_web_page_preview=True,
    )


@Bot.on_message(filters.command("admin") & filters.private & filters.user(ADMINS))
async def admin_panel(client: Client, message: Message):
    PENDING_ADMIN_INPUT.pop(message.from_user.id, None)
    await render_admin_panel(message)


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
    elif action == "admin_remove_session":
        try:
            await stop_auto_repost_worker(client)
            await clear_userbot_session(user_id)
            await query.answer("Userbot removed", show_alert=False)
            await query.message.reply_text("✅ Userbot session successfully removed. Auto repost is disabled.")
        except Exception as exc:
            await query.answer("Remove failed", show_alert=True)
            await query.message.reply_text(f"❌ Userbot removal failed: <code>{exc}</code>")
        return
    elif action == "admin_auto_enable":
        try:
            if not await get_userbot_session():
                raise RuntimeError("Add a valid userbot session first")
            await set_auto_repost_enabled(True, user_id)
            await reload_auto_repost_worker(client)
            await query.answer("Auto repost enabled")
            await query.message.reply_text("✅ Auto repost successfully enabled and connected.")
        except Exception as exc:
            await query.answer("Enable failed", show_alert=True)
            await query.message.reply_text(f"❌ Auto repost enable failed: <code>{exc}</code>")
        return
    elif action == "admin_auto_disable":
        try:
            await set_auto_repost_enabled(False, user_id)
            await stop_auto_repost_worker(client)
            await query.answer("Auto repost disabled")
            await query.message.reply_text("✅ Auto repost successfully disabled.")
        except Exception as exc:
            await query.answer("Disable failed", show_alert=True)
            await query.message.reply_text(f"❌ Auto repost disable failed: <code>{exc}</code>")
        return
    elif action == "admin_open_fsub":
        PENDING_ADMIN_INPUT.pop(user_id, None)
        await query.answer()
        await render_fsub_panel(query, "<b>Force-Subscribe Admin Panel</b>")
        return
    elif action == "admin_open_pending":
        PENDING_ADMIN_INPUT.pop(user_id, None)
        await query.answer()
        await render_pending_panel(client, query)
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
    elif action == "admin_interval_custom":
        PENDING_ADMIN_INPUT[user_id] = "interval_custom"
        await query.answer("Set custom interval")
        await query.message.reply_text(
            "Send like this:\n<code>-100SOURCE -100TARGET HOURS</code>\n\n"
            "Hours can be from <code>1</code> to <code>24</code>."
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
        enabled = await get_auto_repost_enabled()
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
        await render_admin_panel(query, text)
        return
    elif action == "admin_userbot_status":
        details = await get_userbot_status_details(client)
        enabled = await get_auto_repost_enabled()
        user = details["user"]
        first_name = getattr(user, "first_name", None) or "-"
        username = getattr(user, "username", None)
        username = f"@{username}" if username else "-"
        phone = getattr(user, "phone_number", None) or "-"
        text = "\n".join(
            [
                "<b>Userbot Status</b>",
                f"Session: <code>{'Active' if details['session'] and user else 'Not active'}</code>",
                f"Connection: <code>{'Connected' if details['running'] else 'Disconnected'}</code>",
                f"Phone: <code>{phone}</code>",
                f"Name: <code>{first_name}</code>",
                f"Username: <code>{username}</code>",
                f"Auto repost: <code>{'ON' if enabled else 'OFF'}</code>",
                f"Error: <code>{details['error'] or '-'}</code>",
            ]
        )
    elif action == "admin_link_flow_status":
        saved_link = await get_setting("amazon_link", "")
        text = "\n".join(
            [
                "<b>Link Flow Status</b>",
                f"Gate before deeplink: <code>{'ON' if is_link_flow_enabled() else 'OFF'}</code>",
                f"Current link: <code>{saved_link or 'not set'}</code>",
                "Use buttons below to enable, disable, or set the link.",
            ]
        )
    elif action == "admin_link_flow_enable":
        set_link_flow_enabled(True)
        saved_link = await get_setting("amazon_link", "")
        text = "\n".join(
            [
                "<b>Link Flow Status</b>",
                f"Gate before deeplink: <code>{'ON' if is_link_flow_enabled() else 'OFF'}</code>",
                f"Current link: <code>{saved_link or 'not set'}</code>",
            ]
        )
    elif action == "admin_link_flow_disable":
        set_link_flow_enabled(False)
        saved_link = await get_setting("amazon_link", "")
        text = "\n".join(
            [
                "<b>Link Flow Status</b>",
                f"Gate before deeplink: <code>{'ON' if is_link_flow_enabled() else 'OFF'}</code>",
                f"Current link: <code>{saved_link or 'not set'}</code>",
            ]
        )
    elif action == "admin_set_link":
        PENDING_ADMIN_INPUT[user_id] = "set_link"
        await query.answer("Send link")
        await query.message.reply_text("Send the link you want to save.\nExample:\n<code>https://amzn.to/xxxx</code>")
        return
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
    await render_admin_panel(query, text)
    await query.answer()


@Bot.on_message(filters.private & filters.user(ADMINS) & filters.text & ~filters.command(["admin", "fsubadmin", "addchannel", "removechannel", "pausepair", "resumepair", "listchannels", "addforcesub", "removeforcesub", "setamazon", "stats", "broadcast", "users", "start", "batch", "genlink", "autobatch"]))
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
            if not await add_channel_pair(source, target, user_id):
                raise RuntimeError("Database did not confirm the source-target pair")
            PENDING_ADMIN_INPUT.pop(user_id, None)
            PAIR_DRAFTS.pop(user_id, None)
            await message.reply_text(f"✅ Source and target successfully added:\n<code>{source}</code> -> <code>{target}</code>")
            return
        if action == "remove_pair":
            parts = text.split()
            if len(parts) not in (1, 2):
                await message.reply_text("Send <code>-100SOURCE</code> or <code>-100SOURCE -100TARGET</code>.")
                return
            source = int(parts[0])
            target = int(parts[1]) if len(parts) == 2 else None
            removed = await remove_channel_pair(source, target)
            if not removed:
                raise RuntimeError("No matching source/target pair was found")
            PENDING_ADMIN_INPUT.pop(user_id, None)
            if target is None:
                await message.reply_text(f"✅ Successfully removed {removed} target pair(s) for source <code>{source}</code>.")
            else:
                await message.reply_text(f"✅ Source-target pair successfully removed:\n<code>{source}</code> -> <code>{target}</code>")
            return
        if action in ("schedule_first", "schedule_latest"):
            parts = text.split()
            if len(parts) != 2:
                await message.reply_text(
                    "Send exactly: <code>-100SOURCE -100TARGET</code>"
                )
                return
            source, target = map(int, parts)
            mode = action[len("schedule_"):]
            await configure_channel_pair_schedule(
                source, target, mode=mode, updated_by=user_id
            )
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text(
                f"Schedule enabled: <code>{source}</code> -> "
                f"<code>{target}</code>\n"
                f"Mode: <code>{mode}</code>",
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
            minutes = int(action[len("interval_"):])
            await configure_channel_pair_schedule(
                source, target,
                interval_minutes=minutes,
                updated_by=user_id,
            )
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text(
                f"Posting interval set to <code>{minutes} minutes</code>.",
            )
            return
        if action == "interval_custom":
            parts = text.split()
            if len(parts) != 3:
                await message.reply_text(
                    "Send exactly like this:\n<code>-100SOURCE -100TARGET HOURS</code>"
                )
                return
            source, target = map(int, parts[:2])
            hours = int(parts[2])
            if hours < 1 or hours > 24:
                await message.reply_text("Hours must be between <code>1</code> and <code>24</code>.")
                return
            minutes = hours * 60
            await configure_channel_pair_schedule(
                source, target,
                interval_minutes=minutes,
                updated_by=user_id,
            )
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text(
                f"Posting interval set to <code>{hours} hour(s)</code>.",
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
            )
            return
        if action == "session":
            session = text
            if len(session) < 50:
                await message.reply_text("This does not look like a valid session string. Send again or /admin to cancel.")
                return
            try:
                await validate_userbot_session(session)
            except Exception as exc:
                await message.reply_text(f"Invalid userbot session: <code>{exc}</code>")
                return
            await save_userbot_session(session, user_id)
            await set_auto_repost_enabled(True, user_id)
            await reload_auto_repost_worker(client)
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text("✅ Userbot session successfully added. Auto repost is connected and running.")
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
            await save_userbot_session(session, user_id)
            await set_auto_repost_enabled(True, user_id)
            await reload_auto_repost_worker(client)
            USERBOT_LOGIN.pop(user_id, None)
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text("✅ Userbot login successful. Session added and auto repost is running.")
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
            await save_userbot_session(session, user_id)
            await set_auto_repost_enabled(True, user_id)
            await reload_auto_repost_worker(client)
            USERBOT_LOGIN.pop(user_id, None)
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text("✅ Userbot login successful. Session added and auto repost is running.")
            return
        if action == "set_link":
            if not (text.startswith("http://") or text.startswith("https://")):
                await message.reply_text("Send a valid URL starting with <code>http://</code> or <code>https://</code>.")
                return
            await set_setting("amazon_link", text, user_id)
            PENDING_ADMIN_INPUT.pop(user_id, None)
            await message.reply_text("✅ Link successfully saved.")
            return
    except ValueError:
        await message.reply_text("Invalid ID. Send a numeric channel ID like <code>-1001234567890</code>.")
    except Exception as exc:
        PENDING_ADMIN_INPUT.pop(user_id, None)
        await message.reply_text(f"❌ Setup failed: <code>{exc}</code>")


@Bot.on_message(filters.command("addchannel") & filters.private & filters.user(ADMINS))
async def add_channel_cmd(client: Client, message: Message):
    if len(message.command) < 3:
        await message.reply_text("Usage: <code>/addchannel source_id target_id</code>")
        return
    try:
        source = int(message.command[1])
        target = int(message.command[2])
        if not await add_channel_pair(source, target, message.from_user.id):
            raise RuntimeError("database did not acknowledge the update")
        await message.reply_text(
            f"Source <code>{source}</code> and target <code>{target}</code> successfully added."
        )
    except Exception as exc:
        await message.reply_text(f"Failed to add channel pair: <code>{exc}</code>")


@Bot.on_message(filters.command("removechannel") & filters.private & filters.user(ADMINS))
async def remove_channel_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/removechannel source_id [target_id]</code>")
        return
    try:
        source = int(message.command[1])
        target = int(message.command[2]) if len(message.command) > 2 else None
        removed = await remove_channel_pair(source, target)
        if not removed:
            raise RuntimeError("no matching source/target pair was found")
        target_text = f" and target <code>{target}</code>" if target is not None else ""
        await message.reply_text(
            f"Source <code>{source}</code>{target_text} successfully removed ({removed} pair(s))."
        )
    except Exception as exc:
        await message.reply_text(f"Failed to remove channel pair: <code>{exc}</code>")


@Bot.on_message(filters.command(["pausepair", "resumepair"]) & filters.private & filters.user(ADMINS))
async def toggle_pair_cmd(client: Client, message: Message):
    if len(message.command) < 3:
        await message.reply_text("Usage: <code>/pausepair source_id target_id</code>")
        return
    try:
        source = int(message.command[1])
        target = int(message.command[2])
        active = message.command[0] == "resumepair"
        matched = await set_channel_pair_active(source, target, active)
        if not matched:
            raise RuntimeError("no matching source/target pair was found")
        state = "enabled" if active else "disabled"
        await message.reply_text(f"Channel pair successfully {state}.")
    except Exception as exc:
        await message.reply_text(f"Failed to update channel pair: <code>{exc}</code>")


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
    try:
        channel_id = int(message.command[1])
        if not await add_force_sub_channel(channel_id):
            raise RuntimeError("database did not acknowledge the update")
        await message.reply_text(f"Force-sub channel <code>{channel_id}</code> successfully added.")
    except Exception as exc:
        await message.reply_text(f"Failed to add force-sub channel: <code>{exc}</code>")


@Bot.on_message(filters.command("removeforcesub") & filters.private & filters.user(ADMINS))
async def remove_force_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/removeforcesub channel_id</code>")
        return
    try:
        channel_id = int(message.command[1])
        removed = await remove_force_sub_channel(channel_id)
        if not removed:
            raise RuntimeError("channel is not present in force-sub configuration")
        await message.reply_text(f"Force-sub channel <code>{channel_id}</code> successfully removed.")
    except Exception as exc:
        await message.reply_text(f"Failed to remove force-sub channel: <code>{exc}</code>")


@Bot.on_message(filters.command(["setamazon", "setlink"]) & filters.private & filters.user(ADMINS))
async def set_amazon_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/setlink https://...</code>")
        return
    await set_setting("amazon_link", message.command[1], message.from_user.id)
    await message.reply_text("Link saved in database.")


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
