import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait
from pyrogram.errors.exceptions.bad_request_400 import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config


STORE_PATH = Path(__file__).resolve().parent / "force_sub_store.json"
ACTIVE_STATUSES = {
    ChatMemberStatus.OWNER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value):
    if value in (None, "", False):
        return 0
    try:
        return int(str(value).strip())
    except Exception:
        return 0


def _config_admins():
    admins = set()
    for value in getattr(config, "ADMINS", []) or []:
        ivalue = _safe_int(value)
        if ivalue:
            admins.add(ivalue)
    owner_id = _safe_int(getattr(config, "OWNER_ID", 0))
    if owner_id:
        admins.add(owner_id)
    return admins


def _default_store():
    return {
        "enabled": True,
        "request_auto_approve": True,
        "channels": [],
        "join_requests": {},
    }


def load_store():
    if not STORE_PATH.exists():
        return _default_store()
    try:
        data = json.loads(STORE_PATH.read_text())
    except Exception:
        return _default_store()
    store = _default_store()
    if isinstance(data, dict):
        store.update({k: v for k, v in data.items() if k in store})
    if not isinstance(store["channels"], list):
        store["channels"] = []
    if not isinstance(store["join_requests"], dict):
        store["join_requests"] = {}
    return store


def save_store(store):
    STORE_PATH.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n")


def parse_channel_ids(text):
    ids = []
    for raw in (text or "").replace(",", " ").split():
        value = _safe_int(raw)
        if value:
            ids.append(value)
    return list(dict.fromkeys(ids))


def _config_force_sub_entries():
    entries = []
    seen = set()
    for attr_name in ("FORCE_SUB_CHANNEL", "FORCE_SUB_CHANNEL2"):
        value = _safe_int(getattr(config, attr_name, 0))
        if value and value not in seen:
            entries.append(
                {
                    "channel_id": value,
                    "title": str(value),
                    "username": "",
                    "enabled": True,
                    "request_mode": False,
                    "source": "config",
                }
            )
            seen.add(value)
    values = getattr(config, "FORCE_SUB_CHANNELS", []) or []
    for item in values:
        value = _safe_int(item)
        if value and value not in seen:
            entries.append(
                {
                    "channel_id": value,
                    "title": str(value),
                    "username": "",
                    "enabled": True,
                    "request_mode": False,
                    "source": "config",
                }
            )
            seen.add(value)
    return entries


def get_all_force_sub_entries():
    merged = {}
    for entry in _config_force_sub_entries():
        merged[entry["channel_id"]] = entry
    for entry in load_store()["channels"]:
        channel_id = _safe_int(entry.get("channel_id"))
        if not channel_id:
            continue
        merged[channel_id] = {
            "channel_id": channel_id,
            "title": entry.get("title") or str(channel_id),
            "username": entry.get("username") or "",
            "enabled": bool(entry.get("enabled", True)),
            "request_mode": bool(entry.get("request_mode", False)),
            "source": entry.get("source", "dynamic"),
            "added_by": _safe_int(entry.get("added_by")),
            "created_at": entry.get("created_at") or _now(),
        }
    return list(merged.values())


def get_enabled_force_sub_entries():
    store = load_store()
    if not store.get("enabled", True):
        return []
    return [entry for entry in get_all_force_sub_entries() if entry.get("enabled", True)]


def _format_force_message(message, missing_entries):
    template = getattr(
        config,
        "FORCE_MSG",
        "Hello {first}!\nPlease join the required channel(s), then tap Try Again.",
    )
    user = message.from_user
    values = {
        "first": user.first_name or "",
        "last": user.last_name or "",
        "username": "" if not user.username else f"@{user.username}",
        "mention": user.mention if user else "",
        "id": user.id if user else 0,
    }
    try:
        text = template.format(**values)
    except Exception:
        text = template
    if missing_entries:
        suffix = "\n".join(
            f"{'Request join' if item.get('request_mode') else 'Join'}: {item.get('title') or item['channel_id']}"
            for item in missing_entries
        )
        text = f"{text}\n\n{suffix}"
    return text


