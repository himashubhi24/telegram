#(c) CodeXBotz / Advanced File Share Bot

import asyncio
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from pyrogram import Client, filters, raw
from pymongo.errors import DuplicateKeyError
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo

from config import (
    API_HASH,
    APP_ID,
    AUTO_REPOST_ENABLED,
    DEEPLINK_TIMEOUT,
    DOWNLOAD_DIR,
    LOGGER,
    SESSION_STRING,
    SOURCE_BOT_RATE_LIMIT,
    SOURCE_BOT_RATE_WINDOW,
)
from database.database import (
    database,
    create_batch,
    get_deeplink_mapping,
    get_setting,
    get_userbot_session,
    get_targets_for_source,
    mark_pair_processed,
    mark_pair_error,
    mark_pair_skipped,
    save_deeplink_mapping,
    save_file,
)
from helper_func import encode

logger = LOGGER(__name__)

DEEPLINK_RE = re.compile(
    r"(?P<link>(?:https?://)?(?:t\.me|telegram\.me)/(?P<bot>[A-Za-z0-9_]+)\?start=(?P<param>[A-Za-z0-9_\-=]+)|@(?P<atbot>[A-Za-z0-9_]+)\?start=(?P<atparam>[A-Za-z0-9_\-=]+))",
    re.IGNORECASE,
)


class RateLimiter:
    def __init__(self):
        self.requests = {}

    async def wait(self, key):
        now = time.time()
        bucket = self.requests.setdefault(key, [])
        self.requests[key] = [item for item in bucket if now - item < SOURCE_BOT_RATE_WINDOW]
        if len(self.requests[key]) >= SOURCE_BOT_RATE_LIMIT:
            wait_for = SOURCE_BOT_RATE_WINDOW - (now - self.requests[key][0])
            await asyncio.sleep(max(wait_for, 1))
        self.requests[key].append(time.time())


rate_limiter = RateLimiter()
repost_locks_col = database["repost_locks"]
repost_locks_col.create_index("key", unique=True)
target_posts_col = database["target_posts"]
target_posts_col.create_index("key", unique=True)


async def acquire_repost_lock(key):
    stale_before = datetime.utcnow() - timedelta(minutes=20)
    repost_locks_col.delete_many({"status": "processing", "created_at": {"$lt": stale_before}})

    existing = repost_locks_col.find_one({"key": key})
    if existing:
        if existing.get("status") == "processing":
            return False
        repost_locks_col.delete_one({"key": key})

    try:
        repost_locks_col.insert_one({
            "key": key,
            "status": "processing",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        })
        return True
    except DuplicateKeyError:
        return False


async def finish_repost_lock(key, status="done"):
    repost_locks_col.update_one(
        {"key": key},
        {"$set": {"status": status, "updated_at": datetime.utcnow()}},
        upsert=True,
    )


async def reserve_target_post(key):
    stale_before = datetime.utcnow() - timedelta(minutes=30)
    existing = target_posts_col.find_one({"key": key})
    if existing:
        status = existing.get("status")
        if status == "done":
            return "done"
        if status == "sending" and existing.get("updated_at", existing.get("created_at", datetime.utcnow())) > stale_before:
            return "sending"
        target_posts_col.delete_one({"key": key})

    try:
        target_posts_col.insert_one({
            "key": key,
            "status": "sending",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        })
        return "reserved"
    except DuplicateKeyError:
        return "sending"


async def finish_target_post(key, status="done", error=None):
    update = {"status": status, "updated_at": datetime.utcnow()}
    if error:
        update["error"] = str(error)[:500]
    target_posts_col.update_one({"key": key}, {"$set": update}, upsert=True)


processing_deeplinks = set()
processing_posts = set()


def extract_deeplinks(text):
    links = []
    for match in DEEPLINK_RE.finditer(text or ""):
        bot = match.group("bot") or match.group("atbot")
        param = match.group("param") or match.group("atparam")
        links.append({"full_link": match.group("link"), "bot": bot, "param": param})
    return links


