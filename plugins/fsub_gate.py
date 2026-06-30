from pyrogram import StopPropagation, filters
from pyrogram.types import Message

from admin_fsub_common import is_force_sub_globally_enabled, is_link_flow_enabled, send_force_sub_gate
from bot import Bot


@Bot.on_message(filters.command("start") & filters.private, group=-200)
async def force_sub_gate(client, message: Message):
    if not getattr(message, "from_user", None):
        return
    if not is_link_flow_enabled():
        return
    if not is_force_sub_globally_enabled():
        return
    try:
        blocked = await send_force_sub_gate(client, message)
    except Exception as exc:
        print(f"[force-sub gate] soft-fail: {exc}")
        return
    if blocked:
        raise StopPropagation
