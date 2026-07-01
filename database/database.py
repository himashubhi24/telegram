#(c) CodeXBotz / Advanced File Share Bot

from datetime import datetime, timedelta
import base64
from bson import ObjectId
import pymongo
import config

from config import DB_URI, DB_NAME, VERIFY_TTL_SECONDS


dbclient = pymongo.MongoClient(DB_URI)
database = dbclient[DB_NAME]

user_data = database["users"]
files_col = database["files"]
batches_col = database["batches"]
channels_col = database["channels"]
force_sub_col = database["force_sub_channels"]
verification_logs = database["verification_logs"]
deeplink_mappings = database["deeplink_mappings"]
settings_col = database["bot_settings"]

channels_col.create_index([("source_channel", 1), ("target_channel", 1)], unique=True)
files_col.create_index("created_at")
batches_col.create_index("created_at")
verification_logs.create_index("created_at")
deeplink_mappings.create_index("old_deeplink")


async def present_user(user_id: int):
    return bool(user_data.find_one({"_id": user_id}))


async def add_user(user_id: int, username=None, first_name=None):
    user_data.update_one(
        {"_id": user_id},
        {
            "$set": {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "last_accessed": datetime.utcnow(),
            },
            "$setOnInsert": {
                "joined_at": datetime.utcnow(),
                "verified": False,
                "total_downloads": 0,
            },
        },
        upsert=True,
    )


async def full_userbase():
    return [doc["_id"] for doc in user_data.find({}, {"_id": 1})]


async def del_user(user_id: int):
    user_data.delete_one({"_id": user_id})


async def mark_user_verified(user_id: int):
    user_data.update_one(
        {"_id": user_id},
        {"$set": {"verified": True, "verified_at": datetime.utcnow()}},
        upsert=True,
    )


async def is_user_verified(user_id: int):
    doc = user_data.find_one({"_id": user_id}, {"verified_at": 1, "verified": 1})
    if not doc or not doc.get("verified"):
        return False
    verified_at = doc.get("verified_at")
    if not verified_at:
        return False
    return datetime.utcnow() - verified_at < timedelta(seconds=VERIFY_TTL_SECONDS)


async def increment_downloads(user_id: int, count=1):
    user_data.update_one(
        {"_id": user_id},
        {"$inc": {"total_downloads": count}, "$set": {"last_accessed": datetime.utcnow()}},
        upsert=True,
    )


async def save_file(file_id, file_type, original_deeplink=None, source_bot=None, file_name=None, file_size=None):
    doc = {
        "file_id": file_id,
        "file_type": file_type,
        "file_name": file_name,
        "original_deeplink": original_deeplink,
        "source_bot": source_bot,
        "file_size": file_size,
        "created_at": datetime.utcnow(),
    }
    result = files_col.insert_one(doc)
    return str(result.inserted_id)


async def get_file(record_id):
    try:
        return files_col.find_one({"_id": ObjectId(record_id)})
    except Exception:
        return None


async def create_batch(file_ids, original_batch_link=None):
    object_ids = [ObjectId(fid) if isinstance(fid, str) else fid for fid in file_ids]
    result = batches_col.insert_one(
        {
            "file_ids": object_ids,
            "total_files": len(object_ids),
            "original_batch_link": original_batch_link,
            "created_at": datetime.utcnow(),
        }
    )
    return str(result.inserted_id)


async def get_batch(batch_id):
    try:
        batch = batches_col.find_one({"_id": ObjectId(batch_id)})
    except Exception:
        return None
    if not batch:
        return None
    batch["files"] = list(files_col.find({"_id": {"$in": batch.get("file_ids", [])}}))
    return batch


async def add_channel_pair(source_channel, target_channel, added_by):
    result = channels_col.update_one(
        {"source_channel": int(source_channel), "target_channel": int(target_channel)},
        {
            "$set": {"active": True, "updated_at": datetime.utcnow()},
            "$setOnInsert": {
                "source_channel": int(source_channel),
                "target_channel": int(target_channel),
                "added_by": added_by,
                "added_at": datetime.utcnow(),
                "total_posts_processed": 0,
            },
        },
        upsert=True,
    )
    return bool(result.acknowledged)


async def remove_channel_pair(source_channel, target_channel=None):
    query = {"source_channel": int(source_channel)}
    if target_channel is not None:
        query["target_channel"] = int(target_channel)
    return channels_col.delete_many(query).deleted_count