def build_pyrogram_markup(rows):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=button["text"],
                    url=button.get("url"),
                    callback_data=button.get("callback_data"),
                )
                for button in row
            ]
            for row in rows
        ]
    )


def _bot_api_request(method, payload):
    token = getattr(config, "TG_BOT_TOKEN", "")
    if not token:
        return None
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


async def send_styled_panel(message, text, rows):
    payload = {
        "chat_id": message.chat.id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": rows},
    }
    try:
        result = await __import__("asyncio").to_thread(_bot_api_request, "sendMessage", payload)
        if result and result.get("ok"):
            return True
    except Exception:
        return False
    return False


async def edit_styled_panel(message, text, rows):
    payload = {
        "chat_id": message.chat.id,
        "message_id": message.id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": rows},
    }
    try:
        result = await __import__("asyncio").to_thread(_bot_api_request, "editMessageText", payload)
        if result and result.get("ok"):
            return True
    except Exception:
        return False
    return False


async def _resolve_join_link(client, entry):
    channel_id = entry["channel_id"]
    chat = await client.get_chat(channel_id)
    title = chat.title or entry.get("title") or str(channel_id)
    username = getattr(chat, "username", None) or entry.get("username") or ""
    if entry.get("request_mode"):
        invite = await client.create_chat_invite_link(
            channel_id,
            creates_join_request=True,
            name=f"fsub-{title}",
        )
        return title, username, invite.invite_link
    if username:
        return title, username, f"https://t.me/{username}"
    link = getattr(chat, "invite_link", None)
    if not link:
        link = await client.export_chat_invite_link(channel_id)
    return title, username, link


async def _get_missing_entries(client, user_id):
    if user_id in _config_admins():
        return []
    missing = []
    store = load_store()
    requests = store.get("join_requests", {})
    for entry in get_enabled_force_sub_entries():
        channel_id = entry["channel_id"]
        try:
            member = await client.get_chat_member(channel_id, user_id)
            if member.status in ACTIVE_STATUSES:
                continue
        except UserNotParticipant:
            request_key = f"{channel_id}:{user_id}"
            request_state = requests.get(request_key, {})
            if entry.get("request_mode") and request_state.get("status") in {"pending", "approved"}:
                continue
            missing.append(entry)
            continue
        except FloodWait as exc:
            await __import__("asyncio").sleep(exc.value)
            missing.append(entry)
            continue
        except Exception:
            missing.append(entry)
    return missing


async def is_user_authorized_for_force_sub(client, user_id):
    missing = await _get_missing_entries(client, user_id)
    return not missing


async def send_force_sub_gate(client, message):
    payload = ""
    if getattr(message, "command", None) and len(message.command) > 1:
        payload = message.command[1]
    missing = await _get_missing_entries(client, message.from_user.id)
    if not missing:
        return False
    rows = []
    for entry in missing:
        title, username, link = await _resolve_join_link(client, entry)
        entry["title"] = title
        entry["username"] = username
        rows.append(
            [
                {
                    "text": f"{'Request' if entry.get('request_mode') else 'Join'} {title}",
                    "url": link,
                    "style": "primary",
                }
            ]
        )
    retry_url = f"https://t.me/{client.username}?start={payload}" if payload else f"https://t.me/{client.username}"
    rows.append([{"text": "Try Again", "url": retry_url, "style": "success"}])
    text = _format_force_message(message, missing)
    if not await send_styled_panel(message, text, rows):
        await message.reply_text(
            text,
            reply_markup=build_pyrogram_markup(rows),
            quote=True,
            disable_web_page_preview=True,
        )
    return True


