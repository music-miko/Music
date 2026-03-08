import asyncio
from pyrogram import filters
from pyrogram.types import CallbackQuery, Message

from config import Config
from Music.core.clients import hellbot
from Music.core.decorators import UserWrapper, check_mode
from Music.helpers.formatters import formatter
from Music.utils.pages import MakePages
from Music.utils.youtube import ytube


# === BACKGROUND AUTO-DELETE TASK ===
async def auto_delete_message(message: Message, delay: int):
    """Wait for the specified delay (in seconds), then delete the message."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


@hellbot.app.on_message(filters.command("song") & ~Config.BANNED_USERS)
@check_mode
@UserWrapper
async def songs(_, message: Message):
    if len(message.command) == 1:
        return await message.reply_text("Nothing given to search.")
    query = message.text.split(None, 1)[1]
    hell = await message.reply_photo(
        Config.BLACK_IMG, caption=f"<b><i>Searching</i></b> “`{query}`” ..."
    )
    all_tracks = await ytube.get_data(query, False, 10)
    
    # Safety check to prevent IndexError
    if not all_tracks:
        return await hell.edit_text("❌ **No results found for your query. Try searching with different keywords.**")
        
    rand_key = formatter.gen_key(str(message.from_user.id), 5)
    Config.SONG_CACHE[rand_key] = all_tracks
    await MakePages.song_page(hell, rand_key, 0)


@hellbot.app.on_message(filters.command("lyrics") & ~Config.BANNED_USERS)
@check_mode
@UserWrapper
async def lyrics(_, message: Message):
    if not Config.LYRICS_API:
        return await message.reply_text("Lyrics module is disabled!")
    lists = message.text.split(" ", 1)
    if not len(lists) == 2:
        return await message.reply_text("Nothing given to search.")
    query = lists[1]
    hell = await message.reply_photo(
        Config.BLACK_IMG, caption=f"<b><i>Searching</i></b> “`{query}`” ..."
    )
    all_tracks = await ytube.get_data(query, False, 1)
    
    # Safety check to prevent IndexError
    if not all_tracks:
        return await hell.edit_text("❌ **No results found for your query.**")
        
    track = all_tracks[0]
    title = track["title"]
    artist = track["channel"]
    link = track["link"]
    lyrics = ytube.get_lyrics(title, artist)
    
    if not lyrics:
         return await hell.edit_text("❌ **Failed to fetch lyrics.**")

    final = f"**⤷ Title:** `{lyrics['title']}`\n\n**⤷ Lyrics:**\n\n{lyrics['lyrics']}"
    if len(final) > 4096:
        final = final[:4090] + "..."
        await hell.edit_text(
            f"{final}\n\n**[Read Full Lyrics Here]({link})**",
            disable_web_page_preview=True,
        )
    else:
        await hell.edit_text(final)
    chat = message.chat.title or message.chat.first_name
    await hellbot.logit(
        "lyrics",
        f"**⤷ Lyrics:** `{title}`\n**⤷ Chat:** {chat} [`{message.chat.id}`]\n**⤷ User:** {message.from_user.mention} [`{message.from_user.id}`]",
    )


@hellbot.app.on_callback_query(filters.regex(r"song_dl(.*)$") & ~Config.BANNED_USERS)
async def song_cb(_, cb: CallbackQuery):
    _, action, key, rand_key = cb.data.split("|")
    user = rand_key.split("_")[0]
    key = int(key)
    if cb.from_user.id != int(user):
        await cb.answer("You are not allowed to do that!", show_alert=True)
        return
        
    if action == "adl":
        sent_msg = await ytube.send_song(cb, rand_key, key, False)
        if sent_msg:
            # Trigger background deletion after 300 seconds (5 minutes)
            asyncio.create_task(auto_delete_message(sent_msg, 300))
        return
    elif action == "vdl":
        sent_msg = await ytube.send_song(cb, rand_key, key, True)
        if sent_msg:
            # Trigger background deletion after 300 seconds (5 minutes)
            asyncio.create_task(auto_delete_message(sent_msg, 300))
        return
    elif action == "close":
        Config.SONG_CACHE.pop(rand_key, None)
        await cb.message.delete()
        return
    else:
        all_tracks = Config.SONG_CACHE.get(rand_key)
        if not all_tracks:
            return await cb.answer("Cache expired. Please search again.", show_alert=True)
            
        if action == "next":
            await MakePages.song_page(cb.message, rand_key, key + 1)
        elif action == "prev":
            await MakePages.song_page(cb.message, rand_key, key - 1)
