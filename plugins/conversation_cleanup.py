import asyncio

from pyrogram import filters

from admin_fsub_common import _bot_api_request
from bot import Bot


CLEANUP_DELAY_SECONDS = 300
CLEANUP_MESSAGE_SPAN = 600
CLEANUP_CHUNK_SIZE = 100
CLEANUP_TASKS = set()


async def _delete_start_session(client, chat_id, start_message_id):
    await asyncio.sleep(CLEANUP_DELAY_SECONDS)
    end_message_id = start_message_id + CLEANUP_MESSAGE_SPAN
    for first_id in range(start_message_id, end_message_id + 1, CLEANUP_CHUNK_SIZE):
        message_ids = list(range(first_id, min(first_id + CLEANUP_CHUNK_SIZE, end_message_id + 1)))
        try:
            await _bot_api_request(
                "deleteMessages",
                {"chat_id": chat_id, "message_ids": message_ids},
            )
        except Exception as bot_api_exc:
            try:
                await client.delete_messages(chat_id, message_ids, revoke=True)
            except TypeError:
                try:
                    await client.delete_messages(chat_id, message_ids)
                except Exception as exc:
                    print(f"[auto delete] failed chat={chat_id} ids={first_id}-{message_ids[-1]}: {bot_api_exc}; {exc}")
            except Exception as exc:
                print(f"[auto delete] failed chat={chat_id} ids={first_id}-{message_ids[-1]}: {bot_api_exc}; {exc}")
        await asyncio.sleep(0.05)


@Bot.on_message(filters.command("start") & filters.private, group=-1000)
async def schedule_start_session_cleanup(client, message):
    if not getattr(message, "id", None):
        return
    task = asyncio.create_task(_delete_start_session(client, message.chat.id, message.id))
    CLEANUP_TASKS.add(task)
    task.add_done_callback(CLEANUP_TASKS.discard)
