import os
import re
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# Telegram media downloader: TikTok, Facebook, YouTube
# Flow: user sends a link -> chooses VIDEO or AUDIO -> bot downloads.
#
# Configure BOT_TOKEN as an environment variable. Never publish it.
# Downloads up to 1 GB are attempted where source/storage permit.
# Telegram's standard Bot API may not support sending a 1 GB file.
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_SIZE_BYTES = 1_000_000_000
MIN_FREE_SPACE_BYTES = 1_300_000_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("media_downloader")

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

PLATFORMS = {
    "TikTok": ("tiktok.com",),
    "Facebook": ("facebook.com", "fb.watch", "fb.com"),
    "YouTube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
}


def detect_platform(url: str):
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for name, domains in PLATFORMS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return name
    return None


def download_media(url: str, output_dir: str, media_type: str):
    """Download a video or extract audio; returns (filepath, title, platform)."""
    output = Path(output_dir)
    platform = detect_platform(url)
    if not platform:
        raise ValueError("Lien non pris en charge.")

    def progress_hook(data):
        if data.get("status") == "downloading":
            downloaded = data.get("downloaded_bytes", 0)
            if downloaded > MAX_SIZE_BYTES:
                raise ValueError("La taille dépasse 1 Go.")
            if shutil.disk_usage(output).free < 150_000_000:
                raise OSError("Espace disque presque épuisé.")

    options = {
        "outtmpl": str(output / "%(title).100B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "max_filesize": MAX_SIZE_BYTES,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }

    if media_type == "audio":
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    else:
        options["format"] = "best[ext=mp4]/best"

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = Path(ydl.prepare_filename(info))
        title = info.get("title") or ("Audio" if media_type == "audio" else "Vidéo")

    # Audio post-processing changes extension to .mp3; format merging can also
    # change video extension, so locate the actual resulting file if needed.
    if media_type == "audio":
        expected = prepared.with_suffix(".mp3")
        if expected.is_file():
            filename = expected
        else:
            candidates = [
                p for p in output.iterdir()
                if p.is_file() and p.suffix.lower() in {".mp3", ".m4a", ".opus", ".wav"}
            ]
            if not candidates:
                raise FileNotFoundError("Fichier audio introuvable après conversion.")
            filename = max(candidates, key=lambda p: p.stat().st_mtime)
    elif prepared.is_file():
        filename = prepared
    else:
        candidates = [
            p for p in output.iterdir()
            if p.is_file() and p.stat().st_size > 0
        ]
        if not candidates:
            raise FileNotFoundError("Fichier vidéo introuvable après téléchargement.")
        filename = max(candidates, key=lambda p: p.stat().st_mtime)

    if filename.stat().st_size > MAX_SIZE_BYTES:
        raise ValueError("Le fichier final dépasse la limite de 1 Go.")

    return str(filename), title, platform


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎬 Bienvenue sur le téléchargeur multimédia !\n\n"
        "Plateformes : TikTok, Facebook et YouTube.\n\n"
        "🔗 Envoie un lien et je te proposerai de choisir :\n"
        "🎥 Vidéo\n🎵 Audio MP3\n\n"
        "📦 Taille de téléchargement visée : jusqu'à 1 Go.\n"
        "L'envoi d'un fichier de cette taille dépend des limites de Telegram."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Envoie un lien TikTok, Facebook ou YouTube. "
        "Choisis ensuite Vidéo ou Audio.\n\n"
        "Les contenus privés ou protégés peuvent être indisponibles. "
        "Respecte les droits d'auteur et les conditions des plateformes."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    match = URL_RE.search(message.text or "")
    if not match:
        await message.reply_text("❌ Envoie un lien vidéo ou audio valide.")
        return

    url = match.group(0).rstrip(".,!?)]}")
    platform = detect_platform(url)
    if not platform:
        await message.reply_text(
            "❌ Lien non reconnu. Utilise TikTok, Facebook ou YouTube."
        )
        return

    # Keep the URL in per-user chat data until they choose a format.
    context.user_data["pending_media_url"] = url
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎥 Télécharger la vidéo", callback_data="media:video"),
            InlineKeyboardButton("🎵 Extraire l'audio MP3", callback_data="media:audio"),
        ],
        [InlineKeyboardButton("❌ Annuler", callback_data="media:cancel")],
    ])
    await message.reply_text(
        f"✅ Lien {platform} détecté !\n\n"
        "Que veux-tu télécharger ?",
        reply_markup=keyboard,
    )