def extract_channel_from_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if parsed.netloc not in ("t.me", "telegram.me"):
        return None
    if not path:
        return None
    # Private invite and request links must be passed as full links to join_chat.
    if path.startswith("+") or path.startswith("joinchat/"):
        return url
    channel = path.split("/")[0]
    if channel in ("c", "s"):
        return None
    return channel


def extract_join_urls(text):
    found = []
    for match in re.finditer(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+|[A-Za-z0-9_]{5,})(?:\?[^\s]+)?", text or "", re.IGNORECASE):
        url = match.group(0)
        if "start=" in url.lower():
            continue
        if not url.startswith("http"):
            url = "https://" + url
        target = extract_channel_from_url(url)
        if target:
            found.append(target)
    return found


def media_type(message):
    for attr in ("photo", "video", "document", "audio", "voice", "animation"):
        if getattr(message, attr, None):
            return attr
    return None


GATE_WORDS = ("join", "subscribe", "channel", "required", "request", "must", "verify", "check", "first")


def message_text(message):
    text = ""
    if getattr(message, "text", None):
        text = message.text.html if hasattr(message.text, "html") else str(message.text)
    elif getattr(message, "caption", None):
        text = message.caption.html if hasattr(message.caption, "html") else str(message.caption)
    return text or ""


def button_join_targets(message):
    targets = []
    markup = getattr(message, "reply_markup", None)
    if not markup:
        return targets
    for row in getattr(markup, "inline_keyboard", []) or []:
        for button in row:
            url = getattr(button, "url", None)
            channel = extract_channel_from_url(url) if url else None
            if channel:
                targets.append(channel)
    return targets


def looks_like_gate_message(message):
    raw_text = message_text(message)
    text = raw_text.lower()
    return bool(button_join_targets(message) or any(word in text for word in GATE_WORDS))


def is_real_delivered_file(message):
    mtype = media_type(message)
    if not mtype:
        return False
    # Source bots often attach "next/page/download" inline buttons below the
    # delivered video. Once video arrives, ignore those buttons and process it.
    if mtype == "video":
        return True
    if looks_like_gate_message(message):
        return False
    return True


def file_name(message):
    for attr in ("document", "video", "audio", "animation"):
        media = getattr(message, attr, None)
        if media and getattr(media, "file_name", None):
            return media.file_name
    return None


def video_kwargs_from_message(message):
    video = getattr(message, "video", None)
    if not video:
        return {}
    kwargs = {"supports_streaming": True}
    if getattr(video, "duration", None):
        kwargs["duration"] = int(video.duration)
    if getattr(video, "width", None):
        kwargs["width"] = int(video.width)
    if getattr(video, "height", None):
        kwargs["height"] = int(video.height)
    return kwargs


async def download_media_verified(userbot, message, attempts=3):
    last_path = None
    for attempt in range(attempts):
        path = None
        try:
            path = await asyncio.wait_for(userbot.download_media(message, file_name=f"{DOWNLOAD_DIR}/"), timeout=1800)
            last_path = path
            if path and os.path.exists(path) and os.path.getsize(path) > 0:
                logger.info("Downloaded media message %s to %s size=%s", getattr(message, "id", None), path, os.path.getsize(path))
                return path
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        except Exception as exc:
            logger.exception("download_media attempt %s failed for message %s", attempt + 1, getattr(message, "id", None))
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        await asyncio.sleep(2 + attempt * 2)
    raise RuntimeError(f"download_media produced empty file for message {getattr(message, 'id', None)} path={last_path}")


async def collect_recent_messages(userbot, bot_username, since_ts, limit=25):
    messages = []
    async for msg in userbot.get_chat_history(bot_username, limit=limit):
        if msg.date and msg.date.timestamp() >= since_ts:
            messages.append(msg)
    return list(reversed(messages))


def invite_hash_from_target(target):
    parsed = urlparse(target)
    path = parsed.path.strip("/")
    if path.startswith("+"):
        return path[1:]
    if path.startswith("joinchat/"):
        return path.split("/", 1)[1]
    return None


