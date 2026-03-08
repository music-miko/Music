import asyncio
import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pyrogram import filters
from pyrogram.types import Message
from pytgcalls.types import JoinedGroupCallParticipant, LeftGroupCallParticipant, Update
from pytgcalls.types.stream import StreamAudioEnded

from config import Config
from Music.core.calls import hellmusic
from Music.core.clients import hellbot
from Music.core.database import db
from Music.core.logger import LOGS
from Music.helpers.buttons import Buttons
from Music.utils.leaderboard import leaders
from Music.utils.queue import Queue


@hellbot.app.on_message(filters.private, group=2)
async def new_users(_, msg: Message):
    chat_id = msg.from_user.id
    user_name = msg.from_user.first_name
    if not await db.is_user_exist(chat_id):
        BOT_USERNAME = hellbot.app.username
        await db.add_user(chat_id, user_name)
        if Config.LOGGER_ID:
            await hellbot.logit(
                "newuser",
                f"**⤷ User:** {msg.from_user.mention(style='md')}\n**⤷ ID:** `{chat_id}`\n__⤷ Started @{BOT_USERNAME} !!__",
            )
        else:
            LOGS.info(f"#NewUser: \n\nName: {user_name} \nID: {chat_id}")
    else:
        try:
            await db.update_user(chat_id, "user_name", user_name)
        except:
            pass
    await msg.continue_propagation()


@hellbot.app.on_message(filters.group, group=3)
async def new_group_message(_, msg: Message):
    chat_id = msg.chat.id
    if not await db.is_chat_exist(chat_id):
        BOT_USERNAME = hellbot.app.username
        await db.add_chat(chat_id)
        if Config.LOGGER_ID:
            await hellbot.logit(
                "newchat",
                f"**⤷ Chat Title:** {msg.chat.title} \n**⤷ Chat UN:** @{msg.chat.username or None}) \n**⤷ Chat ID:** `{chat_id}` \n__⤷ ADDED @{BOT_USERNAME} !!__",
            )
        else:
            LOGS.info(
                f"#NEWCHAT: \n\nChat Title: {msg.chat.title} \nChat UN: @{msg.chat.username}) \nChat ID: {chat_id} \n\nADDED @{BOT_USERNAME} !!",
            )
    await msg.continue_propagation()


@hellbot.app.on_message(filters.video_chat_ended, group=4)
async def vc_end(_, msg: Message):
    chat_id = msg.chat.id
    try:
        await hellmusic.leave_vc(chat_id)
        await db.set_loop(chat_id, 0)
    except:
        pass
    await msg.continue_propagation()


# Dynamically bind PyTgCalls listeners to ALL active assistant sessions
for music in hellmusic.musics:
    @music.on_kicked()
    @music.on_left()
    async def end_streaming(_, chat_id: int):
        await hellmusic.leave_vc(chat_id)
        await db.set_loop(chat_id, 0)

    @music.on_stream_end()
    async def changed(_, update: Update):
        if isinstance(update, StreamAudioEnded):
            await hellmusic.change_vc(update.chat_id)

    @music.on_participants_change()
    async def members_change(_, update: Update):
        if not isinstance(update, JoinedGroupCallParticipant) and not isinstance(
            update, LeftGroupCallParticipant
        ):
            return
        try:
            chat_id = update.chat_id
            audience = hellmusic.audience.get(chat_id)
            users = await hellmusic.vc_participants(chat_id)
            user_ids = [user.user_id for user in users]
            
            if not audience:
                await hellmusic.autoend(chat_id, user_ids)
            else:
                new = (
                    audience + 1
                    if isinstance(update, JoinedGroupCallParticipant)
                    else audience - 1
                )
                hellmusic.audience[chat_id] = new
                await hellmusic.autoend(chat_id, user_ids)
        except:
            return


async def update_played():
    while not await asyncio.sleep(1):
        active_chats = await db.get_active_vc()
        for x in active_chats:
            chat_id = int(x["chat_id"])
            if chat_id == 0:
                continue
            is_paused = await db.get_watcher(chat_id, "pause")
            if is_paused:
                continue
            que = Queue.get_queue(chat_id)
            if que == []:
                continue
            Queue.update_duration(chat_id, 1, 1)

asyncio.create_task(update_played())


# Restored `end_inactive_vc` task
async def end_inactive_vc():
    while not await asyncio.sleep(10):
        for chat_id in list(db.inactive.keys()):
            dur = db.inactive.get(chat_id)
            if not dur or dur == {}:
                continue
            if datetime.datetime.now() > dur:
                if not await db.is_active_vc(chat_id):
                    db.inactive[chat_id] = {}
                    continue
                db.inactive[chat_id] = {}
                try:
                    await hellmusic.leave_vc(chat_id)
                except:
                    continue
                try:
                    await hellbot.app.send_message(
                        chat_id,
                        "⏹️ **Inactive VC:** Streaming has been stopped!",
                    )
                except:
                    continue

asyncio.create_task(end_inactive_vc())


# ==========================================
# SCHEDULED MORNING SWEEP (4:35 AM)
# ==========================================

async def morning_inactive_sweep():
    """Fetches inactive chats from db and blindly leaves them at 4:35 AM"""
    inactive_chats = list(db.inactive.keys())
    
    for chat_id in inactive_chats:
        try:
            # Blindly force leave the Voice Chat
            await hellmusic.leave_vc(chat_id)
            
            # Remove from DB dictionary
            db.inactive.pop(chat_id, None)
            
            # EXCEPTION: Never leave the Logger Chat
            if str(chat_id) == str(Config.LOGGER_ID):
                continue
            
            # Make the assistant user account leave the group entirely
            assistant = hellbot.get_user(chat_id)
            if assistant:
                await assistant.leave_chat(chat_id)
                
            try:
                await hellbot.app.send_message(
                    chat_id,
                    "🌅 **Morning Sweep:** Left the chat to clear inactive resources."
                )
            except:
                pass
        except Exception:
            pass

# Initialize and start APScheduler
scheduler = AsyncIOScheduler()
# Set to run exactly at 04:35 AM server time daily
scheduler.add_job(morning_inactive_sweep, "cron", hour=4, minute=35)
scheduler.start()
