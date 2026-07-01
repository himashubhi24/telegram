import asyncio
import json
import time
from pathlib import Path

from pyrogram import StopPropagation, filters
from pyrogram.enums import ParseMode

from bot import Bot
from config import ADMINS, CUSTOM_CAPTION, DISABLE_CHANNEL_BUTTON, PROTECT_CONTENT
from helper_func import decode, get_messages
from admin_fsub_common import (
    edit_markup_message,
    get_missing_force_sub_entries,
    is_force_sub_globally_enabled,
    is_link_flow_enabled,
    send_force_sub_gate,
    send_markup_message,
)
from database.database import get_setting


LINK_UNLOCK_SECONDS = 8
LINK_FLOW_STORE = Path(__file__).resolve().parent.parent / "link_flow_sessions.json"
PENDING_LINK_FLOW = {}


def _load_sessions():
    if not LINK_FLOW_STORE.exists():
        return {}
    try:
        data = json.loads(LINK_FLOW_STORE.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_sessions(sessions):
    temporary = LINK_FLOW_STORE.with_suffix(".tmp")
    temporary.write_text(json.dumps(sessions, indent=2, sort_keys=True) + "\n")
    temporary.replace(LINK_FLOW_STORE)


def _session_key(user_id, message_id):
    return f"{int(user_id)}:{int(message_id)}"


def _put_session(user_id, message_id, payload):
    sessions = _load_sessions()
    now = time.time()
    sessions = {
        key: value for key, value in sessions.items()
        if isinstance(value, dict) and float(value.get("expires_at", 0)) > now
    }
    sessions[_session_key(user_id, message_id)] = {
        "user_id": int(user_id),
        "message_id": int(message_id),
        "payload": payload,
        "ready_at": now + LINK_UNLOCK_SECONDS,
        "expires_at": now + 21600,
    }
    _save_sessions(sessions)


def _get_session(user_id, message_id):
    return _load_sessions().get(_session_key(user_id, message_id))


def _pop_session(user_id, message_id):
    sessions = _load_sessions()
    state = sessions.pop(_session_key(user_id, message_id), None)
    _save_sessions(sessions)
    return state


def _link_rows(saved_link, callback_data=None):
    rows = [[{"text": "Open Link", "url": saved_link, "style": "primary"}]]
    if callback_data:
        rows.append([{"text": "Get File", "callback_data": callback_data, "style": "success"}])
    return rows


async def _reveal_get_files(client, chat_id, message_id, user_id, saved_link):
    await asyncio.sleep(LINK_UNLOCK_SECONDS)
    key = (user_id, message_id)
    state = PENDING_LINK_FLOW.get(key) or _get_session(user_id, message_id)
    if not state:
        return
    callback_data = f"linkflow_get:{message_id}"
    try:
        await edit_markup_message(
            client,
            chat_id=chat_id,
            message_id=message_id,
            text="Open Link and Try Again",
            rows=_link_rows(saved_link, callback_data),
        )
        if key in PENDING_LINK_FLOW:
            PENDING_LINK_FLOW[key]["ready_at"] = time.time()
    except Exception as exc:
        try:
            await send_markup_message(
                client,
                chat_id=chat_id,
                text="Open Link and Try Again",
                rows=_link_rows(saved_link, callback_data),
            )
        except Exception as fallback_exc:
            print(f"[link flow] failed to reveal Get File: {exc}; fallback: {fallback_exc}")


async def _deliver_payload(client, user_id: int, payload: str):
    string = await decode(payload)
    argument = string.split("-")
    if len(argument) == 3:
        start = int(int(argument[1]) / abs(client.db_channel.id))
        end = int(int(argument[2]) / abs(client.db_channel.id))
        if start <= end:
            ids = range(start, end + 1)
        else:
            ids = []
            i = start
            while True:
                ids.append(i)
                i -= 1
                if i < end:
                    break
    elif len(argument) == 2:
        ids = [int(int(argument[1]) / abs(client.db_channel.id))]
    else:
        raise RuntimeError("Invalid payload")

    messages = await get_messages(client, ids)
    for msg in messages:
        if bool(CUSTOM_CAPTION) and bool(getattr(msg, "document", None)):
            caption = CUSTOM_CAPTION.format(
                previouscaption="" if not msg.caption else msg.caption.html,
                filename=msg.document.file_name,
            )
        else:
            caption = "" if not msg.caption else msg.caption.html

        reply_markup = msg.reply_markup if DISABLE_CHANNEL_BUTTON else None
        await msg.copy(
            chat_id=user_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            protect_content=PROTECT_CONTENT,
        )
        await asyncio.sleep(0.5)


@Bot.on_message(filters.command("start") & filters.private, group=-150)
async def link_flow_gate(client, message):
    if not getattr(message, "from_user", None):
        return
    if message.from_user.id in ADMINS:
        return
    text = message.text or ""
    if len(text) <= 7 or " " not in text:
        return
    if not is_link_flow_enabled():
        return

    # Never show the link page before every active force-sub requirement is met.
    if is_force_sub_globally_enabled():
        try:
            missing = await get_missing_force_sub_entries(client, message.from_user.id)
        except Exception as exc:
            print(f"[link flow] force-sub check soft-failed: {exc}")
            return
        if missing:
            try:
                await send_force_sub_gate(client, message)
            except Exception as exc:
                print(f"[link flow] force-sub page failed: {exc}")
                return
            raise StopPropagation

    saved_link = await get_setting("amazon_link", "")
    if not saved_link:
        return
    try:
        payload = text.split(" ", 1)[1].strip()
        await decode(payload)
    except Exception:
        return

    sent_message_id = await send_markup_message(
        client,
        chat_id=message.chat.id,
        text="Open Link and Try Again",
        rows=_link_rows(saved_link),
        reply_to_message_id=message.id,
    )
    if not sent_message_id:
        return
    key = (message.from_user.id, sent_message_id)
    PENDING_LINK_FLOW[key] = {
        "payload": payload,
        "ready_at": time.time() + LINK_UNLOCK_SECONDS,
    }
    _put_session(message.from_user.id, sent_message_id, payload)
    asyncio.create_task(
        _reveal_get_files(client, message.chat.id, sent_message_id, message.from_user.id, saved_link)
    )
    raise StopPropagation


@Bot.on_callback_query(filters.regex(r"^linkflow_get:\d+$"), group=-1000)
async def link_flow_get_files(client, query):
    user = getattr(query, "from_user", None)
    if not user:
        return
    try:
        message_id = int(query.data.rsplit(":", 1)[1])
    except Exception:
        await query.answer("Invalid link session.", show_alert=True)
        return
    key = (user.id, message_id)
    state = PENDING_LINK_FLOW.get(key) or _get_session(user.id, message_id)
    if not state:
        await query.answer("Link session expired. Open your file link again.", show_alert=True)
        raise StopPropagation
    if time.time() < float(state["ready_at"]):
        await query.answer("Please wait for Get File to unlock.", show_alert=True)
        raise StopPropagation
    memory_state = PENDING_LINK_FLOW.pop(key, None)
    stored_state = _pop_session(user.id, message_id)
    payload = (memory_state or stored_state or state)["payload"]
    try:
        await query.answer("Sending files...")
    except Exception:
        pass
    try:
        await query.message.delete()
    except Exception:
        pass
    try:
        await _deliver_payload(client, user.id, payload)
    except Exception as exc:
        print(f"[link flow] file delivery failed user={user.id}: {exc}")
        await client.send_message(user.id, "Something went wrong. Please open your file link again.")
    raise StopPropagation