async def join_one_target(userbot, target):
    try:
        if isinstance(target, str) and not target.startswith("http") and target.lower().endswith("bot"):
            logger.info("Skipping bot username as force-sub target: %s", target)
            return "failed"
        invite_hash = invite_hash_from_target(target)
        if invite_hash:
            await userbot.invoke(raw.functions.messages.ImportChatInvite(hash=invite_hash))
        else:
            await userbot.join_chat(target)
        logger.info("Joined force-sub target: %s", target)
        await asyncio.sleep(1)
        return "joined"
    except FloodWait as exc:
        logger.warning("FloodWait while joining %s: %s", target, exc.value)
        if exc.value > 60:
            logger.warning("Skipping long FloodWait target %s for now; will retry on future posts", target)
            return "requested"
        await asyncio.sleep(exc.value + 2)
        return await join_one_target(userbot, target)
    except Exception as exc:
        text = str(exc)
        lowered = text.lower()
        if "already" in lowered or "user_already_participant" in lowered:
            logger.info("Already joined force-sub target: %s", target)
            return "joined"
        if "request" in lowered or "invite_request" in lowered or "invite_request_sent" in lowered:
            logger.info("Join request sent for force-sub target: %s", target)
            await asyncio.sleep(3)
            return "requested"
        logger.warning("Failed joining %s: %s", target, exc)
        return "failed"


async def click_force_sub_buttons(msg, clicked_message_ids=None):
    clicked_message_ids = clicked_message_ids if clicked_message_ids is not None else set()
    msg_id = getattr(msg, "id", None)
    if msg_id in clicked_message_ids:
        return 0
    markup = getattr(msg, "reply_markup", None)
    if not markup:
        return 0
    raw_text = message_text(msg).lower()
    if not any(word in raw_text for word in GATE_WORDS):
        return 0
    clicked = 0
    for row_index, row in enumerate(getattr(markup, "inline_keyboard", []) or []):
        for col_index, button in enumerate(row):
            if getattr(button, "url", None):
                continue
            try:
                await msg.click(row_index, col_index)
                clicked += 1
                await asyncio.sleep(1)
            except FloodWait as exc:
                await asyncio.sleep(exc.value + 2)
            except Exception as exc:
                logger.info("Callback button click skipped on message %s: %s", msg_id, exc)
    if clicked:
        clicked_message_ids.add(msg_id)
    return clicked


async def join_force_sub_channels(userbot, messages, seen_targets=None, clicked_message_ids=None):
    results = {"joined": 0, "requested": 0, "failed": 0, "clicked": 0}
    seen_targets = seen_targets if seen_targets is not None else set()
    clicked_message_ids = clicked_message_ids if clicked_message_ids is not None else set()
    for msg in messages:
        raw_text = message_text(msg)
        looks_like_gate = looks_like_gate_message(msg)
        targets = extract_join_urls(raw_text)
        targets.extend(button_join_targets(msg))
        if not targets and not looks_like_gate:
            continue
        for target in targets:
            if target in seen_targets:
                continue
            seen_targets.add(target)
            status = await join_one_target(userbot, target)
            if status in results:
                results[status] += 1
        if looks_like_gate:
            results["clicked"] += await click_force_sub_buttons(msg, clicked_message_ids)
    return results


async def send_start_to_source_bot(userbot, bot_username, start_param):
    try:
        await userbot.send_message(bot_username, f"/start {start_param}")
    except FloodWait as exc:
        await asyncio.sleep(exc.value + 2)
        await userbot.send_message(bot_username, f"/start {start_param}")


