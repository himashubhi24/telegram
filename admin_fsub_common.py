import json
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
        "link_flow_enabled": True,
        "request_auto_approve": True,
        "channels": [],
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


def is_force_sub_globally_enabled():
    return bool(load_store().get("enabled", True))


def set_force_sub_globally_enabled(enabled):
    store = load_store()
    store["enabled"] = bool(enabled)
    save_store(store)


def is_link_flow_enabled():
    return bool(load_store().get("link_flow_enabled", True))


def set_link_flow_enabled(enabled):
    store = load_store()
    store["link_flow_enabled"] = bool(enabled)
    save_store(store)


def get_enabled_force_sub_entries():
    store = load_store()
    if not store.get("enabled", True):
        return []
    return [entry for entry in get_all_force_sub_entries() if entry.get("enabled", True)]


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


async def get_client_username(client):
    username = getattr(client, "username", None)
    if username:
        return username
    me = await client.get_me()
    username = me.username
    try:
        client.username = username
    except Exception:
        pass
    return username


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


async def validate_channel(client, channel_id):
    chat = await client.get_chat(channel_id)
    me = await client.get_me()
    try:
        member = await client.get_chat_member(channel_id, me.id)
    except Exception as exc:
        raise RuntimeError(f"Bot is not a member/admin in {channel_id}: {exc}")
    if member.status not in ACTIVE_STATUSES:
        raise RuntimeError(f"Bot is not active in {channel_id}")
    title, username, link = await _resolve_join_link(
        client,
        {
            "channel_id": channel_id,
            "title": chat.title or str(channel_id),
            "username": getattr(chat, "username", None) or "",
            "request_mode": False,
        },
    )
    return {
        "channel_id": channel_id,
        "title": title,
        "username": username,
        "link": link,
    }


async def add_force_sub_channels(client, channel_ids, request_mode, added_by):
    store = load_store()
    channels = {entry["channel_id"]: entry for entry in store["channels"] if _safe_int(entry.get("channel_id"))}
    added = []
    for channel_id in channel_ids:
        info = await validate_channel(client, channel_id)
        channels[channel_id] = {
            "channel_id": channel_id,
            "title": info["title"],
            "username": info["username"],
            "enabled": True,
            "request_mode": bool(request_mode),
            "source": "dynamic",
            "added_by": _safe_int(added_by),
            "created_at": channels.get(channel_id, {}).get("created_at") or _now(),
        }
        added.append(channels[channel_id])
    store["channels"] = sorted(channels.values(), key=lambda item: str(item.get("title") or item["channel_id"]).lower())
    save_store(store)
    return added


def remove_force_sub_channels(channel_ids):
    store = load_store()
    before = len(store["channels"])
    wanted = set(channel_ids)
    store["channels"] = [entry for entry in store["channels"] if _safe_int(entry.get("channel_id")) not in wanted]
    save_store(store)
    return before - len(store["channels"])


def set_force_sub_channels_enabled(channel_ids, enabled):
    store = load_store()
    wanted = set(channel_ids)
    changed = 0
    for entry in store["channels"]:
        if _safe_int(entry.get("channel_id")) in wanted:
            if bool(entry.get("enabled", True)) != bool(enabled):
                entry["enabled"] = bool(enabled)
                changed += 1
    save_store(store)
    return changed


async def is_user_authorized_for_force_sub(client, user_id):
    if user_id in _config_admins():
        return True
    entries = get_enabled_force_sub_entries()
    if not entries:
        return True
    for entry in entries:
        try:
            member = await client.get_chat_member(entry["channel_id"], user_id)
        except UserNotParticipant:
            return False
        except Exception:
            return False
        if member.status not in ACTIVE_STATUSES:
            return False
    return True


async def get_missing_force_sub_entries(client, user_id):
    if user_id in _config_admins():
        return []
    missing = []
    for entry in get_enabled_force_sub_entries():
        try:
            member = await client.get_chat_member(entry["channel_id"], user_id)
        except UserNotParticipant:
            missing.append(entry)
            continue
        except Exception:
            missing.append(entry)
            continue
        if member.status not in ACTIVE_STATUSES:
            missing.append(entry)
    return missing


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


async def build_force_sub_rows(client, message, missing_entries):
    rows = []
    for entry in missing_entries:
        try:
            title, username, link = await _resolve_join_link(client, entry)
            entry["title"] = title
            entry["username"] = username
        except Exception:
            title = entry.get("title") or str(entry["channel_id"])
            link = None
        if link:
            label = "Request Join" if entry.get("request_mode") else "Join Channel"
            rows.append(
                [
                    {
                        "text": f"{label}: {title}",
                        "url": link,
                        "style": "primary",
                    }
                ]
            )
    bot_username = await get_client_username(client)
    payload = ""
    try:
        payload = message.command[1]
    except Exception:
        payload = ""
    retry_url = f"https://t.me/{bot_username}?start={payload}" if payload else f"https://t.me/{bot_username}"
    rows.append([{"text": "Try Again", "url": retry_url, "style": "success"}])
    return rows


async def send_force_sub_gate(client, message):
    missing_entries = await get_missing_force_sub_entries(client, message.from_user.id)
    if not missing_entries:
        return False
    rows = await build_force_sub_rows(client, message, missing_entries)
    text = _format_force_message(message, missing_entries)
    await message.reply(
        text=text,
        reply_markup=build_pyrogram_markup(rows),
        quote=True,
        disable_web_page_preview=True,
    )
    return True


async def handle_join_request(client, join_request):
    store = load_store()
    if not store.get("request_auto_approve", True):
        return
    chat_id = getattr(join_request.chat, "id", 0)
    if chat_id not in {_safe_int(item.get("channel_id")) for item in get_enabled_force_sub_entries() if item.get("request_mode")}:
        return
    try:
        await client.approve_chat_join_request(chat_id, join_request.from_user.id)
    except FloodWait as exc:
        import asyncio
        await asyncio.sleep(exc.value)
        await client.approve_chat_join_request(chat_id, join_request.from_user.id)


def render_force_sub_list():
    lines = [
        "<b>Force-Subscribe Settings</b>",
        f"Global gate: <code>{'ON' if is_force_sub_globally_enabled() else 'OFF'}</code>",
        f"Link flow: <code>{'ON' if is_link_flow_enabled() else 'OFF'}</code>",
        "",
    ]
    entries = get_all_force_sub_entries()
    if not entries:
        lines.append("No force-sub channels configured.")
    else:
        for item in entries:
            lines.append(
                f"<code>{item['channel_id']}</code> | "
                f"{item.get('title') or item['channel_id']} | "
                f"{'request' if item.get('request_mode') else 'normal'} | "
                f"{'enabled' if item.get('enabled', True) else 'disabled'} | "
                f"{item.get('source', 'dynamic')}"
            )
    lines.extend(
        [
            "",
            "Dynamic buttons hide channels already joined by the user.",
            "Request-mode channels create request-to-join links when possible.",
        ]
    )
    return "\n".join(lines)