async def choose_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "media:cancel":
        context.user_data.pop("pending_media_url", None)
        await query.edit_message_text("❎ Opération annulée.")
        return

    url = context.user_data.get("pending_media_url")
    if not url:
        await query.edit_message_text(
            "⚠️ Le lien a expiré. Envoie-le de nouveau."
        )
        return

    media_type = "audio" if query.data == "media:audio" else "video"
    platform = detect_platform(url)
    label = "audio MP3" if media_type == "audio" else "vidéo"
    await query.edit_message_text(
        f"⏳ Téléchargement de la {label} depuis {platform}…"
    )

    try:
        with tempfile.TemporaryDirectory(prefix="tg_media_") as folder:
            if shutil.disk_usage(folder).free < MIN_FREE_SPACE_BYTES:
                raise OSError("Espace libre insuffisant pour un téléchargement pouvant atteindre 1 Go.")

            filename, title, platform = await asyncio.to_thread(
                download_media, url, folder, media_type
            )
            size = os.path.getsize(filename)

            await query.message.reply_text(
                f"✅ {label.capitalize()} prêt !\n"
                f"📄 {title[:160]}\n"
                f"📦 {size / 1_000_000:.1f} Mo\n"
                "📤 Envoi vers Telegram…"
            )

            with open(filename, "rb") as media:
                if media_type == "audio":
                    await query.message.reply_audio(
                        audio=media,
                        filename=Path(filename).name[:200],
                        title=title[:64],
                        caption=f"🎵 {title[:180]} | Source : {platform}",
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=60,
                        pool_timeout=60,
                    )
                else:
                    await query.message.reply_document(
                        document=media,
                        filename=Path(filename).name[:200],
                        caption=f"🎬 {title[:180]}\n📥 Source : {platform}\n📦 {size / 1_000_000:.1f} Mo",
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=60,
                        pool_timeout=60,
                    )

        context.user_data.pop("pending_media_url", None)
        await query.message.reply_text("✅ Terminé ! Envoie un autre lien quand tu veux.")

    except Exception as exc:
        logger.exception("Media processing failed")
        detail = str(exc).lower()
        if "file is too large" in detail or "request entity too large" in detail or "file too big" in detail:
            reply = (
                "❌ Telegram a refusé l'envoi à cause de la taille. "
                "L'API Bot standard ne garantit pas l'envoi de fichiers de 1 Go."
            )
        elif "espace" in detail or "disk" in detail or "no space" in detail:
            reply = "❌ Espace de stockage insuffisant sur l'appareil ou le serveur."
        elif "1 go" in detail or "max_filesize" in detail or "maximum file size" in detail:
            reply = "❌ Le fichier dépasse la limite configurée de 1 Go."
        else:
            reply = (
                "❌ Impossible de télécharger ou d'envoyer ce média. "
                "Vérifie le lien, l'espace libre, FFmpeg et les limites de la plateforme."
            )
        try:
            await query.message.reply_text(reply)
        except Exception:
            pass


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Token manquant. Définis BOT_TOKEN avec le token reçu de @BotFather. "
            "Ne publie jamais le token sur GitHub."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(choose_media, pattern=r"^media:(video|audio|cancel)$"))

    logger.info("Bot démarré. En attente de liens.")
    app.run_polling()


if __name__ == "__main__":
    main()