async def smart_file_extraction(userbot, bot_username, start_param):
    await rate_limiter.wait(bot_username)
    since_ts = time.time()
    seen_targets = set()
    clicked_message_ids = set()
    start_sent_after_gate = False
    timeout = max(DEEPLINK_TIMEOUT, 90)
    deadline = time.time() + timeout

    await send_start_to_source_bot(userbot, bot_username, start_param)
    poll_delay = 1
    while time.time() < deadline:
        try:
            messages = await asyncio.wait_for(collect_recent_messages(userbot, bot_username, since_ts, limit=80), timeout=20)
        except Exception as exc:
            logger.warning("Source bot %s history poll timed out/failed: %s", bot_username, exc)
            await asyncio.sleep(poll_delay)
            continue
        files = [msg for msg in messages if is_real_delivered_file(msg)]
        if files:
            logger.info("Source bot %s returned %s real file messages; starting conversion now", bot_username, len(files))
            return files

        results = await join_force_sub_channels(userbot, messages, seen_targets, clicked_message_ids)
        gate_actions = results["joined"] + results["requested"] + results["clicked"]
        if gate_actions:
            logger.info("Force-sub handled for %s: %s", bot_username, results)
            if not start_sent_after_gate:
                await asyncio.sleep(2)
                await send_start_to_source_bot(userbot, bot_username, start_param)
                start_sent_after_gate = True
            poll_delay = 1
        else:
            poll_delay = min(poll_delay + 1, 6)
        await asyncio.sleep(poll_delay)

    logger.warning("Source bot %s did not return files after adaptive force-sub handling", bot_username)
    return []


async def get_bot_username(bot):
    username = getattr(bot, "username", None)
    if username:
        return username
    me = await bot.get_me()
    username = me.username
    try:
        bot.username = username
    except Exception:
        pass
    return username


async def upload_downloaded_file(bot, path, source_msg, original_deeplink, source_bot):
    mtype = media_type(source_msg)
    caption = source_msg.caption.html if source_msg.caption else None
    kwargs = {"chat_id": bot.db_channel.id, "caption": caption, "parse_mode": ParseMode.HTML}
    if mtype == "photo":
        sent = await bot.send_photo(photo=path, **kwargs)
        file_id = sent.photo.file_id
    elif mtype == "video":
        sent = await bot.send_video(video=path, **kwargs, **video_kwargs_from_message(source_msg))
        file_id = sent.video.file_id
    elif mtype == "audio":
        sent = await bot.send_audio(audio=path, **kwargs)
        file_id = sent.audio.file_id
    elif mtype == "animation":
        sent = await bot.send_animation(animation=path, **kwargs)
        file_id = sent.animation.file_id
    else:
        sent = await bot.send_document(document=path, **kwargs)
        file_id = sent.document.file_id

    db_id = await save_file(
        file_id=file_id,
        file_type=mtype or "document",
        original_deeplink=original_deeplink,
        source_bot=source_bot,
        file_name=file_name(source_msg),
    )
    converted_id = sent.id * abs(bot.db_channel.id)
    payload = await encode(f"get-{converted_id}")
    bot_username = await get_bot_username(bot)
    return db_id, f"https://t.me/{bot_username}?start={payload}"


async def direct_save_fileid(bot, msg, original_deeplink, source_bot):
    try:
        logger.info("Copying source-bot media message %s to DB channel %s", getattr(msg, "id", None), bot.db_channel.id)
        copied = await asyncio.wait_for(msg.copy(bot.db_channel.id), timeout=45)
        logger.info("Copied source-bot media message %s to DB channel message %s", getattr(msg, "id", None), getattr(copied, "id", None))
        copied_msg = await asyncio.wait_for(bot.get_messages(bot.db_channel.id, copied.id), timeout=20)
        logger.info("Fetched copied DB message %s for file_id save", getattr(copied_msg, "id", None))
    except Exception as exc:
        logger.warning("Copy restricted/failed for message %s, falling back to download+upload: %s", getattr(msg, "id", None), exc)
        path = None
        try:
            Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
            logger.info("Starting restricted media download fallback for message %s", getattr(msg, "id", None))
            path = await download_media_verified(msg._client, msg)
            logger.info("Finished restricted media download fallback for message %s path=%s", getattr(msg, "id", None), path)
            db_id, link = await upload_downloaded_file(bot, path, msg, original_deeplink, source_bot)
            logger.info("Uploaded restricted media fallback for message %s", getattr(msg, "id", None))
            return db_id, link
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    copied_type = media_type(copied_msg) or media_type(msg)
    media_obj = getattr(copied_msg, copied_type, None) if copied_type else None

    if copied_type == "photo":
        file_id = copied_msg.photo.file_id
    elif copied_type == "video":
        file_id = copied_msg.video.file_id
    elif copied_type == "audio":
        file_id = copied_msg.audio.file_id
    elif copied_type == "animation":
        file_id = copied_msg.animation.file_id
    else:
        file_id = copied_msg.document.file_id

    db_id = await save_file(
        file_id=file_id,
        file_type=copied_type or "document",
        original_deeplink=original_deeplink,
        source_bot=source_bot,
        file_name=file_name(copied_msg) or file_name(msg),
        file_size=getattr(media_obj, "file_size", None),
    )

    converted_id = copied_msg.id * abs(bot.db_channel.id)
    payload = await encode(f"get-{converted_id}")
    bot_username = await get_bot_username(bot)
    return db_id, f"https://t.me/{bot_username}?start={payload}"


