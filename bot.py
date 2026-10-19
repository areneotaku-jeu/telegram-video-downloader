"""
Bot Telegram : télécharge une vidéo / une musique / une image à partir d'un lien

1. Envoie le lien au bot (tu peux ajouter ton propre texte après le lien).
2. Le bot demande le format : Vidéo, Musique ou Image.
3. Il télécharge (sans filigrane quand la source le permet) et te renvoie le
   fichier, puis la description et les hashtags d'origine.
"""
import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])  # ton ID Telegram

URL_RE = re.compile(r"https?://\S+")
# Serveur Bot API local (optionnel) : ex. http://localhost:8081
# Sans lui, Telegram limite les envois à 50 Mo ; avec lui, jusqu'à 2000 Mo.
LOCAL_API_URL = os.environ.get("LOCAL_API_URL", "").rstrip("/")
MAX_SIZE = (2000 if LOCAL_API_URL else 50) * 1024 * 1024
MAX_MB = MAX_SIZE // (1024 * 1024)
EXT = {"video": "mp4", "audio": "mp3", "image": "jpg"}
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")


def download(url: str, kind: str, folder: str):
    base = {
        "outtmpl": os.path.join(folder, "media.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
    }
    if kind == "video":
        opts = {
            **base,
            # on évite le format "download" de TikTok, qui contient le filigrane
            "format": (
                "best[format_id!=download][ext=mp4]/"
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "best[format_id!=download]/best"
            ),
            "merge_output_format": "mp4",
        }
    elif kind == "audio":
        opts = {
            **base,
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
        }
    else:  # image = miniature / couverture
        opts = {
            **base,
            "skip_download": True,
            "writethumbnail": True,
            "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg"}],
        }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    caption = (info.get("description") or info.get("title") or "").strip()
    tags = info.get("tags") or []
    if tags and "#" not in caption:
        caption += "\n" + " ".join(f"#{t}" for t in tags)

    found = list(Path(folder).glob(f"*.{EXT[kind]}"))
    if not found:
        raise RuntimeError("Aucun fichier récupéré pour ce lien.")
    return str(found[0]), caption


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.from_user.id != ALLOWED_USER_ID:
        return

    text = msg.text or ""
    m = URL_RE.search(text)
    if not m:
        await msg.reply_text("Envoie-moi un lien.")
        return

    key = str(msg.message_id)
    context.bot_data[key] = {
        "url": m.group(0),
        "custom": (text[: m.start()] + text[m.end():]).strip(),
    }
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🎬 Vidéo", callback_data=f"video|{key}"),
            InlineKeyboardButton("🎵 Musique", callback_data=f"audio|{key}"),
            InlineKeyboardButton("🖼️ Image", callback_data=f"image|{key}"),
        ]]
    )
    await msg.reply_text("Quel format veux-tu ?", reply_markup=keyboard)


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ALLOWED_USER_ID:
        return
    await q.answer()

    kind, key = q.data.split("|", 1)
    job = context.bot_data.get(key)
    if not job:
        await q.edit_message_text("Lien expiré, renvoie-le-moi.")
        return

    chat_id = q.message.chat_id
    await q.edit_message_text("⏬ Téléchargement…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, original_caption = await asyncio.to_thread(download, job["url"], kind, tmp)
            size = os.path.getsize(path)
            if size > MAX_SIZE:
                await q.edit_message_text(f"❌ Fichier trop lourd (> {MAX_MB} Mo) pour Telegram.")
                return

            with open(path, "rb") as f:
                if kind == "video":
                    await context.bot.send_video(chat_id, f, supports_streaming=True)
                elif kind == "audio":
                    await context.bot.send_audio(chat_id, f)
                elif size > 10 * 1024 * 1024:
                    await context.bot.send_document(chat_id, f)
                else:
                    await context.bot.send_photo(chat_id, f)

        context.bot_data.pop(key, None)
        await q.message.delete()
        caption = job["custom"] or original_caption
        if caption and kind != "audio":
            await context.bot.send_message(chat_id, caption)
    except Exception as e:
        log.exception("Erreur")
        await q.edit_message_text(f"❌ Erreur : {e}")


def main():
    builder = Application.builder().token(TELEGRAM_TOKEN)
    if LOCAL_API_URL:
        builder = (
            builder.base_url(f"{LOCAL_API_URL}/bot")
            .base_file_url(f"{LOCAL_API_URL}/file/bot")
            .local_mode(True)
            .read_timeout(600)
            .write_timeout(600)
        )
    app = builder.build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_choice))
    app.run_polling()


if __name__ == "__main__":
    main()