async def set_channel_pair_active(source_channel, target_channel, active):
    result = channels_col.update_one(
        {"source_channel": int(source_channel), "target_channel": int(target_channel)},
        {"$set": {"active": bool(active), "updated_at": datetime.utcnow()}},
    )
    return result.matched_count


async def set_channel_pair_start(source_channel, target_channel, start_message_id, updated_by=None):
    channels_col.update_one(
        {"source_channel": int(source_channel), "target_channel": int(target_channel)},
        {
            "$set": {
                "start_message_id": int(start_message_id),
                "updated_at": datetime.utcnow(),
                "updated_by": updated_by,
            }
        },
    )


async def configure_channel_pair_schedule(
    source_channel, target_channel, mode=None, interval_minutes=None, updated_by=None
):
    query = {
        "source_channel": int(source_channel),
        "target_channel": int(target_channel),
    }
    current = channels_col.find_one(query)
    if not current:
        raise ValueError("Source-target pair not found")

    selected_mode = mode or current.get("processing_mode") or "latest"
    interval = int(interval_minutes or current.get("interval_minutes") or 60)
    if selected_mode not in ("first", "latest"):
        raise ValueError("Invalid processing mode")
    if interval < 30 or interval > 1440:
        raise ValueError("Interval must be between 30 and 1440 minutes")

    update = {
        "$set": {
            "processing_mode": selected_mode,
            "processing_priority": 1 if selected_mode == "first" else 2,
            "interval_minutes": interval,
            "next_run_at": datetime.utcnow(),
            "active": True,
            "updated_at": datetime.utcnow(),
            "updated_by": updated_by,
        }
    }

    if mode == "first":
        update["$set"].update({
            "start_message_id": 1,
            "last_message_id": 0,
            "schedule_initialized": True,
        })
    elif mode == "latest":
        update["$set"]["schedule_initialized"] = False
        update["$unset"] = {
            "start_message_id": "",
            "last_message_id": "",
        }

    channels_col.update_one(query, update)