async def reupload_and_generate_link(bot, userbot, messages, original_deeplink, source_bot):
    stored = []
    generated_links = []
    for msg in messages:
        try:
            db_id, link = await direct_save_fileid(bot, msg, original_deeplink, source_bot)
            stored.append(db_id)
            generated_links.append(link)
        except Exception as exc:
            logger.exception("Direct file_id save failed: %s", exc)

    if not stored:
        return None
    if len(stored) == 1:
        return generated_links[0]
    batch_id = await create_batch(stored, original_deeplink)
    payload = await encode(f"batch-{batch_id}")
    bot_username = await get_bot_username(bot)
    return f"https://t.me/{bot_username}?start={payload}"


async def process_deeplink(bot, userbot, link):
    full_link = link["full_link"]
    cached = await get_deeplink_mapping(full_link)
    if cached:
        return cached["new_deeplink"]

    got_lock = await acquire_repost_lock(full_link)
    if not got_lock:
        logger.info("Deeplink already processing, waiting up to 300s for cache: %s", full_link)
        for _ in range(300):
            await asyncio.sleep(1)
            cached = await get_deeplink_mapping(full_link)
            if cached:
                return cached["new_deeplink"]
        return None

    try:
        cached = await get_deeplink_mapping(full_link)
        if cached:
            await finish_repost_lock(full_link, "done")
            return cached["new_deeplink"]

        try:
            files = await asyncio.wait_for(smart_file_extraction(userbot, link["bot"], link["param"]), timeout=300)
        except Exception as exc:
            logger.warning("Deeplink processing timed out/failed for %s: %s", full_link, exc)
            await finish_repost_lock(full_link, "failed")
            return None

        if not files:
            await finish_repost_lock(full_link, "failed")
            return None

        new_link = await reupload_and_generate_link(bot, userbot, files, full_link, link["bot"])
        if new_link:
            await save_deeplink_mapping(full_link, new_link, link["bot"], len(files))
            await finish_repost_lock(full_link, "done")
            return new_link

        await finish_repost_lock(full_link, "failed")
        return None
    except Exception:
        await finish_repost_lock(full_link, "failed")
        raise


async def get_source_album(userbot, message):
    if not getattr(message, "media_group_id", None):
        return [message]
    try:
        album = await userbot.get_media_group(message.chat.id, message.id)
        return sorted(album, key=lambda item: item.id)
    except Exception as exc:
        logger.warning("Could not fetch media group for message %s: %s", getattr(message, "id", None), exc)
        return [message]


def caption_from_messages(messages):
    for item in messages:
        text = item.caption.html if item.caption else item.text.html if item.text else ""
        if extract_deeplinks(text):
            return text
    for item in messages:
        text = item.caption.html if item.caption else item.text.html if item.text else ""
        if text:
            return text
    return ""