async def validate_channel_for_force_sub(client, channel_id, request_mode=False):
    channel_id = _safe_int(channel_id)
    if not channel_id:
        raise ValueError("Invalid channel ID")
    chat = await client.get_chat(channel_id)
    me = await client.get_me()
    member = await client.get_chat_member(channel_id, me.id)
    if member.status not in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}:
        raise ValueError("Bot is not a member of this channel")
    privileges = getattr(member, "privileges", None)
    can_invite = bool(getattr(privileges, "can_invite_users", False))
    if request_mode and member.status != ChatMemberStatus.OWNER and not can_invite:
        raise ValueError("Bot must be admin with invite permission for request-to-join fsub")
    title = chat.title or str(channel_id)
    username = getattr(chat, "username", None) or ""
    if not username and member.status != ChatMemberStatus.OWNER and not can_invite and not getattr(chat, "invite_link", None):
        raise ValueError("Bot needs admin invite permission for a private force-sub channel")
    return {
        "channel_id": channel_id,
        "title": title,
        "username": username,
        "request_mode": bool(request_mode),
    }


async def add_force_sub_channels(client, channel_ids, request_mode, added_by):
    store = load_store()
    added = []
    for channel_id in channel_ids:
        info = await validate_channel_for_force_sub(client, channel_id, request_mode=request_mode)
        entry = {
            "channel_id": info["channel_id"],
            "title": info["title"],
            "username": info["username"],
            "enabled": True,
            "request_mode": bool(request_mode),
            "source": "dynamic",
            "added_by": _safe_int(added_by),
            "created_at": _now(),
        }
        existing = [item for item in store["channels"] if _safe_int(item.get("channel_id")) != info["channel_id"]]
        existing.append(entry)
        store["channels"] = existing
        added.append(entry)
    save_store(store)
    return added


def remove_force_sub_channels(channel_ids):
    store = load_store()
    remove_set = {_safe_int(item) for item in channel_ids}
    before = len(store["channels"])
    store["channels"] = [item for item in store["channels"] if _safe_int(item.get("channel_id")) not in remove_set]
    save_store(store)
    return before - len(store["channels"])


def set_force_sub_channels_enabled(channel_ids, enabled):
    store = load_store()
    changed = 0
    target_ids = {_safe_int(item) for item in channel_ids}
    for item in store["channels"]:
        if _safe_int(item.get("channel_id")) in target_ids:
            item["enabled"] = bool(enabled)
            changed += 1
    save_store(store)
    return changed


def render_force_sub_list():
    entries = get_all_force_sub_entries()
    if not entries:
        return "No force-sub channels configured."
    lines = ["<b>Force-Subscribe Channels</b>"]
    for item in entries:
        mode = "request" if item.get("request_mode") else "normal"
        status = "enabled" if item.get("enabled", True) else "disabled"
        title = item.get("title") or item["channel_id"]
        lines.append(f"{title} | <code>{item['channel_id']}</code> | {mode} | {status}")
    return "\n".join(lines)


def set_force_sub_globally_enabled(enabled):
    store = load_store()
    store["enabled"] = bool(enabled)
    save_store(store)


def is_force_sub_globally_enabled():
    return bool(load_store().get("enabled", True))


async def handle_join_request(client, join_request):
    channel_id = _safe_int(join_request.chat.id)
    user_id = _safe_int(join_request.from_user.id)
    store = load_store()
    request_key = f"{channel_id}:{user_id}"
    store["join_requests"][request_key] = {
        "status": "pending",
        "updated_at": _now(),
        "user_id": user_id,
        "channel_id": channel_id,
    }
    save_store(store)
    dynamic = next((item for item in store["channels"] if _safe_int(item.get("channel_id")) == channel_id), None)
    if not dynamic or not dynamic.get("request_mode"):
        return
    if not store.get("request_auto_approve", True):
        return
    try:
        await client.approve_chat_join_request(channel_id, user_id)
        store = load_store()
        store["join_requests"][request_key] = {
            "status": "approved",
            "updated_at": _now(),
            "user_id": user_id,
            "channel_id": channel_id,
        }
        save_store(store)
    except Exception:
        pass