async def initialize_pair_latest(pair_id, latest_message_id):
    latest_message_id = int(latest_message_id)
    channels_col.update_one(
        {"_id": pair_id},
        {"$set": {
            "start_message_id": latest_message_id,
            "last_message_id": max(0, latest_message_id - 1),
            "schedule_initialized": True,
            "next_run_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }},
    )


async def set_pair_next_run(pair_id, interval_minutes):
    channels_col.update_one(
        {"_id": pair_id},
        {"$set": {
            "next_run_at": datetime.utcnow() + timedelta(minutes=int(interval_minutes)),
            "updated_at": datetime.utcnow(),
        }},
    )


async def advance_pair_cursor(pair_id, message_id, reason=None):
    values = {
        "last_message_id": int(message_id),
        "updated_at": datetime.utcnow(),
    }
    if reason:
        values["last_error"] = str(reason)[:500]
        values["last_error_at"] = datetime.utcnow()
    channels_col.update_one({"_id": pair_id}, {"$set": values})


async def list_channel_pairs(active_only=False):
    query = {"active": True} if active_only else {}
    return list(channels_col.find(query).sort("added_at", -1))


async def get_targets_for_source(source_channel):
    return list(channels_col.find({"source_channel": int(source_channel), "active": True}))


async def mark_pair_processed(pair_id, message_id=None):
    update = {"$inc": {"total_posts_processed": 1}, "$set": {"last_post_at": datetime.utcnow(), "last_error": None}}
    if message_id is not None:
        update["$set"]["last_message_id"] = int(message_id)
    channels_col.update_one({"_id": pair_id}, update)


async def mark_pair_error(pair_id, error, message_id=None):
    update = {"last_error": str(error)[:500], "last_error_at": datetime.utcnow()}
    if message_id is not None:
        update["last_error_message_id"] = int(message_id)
    channels_col.update_one({"_id": pair_id}, {"$set": update})


async def mark_pair_skipped(pair_id, message_id, reason):
    channels_col.update_one(
        {"_id": pair_id},
        {
            "$set": {
                "last_message_id": int(message_id),
                "last_error": str(reason)[:500],
                "last_error_at": datetime.utcnow(),
                "last_post_at": datetime.utcnow(),
            }
        },
    )


async def add_force_sub_channel(channel_id, username=None):
    result = force_sub_col.update_one(
        {"channel_id": int(channel_id)},
        {
            "$set": {"channel_username": username, "active": True},
            "$setOnInsert": {"added_at": datetime.utcnow()},
        },
        upsert=True,
    )
    return bool(result.acknowledged)


async def remove_force_sub_channel(channel_id):
    result = force_sub_col.update_one({"channel_id": int(channel_id)}, {"$set": {"active": False}})
    return result.matched_count


async def list_force_sub_channels():
    return list(force_sub_col.find({"active": True}))


async def save_verification_click(user_id, token, ip_address=None):
    verification_logs.update_one(
        {"token": token},
        {
            "$set": {
                "user_id": int(user_id),
                "click_timestamp": datetime.utcnow(),
                "ip_address": ip_address,
                "created_at": datetime.utcnow(),
                "verified": False,
            }
        },
        upsert=True,
    )


async def save_verification_return(user_id, token, min_seconds):
    doc = verification_logs.find_one({"token": token, "user_id": int(user_id)})
    now = datetime.utcnow()
    if not doc or not doc.get("click_timestamp"):
        return False, 0
    spent = (now - doc["click_timestamp"]).total_seconds()
    verified = spent >= min_seconds
    verification_logs.update_one(
        {"token": token},
        {"$set": {"return_timestamp": now, "time_spent": spent, "verified": verified}},
    )
    if verified:
        await mark_user_verified(user_id)
    return verified, spent


async def save_deeplink_mapping(old_deeplink, new_deeplink, source_bot, file_count):
    deeplink_mappings.update_one(
        {"old_deeplink": old_deeplink},
        {
            "$set": {
                "new_deeplink": new_deeplink,
                "source_bot": source_bot,
                "file_count": file_count,
                "created_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )


async def get_deeplink_mapping(old_deeplink):
    return deeplink_mappings.find_one({"old_deeplink": old_deeplink})


async def set_setting(key, value, updated_by=None):
    settings_col.update_one(
        {"setting_key": key},
        {"$set": {"setting_value": value, "updated_at": datetime.utcnow(), "updated_by": updated_by}},
        upsert=True,
    )


async def get_setting(key, default=None):
    doc = settings_col.find_one({"setting_key": key})
    return doc.get("setting_value") if doc else default


async def get_userbot_session():
    current_bot_id = _configured_bot_id()
    if not current_bot_id:
        return None
    session = await get_setting(f"session_string:{current_bot_id}", None)
    if not session:
        return None
    try:
        padded = str(session) + ("=" * (-len(str(session)) % 4))
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception:
        return None
    return str(session) if len(decoded) >= 250 else None


def _configured_bot_id():
    for name in ("TG_BOT_TOKEN", "BOT_TOKEN", "TOKEN"):
        token = str(getattr(config, name, "") or "")
        if ":" not in token:
            continue
        try:
            return int(token.split(":", 1)[0])
        except ValueError:
            continue
    return 0


async def save_userbot_session(session, updated_by=None):
    bot_id = _configured_bot_id()
    if not bot_id:
        raise RuntimeError("Current bot identity is unavailable")
    await set_setting(f"session_string:{bot_id}", str(session), updated_by)
    await set_setting(f"session_bot_id:{bot_id}", bot_id, updated_by)


async def get_auto_repost_enabled():
    bot_id = _configured_bot_id()
    if not bot_id:
        return False
    value = await get_setting(f"auto_repost_enabled:{bot_id}", None)
    if value is not None:
        return bool(value)
    if await get_userbot_session() and await get_setting("auto_repost_enabled", False):
        return True
    return False


async def set_auto_repost_enabled(enabled, updated_by=None):
    bot_id = _configured_bot_id()
    if not bot_id:
        raise RuntimeError("Current bot identity is unavailable")
    await set_setting(f"auto_repost_enabled:{bot_id}", bool(enabled), updated_by)


async def clear_userbot_session(updated_by=None):
    bot_id = _configured_bot_id()
    if not bot_id:
        raise RuntimeError("Current bot identity is unavailable")
    settings_col.delete_many({"setting_key": {"$in": [f"session_string:{bot_id}", f"session_bot_id:{bot_id}"]}})
    await set_auto_repost_enabled(False, updated_by)


async def get_bot_statistics():
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "total_users": user_data.count_documents({}),
        "verified_users": user_data.count_documents({"verified": True}),
        "total_files": files_col.count_documents({}),
        "total_batches": batches_col.count_documents({}),
        "active_channels": channels_col.count_documents({"active": True}),
        "today_users": user_data.count_documents({"joined_at": {"$gte": today}}),
        "today_verifications": verification_logs.count_documents({"return_timestamp": {"$gte": today}, "verified": True}),
    }