def input_media_for_path(mtype, path, caption=None):
    if mtype == "photo":
        return InputMediaPhoto(media=path, caption=caption, parse_mode=ParseMode.HTML)
    if mtype == "video":
        return InputMediaVideo(media=path, caption=caption, parse_mode=ParseMode.HTML)
    if mtype == "audio":
        return InputMediaAudio(media=path, caption=caption, parse_mode=ParseMode.HTML)
    return InputMediaDocument(media=path, caption=caption, parse_mode=ParseMode.HTML)


async def process_source_message(bot, userbot, message, pairs):
    source_messages = await get_source_album(userbot, message)
    caption = caption_from_messages(source_messages)
    raw_links = extract_deeplinks(caption)
    links = []
    seen_full_links = set()
    for item in raw_links:
        if item["full_link"] in seen_full_links:
            continue
        seen_full_links.add(item["full_link"])
        links.append(item)
    if not links:
        group_last_id = max(item.id for item in source_messages)
        logger.info("Auto repost skipped message %s: no deeplink", getattr(message, "id", None))
        for pair in pairs:
            last_message_id = pair.get("last_message_id")
            if last_message_id and group_last_id <= int(last_message_id):
                continue
            await mark_pair_skipped(pair["_id"], group_last_id, "no deeplink")
        return 0

    replacements = {}
    for link in links:
        new_link = await process_deeplink(bot, userbot, link)
        if new_link:
            replacements[link["full_link"]] = new_link

    if not replacements:
        logger.warning("Auto repost skipped message %s: deeplink conversion failed", getattr(message, "id", None))
        group_last_id = max(item.id for item in source_messages)
        for pair in pairs:
            start_message_id = pair.get("start_message_id")
            last_message_id = pair.get("last_message_id")
            if start_message_id and group_last_id < int(start_message_id):
                continue
            if last_message_id and group_last_id <= int(last_message_id):
                continue
            await mark_pair_skipped(pair["_id"], group_last_id, "deeplink conversion failed")
        return 0

    new_caption = caption
    for old, new in replacements.items():
        new_caption = new_caption.replace(old, new)

    group_last_id = max(item.id for item in source_messages)
    group_first_id = min(item.id for item in source_messages)
    sent_count = 0
    for pair in pairs:
        start_message_id = pair.get("start_message_id")
        if start_message_id and group_last_id < int(start_message_id):
            continue
        last_message_id = pair.get("last_message_id")
        if last_message_id and group_last_id <= int(last_message_id):
            continue
        post_key = f"{pair['_id']}:{pair['source_channel']}:{pair['target_channel']}:{group_first_id}:{group_last_id}"
        reserve_status = await reserve_target_post(post_key)
        if reserve_status != "reserved":
            logger.info(
                "Skipping duplicate target post source=%s target=%s group=%s-%s status=%s",
                pair["source_channel"],
                pair["target_channel"],
                group_first_id,
                group_last_id,
                reserve_status,
            )
            continue
        try:
            await send_processed_post(bot, userbot, pair["target_channel"], source_messages, new_caption)
            await finish_target_post(post_key, "done")
            await mark_pair_processed(pair["_id"], group_last_id)
            sent_count += 1
        except Exception as exc:
            await finish_target_post(post_key, "failed", exc)
            await mark_pair_error(pair["_id"], exc, group_last_id)
            logger.exception("Failed posting to %s for source group %s-%s: %s", pair["target_channel"], group_first_id, group_last_id, exc)
    return sent_count


async def send_processed_post(bot, userbot, target_channel, messages, caption):
    if not isinstance(messages, list):
        messages = [messages]
    media_messages = [item for item in messages if media_type(item)]
    if not media_messages:
        await bot.send_message(target_channel, caption or "Processed post", parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        return

    Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
    paths = []
    try:
        for item in media_messages:
            mtype = media_type(item)
            path = await download_media_verified(userbot, item)
            path_obj = Path(path)
            if mtype == "photo" and not path_obj.suffix:
                fixed_path = str(path_obj.with_suffix(".jpg"))
                os.rename(path, fixed_path)
                path = fixed_path
            elif mtype == "video" and not path_obj.suffix:
                fixed_path = str(path_obj.with_suffix(".mp4"))
                os.rename(path, fixed_path)
                path = fixed_path
            paths.append((mtype, path, item))

        if len(paths) > 1:
            media = []
            for index, (mtype, path, item) in enumerate(paths):
                media_item = input_media_for_path(mtype, path, caption if index == 0 else None)
                if mtype == "video":
                    for key, value in video_kwargs_from_message(item).items():
                        setattr(media_item, key, value)
                media.append(media_item)
            await bot.send_media_group(target_channel, media)
            return

        mtype, path, item = paths[0]
        kwargs = {"chat_id": target_channel, "caption": caption, "parse_mode": ParseMode.HTML}
        if mtype == "photo":
            await bot.send_photo(photo=path, **kwargs)
        elif mtype == "video":
            await bot.send_video(video=path, **kwargs, **video_kwargs_from_message(item))
        elif mtype == "audio":
            await bot.send_audio(audio=path, **kwargs)
        elif mtype == "animation":
            await bot.send_animation(animation=path, **kwargs)
        else:
            await bot.send_document(document=path, **kwargs)
    except Exception as exc:
        ids = ",".join(str(getattr(item, "id", "")) for item in media_messages)
        logger.warning("Target media upload failed for messages %s, sending text fallback: %s", ids, exc)
        await bot.send_message(target_channel, caption or "Processed post", parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    finally:
        for _, path, _ in paths:
            try:
                os.remove(path)
            except OSError:
                pass


class AutoRepostWorker:
    def __init__(self, bot):
        self.bot = bot
        self.userbot = None
        self.poll_task = None

    async def start(self):
        session_string = SESSION_STRING or await get_userbot_session()
        db_enabled = await get_setting("auto_repost_enabled", False)
        if not (AUTO_REPOST_ENABLED or db_enabled) or not session_string:
            logger.info("Auto repost worker disabled. Set SESSION_STRING or add it from admin panel to enable.")
            return
        self.userbot = Client(
            "auto_repost_userbot",
            api_id=APP_ID,
            api_hash=API_HASH,
            session_string=session_string,
            sleep_threshold=180,
            in_memory=False,
        )
        self.userbot.add_handler(self._handler())
        await self.userbot.start()
        self.poll_task = asyncio.create_task(self._poll_sources())
        logger.info("Auto repost userbot started.")

    def _handler(self):
        from pyrogram.handlers import MessageHandler

        async def handle(client, message):
            pairs = await get_targets_for_source(message.chat.id)
            immediate_pairs = [
                pair for pair in pairs
                if pair.get("processing_mode") not in ("first", "latest")
            ]
            if immediate_pairs:
                await process_source_message(
                    self.bot, client, message, immediate_pairs
                )

        return MessageHandler(handle, filters.channel)

    async def _poll_sources(self):
        from datetime import datetime
        from database.database import (
            advance_pair_cursor,
            initialize_pair_latest,
            list_channel_pairs,
            mark_pair_skipped,
            set_pair_next_run,
        )

        await asyncio.sleep(5)
        while True:
            try:
                all_pairs = await list_channel_pairs(active_only=True)
                pairs = [
                    pair for pair in all_pairs
                    if pair.get("processing_mode") in ("first", "latest")
                ]
                pairs.sort(key=lambda pair: (
                    int(pair.get("processing_priority") or 2),
                    pair.get("next_run_at") or datetime.min,
                    pair.get("added_at") or datetime.min,
                ))

                for pair in pairs:
                    now = datetime.utcnow()
                    next_run = pair.get("next_run_at")
                    if next_run and next_run > now:
                        continue

                    source = int(pair["source_channel"])
                    mode = pair.get("processing_mode", "latest")
                    interval = int(pair.get("interval_minutes") or 60)

                    latest = None
                    async for item in self.userbot.get_chat_history(source, limit=1):
                        latest = item
                        break
                    if not latest:
                        continue

                    if mode == "latest" and not pair.get("schedule_initialized"):
                        await initialize_pair_latest(pair["_id"], latest.id)
                        pair["start_message_id"] = latest.id
                        pair["last_message_id"] = max(0, latest.id - 1)
                        pair["schedule_initialized"] = True

                    cursor = int(pair.get("last_message_id") or 0)
                    latest_id = int(latest.id)
                    if cursor >= latest_id:
                        continue

                    sent = False
                    checked = 0

                    while cursor < latest_id and checked < 25 and not sent:
                        ids = list(range(
                            cursor + 1,
                            min(cursor + 25, latest_id) + 1,
                        ))
                        fetched = await self.userbot.get_messages(
                            source, message_ids=ids
                        )
                        if not isinstance(fetched, list):
                            fetched = [fetched]

                        messages = sorted(
                            [
                                item for item in fetched
                                if getattr(item, "id", None)
                                and not getattr(item, "empty", False)
                            ],
                            key=lambda item: item.id,
                        )

                        if not messages:
                            cursor = ids[-1]
                            await advance_pair_cursor(
                                pair["_id"], cursor, "empty/deleted message"
                            )
                            checked += len(ids)
                            continue

                        for message in messages:
                            if message.id <= cursor:
                                continue
                            logger.info(
                                "Scheduled processing mode=%s source=%s "
                                "target=%s message=%s",
                                mode,
                                source,
                                pair["target_channel"],
                                message.id,
                            )
                            try:
                                count = await asyncio.wait_for(
                                    process_source_message(
                                        self.bot,
                                        self.userbot,
                                        message,
                                        [pair],
                                    ),
                                    timeout=300,
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    "Scheduled message timed out; skipping "
                                    "source=%s message=%s",
                                    source,
                                    message.id,
                                )
                                await mark_pair_skipped(
                                    pair["_id"],
                                    message.id,
                                    "processing timeout; skipped",
                                )
                                count = 0
                            except Exception as exc:
                                logger.exception(
                                    "Scheduled message failed; skipping "
                                    "source=%s message=%s: %s",
                                    source,
                                    message.id,
                                    exc,
                                )
                                await mark_pair_skipped(
                                    pair["_id"],
                                    message.id,
                                    f"processing error; skipped: {exc}",
                                )
                                count = 0

                            cursor = message.id
                            checked += 1
                            if count:
                                await set_pair_next_run(
                                    pair["_id"], interval
                                )
                                logger.info(
                                    "Scheduled target post completed; next "
                                    "run in %s minutes",
                                    interval,
                                )
                                sent = True
                                break

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Scheduled auto repost polling failed: %s", exc)

            await asyncio.sleep(10)

    async def stop(self):
        if self.poll_task:
            self.poll_task.cancel()
            try:
                await self.poll_task
            except asyncio.CancelledError:
                pass
        if self.userbot:
            await self.userbot.stop()


async def run_backfill(bot, limit=50):
    session_string = SESSION_STRING or await get_userbot_session()
    if not session_string:
        return {"ok": False, "error": "Userbot session missing", "processed": 0}

    total = 0
    details = []
    userbot = Client("auto_repost_backfill", api_id=APP_ID, api_hash=API_HASH, session_string=session_string, sleep_threshold=180, in_memory=False)
    await userbot.start()
    try:
        from database.database import list_channel_pairs
        sources = {}
        for pair in await list_channel_pairs(active_only=True):
            sources.setdefault(int(pair["source_channel"]), []).append(pair)

        for source, pairs in sources.items():
            min_start = min(int(p.get("start_message_id") or 0) for p in pairs)
            checked = 0
            processed = 0
            async for message in userbot.get_chat_history(source, limit=limit):
                checked += 1
                if min_start and message.id < min_start:
                    break
                count = await process_source_message(bot, userbot, message, pairs)
                processed += count
                total += count
            details.append(f"{source}: checked={checked}, sent={processed}")
    finally:
        await userbot.stop()
    return {"ok": True, "processed": total, "details": details}
